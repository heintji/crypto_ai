from __future__ import annotations

import os
import time
import traceback
from typing import Any, Dict, Optional, Tuple, List

import psycopg2
from psycopg2.extras import Json
from flask import Flask, request, Response

app = Flask(__name__)

# =========================
# ENV
# =========================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()
TRADING_MODE = (os.getenv("TRADING_MODE") or "paper").strip().lower()  # paper/live

ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

HELP_TEXT = (
    "Commands:\n"
    "HELP\n"
    "LIST (laatste PENDING pre-buys)\n"
    "TOP (beste 5 op score)\n"
    "YES <bedrag> <ID>\n"
    "NO <ID>\n\n"
    "Voorbeeld: YES 10 PB-BTCUSDT-1771100252"
)

# =========================
# TwiML helper
# =========================
def escape_xml(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def twiml(msg: str) -> Response:
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escape_xml(msg)}</Message></Response>'
    return Response(xml, mimetype="text/xml")

# =========================
# DB helpers
# =========================
_DB_READY = False

def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt in de Web Service env (Render > crypto_ai > Environment).")
    return psycopg2.connect(DATABASE_URL)

def ensure_tables() -> None:
    global _DB_READY
    if _DB_READY:
        return

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pending_approvals (
        id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        setup_type TEXT,
        regime TEXT,
        market_regime TEXT,
        score INT,
        label TEXT,
        grade TEXT,
        entry NUMERIC,
        stop NUMERIC,
        stop_loss NUMERIC,
        target NUMERIC,
        created_at BIGINT,
        expires_at BIGINT,
        status TEXT NOT NULL DEFAULT 'PENDING',
        payload_json JSONB,
        bot_confidence INT,
        approved_amount INT,
        approved_at BIGINT,
        rejected_at BIGINT
    );
    """)

    # safety: status column exists
    cur.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name='pending_approvals' AND column_name='status'
        ) THEN
            ALTER TABLE pending_approvals ADD COLUMN status TEXT NOT NULL DEFAULT 'PENDING';
        END IF;
    END$$;
    """)

    conn.commit()
    cur.close()
    conn.close()
    _DB_READY = True

# =========================
# Commands
# =========================
def parse_command(body: str) -> Tuple[str, List[str]]:
    parts = (body or "").strip().split()
    if not parts:
        return "", []
    return parts[0].upper(), parts[1:]

def now_ts() -> int:
    return int(time.time())

def is_expired(expires_at: Optional[int]) -> bool:
    return bool(expires_at and expires_at < now_ts())

def fetch_pending(limit: int = 10) -> List[Dict[str, Any]]:
    ensure_tables()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, symbol, score, grade, entry, stop_loss, stop, target, created_at, expires_at
        FROM pending_approvals
        WHERE status='PENDING'
        ORDER BY created_at DESC
        LIMIT %s;
    """, (limit,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    out = []
    for r in rows:
        out.append({
            "id": r[0],
            "symbol": r[1],
            "score": r[2],
            "grade": r[3],
            "entry": float(r[4]) if r[4] is not None else None,
            "stop_loss": float(r[5]) if r[5] is not None else (float(r[6]) if r[6] is not None else None),
            "target": float(r[7]) if r[7] is not None else None,
            "created_at": int(r[8]) if r[8] is not None else None,
            "expires_at": int(r[9]) if r[9] is not None else None,
        })
    return out

def fetch_top(limit: int = 5) -> List[Dict[str, Any]]:
    ensure_tables()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, symbol, score, grade, entry, stop_loss, stop, target, expires_at
        FROM pending_approvals
        WHERE status='PENDING'
        ORDER BY score DESC NULLS LAST, created_at DESC
        LIMIT %s;
    """, (limit,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    out = []
    for r in rows:
        out.append({
            "id": r[0],
            "symbol": r[1],
            "score": r[2],
            "grade": r[3],
            "entry": float(r[4]) if r[4] is not None else None,
            "stop_loss": float(r[5]) if r[5] is not None else (float(r[6]) if r[6] is not None else None),
            "target": float(r[7]) if r[7] is not None else None,
            "expires_at": int(r[8]) if r[8] is not None else None,
        })
    return out

def find_pending_by_id(prebuy_id: str) -> Optional[Dict[str, Any]]:
    ensure_tables()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, symbol, score, grade, entry, stop_loss, stop, target, expires_at
        FROM pending_approvals
        WHERE id=%s AND status='PENDING'
        LIMIT 1;
    """, (prebuy_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "symbol": row[1],
        "score": row[2],
        "grade": row[3],
        "entry": float(row[4]) if row[4] is not None else None,
        "stop_loss": float(row[5]) if row[5] is not None else (float(row[6]) if row[6] is not None else None),
        "target": float(row[7]) if row[7] is not None else None,
        "expires_at": int(row[8]) if row[8] is not None else None,
    }

def mark_approved(prebuy_id: str, amount: int) -> None:
    ensure_tables()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE pending_approvals
        SET status='APPROVED', approved_amount=%s, approved_at=%s
        WHERE id=%s;
    """, (amount, now_ts(), prebuy_id))
    conn.commit()
    cur.close()
    conn.close()

def mark_rejected(prebuy_id: str) -> None:
    ensure_tables()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE pending_approvals
        SET status='REJECTED', rejected_at=%s
        WHERE id=%s AND status='PENDING';
    """, (now_ts(), prebuy_id))
    conn.commit()
    cur.close()
    conn.close()

def execute_buy(symbol: str, amount_eur: int, entry: Optional[float], stop: Optional[float], target: Optional[float]) -> str:
    # Veilig: standaard paper. Live moet je bewust koppelen.
    if TRADING_MODE == "live":
        return "LIVE mode staat aan, maar live Bitvavo BUY is nog niet gekoppeld."
    try:
        from trading.paper_trader import buy_eur  # type: ignore
        buy_eur(symbol=symbol, eur_amount=amount_eur, entry_hint=entry, stop_loss=stop, target=target)
        return "Paper BUY uitgevoerd ✅"
    except Exception:
        print("PAPER BUY ERROR:\n", traceback.format_exc())
        return "Paper BUY faalde. Check Render logs."

# =========================
# Routes
# =========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp_inbound():
    body_raw = (request.form.get("Body") or "").strip()
    from_number = (request.form.get("From") or "").strip()
    cmd, args = parse_command(body_raw)

    print(f"WHATSAPP_IN from={from_number} body={body_raw}")

    try:
        if cmd in ("HELP", "?") or cmd == "":
            return twiml(HELP_TEXT)

        if cmd == "LIST":
            items = fetch_pending(limit=10)
            if not items:
                return twiml("Geen PENDING pre-buys.\n\n" + HELP_TEXT)

            lines = []
            for it in items:
                exp = "exp ?" if not it["expires_at"] else f"exp {max(0, it['expires_at']-now_ts())//60}m"
                lines.append(
                    f"{it['id']} | {it['symbol']} | {it['grade']} | score={it['score']} | "
                    f"entry={it['entry']} | stop={it['stop_loss']} | target={it['target']} | {exp}"
                )
            return twiml("\n".join(lines) + "\n\nBevestig: YES <bedrag> <ID>")

        if cmd == "TOP":
            items = fetch_top(limit=5)
            if not items:
                return twiml("Geen PENDING pre-buys.\n\n" + HELP_TEXT)

            lines = []
            for it in items:
                exp = "exp ?" if not it["expires_at"] else f"exp {max(0, it['expires_at']-now_ts())//60}m"
                lines.append(
                    f"{it['id']} | {it['symbol']} | {it['grade']} | score={it['score']} | "
                    f"entry={it['entry']} | stop={it['stop_loss']} | target={it['target']} | {exp}"
                )
            return twiml("Top 5 (op score):\n" + "\n".join(lines) + "\n\nBevestig: YES <bedrag> <ID>")

        if cmd == "YES":
            if len(args) < 2:
                return twiml("Gebruik: YES <bedrag> <ID>\nVoorbeeld: YES 10 PB-BTCUSDT-1771100252")

            try:
                amount = int(args[0])
            except ValueError:
                return twiml("Bedrag ongeldig. Toegestaan: 5,10,15,20,30,100")

            prebuy_id = args[1].strip()

            if amount not in ALLOWED_AMOUNTS:
                return twiml("Bedrag niet toegestaan. Kies: 5,10,15,20,30,100")

            pb = find_pending_by_id(prebuy_id)
            if not pb:
                return twiml("Geen PENDING Pre-BUY gevonden met die ID. Doe eerst LIST of TOP.")

            if is_expired(pb.get("expires_at")):
                mark_rejected(prebuy_id)
                return twiml("Deze Pre-BUY is verlopen. (status -> REJECTED)")

            mark_approved(prebuy_id, amount)

            buy_result = execute_buy(
                symbol=pb["symbol"],
                amount_eur=amount,
                entry=pb.get("entry"),
                stop=pb.get("stop_loss"),
                target=pb.get("target"),
            )

            msg = (
                "✅ GOEDKEURING ONTVANGEN\n"
                f"ID: {pb['id']}\n"
                f"Inzet: €{amount}\n"
                f"{pb['symbol']} | {pb['grade']} | score={pb['score']}\n"
                f"entry={pb.get('entry')} stop={pb.get('stop_loss')} target={pb.get('target')}\n\n"
                f"{buy_result}"
            )
            return twiml(msg)

        if cmd == "NO":
            if len(args) < 1:
                return twiml("Gebruik: NO <ID>")
            prebuy_id = args[0].strip()
            mark_rejected(prebuy_id)
            return twiml(f"❌ Afgewezen: {prebuy_id}")

        return twiml("Onbekend commando. Stuur HELP.")

    except Exception as e:
        # Hier sturen we de echte oorzaak terug (zodat jij direct ziet wat ontbreekt)
        err = f"{type(e).__name__}: {str(e)}"
        print("WHATSAPP ERROR:\n", traceback.format_exc())
        return twiml("❌ Webhook error:\n" + err + "\n\nFix: check Render env/logs.")

@app.route("/internal/prebuy", methods=["POST"])
def internal_prebuy():
    try:
        token = (request.headers.get("X-Internal-Token") or "").strip()
        if not INTERNAL_TOKEN or token != INTERNAL_TOKEN:
            return ("Unauthorized", 401)

        ensure_tables()

        data = request.get_json(force=True) or {}
        prebuy_id = str(data.get("id") or "").strip()
        symbol = str(data.get("symbol") or data.get("coin") or "").strip()
        if not prebuy_id or not symbol:
            return ("Missing id/symbol", 400)

        score = data.get("score")
        grade = data.get("grade") or data.get("label")
        entry = data.get("entry")
        stop_loss = data.get("stop_loss") if data.get("stop_loss") is not None else data.get("stop")
        target = data.get("target")
        created_at = int(data.get("created_at") or now_ts())
        expires_at = int(data.get("expires_at") or (created_at + 4 * 60 * 60))

        payload_json = data.get("payload_json") or data

        conn = db_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO pending_approvals (
                id, symbol, score, grade, entry, stop_loss, target,
                created_at, expires_at, status, payload_json
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)
            ON CONFLICT (id) DO NOTHING;
        """, (prebuy_id, symbol, score, grade, entry, stop_loss, target, created_at, expires_at, Json(payload_json)))
        conn.commit()
        cur.close()
        conn.close()

        return ("OK", 200)

    except Exception:
        print("INTERNAL_PREBUY ERROR:\n", traceback.format_exc())
        return ("ERROR", 500)

if __name__ == "__main__":
    port = int(os.getenv("PORT") or "10000")
    app.run(host="0.0.0.0", port=port)
