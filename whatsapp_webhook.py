from __future__ import annotations

import os
import re
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from flask import Flask, request

# ==========================================================
# APP
# ==========================================================
app = Flask(__name__)

# ==========================================================
# ENV
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL ontbreekt (Render env var).")

DEBUG = (os.getenv("DEBUG") or "0").strip().lower() in {"1", "true", "yes", "on"}

ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

# ==========================================================
# DB
# ==========================================================
def db_conn():
    return psycopg2.connect(DATABASE_URL)

def db_get_pending(limit: int = 10) -> List[Tuple]:
    now = int(time.time())
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, symbol, label, score, entry, stop_loss, target, expires_at
        FROM pending_approvals
        WHERE status='PENDING' AND expires_at > %s
        ORDER BY created_at DESC
        LIMIT %s;
    """, (now, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def db_get_top(limit: int = 5) -> List[Tuple]:
    now = int(time.time())
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, symbol, label, score, entry, stop_loss, target, expires_at
        FROM pending_approvals
        WHERE status='PENDING' AND expires_at > %s
        ORDER BY score DESC, created_at DESC
        LIMIT %s;
    """, (now, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def db_find_pending_by_id(prebuy_id: str) -> Optional[Tuple]:
    now = int(time.time())
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, symbol, label, score, entry, stop_loss, target, expires_at
        FROM pending_approvals
        WHERE id=%s AND status='PENDING' AND expires_at > %s
        LIMIT 1;
    """, (prebuy_id, now))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row

def db_mark_no(prebuy_id: str) -> bool:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE pending_approvals
        SET status='DECLINED'
        WHERE id=%s AND status='PENDING'
        RETURNING id;
    """, (prebuy_id,))
    ok = cur.fetchone() is not None
    conn.commit()
    cur.close()
    conn.close()
    return ok

def db_mark_approved(prebuy_id: str) -> Optional[Tuple]:
    """
    Zet PENDING -> APPROVED en geeft trade data terug.
    """
    now = int(time.time())
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE pending_approvals
        SET status='APPROVED'
        WHERE id=%s AND status='PENDING' AND expires_at > %s
        RETURNING id, symbol, entry, stop_loss, target, score, label;
    """, (prebuy_id, now))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return row

# ==========================================================
# TWILIO XML RESPONSE
# ==========================================================
def respond(message: str) -> str:
    safe = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Message>{safe}</Message>
</Response>"""

# ==========================================================
# PARSING
# ==========================================================
def normalize_body(body: str) -> str:
    return (body or "").strip()

def parse_yes(body: str) -> Optional[Tuple[int, str]]:
    """
    YES <amount> <ID>
    """
    m = re.match(r"^\s*YES\s+(\d+)\s+([A-Z0-9\-_]+)\s*$", body.strip(), flags=re.IGNORECASE)
    if not m:
        return None
    amount = int(m.group(1))
    prebuy_id = m.group(2).strip()
    return amount, prebuy_id

def parse_no(body: str) -> Optional[str]:
    m = re.match(r"^\s*NO\s+([A-Z0-9\-_]+)\s*$", body.strip(), flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip()

# ==========================================================
# BUY KOPPELING
# ==========================================================
def do_buy(symbol: str, amount: int, stop_loss: float, target: float, prebuy_id: str) -> Dict[str, Any]:
    """
    Koppeling naar jouw trading/paper_trader.py
    JOUW SIGNATURE (zoals je stuurde):
        buy_eur(symbol, amount_eur, stop_loss, target, prebuy_id)
    """
    from trading.paper_trader import buy_eur  # late import (Render safe)

    return buy_eur(
        symbol=symbol,
        amount_eur=float(amount),
        stop_loss=float(stop_loss),
        target=float(target),
        prebuy_id=prebuy_id,
    )

# ==========================================================
# COMMAND TEXT
# ==========================================================
HELP_TEXT = (
    "Commands:\n"
    "HELP\n"
    "LIST (laatste PENDING pre-buys)\n"
    "TOP (beste 5 op score)\n"
    "YES <bedrag> <ID>\n"
    "NO <ID>\n\n"
    "Voorbeeld: YES 10 PB-BTCUSDT-1771100252"
)

# ==========================================================
# ROUTE
# ==========================================================
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    body_raw = request.form.get("Body", "")
    body = normalize_body(body_raw)

    # (optioneel) log
    if DEBUG:
        print(f"WHATSAPP_IN body={body}", flush=True)

    try:
        cmd = body.strip().upper()

        if cmd in {"HELP", "H", "?"}:
            return respond(HELP_TEXT)

        if cmd == "LIST":
            rows = db_get_pending(limit=10)
            if not rows:
                return respond("Geen PENDING Pre-BUY gevonden. (of alles verlopen)")
            lines = []
            for (pid, sym, label, score, entry, sl, tgt, exp) in rows:
                mins = max(0, int((int(exp) - int(time.time())) / 60))
                lines.append(
                    f"{pid} | {sym} | {label} | score={int(score)} | "
                    f"entry={float(entry):.6f} | stop={float(sl):.6f} | target={float(tgt):.6f} | exp {mins}m"
                )
            return respond("\n".join(lines) + "\n\nBevestig: YES <bedrag> <ID>")

        if cmd == "TOP":
            rows = db_get_top(limit=5)
            if not rows:
                return respond("Geen PENDING Pre-BUY gevonden. (of alles verlopen)")
            lines = []
            for (pid, sym, label, score, entry, sl, tgt, exp) in rows:
                mins = max(0, int((int(exp) - int(time.time())) / 60))
                lines.append(
                    f"{pid} | {sym} | {label} | score={int(score)} | "
                    f"entry={float(entry):.6f} | stop={float(sl):.6f} | target={float(tgt):.6f} | exp {mins}m"
                )
            return respond("\n".join(lines) + "\n\nBevestig: YES <bedrag> <ID>")

        yes = parse_yes(body)
        if yes:
            amount, prebuy_id = yes

            if amount not in ALLOWED_AMOUNTS:
                return respond(f"Bedrag niet toegestaan. Toegestaan: {sorted(ALLOWED_AMOUNTS)}")

            pending = db_find_pending_by_id(prebuy_id)
            if not pending:
                return respond("Geen PENDING Pre-BUY gevonden (met die ID). Doe eerst LIST.")

            # Mark APPROVED in DB (zodat je geen dubbele approves krijgt)
            approved = db_mark_approved(prebuy_id)
            if not approved:
                return respond("Kon niet goedkeuren. (is mogelijk al verwerkt of verlopen)")

            _pid, sym, entry, sl, tgt, score, label = approved

            # BUY uitvoeren
            try:
                buy_res = do_buy(
                    symbol=sym,
                    amount=amount,
                    stop_loss=float(sl),
                    target=float(tgt),
                    prebuy_id=_pid,
                )

                if isinstance(buy_res, dict) and buy_res.get("ok"):
                    return respond(
                        "✅ GOEDKEURING ONTVANGEN\n"
                        f"ID: {_pid}\n"
                        f"Inzet: €{amount}\n"
                        f"{sym} | {label} | score={int(score)}\n"
                        f"entry={float(entry):.6f} stop={float(sl):.6f} target={float(tgt):.6f}\n"
                        "🟢 BUY uitgevoerd."
                    )

                return respond(
                    "✅ GOEDKEURING ONTVANGEN\n"
                    f"ID: {_pid}\n"
                    f"Inzet: €{amount}\n"
                    f"{sym} | {label} | score={int(score)}\n"
                    f"entry={float(entry):.6f} stop={float(sl):.6f} target={float(tgt):.6f}\n"
                    f"⚠️ BUY niet uitgevoerd: {buy_res}"
                )

            except Exception as e:
                if DEBUG:
                    print(traceback.format_exc(), flush=True)
                return respond(
                    "✅ GOEDKEURING ONTVANGEN\n"
                    f"ID: {_pid}\n"
                    f"Inzet: €{amount}\n"
                    f"{sym} | {label} | score={int(score)}\n"
                    f"entry={float(entry):.6f} stop={float(sl):.6f} target={float(tgt):.6f}\n"
                    f"⚠️ BUY error: {e}"
                )

        no_id = parse_no(body)
        if no_id:
            ok = db_mark_no(no_id)
            return respond("❌ Afgewezen." if ok else "Niet gevonden / al verwerkt. Doe LIST.")

        # fallback
        return respond("Onbekend commando. Stuur HELP.")

    except Exception as e:
        if DEBUG:
            print(traceback.format_exc(), flush=True)
        return respond(f"❌ Interne fout in webhook: {e}\nCheck Render logs (Traceback).")

# ==========================================================
# RUN
# ==========================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT") or "10000")
    app.run(host="0.0.0.0", port=port)
