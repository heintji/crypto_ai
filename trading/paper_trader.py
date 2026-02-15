# trading/paper_trader.py
# =========================================
# LIVE TRADER (Bitvavo) – single source of truth
# + Postgres approvals (pending_approvals)
# =========================================

from __future__ import annotations

import os
import time
import json
import hmac
import hashlib
import requests
from datetime import datetime
from typing import Dict, Any

import psycopg2
import psycopg2.extras

# =========================================
# ENV
# =========================================
BITVAVO_API_KEY = os.getenv("BITVAVO_API_KEY")
BITVAVO_API_SECRET = os.getenv("BITVAVO_API_SECRET")
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
USE_DB_APPROVALS = (os.getenv("USE_DB_APPROVALS") or "1").strip().lower() in {"1", "true", "yes", "on"}

if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
    raise RuntimeError("❌ BITVAVO_API_KEY / SECRET ontbreken")

BASE_URL = "https://api.bitvavo.com/v2"
HTTP_TIMEOUT = 15

# =========================================
# DATA DIR (disk-safe fallback)
# =========================================
def _get_data_dir() -> str:
    d = (os.getenv("DATA_DIR") or "").strip()
    if d:
        return d
    return "/data" if os.path.isdir("/data") else "/tmp/data"

DATA_DIR = _get_data_dir()
STATE_PATH = os.path.join(DATA_DIR, "paper_state.json")
LOG_PATH = os.path.join(DATA_DIR, "paper_trades.csv")


# =========================================
# DB
# =========================================
def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt (maar USE_DB_APPROVALS=1).")
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
        status          TEXT NOT NULL DEFAULT 'PENDING',
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


def _consume_approval_db(prebuy_id: str) -> bool:
    """
    Alleen CONSUME als:
    - status=APPROVED
    - expires_at > NOW()
    Dan zetten we status=CONSUMED.
    """
    ensure_tables()
    q = """
    UPDATE pending_approvals
    SET status='CONSUMED',
        consumed_at=NOW()
    WHERE id=%s
      AND status='APPROVED'
      AND expires_at > NOW()
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (prebuy_id,))
            ok = cur.rowcount > 0
        conn.commit()
    return ok


# =========================================
# HELPERS
# =========================================
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
        "Content-Type": "application/json"
    }


def ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


# =========================================
# MARKET DATA
# =========================================
def get_price(symbol: str) -> float:
    r = requests.get(
        f"{BASE_URL}/ticker/price",
        params={"market": symbol.replace("USDT", "-EUR")},
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    return float(r.json()["price"])


# =========================================
# STATE
# =========================================
def _load_state() -> Dict[str, Any]:
    ensure_dir(STATE_PATH)
    if not os.path.exists(STATE_PATH):
        return {"positions": {}}
    with open(STATE_PATH, "r") as f:
        return json.load(f)


def _save_state(state: Dict[str, Any]):
    ensure_dir(STATE_PATH)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


# =========================================
# LOG
# =========================================
def _log(symbol, side, price, qty, pnl=0.0, meta=""):
    ensure_dir(LOG_PATH)
    new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a") as f:
        if new:
            f.write("datetime,symbol,side,price,qty,pnl,meta\n")
        f.write(
            f"{datetime.utcnow().isoformat()},"
            f"{symbol},{side},{price},{qty},{pnl},{meta}\n"
        )


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

    # 1) Consume approval vanuit DB
    if USE_DB_APPROVALS:
        if not _consume_approval_db(prebuy_id):
            return {"ok": False, "reason": "APPROVAL_INVALID_OR_EXPIRED"}
    else:
        return {"ok": False, "reason": "USE_DB_APPROVALS_DISABLED"}

    # 2) Plaats market buy op Bitvavo
    body = json.dumps({
        "market": symbol.replace("USDT", "-EUR"),
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

    filled_qty = float(res.get("filledAmount", 0) or 0)
    price = float(res.get("price", 0) or 0)

    # 3) State opslaan (monitor gebruikt dit)
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

    _log(symbol, "BUY", price, filled_qty, meta=f"prebuy={prebuy_id}")

    return {
        "ok": True,
        "symbol": symbol,
        "qty": filled_qty,
        "price": price
    }


# =========================================
# LIVE SELL
# =========================================
def sell(symbol: str, fraction: float = 1.0) -> Dict[str, Any]:
    state = _load_state()
    pos = state.get("positions", {}).get(symbol)

    if not pos:
        return {"ok": False, "reason": "NO_POSITION"}

    qty = pos["qty"] * fraction

    body = json.dumps({
        "market": symbol.replace("USDT", "-EUR"),
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

    exit_price = float(res.get("price", 0) or 0)
    pnl = (exit_price - pos["entry"]) * qty

    if fraction >= 1.0:
        del state["positions"][symbol]
    else:
        pos["qty"] -= qty

    _save_state(state)
    _log(symbol, "SELL", exit_price, qty, pnl=pnl)

    return {
        "ok": True,
        "price": exit_price,
        "qty": qty,
        "pnl": pnl
    }
