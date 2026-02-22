# trading/live_trader.py
from __future__ import annotations

import os
import time
import json
import hmac
import hashlib
from datetime import datetime
from typing import Any, Dict, Optional

import requests

BITVAVO_API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()

BASE_URL = "https://api.bitvavo.com/v2"
HTTP_TIMEOUT = 15

def _get_data_dir() -> str:
    d = (os.getenv("DATA_DIR") or "").strip()
    if d:
        return d
    return "/data" if os.path.isdir("/data") else "/tmp/data"

DATA_DIR = _get_data_dir()
STATE_PATH = (os.getenv("PAPER_STATE_PATH") or os.path.join(DATA_DIR, "paper_state.json")).strip()
LOG_PATH = (os.getenv("PAPER_TRADES_CSV") or os.path.join(DATA_DIR, "paper_trades.csv")).strip()

def _ensure_dir_for(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _load_state() -> Dict[str, Any]:
    _ensure_dir_for(STATE_PATH)
    if not os.path.exists(STATE_PATH):
        return {"positions": {}, "open_trades": []}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
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

def _require_keys() -> None:
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY/SECRET ontbreken")

def _sign(timestamp: str, method: str, path: str, body: str = "") -> str:
    msg = timestamp + method + path + body
    return hmac.new(BITVAVO_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def _headers(method: str, path: str, body: str = "") -> Dict[str, str]:
    ts = str(int(time.time() * 1000))
    return {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": _sign(ts, method, path, body),
        "Bitvavo-Access-Timestamp": ts,
        "Content-Type": "application/json",
    }

def _market(symbol: str) -> str:
    # BTCUSDT -> BTC-EUR (jij trade’t in EUR via Bitvavo)
    base = symbol.replace("USDT", "")
    return f"{base}-EUR"

def buy_eur(symbol: str, amount_eur: float, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    LIVE BUY (Bitvavo):
    meta verwacht: prebuy_id, stop_loss, target
    Schrijft positie + open_trade in paper_state.json zodat trade_monitor kan verkopen.
    """
    _require_keys()
    meta = meta or {}
    prebuy_id = str(meta.get("prebuy_id") or "")
    stop_loss = float(meta.get("stop_loss") or meta.get("stop") or 0.0)
    target = float(meta.get("target") or 0.0)

    if amount_eur <= 0:
        return {"ok": False, "reason": "AMOUNT_INVALID"}

    body = json.dumps(
        {
            "market": _market(symbol),
            "side": "buy",
            "orderType": "market",
            "amountQuote": f"{float(amount_eur):.2f}",
        }
    )

    path = "/order"
    r = requests.post(
        BASE_URL + path,
        headers=_headers("POST", path, body),
        data=body,
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code >= 400:
        return {"ok": False, "reason": "BITVAVO_BUY_FAILED", "status": r.status_code, "text": r.text[:300]}

    res = r.json()
    filled_qty = float(res.get("filledAmount", 0) or 0)
    price = float(res.get("price", 0) or 0)

    # state voor monitor
    state = _load_state()
    state.setdefault("positions", {})
    state.setdefault("open_trades", [])

    state["positions"][symbol] = {
        "qty": filled_qty,
        "entry": price,
        "stop_loss": stop_loss,
        "target": target,
        "opened_at": int(time.time()),
        "prebuy_id": prebuy_id,
        "live": True,
    }

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
            "live": True,
        }
    )

    _save_state(state)
    _log_row(symbol, "BUY", price, filled_qty, meta=f"prebuy={prebuy_id}")

    return {"ok": True, "symbol": symbol, "qty": filled_qty, "price": price, "live": True}

def sell(symbol: str, fraction: float = 1.0, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    LIVE SELL (Bitvavo):
    verkoopt fraction van state-qty.
    """
    _require_keys()
    meta = meta or {}
    state = _load_state()
    pos = state.get("positions", {}).get(symbol)
    if not pos:
        return {"ok": False, "reason": "NO_POSITION"}

    qty_total = float(pos.get("qty") or 0.0)
    if qty_total <= 0:
        return {"ok": False, "reason": "QTY_INVALID"}

    fraction = float(fraction)
    qty = qty_total if fraction >= 1.0 else qty_total * fraction

    body = json.dumps(
        {
            "market": _market(symbol),
            "side": "sell",
            "orderType": "market",
            "amount": f"{qty:.8f}",
        }
    )

    path = "/order"
    r = requests.post(
        BASE_URL + path,
        headers=_headers("POST", path, body),
        data=body,
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code >= 400:
        return {"ok": False, "reason": "BITVAVO_SELL_FAILED", "status": r.status_code, "text": r.text[:300]}

    res = r.json()
    exit_price = float(res.get("price", 0) or 0)
    entry = float(pos.get("entry") or 0.0)
    pnl = (exit_price - entry) * qty

    if fraction >= 1.0:
        del state["positions"][symbol]
    else:
        pos["qty"] = qty_total - qty
        state["positions"][symbol] = pos

    _save_state(state)
    _log_row(symbol, "SELL", exit_price, qty, pnl=pnl, meta=str(meta.get("reason") or ""))

    return {"ok": True, "price": exit_price, "qty": qty, "pnl": pnl, "live": True}
