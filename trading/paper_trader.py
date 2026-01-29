import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

# =========================================
# PATHS (altijd correct, ook vanuit andere map)
# =========================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOG_PATH = os.path.join(PROJECT_ROOT, "logs", "paper_trades.csv")
STATE_PATH = os.path.join(PROJECT_ROOT, "data", "paper_state.json")
APPROVALS_PATH = os.path.join(PROJECT_ROOT, "data", "pending_approvals.json")

START_BALANCE = 1000.0

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
HTTP_TIMEOUT = 15


# =========================================
# MARKET DATA
# =========================================
def get_price(symbol: str) -> float:
    r = requests.get(BINANCE_TICKER_URL, params={"symbol": symbol}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return float(r.json()["price"])


# =========================================
# STATE
# =========================================
def _default_state() -> Dict[str, Any]:
    return {
        "balance": float(START_BALANCE),
        "positions": {},
        "open_trades": []
    }


def load_state() -> Dict[str, Any]:
    if not os.path.isfile(STATE_PATH):
        return _default_state()

    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        return _default_state()

    base = _default_state()
    s.setdefault("balance", base["balance"])
    s.setdefault("positions", base["positions"])
    s.setdefault("open_trades", base["open_trades"])

    return s


def save_state(state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def get_balance() -> float:
    return float(load_state().get("balance", 0.0))


def has_position(symbol: str) -> bool:
    s = load_state()
    return float(s.get("positions", {}).get(symbol, 0.0)) > 0


# =========================================
# APPROVAL HANDLING (CRUCIAAL)
# =========================================
def load_approvals() -> List[Dict[str, Any]]:
    if not os.path.isfile(APPROVALS_PATH):
        return []
    try:
        with open(APPROVALS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_approvals(data: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(APPROVALS_PATH), exist_ok=True)
    with open(APPROVALS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def consume_approval(prebuy_id: str) -> bool:
    """
    🔐 VERWIJDERT approval DEFINITIEF na BUY
    → voorkomt dubbele trades na restart
    """
    approvals = load_approvals()
    new_list = []
    consumed = False

    now = int(time.time())

    for a in approvals:
        if a.get("id") == prebuy_id:
            if a.get("status") != "APPROVED":
                return False
            if int(a.get("expires_at", 0)) < now:
                return False
            consumed = True
            continue
        new_list.append(a)

    if consumed:
        save_approvals(new_list)

    return consumed


# =========================================
# LOGGING
# =========================================
def log_trade(symbol: str, side: str, price: float, qty: float, balance: float, pnl: float = 0.0, meta: str = "") -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    exists = os.path.isfile(LOG_PATH)

    with open(LOG_PATH, "a", encoding="utf-8") as f:
        if not exists:
            f.write("datetime,symbol,side,price,qty,balance,pnl,meta\n")
        f.write(
            f"{datetime.now().isoformat(timespec='seconds')},"
            f"{symbol},{side},{price:.8f},{qty:.12f},{balance:.2f},{pnl:.2f},{meta}\n"
        )


# =========================================
# BUY (ALLEEN VIA APPROVAL)
# =========================================
def buy_eur(
    symbol: str,
    price: Optional[float],
    amount_eur: float,
    stop_loss: float,
    target: float,
    prebuy_id: Optional[str] = None
) -> Dict[str, Any]:

    if not prebuy_id:
        return {"ok": False, "reason": "MISSING_PREBUY_ID"}

    if not consume_approval(prebuy_id):
        return {"ok": False, "reason": "APPROVAL_INVALID_OR_USED"}

    state = load_state()
    balance = float(state["balance"])

    if has_position(symbol):
        return {"ok": False, "reason": "ALREADY_IN_POSITION"}

    if price is None:
        price = get_price(symbol)

    if amount_eur > balance:
        return {"ok": False, "reason": "INSUFFICIENT_BALANCE"}

    qty = amount_eur / price
    state["balance"] -= amount_eur

    state["positions"][symbol] = qty
    state["positions"][f"{symbol}_entry"] = price

    trade_id = f"{symbol}-{int(time.time())}"

    state["open_trades"].append({
        "trade_id": trade_id,
        "symbol": symbol,
        "entry": price,
        "stop_loss": stop_loss,
        "target": target,
        "qty": qty,
        "side": "LONG",
        "prebuy_id": prebuy_id,
        "opened_at": int(time.time())
    })

    save_state(state)
    log_trade(symbol, "BUY", price, qty, state["balance"], meta=f"prebuy={prebuy_id}")

    print(f"✅ BUY OK | {symbol} €{amount_eur:.2f} | price={price:.6f}")

    return {
        "ok": True,
        "trade_id": trade_id,
        "symbol": symbol,
        "price": price,
        "qty": qty,
        "balance": state["balance"]
    }


# =========================================
# SELL (AANGESTUURD DOOR trade_monitor)
# =========================================
def sell(symbol: str, fraction: float = 1.0) -> Dict[str, Any]:
    state = load_state()
    positions = state["positions"]
    open_trades = state["open_trades"]

    qty_total = float(positions.get(symbol, 0.0))
    if qty_total <= 0:
        return {"ok": False, "reason": "NO_POSITION"}

    fraction = max(0.0, min(1.0, fraction))
    if fraction <= 0:
        return {"ok": False, "reason": "INVALID_FRACTION"}

    price = get_price(symbol)
    entry = float(positions.get(f"{symbol}_entry", price))

    qty_sell = qty_total * fraction
    proceeds = qty_sell * price
    pnl = (price - entry) * qty_sell

    state["balance"] += proceeds
    qty_left = qty_total - qty_sell

    if qty_left <= 1e-12:
        positions.pop(symbol, None)
        positions.pop(f"{symbol}_entry", None)
        state["open_trades"] = [t for t in open_trades if t["symbol"] != symbol]
    else:
        positions[symbol] = qty_left
        for t in open_trades:
            if t["symbol"] == symbol:
                t["qty"] = qty_left

    save_state(state)
    log_trade(symbol, "SELL", price, qty_sell, state["balance"], pnl=pnl, meta=f"fraction={fraction}")

    print(f"💰 SELL | {symbol} {fraction*100:.0f}% | pnl={pnl:.2f}")

    return {
        "ok": True,
        "symbol": symbol,
        "price": price,
        "qty_sold": qty_sell,
        "qty_left": max(0.0, qty_left),
        "pnl": pnl,
        "balance": state["balance"]
    }


if __name__ == "__main__":
    print("✅ paper_trader klaar")
    print(f"Balance: €{get_balance():.2f}")
