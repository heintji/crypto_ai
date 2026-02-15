# whatsapp_webhook.py
from __future__ import annotations

import os
import re
import json
import traceback
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
from flask import Flask, request, Response

# ==========================================================
# ENV
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()  # optional
USE_DB_APPROVALS = (os.getenv("USE_DB_APPROVALS") or "1").strip().lower() in {"1", "true", "yes", "on"}

ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

# Twilio posts form fields: Body, From
# ----------------------------------------------------------

app = Flask(__name__)


# ==========================================================
# DB
# ==========================================================
def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def ensure_tables():
    if not USE_DB_APPROVALS:
        return
    sql = """
    CREATE TABLE IF NOT EXISTS pending_approvals (
        id              TEXT PRIMARY KEY,
        symbol          TEXT NOT NULL,
        coin            TEXT,
        setup_type      TEXT,
        label           TEXT,
        score           INTEGER,
        grade           TEXT,
        entry           DOUBLE PRECISION,
        stop_loss       DOUBLE PRECISION,
        target          DOUBLE PRECISION,
        regime          TEXT,
        bot_confidence  INTEGER,
        payload         JSONB,
        status          TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING/APPROVED/NO/CONSUMED/EXPIRED
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at      TIMESTAMPTZ NOT NULL,
        approved_amount DOUBLE PRECISION,
        approved_by     TEXT,
        approved_at     TIMESTAMPTZ,
        consumed_at     TIMESTAMPTZ,
        note            TEXT
    );
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


# ==========================================================
# RESP (TwiML)
# ==========================================================
def twiml(text: str) -> Response:
    # minimal TwiML
    body = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escape_xml(text)}</Message></Response>'
    return Response(body, mimetype="application/xml")


def escape_xml(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


# ==========================================================
# COMMAND PARSER
# ==========================================================
def parse_command(raw: str) -> Tuple[str, List[str]]:
    s = (raw or "").strip()
    if not s:
        return ("", [])
    parts = s.split()
    cmd = parts[0].upper()
    args = parts[1:]
    return (cmd, args)


def help_text() -> str:
    return (
        "Commands:\n"
        "HELP\n"
        "LIST (laatste PENDING pre-buys)\n"
        "TOP (beste 5 op score)\n"
        "YES <bedrag> <ID>\n"
        "NO <ID>\n\n"
        "Voorbeeld: YES 10 PB-BTCUSDT-1771100252"
    )


# ==========================================================
# DB QUERIES
# ==========================================================
def db_fetch_pending(limit: int = 10) -> List[Dict[str, Any]]:
    ensure_tables()
    q = """
    SELECT id, symbol, label, score, entry, stop_loss, target,
           EXTRACT(EPOCH FROM (expires_at - NOW()))::INT AS exp_seconds
    FROM pending_approvals
    WHERE status='PENDING'
      AND expires_at > NOW()
    ORDER BY created_at DESC
    LIMIT %s
    """
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(q, (limit,))
            return [dict(r) for r in cur.fetchall()]


def db_fetch_top(limit: int = 5) -> List[Dict[str, Any]]:
    ensure_tables()
    q = """
    SELECT id, symbol, label, score, entry, stop_loss, target,
           EXTRACT(EPOCH FROM (expires_at - NOW()))::INT AS exp_seconds
    FROM pending_approvals
    WHERE status='PENDING'
      AND expires_at > NOW()
    ORDER BY score DESC NULLS LAST, created_at DESC
    LIMIT %s
    """
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(q, (limit,))
            return [dict(r) for r in cur.fetchall()]


def db_get_prebuy(prebuy_id: str) -> Optional[Dict[str, Any]]:
    ensure_tables()
    q = """
    SELECT *
    FROM pending_approvals
    WHERE id=%s
    LIMIT 1
    """
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(q, (prebuy_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def db_mark_no(prebuy_id: str, by: str) -> bool:
    ensure_tables()
    q = """
    UPDATE pending_approvals
    SET status='NO', note=COALESCE(note,'') || %s
    WHERE id=%s
      AND status IN ('PENDING','APPROVED')
    """
    note = f"\nNO by {by}"
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (note, prebuy_id))
            ok = cur.rowcount > 0
        conn.commit()
    return ok


def db_approve(prebuy_id: str, amount: float, by: str) -> Optional[Dict[str, Any]]:
    """
    Zet status op APPROVED als hij nog PENDING en niet verlopen is.
    Return row na update.
    """
    ensure_tables()
    q = """
    UPDATE pending_approvals
    SET status='APPROVED',
        approved_amount=%s,
        approved_by=%s,
        approved_at=NOW()
    WHERE id=%s
      AND status='PENDING'
      AND expires_at > NOW()
    RETURNING id, symbol, label, score, entry, stop_loss, target
    """
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(q, (amount, by, prebuy_id))
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


# ==========================================================
# MAIN ROUTE
# ==========================================================
@app.route("/whatsapp", methods=["POST", "GET"])
def whatsapp():
    try:
        # Optional: simpele token check (alleen als je het wilt afdwingen)
        if INTERNAL_TOKEN:
            token = (request.args.get("token") or "").strip()
            if token and token != INTERNAL_TOKEN:
                return twiml("⛔ Onjuiste token.")

        msg = (request.form.get("Body") or request.args.get("Body") or "").strip()
        from_number = (request.form.get("From") or request.args.get("From") or "").strip()

        cmd, args = parse_command(msg)

        if cmd in {"", "HELP", "H"}:
            return twiml(help_text())

        if cmd == "LIST":
            rows = db_fetch_pending(limit=10)
            if not rows:
                return twiml("Geen PENDING Pre-BUYs gevonden (of alles is verlopen).")
            lines = []
            for r in rows:
                expm = max(0, int((r.get("exp_seconds") or 0) / 60))
                lines.append(
                    f"{r['id']} | {r['symbol']} | {r.get('label','')} | score={r.get('score','?')} | "
                    f"entry={fmt(r.get('entry'))} | stop={fmt(r.get('stop_loss'))} | target={fmt(r.get('target'))} | exp {expm}m"
                )
            return twiml("\n".join(lines) + "\n\nBevestig: YES <bedrag> <ID>")

        if cmd == "TOP":
            rows = db_fetch_top(limit=5)
            if not rows:
                return twiml("Geen PENDING Pre-BUYs gevonden (of alles is verlopen).")
            lines = []
            for r in rows:
                expm = max(0, int((r.get("exp_seconds") or 0) / 60))
                lines.append(
                    f"{r['id']} | {r['symbol']} | {r.get('label','')} | score={r.get('score','?')} | "
                    f"entry={fmt(r.get('entry'))} | stop={fmt(r.get('stop_loss'))} | target={fmt(r.get('target'))} | exp {expm}m"
                )
            return twiml("\n".join(lines) + "\n\nBevestig: YES <bedrag> <ID>")

        if cmd == "NO":
            if len(args) < 1:
                return twiml("Gebruik: NO <ID>")
            prebuy_id = args[0].strip()
            ok = db_mark_no(prebuy_id, by=from_number or "unknown")
            return twiml("✅ Afgewezen (NO) opgeslagen." if ok else "⚠️ ID niet gevonden of al verwerkt.")

        if cmd == "YES":
            if len(args) < 2:
                return twiml("Gebruik: YES <bedrag> <ID>\nVoorbeeld: YES 10 PB-BTCUSDT-1771100252")

            amount_raw = args[0].strip()
            prebuy_id = args[1].strip()

            try:
                amount = float(amount_raw)
            except Exception:
                return twiml("⚠️ Bedrag ongeldig. Toegestaan: 5, 10, 15, 20, 30, 100")

            if int(amount) not in ALLOWED_AMOUNTS:
                return twiml("⚠️ Bedrag niet toegestaan. Toegestaan: 5, 10, 15, 20, 30, 100")

            row = db_approve(prebuy_id, amount=amount, by=from_number or "unknown")
            if not row:
                return twiml("⚠️ Kan niet goedkeuren. (ID niet PENDING of verlopen)")

            # BUY koppeling (LIVE) – werkt nu omdat paper_trader DB approvals ondersteunt
            try:
                from trading.paper_trader import buy_eur  # live trader

                res = buy_eur(
                    symbol=row["symbol"],
                    amount_eur=float(amount),             # <-- let op: amount_eur
                    stop_loss=float(row["stop_loss"]),
                    target=float(row["target"]),
                    prebuy_id=row["id"],
                )

                if not isinstance(res, dict) or not res.get("ok"):
                    return twiml(
                        "✅ GOEDKEURING ONTVANGEN\n"
                        f"ID: {row['id']}\n"
                        f"Inzet: €{int(amount)}\n"
                        f"{row['symbol']} | {row.get('label','') or ''} | score={row.get('score','?')}\n"
                        f"entry={fmt(row.get('entry'))} stop={fmt(row.get('stop_loss'))} target={fmt(row.get('target'))}\n\n"
                        f"⚠️ BUY niet uitgevoerd: {res}"
                    )

                return twiml(
                    "✅ GOEDKEURING ONTVANGEN + BUY uitgevoerd\n"
                    f"ID: {row['id']}\n"
                    f"Inzet: €{int(amount)}\n"
                    f"{row['symbol']} | {row.get('label','') or ''} | score={row.get('score','?')}\n"
                    f"entry={fmt(row.get('entry'))} stop={fmt(row.get('stop_loss'))} target={fmt(row.get('target'))}\n"
                    f"BUY: qty={res.get('qty')} @ {res.get('price')}"
                )

            except Exception as e:
                return twiml(
                    "✅ GOEDKEURING ONTVANGEN\n"
                    f"ID: {row['id']}\n"
                    f"Inzet: €{int(amount)}\n"
                    f"{row['symbol']} | {row.get('label','') or ''} | score={row.get('score','?')}\n"
                    f"entry={fmt(row.get('entry'))} stop={fmt(row.get('stop_loss'))} target={fmt(row.get('target'))}\n\n"
                    f"⚠️ BUY niet uitgevoerd (exception): {str(e)}"
                )

        return twiml("Onbekend command. Stuur HELP voor opties.")

    except Exception as e:
        # Dit is je “Interne fout in webhook”
        print("WEBHOOK ERROR:", e, flush=True)
        print(traceback.format_exc(), flush=True)
        # Als Twilio hier komt: we geven korte fout terug
        return twiml(f"❌ Webhook error: {type(e).__name__}: {str(e)}")


def fmt(v: Any) -> str:
    try:
        if v is None:
            return "?"
        x = float(v)
        # kleine coins: meer decimals
        if x < 1:
            return f"{x:.6f}"
        if x < 100:
            return f"{x:.4f}"
        return f"{x:.2f}"
    except Exception:
        return str(v)


if __name__ == "__main__":
    # Render bindt PORT
    port = int(os.getenv("PORT") or "10000")
    app.run(host="0.0.0.0", port=port)
