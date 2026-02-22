# whatsapp_webhook.py
from __future__ import annotations

import os
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
from flask import Flask, request

# Twilio is optioneel (we willen NOOIT crashen als env ontbreekt)
try:
    from twilio.twiml.messaging_response import MessagingResponse
except Exception:  # pragma: no cover
    MessagingResponse = None  # type: ignore


app = Flask(__name__)

# ==========================================================
# ENV
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if not DATABASE_URL:
    # Niet raisen bij import => anders 502.
    DATABASE_URL = ""

ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

# ==========================================================
# UTIL
# ==========================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(msg, flush=True)


def twiml(text: str):
    """
    Twilio response helper (crash-proof).
    """
    if MessagingResponse is None:
        return (text, 200, {"Content-Type": "text/plain; charset=utf-8"})
    resp = MessagingResponse()
    resp.message(text)
    return (str(resp), 200, {"Content-Type": "application/xml"})


def db_connect():
    # Render Postgres: sslmode=require
    # (als je DATABASE_URL al ssl bevat, is dit alsnog ok)
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def as_aware_utc(dt: Any) -> Optional[datetime]:
    """
    Zorg dat expires_at altijd vergelijkbaar is met now_utc().
    """
    if not dt:
        return None
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        # naive => behandel als UTC (Postgres kan soms zo terugkomen)
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ==========================================================
# HEALTH ROUTES (fix voor 502 / pings)
# ==========================================================
@app.get("/")
def root():
    return "OK", 200


@app.get("/healthz")
def healthz():
    return "OK", 200


# ==========================================================
# DB QUERIES
# ==========================================================
def fetch_pending(conn, limit: int = 10) -> List[Dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
              id, symbol, setup_type, timeframe, regime,
              score, chance, confidence,
              entry, stop, target,
              status, created_at, expires_at
            FROM public.pending_approvals
            WHERE COALESCE(status,'PENDING') = 'PENDING'
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY COALESCE(chance,0) DESC, created_at ASC
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
              status, created_at, expires_at
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


# ==========================================================
# EXECUTION (TRADER MODULE) - crash-proof, lazy import
# ==========================================================
def execute_buy(prebuy: Dict[str, Any], amount_eur: int) -> Tuple[bool, str]:
    """
    Probeert BUY uit te voeren via paper_trader of live_trader.
    Crasht nooit: bij ontbreken import => netjes terug.
    """
    symbol = (prebuy.get("symbol") or "").strip()
    entry = float(prebuy.get("entry") or 0.0)
    stop = float(prebuy.get("stop") or 0.0)
    target = float(prebuy.get("target") or 0.0)
    prebuy_id = prebuy.get("id")

    try:
        from trading.paper_trader import buy_eur  # type: ignore

        meta = {"prebuy_id": prebuy_id, "entry": entry, "stop": stop, "target": target}
        buy_eur(symbol, float(amount_eur), meta=meta)  # type: ignore
        return True, f"BUY uitgevoerd (paper) {symbol} €{amount_eur}"

    except Exception as e1:
        try:
            from trading.live_trader import buy_eur  # type: ignore

            meta = {"prebuy_id": prebuy_id, "entry": entry, "stop": stop, "target": target}
            buy_eur(symbol, float(amount_eur), meta=meta)  # type: ignore
            return True, f"BUY uitgevoerd (live) {symbol} €{amount_eur}"

        except Exception as e2:
            return (
                False,
                "BUY NIET uitgevoerd (trader ontbreekt of error).\n"
                f"paper_err={type(e1).__name__}: {e1}\n"
                f"live_err={type(e2).__name__}: {e2}",
            )


# ==========================================================
# COMMAND PARSER
# ==========================================================
def parse_command(text: str) -> Tuple[str, List[str]]:
    t = (text or "").strip()
    if not t:
        return "HELP", []
    parts = t.split()
    cmd = parts[0].upper()
    args = parts[1:]
    return cmd, args


def fmt_prebuy_row(p: Dict[str, Any]) -> str:
    return (
        f"{p.get('id')} | {p.get('symbol')} | chance={p.get('chance')} "
        f"| score={p.get('score')} | {p.get('setup_type')} | entry={p.get('entry')}"
    )


HELP_TEXT = (
    "Commands:\n"
    "HELP\n"
    "LIST (laat pending zien)\n"
    "TOP (top 5 hoogste chance)\n"
    "YES <bedrag> [ID]  (zonder ID = pakt TOP 1)\n"
    "NO <ID>\n\n"
    "Bedragen: 5/10/15/20/30/100"
)


# ==========================================================
# MAIN WHATSAPP ENDPOINT
# ==========================================================
@app.post("/whatsapp")
def whatsapp():
    try:
        body = (request.values.get("Body") or "").strip()
        cmd, args = parse_command(body)

        if not DATABASE_URL:
            return twiml("DATABASE_URL ontbreekt in Render Environment.")

        # DB connect per request (lazy)
        with db_connect() as conn:
            if cmd in ("HELP", "?"):
                return twiml(HELP_TEXT)

            if cmd == "LIST":
                pending = fetch_pending(conn, limit=10)
                if not pending:
                    return twiml("Geen pending Pre-BUYs.")
                lines = ["Pending Pre-BUYs (max 10):"]
                lines += [fmt_prebuy_row(p) for p in pending]
                lines.append("\nGebruik: YES <bedrag> [ID]  of  NO <ID>")
                return twiml("\n".join(lines))

            if cmd == "TOP":
                pending = fetch_pending(conn, limit=5)
                if not pending:
                    return twiml("Geen pending Pre-BUYs.")
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

                # ID is optioneel: zonder ID pakken we TOP 1 (hoogste chance)
                if len(args) >= 2:
                    prebuy_id = args[1].strip()
                    p = get_pending_by_id(conn, prebuy_id)
                    if not p:
                        return twiml("ID niet gevonden.")
                else:
                    p = get_top_pending(conn)
                    if not p:
                        return twiml("Geen pending Pre-BUYs om te keuren.")
                    prebuy_id = str(p.get("id"))

                status = (p.get("status") or "PENDING").upper()
                if status != "PENDING":
                    return twiml(f"Kan niet YES doen: status is {status}")

                # Expiry check
                exp = as_aware_utc(p.get("expires_at"))
                if exp and exp <= now_utc():
                    mark_rejected(conn, prebuy_id)
                    return twiml("Deze Pre-BUY is verlopen en is nu afgewezen.")

                # 1) mark approved
                mark_approved(conn, prebuy_id)

                # 2) execute buy (crash-proof)
                ok, msg = execute_buy(p, amount)

                # 3) mark consumed als BUY ok is
                if ok:
                    mark_consumed(conn, prebuy_id)
                    return twiml(f"GOEDGEKEURD ✅\n{msg}\nID: {prebuy_id}")

                # BUY faalde => laat APPROVED staan (zodat je ziet dat jij akkoord gaf)
                return twiml(f"GOEDGEKEURD ✅\nMaar BUY faalde:\n{msg}\nID: {prebuy_id}")

            # fallback
            return twiml("Onbekend command.\n\n" + HELP_TEXT)

    except Exception as e:
        log("❌ ERROR in /whatsapp")
        log(str(e))
        log(traceback.format_exc())
        return twiml("Interne fout. Check Render logs.")


if __name__ == "__main__":
    port = int(os.getenv("PORT") or "10000")
    app.run(host="0.0.0.0", port=port)
