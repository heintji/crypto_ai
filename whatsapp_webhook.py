# whatsapp_webhook.py
from __future__ import annotations

import os
import re
import time
import json
import traceback
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, request, Response

# ==========================================================
# ENV
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()
WEBHOOK_BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or "").strip().rstrip("/")

ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}
DEFAULT_APPROVAL_LIMIT = 10

app = Flask(__name__)

# ==========================================================
# TWILIO: always respond with TwiML
# ==========================================================
def twiml(message: str) -> Response:
    # Twilio expects XML. If you return JSON/text, you often get "silent" behavior.
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Message>{escape_xml(message)}</Message>
</Response>"""
    return Response(xml, mimetype="text/xml")

def escape_xml(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&apos;")
    )

# ==========================================================
# DB helpers
# ==========================================================
def db_available() -> bool:
    return bool(DATABASE_URL)

def db_connect():
    # Hard timeouts so Twilio never waits too long
    # connect_timeout is seconds; statement_timeout via options in ms
    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=3,
        options="-c statement_timeout=2000",
    )

def db_init() -> None:
    if not db_available():
        return
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_approvals (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                setup_type TEXT,
                regime TEXT,
                score INT,
                label TEXT,
                entry NUMERIC,
                stop NUMERIC,
                target NUMERIC,
                status TEXT NOT NULL DEFAULT 'PENDING',
                created_at BIGINT NOT NULL,
                expires_at BIGINT NOT NULL,
                payload JSONB
            );
            """
        )
        # Ensure columns exist (safe)
        cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'PENDING';")
        cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS payload JSONB;")
        conn.commit()
    finally:
        conn.close()

def db_list_pending(limit: int = DEFAULT_APPROVAL_LIMIT, top: bool = False) -> List[Dict[str, Any]]:
    conn = db_connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if top:
            cur.execute(
                """
                SELECT id, symbol, score, label, entry, stop, target, expires_at, created_at
                FROM pending_approvals
                WHERE status = 'PENDING'
                ORDER BY score DESC NULLS LAST, created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
        else:
            cur.execute(
                """
                SELECT id, symbol, score, label, entry, stop, target, expires_at, created_at
                FROM pending_approvals
                WHERE status = 'PENDING'
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def db_get_pending_by_id(prebuy_id: str) -> Optional[Dict[str, Any]]:
    conn = db_connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT *
            FROM pending_approvals
            WHERE id = %s AND status = 'PENDING'
            LIMIT 1
            """,
            (prebuy_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def db_mark_status(prebuy_id: str, new_status: str) -> None:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE pending_approvals SET status=%s WHERE id=%s",
            (new_status, prebuy_id),
        )
        conn.commit()
    finally:
        conn.close()

# ==========================================================
# Execution hook (BUY)
# ==========================================================
def call_buy_executor(prebuy: Dict[str, Any], amount_eur: int) -> Tuple[bool, str]:
    """
    This does NOT magically buy on Bitvavo unless you have an executor endpoint wired.
    It triggers your internal flow, which can be paper or live depending on your setup.
    """
    if not WEBHOOK_BASE_URL or not INTERNAL_TOKEN:
        return False, "WEBHOOK_BASE_URL of INTERNAL_TOKEN ontbreekt in env."

    # You must implement this endpoint in your execution service (paper_trader/live).
    # If you don't have it yet, this will fail (but you'll see it in logs).
    url = f"{WEBHOOK_BASE_URL}/internal/buy"
    payload = {
        "id": prebuy.get("id"),
        "symbol": prebuy.get("symbol"),
        "entry": float(prebuy.get("entry") or 0.0),
        "stop": float(prebuy.get("stop") or 0.0),
        "target": float(prebuy.get("target") or 0.0),
        "amount_eur": int(amount_eur),
        "label": prebuy.get("label"),
        "score": prebuy.get("score"),
    }
    headers = {"Authorization": f"Bearer {INTERNAL_TOKEN}"}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=5)
        if r.status_code >= 200 and r.status_code < 300:
            return True, "BUY executor aangeroepen ✅"
        return False, f"BUY executor fout: HTTP {r.status_code} | {r.text[:200]}"
    except Exception as e:
        return False, f"BUY executor exception: {e}"

# ==========================================================
# Commands
# ==========================================================
HELP_TEXT = (
    "Commands:\n"
    "HELP\n"
    "LIST  (laatste PENDING pre-buys)\n"
    "TOP   (beste 5 op score)\n"
    "YES <bedrag> <ID>\n"
    "NO <ID>\n"
    "\nVoorbeeld: YES 10 PB-BTCUSDT-1771100252"
)

def fmt_rows(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "Geen PENDING Pre-BUY’s gevonden."
    lines = []
    now = int(time.time())
    for r in rows:
        exp = int(r.get("expires_at") or 0)
        mins = max(0, (exp - now) // 60)
        lines.append(
            f"{r.get('id')} | {r.get('symbol')} | {r.get('label')} | score={r.get('score')} "
            f"| entry={r.get('entry')} stop={r.get('stop')} target={r.get('target')} | exp {mins}m"
        )
    return "\n".join(lines)

YES_RE = re.compile(r"^\s*YES\s+(\d+)\s+([A-Z0-9\-_.:]+)\s*$", re.I)
NO_RE  = re.compile(r"^\s*NO\s+([A-Z0-9\-_.:]+)\s*$", re.I)

# ==========================================================
# Routes
# ==========================================================
@app.route("/", methods=["GET"])
def root() -> str:
    return "OK"

@app.route("/whatsapp", methods=["POST"])
def whatsapp() -> Response:
    # Always reply with TwiML, even on errors.
    try:
        msg_raw = (request.form.get("Body") or "").strip()
        msg = msg_raw.upper()
        from_number = (request.form.get("From") or "").strip()

        # Quick log line in Render
        print(f"WHATSAPP_IN from={from_number} body={msg_raw}")

        if msg in ("HELP", "?"):
            return twiml(HELP_TEXT)

        if msg == "LIST":
            if not db_available():
                return twiml("DB niet beschikbaar (DATABASE_URL ontbreekt).")
            rows = db_list_pending(limit=10, top=False)
            return twiml(fmt_rows(rows) + "\n\nBevestig: YES <bedrag> <ID>")

        if msg == "TOP":
            if not db_available():
                return twiml("DB niet beschikbaar (DATABASE_URL ontbreekt).")
            rows = db_list_pending(limit=5, top=True)
            return twiml("Top 5 (score):\n" + fmt_rows(rows) + "\n\nBevestig: YES <bedrag> <ID>")

        m_yes = YES_RE.match(msg_raw)
        if m_yes:
            amount = int(m_yes.group(1))
            prebuy_id = m_yes.group(2).strip()

            if amount not in ALLOWED_AMOUNTS:
                return twiml(f"Bedrag niet toegestaan. Kies uit: {sorted(ALLOWED_AMOUNTS)}")

            if not db_available():
                return twiml("DB niet beschikbaar (DATABASE_URL ontbreekt).")

            prebuy = db_get_pending_by_id(prebuy_id)
            if not prebuy:
                return twiml("Geen PENDING Pre-BUY gevonden met die ID. Doe eerst LIST/TOP.")

            # mark approved first (prevents double spend)
            db_mark_status(prebuy_id, "APPROVED")

            ok, info = call_buy_executor(prebuy, amount)
            if ok:
                # mark consumed to prevent repeats
                db_mark_status(prebuy_id, "CONSUMED")
                return twiml(
                    "✅ GOEDKEURING ONTVANGEN\n"
                    f"ID: {prebuy_id}\n"
                    f"Inzet: €{amount}\n"
                    f"{prebuy.get('symbol')} | {prebuy.get('label')} | score={prebuy.get('score')}\n"
                    f"entry={prebuy.get('entry')} stop={prebuy.get('stop')} target={prebuy.get('target')}\n\n"
                    f"{info}"
                )
            else:
                # revert so it remains actionable
                db_mark_status(prebuy_id, "PENDING")
                return twiml(
                    "❌ GOEDKEURING ontvangen, maar BUY is NIET uitgevoerd.\n"
                    f"ID: {prebuy_id}\n"
                    f"Reden: {info}\n\n"
                    "Tip: check Render logs en of /internal/buy bestaat + INTERNAL_TOKEN klopt."
                )

        m_no = NO_RE.match(msg_raw)
        if m_no:
            prebuy_id = m_no.group(1).strip()
            if not db_available():
                return twiml("DB niet beschikbaar (DATABASE_URL ontbreekt).")
            prebuy = db_get_pending_by_id(prebuy_id)
            if not prebuy:
                return twiml("Geen PENDING Pre-BUY gevonden met die ID.")
            db_mark_status(prebuy_id, "REJECTED")
            return twiml(f"❎ Afgewezen: {prebuy_id}")

        # Unknown
        return twiml("Onbekend commando. Stuur HELP.")
    except Exception:
        print("WHATSAPP_WEBHOOK_ERROR\n" + traceback.format_exc())
        return twiml("❌ Interne fout in webhook. Check Render logs (Traceback).")

if __name__ == "__main__":
    # For local run; Render will run via start command
    db_init()
    port = int(os.getenv("PORT") or "10000")
    app.run(host="0.0.0.0", port=port)
