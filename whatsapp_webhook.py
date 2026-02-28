# whatsapp_webhook.py
from __future__ import annotations

import inspect
import os
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
from flask import Flask, request

try:
    from twilio.twiml.messaging_response import MessagingResponse
except Exception:  # pragma: no cover
    MessagingResponse = None  # type: ignore

app = Flask(__name__)

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

# ✅ jouw nieuwe bedragen
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 40, 50}

# paper | live | auto
TRADER_MODE = (os.getenv("TRADER_MODE") or "auto").strip().lower()

# ✅ jij wil max 10 tegelijk in WhatsApp
LIST_LIMIT = int(os.getenv("WHATSAPP_LIST_LIMIT") or "10")
TOP_LIMIT = int(os.getenv("WHATSAPP_TOP_LIMIT") or "10")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(msg, flush=True)


def twiml(text: str):
    if MessagingResponse is None:
        return (text, 200, {"Content-Type": "text/plain; charset=utf-8"})
    resp = MessagingResponse()
    resp.message(text)
    return (str(resp), 200, {"Content-Type": "application/xml"})


def db_connect():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def as_aware_utc(dt: Any) -> Optional[datetime]:
    if not dt or not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def chance_text(ch: Any) -> str:
    c = safe_int(ch, 0)
    # ✅ jouw labels
    if c >= 85:
        return "kans HEEL GROOT"
    if c >= 75:
        return "kans GROOT"
    if c >= 65:
        return "kans GOED"
    return "kans LAGER"


def fmt_price(x: Any) -> str:
    v = safe_float(x, 0.0)
    if v == 0:
        return "-"
    # compact maar duidelijk
    return f"{v:.6f}"


def column_exists(conn, table: str, col: str, schema: str = "public") -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS(
              SELECT 1
              FROM information_schema.columns
              WHERE table_schema=%s AND table_name=%s AND column_name=%s
            )
            """,
            (schema, table, col),
        )
        return bool(cur.fetchone()[0])


@app.get("/")
def root():
    return "OK", 200


@app.get("/healthz")
def healthz():
    return "OK", 200


# ---------------- DB ----------------
def _select_cols(conn) -> str:
    # ✅ raw_score alleen meenemen als kolom bestaat (anders crash)
    has_raw = column_exists(conn, "pending_approvals", "raw_score")
    raw_sql = ", raw_score" if has_raw else ""
    return f"""
      id, symbol, setup_type, timeframe, regime,
      score{raw_sql}, chance, confidence,
      entry, stop, target,
      status, created_at, expires_at
    """


def fetch_prebuys(conn, *, limit: int = 10, include_expired: bool = False) -> List[Dict[str, Any]]:
    extra_expired = "" if include_expired else "AND (expires_at IS NULL OR expires_at > NOW())"
    cols = _select_cols(conn)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT {cols}
            FROM public.pending_approvals
            WHERE COALESCE(status,'PENDING') IN ('PENDING','APPROVED')
              {extra_expired}
            ORDER BY
              COALESCE(chance,0) DESC,
              created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def stats(conn) -> Dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE COALESCE(status,'PENDING') IN ('PENDING','APPROVED'))::int AS pending_or_approved,
              COUNT(*) FILTER (
                WHERE COALESCE(status,'PENDING') IN ('PENDING','APPROVED')
                  AND (expires_at IS NULL OR expires_at > NOW())
              )::int AS active_now,
              COUNT(*) FILTER (
                WHERE COALESCE(status,'PENDING') IN ('PENDING','APPROVED')
                  AND expires_at IS NOT NULL
                  AND expires_at <= NOW()
              )::int AS expired_now
            FROM public.pending_approvals
            """
        )
        row = cur.fetchone() or (0, 0, 0)
    return {"pending_or_approved": row[0], "active_now": row[1], "expired_now": row[2]}


def get_pending_by_id(conn, prebuy_id: str) -> Optional[Dict[str, Any]]:
    cols = _select_cols(conn)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT {cols}
            FROM public.pending_approvals
            WHERE id = %s
            """,
            (prebuy_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_top_pending(conn) -> Optional[Dict[str, Any]]:
    rows = fetch_prebuys(conn, limit=1, include_expired=False)
    return rows[0] if rows else None


def mark_rejected(conn, prebuy_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.pending_approvals
            SET status='REJECTED', rejected_at=NOW()
            WHERE id=%s
            """,
            (prebuy_id,),
        )
    conn.commit()


def mark_approved(conn, prebuy_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.pending_approvals
            SET status='APPROVED', approved_at=NOW()
            WHERE id=%s
            """,
            (prebuy_id,),
        )
    conn.commit()


def mark_consumed(conn, prebuy_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.pending_approvals
            SET status='CONSUMED', consumed_at=NOW()
            WHERE id=%s
            """,
            (prebuy_id,),
        )
    conn.commit()


# ---------------- TRADER ----------------
def _call_buy_compat(buy_fn, symbol: str, amount_eur: float, meta: Dict[str, Any]) -> Any:
    try:
        sig = inspect.signature(buy_fn)
        params = sig.parameters
    except Exception:
        return buy_fn(symbol, amount_eur)

    args = [symbol, amount_eur]
    kwargs: Dict[str, Any] = {}

    if "meta" in params:
        kwargs["meta"] = meta
    if "prebuy_id" in params:
        kwargs["prebuy_id"] = meta.get("prebuy_id")
    if "entry" in params:
        kwargs["entry"] = meta.get("entry")
    if "stop" in params:
        kwargs["stop"] = meta.get("stop")
    if "stop_loss" in params:
        kwargs["stop_loss"] = meta.get("stop")
    if "target" in params:
        kwargs["target"] = meta.get("target")

    return buy_fn(*args, **kwargs)


def _get_buy_fn(module_path: str):
    mod = __import__(module_path, fromlist=["buy_eur"])
    fn = getattr(mod, "buy_eur", None)
    if not callable(fn):
        raise AttributeError(f"{module_path}.buy_eur ontbreekt")
    return fn


def execute_buy(prebuy: Dict[str, Any], amount_eur: int) -> Tuple[bool, str]:
    symbol = (prebuy.get("symbol") or "").strip()
    entry = float(prebuy.get("entry") or 0.0)
    stop = float(prebuy.get("stop") or 0.0)
    target = float(prebuy.get("target") or 0.0)
    prebuy_id = str(prebuy.get("id") or "").strip()

    if not symbol:
        return False, "BUY faalde: symbol ontbreekt."

    meta = {"prebuy_id": prebuy_id, "entry": entry, "stop": stop, "target": target}

    def try_one(mode: str) -> Tuple[bool, str]:
        module_path = "trading.paper_trader" if mode == "paper" else "trading.live_trader"
        try:
            buy_fn = _get_buy_fn(module_path)
        except Exception as e:
            return False, f"{mode} trader niet beschikbaar: {type(e).__name__}: {e}"

        try:
            res = _call_buy_compat(buy_fn, symbol, float(amount_eur), meta)
            if isinstance(res, dict):
                if res.get("ok") is True:
                    return True, f"BUY uitgevoerd ({mode}) {symbol} €{amount_eur}"
                return False, f"{mode} BUY faalde: {res}"
            return True, f"BUY uitgevoerd ({mode}) {symbol} €{amount_eur}"
        except Exception as e:
            return False, f"{mode} buy error: {type(e).__name__}: {e}"

    if TRADER_MODE == "paper":
        return try_one("paper")
    if TRADER_MODE == "live":
        return try_one("live")

    ok, msg = try_one("paper")
    if ok:
        return True, msg

    ok2, msg2 = try_one("live")
    if ok2:
        return True, msg2

    return False, "BUY NIET uitgevoerd (paper én live faalden)."


# ---------------- COMMANDS ----------------
def parse_command(text: str) -> Tuple[str, List[str]]:
    t = (text or "").strip()
    if not t:
        return "HELP", []
    parts = t.split()
    return parts[0].upper(), parts[1:]


def fmt_prebuy_row(p: Dict[str, Any]) -> str:
    st = (p.get("status") or "PENDING").upper()
    ch = p.get("chance")
    return (
        f"{p.get('id')} | {p.get('symbol')} | {chance_text(ch)} | chance={ch} | score={p.get('score')} | "
        f"{p.get('setup_type')} | entry={fmt_price(p.get('entry'))}"
    )


def fmt_prebuy_full(p: Dict[str, Any]) -> str:
    st = (p.get("status") or "PENDING").upper()
    exp = as_aware_utc(p.get("expires_at"))
    exp_s = exp.strftime("%Y-%m-%d %H:%M UTC") if exp else "-"
    raw = p.get("raw_score", None)
    raw_s = str(raw) if raw is not None else "-"

    return (
        f"📌 PRE-BUY DETAIL\n"
        f"ID: {p.get('id')}\n"
        f"COIN: {p.get('symbol')}\n"
        f"status: {st}\n"
        f"timeframe: {p.get('timeframe')}\n"
        f"setup: {p.get('setup_type')}\n"
        f"regime: {p.get('regime')}\n"
        f"score: {p.get('score')} (raw={raw_s})\n"
        f"{chance_text(p.get('chance'))} (chance={p.get('chance')})\n"
        f"entry:  {fmt_price(p.get('entry'))}\n"
        f"stop:   {fmt_price(p.get('stop'))}\n"
        f"target: {fmt_price(p.get('target'))}\n"
        f"expires: {exp_s}\n\n"
        f"Kopen: YES <bedrag> {p.get('id')}\n"
        f"Bedragen: 5/10/15/20/30/40/50"
    )


HELP_TEXT = (
    "Commands:\n"
    "HELP\n"
    "LIST (laat max 10 ACTIVE zien)\n"
    "LISTALL (ook EXPIRED)\n"
    "TOP (top 10 hoogste chance - ACTIVE)\n"
    "STATS\n"
    "YES <bedrag> [ID]  (zonder ID = pakt TOP 1)\n"
    "NO <ID>\n"
    "Of stuur gewoon een ID (PB-...) voor volledige details\n\n"
    "Bedragen: 5/10/15/20/30/40/50\n"
    "TRADER_MODE=paper|live|auto"
)


@app.post("/whatsapp")
def whatsapp():
    try:
        body = (request.values.get("Body") or "").strip()
        cmd, args = parse_command(body)

        if not DATABASE_URL:
            return twiml("DATABASE_URL ontbreekt in Render Environment.")

        with db_connect() as conn:
            # ✅ Als user direct een PB-ID stuurt: details terug
            if body.startswith("PB-") and len(body) > 8:
                p = get_pending_by_id(conn, body.strip())
                if not p:
                    return twiml("ID niet gevonden.")
                return twiml(fmt_prebuy_full(p))

            if cmd in ("HELP", "?"):
                return twiml(HELP_TEXT)

            if cmd == "STATS":
                s = stats(conn)
                return twiml(
                    "STATS:\n"
                    f"pending_or_approved = {s['pending_or_approved']}\n"
                    f"active_now = {s['active_now']}\n"
                    f"expired_now = {s['expired_now']}"
                )

            if cmd == "LIST":
                pending = fetch_prebuys(conn, limit=LIST_LIMIT, include_expired=False)
                s = stats(conn)
                if not pending:
                    return twiml(
                        "Geen ACTIVE pending/approved Pre-BUYs.\n"
                        f"(expired_now={s['expired_now']})\n\n"
                        "Tip: STATS of LISTALL"
                    )
                lines = [f"Pre-BUYs (ACTIVE) – max {LIST_LIMIT}:"]
                lines += [fmt_prebuy_row(p) for p in pending]
                lines.append("\nTip: stuur een ID (PB-...) voor alle details.")
                return twiml("\n".join(lines))

            if cmd == "LISTALL":
                pending = fetch_prebuys(conn, limit=LIST_LIMIT, include_expired=True)
                if not pending:
                    return twiml("Geen pending/approved records gevonden.")
                lines = [f"Pre-BUYs (PENDING/APPROVED incl EXPIRED) – max {LIST_LIMIT}:"]
                lines += [fmt_prebuy_row(p) for p in pending]
                return twiml("\n".join(lines))

            if cmd == "TOP":
                pending = fetch_prebuys(conn, limit=TOP_LIMIT, include_expired=False)
                if not pending:
                    s = stats(conn)
                    return twiml(
                        "Geen ACTIVE pending/approved Pre-BUYs.\n"
                        f"(expired_now={s['expired_now']})\n\n"
                        "Tip: STATS"
                    )
                lines = [f"TOP {TOP_LIMIT} (beste kansen) – ACTIVE:"]
                lines += [fmt_prebuy_row(p) for p in pending]
                lines.append("\nTip: stuur een ID (PB-...) voor alle details.")
                return twiml("\n".join(lines))

            if cmd == "NO":
                if not args:
                    return twiml("Gebruik: NO <ID>")
                prebuy_id = args[0].strip()
                p = get_pending_by_id(conn, prebuy_id)
                if not p:
                    return twiml("ID niet gevonden.")
                mark_rejected(conn, prebuy_id)
                return twiml(f"Afgewezen: {prebuy_id}")

            if cmd == "YES":
                if len(args) < 1:
                    return twiml("Gebruik: YES <bedrag> [ID]")

                amount = safe_int(args[0], 0)
                if amount not in ALLOWED_AMOUNTS:
                    return twiml("Bedrag ongeldig. Toegestaan: 5/10/15/20/30/40/50")

                if len(args) >= 2:
                    prebuy_id = args[1].strip()
                    p = get_pending_by_id(conn, prebuy_id)
                    if not p:
                        return twiml("ID niet gevonden.")
                else:
                    p = get_top_pending(conn)
                    if not p:
                        return twiml("Geen ACTIVE pending/approved Pre-BUYs om te keuren.")
                    prebuy_id = str(p.get("id"))

                status = (p.get("status") or "PENDING").upper()
                if status not in ("PENDING", "APPROVED"):
                    return twiml(f"Kan niet YES doen: status is {status}")

                exp = as_aware_utc(p.get("expires_at"))
                if exp and exp <= now_utc():
                    return twiml(
                        "Deze Pre-BUY is verlopen.\n"
                        f"ID: {prebuy_id}\n\n"
                        "Wacht op nieuwe TOP/LIST."
                    )

                mark_approved(conn, prebuy_id)

                ok, msg = execute_buy(p, amount)
                if ok:
                    mark_consumed(conn, prebuy_id)
                    return twiml(f"GOEDGEKEURD ✅\n{msg}\nID: {prebuy_id}")

                return twiml(
                    f"GOEDGEKEURD ✅\nMaar BUY faalde:\n{msg}\nID: {prebuy_id}\n\n"
                    f"TIP: probeer nogmaals YES <bedrag> {prebuy_id}"
                )

            return twiml("Onbekend command.\n\n" + HELP_TEXT)

    except Exception as e:
        log("❌ ERROR in /whatsapp")
        log(str(e))
        log(traceback.format_exc())
        return twiml("Interne fout. Check Render logs.")


if __name__ == "__main__":
    port = int(os.getenv("PORT") or "10000")
    app.run(host="0.0.0.0", port=port)
