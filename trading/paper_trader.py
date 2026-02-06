# trading/paper_trader.py
# =========================================
# LIVE TRADER (Bitvavo) – single source of truth
# =========================================

import os
import time
import json
import hmac
import hashlib
import requests
from datetime import datetime
from typing import Dict, Any, Optional

# =========================================
# ENV
# =========================================
BITVAVO_API_KEY = os.getenv("BITVAVO_API_KEY")
BITVAVO_API_SECRET = os.getenv("BITVAVO_API_SECRET")

if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
    raise RuntimeError("❌ BITVAVO_API_KEY / SECRET ontbreken")

BASE_URL = "https://api.bitvavo.com/v2"

STATE_PATH = "/data/paper_state.json"
LOG_PATH = "/data/paper_trades.csv"
APPROVALS_PATH = "/data/pending_approvals.json"

HTTP_TIMEOUT = 15


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
# APPROVAL
# =========================================
def _consume_approval(prebuy_id: str) -> bool:
    if not os.path.exists(APPROVALS_PATH):
        return False

    with open(APPROVALS_PATH, "r") as f:
        data = json.load(f)

    now = int(time.time())
    new = []
    ok = False

    for a in data:
        if a.get("id") == prebuy_id:
            if a.get("status") == "APPROVED" and a.get("expires_at", 0) > now:
                ok = True
                continue
        new.append(a)

    if ok:
        with open(APPROVALS_PATH, "w") as f:
            json.dump(new, f, indent=2)

    return ok


# =========================================
# STATE
# =========================================
def _load_state() -> Dict[str, Any]:
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

    if not _consume_approval(prebuy_id):
        return {"ok": False, "reason": "APPROVAL_INVALID"}

    body = json.dumps({
        "market": symbol.replace("USDT", "-EUR"),
        "side": "buy",
        "orderType": "market",
        "amountQuote": f"{amount_eur:.2f}"
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

    filled_qty = float(res.get("filledAmount", 0))
    price = float(res.get("price", 0))

    state = _load_state()
    state["positions"][symbol] = {
        "qty": filled_qty,
        "entry": price,
        "stop_loss": stop_loss,
        "target": target,
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

    exit_price = float(res.get("price", 0))
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
