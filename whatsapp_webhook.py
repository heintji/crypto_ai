from __future__ import annotations

import os
import json
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, request, Response, jsonify
from twilio.twiml.messaging_response import MessagingResponse

# ==========================================================
# PROJECT ROOT import (zodat trading modules werken op Render)
# ==========================================================
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ==========================================================
# CONFIG
# ==========================================================
app = Flask(__name__)

ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

DATA_DIR = (os.getenv("DATA_DIR") or "/data").rstrip("/")
PENDING_PATH = os.getenv("PENDING_PATH") or os.path.join(DATA_DIR, "pending_approvals.json")

INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()
DATABASE_URL = (os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_RENDER") or os.getenv("DATABASE_URL_INTERNAL") or os.getenv("DATABASE_URL_EXTERNAL") or os.getenv("DATABASE_URL_PUBLIC") or os.getenv("DATABASE_URL_PRIVATE") or os.getenv("DATABASE_URL_POSTGRES") or os.getenv("DATABASE_URL_PG") or os.getenv("DATABASE_URL_DB") or os.getenv("DATABASE_URL_CONNECTION") or os.getenv("DATABASE_URL") or "").strip()

# Let op: Render gebruikt meestal gewoon DATABASE_URL
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

# ==========================================================
# UTILS
# ==========================================================
def now_utc() -> int:
    return int(time.time())

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

def twiml(text: str) -> str:
    resp = MessagingResponse()
    resp.message(text)
    return str(resp)

def parse_msg(body: str) -> Tuple[str, List[str]]:
    """
    Returns: (command, parts)
    Example:
      "YES 10 PB-BTCUSDT-123" -> ("YES", ["10","PB-BTCUSDT-123"])
      "LIST" -> ("LIST", [])
    """
    raw = (body or "").strip()
    if not raw:
        return "", []
    parts = raw.split()
    cmd = parts[0].upper()
    rest = parts[1:]
    return cmd, rest

def safe_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None

# ==========================================================
# DB LAYER (psycopg2)
# ==========================================================
def db_available() -> bool:
    return bool(DATABASE_URL)

def db_conn():
    import psycopg2  # noqa
    return psycopg2.connect(DATABASE_URL)

def db_bootstrap() -> None:
    """
    Maakt pending_approvals tabel aan OF upgrade hem als hij al bestaat
    (voegt ontbrekende kolommen toe zoals status, expires_at, payload, etc.)
    """
    if not db_available():
        return

    sql_create = """
    CREATE TABLE IF NOT EXISTS pending_approvals (
      id TEXT PRIMARY KEY,
      coin TEXT,
      symbol TEXT,
      setup_type TEXT,
      market_regime TEXT,
      score INT,
      grade TEXT,
      entry NUMERIC,
      stop_loss NUMERIC,
      target NUMERIC,
      bot_confidence INT,
      status TEXT,
      created_at TIMESTAMPTZ DEFAULT NOW(),
      expires_at TIMESTAMPTZ,
      payload JSONB
    );
    """

    # Upgrade/patch voor mensen die eerst “klein” begonnen:
    sql_alter = [
        "ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS coin TEXT;",
        "ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS symbol TEXT;",
        "ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS setup_type TEXT;",
        "ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS market_regime TEXT;",
        "ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS score INT;",
        "ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS grade TEXT;",
        "ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS entry NUMERIC;",
        "ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS stop_loss NUMERIC;",
        "ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS target NUMERIC;",
        "ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS bot_confidence INT;",
        "ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS status TEXT;",
        "ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();",
        "ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;",
        "ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS payload JSONB;",
    ]

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_create)
            for q in sql_alter:
                cur.execute(q)
        conn.commit()

def db_insert_prebuy(prebuy: Dict[str, Any]) -> None:
    db_bootstrap()

    pid = str(prebuy.get("id") or "")
    coin = str(prebuy.get("coin") or prebuy.get("symbol") or "")
    symbol = coin  # we bewaren beide; het is vaak hetzelfde
    setup_type = str(prebuy.get("setup_type") or "")
    market_regime = str(prebuy.get("market_regime") or "")
    score = int(prebuy.get("score") or 0)
    grade = str(prebuy.get("grade") or prebuy.get("label") or "WATCH")
    entry = safe_float(prebuy.get("entry"))
    stop_loss = safe_float(prebuy.get("stop_loss") or prebuy.get("stop"))
    target = safe_float(prebuy.get("target"))
    bot_confidence = int(prebuy.get("bot_confidence") or prebuy.get("bot_confidence") or score)
    status = str(prebuy.get("status") or "PENDING")

    created_at_unix = int(prebuy.get("created_at") or now_utc())
    expires_at_unix = int(prebuy.get("expires_at") or (created_at_unix + int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))))

    # we slaan de hele payload op als JSONB (handig voor later)
    payload = prebuy

    sql = """
    INSERT INTO pending_approvals
      (id, coin, symbol, setup_type, market_regime, score, grade, entry, stop_loss, target, bot_confidence, status, created_at, expires_at, payload)
    VALUES
      (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s::jsonb)
    ON CONFLICT (id) DO UPDATE SET
      coin = EXCLUDED.coin,
      symbol = EXCLUDED.symbol,
      setup_type = EXCLUDED.setup_type,
      market_regime = EXCLUDED.market_regime,
      score = EXCLUDED.score,
      grade = EXCLUDED.grade,
      entry = EXCLUDED.entry,
      stop_loss = EXCLUDED.stop_loss,
      target = EXCLUDED.target,
      bot_confidence = EXCLUDED.bot_confidence,
      status = EXCLUDED.status,
      created_at = EXCLUDED.created_at,
      expires_at = EXCLUDED.expires_at,
      payload = EXCLUDED.payload;
    """

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    pid,
                    coin,
                    symbol,
                    setup_type,
                    market_regime,
                    score,
                    grade,
                    entry,
                    stop_loss,
                    target,
                    bot_confidence,
                    status,
                    created_at_unix,
                    expires_at_unix,
                    json.dumps(payload),
                ),
            )
        conn.commit()

def db_list_pending(limit: int = 10) -> List[Dict[str, Any]]:
    db_bootstrap()
    sql = """
      SELECT id, coin, setup_type, score, grade, entry, stop_loss, target, bot_confidence, status, extract(epoch from expires_at)::bigint
      FROM pending_approvals
      WHERE COALESCE(status,'PENDING') = 'PENDING'
      ORDER BY created_at ASC
      LIMIT %s;
    """
    out: List[Dict[str, Any]] = []
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (limit,))
            rows = cur.fetchall()
    for r in rows:
        out.append(
            {
                "id": r[0],
                "coin": r[1],
                "setup_type": r[2],
                "score": r[3],
                "grade": r[4],
                "entry": float(r[5]) if r[5] is not None else None,
                "stop_loss": float(r[6]) if r[6] is not None else None,
                "target": float(r[7]) if r[7] is not None else None,
                "bot_confidence": r[8],
                "status": r[9] or "PENDING",
                "expires_at": int(r[10] or 0),
            }
        )
    return out

def db_get_pending_by_id(pid: str) -> Optional[Dict[str, Any]]:
    db_bootstrap()
    sql = """
      SELECT payload
      FROM pending_approvals
      WHERE id = %s
      LIMIT 1;
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (pid,))
            row = cur.fetchone()
    if not row:
        return None
    return row[0]

def db_get_oldest_pending() -> Optional[Dict[str, Any]]:
    items = db_list_pending(limit=1)
    if not items:
        return None
    pid = items[0]["id"]
    return db_get_pending_by_id(pid)

def db_set_status(pid: str, status: str) -> None:
    db_bootstrap()
    sql = "UPDATE pending_approvals SET status=%s WHERE id=%s;"
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (status, pid))
        conn.commit()

# ==========================================================
# JSON FALLBACK (als je geen DB gebruikt)
# ==========================================================
def read_pending_json() -> List[Dict[str, Any]]:
    ensure_dir(DATA_DIR)
    data = load_json(PENDING_PATH, [])
    if isinstance(data, dict):
        # soms ooit als dict opgeslagen; normalize naar list
        data = list(data.values())
    if not isinstance(data, list):
        return []
    return data

def write_pending_json(items: List[Dict[str, Any]]) -> None:
    ensure_dir(DATA_DIR)
    save_json(PENDING_PATH, items)

def json_list_pending(limit: int = 10) -> List[Dict[str, Any]]:
    items = read_pending_json()
    now = now_utc()
    # alleen pending + niet expired
    out = []
    for it in items:
        if str(it.get("status") or "PENDING") != "PENDING":
            continue
        if int(it.get("expires_at") or 0) and int(it["expires_at"]) < now:
            continue
        out.append(it)
    out.sort(key=lambda x: int(x.get("created_at") or 0))
    return out[:limit]

def json_get_oldest_pending() -> Optional[Dict[str, Any]]:
    items = json_list_pending(limit=1)
    return items[0] if items else None

def json_get_by_id(pid: str) -> Optional[Dict[str, Any]]:
    items = read_pending_json()
    for it in items:
        if str(it.get("id") or "") == pid:
            return it
    return None

def json_set_status(pid: str, status: str) -> None:
    items = read_pending_json()
    for it in items:
        if str(it.get("id") or "") == pid:
            it["status"] = status
    write_pending_json(items)

def json_insert_prebuy(prebuy: Dict[str, Any]) -> None:
    items = read_pending_json()
    pid = str(prebuy.get("id") or "")
    # upsert
    replaced = False
    for i, it in enumerate(items):
        if str(it.get("id") or "") == pid:
            items[i] = prebuy
            replaced = True
            break
    if not replaced:
        items.append(prebuy)
    write_pending_json(items)

# ==========================================================
# EXECUTION (paper_trader)
# ==========================================================
def execute_buy(prebuy: Dict[str, Any], amount_eur: int) -> Dict[str, Any]:
    """
    Houdt het robuust: probeert buy_eur(), anders buy().
    Jij hebt paper_trader als execution + administratie.
    """
    from trading import paper_trader  # jouw module

    symbol = str(prebuy.get("coin") or prebuy.get("symbol") or "")
    entry = safe_float(prebuy.get("entry"))

    # voorkeur: buy_eur(symbol, amount_eur, price_hint=entry)
    if hasattr(paper_trader, "buy_eur"):
        try:
            return paper_trader.buy_eur(symbol=symbol, amount_eur=amount_eur, price_hint=entry)  # type: ignore
        except TypeError:
            # andere signature
            return paper_trader.buy_eur(symbol, amount_eur)  # type: ignore

    # fallback: buy(symbol, amount_eur)
    if hasattr(paper_trader, "buy"):
        try:
            return paper_trader.buy(symbol=symbol, amount_eur=amount_eur)  # type: ignore
        except TypeError:
            return paper_trader.buy(symbol, amount_eur)  # type: ignore

    raise RuntimeError("paper_trader heeft geen buy_eur() of buy() functie gevonden.")

# ==========================================================
# ROUTES
# ==========================================================
@app.get("/")
def health() -> Response:
    return Response("OK", mimetype="text/plain")

@app.post("/internal/prebuy")
def internal_prebuy() -> Response:
    """
    multi_coin_score kan Pre-BUY hier posten.
    Vereist header: X-Internal-Token
    """
    token = (request.headers.get("X-Internal-Token") or "").strip()
    if not INTERNAL_TOKEN or token != INTERNAL_TOKEN:
        return Response("Unauthorized", status=401)

    prebuy = request.get_json(silent=True) or {}
    if not isinstance(prebuy, dict) or not prebuy.get("id"):
        return Response("Bad payload", status=400)

    try:
        if db_available():
            db_insert_prebuy(prebuy)
            where = "db"
        else:
            json_insert_prebuy(prebuy)
            where = "json"

        return jsonify({"ok": True, "stored": where, "id": prebuy["id"]})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/whatsapp")
def whatsapp_webhook() -> Response:
    body = request.form.get("Body", "") or ""
    from_number = request.form.get("From", "") or ""

    cmd, parts = parse_msg(body)

    # Kies bron: DB als beschikbaar, anders JSON
    use_db = db_available()

    try:
        if cmd in ("HELP", "H", "?"):
            return Response(
                twiml(
                    "Commands:\n"
                    "LIST\n"
                    "YES <bedrag> [ID]\n"
                    "NO [ID]\n\n"
                    "Voorbeeld: YES 10 PB-BTCUSDT-1700000000"
                ),
                mimetype="application/xml",
            )

        if cmd == "LIST":
            if use_db:
                items = db_list_pending(limit=10)
            else:
                items = json_list_pending(limit=10)

            if not items:
                return Response(twiml("Geen PENDING Pre-BUY’s gevonden."), mimetype="application/xml")

            lines = ["PENDING Pre-BUY’s:"]
            now = now_utc()
            for it in items:
                pid = it.get("id")
                coin = it.get("coin") or it.get("symbol")
                grade = it.get("grade") or it.get("label")
                score = it.get("score")
                entry = it.get("entry")
                target = it.get("target")
                exp = int(it.get("expires_at") or 0)
                mins = int(max(0, (exp - now) / 60)) if exp else 0
                lines.append(f"- {pid} | {coin} | {grade} | score={score} | entry={entry} | target={target} | exp {mins}m")
            return Response(twiml("\n".join(lines)), mimetype="application/xml")

        if cmd == "YES":
            if not parts:
                return Response(twiml("Gebruik: YES <bedrag> [ID]"), mimetype="application/xml")

            # bedrag
            try:
                amount = int(parts[0])
            except Exception:
                return Response(twiml("Bedrag ongeldig. Gebruik bv: YES 10"), mimetype="application/xml")

            if amount not in ALLOWED_AMOUNTS:
                return Response(twiml(f"Bedrag niet toegestaan. Kies uit: {sorted(ALLOWED_AMOUNTS)}"), mimetype="application/xml")

            pid = parts[1] if len(parts) >= 2 else None

            # haal prebuy
            if use_db:
                prebuy = db_get_pending_by_id(pid) if pid else db_get_oldest_pending()
            else:
                prebuy = json_get_by_id(pid) if pid else json_get_oldest_pending()

            if not prebuy:
                return Response(twiml("Geen PENDING Pre-BUY gevonden (met die ID). Doe eerst LIST."), mimetype="application/xml")

            # check expiry
            exp = int(prebuy.get("expires_at") or 0)
            if exp and exp < now_utc():
                # mark expired
                if use_db:
                    db_set_status(str(prebuy["id"]), "EXPIRED")
                else:
                    json_set_status(str(prebuy["id"]), "EXPIRED")
                return Response(twiml("Deze Pre-BUY is verlopen (EXPIRED). Doe LIST voor nieuwe."), mimetype="application/xml")

            # execute BUY
            result = execute_buy(prebuy, amount)

            # mark consumed
            if use_db:
                db_set_status(str(prebuy["id"]), "CONSUMED")
            else:
                json_set_status(str(prebuy["id"]), "CONSUMED")

            coin = prebuy.get("coin") or prebuy.get("symbol")
            return Response(
                twiml(
                    f"✅ BUY bevestigd: {coin}\n"
                    f"Bedrag: €{amount}\n"
                    f"ID: {prebuy.get('id')}\n"
                    f"Status: CONSUMED\n"
                    f"Result: {result}"
                ),
                mimetype="application/xml",
            )

        if cmd == "NO":
            pid = parts[0] if parts else None

            if use_db:
                prebuy = db_get_pending_by_id(pid) if pid else db_get_oldest_pending()
            else:
                prebuy = json_get_by_id(pid) if pid else json_get_oldest_pending()

            if not prebuy:
                return Response(twiml("Geen PENDING Pre-BUY gevonden. Doe LIST."), mimetype="application/xml")

            if use_db:
                db_set_status(str(prebuy["id"]), "DECLINED")
            else:
                json_set_status(str(prebuy["id"]), "DECLINED")

            coin = prebuy.get("coin") or prebuy.get("symbol")
            return Response(twiml(f"❌ Afgewezen: {coin} | {prebuy.get('id')} (DECLINED)"), mimetype="application/xml")

        # default
        return Response(twiml("Onbekend commando. Stuur HELP."), mimetype="application/xml")

    except Exception as e:
        traceback.print_exc()
        return Response(twiml(f"ERROR: {e}"), mimetype="application/xml")


# ==========================================================
# START
# ==========================================================
if __name__ == "__main__":
    # DB bootstrap is veilig; als geen DB, doet hij niks
    try:
        if db_available():
            db_bootstrap()
            print("DB: pending_approvals is ready ✅", flush=True)
        else:
            print("DB: niet actief (geen DATABASE_URL). JSON fallback ✅", flush=True)
    except Exception:
        traceback.print_exc()

    port = int(os.getenv("PORT") or "10000")
    app.run(host="0.0.0.0", port=port)
