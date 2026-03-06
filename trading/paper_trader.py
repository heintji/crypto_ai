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


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _safe_str(x: Any, default: str = "") -> str:
    if x is None:
        return default
    try:
        s = str(x).strip()
        return s if s else default
    except Exception:
        return default


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


def _meta_to_log_str(meta: Any) -> str:
    try:
        if isinstance(meta, dict):
            return json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
        return str(meta or "")
    except Exception:
        return str(meta or "")


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


def _remove_existing_open_trade(state: Dict[str, Any], symbol: str) -> None:
    open_trades = state.get("open_trades", []) or []
    state["open_trades"] = [t for t in open_trades if t.get("symbol") != symbol]


def buy_eur(symbol: str, amount_eur: float, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    PAPER BUY:
    - koopt tegen Binance prijs
    - schrijft positie + open_trade in paper_state.json
    - neemt uitgebreide leer-context mee uit meta

    Verwachte meta-velden kunnen o.a. zijn:
    prebuy_id, stop_loss, stop, target, timeframe, setup_type, regime,
    score, raw_score, chance, confidence, label, why_tag,
    market_condition, overextended_pct
    """
    meta = meta or {}

    prebuy_id = _safe_str(meta.get("prebuy_id"))
    stop_loss = _safe_float(meta.get("stop_loss") or meta.get("stop"), 0.0)
    target = _safe_float(meta.get("target"), 0.0)

    timeframe = _safe_str(meta.get("timeframe"), "4h")
    setup_type = _safe_str(meta.get("setup_type"), "TREND_PULLBACK")
    regime = _safe_str(meta.get("regime"), "UNKNOWN")

    score = _safe_int(meta.get("score"), 0)
    raw_score = _safe_int(meta.get("raw_score"), score)
    chance = _safe_int(meta.get("chance"), 0)
    confidence = _safe_int(meta.get("confidence"), chance)

    label = _safe_str(meta.get("label"), "GO")
    why_tag = _safe_str(meta.get("why_tag"))
    market_condition = _safe_str(meta.get("market_condition"), "NORMAAL")
    overextended_pct = _safe_float(meta.get("overextended_pct"), 0.0)

    user_decision = _safe_str(meta.get("user_decision"), "YES")
    bot_decision = _safe_str(meta.get("bot_decision"), label)

    state = _load_state()
    bal = _safe_float(state.get("balance_eur"), 0.0)

    if amount_eur <= 0:
        return {"ok": False, "reason": "AMOUNT_INVALID"}

    if bal < amount_eur:
        return {"ok": False, "reason": "INSUFFICIENT_PAPER_BALANCE", "balance_eur": bal}

    price = _binance_price(symbol)
    qty = amount_eur / price
    opened_at = int(time.time())

    # update balance
    state["balance_eur"] = bal - amount_eur

    # position
    state["positions"][symbol] = {
        "qty": qty,
        "entry": price,
        "stop_loss": stop_loss,
        "target": target,
        "opened_at": opened_at,
        "prebuy_id": prebuy_id,
        "amount_eur": amount_eur,
        "position_size": qty,
        "base_amount": qty,
        "timeframe": timeframe,
        "setup_type": setup_type,
        "regime": regime,
        "score": score,
        "raw_score": raw_score,
        "chance": chance,
        "confidence": confidence,
        "label": label,
        "why_tag": why_tag,
        "market_condition": market_condition,
        "overextended_pct": overextended_pct,
        "user_decision": user_decision,
        "bot_decision": bot_decision,
        "live": False,
    }

    # voorkom dubbele open_trades voor zelfde symbool
    _remove_existing_open_trade(state, symbol)

    # open_trade (trade_monitor verwacht dit)
    state["open_trades"].append(
        {
            "symbol": symbol,
            "entry": price,
            "stop_loss": stop_loss,
            "stop": stop_loss,
            "target": target,
            "prebuy_id": prebuy_id,
            "status": "OPEN",
            "opened_at": opened_at,
            "monitor_started_at": opened_at,
            "mode": "NORMAL",
            "live": False,

            # trade sizing
            "amount_eur": amount_eur,
            "position_size": qty,
            "base_amount": qty,

            # leer/context velden
            "timeframe": timeframe,
            "setup_type": setup_type,
            "regime": regime,
            "score": score,
            "raw_score": raw_score,
            "chance": chance,
            "confidence": confidence,
            "label": label,
            "why_tag": why_tag,
            "market_condition": market_condition,
            "overextended_pct": overextended_pct,
            "user_decision": user_decision,
            "bot_decision": bot_decision,

            # monitor leerwaarden
            "max_r": 0.0,
            "mfe_r": 0.0,
            "mae_r": 0.0,
            "max_price_seen": price,
            "min_price_seen": price,
            "had_over_1r": False,
            "partial_sold_40": False,
            "below_1r_count": 0,
            "target_reached_notified": False,
            "last_check": opened_at,
        }
    )

    _save_state(state)
    _log_row(symbol, "BUY", price, qty, meta=_meta_to_log_str({
        "prebuy_id": prebuy_id,
        "amount_eur": amount_eur,
        "setup_type": setup_type,
        "regime": regime,
        "score": score,
        "chance": chance,
        "label": label,
    }))

    return {
        "ok": True,
        "symbol": symbol,
        "qty": qty,
        "price": price,
        "paper": True,
        "prebuy_id": prebuy_id,
    }


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

    qty_total = _safe_float(pos.get("qty"), 0.0)
    if qty_total <= 0:
        return {"ok": False, "reason": "QTY_INVALID"}

    qty = qty_total if fraction >= 1.0 else qty_total * fraction
    price = _binance_price(symbol)

    entry = _safe_float(pos.get("entry"), 0.0)
    pnl = (price - entry) * qty
    proceeds = price * qty

    # update balance + position
    state["balance_eur"] = _safe_float(state.get("balance_eur"), 0.0) + proceeds

    if fraction >= 1.0:
        del state["positions"][symbol]
    else:
        pos["qty"] = qty_total - qty
        pos["position_size"] = pos["qty"]
        pos["base_amount"] = pos["qty"]
        pos["last_sell_price"] = price
        pos["last_sell_at"] = int(time.time())
        state["positions"][symbol] = pos

    _save_state(state)
    _log_row(
        symbol,
        "SELL",
        price,
        qty,
        pnl=pnl,
        meta=_meta_to_log_str({
            "reason": meta.get("reason") or "",
            "fraction": fraction,
            "prebuy_id": pos.get("prebuy_id") or "",
        }),
    )

    return {
        "ok": True,
        "price": price,
        "qty": qty,
        "pnl": pnl,
        "paper": True,
    }
