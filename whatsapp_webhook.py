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

# ✅ jouw gewenste bedragen
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 40, 50}

# paper | live | auto
TRADER_MODE = (os.getenv("TRADER_MODE") or "auto").strip().lower()

# ✅ max items
LIST_LIMIT = int(os.getenv("LIST_LIMIT") or "10")
TOP_LIMIT = int(os.getenv("TOP_LIMIT") or "10")


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


def col_exists(conn, table: str, col: str, schema: str = "public") -> bool:
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


# =========================
# Kans-tekst (jouw wording)
# =========================
def chance_label(chance: int) -> str:
    # jij wilde: "kans heel groot / kans groot / kans goed"
    if chance >= 90:
        return "KANS HEEL GROOT"
    if chance >= 80:
        return "KANS GROOT"
    return "KANS GOED"


# =========================
# DB helpers (schema-robust)
# =========================
def _select_expr(conn) -> Dict[str, str]:
    """
    Bouwt veilige SELECT expressies.
    Als kolom ontbreekt -> selecteer NULL of fallback zodat webhook niet crasht.
    """
    have_tf = col_exists(conn, "pending_approvals", "timeframe")
    have_conf = col_exists(conn, "pending_approvals", "confidence")
    have_stop = col_exists(conn, "pending_approvals", "stop")
    have_target = col_exists(conn, "pending_approvals", "target")
    have_entry = col_exists(conn, "pending_approvals", "entry")
    have_score = col_exists(conn, "pending_approvals", "score")
    have_chance = col_exists(conn, "pending_approvals", "chance")
    have_setup = col_exists(conn, "pending_approvals", "setup_type")
    have_regime = col_exists(conn, "pending_approvals", "regime")

    return {
        "timeframe": "timeframe" if have_tf else "NULL::text AS timeframe",
        "confidence": "confidence" if have_conf else "NULL::int AS confidence",
        "stop": "stop" if have_stop else "NULL::float AS stop",
        "target": "target" if have_target else "NULL::float AS target",
        "entry": "entry" if have_entry else "NULL::float AS entry",
        "score": "score" if have_score else "NULL::int AS score",
        "chance": "chance" if have_chance else "NULL::int AS chance",
        "setup_type": "setup_type" if have_setup else "NULL::text AS setup_type",
        "regime": "regime" if have_regime else "NULL::text AS regime",
    }


def fetch_prebuys(conn, *, limit: int = 10, include_expired: bool = False) -> List[Dict[str, Any]]:
    """
    LIST: toon ACTIVE PENDING/APPROVED
    LISTALL: toon ook EXPIRED
    ✅ crash-proof: werkt ook als kolommen ontbreken
    """
    sel = _select_expr(conn)
    extra_expired = "" if include_expired else "AND (expires_at IS NULL OR expires_at > NOW())"

    sql = f"""
        SELECT
          id,
          symbol,
          {sel["setup_type"]},
          {sel["timeframe"]},
          {sel["regime"]},
          {sel["score"]},
          {sel["chance"]},
          {sel["confidence"]},
          {sel["entry"]},
          {sel["stop"]},
          {sel["target"]},
          status, created_at, expires_at
        FROM public.pending_approvals
        WHERE COALESCE(status,'PENDING') IN ('PENDING','APPROVED')
          {extra_expired}
        ORDER BY
          CASE WHEN COALESCE(status,'PENDING')='PENDING' THEN 0 ELSE 1 END,
          COALESCE(chance,0) DESC,
          created_at ASC
        LIMIT %s
    """

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (limit,))
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
    sel = _select_expr(conn)
    sql = f"""
        SELECT
          id,
          symbol,
          {sel["setup_type"]},
          {sel["timeframe"]},
          {sel["regime"]},
          {sel["score"]},
          {sel["chance"]},
          {sel["confidence"]},
          {sel["entry"]},
          {sel["stop"]},
          {sel["target"]},
          status, created_at, expires_at
        FROM public.pending_approvals
        WHERE id = %s
        LIMIT 1
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (prebuy_id,))
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


# =========================
# TRADER
# =========================
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
    entry = safe_float(prebuy.get("entry"), 0.0)
    stop = safe_float(prebuy.get("stop"), 0.0)
    target = safe_float(prebuy.get("target"), 0.0)
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


# =========================
# COMMANDS
# =========================
def parse_command(text: str) -> Tuple[str, List[str]]:
    t = (text or "").strip()
    if not t:
        return "HELP", []
    parts = t.split()
    return parts[0].upper(), parts[1:]


def looks_like_prebuy_id(s: str) -> bool:
    # jouw IDs zijn vaak PB-SYMBOL-YYYYMMDD-HHMMSS-xxxxxx
    s = (s or "").strip()
    return s.startswith("PB-") and len(s) >= 10


def fmt_prebuy_row(p: Dict[str, Any]) -> str:
    st = (p.get("status") or "PENDING").upper()

    symbol = (p.get("symbol") or "-").upper()
    tf = p.get("timeframe") or "-"
    setup = p.get("setup_type") or "-"
    regime = p.get("regime") or "-"

    score = safe_int(p.get("score"), 0)
    chance = safe_int(p.get("chance"), 0)

    exp = p.get("expires_at")
    exp_s = exp.strftime("%Y-%m-%d %H:%M UTC") if isinstance(exp, datetime) else "-"

    return (
        f"{p.get('id')} | {symbol} | {chance_label(chance)} ({chance}) | score={score} "
        f"| tf={tf} | {setup} | regime={regime} | exp={exp_s} | status={st}"
    )


def fmt_prebuy_full(p: Dict[str, Any]) -> str:
    st = (p.get("status") or "PENDING").upper()
    symbol = (p.get("symbol") or "-").upper()
    tf = p.get("timeframe") or "-"
    setup = p.get("setup_type") or "-"
    regime = p.get("regime") or "-"

    score = safe_int(p.get("score"), 0)
    chance = safe_int(p.get("chance"), 0)
    conf = p.get("confidence")
    conf_txt = f"{safe_int(conf, 0)}" if conf is not None else "-"

    entry = safe_float(p.get("entry"), 0.0)
    stop = safe_float(p.get("stop"), 0.0)
    target = safe_float(p.get("target"), 0.0)

    created = p.get("created_at")
    created_s = created.strftime("%Y-%m-%d %H:%M UTC") if isinstance(created, datetime) else "-"

    exp = p.get("expires_at")
    exp_s = exp.strftime("%Y-%m-%d %H:%M UTC") if isinstance(exp, datetime) else "-"

    return (
        f"📌 PRE-BUY DETAIL\n"
        f"ID: {p.get('id')}\n"
        f"COIN: {symbol}\n"
        f"Status: {st}\n"
        f"Timeframe: {tf}\n"
        f"Setup: {setup}\n"
        f"Regime: {regime}\n\n"
        f"Score: {score}\n"
        f"Chance: {chance_label(chance)} ({chance})\n"
        f"Confidence: {conf_txt}\n\n"
        f"Entry: {entry:.6f}\n"
        f"Stop:  {stop:.6f}\n"
        f"Target:{target:.6f}\n\n"
        f"Created: {created_s}\n"
        f"Expires: {exp_s}\n\n"
        f"Kopen: YES <bedrag> {p.get('id')}\n"
        f"Afwijzen: NO {p.get('id')}"
    )


HELP_TEXT = (
    "Commands:\n"
    "HELP\n"
    "LIST (max 10 ACTIVE)\n"
    "LISTALL (incl EXPIRED)\n"
    "TOP (top 10 chance - ACTIVE)\n"
    "STATS\n"
    "YES <bedrag> [ID]\n"
    "NO <ID>\n"
    "SHOW <ID>  (of stuur alleen ID)\n\n"
    "Bedragen: 5/10/15/20/30/40/50\n"
    "TRADER_MODE=paper|live|auto"
)


@app.post("/whatsapp")
def whatsapp():
    try:
        body = (request.values.get("Body") or "").strip()

        if not DATABASE_URL:
            return twiml("DATABASE_URL ontbreekt in Render Environment.")

        # ✅ extra: als user alleen een ID stuurt -> detail
        if looks_like_prebuy_id(body):
            cmd = "SHOW"
            args = [body.strip()]
        else:
            cmd, args = parse_command(body)

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
                    "Als active_now=0 en expired_now>0, dan zijn je Pre-BUYs verlopen."
                )

            if cmd == "LIST":
                pending = fetch_prebuys(conn, limit=LIST_LIMIT, include_expired=False)
                s = stats(conn)
                if not pending:
                    return twiml(
                        "Geen ACTIVE pending/approved Pre-BUYs.\n"
                        f"(expired_now={s['expired_now']})\n\n"
                        "Tip: STATS of LISTALL."
                    )
                lines = [f"Pre-BUYs (ACTIVE) max {LIST_LIMIT}:"]
                lines += [fmt_prebuy_row(p) for p in pending]
                lines.append("\nTip: stuur een ID los om alle info te zien.")
                return twiml("\n".join(lines))

            if cmd == "LISTALL":
                pending = fetch_prebuys(conn, limit=LIST_LIMIT, include_expired=True)
                if not pending:
                    return twiml("Geen pending/approved records gevonden.")
                lines = [f"Pre-BUYs (incl EXPIRED) max {LIST_LIMIT}:"]
                lines += [fmt_prebuy_row(p) for p in pending]
                lines.append("\nTip: stuur een ID los om alle info te zien.")
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
                lines = [f"TOP {TOP_LIMIT} (chance) - ACTIVE:"]
                lines += [fmt_prebuy_row(p) for p in pending]
                lines.append("\nGebruik: YES <bedrag> [ID]")
                return twiml("\n".join(lines))

            if cmd == "SHOW":
                if not args:
                    return twiml("Gebruik: SHOW <ID>  (of stuur alleen het ID)")
                prebuy_id = args[0].strip()
                p = get_pending_by_id(conn, prebuy_id)
                if not p:
                    return twiml("ID niet gevonden.")
                return twiml(fmt_prebuy_full(p))

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
                        "Tip: wacht op nieuwe Pre-BUYs (expires wordt nu correct gezet in multi_coin_score)."
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
