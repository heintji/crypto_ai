# whatsapp_webhook.py
from __future__ import annotations

import os
import time
import json
import traceback
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
from flask import Flask, request, Response

# Twilio (WhatsApp)
from twilio.twiml.messaging_response import MessagingResponse

app = Flask(__name__)

# =========================
# ENV
# =========================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()
WEBHOOK_BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or "").strip().rstrip("/")

LIVE_TRADING = (os.getenv("LIVE_TRADING") or "0").strip() == "1"
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

# hoeveel items tonen
LIST_LIMIT = int(os.getenv("LIST_LIMIT") or "8")
TOP_LIMIT = int(os.getenv("TOP_LIMIT") or "5")

# =========================
# DB helpers
# =========================
def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt (Render env var).")
    return psycopg2.connect(DATABASE_URL)

def ensure_tables() -> None:
    """
    pending_approvals tabel voor Pre-BUY opslag.
    We bewaren created_at/expires_at als BIGINT epoch seconds om datetime-bugs te voorkomen.
    """
    sql = """
    CREATE TABLE IF NOT EXISTS pending_approvals (
        id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        setup_type TEXT,
        regime TEXT,
        score INT,
        label TEXT,
        grade TEXT,
        entry NUMERIC,
        stop NUMERIC,
        target NUMERIC,
        created_at BIGINT,
        expires_at BIGINT,
        status TEXT NOT NULL DEFAULT 'PENDING',
        payload_json JSONB
    );

    CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_approvals(status);
    CREATE INDEX IF NOT EXISTS idx_pending_score ON pending_approvals(score);
    CREATE INDEX IF NOT EXISTS idx_pending_created ON pending_approvals(created_at);
    """
    conn = db_conn()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(sql)
        cur.close()
    finally:
        conn.close()

def now_ts() -> int:
    return int(time.time())

def as_int_ts(value: Any) -> int:
    """
    Fix voor jouw error:
    TypeError: int() argument must be ... not datetime.datetime
    We accepteren:
    - int/float
    - string digits
    - datetime (heeft .timestamp)
    - None => 0
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    # datetime-like
    if hasattr(value, "timestamp"):
        try:
            return int(value.timestamp())
        except Exception:
            return 0
    # string
    try:
        s = str(value).strip()
        if s.isdigit():
            return int(s)
    except Exception:
        pass
    return 0

def fetch_pending(limit: int = LIST_LIMIT) -> List[Dict[str, Any]]:
    conn = db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT *
            FROM pending_approvals
            WHERE status='PENDING'
            ORDER BY created_at DESC NULLS LAST
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def fetch_top_pending(limit: int = TOP_LIMIT) -> List[Dict[str, Any]]:
    conn = db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT *
            FROM pending_approvals
            WHERE status='PENDING'
            ORDER BY score DESC NULLS LAST, created_at DESC NULLS LAST
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def fetch_pending_by_id(pb_id: str) -> Optional[Dict[str, Any]]:
    conn = db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT *
            FROM pending_approvals
            WHERE id=%s
            """,
            (pb_id,),
        )
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    finally:
        conn.close()

def update_status(pb_id: str, status: str) -> None:
    conn = db_conn()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "UPDATE pending_approvals SET status=%s WHERE id=%s",
            (status, pb_id),
        )
        cur.close()
    finally:
        conn.close()

def upsert_prebuy(payload: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Wordt gebruikt door /internal/prebuy (optioneel).
    payload verwacht keys zoals:
    id, symbol, setup_type, regime, score, grade/label, entry, stop/stop_loss, target, created_at, expires_at, payload_json
    """
    pb_id = str(payload.get("id") or "").strip()
    symbol = str(payload.get("symbol") or payload.get("coin") or "").strip()
    if not pb_id or not symbol:
        return False, "id/symbol ontbreekt"

    setup_type = payload.get("setup_type")
    regime = payload.get("regime") or payload.get("market_regime")
    score = payload.get("score")
    label = payload.get("label")
    grade = payload.get("grade")

    entry = payload.get("entry")
    stop = payload.get("stop") if payload.get("stop") is not None else payload.get("stop_loss")
    target = payload.get("target")

    created_at = as_int_ts(payload.get("created_at"))
    expires_at = as_int_ts(payload.get("expires_at"))

    payload_json = payload.get("payload_json")
    if payload_json is None:
        # fallback: alles bewaren als JSON
        payload_json = payload

    conn = db_conn()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO pending_approvals
            (id, symbol, setup_type, regime, score, label, grade, entry, stop, target, created_at, expires_at, status, payload_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s::jsonb)
            ON CONFLICT (id) DO UPDATE SET
              symbol=EXCLUDED.symbol,
              setup_type=EXCLUDED.setup_type,
              regime=EXCLUDED.regime,
              score=EXCLUDED.score,
              label=EXCLUDED.label,
              grade=EXCLUDED.grade,
              entry=EXCLUDED.entry,
              stop=EXCLUDED.stop,
              target=EXCLUDED.target,
              created_at=EXCLUDED.created_at,
              expires_at=EXCLUDED.expires_at,
              status='PENDING',
              payload_json=EXCLUDED.payload_json
            """,
            (
                pb_id,
                symbol,
                setup_type,
                regime,
                score,
                label,
                grade,
                entry,
                stop,
                target,
                created_at,
                expires_at,
                json.dumps(payload_json),
            ),
        )
        cur.close()
        return True, f"upsert OK: {pb_id}"
    finally:
        conn.close()

# =========================
# BUY execution wrapper (FIX)
# =========================
def try_execute_buy(row: Dict[str, Any], stake_eur: int) -> Tuple[bool, str]:
    """
    Oplossing voor:
    buy_eur() got an unexpected keyword argument 'eur_amount'
    -> we proberen meerdere signatures, zodat het altijd werkt.
    """
    symbol = (row.get("symbol") or "").strip()
    entry = float(row.get("entry") or 0.0)
    stop = float(row.get("stop") or 0.0)
    target = float(row.get("target") or 0.0)

    if not symbol:
        return False, "symbol ontbreekt"

    # LIVE (optioneel)
    if LIVE_TRADING:
        try:
            from trading.bitvavo_trader import buy_eur as live_buy_eur  # type: ignore
            res = live_buy_eur(symbol=symbol, eur_amount=stake_eur)
            return True, f"LIVE BUY OK: {res}"
        except Exception as e:
            return False, f"LIVE BUY faalde: {e}"

    # PAPER (default)
    try:
        from trading.paper_trader import buy_eur  # type: ignore

        # 1) eur_amount + extra hints
        try:
            res = buy_eur(symbol=symbol, eur_amount=stake_eur, entry_hint=entry, stop_loss=stop, target=target)
            return True, f"PAPER BUY OK: {res}"
        except TypeError:
            pass

        # 2) amount
        try:
            res = buy_eur(symbol=symbol, amount=stake_eur)
            return True, f"PAPER BUY OK: {res}"
        except TypeError:
            pass

        # 3) stake_eur
        try:
            res = buy_eur(symbol=symbol, stake_eur=stake_eur)
            return True, f"PAPER BUY OK: {res}"
        except TypeError:
            pass

        # 4) positional
        try:
            res = buy_eur(symbol, stake_eur)
            return True, f"PAPER BUY OK: {res}"
        except TypeError as e:
            return False, f"PAPER BUY signature mismatch: {e}"

    except Exception as e:
        return False, f"PAPER BUY import/fout: {e}"

# =========================
# Formatting
# =========================
def fmt_row(r: Dict[str, Any]) -> str:
    pb_id = r.get("id")
    symbol = r.get("symbol")
    grade = r.get("grade") or r.get("label") or ""
    score = r.get("score")
    entry = r.get("entry")
    stop = r.get("stop")
    target = r.get("target")
    exp = as_int_ts(r.get("expires_at"))
    mins = max(0, int((exp - now_ts()) / 60)) if exp else 0
    return f"{pb_id} | {symbol} | {grade} | score={score} | entry={entry} | stop={stop} | target={target} | exp {mins}m"

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

def twilio_reply(text: str) -> Response:
    resp = MessagingResponse()
    resp.message(text)
    return Response(str(resp), mimetype="application/xml")

# =========================
# Routes
# =========================
@app.get("/")
def health():
    return "OK", 200

@app.post("/internal/prebuy")
def internal_prebuy():
    # beveiliging zodat niet iedereen je DB kan volspammen
    token = (request.headers.get("X-Internal-Token") or "").strip()
    if not INTERNAL_TOKEN or token != INTERNAL_TOKEN:
        return {"ok": False, "error": "unauthorized"}, 401

    try:
        payload = request.get_json(force=True, silent=False) or {}
        ok, info = upsert_prebuy(payload)
        return {"ok": ok, "info": info}, (200 if ok else 400)
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.post("/whatsapp")
def whatsapp():
    """
    Twilio WhatsApp webhook endpoint.
    """
    try:
        ensure_tables()

        body_raw = request.form.get("Body", "") or ""
        msg = body_raw.strip()
        msg_u = msg.upper()

        from_number = request.form.get("From", "") or ""

        # HELP
        if msg_u == "HELP":
            return twilio_reply(help_text())

        # LIST
        if msg_u == "LIST":
            rows = fetch_pending(LIST_LIMIT)
            if not rows:
                return twilio_reply("Geen PENDING Pre-BUY’s gevonden.")
            lines = ["PENDING pre-buys (laatste):"]
            for r in rows:
                lines.append(fmt_row(r))
            lines.append("\nBevestig: YES <bedrag> <ID>")
            return twilio_reply("\n".join(lines))

        # TOP
        if msg_u == "TOP":
            rows = fetch_top_pending(TOP_LIMIT)
            if not rows:
                return twilio_reply("Geen PENDING Pre-BUY’s gevonden.")
            lines = ["TOP (beste op score):"]
            for r in rows:
                lines.append(fmt_row(r))
            lines.append("\nBevestig: YES <bedrag> <ID>")
            return twilio_reply("\n".join(lines))

        # NO <ID>
        if msg_u.startswith("NO "):
            parts = msg.split()
            if len(parts) < 2:
                return twilio_reply("Gebruik: NO <ID>")
            pb_id = parts[1].strip()
            row = fetch_pending_by_id(pb_id)
            if not row:
                return twilio_reply("Geen Pre-BUY gevonden met die ID.")
            update_status(pb_id, "REJECTED")
            return twilio_reply(f"❌ Afgewezen: {pb_id}")

        # YES <bedrag> <ID>
        if msg_u.startswith("YES "):
            parts = msg.split()
            if len(parts) < 3:
                return twilio_reply("Gebruik: YES <bedrag> <ID>\nVoorbeeld: YES 10 PB-BTCUSDT-123")

            try:
                amount = int(parts[1])
            except Exception:
                return twilio_reply("Bedrag moet een getal zijn. Toegestaan: 5,10,15,20,30,100")

            if amount not in ALLOWED_AMOUNTS:
                return twilio_reply("Bedrag niet toegestaan. Toegestaan: 5,10,15,20,30,100")

            pb_id = parts[2].strip()
            row = fetch_pending_by_id(pb_id)
            if not row:
                return twilio_reply("Geen PENDING Pre-BUY gevonden (met die ID). Doe eerst LIST of TOP.")

            if (row.get("status") or "").upper() != "PENDING":
                return twilio_reply(f"Deze Pre-BUY is niet meer PENDING (status={row.get('status')}).")

            # expiry check
            exp = as_int_ts(row.get("expires_at"))
            if exp and now_ts() > exp:
                update_status(pb_id, "EXPIRED")
                return twilio_reply("⏱️ Deze Pre-BUY is verlopen. Doe LIST of TOP voor nieuwe.")

            # OK -> approve + attempt BUY
            update_status(pb_id, "APPROVED")

            ok, info = try_execute_buy(row, amount)
            if ok:
                update_status(pb_id, "CONSUMED")
                return twilio_reply(
                    "✅ GOEDKEURING ONTVANGEN\n"
                    f"ID: {pb_id}\n"
                    f"Inzet: €{amount}\n"
                    f"{row.get('symbol')} | {row.get('grade') or row.get('label')}\n"
                    f"entry={row.get('entry')} stop={row.get('stop')} target={row.get('target')}\n\n"
                    f"✅ BUY uitgevoerd.\n{info}"
                )
            else:
                # keep APPROVED zodat jij later kunt herhalen / debuggen
                return twilio_reply(
                    "✅ GOEDKEURING ONTVANGEN\n"
                    f"ID: {pb_id}\n"
                    f"Inzet: €{amount}\n"
                    f"{row.get('symbol')} | {row.get('grade') or row.get('label')}\n"
                    f"entry={row.get('entry')} stop={row.get('stop')} target={row.get('target')}\n\n"
                    f"⚠️ BUY niet uitgevoerd: {info}"
                )

        # Onbekend commando
        return twilio_reply("Onbekend commando. Stuur HELP.")

    except Exception as e:
        # Log naar Render + nette WhatsApp response
        print("WEBHOOK_ERROR", str(e))
        traceback.print_exc()
        return twilio_reply(f"❌ Interne fout in webhook. Check Render logs (Traceback).")

# =========================
# Main
# =========================
if __name__ == "__main__":
    # Render zet PORT
    port = int(os.getenv("PORT") or "10000")
    ensure_tables()
    app.run(host="0.0.0.0", port=port, debug=False)

