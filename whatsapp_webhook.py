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

# ✅ bedragen aangepast (jouw wens)
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 40, 50}

# paper | live | auto
TRADER_MODE = (os.getenv("TRADER_MODE") or "auto").strip().lower()


# ---------------- basics ----------------
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


def table_has_column(conn, table: str, col: str, schema: str = "public") -> bool:
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


# ---------------- label helpers ----------------
def kans_tekst(chance: int) -> str:
    if chance >= 85:
        return "Kans heel groot"
    if chance >= 75:
        return "Kans groot"
    if chance >= 65:
        return "Kans goed"
    return "Kans laag"


def fmt_trade_card(p: Dict[str, Any]) -> str:
    prebuy_id = str(p.get("id") or "-")
    symbol = str(p.get("symbol") or "-")
    setup = str(p.get("setup_type") or "-")
    tf = str(p.get("timeframe") or "-")
    regime = str(p.get("regime") or "-")
    status = str((p.get("status") or "PENDING")).upper()

    score = safe_int(p.get("score"), 0)
    chance = safe_int(p.get("chance"), 0)
    conf = safe_int(p.get("confidence"), chance)

    entry = safe_float(p.get("entry"), 0.0)
    stop = safe_float(p.get("stop"), 0.0)
    target = safe_float(p.get("target"), 0.0)

    exp = as_aware_utc(p.get("expires_at"))
    exp_s = exp.strftime("%Y-%m-%d %H:%M UTC") if exp else "-"

    extra_raw = ""
    if p.get("raw_score") is not None:
        extra_raw = f" (raw={safe_int(p.get('raw_score'), 0)})"

    return (
        f"📌 TRADE DETAILS\n"
        f"Coin: {symbol}\n"
        f"ID: {prebuy_id}\n"
        f"Status: {status}\n"
        f"Setup: {setup}\n"
        f"Timeframe: {tf}\n"
        f"Regime: {regime}\n\n"
        f"Score: {score}{extra_raw}\n"
        f"Chance: {chance} → {kans_tekst(chance)}\n"
        f"Confidence: {conf}\n\n"
        f"Entry: {entry}\n"
        f"Stop: {stop}\n"
        f"Target: {target}\n"
        f"Expires: {exp_s}\n\n"
        f"Keuren: YES <bedrag> {prebuy_id}\n"
        f"Afwijzen: NO {prebuy_id}"
    )


def fmt_prebuy_row(p: Dict[str, Any]) -> str:
    prebuy_id = str(p.get("id") or "-")
    symbol = str(p.get("symbol") or "-")
    status = str((p.get("status") or "PENDING")).upper()
    chance = safe_int(p.get("chance"), 0)
    score = safe_int(p.get("score"), 0)
    tf = str(p.get("timeframe") or "-")
    setup = str(p.get("setup_type") or "-")

    return (
        f"{prebuy_id} | {symbol} | status={status} | "
        f"{kans_tekst(chance)} | chance={chance} score={score} | {setup} | tf={tf}"
    )


# ---------------- DB: fetch ----------------
def _select_cols(conn) -> List[str]:
    cols = [
        "id",
        "symbol",
        "setup_type",
        "regime",
        "score",
        "chance",
        "confidence",
        "entry",
        "stop",
        "target",
        "status",
        "created_at",
        "expires_at",
    ]

    if table_has_column(conn, "pending_approvals", "timeframe"):
        cols.insert(3, "timeframe")

    if table_has_column(conn, "pending_approvals", "raw_score"):
        cols.insert(cols.index("score") + 1, "raw_score")

    return cols


def fetch_prebuys_list(conn, *, limit: int = 10, include_expired: bool = False) -> List[Dict[str, Any]]:
    extra_expired = "" if include_expired else "AND (expires_at IS NULL OR expires_at > NOW())"
    cols = _select_cols(conn)
    col_sql = ", ".join(cols)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT {col_sql}
            FROM public.pending_approvals
            WHERE COALESCE(status,'PENDING') IN ('PENDING','APPROVED')
              {extra_expired}
            ORDER BY
              CASE WHEN COALESCE(status,'PENDING')='PENDING' THEN 0 ELSE 1 END,
              created_at ASC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def fetch_prebuys_top(conn, *, limit: int = 10, include_expired: bool = False) -> List[Dict[str, Any]]:
    extra_expired = "" if include_expired else "AND (expires_at IS NULL OR expires_at > NOW())"
    cols = _select_cols(conn)
    col_sql = ", ".join(cols)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT {col_sql}
            FROM public.pending_approvals
            WHERE COALESCE(status,'PENDING') IN ('PENDING','APPROVED')
              {extra_expired}
            ORDER BY
              CASE WHEN COALESCE(status,'PENDING')='PENDING' THEN 0 ELSE 1 END,
              COALESCE(chance,0) DESC,
              created_at ASC
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

    return {
        "pending_or_approved": row[0],
        "active_now": row[1],
        "expired_now": row[2],
    }


def get_pending_by_id(conn, prebuy_id: str) -> Optional[Dict[str, Any]]:
    cols = _select_cols(conn)
    col_sql = ", ".join(cols)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT {col_sql}
            FROM public.pending_approvals
            WHERE id = %s
            LIMIT 1
            """,
            (prebuy_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_top_pending(conn) -> Optional[Dict[str, Any]]:
    rows = fetch_prebuys_top(conn, limit=1, include_expired=False)
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

    meta = {
        "prebuy_id": prebuy_id,
        "entry": entry,
        "stop": stop,
        "target": target,
    }

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

    if len(parts) == 1 and parts[0].upper().startswith("PB-"):
        return "SHOW", [parts[0]]

    return parts[0].upper(), parts[1:]


HELP_TEXT = (
    "Commands:\n"
    "HELP\n"
    "LIST (max 10 ACTIVE)\n"
    "TOP (top 10 ACTIVE op chance)\n"
    "STATS\n"
    "SHOW <ID>  (of typ alleen ID)\n"
    "YES <bedrag> [ID]  (zonder ID = pakt TOP 1)\n"
    "NO <ID>\n\n"
    "Bedragen: 5/10/15/20/30/40/50\n"
    "TRADER_MODE=paper|live|auto\n\n"
    "Let op:\n"
    "- Auto-push limiet blokkeert geen LIST/TOP\n"
    "- Dus ook na 50 automatische meldingen kun je nog YES doen op actieve Pre-BUY's"
)


@app.post("/whatsapp")
def whatsapp():
    try:
        body = (request.values.get("Body") or "").strip()
        cmd, args = parse_command(body)

        if not DATABASE_URL:
            return twiml("DATABASE_URL ontbreekt in Render Environment.")

        with db_connect() as conn:
            if cmd in ("HELP", "?"):
                return twiml(HELP_TEXT)

            if cmd == "STATS":
                s = stats(conn)
                return twiml(
                    "STATS:\n"
                    f"pending_or_approved = {s['pending_or_approved']}\n"
                    f"active_now = {s['active_now']}\n"
                    f"expired_now = {s['expired_now']}\n\n"
                    "Auto-push limiet stopt alleen nieuwe automatische meldingen.\n"
                    "LIST/TOP en handmatig YES kunnen nog steeds werken zolang er actieve Pre-BUY's zijn."
                )

            if cmd == "LIST":
                pending = fetch_prebuys_list(conn, limit=10, include_expired=False)
                s = stats(conn)

                if not pending:
                    return twiml(
                        "Geen ACTIVE pending/approved Pre-BUYs.\n"
                        f"(expired_now={s['expired_now']})\n\n"
                        "Tip: STATS of check multi_coin_score logs."
                    )

                lines = ["Pre-BUYs (ACTIVE) — typ ID voor details:"]
                lines += [fmt_prebuy_row(p) for p in pending]
                return twiml("\n".join(lines))

            if cmd == "TOP":
                pending = fetch_prebuys_top(conn, limit=10, include_expired=False)
                s = stats(conn)

                if not pending:
                    return twiml(
                        "Geen ACTIVE pending/approved Pre-BUYs.\n"
                        f"(expired_now={s['expired_now']})\n\n"
                        "Tip: STATS of check multi_coin_score logs."
                    )

                lines = ["TOP 10 (chance) — typ ID voor details:"]
                lines += [fmt_prebuy_row(p) for p in pending]
                return twiml("\n".join(lines))

            if cmd == "SHOW":
                if not args:
                    return twiml("Gebruik: SHOW <ID> (of typ alleen het ID)")

                prebuy_id = args[0].strip()
                p = get_pending_by_id(conn, prebuy_id)
                if not p:
                    return twiml("ID niet gevonden.")
                return twiml(fmt_trade_card(p))

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
                        "Wacht op nieuwe Pre-BUY's of check LIST/TOP later opnieuw."
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
