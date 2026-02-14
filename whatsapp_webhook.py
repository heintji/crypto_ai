# whatsapp_webhook.py
# Web Service (Render) - Twilio WhatsApp inbound
# Commands:
#   HELP
#   LIST
#   TOP              -> top 5 (hoogste score) PENDING pre-buys
#   YES <bedrag> [ID]
#   NO [ID]
#
# ENV required:
#   DATABASE_URL        (Render Postgres URL)
#   INTERNAL_TOKEN      (optioneel, alleen voor interne calls)
#   WEBHOOK_BASE_URL    (optioneel, voor logging/links)
#   TWILIO_*            (alleen nodig als jij zelf outbound wilt sturen; inbound werkt zonder)

from __future__ import annotations

import os
import re
import time
import json
import traceback
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import Json
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse

# ============ PROJECT ROOT (zodat imports altijd werken) ============
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ============ OPTIONAL: paper/live execution ============
# Let op: als je nog geen live-trading hebt gekoppeld, dan blijft dit een "approval only".
# Zodra je live-buy klaar hebt, kun je hieronder jouw uitvoer-functie koppelen.
try:
    from trading.paper_trader import buy_eur  # type: ignore
except Exception:
    buy_eur = None  # fallback

# ============ ENV ============
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()
WEBHOOK_BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or "").strip().rstrip("/")

ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

# ============ APP ============
app = Flask(__name__)


# ------------------ helpers: TwiML ------------------
def twiml(text: str) -> Response:
    resp = MessagingResponse()
    resp.message(text)
    return Response(str(resp), mimetype="application/xml")


# ------------------ health routes (stopt 502 spam) ------------------
@app.get("/")
def root():
    return "OK", 200


@app.get("/health")
def health():
    return "OK", 200


# ------------------ DB ------------------
def db_available() -> bool:
    return bool(DATABASE_URL)


def db_connect():
    # psycopg2 connect; Render DATABASE_URL werkt direct
    return psycopg2.connect(DATABASE_URL)


def db_init() -> None:
    """Zorg dat de tabel bestaat + status kolom aanwezig is."""
    if not db_available():
        return
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_approvals (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            setup_type TEXT,
            regime TEXT,
            score INT,
            grade TEXT,
            entry NUMERIC,
            stop NUMERIC,
            target NUMERIC,
            created_at BIGINT,
            expires_at BIGINT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            approved_amount INT,
            approved_at BIGINT,
            payload JSONB
        );
        """
    )
    conn.commit()
    cur.close()
    conn.close()


def db_list_pending(limit: int = 10, order_by_score: bool = False) -> List[Dict[str, Any]]:
    conn = db_connect()
    cur = conn.cursor()
    order = "score DESC NULLS LAST, created_at DESC" if order_by_score else "created_at DESC"
    cur.execute(
        f"""
        SELECT id, symbol, setup_type, regime, score, grade, entry, stop, target, created_at, expires_at, status
        FROM pending_approvals
        WHERE status='PENDING'
        ORDER BY {order}
        LIMIT %s
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
                "regime": r[3],
                "score": r[4],
                "grade": r[5],
                "entry": float(r[6]) if r[6] is not None else None,
                "stop": float(r[7]) if r[7] is not None else None,
                "target": float(r[8]) if r[8] is not None else None,
                "created_at": int(r[9] or 0),
                "expires_at": int(r[10] or 0),
                "status": r[11],
            }
        )
    return out


def db_get_pending_by_id(prebuy_id: str) -> Optional[Dict[str, Any]]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, symbol, setup_type, regime, score, grade, entry, stop, target, created_at, expires_at, status, payload
        FROM pending_approvals
        WHERE id=%s
        """,
        (prebuy_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "symbol": row[1],
        "setup_type": row[2],
        "regime": row[3],
        "score": row[4],
        "grade": row[5],
        "entry": float(row[6]) if row[6] is not None else None,
        "stop": float(row[7]) if row[7] is not None else None,
        "target": float(row[8]) if row[8] is not None else None,
        "created_at": int(row[9] or 0),
        "expires_at": int(row[10] or 0),
        "status": row[11],
        "payload": row[12],
    }


def db_find_oldest_pending() -> Optional[Dict[str, Any]]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, symbol, setup_type, regime, score, grade, entry, stop, target, created_at, expires_at, status
        FROM pending_approvals
        WHERE status='PENDING'
        ORDER BY created_at ASC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "symbol": row[1],
        "setup_type": row[2],
        "regime": row[3],
        "score": row[4],
        "grade": row[5],
        "entry": float(row[6]) if row[6] is not None else None,
        "stop": float(row[7]) if row[7] is not None else None,
        "target": float(row[8]) if row[8] is not None else None,
        "created_at": int(row[9] or 0),
        "expires_at": int(row[10] or 0),
        "status": row[11],
    }


def db_mark_status(prebuy_id: str, status: str, amount: Optional[int] = None) -> None:
    conn = db_connect()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        """
        UPDATE pending_approvals
        SET status=%s,
            approved_amount = COALESCE(%s, approved_amount),
            approved_at = CASE WHEN %s IN ('APPROVED','REJECTED') THEN %s ELSE approved_at END
        WHERE id=%s
        """,
        (status, amount, status, now, prebuy_id),
    )
    conn.commit()
    cur.close()
    conn.close()


# ------------------ parsing ------------------
YES_RE = re.compile(r"^\s*yes\s+(\d+)\s*(.*)\s*$", re.IGNORECASE)
NO_RE = re.compile(r"^\s*no\s*(.*)\s*$", re.IGNORECASE)


def format_item(it: Dict[str, Any]) -> str:
    exp = it.get("expires_at") or 0
    now = int(time.time())
    exp_m = max(0, int((exp - now) / 60)) if exp else 0
    grade = (it.get("grade") or "").upper()
    return (
        f"{it['id']} | {it.get('symbol')} | {grade} | score={it.get('score')} | "
        f"entry={it.get('entry')} stop={it.get('stop')} target={it.get('target')} | exp {exp_m}m"
    )


def help_text() -> str:
    return (
        "Commands:\n"
        "• TOP  (top 5 beste PENDING trades)\n"
        "• LIST (laatste PENDING pre-buys)\n"
        "• YES <bedrag> [ID]\n"
        "  Voorbeeld: YES 10 PB-BTCUSDT-123\n"
        "• NO [ID]\n"
    )


# ------------------ main webhook ------------------
@app.post("/whatsapp")
def whatsapp_webhook():
    # altijd init opstarten (safe)
    try:
        if db_available():
            db_init()
    except Exception:
        # init failure mag NOOIT Twilio stil maken
        print("DB init error:")
        print(traceback.format_exc())

    try:
        body_raw = (request.form.get("Body", "") or "").strip()
        body = body_raw.strip()
        body_lower = body.lower()

        # snelle normalize: mensen typen soms "/help"
        if body_lower.startswith("/"):
            body_lower = body_lower[1:].strip()

        # HELP
        if body_lower in {"help", "h", "?"}:
            return twiml(help_text())

        # TOP (top 5 score)
        if body_lower == "top":
            if not db_available():
                return twiml("DB niet ingesteld (DATABASE_URL ontbreekt).")
            items = db_list_pending(limit=5, order_by_score=True)
            if not items:
                return twiml("Geen PENDING Pre-BUYs gevonden.")
            msg = "TOP 5 (PENDING):\n" + "\n".join([format_item(x) for x in items])
            msg += "\n\nBevestig: YES <bedrag> <ID>"
            return twiml(msg)

        # LIST (max 10 latest)
        if body_lower == "list":
            if not db_available():
                return twiml("DB niet ingesteld (DATABASE_URL ontbreekt).")
            items = db_list_pending(limit=10, order_by_score=False)
            if not items:
                return twiml("Geen PENDING Pre-BUYs gevonden.")
            msg = "PENDING (laatste 10):\n" + "\n".join([format_item(x) for x in items])
            msg += "\n\nBevestig: YES <bedrag> <ID>"
            return twiml(msg)

        # YES <amount> [id]
        m = YES_RE.match(body)
        if m:
            amount = int(m.group(1))
            rest = (m.group(2) or "").strip()
            prebuy_id = rest if rest else None

            if amount not in ALLOWED_AMOUNTS:
                return twiml(f"Bedrag niet toegestaan. Kies uit: {sorted(ALLOWED_AMOUNTS)}")

            if not db_available():
                return twiml("DB niet ingesteld (DATABASE_URL ontbreekt).")

            # kies ID: of meegegeven, of oudste pending
            it = None
            if prebuy_id:
                it = db_get_pending_by_id(prebuy_id)
                if not it or (it.get("status") != "PENDING"):
                    return twiml("Geen PENDING Pre-BUY gevonden (met die ID). Doe eerst LIST/TOP.")
            else:
                it = db_find_oldest_pending()
                if not it:
                    return twiml("Geen PENDING Pre-BUY gevonden. Doe eerst LIST/TOP.")

            # expiry check
            exp = int(it.get("expires_at") or 0)
            if exp and exp < int(time.time()):
                # mark expired
                try:
                    db_mark_status(it["id"], "EXPIRED")
                except Exception:
                    pass
                return twiml("Deze Pre-BUY is verlopen. Doe opnieuw TOP/LIST.")

            # mark approved
            db_mark_status(it["id"], "APPROVED", amount=amount)

            # probeer buy uit te voeren (paper/live) ALS jij die koppeling al hebt
            executed = False
            exec_err = None
            if buy_eur is not None:
                try:
                    # buy_eur verwacht meestal (symbol, eur_amount) of (symbol, amount)
                    # Als jouw buy_eur anders is, pas dit 1x aan.
                    buy_eur(it["symbol"], amount)  # type: ignore
                    executed = True
                except Exception as e:
                    exec_err = f"{type(e).__name__}: {e}"
                    print("BUY execution error:")
                    print(traceback.format_exc())

            msg = (
                "✅ GOEDKEURING ONTVANGEN\n"
                f"ID: {it['id']}\n"
                f"Inzet: €{amount}\n"
                f"{it.get('symbol')} | {str(it.get('grade') or '').upper()} | score={it.get('score')}\n"
                f"entry={it.get('entry')} stop={it.get('stop')} target={it.get('target')}\n"
            )

            if executed:
                msg += "✅ BUY uitgevoerd.\n"
            else:
                msg += "Volgende stap: BUY koppeling (paper_trader/live) uitvoeren.\n"
                if exec_err:
                    msg += f"(BUY error: {exec_err})\n"

            return twiml(msg)

        # NO [id]
        m2 = NO_RE.match(body)
        if m2:
            rest = (m2.group(1) or "").strip()
            prebuy_id = rest if rest else None

            if not db_available():
                return twiml("DB niet ingesteld (DATABASE_URL ontbreekt).")

            it = None
            if prebuy_id:
                it = db_get_pending_by_id(prebuy_id)
                if not it or (it.get("status") != "PENDING"):
                    return twiml("Geen PENDING Pre-BUY gevonden (met die ID). Doe eerst LIST/TOP.")
            else:
                it = db_find_oldest_pending()
                if not it:
                    return twiml("Geen PENDING Pre-BUY gevonden. Doe eerst LIST/TOP.")

            db_mark_status(it["id"], "REJECTED")
            return twiml(f"❌ Afgewezen: {it['id']} ({it.get('symbol')})")

        # fallback
        return twiml("Onbekend commando. Stuur HELP.")

    except Exception as e:
        # CRUCIAAL: Twilio moet ALTIJD TwiML terugkrijgen
        print("ERROR in /whatsapp:")
        print(traceback.format_exc())
        return twiml(f"ERROR in webhook: {type(e).__name__}")


if __name__ == "__main__":
    # Render gebruikt $PORT
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)
