from __future__ import annotations

import os
import re
import json
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, request
import psycopg2
import requests

# =========================
# ENV
# =========================
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()

USE_DB_APPROVALS = (os.getenv("USE_DB_APPROVALS") or "1").strip().lower() in {"1", "true", "yes", "on"}
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

TWILIO_ACCOUNT_SID = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
TWILIO_WHATSAPP_TO = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()

ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

# =========================
# Project-root fix (imports)
# =========================
import sys
import os as _os
PROJECT_ROOT = _os.path.dirname(_os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Belangrijk: whatsapp_webhook = alleen human approval + start BUY
from trading.paper_trader import buy_eur  # noqa: E402

app = Flask(__name__)


# =========================
# Helpers: Twilio send
# =========================
def twilio_ready() -> bool:
    return all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO])

def send_whatsapp(message: str) -> bool:
    if not twilio_ready():
        print("📭 WhatsApp melding overgeslagen (Twilio env vars ontbreken).", flush=True)
        return False

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    data = {"From": TWILIO_WHATSAPP_FROM, "To": TWILIO_WHATSAPP_TO, "Body": message}

    try:
        r = requests.post(url, data=data, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=15)
        if r.status_code >= 400:
            print(f"⚠️ Twilio send failed ({r.status_code}): {r.text[:200]}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"⚠️ Twilio send exception: {e}", flush=True)
        return False


# =========================
# DB Helpers (Postgres)
# =========================
def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def db_fetch_pending(symbol: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Haal PENDING approvals op die nog niet verlopen zijn.
    Fix: TIMESTAMPTZ vergelijken met NOW() (geen epoch int).
    """
    if not USE_DB_APPROVALS:
        return []

    q = """
    SELECT
      id, symbol, setup_type, label, score, grade,
      entry, stop_loss, target, regime,
      created_at, expires_at, status
    FROM pending_approvals
    WHERE status = 'PENDING'
      AND expires_at > NOW()
    """
    params: List[Any] = []
    if symbol:
        q += " AND symbol = %s"
        params.append(symbol.upper().strip())
    q += " ORDER BY created_at DESC LIMIT %s"
    params.append(int(limit))

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, params)
            rows = cur.fetchall()

    cols = [
        "id","symbol","setup_type","label","score","grade",
        "entry","stop_loss","target","regime",
        "created_at","expires_at","status"
    ]
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(dict(zip(cols, r)))
    return out

def db_mark_status(prebuy_id: str, new_status: str) -> None:
    if not USE_DB_APPROVALS:
        return
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pending_approvals SET status=%s WHERE id=%s",
                (new_status, prebuy_id)
            )

def fmt_prebuy(p: Dict[str, Any]) -> str:
    return (
        f"• {p.get('symbol')} | {p.get('setup_type')} | {p.get('label')} | score {p.get('score')}\n"
        f"  entry {float(p.get('entry') or 0):.6f} | SL {float(p.get('stop_loss') or 0):.6f} | tgt {float(p.get('target') or 0):.6f}\n"
        f"  id: {p.get('id')}"
    )

def parse_msg(body: str) -> Tuple[str, List[str]]:
    msg = (body or "").strip()
    if not msg:
        return "", []
    parts = msg.strip().split()
    cmd = parts[0].upper()
    args = parts[1:]
    return cmd, args


# =========================
# Webhook route
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    body = (request.form.get("Body") or "").strip()
    from_number = (request.form.get("From") or "").strip()

    cmd, args = parse_msg(body)

    # HELP
    if cmd in {"HELP", "H", "?"}:
        send_whatsapp(
            "🤖 Commands:\n"
            "• LIST  -> toon pending Pre-BUYs\n"
            "• YES <bedrag> -> koop nieuwste pending (bijv. YES 10)\n"
            "• YES <SYMBOL> <bedrag> -> koop specifieke (bijv. YES BTCUSDT 20)\n"
            "• NO -> annuleer nieuwste pending\n"
            "• NO <SYMBOL> -> annuleer specifieke"
        )
        return ("", 204)

    # LIST
    if cmd == "LIST":
        pending = db_fetch_pending(limit=5)
        if not pending:
            send_whatsapp("Geen PENDING Pre-BUYs gevonden (of alles is verlopen).")
            return ("", 204)

        msg_lines = ["📌 PENDING Pre-BUYs (max 5):"]
        for p in pending:
            msg_lines.append(fmt_prebuy(p))
        msg_lines.append("\nAntwoord: YES 10  of  YES BTCUSDT 20  of  NO")
        send_whatsapp("\n".join(msg_lines))
        return ("", 204)

    # NO / CANCEL
    if cmd == "NO":
        symbol = args[0].upper().strip() if args else None
        pending = db_fetch_pending(symbol=symbol, limit=1)
        if not pending:
            send_whatsapp("Geen PENDING Pre-BUY gevonden om te annuleren.")
            return ("", 204)

        p = pending[0]
        db_mark_status(p["id"], "CANCELLED")
        send_whatsapp(f"❌ Geannuleerd: {p.get('symbol')} ({p.get('id')})")
        return ("", 204)

    # YES / APPROVE + BUY
    if cmd == "YES":
        if not args:
            send_whatsapp("Gebruik: YES <bedrag>  (bijv. YES 10)  of  YES <SYMBOL> <bedrag>")
            return ("", 204)

        symbol: Optional[str] = None
        amount_str: Optional[str] = None

        if len(args) == 1:
            amount_str = args[0]
        else:
            symbol = args[0].upper().strip()
            amount_str = args[1]

        try:
            eur_amount = int(float(amount_str))
        except Exception:
            send_whatsapp("⚠️ Bedrag niet geldig. Toegestaan: 5, 10, 15, 20, 30, 100")
            return ("", 204)

        if eur_amount not in ALLOWED_AMOUNTS:
            send_whatsapp("⚠️ Bedrag niet toegestaan. Toegestaan: 5, 10, 15, 20, 30, 100")
            return ("", 204)

        pending = db_fetch_pending(symbol=symbol, limit=1)
        if not pending:
            send_whatsapp("Geen PENDING Pre-BUY gevonden (of alles is verlopen).")
            return ("", 204)

        p = pending[0]

        prebuy_id = str(p["id"])
        sym = str(p["symbol"])
        stop_loss = float(p["stop_loss"] or 0.0)
        target = float(p["target"] or 0.0)

        # Mark as CONSUMED eerst (zodat geen dubbele BUY door retries)
        db_mark_status(prebuy_id, "CONSUMED")

        try:
            # Let op: jouw eerdere fout zei dat buy_eur() stop_loss, target, prebuy_id nodig heeft
            res = buy_eur(sym, eur_amount, stop_loss, target, prebuy_id)
        except TypeError as e:
            # Signature mismatch -> direct zichtbaar maken
            send_whatsapp(
                "⚠️ BUY niet uitgevoerd: PAPER BUY signature mismatch.\n"
                f"Details: {e}"
            )
            return ("", 204)
        except Exception as e:
            # Revert status zodat je niet vastloopt
            db_mark_status(prebuy_id, "PENDING")
            send_whatsapp(f"⚠️ BUY fout: {e}")
            return ("", 204)

        # Result verwerken
        if isinstance(res, dict) and res.get("ok"):
            send_whatsapp(
                f"✅ BUY uitgevoerd: {sym}\n"
                f"Bedrag: €{eur_amount}\n"
                f"SL: {stop_loss:.6f}\n"
                f"Target: {target:.6f}\n"
                f"Prebuy: {prebuy_id}"
            )
        else:
            # als buy faalde -> terug naar PENDING of mark FAILED
            db_mark_status(prebuy_id, "FAILED")
            send_whatsapp(f"⚠️ BUY mislukt: {res}")
        return ("", 204)

    # Default
    send_whatsapp("Onbekend commando. Stuur HELP.")
    return ("", 204)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
