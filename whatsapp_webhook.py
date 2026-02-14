from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from flask import Flask, request, Response

# ==========================================================
# ENV
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()

# Twilio (je webhook werkt ook zonder outbound send; replies zijn TwiML)
TWILIO_AUTH_TOKEN = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()  # optioneel, alleen als je signature check wilt

# Trading toggles
LIVE_TRADING = (os.getenv("LIVE_TRADING") or "0").strip() == "1"

# Allowed stakes
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

# Default risk model (als multi_coin_score nog geen stop/target vult)
DEFAULT_STOP_PCT = float(os.getenv("DEFAULT_STOP_PCT") or "0.02")  # 2%
DEFAULT_R_MULTIPLE = float(os.getenv("DEFAULT_R_MULTIPLE") or "2.0")  # 2R

# ==========================================================
# OPTIONAL EXECUTION IMPORTS
# ==========================================================
# Paper execution (altijd aanwezig)
from trading.paper_trader import buy_eur as paper_buy_eur  # type: ignore

# Live execution (alleen als jij dit bestand hebt)
# Je kunt later jouw bitvavo execution hier koppelen.
try:
    from trading.bitvavo_trader import buy_eur as live_buy_eur  # type: ignore
except Exception:
    live_buy_eur = None


# ==========================================================
# DB HELPERS
# ==========================================================
def db_available() -> bool:
    return bool(DATABASE_URL)

def db_conn():
    return psycopg2.connect(DATABASE_URL)

def ensure_tables() -> None:
    if not db_available():
        return
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_approvals (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            setup_type TEXT,
            market_regime TEXT,
            score INT,
            grade TEXT,
            entry NUMERIC,
            stop_loss NUMERIC,
            target NUMERIC,
            created_at BIGINT NOT NULL,
            expires_at BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            payload JSONB
        );
        """
    )
    conn.commit()
    cur.close()
    conn.close()

def db_list_pending(limit: int = 10) -> List[Dict[str, Any]]:
    ensure_tables()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, symbol, setup_type, market_regime, score, grade,
               entry, stop_loss, target, created_at, expires_at
        FROM pending_approvals
        WHERE status='PENDING'
        ORDER BY created_at DESC
        LIMIT %s;
        """,
        (limit,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r[0],
                "symbol": r[1],
                "setup_type": r[2],
                "market_regime": r[3],
                "score": int(r[4]) if r[4] is not None else None,
                "grade": r[5],
                "entry": float(r[6]) if r[6] is not None else None,
                "stop_loss": float(r[7]) if r[7] is not None else None,
                "target": float(r[8]) if r[8] is not None else None,
                "created_at": int(r[9]),
                "expires_at": int(r[10]),
            }
        )
    return out

def db_top_pending(limit: int = 5) -> List[Dict[str, Any]]:
    ensure_tables()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, symbol, setup_type, market_regime, score, grade,
               entry, stop_loss, target, created_at, expires_at
        FROM pending_approvals
        WHERE status='PENDING'
        ORDER BY COALESCE(score, 0) DESC, created_at DESC
        LIMIT %s;
        """,
        (limit,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r[0],
                "symbol": r[1],
                "setup_type": r[2],
                "market_regime": r[3],
                "score": int(r[4]) if r[4] is not None else None,
                "grade": r[5],
                "entry": float(r[6]) if r[6] is not None else None,
                "stop_loss": float(r[7]) if r[7] is not None else None,
                "target": float(r[8]) if r[8] is not None else None,
                "created_at": int(r[9]),
                "expires_at": int(r[10]),
            }
        )
    return out

def db_get_pending_by_id(prebuy_id: str) -> Optional[Dict[str, Any]]:
    ensure_tables()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, symbol, setup_type, market_regime, score, grade,
               entry, stop_loss, target, created_at, expires_at, payload
        FROM pending_approvals
        WHERE id=%s AND status='PENDING'
        """,
        (prebuy_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    payload = row[11] if row[11] is not None else {}
    return {
        "id": row[0],
        "symbol": row[1],
        "setup_type": row[2],
        "market_regime": row[3],
        "score": int(row[4]) if row[4] is not None else None,
        "grade": row[5],
        "entry": float(row[6]) if row[6] is not None else None,
        "stop_loss": float(row[7]) if row[7] is not None else None,
        "target": float(row[8]) if row[8] is not None else None,
        "created_at": int(row[9]),
        "expires_at": int(row[10]),
        "payload": payload,
    }

def db_update_status(prebuy_id: str, status: str) -> None:
    ensure_tables()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE pending_approvals SET status=%s WHERE id=%s;",
        (status, prebuy_id),
    )
    conn.commit()
    cur.close()
    conn.close()

# ==========================================================
# COMMAND PARSING
# ==========================================================
CMD_HELP = """Commands:
- LIST               -> toon PENDING pre-buys (max 10)
- TOP                -> toon 5 beste PENDING (op score)
- YES <bedrag> <ID>  -> keur goed + (paper/live) BUY
- NO <ID>            -> afkeuren
Voorbeeld: YES 10 PB-BTCUSDT-1771100252
"""

def fmt_item(it: Dict[str, Any]) -> str:
    return (
        f"{it['id']} | {it['symbol']} | {it.get('grade')} | score={it.get('score')} | "
        f"entry={it.get('entry')} | stop={it.get('stop_loss')} | target={it.get('target')} | "
        f"exp {max(0, int(it.get('expires_at', 0) - time.time()))//60}m"
    )

def compute_defaults(symbol: str, entry: float, stop_loss: Optional[float], target: Optional[float]) -> Tuple[float, float]:
    # stop default = 2% onder entry
    if stop_loss is None or stop_loss <= 0:
        stop_loss = entry * (1.0 - DEFAULT_STOP_PCT)

    # target default = entry + R*DEFAULT_R_MULTIPLE
    # R = entry - stop
    if target is None or target <= 0:
        r = max(1e-9, entry - stop_loss)
        target = entry + (DEFAULT_R_MULTIPLE * r)

    return float(stop_loss), float(target)

# ==========================================================
# FLASK
# ==========================================================
app = Flask(__name__)

@app.route("/health", methods=["GET"])
def health():
    return {"ok": True, "db": db_available(), "live_trading": LIVE_TRADING}, 200

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    body_raw = (request.form.get("Body") or "").strip()
    body = body_raw.strip()
    body_u = body.upper()

    # --- LOG LINE (Render logs)
    print(f"WHATSAPP_IN: from={request.form.get('From')} body={body_raw} db={'YES' if db_available() else 'NO'}")

    # HELP
    if body_u in {"HELP", "H", "?"}:
        return twiml(CMD_HELP)

    # LIST
    if body_u == "LIST":
        items = db_list_pending(limit=10) if db_available() else []
        if not items:
            return twiml("Geen PENDING Pre-BUY gevonden.")
        msg = "PENDING Pre-BUY (max 10):\n" + "\n".join(f"• {fmt_item(x)}" for x in items)
        msg += "\n\nBevestig: YES <bedrag> <ID>\nVoorbeeld: YES 10 PB-ADAUSDT-1771101030"
        return twiml(msg)

    # TOP (5 beste op score)
    if body_u == "TOP":
        items = db_top_pending(limit=5) if db_available() else []
        if not items:
            return twiml("Geen PENDING Pre-BUY gevonden. (Tip: eerst LIST)")
        msg = "TOP 5 Pre-BUY (beste score):\n" + "\n".join(f"• {fmt_item(x)}" for x in items)
        msg += "\n\nBevestig: YES <bedrag> <ID>\nVoorbeeld: YES 10 PB-BTCUSDT-1771100252"
        return twiml(msg)

    # YES <amount> <id>
    m_yes = re.match(r"^\s*YES\s+(\d+)\s+([A-Z0-9\-\_]+)\s*$", body_u)
    if m_yes:
        amount = int(m_yes.group(1))
        prebuy_id = m_yes.group(2)

        if amount not in ALLOWED_AMOUNTS:
            return twiml(f"Bedrag niet toegestaan. Kies uit: {sorted(ALLOWED_AMOUNTS)}")

        if not db_available():
            return twiml("DB niet beschikbaar (DATABASE_URL ontbreekt).")

        item = db_get_pending_by_id(prebuy_id)
        if not item:
            return twiml("Geen PENDING Pre-BUY gevonden (met die ID). Doe eerst LIST of TOP.")

        now = int(time.time())
        if int(item["expires_at"]) <= now:
            db_update_status(prebuy_id, "EXPIRED")
            return twiml("Deze Pre-BUY is verlopen. Doe opnieuw LIST of TOP.")

        entry = float(item["entry"] or 0.0)
        if entry <= 0:
            return twiml("Entry ontbreekt/ongeldig in Pre-BUY. (multi_coin_score moet entry vullen)")

        stop_loss, target = compute_defaults(item["symbol"], entry, item.get("stop_loss"), item.get("target"))

        # mark approved
        db_update_status(prebuy_id, "APPROVED")

        # EXECUTION: paper vs live
        exec_mode = "PAPER"
        exec_note = ""
        try:
            if LIVE_TRADING:
                if live_buy_eur is None:
                    exec_mode = "LIVE"
                    exec_note = "LIVE_TRADING=1 maar trading/bitvavo_trader.py ontbreekt → geen order geplaatst."
                else:
                    exec_mode = "LIVE"
                    live_buy_eur(symbol=item["symbol"], eur_amount=amount)  # type: ignore
            else:
                paper_buy_eur(symbol=item["symbol"], eur_amount=amount)
        except Exception as e:
            exec_note = f"BUY error: {type(e).__name__}: {e}"

        msg = (
            f"✅ GOEDKEURING ONTVANGEN\n"
            f"ID: {prebuy_id}\n"
            f"Inzet: €{amount}\n"
            f"{item['symbol']} | {item.get('grade')} | score={item.get('score')}\n"
            f"entry={entry} stop={stop_loss:.6f} target={target:.6f}\n"
            f"Mode: {exec_mode}\n"
        )
        if exec_note:
            msg += f"\n⚠️ {exec_note}\n"

        if not LIVE_TRADING:
            msg += "\n(Info) LIVE buy staat uit. Zet LIVE_TRADING=1 + bitvavo_trader.py om echte orders te plaatsen."

        return twiml(msg)

    # NO <id>
    m_no = re.match(r"^\s*NO\s+([A-Z0-9\-\_]+)\s*$", body_u)
    if m_no:
        prebuy_id = m_no.group(1)
        if not db_available():
            return twiml("DB niet beschikbaar (DATABASE_URL ontbreekt).")
        item = db_get_pending_by_id(prebuy_id)
        if not item:
            return twiml("Geen PENDING Pre-BUY gevonden (met die ID).")
        db_update_status(prebuy_id, "REJECTED")
        return twiml(f"❌ Afgekeurd: {prebuy_id}")

    return twiml("Onbekend commando. Stuur HELP.")

def twiml(message: str) -> Response:
    # Minimal TwiML response
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escape_xml(message)}</Message></Response>'
    return Response(xml, mimetype="application/xml")

def escape_xml(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&apos;")
    )

if __name__ == "__main__":
    ensure_tables()
    port = int(os.getenv("PORT") or "10000")
    app.run(host="0.0.0.0", port=port)
