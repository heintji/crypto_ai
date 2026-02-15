# whatsapp_webhook.py
# Web Service (Render) - Twilio WhatsApp webhook
# Commands: HELP, LIST, TOP, YES <bedrag> [ID], NO <ID>
#
# Requires env:
# - DATABASE_URL (Render Postgres)
# - TWILIO_ACCOUNT_SID
# - TWILIO_AUTH_TOKEN
# - INTERNAL_TOKEN (optional; not used by Twilio directly)
# - WEBHOOK_BASE_URL (optional; only used if you want to print links)
#
# Notes:
# - Fixes BUY signature mismatch by calling buy_eur() with:
#   symbol, eur_amount, entry, stop_loss, target, prebuy_id
# - Fixes "int(datetime)" issues by converting timestamps safely.

from __future__ import annotations

import os
import re
import time
import traceback
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

# ==========================================================
# ENV
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()
WEBHOOK_BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or "").strip().rstrip("/")

# Execution mode: "paper" (default) or "live" (later)
EXECUTION_MODE = (os.getenv("EXECUTION_MODE") or "paper").strip().lower()

# Allowed amounts for YES
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

# How many rows to show in LIST / TOP
LIST_LIMIT = int(os.getenv("LIST_LIMIT") or "8")
TOP_LIMIT = int(os.getenv("TOP_LIMIT") or "5")

app = Flask(__name__)


# ==========================================================
# HELPERS
# ==========================================================
def now_ts() -> int:
    return int(time.time())


def respond(text: str):
    """Return TwiML response."""
    r = MessagingResponse()
    r.message(text)
    return str(r)


def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is empty")
    return psycopg2.connect(DATABASE_URL)


def safe_int_ts(value: Any) -> Optional[int]:
    """
    Convert DB timestamp-ish values to epoch int.
    Supports:
    - int/float already
    - datetime (has .timestamp())
    - ISO string -> best-effort (fallback None)
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    # datetime-like
    ts_func = getattr(value, "timestamp", None)
    if callable(ts_func):
        return int(ts_func())
    # string -> try parse basic ISO forms
    if isinstance(value, str):
        s = value.strip()
        # If it's numeric string
        if re.fullmatch(r"\d{9,12}", s):
            return int(s)
        try:
            # Python stdlib parse: limited; so do best-effort:
            # accept "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DDTHH:MM:SS"
            import datetime as _dt

            s2 = s.replace("T", " ")
            # remove timezone "Z" if present
            s2 = s2.replace("Z", "")
            dt = _dt.datetime.fromisoformat(s2)
            return int(dt.timestamp())
        except Exception:
            return None
    return None


def format_prebuy_row(row: Dict[str, Any]) -> str:
    grade = (row.get("grade") or row.get("label") or "").strip()
    score = row.get("score")
    sym = row.get("symbol") or row.get("coin") or "?"
    entry = row.get("entry")
    stop_loss = row.get("stop_loss", row.get("stop"))
    target = row.get("target")
    pb_id = row.get("id")

    exp_ts = safe_int_ts(row.get("expires_at"))
    exp_left = ""
    if exp_ts:
        mins = max(0, int((exp_ts - now_ts()) / 60))
        exp_left = f" | exp {mins}m"
    return (
        f"{pb_id} | {sym} | {grade} | score={score} | "
        f"entry={entry} | stop={stop_loss} | target={target}{exp_left}"
    )


def get_pending_rows(cur, limit: int, order_by: str) -> list[Dict[str, Any]]:
    """
    Fetch pending rows that are not expired.
    order_by: 'created_at DESC' or 'score DESC' etc (validated below)
    """
    allowed_order = {
        "created_at desc": "created_at DESC",
        "created_at asc": "created_at ASC",
        "score desc": "score DESC NULLS LAST, created_at DESC",
    }
    ob = allowed_order.get(order_by.lower(), "created_at DESC")

    cur.execute(
        f"""
        SELECT id, symbol, setup_type, regime, score, grade, entry, stop_loss, target,
               created_at, expires_at, status
        FROM pending_approvals
        WHERE status = 'PENDING'
          AND (expires_at IS NULL OR expires_at > NOW())
        ORDER BY {ob}
        LIMIT %s
        """,
        (limit,),
    )
    return cur.fetchall()


def find_one_pending(cur, prebuy_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Find one pending row (optionally by id) and lock it for update.
    Uses FOR UPDATE SKIP LOCKED to avoid double-processing.
    """
    if prebuy_id:
        cur.execute(
            """
            SELECT id, symbol, setup_type, regime, score, grade, entry, stop_loss, target,
                   created_at, expires_at, status
            FROM pending_approvals
            WHERE id = %s
              AND status = 'PENDING'
              AND (expires_at IS NULL OR expires_at > NOW())
            FOR UPDATE
            """,
            (prebuy_id,),
        )
        return cur.fetchone()

    # No ID provided: pick best score first (and newest if tie)
    cur.execute(
        """
        SELECT id, symbol, setup_type, regime, score, grade, entry, stop_loss, target,
               created_at, expires_at, status
        FROM pending_approvals
        WHERE status = 'PENDING'
          AND (expires_at IS NULL OR expires_at > NOW())
        ORDER BY score DESC NULLS LAST, created_at DESC
        LIMIT 1
        FOR UPDATE SKIP LOCKED
        """
    )
    return cur.fetchone()


def mark_status(cur, prebuy_id: str, status: str):
    cur.execute(
        "UPDATE pending_approvals SET status=%s WHERE id=%s",
        (status, prebuy_id),
    )


def execute_buy(row: Dict[str, Any], amount_eur: int) -> None:
    """
    Execute buy via paper (default).
    IMPORTANT: buy_eur signature must match:
      buy_eur(symbol, eur_amount, entry, stop_loss, target, prebuy_id)
    """
    symbol = row["symbol"]
    entry = float(row["entry"])
    stop_loss = float(row["stop_loss"])
    target = float(row["target"])
    prebuy_id = row["id"]

    if EXECUTION_MODE == "paper":
        from trading.paper_trader import buy_eur  # adjust import to your project

        # ✅ Correct signature call
        buy_eur(
            symbol=symbol,
            eur_amount=amount_eur,
            entry=entry,
            stop_loss=stop_loss,
            target=target,
            prebuy_id=prebuy_id,
        )
        return

    # Future: live mode
    raise RuntimeError(f"EXECUTION_MODE '{EXECUTION_MODE}' not implemented yet")


# ==========================================================
# ROUTES
# ==========================================================
@app.get("/")
def health():
    return "ok", 200


@app.post("/whatsapp")
def whatsapp():
    body_raw = (request.form.get("Body") or "").strip()
    body = body_raw.strip()
    msg_up = body.upper()

    from_number = (request.form.get("From") or "").strip()

    try:
        # -----------------------------
        # HELP
        # -----------------------------
        if msg_up in {"HELP", "H"}:
            return respond(
                "Commands:\n"
                "HELP\n"
                "LIST (laatste PENDING pre-buys)\n"
                "TOP (beste 5 op score)\n"
                "YES <bedrag> [ID]\n"
                "NO <ID>\n\n"
                "Voorbeeld: YES 10 PB-BTCUSDT-1771100252"
            )

        # -----------------------------
        # LIST
        # -----------------------------
        if msg_up == "LIST":
            with db_conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    rows = get_pending_rows(cur, limit=LIST_LIMIT, order_by="created_at desc")

            if not rows:
                return respond("Geen PENDING Pre-BUYs gevonden. (of alles is verlopen)")

            lines = [format_prebuy_row(r) for r in rows]
            return respond("\n".join(lines) + "\n\nBevestig: YES <bedrag> [ID]")

        # -----------------------------
        # TOP
        # -----------------------------
        if msg_up == "TOP":
            with db_conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    rows = get_pending_rows(cur, limit=TOP_LIMIT, order_by="score desc")

            if not rows:
                return respond("Geen PENDING Pre-BUYs gevonden. (of alles is verlopen)")

            lines = [format_prebuy_row(r) for r in rows]
            return respond("TOP picks:\n" + "\n".join(lines) + "\n\nBevestig: YES <bedrag> [ID]")

        # -----------------------------
        # NO <ID>
        # -----------------------------
        m_no = re.match(r"^\s*NO\s+([A-Za-z0-9\-_]+)\s*$", body, flags=re.IGNORECASE)
        if m_no:
            prebuy_id = m_no.group(1).strip()
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE pending_approvals
                        SET status='REJECTED'
                        WHERE id=%s AND status='PENDING'
                        """,
                        (prebuy_id,),
                    )
                    changed = cur.rowcount
            if changed:
                return respond(f"✅ Afgewezen: {prebuy_id}")
            return respond(f"Geen PENDING Pre-BUY gevonden met ID: {prebuy_id}")

        # -----------------------------
        # YES <amount> [ID]
        # -----------------------------
        m_yes = re.match(
            r"^\s*YES\s+(\d+)\s*([A-Za-z0-9\-_]+)?\s*$",
            body,
            flags=re.IGNORECASE,
        )
        if m_yes:
            amount = int(m_yes.group(1))
            prebuy_id = (m_yes.group(2) or "").strip() or None

            if amount not in ALLOWED_AMOUNTS:
                return respond(f"❌ Bedrag niet toegestaan. Kies uit: {sorted(ALLOWED_AMOUNTS)}")

            # Transaction: lock row -> set approved -> execute buy -> set bought
            with db_conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    row = find_one_pending(cur, prebuy_id=prebuy_id)
                    if not row:
                        if prebuy_id:
                            return respond(f"Geen PENDING Pre-BUY gevonden (met die ID). Doe eerst LIST.")
                        return respond("Geen PENDING Pre-BUY gevonden. Doe eerst LIST.")

                    # Pre-check required fields
                    for k in ("symbol", "entry", "stop_loss", "target", "id"):
                        if row.get(k) is None:
                            return respond(f"❌ Pre-BUY mist veld '{k}'. Fix multi_coin_score insert.")

                    # Mark approved (temporary)
                    mark_status(cur, row["id"], "APPROVED")
                    conn.commit()

            # Execute buy outside cursor scope (can write files, etc.)
            try:
                execute_buy(row, amount_eur=amount)
            except Exception as e:
                # Rollback status to PENDING so you can try again
                try:
                    with db_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE pending_approvals SET status='PENDING' WHERE id=%s",
                                (row["id"],),
                            )
                except Exception:
                    pass

                return respond(
                    "⚠️ BUY niet uitgevoerd: PAPER BUY signature mismatch / fout in koppeling.\n"
                    f"Error: {str(e)}"
                )

            # Mark bought/consumed
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE pending_approvals SET status='BOUGHT' WHERE id=%s",
                        (row["id"],),
                    )

            return respond(
                "✅ GOEDKEURING ONTVANGEN + BUY uitgevoerd\n"
                f"ID: {row['id']}\n"
                f"Inzet: €{amount}\n"
                f"{row['symbol']} | {row.get('grade','') or row.get('label','')}\n"
                f"entry={row['entry']} stop={row['stop_loss']} target={row['target']}"
            )

        # -----------------------------
        # Unknown
        # -----------------------------
        return respond("Onbekend commando. Stuur HELP.")

    except Exception as e:
        # This prevents silent 500s, and shows you the real error in WhatsApp.
        # Also print to logs for Render.
        print("WHATSAPP_WEBHOOK_ERROR", repr(e))
        traceback.print_exc()
        return respond(
            "❌ Interne fout in webhook. Check Render logs.\n"
            f"{type(e).__name__}: {str(e)}"
        )


if __name__ == "__main__":
    # Render binds PORT
    port = int(os.getenv("PORT") or "10000")
    app.run(host="0.0.0.0", port=port)
