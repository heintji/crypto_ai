from __future__ import annotations

import os
import json
import time
import re
import traceback
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, request, Response, jsonify

# Twilio is optional (maar jij gebruikt 'm)
try:
    from twilio.twiml.messaging_response import MessagingResponse
except Exception:  # pragma: no cover
    MessagingResponse = None  # type: ignore

app = Flask(__name__)

# ==========================================================
# ENV
# ==========================================================
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

# Fallback file (mag blijven bestaan)
DATA_DIR = (os.getenv("DATA_DIR") or "/data").rstrip("/")
PENDING_PATH = os.getenv("PENDING_PATH", os.path.join(DATA_DIR, "pending_approvals.json"))

ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

# ==========================================================
# BASIC HELPERS
# ==========================================================
def now_utc() -> int:
    return int(time.time())

def log(msg: str) -> None:
    print(msg, flush=True)

def twiml(text: str) -> Response:
    """
    Altijd TwiML teruggeven aan Twilio.
    """
    if MessagingResponse is None:
        # Fallback: plain text (Twilio vindt TwiML fijner, maar dit voorkomt crash)
        return Response(text, status=200, mimetype="text/plain")

    r = MessagingResponse()
    r.message(text)
    return Response(str(r), status=200, mimetype="application/xml")

def ensure_pending_file() -> None:
    os.makedirs(os.path.dirname(PENDING_PATH) or ".", exist_ok=True)
    if not os.path.exists(PENDING_PATH):
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump({"items": []}, f, indent=2)

def read_pending_json() -> List[Dict[str, Any]]:
    try:
        ensure_pending_file()
        with open(PENDING_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items", [])
        if isinstance(items, list):
            return items
        return []
    except Exception:
        return []

def write_pending_json(items: List[Dict[str, Any]]) -> None:
    ensure_pending_file()
    tmp = PENDING_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, indent=2, ensure_ascii=False)
    os.replace(tmp, PENDING_PATH)

# ==========================================================
# POSTGRES HELPERS
# ==========================================================
def db_available() -> bool:
    return bool(DATABASE_URL)

def db_connect():
    import psycopg2  # type: ignore
    return psycopg2.connect(DATABASE_URL)

def db_ensure_tables() -> None:
    """
    Belangrijk: deze functie voorkomt precies jouw oude error:
    psycopg2.errors.UndefinedColumn: column "status" does not exist
    """
    if not db_available():
        return

    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("""
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
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ,
            payload JSONB
        );
        """)
        cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS status TEXT;")
        cur.execute("ALTER TABLE pending_approvals ALTER COLUMN status SET DEFAULT 'PENDING';")
        cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS payload JSONB;")
        cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;")
        conn.commit()
        log("DB: pending_approvals is ready ✅")
    finally:
        conn.close()

def db_list_pending(limit: int = 10) -> List[Dict[str, Any]]:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, symbol, setup_type, regime, score, label, entry, stop, target, status,
                   EXTRACT(EPOCH FROM created_at)::bigint,
                   COALESCE(EXTRACT(EPOCH FROM expires_at)::bigint, 0),
                   COALESCE(payload::text, '')
            FROM pending_approvals
            WHERE status = 'PENDING'
            ORDER BY created_at DESC
            LIMIT %s;
        """, (limit,))
        rows = cur.fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            payload_txt = r[12] or ""
            payload: Dict[str, Any] = {}
            if payload_txt:
                try:
                    payload = json.loads(payload_txt)
                except Exception:
                    payload = {}

            out.append({
                "id": r[0],
                "coin": r[1],
                "setup_type": r[2] or "",
                "market_regime": r[3] or "",
                "score": int(r[4] or 0),
                "grade": r[5] or "",
                "entry": float(r[6] or 0.0),
                "stop_loss": float(r[7] or 0.0),
                "target": float(r[8] or 0.0),
                "status": r[9] or "PENDING",
                "created_at": int(r[10] or 0),
                "expires_at": int(r[11] or 0),
                "payload": payload,
            })
        return out
    finally:
        conn.close()

def db_insert_pending(prebuy: Dict[str, Any]) -> None:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO pending_approvals(
            id, symbol, setup_type, regime, score, label, entry, stop, target, status, expires_at, payload
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s),%s::jsonb)
        ON CONFLICT (id) DO NOTHING;
        """, (
            prebuy["id"],
            prebuy["coin"],
            prebuy.get("setup_type", ""),
            prebuy.get("market_regime", ""),
            int(prebuy.get("score", 0)),
            prebuy.get("grade", ""),
            float(prebuy.get("entry", 0.0)),
            float(prebuy.get("stop_loss", 0.0)),
            float(prebuy.get("target", 0.0)),
            "PENDING",
            int(prebuy.get("expires_at", now_utc() + 4 * 60 * 60)),
            json.dumps(prebuy, ensure_ascii=False),
        ))
        conn.commit()
    finally:
        conn.close()

def db_update_status(prebuy_id: str, status: str) -> None:
    conn = db_connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE pending_approvals SET status=%s WHERE id=%s;", (status, prebuy_id))
        conn.commit()
    finally:
        conn.close()

# ==========================================================
# COMMAND PARSING
# ==========================================================
def parse_message(text: str) -> Tuple[str, Optional[int], Optional[str]]:
    """
    Returns: (cmd, amount, id)
    Supported:
      - LIST
      - HELP
      - YES 10 PB-...   (amount required)
      - NO PB-...       (id optional)
    """
    t = (text or "").strip()
    u = t.upper()

    if u in {"LIST", "LIJST"}:
        return ("LIST", None, None)
    if u in {"HELP", "HULP"}:
        return ("HELP", None, None)

    m = re.match(r"^\s*YES\s+(\d+)(?:\s+(\S+))?\s*$", u)
    if m:
        amt = int(m.group(1))
        pid = m.group(2)
        return ("YES", amt, pid)

    m = re.match(r"^\s*NO(?:\s+(\S+))?\s*$", u)
    if m:
        pid = m.group(1)
        return ("NO", None, pid)

    return ("UNKNOWN", None, None)

def format_prebuy_line(pb: Dict[str, Any]) -> str:
    return (
        f"{pb['id']} | {pb.get('coin','')} | {pb.get('grade','')} | "
        f"score={pb.get('score','')} | entry={pb.get('entry','')} | "
        f"stop={pb.get('stop_loss','')} | target={pb.get('target','')}"
    )

def is_expired(pb: Dict[str, Any]) -> bool:
    exp = int(pb.get("expires_at") or 0)
    if exp <= 0:
        return False
    return now_utc() > exp

# ==========================================================
# ROUTES
# ==========================================================
@app.get("/")
def health_root():
    # Render healthchecks / browser hits
    return "OK", 200

@app.get("/whatsapp")
def whatsapp_get_ok():
    # voorkomt rare 502's bij GET probes
    return "OK", 200

@app.post("/whatsapp")
def whatsapp_webhook():
    """
    Twilio WhatsApp inbound webhook
    """
    try:
        body = request.form.get("Body", "") or ""
        from_number = request.form.get("From", "") or ""
        cmd, amt, pid = parse_message(body)

        log(f"WHATSAPP_IN: from={from_number} body={body} db={'YES' if db_available() else 'NO'} pending_file={PENDING_PATH}")

        # Zorg dat DB tabel bestaat als DB aan staat
        if db_available():
            try:
                db_ensure_tables()
            except Exception:
                log("⚠️ DB ensure failed (gaat door met fallback file).")
                traceback.print_exc()

        # ------------------------
        # HELP
        # ------------------------
        if cmd == "HELP":
            return twiml(
                "Commands:\n"
                "LIST\n"
                "YES <bedrag> <ID>\n"
                "NO <ID>\n"
                "Voorbeeld: YES 10 PB-BTCUSDT-123"
            )

        # ------------------------
        # LIST
        # ------------------------
        if cmd == "LIST":
            items: List[Dict[str, Any]] = []
            if db_available():
                try:
                    items = db_list_pending(limit=10)
                except Exception:
                    log("⚠️ DB list failed -> fallback json")
                    traceback.print_exc()
                    items = read_pending_json()
            else:
                items = read_pending_json()

            # filter expired (en update status in DB)
            alive: List[Dict[str, Any]] = []
            for pb in items:
                if is_expired(pb):
                    if db_available() and pb.get("id"):
                        try:
                            db_update_status(pb["id"], "EXPIRED")
                        except Exception:
                            pass
                    continue
                alive.append(pb)

            if not alive:
                return twiml("Geen PENDING Pre-BUY’s op dit moment.")

            lines = ["PENDING Pre-BUY’s:"]
            for pb in alive[:10]:
                lines.append(format_prebuy_line(pb))
            lines.append("\nBevestig: YES <bedrag> <ID>\nVoorbeeld: YES 10 " + alive[0]["id"])
            return twiml("\n".join(lines))

        # ------------------------
        # YES
        # ------------------------
        if cmd == "YES":
            if amt is None or amt not in ALLOWED_AMOUNTS:
                return twiml(f"Bedrag niet toegestaan. Kies: {sorted(ALLOWED_AMOUNTS)}\nVoorbeeld: YES 10 PB-...")

            # haal pending list
            items: List[Dict[str, Any]] = []
            if db_available():
                try:
                    items = db_list_pending(limit=25)
                except Exception:
                    items = read_pending_json()
            else:
                items = read_pending_json()

            # kies pb: op ID als meegegeven, anders nieuwste
            chosen: Optional[Dict[str, Any]] = None
            if pid:
                for pb in items:
                    if str(pb.get("id", "")).upper() == str(pid).upper():
                        chosen = pb
                        break
            else:
                chosen = items[0] if items else None

            if not chosen:
                return twiml("Geen Pre-BUY gevonden (of verkeerde ID). Stuur LIST om te kijken.")

            if is_expired(chosen):
                if db_available():
                    try:
                        db_update_status(chosen["id"], "EXPIRED")
                    except Exception:
                        pass
                return twiml("Deze Pre-BUY is verlopen. Stuur LIST voor nieuwe.")

            # ✅ Hier start normaal jouw BUY flow (paper_trader / live trader)
            # Voor nu: alleen status naar APPROVED zetten (en later koppelen we execution)
            if db_available():
                try:
                    db_update_status(chosen["id"], "APPROVED")
                except Exception:
                    traceback.print_exc()

            msg = (
                f"✅ GOEDKEURING ONTVANGEN\n"
                f"ID: {chosen['id']}\n"
                f"Inzet: €{amt}\n"
                f"{chosen.get('coin','')} | {chosen.get('grade','')} | score={chosen.get('score','')}\n"
                f"entry={chosen.get('entry','')} stop={chosen.get('stop_loss','')} target={chosen.get('target','')}\n"
                f"Volgende stap: BUY koppeling (paper_trader/live) uitvoeren."
            )
            return twiml(msg)

        # ------------------------
        # NO
        # ------------------------
        if cmd == "NO":
            items: List[Dict[str, Any]] = []
            if db_available():
                try:
                    items = db_list_pending(limit=25)
                except Exception:
                    items = read_pending_json()
            else:
                items = read_pending_json()

            chosen: Optional[Dict[str, Any]] = None
            if pid:
                for pb in items:
                    if str(pb.get("id", "")).upper() == str(pid).upper():
                        chosen = pb
                        break
            else:
                chosen = items[0] if items else None

            if not chosen:
                return twiml("Geen Pre-BUY om te weigeren. Stuur LIST.")

            if db_available():
                try:
                    db_update_status(chosen["id"], "REJECTED")
                except Exception:
                    traceback.print_exc()

            return twiml(f"❌ Afgewezen: {chosen['id']}")

        # ------------------------
        # UNKNOWN
        # ------------------------
        return twiml("Onbekend commando. Stuur HELP of LIST.")

    except Exception:
        traceback.print_exc()
        # ✅ Super belangrijk: Twilio moet altijd response krijgen, anders “geen reactie”
        return twiml("⚠️ Error in webhook. Probeer opnieuw of stuur HELP.")

@app.post("/internal/prebuy")
def internal_prebuy():
    """
    Endpoint waar multi_coin_score.py Pre-BUY’s heen POST.
    Deze endpoint slaat op (DB of file) + kan later WhatsApp push doen.
    """
    try:
        token = request.headers.get("X-Internal-Token", "")
        if not INTERNAL_TOKEN or token != INTERNAL_TOKEN:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        prebuy = request.get_json(force=True, silent=False) or {}
        if not prebuy.get("id"):
            return jsonify({"ok": False, "error": "missing id"}), 400

        # Ensure DB schema
        if db_available():
            db_ensure_tables()

        # Store
        if db_available():
            db_insert_pending(prebuy)
        else:
            items = read_pending_json()
            # dedupe op id
            if not any(x.get("id") == prebuy["id"] for x in items):
                items.insert(0, prebuy)
                write_pending_json(items)

        return jsonify({"ok": True, "stored": True, "id": prebuy["id"]}), 200

    except Exception:
        traceback.print_exc()
        return jsonify({"ok": False, "error": "internal error"}), 500

if __name__ == "__main__":
    # lokaal draaien
    port = int(os.getenv("PORT") or "10000")
    app.run(host="0.0.0.0", port=port)
