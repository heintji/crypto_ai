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

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip() or ""

# Allowed manual amounts via WhatsApp
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

# TRADER_MODE:
# - live  -> always execute live
# - paper -> always execute paper
# - auto  -> try live first, then paper fallback
TRADER_MODE = (os.getenv("TRADER_MODE") or "live").strip().lower()

# IMPORTANT:
# If ASYNC_BUY=1, webhook will NOT call Bitvavo directly.
# It will store amount in DB and return immediately (avoids 502 timeouts).
ASYNC_BUY = (os.getenv("ASYNC_BUY") or "1").strip() == "1"

# How many pending items to show
LIST_LIMIT = int(os.getenv("LIST_LIMIT") or "10")


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


def as_aware_utc(dt: Any) -> Optional[datetime]:
    if not dt or not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------- DB utils ----------------
def ensure_schema(conn) -> None:
    """
    Make sure the DB has the fields we need to store approval amounts + errors.
    This keeps everything in Postgres (no /data dependency).
    """
    with conn.cursor() as cur:
        # approved_amount: EUR amount approved via WhatsApp
        cur.execute(
            "ALTER TABLE public.pending_approvals "
            "ADD COLUMN IF NOT EXISTS approved_amount INTEGER"
        )
        # last_buy_error: last error text/json from buy attempt (optional)
        cur.execute(
            "ALTER TABLE public.pending_approvals "
            "ADD COLUMN IF NOT EXISTS last_buy_error TEXT"
        )
        # buy_attempts: count how many times a BUY was attempted
        cur.execute(
            "ALTER TABLE public.pending_approvals "
            "ADD COLUMN IF NOT EXISTS buy_attempts INTEGER NOT NULL DEFAULT 0"
        )
    conn.commit()


def fetch_pending(conn, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Show PENDING + APPROVED (approved can mean: YES given but BUY not done yet / failed).
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
              id, symbol, setup_type, timeframe, regime,
              score, chance, confidence,
              entry, stop, target,
              status, created_at, expires_at,
              approved_amount, buy_attempts
            FROM public.pending_approvals
            WHERE COALESCE(status,'PENDING') IN ('PENDING','APPROVED')
              AND (expires_at IS NULL OR expires_at > NOW())
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


def get_pending_by_id(conn, prebuy_id: str) -> Optional[Dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
              id, symbol, setup_type, timeframe, regime,
              score, chance, confidence,
              entry, stop, target,
              status, created_at, expires_at,
              approved_amount, buy_attempts
            FROM public.pending_approvals
            WHERE id = %s
            """,
            (prebuy_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_top_pending(conn) -> Optional[Dict[str, Any]]:
    rows = fetch_pending(conn, limit=1)
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


def mark_approved(conn, prebuy_id: str, amount_eur: int) -> None:
    """
    Store approved amount so worker can execute exact EUR buy.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.pending_approvals
            SET status='APPROVED',
                approved_at=NOW(),
                approved_amount=%s
            WHERE id=%s
            """,
            (amount_eur, prebuy_id),
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


def mark_buy_failed(conn, prebuy_id: str, err_text: str) -> None:
    """
    Keep status APPROVED but store error + attempts.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.pending_approvals
            SET last_buy_error=%s,
                buy_attempts = COALESCE(buy_attempts,0) + 1
            WHERE id=%s
            """,
            (err_text[:4000], prebuy_id),
        )
    conn.commit()


# ---------------- TRADER (sync fallback) ----------------
def _call_buy_compat(buy_fn, symbol: str, amount_eur: float, meta: Dict[str, Any]) -> Any:
    """
    Keeps compatibility if your buy function signature changes.
    """
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
    try:
        mod = __import__(module_path, fromlist=["buy_eur"])
        fn = getattr(mod, "buy_eur", None)
        if not callable(fn):
            raise AttributeError(f"{module_path}.buy_eur ontbreekt")
        return fn, None
    except Exception as e:
        return None, e


def execute_buy_sync(prebuy: Dict[str, Any], amount_eur: int) -> Tuple[bool, str]:
    """
    Synchronous BUY execution (can timeout). We keep it as optional fallback.
    In production, prefer ASYNC_BUY=1 so worker does this.
    """
    symbol = (prebuy.get("symbol") or "").strip()
    entry = float(prebuy.get("entry") or 0.0)
    stop = float(prebuy.get("stop") or 0.0)
    target = float(prebuy.get("target") or 0.0)
    prebuy_id = str(prebuy.get("id") or "").strip()

    if not symbol:
        return False, "BUY faalde: symbol ontbreekt."

    meta = {"prebuy_id": prebuy_id, "entry": entry, "stop": stop, "target": target}
    attempts: List[Tuple[str, str]] = []

    def try_one(mode: str) -> Tuple[bool, str]:
        module_path = "trading.paper_trader" if mode == "paper" else "trading.live_trader"
        buy_fn, err = _get_buy_fn(module_path)
        if err or buy_fn is None:
            attempts.append((mode, f"{type(err).__name__}: {err}"))
            return False, f"{mode} trader niet beschikbaar: {type(err).__name__}: {err}"

        try:
            res = _call_buy_compat(buy_fn, symbol, float(amount_eur), meta)
            if isinstance(res, dict):
                if res.get("ok") is True:
                    return True, f"BUY uitgevoerd ({mode}) {symbol} €{amount_eur}"
                return False, f"{mode} BUY faalde: {res}"
            return True, f"BUY uitgevoerd ({mode}) {symbol} €{amount_eur} (no-return)"
        except Exception as e:
            attempts.append((mode, f"{type(e).__name__}: {e}"))
            return False, f"{mode} buy error: {type(e).__name__}: {e}"

    # ✅ auto = live-first (jij wil echte buys)
    if TRADER_MODE == "paper":
        return try_one("paper")
    if TRADER_MODE == "live":
        return try_one("live")

    ok, msg = try_one("live")
    if ok:
        return True, msg

    ok2, msg2 = try_one("paper")
    if ok2:
        return True, msg2

    detail = "\n".join([f"{m}_err={e}" for (m, e) in attempts]) if attempts else "geen details"
    return False, "BUY NIET uitgevoerd.\n" + detail


# ---------------- COMMANDS ----------------
def parse_command(text: str) -> Tuple[str, List[str]]:
    t = (text or "").strip()
    if not t:
        return "HELP", []
    parts = t.split()
    return parts[0].upper(), parts[1:]


def fmt_prebuy_row(p: Dict[str, Any]) -> str:
    st = (p.get("status") or "PENDING").upper()
    amt = p.get("approved_amount")
    attempts = p.get("buy_attempts")
    amt_txt = f" | approved_amount={amt}" if amt else ""
    att_txt = f" | attempts={attempts}" if attempts else ""
    return (
        f"{p.get('id')} | {p.get('symbol')} | status={st} | chance={p.get('chance')} "
        f"| score={p.get('score')} | {p.get('setup_type')} | entry={p.get('entry')}{amt_txt}{att_txt}"
    )


HELP_TEXT = (
    "Commands:\n"
    "HELP\n"
    "LIST (laat pending/approved zien)\n"
    "TOP (top 5 hoogste chance)\n"
    "YES <bedrag> [ID]  (zonder ID = pakt TOP 1)\n"
    "NO <ID>\n\n"
    "Bedragen: 5/10/15/20/30/100\n"
    "TRADER_MODE=paper|live|auto\n"
    "ASYNC_BUY=1 (aanrader) -> worker doet BUY, webhook antwoordt meteen"
)


@app.get("/")
def root():
    return "OK", 200


@app.get("/healthz")
def healthz():
    return "OK", 200


@app.post("/whatsapp")
def whatsapp():
    try:
        body = (request.values.get("Body") or "").strip()
        cmd, args = parse_command(body)

        if not DATABASE_URL:
            return twiml("DATABASE_URL ontbreekt in Render Environment.")

        with db_connect() as conn:
            ensure_schema(conn)

            if cmd in ("HELP", "?"):
                return twiml(HELP_TEXT)

            if cmd == "LIST":
                pending = fetch_pending(conn, limit=LIST_LIMIT)
                if not pending:
                    return twiml("Geen pending/approved Pre-BUYs.")
                lines = ["Pre-BUYs (PENDING/APPROVED):"]
                lines += [fmt_prebuy_row(p) for p in pending]
                lines.append("\nGebruik: YES <bedrag> [ID]  of  NO <ID>")
                return twiml("\n".join(lines))

            if cmd == "TOP":
                pending = fetch_pending(conn, limit=5)
                if not pending:
                    return twiml("Geen pending/approved Pre-BUYs.")
                lines = ["TOP 5 (chance):"]
                lines += [fmt_prebuy_row(p) for p in pending]
                lines.append("\nGebruik: YES <bedrag> [ID]")
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
                    return twiml("Bedrag ongeldig. Toegestaan: 5/10/15/20/30/100")

                if len(args) >= 2:
                    prebuy_id = args[1].strip()
                    p = get_pending_by_id(conn, prebuy_id)
                    if not p:
                        return twiml("ID niet gevonden.")
                else:
                    p = get_top_pending(conn)
                    if not p:
                        return twiml("Geen pending/approved Pre-BUYs om te keuren.")
                    prebuy_id = str(p.get("id"))

                status = (p.get("status") or "PENDING").upper()
                if status not in ("PENDING", "APPROVED"):
                    return twiml(f"Kan niet YES doen: status is {status}")

                exp = as_aware_utc(p.get("expires_at"))
                if exp and exp <= now_utc():
                    mark_rejected(conn, prebuy_id)
                    return twiml("Deze Pre-BUY is verlopen en is nu afgewezen.")

                # Mark approved + store amount (so worker can execute)
                mark_approved(conn, prebuy_id, amount)

                # ✅ ASYNC path (recommended): return immediately; worker executes the buy
                if ASYNC_BUY:
                    return twiml(
                        f"GOEDGEKEURD ✅\n"
                        f"BUY queued (worker doet dit) {p.get('symbol')} €{amount}\n"
                        f"ID: {prebuy_id}\n\n"
                        f"TIP: check later LIST voor status."
                    )

                # Fallback: synchronous buy (can timeout)
                ok, msg = execute_buy_sync(p, amount)

                if ok:
                    mark_consumed(conn, prebuy_id)
                    return twiml(f"GOEDGEKEURD ✅\n{msg}\nID: {prebuy_id}")

                mark_buy_failed(conn, prebuy_id, msg)
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
