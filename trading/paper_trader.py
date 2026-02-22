# trading/paper_trader.py
from __future__ import annotations

import os
import json
import time
from datetime import datetime
from typing import Any, Dict, Optional

import requests

# ==========================================================
# DATA DIR (disk-safe fallback)
# ==========================================================
def _get_data_dir() -> str:
    d = (os.getenv("DATA_DIR") or "").strip()
    if d:
        return d
    return "/data" if os.path.isdir("/data") else "/tmp/data"


DATA_DIR = _get_data_dir()
STATE_PATH = (os.getenv("PAPER_STATE_PATH") or os.path.join(DATA_DIR, "paper_state.json")).strip()
TRADES_CSV = (os.getenv("PAPER_TRADES_CSV") or os.path.join(DATA_DIR, "paper_trades.csv")).strip()

HTTP_TIMEOUT = 15
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"


# ==========================================================
# IO HELPERS
# ==========================================================
def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def _load_state() -> Dict[str, Any]:
    _ensure_dir(STATE_PATH)
    if not os.path.exists(STATE_PATH):
        return {"positions": {}, "open_trades": [], "balance_eur": 0.0}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {"positions": {}, "open_trades": [], "balance_eur": 0.0}

    s.setdefault("positions", {})
    s.setdefault("open_trades", [])
    s.setdefault("balance_eur", 0.0)
    return s


def _save_state(state: Dict[str, Any]) -> None:
    _ensure_dir(STATE_PATH)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def _log_csv(symbol: str, side: str, price: float, qty: float, pnl: float = 0.0, meta: str = "") -> None:
    _ensure_dir(TRADES_CSV)
    new = not os.path.exists(TRADES_CSV)
    with open(TRADES_CSV, "a", encoding="utf-8") as f:
        if new:
            f.write("datetime,symbol,side,price,qty,pnl,meta\n")
        f.write(
            f"{datetime.utcnow().isoformat()},"
            f"{symbol},{side},{price:.10f},{qty:.10f},{pnl:.6f},{meta}\n"
        )


# ==========================================================
# MARKET DATA
# ==========================================================
def get_price(symbol: str) -> float:
    r = requests.get(BINANCE_TICKER_URL, params={"symbol": symbol}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return float(r.json()["price"])


# ==========================================================
# PAPER BUY / SELL
# ==========================================================
def buy_eur(symbol: str, amount_eur: float, meta: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Paper BUY:
    - accepteert meta=... (zodat webhook nooit crasht)
    - schrijft naar paper_state.json:
        positions[symbol] + open_trades[]
    """
    meta = meta or {}

    entry = float(meta.get("entry") or 0.0)
    stop = float(meta.get("stop") or meta.get("stop_loss") or 0.0)
    target = float(meta.get("target") or 0.0)
    prebuy_id = str(meta.get("prebuy_id") or meta.get("id") or kwargs.get("prebuy_id") or "")

    # fallback: haal prijs live op
    if entry <= 0:
        try:
            entry = get_price(symbol)
        except Exception:
            entry = 0.0

    if entry <= 0:
        return {"ok": False, "reason": "NO_PRICE"}

    # simpele qty berekening (paper)
    qty = float(amount_eur) / entry if entry > 0 else 0.0
    if qty <= 0:
        return {"ok": False, "reason": "QTY_ZERO"}

    state = _load_state()
    state.setdefault("positions", {})
    state.setdefault("open_trades", [])

    # positions voor monitor
    state["positions"][symbol] = {
        "qty": qty,
        "entry": entry,
        "stop_loss": stop,
        "target": target,
        "opened_at": int(time.time()),
        "prebuy_id": prebuy_id,
    }

    # open_trades voor monitor (jouw trade_monitor leest dit)
    trade_obj: Dict[str, Any] = {
        "symbol": symbol,
        "entry": entry,
        "stop_loss": stop,
        "target": target,
        "opened_at": int(time.time()),
        "prebuy_id": prebuy_id,
        # extra context als aanwezig
        "setup_type": meta.get("setup_type"),
        "timeframe": meta.get("timeframe"),
        "regime": meta.get("regime"),
        "score": meta.get("score"),
        "raw_score": meta.get("raw_score"),
        "chance": meta.get("chance"),
        "confidence": meta.get("confidence"),
        "label": meta.get("label"),
    }
    state["open_trades"].append(trade_obj)

    _save_state(state)
    _log_csv(symbol, "BUY", entry, qty, pnl=0.0, meta=f"prebuy={prebuy_id}")

    return {"ok": True, "symbol": symbol, "qty": qty, "price": entry}


def sell(symbol: str, fraction: float = 1.0, **kwargs) -> Dict[str, Any]:
    """
    Paper SELL:
    - gebruikt paper_state.json
    - returnt dict met ok, price, qty, pnl (zoals jouw trade_monitor verwacht)
    """
    state = _load_state()
    pos = (state.get("positions") or {}).get(symbol)
    if not pos:
        return {"ok": False, "reason": "NO_POSITION"}

    entry = float(pos.get("entry") or 0.0)
    qty_total = float(pos.get("qty") or 0.0)
    if qty_total <= 0:
        return {"ok": False, "reason": "QTY_ZERO"}

    fraction = float(fraction)
    if fraction <= 0:
        return {"ok": False, "reason": "BAD_FRACTION"}

    qty = qty_total * min(1.0, fraction)

    try:
        price = get_price(symbol)
    except Exception:
        price = entry if entry > 0 else 0.0

    pnl = (price - entry) * qty

    # update position
    if fraction >= 1.0:
        del state["positions"][symbol]
        # open_trades opruimen
        state["open_trades"] = [t for t in (state.get("open_trades") or []) if t.get("symbol") != symbol]
    else:
        pos["qty"] = qty_total - qty
        state["positions"][symbol] = pos

    _save_state(state)
    _log_csv(symbol, "SELL", price, qty, pnl=pnl, meta="paper_sell")

    return {"ok": True, "price": float(price), "qty": float(qty), "pnl": float(pnl)}
