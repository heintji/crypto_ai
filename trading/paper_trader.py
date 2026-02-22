# trading/paper_trader.py
from __future__ import annotations

import os
import time
import json
from datetime import datetime
from typing import Any, Dict, Optional

import requests

HTTP_TIMEOUT = 15
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"

# =========================
# DATA DIR (disk-safe fallback)
# =========================
def _get_data_dir() -> str:
    d = (os.getenv("DATA_DIR") or "").strip()
    if d:
        return d
    return "/data" if os.path.isdir("/data") else "/tmp/data"

DATA_DIR = _get_data_dir()
STATE_PATH = (os.getenv("PAPER_STATE_PATH") or os.path.join(DATA_DIR, "paper_state.json")).strip()
LOG_PATH = (os.getenv("PAPER_TRADES_CSV") or os.path.join(DATA_DIR, "paper_trades.csv")).strip()

START_BALANCE_EUR = float(os.getenv("PAPER_START_BALANCE_EUR") or "1000")


def _ensure_dir_for(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def _load_state() -> Dict[str, Any]:
    _ensure_dir_for(STATE_PATH)
    if not os.path.exists(STATE_PATH):
        return {
            "balance_eur": START_BALANCE_EUR,
            "positions": {},
            "open_trades": [],
        }
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("balance_eur", START_BALANCE_EUR)
    s.setdefault("positions", {})
    s.setdefault("open_trades", [])
    return s


def _save_state(state: Dict[str, Any]) -> None:
    _ensure_dir_for(STATE_PATH)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def _log_row(symbol: str, side: str, price: float, qty: float, pnl: float = 0.0, meta: str = "") -> None:
    _ensure_dir_for(LOG_PATH)
    new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        if new:
            f.write("datetime,symbol,side,price,qty,pnl,meta\n")
        f.write(f"{datetime.utcnow().isoformat()},{symbol},{side},{price},{qty},{pnl},{meta}\n")


def _binance_price(symbol: str) -> float:
    r = requests.get(BINANCE_TICKER_URL, params={"symbol": symbol}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return float(r.json()["price"])


def buy_eur(symbol: str, amount_eur: float, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    PAPER BUY:
    - koopt tegen Binance prijs
    - schrijft positie + open_trade in paper_state.json
    meta verwacht: prebuy_id, stop_loss, target
    """
    meta = meta or {}
    prebuy_id = str(meta.get("prebuy_id") or "")
    stop_loss = float(meta.get("stop_loss") or meta.get("stop") or 0.0)
    target = float(meta.get("target") or 0.0)

    state = _load_state()
    bal = float(state.get("balance_eur") or 0.0)

    if amount_eur <= 0:
        return {"ok": False, "reason": "AMOUNT_INVALID"}
    if bal < amount_eur:
        return {"ok": False, "reason": "INSUFFICIENT_PAPER_BALANCE", "balance_eur": bal}

    price = _binance_price(symbol)
    qty = amount_eur / price

    # update balance
    state["balance_eur"] = bal - amount_eur

    # position
    state["positions"][symbol] = {
        "qty": qty,
        "entry": price,
        "stop_loss": stop_loss,
        "target": target,
        "opened_at": int(time.time()),
        "prebuy_id": prebuy_id,
    }

    # open_trade (trade_monitor verwacht dit)
    state["open_trades"].append(
        {
            "symbol": symbol,
            "entry": price,
            "stop_loss": stop_loss,
            "target": target,
            "prebuy_id": prebuy_id,
            "status": "OPEN",
            "opened_at": int(time.time()),
            "mode": "NORMAL",
        }
    )

    _save_state(state)
    _log_row(symbol, "BUY", price, qty, meta=f"prebuy={prebuy_id}")

    return {"ok": True, "symbol": symbol, "qty": qty, "price": price, "paper": True}


def sell(symbol: str, fraction: float = 1.0, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    PAPER SELL:
    - verkoopt fraction van qty tegen Binance prijs
    - update balance
    """
    meta = meta or {}
    state = _load_state()
    pos = state.get("positions", {}).get(symbol)
    if not pos:
        return {"ok": False, "reason": "NO_POSITION"}

    fraction = float(fraction)
    if fraction <= 0:
        return {"ok": False, "reason": "FRACTION_INVALID"}

    qty_total = float(pos.get("qty") or 0.0)
    if qty_total <= 0:
        return {"ok": False, "reason": "QTY_INVALID"}

    qty = qty_total if fraction >= 1.0 else qty_total * fraction
    price = _binance_price(symbol)

    entry = float(pos.get("entry") or 0.0)
    pnl = (price - entry) * qty
    proceeds = price * qty

    # update balance + position
    state["balance_eur"] = float(state.get("balance_eur") or 0.0) + proceeds

    if fraction >= 1.0:
        del state["positions"][symbol]
    else:
        pos["qty"] = qty_total - qty
        state["positions"][symbol] = pos

    _save_state(state)
    _log_row(symbol, "SELL", price, qty, pnl=pnl, meta=str(meta.get("reason") or ""))

    return {"ok": True, "price": price, "qty": qty, "pnl": pnl, "paper": True}
