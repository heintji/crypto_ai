from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

# =========================
# ENV
# =========================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL ontbreekt (Render Postgres).")

INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()  # optioneel, niet nodig voor Twilio
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

APP_NAME = os.getenv("APP_NAME", "crypto_ai").strip()

# =========================
# BUY koppeling (Bitvavo/live via trading.paper_trader)
# =========================
try:
    from trading.paper_trader import buy_eur  # jouw live-buy functie
except Exception as e:
    buy_eur = None
    print(f"⚠️ buy_eur import failed: {e}", flush=True)

app = Flask(__name__)

# =========================
# DB helpers
# =========================
def db_conn():
    # Render Postgres vereist vaak sslmode=require
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def db_init() -> None:
    """Zorgt dat alle tabellen bestaan met de juiste kolommen."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS pending_approvals (
                id              TEXT PRIMARY KEY,
                symbol          TEXT NOT NULL,
                setup_type      TEXT,
                label           TEXT,
                score           INTEGER,
                grade           TEXT,
                entry           DOUBLE PRECISION,
                stop_loss       DOUBLE PRECISION,
                target          DOUBLE PRECISION,
                regime          TEXT,

                status          TEXT NOT NULL DEFAULT 'PENDING', -- PENDING/APPROVED/CONSUMED/REJECTED/EXPIRED
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at      TIMESTAMPTZ NOT NULL,

                approved_amount DOUBLE PRECISION,
                approved_at     TIMESTAMPTZ,
                consumed_at     TIMESTAMPTZ,
                rejected_at     TIMESTAMPTZ
            );
            """)

            cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending_approvals_status_expires
            ON pending_approvals(status, expires_at);
            """)

        conn.commit()

def respond(text: str):
    r = MessagingResponse()
    r.message(text)
    return str(r)

# =========================
# Business logic
# =========================
def parse_command(body: str) -> Tuple[str, List[str]]:
    msg = (body or "").strip()
    upper = msg.upper()
    parts = upper.split()
    if not parts:
        return ("", [])
    return (parts[0], parts[1:])

def db_fetch_pending(limit: int = 10) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT *
                FROM pending_approvals
                WHERE status='PENDING'
                  AND expires_at > NOW()
                ORDER BY created_at ASC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
            return [dict(r) for r in rows]

def db_get_pending_by_id(prebuy_id: str) -> Optional[Dict[str, Any]]:
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT *
                FROM pending_approvals
                WHERE id=%s
                LIMIT 1
            """, (prebuy_id,))
            row = cur.fetchone()
            return dict(row) if row else None

def db_mark_expired() -> int:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pending_approvals
                SET status='EXPIRED'
                WHERE status='PENDING'
                  AND expires_at <= NOW()
            """)
            n = cur.rowcount
        conn.commit()
        return n

def db_approve(prebuy_id: str, amount: float) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Zet status -> APPROVED (alleen als nog PENDING en niet verlopen)."""
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                UPDATE pending_approvals
                SET status='APPROVED',
                    approved_amount=%s,
                    approved_at=NOW()
                WHERE id=%s
                  AND status='PENDING'
                  AND expires_at > NOW()
                RETURNING *
            """, (amount, prebuy_id))
            row = cur.fetchone()
        conn.commit()

    if not row:
        return (False, "Niet gevonden / al verlopen / niet meer PENDING.", None)
    return (True, "OK", dict(row))

def db_consume(prebuy_id: str) -> None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pending_approvals
                SET status='CONSUMED',
                    consumed_at=NOW()
                WHERE id=%s
                  AND status='APPROVED'
            """, (prebuy_id,))
        conn.commit()

def db_reject(prebuy_id: str) -> Tuple[bool, str]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE pending_approvals
                SET status='REJECTED',
                    rejected_at=NOW()
                WHERE id=%s
                  AND status IN ('PENDING','APPROVED')
            """, (prebuy_id,))
            n = cur.rowcount
        conn.commit()
    if n == 0:
        return (False, "Niet gevonden of al CONSUMED/EXPIRED.")
    return (True, "OK")

def format_prebuy_row(r: Dict[str, Any]) -> str:
    # compact en WhatsApp-vriendelijk
    return (
        f"{r.get('id')} | {r.get('symbol')} | {r.get('label')} | score={r.get('score')}\n"
        f"entry={r.get('entry')} | stop={r.get('stop_loss')} | target={r.get('target')}\n"
        f"exp={r.get('expires_at')}"
    )

# =========================
# Routes
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    try:
        db_init()
    except Exception as e:
        return respond(f"❌ DB init fout: {e}")

    body = request.form.get("Body", "")
    cmd, args = parse_command(body)

    # Housekeeping: expire oude pending
    try:
        db_mark_expired()
    except Exception as e:
        return respond(f"❌ DB expire fout: {e}")

    if cmd in {"HELP", "H"}:
        return respond(
            "Commands:\n"
            "- LIST (toon pending Pre-BUYs)\n"
            "- YES <bedrag> <ID>\n"
            "- NO <ID>\n"
            "- TOP (status)\n"
            f"Bedragen: {sorted(ALLOWED_AMOUNTS)}"
        )

    if cmd == "TOP":
        return respond("✅ Webhook is live.\nGebruik: LIST / YES <bedrag> <ID> / NO <ID>")

    if cmd == "LIST":
        try:
            rows = db_fetch_pending(limit=10)
        except Exception as e:
            return respond(f"❌ DB LIST fout: {e}")

        if not rows:
            return respond("Geen PENDING Pre-BUYs gevonden (of alles is verlopen).")

        msg = "📌 PENDING Pre-BUYs:\n\n" + "\n\n".join(format_prebuy_row(r) for r in rows)
        msg += "\n\nBevestig: YES <bedrag> <ID>"
        return respond(msg)

    if cmd == "NO":
        if not args:
            return respond("Gebruik: NO <ID>")
        prebuy_id = args[0].strip()
        ok, reason = db_reject(prebuy_id)
        return respond("✅ Afgewezen." if ok else f"⚠️ Niet gelukt: {reason}")

    if cmd == "YES":
        if len(args) < 2:
            return respond("Gebruik: YES <bedrag> <ID>\nBijv: YES 10 PB-BTCUSDT-123")
        try:
            amount = int(args[0])
        except Exception:
            return respond("⚠️ Bedrag ongeldig. Gebruik bijv: YES 10 <ID>")

        if amount not in ALLOWED_AMOUNTS:
            return respond(f"⚠️ Bedrag niet toegestaan. Kies uit: {sorted(ALLOWED_AMOUNTS)}")

        prebuy_id = args[1].strip()

        ok, reason, row = db_approve(prebuy_id, float(amount))
        if not ok or not row:
            return respond(f"⚠️ Goedkeuren mislukt: {reason}")

        # Als buy_eur niet beschikbaar is
        if buy_eur is None:
            return respond("⚠️ buy_eur is niet beschikbaar (import error). Check Render logs.")

        # Execute BUY (live)
        try:
            # BELANGRIJK: jouw buy_eur verwacht amount_eur (niet eur_amount)
            res = buy_eur(
                symbol=str(row["symbol"]),
                amount_eur=float(amount),
                stop_loss=float(row["stop_loss"]),
                target=float(row["target"]),
                prebuy_id=str(row["id"]),
            )

            if not isinstance(res, dict) or not res.get("ok"):
                return respond(f"⚠️ BUY niet uitgevoerd: {res}")

            # mark consumed in DB
            db_consume(str(row["id"]))

            return respond(
                "✅ GOEDKEURING ONTVANGEN + BUY uitgevoerd\n"
                f"ID: {row['id']}\n"
                f"Inzet: €{amount}\n"
                f"{row['symbol']} | {row.get('label')} | score={row.get('score')}\n"
                f"entry={row.get('entry')} stop={row.get('stop_loss')} target={row.get('target')}\n"
                f"filled_qty={res.get('qty')} price={res.get('price')}"
            )

        except Exception as e:
            return respond(f"⚠️ BUY niet uitgevoerd: {e}")

    return respond("Onbekend commando. Stuur HELP.")
