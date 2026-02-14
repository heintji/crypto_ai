from __future__ import annotations

import os
import sys
import json
import time
import traceback
import re
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, request, Response

# ==========================================================
# PROJECT ROOT
# ==========================================================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ==========================================================
# OPTIONAL: paper execution (blijft jouw rolverdeling)
# whatsapp_webhook = menselijke gate + start BUY
# ==========================================================
try:
    from trading.paper_trader import buy_eur  # pas aan als jouw functie anders heet
except Exception:
    buy_eur = None  # type: ignore

# ==========================================================
# ENV
# ==========================================================
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()

TWILIO_ACCOUNT_SID = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "whatsapp:+14155238886").strip()
TWILIO_WHATSAPP_TO = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()  # jouw nummer

DATABASE_URL = (os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_INTERNAL") or "").strip()

DATA_DIR = (os.getenv("DATA_DIR") or "/data").rstrip("/")
PENDING_PATH = os.getenv("PENDING_PATH", os.path.join(DATA_DIR, "pending_approvals.json"))

ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

PORT = int(os.getenv("PORT") or "10000")

app = Flask(__name__)

# ==========================================================
# HELPERS
# ==========================================================
def now_utc() -> int:
    return int(time.time())

def log(msg: str) -> None:
    print(msg, flush=True)

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def load_json(path: str, default: Any) -> Any:
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def safe_storage_ready() -> None:
    try:
        ensure_dir(DATA_DIR)
    except PermissionError:
        # fallback
        fallback = "/tmp/data"
        log(f"⚠️ Geen write access op {DATA_DIR}. Fallback naar {fallback}")
        global DATA_DIR, PENDING_PATH
        DATA_DIR = fallback
        PENDING_PATH = os.path.join(DATA_DIR, "pending_approvals.json")
        ensure_dir(DATA_DIR)

# ==========================================================
# DB LAYER
# ==========================================================
def db_available() -> bool:
    return bool(DATABASE_URL)

def db_connect():
    import psycopg2  # type: ignore
    return psycopg2.connect(DATABASE_URL)

def db_exec(sql: str, params: Optional[Tuple[Any, ...]] = None) -> None:
    conn = db_connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
    finally:
        conn.close()

def db_init_pending_approvals_table() -> None:
    db_exec(
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
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )

    db_exec("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'PENDING';")
    db_exec("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;")
    db_exec("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS bot_confidence INT;")
    db_exec("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'webhook';")
    try:
        db_exec("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS payload JSONB;")
    except Exception:
        db_exec("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS payload TEXT;")

    db_exec("CREATE INDEX IF NOT EXISTS idx_pending_status_created ON pending_approvals(status, created_at DESC);")

def db_insert_prebuy(prebuy: Dict[str, Any], source: str = "internal_prebuy") -> bool:
    if not db_available():
        return False

    db_init_pending_approvals_table()

    sql = """
    INSERT INTO pending_approvals
      (id, symbol, setup_type, regime, score, label, entry, stop, target, created_at, expires_at, status, bot_confidence, source, payload)
    VALUES
      (%s, %s, %s, %s, %s, %s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s, %s, %s, %s)
    ON CONFLICT (id) DO UPDATE SET
      symbol = EXCLUDED.symbol,
      setup_type = EXCLUDED.setup_type,
      regime = EXCLUDED.regime,
      score = EXCLUDED.score,
      label = EXCLUDED.label,
      entry = EXCLUDED.entry,
      stop = EXCLUDED.stop,
      target = EXCLUDED.target,
      expires_at = EXCLUDED.expires_at,
      bot_confidence = EXCLUDED.bot_confidence,
      source = EXCLUDED.source,
      payload = EXCLUDED.payload;
    """

    payload_val: Any = prebuy
    payload_json = json.dumps(prebuy, ensure_ascii=False)

    params = (
        prebuy.get("id"),
        prebuy.get("coin") or prebuy.get("symbol"),
        prebuy.get("setup_type"),
        prebuy.get("market_regime") or prebuy.get("regime"),
        int(prebuy.get("score") or 0),
        prebuy.get("grade") or prebuy.get("label"),
        float(prebuy.get("entry") or 0),
        float(prebuy.get("stop_loss") or prebuy.get("stop") or 0),
        float(prebuy.get("target") or 0),
        int(prebuy.get("created_at") or now_utc()),
        int(prebuy.get("expires_at") or (now_utc() + 4 * 60 * 60)),
        prebuy.get("status") or "PENDING",
        int(prebuy.get("bot_confidence") or int(prebuy.get("score") or 0)),
        source,
        payload_val if isinstance(payload_val, dict) else payload_json,
    )

    try:
        conn = db_connect()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.close()
        return True
    except Exception:
        traceback.print_exc()
        return False

def db_list_pending(limit: int = 10) -> List[Dict[str, Any]]:
    if not db_available():
        return []

    db_init_pending_approvals_table()

    sql = """
    SELECT id, symbol, setup_type, regime, score, label, entry, stop, target,
           EXTRACT(EPOCH FROM created_at)::bigint as created_ts,
           COALESCE(EXTRACT(EPOCH FROM expires_at)::bigint, 0) as expires_ts,
           status, COALESCE(bot_confidence, score) as bot_confidence
    FROM pending_approvals
    WHERE status = 'PENDING'
    ORDER BY created_at DESC
    LIMIT %s;
    """
    items: List[Dict[str, Any]] = []
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (limit,))
            rows = cur.fetchall()
            for r in rows:
                items.append({
                    "id": r[0],
                    "coin": r[1],
                    "setup_type": r[2],
                    "market_regime": r[3],
                    "score": int(r[4] or 0),
                    "grade": r[5],
                    "entry": float(r[6] or 0),
                    "stop_loss": float(r[7] or 0),
                    "target": float(r[8] or 0),
                    "created_at": int(r[9] or 0),
                    "expires_at": int(r[10] or 0),
                    "status": r[11],
                    "bot_confidence": int(r[12] or 0),
                })
    finally:
        conn.close()
    return items

def db_get_pending_by_id(prebuy_id: str) -> Optional[Dict[str, Any]]:
    if not db_available():
        return None

    db_init_pending_approvals_table()

    sql = """
    SELECT id, symbol, setup_type, regime, score, label, entry, stop, target,
           EXTRACT(EPOCH FROM created_at)::bigint as created_ts,
           COALESCE(EXTRACT(EPOCH FROM expires_at)::bigint, 0) as expires_ts,
           status, COALESCE(bot_confidence, score) as bot_confidence
    FROM pending_approvals
    WHERE id = %s
    LIMIT 1;
    """
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (prebuy_id,))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "coin": row[1],
                "setup_type": row[2],
                "market_regime": row[3],
                "score": int(row[4] or 0),
                "grade": row[5],
                "entry": float(row[6] or 0),
                "stop_loss": float(row[7] or 0),
                "target": float(row[8] or 0),
                "created_at": int(row[9] or 0),
                "expires_at": int(row[10] or 0),
                "status": row[11],
                "bot_confidence": int(row[12] or 0),
            }
    finally:
        conn.close()

def db_set_status(prebuy_id: str, status: str) -> None:
    if not db_available():
        return
    db_init_pending_approvals_table()
    db_exec("UPDATE pending_approvals SET status=%s WHERE id=%s;", (status, prebuy_id))

# ==========================================================
# JSON FALLBACK (als DB niet beschikbaar)
# ==========================================================
def read_pending_json() -> List[Dict[str, Any]]:
    safe_storage_ready()
    data = load_json(PENDING_PATH, [])
    if isinstance(data, list):
        return data
    return []

def write_pending_json(items: List[Dict[str, Any]]) -> None:
    safe_storage_ready()
    save_json(PENDING_PATH, items)

def json_insert_prebuy(prebuy: Dict[str, Any]) -> None:
    items = read_pending_json()
    # dedup op id
    if any(x.get("id") == prebuy.get("id") for x in items):
        return
    items.insert(0, prebuy)
    write_pending_json(items)

def json_list_pending(limit: int = 10) -> List[Dict[str, Any]]:
    items = [x for x in read_pending_json() if (x.get("status") or "PENDING") == "PENDING"]
    return items[:limit]

def json_get_pending_by_id(prebuy_id: str) -> Optional[Dict[str, Any]]:
    for x in read_pending_json():
        if x.get("id") == prebuy_id:
            return x
    return None

def json_set_status(prebuy_id: str, status: str) -> None:
    items = read_pending_json()
    for x in items:
        if x.get("id") == prebuy_id:
            x["status"] = status
    write_pending_json(items)

# ==========================================================
# Unified read/write (DB first, else JSON)
# ==========================================================
def list_pending(limit: int = 10) -> List[Dict[str, Any]]:
    if db_available():
        return db_list_pending(limit=limit)
    return json_list_pending(limit=limit)

def get_pending(prebuy_id: str) -> Optional[Dict[str, Any]]:
    if db_available():
        return db_get_pending_by_id(prebuy_id)
    return json_get_pending_by_id(prebuy_id)

def set_status(prebuy_id: str, status: str) -> None:
    if db_available():
        db_set_status(prebuy_id, status)
    else:
        json_set_status(prebuy_id, status)

# ==========================================================
# Twilio reply helper (TwiML)
# ==========================================================
def twiml(message: str) -> Response:
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{message}</Message></Response>'
    return Response(xml, mimetype="application/xml")

# ==========================================================
# INTERNAL ENDPOINT (multi_coin_score -> webhook)
# ==========================================================
@app.route("/internal/prebuy", methods=["POST"])
def internal_prebuy() -> Response:
    token = (request.headers.get("X-Internal-Token") or "").strip()
    if not INTERNAL_TOKEN or token != INTERNAL_TOKEN:
        return Response("Unauthorized", status=401)

    try:
        prebuy = request.get_json(force=True, silent=False) or {}
        if not isinstance(prebuy, dict):
            return Response("Bad payload", status=400)

        # status default
        prebuy.setdefault("status", "PENDING")

        stored_db = False
        if db_available():
            stored_db = db_insert_prebuy(prebuy, source="internal_prebuy")

        if not stored_db:
            json_insert_prebuy(prebuy)

        return Response("OK", status=200)
    except Exception:
        traceback.print_exc()
        return Response("ERROR", status=500)

# ==========================================================
# WHATSAPP COMMANDS
# LIST
# YES <amount> [id]
# NO [id]
# ==========================================================
def parse_command(text: str) -> Tuple[str, List[str]]:
    t = (text or "").strip()
    parts = t.split()
    if not parts:
        return "", []
    cmd = parts[0].upper()
    return cmd, parts[1:]

def format_prebuy_line(p: Dict[str, Any]) -> str:
    return (
        f"{p.get('id')} | {p.get('coin')} | {p.get('grade')} | "
        f"score={p.get('score')} conf={p.get('bot_confidence', p.get('score'))} | "
        f"entry={p.get('entry')} stop={p.get('stop_loss')} target={p.get('target')}"
    )

def is_expired(p: Dict[str, Any]) -> bool:
    exp = int(p.get("expires_at") or 0)
    return exp > 0 and now_utc() > exp

def execute_buy(prebuy: Dict[str, Any], amount_eur: int) -> Tuple[bool, str]:
    """
    Start BUY via paper_trader (of later live).
    """
    if is_expired(prebuy):
        return False, "Deze Pre-BUY is verlopen."

    if buy_eur is None:
        return False, "buy_eur() niet beschikbaar (paper_trader import faalt)."

    symbol = str(prebuy.get("coin") or "")
    entry = float(prebuy.get("entry") or 0)
    stop = float(prebuy.get("stop_loss") or 0)
    target = float(prebuy.get("target") or 0)

    try:
        # jouw paper_trader kan andere signature hebben; pas aan indien nodig.
        buy_eur(symbol=symbol, amount_eur=amount_eur, entry=entry, stop_loss=stop, target=target, prebuy_id=str(prebuy.get("id")))
        return True, f"BUY gestart ✅ {symbol} €{amount_eur} (id={prebuy.get('id')})"
    except TypeError:
        # fallback signature
        try:
            buy_eur(symbol, amount_eur)
            return True, f"BUY gestart ✅ {symbol} €{amount_eur} (id={prebuy.get('id')})"
        except Exception as e:
            return False, f"BUY faalde: {e}"
    except Exception as e:
        return False, f"BUY faalde: {e}"

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook() -> Response:
    body = (request.form.get("Body") or "").strip()
    from_number = (request.form.get("From") or "").strip()

    # debug log
    log(f"WHATSAPP_IN: from={from_number} body={body} db={'YES' if db_available() else 'NO'} pending_file={PENDING_PATH}")

    cmd, args = parse_command(body)

    try:
        if cmd in ("HELP", "?"):
            return twiml(
                "Commands:\n"
                "LIST\n"
                "YES <bedrag> [ID]\n"
                "NO [ID]\n"
                "Voorbeeld: YES 10 PB-BTCUSDT-1770831004"
            )

        if cmd == "LIST":
            items = list_pending(limit=10)
            if not items:
                return twiml("Geen PENDING Pre-BUY’s gevonden.")
            lines = ["PENDING Pre-BUY’s (max 10):"]
            for p in items:
                lines.append(format_prebuy_line(p))
            return twiml("\n".join(lines))

        if cmd == "YES":
            if not args:
                return twiml("Gebruik: YES <bedrag> [ID] (bijv. YES 10 PB-...)")

            # bedrag
            try:
                amount = int(args[0])
            except Exception:
                return twiml("Bedrag ongeldig. Toegestaan: 5,10,15,20,30,100")

            if amount not in ALLOWED_AMOUNTS:
                return twiml("Bedrag niet toegestaan. Toegestaan: 5,10,15,20,30,100")

            # id optioneel
            prebuy_id = args[1] if len(args) >= 2 else ""

            if prebuy_id:
                prebuy = get_pending(prebuy_id)
                if not prebuy:
                    return twiml(f"ID niet gevonden: {prebuy_id}")
            else:
                items = list_pending(limit=1)
                if not items:
                    return twiml("Geen PENDING Pre-BUY’s.")
                prebuy = items[0]

            # execute buy
            ok, msg = execute_buy(prebuy, amount)

            if ok:
                set_status(str(prebuy.get("id")), "APPROVED")
                return twiml(msg)

            return twiml(msg)

        if cmd == "NO":
            prebuy_id = args[0] if args else ""
            if prebuy_id:
                prebuy = get_pending(prebuy_id)
                if not prebuy:
                    return twiml(f"ID niet gevonden: {prebuy_id}")
            else:
                items = list_pending(limit=1)
                if not items:
                    return twiml("Geen PENDING Pre-BUY’s.")
                prebuy = items[0]

            set_status(str(prebuy.get("id")), "REJECTED")
            return twiml(f"Afgewezen ❌ id={prebuy.get('id')}")

        return twiml("Onbekend commando. Stuur HELP voor opties.")

    except Exception:
        traceback.print_exc()
        return twiml("ERROR in webhook (check Render logs).")

# ==========================================================
# HEALTH
# ==========================================================
@app.route("/", methods=["GET"])
def health() -> Response:
    return Response("OK", status=200)

# ==========================================================
# RUN
# ==========================================================
if __name__ == "__main__":
    safe_storage_ready()
    # init db schema als DB er is (handig bij deploy)
    if db_available():
        try:
            db_init_pending_approvals_table()
            log("DB schema ready ✅ (pending_approvals)")
        except Exception:
            traceback.print_exc()
            log("DB schema init faalde ⚠️")
    app.run(host="0.0.0.0", port=PORT)
