# trading/paper_trader.py
import os
import json
import time
import hmac
import hashlib
import requests
from datetime import datetime
from typing import Dict, Any, Optional, List

# =========================================================
# 🔐 Bitvavo API
# =========================================================
BITVAVO_API_KEY = os.getenv("BITVAVO_API_KEY", "")
BITVAVO_API_SECRET = os.getenv("BITVAVO_API_SECRET", "")
BASE_URL = "https://api.bitvavo.com/v2"

# =========================================================
# 📁 Render Disk paths
# =========================================================
STATE_PATH = "/data/paper_state.json"
APPROVALS_PATH = "/data/pending_approvals.json"
LOG_PATH = "/data/paper_trades.csv"

HTTP_TIMEOUT = 15

# =========================================================
# Helpers
# =========================================================
def _now() -> int:
    return int(time.time())

def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _sign(method: str, path: str, body: str = "") -> Dict[str, str]:
    ts = str(int(time.time() * 1000))
    msg = ts + method + path + body
    sig = hmac.new(
        BITVAVO_API_SECRET.encode(),
        msg.encode(),
        hashlib.sha256
    ).hexdigest()
    return {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": sig,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json",
    }

# =========================================================
# MARKET DATA (nodig voor webhook)
# =========================================================
def get_price(symbol: str) -> float:
    r = requests.get(
        "https://api.binance.com/api/v3/ticker/price",
        params={"symbol": symbol},
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    return float(r.json()["price"])

# =========================================================
# STATE
# =========================================================
def load_state() -> Dict[str, Any]:
    if not os.path.isfile(STATE_PATH):
        return {"positions": {}, "open_trades": []}
    with open(STATE_PATH, "r") as f:
        return json.load(f)

def save_state(state: Dict[str, Any]):
    _ensure_dir(STATE_PATH)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)

# =========================================================
# APPROVALS
# =========================================================
def load_approvals() -> List[Dict[str, Any]]:
    if not os.path.isfile(APPROVALS_PATH):
        return []
    with open(APPROVALS_PATH, "r") as f:
        return json.load(f)

def consume_approval(prebuy_id: str) -> bool:
    approvals = load_approvals()
    now = _now()
    new = []
    ok = False

    for a in approvals:
        if a.get("id") == prebuy_id:
            if a.get("status") == "APPROVED" and int(a.get("expires_at", 0)) > now:
                ok = True
                continue
            return False
        new.append(a)

    if ok:
        with open(APPROVALS_PATH, "w") as f:
            json.dump(new, f, indent=2)

    return ok

# =========================================================
# LOGGING
# =========================================================
def log_trade(symbol, side, price, qty, meta=""):
    _ensure_dir(LOG_PATH)
    exists = os.path.isfile(LOG_PATH)
    with open(LOG_PATH, "a") as f:
        if not exists:
            f.write("datetime,symbol,side,price,qty,meta\n")
        f.write(
            f"{datetime.utcnow().isoformat()},"
            f"{symbol},{side},{price},{qty},{meta}\n"
        )

# =========================================================
# 🔥 LIVE BUY
# =========================================================
def buy_eur(
    symbol: str,
    price: Optional[float],
    amount_eur: float,
    stop_loss: float,
    target: float,
    prebuy_id: str
) -> Dict[str, Any]:

    if not consume_approval(prebuy_id):
        return {"ok": False, "reason": "APPROVAL_INVALID"}

    body = json.dumps({
        "market": symbol.replace("USDT", "-EUR"),
        "side": "buy",
        "orderType": "market",
        "amountQuote": f"{amount_eur:.2f}"
    })

    headers = _sign("POST", "/v2/order", body)
    r = requests.post(BASE_URL + "/order", headers=headers, data=body, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    order = r.json()

    fills = order.get("fills", [])
    avg_price = float(order.get("price", 0.0))
    qty = float(order.get("amount", 0.0))

    state = load_state()
    trade_id = f"{symbol}-{_now()}"

    state["positions"][symbol] = qty
    state["open_trades"].append({
        "trade_id": trade_id,
        "symbol": symbol,
        "entry": avg_price,
        "stop_loss": stop_loss,
        "target": target,
        "qty": qty,
        "opened_at": _now()
    })

    save_state(state)
    log_trade(symbol, "BUY", avg_price, qty, meta=f"prebuy={prebuy_id}")

    return {
        "ok": True,
        "trade_id": trade_id,
        "symbol": symbol,
        "price": avg_price,
        "qty": qty
    }

# =========================================================
# 🔥 LIVE SELL
# =========================================================
def sell(symbol: str, fraction: float = 1.0) -> Dict[str, Any]:
    state = load_state()
    qty_total = float(state["positions"].get(symbol, 0.0))
    if qty_total <= 0:
        return {"ok": False, "reason": "NO_POSITION"}

    qty_sell = qty_total * fraction

    body = json.dumps({
        "market": symbol.replace("USDT", "-EUR"),
        "side": "sell",
        "orderType": "market",
        "amount": f"{qty_sell:.8f}"
    })

    headers = _sign("POST", "/v2/order", body)
    r = requests.post(BASE_URL + "/order", headers=headers, data=body, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    order = r.json()

    price = float(order.get("price", 0.0))

    if fraction >= 0.999:
        state["positions"].pop(symbol, None)
        state["open_trades"] = [t for t in state["open_trades"] if t["symbol"] != symbol]
    else:
        state["positions"][symbol] = qty_total - qty_sell

    save_state(state)
    log_trade(symbol, "SELL", price, qty_sell, meta=f"fraction={fraction}")

    return {
        "ok": True,
        "symbol": symbol,
        "price": price,
        "qty_sold": qty_sell
    }

if __name__ == "__main__":
    print("🔥 LIVE trader actief (Bitvavo)")
