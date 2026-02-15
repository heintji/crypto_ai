# trading/paper_trader.py
# =========================================
# LIVE TRADER (Bitvavo) – single source of truth
# + DB approvals (Postgres) als USE_DB_APPROVALS=1
# =========================================

from __future__ import annotations

import os
import time
import json
import hmac
import hashlib
from datetime import datetime
from typing import Dict, Any

import requests
import psycopg2
import psycopg2.extras

# =========================================
# ENV
# =========================================
BITVAVO_API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
USE_DB_APPROVALS = (os.getenv("USE_DB_APPROVALS", "1").strip().lower() in {"1", "true", "yes", "on"})

DATA_DIR = (os.getenv("DATA_DIR") or "/data").strip()
STATE_PATH = (os.getenv("PAPER_STATE_PATH") or f"{DATA_DIR}/paper_state.json").strip()
LOG_PATH = (os.getenv("PAPER_TRADES_CSV") or f"{DATA_DIR}/paper_trades.csv").strip()

HTTP_TIMEOUT = 15
BASE_URL = "https://api.bitvavo.com/v2"

if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
    raise RuntimeError("❌ BITVAVO_API_KEY / BITVAVO_API_SECRET ontbreken")

# =========================================
# HELPERS
# =========================================
def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt (Render env).")
    return psycopg2.connect(DATABASE_URL)

def _sign(timestamp: str, method: str, path: str, body: str = "") -> str:
    msg = timestamp + method + path + body
    return hmac.new(
        BITVAVO_API_SECRET.encode(),
        msg.encode(),
        hashlib.sha256
    ).hexdigest()

def _headers(method: str, path: str, body: str = "") -> Dict[str, str]:
    ts = str(int(time.time() * 1000))
    return {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": _sign(ts, method, path, body),
        "Bitvavo-Access-Timestamp": ts,
        "Content-Type": "application/json",
    }

def _to_market(symbol: str) -> str:
    s = (symbol or "").upper().strip()
    if s.endswith("USDT"):
        base = s.replace("USDT", "")
        return f"{base}-EUR"
    if "-" in s:
        return s
    return f"{s}-EUR"

# =========================================
# STATE
# =========================================
def _load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {"positions": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {"positions": {}}
    except Exception:
        return {"positions": {}}

def _save_state(state: Dict[str, Any]):
    _ensure_dir(STATE_PATH)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)

# =========================================
# LOG
# =========================================
def _log(symbol: str, side: str, price: float, qty: float, pnl: float = 0.0, meta: str = ""):
    _ensure_dir(LOG_PATH)
    new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        if new:
            f.write("datetime,symbol,side,price,qty,pnl,meta\n")
        f.write(
            f"{datetime.utcnow().isoformat()},"
            f"{symbol},{side},{price},{qty},{pnl},{meta}\n"
        )

# =========================================
# DB APPROVALS
# =========================================
def _ensure_db_schema() -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_approvals (
                    id TEXT PRIMARY KEY,
                    symbol TEXT,
                    coin TEXT,
                    status TEXT,
                    label TEXT,
                    score INTEGER,
                    grade TEXT,
                    setup_type TEXT,
                    regime TEXT,
                    market_regime TEXT,
                    entry DOUBLE PRECISION,
                    stop DOUBLE PRECISION,
                    stop_loss DOUBLE PRECISION,
                    target DOUBLE PRECISION,
                    bot_confidence INTEGER,
                    payload TEXT,
                    payload_json JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    expires_at TIMESTAMPTZ
                );
                """
            )
            cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS approved_amount DOUBLE PRECISION;")
            cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;")
            cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS approved_by TEXT;")
            conn.commit()

def _consume_approval_db(prebuy_id: str) -> bool:
    _ensure_db_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            # Alleen uitvoeren als hij APPROVED is en nog niet verlopen
            cur.execute(
                """
                UPDATE pending_approvals
                SET status='CONSUMED'
                WHERE id=%s
                  AND status='APPROVED'
                  AND (expires_at IS NULL OR expires_at > NOW());
                """,
                (prebuy_id,),
            )
            ok = (cur.rowcount == 1)
            conn.commit()
            return ok

# =========================================
# MARKET DATA (Bitvavo)
# =========================================
def get_price(symbol: str) -> float:
    market = _to_market(symbol)
    r = requests.get(
        f"{BASE_URL}/ticker/price",
        params={"market": market},
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    return float(r.json()["price"])

# =========================================
# LIVE BUY
# =========================================
def buy_eur(
    symbol: str,
    amount_eur: float,
    stop_loss: float,
    target: float,
    prebuy_id: str
) -> Dict[str, Any]:
    """
    LIVE market buy op Bitvavo met EUR quote amount.
    Let op: symbol blijft intern bijv. "SOLUSDT" (state-key),
    market wordt "SOL-EUR" voor de API-call.
    """

    # 1) approvals gate (DB)
    if USE_DB_APPROVALS:
        if not _consume_approval_db(prebuy_id):
            return {"ok": False, "reason": "APPROVAL_NOT_CONSUMED_DB"}
    # (als je ooit file-mode terug wilt: hier kun je een file consume toevoegen)

    market = _to_market(symbol)

    body = json.dumps({
        "market": market,
        "side": "buy",
        "orderType": "market",
        "amountQuote": f"{float(amount_eur):.2f}"
    })

    path = "/order"
    r = requests.post(
        BASE_URL + path,
        headers=_headers("POST", path, body),
        data=body,
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    res = r.json()

    filled_qty = float(res.get("filledAmount", 0) or 0.0)
    price = float(res.get("price", 0) or 0.0)

    state = _load_state()
    state.setdefault("positions", {})
    state["positions"][symbol] = {
        "qty": filled_qty,
        "entry": price,
        "stop_loss": float(stop_loss),
        "target": float(target),
        "opened_at": int(time.time()),
        "prebuy_id": prebuy_id
    }
    _save_state(state)

    _log(symbol, "BUY", price, filled_qty, meta=f"prebuy={prebuy_id}; market={market}")

    return {"ok": True, "symbol": symbol, "market": market, "qty": filled_qty, "price": price}

# =========================================
# LIVE SELL
# =========================================
def sell(symbol: str, fraction: float = 1.0) -> Dict[str, Any]:
    """
    symbol = dezelfde key als state (bijv. "SOLUSDT"), NIET "SOL-EUR".
    """
    state = _load_state()
    pos = (state.get("positions") or {}).get(symbol)
    if not pos:
        return {"ok": False, "reason": "NO_POSITION", "symbol": symbol}

    qty_total = float(pos.get("qty") or 0.0)
    qty = qty_total * float(fraction)
    if qty <= 0:
        return {"ok": False, "reason": "QTY_ZERO", "symbol": symbol}

    market = _to_market(symbol)

    body = json.dumps({
        "market": market,
        "side": "sell",
        "orderType": "market",
        "amount": f"{qty:.8f}"
    })

    path = "/order"
    r = requests.post(
        BASE_URL + path,
        headers=_headers("POST", path, body),
        data=body,
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    res = r.json()

    exit_price = float(res.get("price", 0) or 0.0)
    pnl = (exit_price - float(pos["entry"])) * qty

    if float(fraction) >= 1.0:
        del state["positions"][symbol]
    else:
        pos["qty"] = max(0.0, float(pos["qty"]) - qty)

    _save_state(state)
    _log(symbol, "SELL", exit_price, qty, pnl=pnl, meta=f"market={market}")

    return {"ok": True, "symbol": symbol, "market": market, "price": exit_price, "qty": qty, "pnl": pnl}
