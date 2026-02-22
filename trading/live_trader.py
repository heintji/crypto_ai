# trading/live_trader.py
from __future__ import annotations

import os
import json
import time
import hmac
import hashlib
from datetime import datetime
from typing import Any, Dict, Optional

import requests

# ==========================================================
# ENV
# ==========================================================
BITVAVO_API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()

BASE_URL = "https://api.bitvavo.com/v2"
HTTP_TIMEOUT = 15

# ==========================================================
# DATA DIR (state voor monitor)
# ==========================================================
def _get_data_dir() -> str:
    d = (os.getenv("DATA_DIR") or "").strip()
    if d:
        return d
    return "/data" if os.path.isdir("/data") else "/tmp/data"


DATA_DIR = _get_data_dir()
STATE_PATH = (os.getenv("PAPER_STATE_PATH") or os.path.join(DATA_DIR, "paper_state.json")).strip()
TRADES_CSV = (os.getenv("PAPER_TRADES_CSV") or os.path.join(DATA_DIR, "paper_trades.csv")).strip()


def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def _load_state() -> Dict[str, Any]:
    _ensure_dir(STATE_PATH)
    if not os.path.exists(STATE_PATH):
        return {"positions": {}, "open_trades": []}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {"positions": {}, "open_trades": []}
    s.setdefault("positions", {})
    s.setdefault("open_trades", [])
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
# BITVAVO SIGNING
# ==========================================================
def _require_keys() -> None:
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY/SECRET ontbreken (Render env).")


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
    # jouw bot werkt met Binance symbols (BTCUSDT). Bitvavo werkt met BTC-EUR.
    # We mappen USDT -> EUR.
    return symbol.replace("USDT", "-EUR")


# ==========================================================
# LIVE BUY / SELL
# ==========================================================
def buy_eur(symbol: str, amount_eur: float, meta: Optional[Dict[str, Any]] = None, **kwargs) -> Dict[str, Any]:
    """
    Live BUY op Bitvavo (market).
    - accepteert meta=... (zodat webhook nooit crasht)
    - schrijft positie + open_trade in STATE_PATH zodat trade_monitor kan werken
    """
    _require_keys()
    meta = meta or {}

    entry_hint = float(meta.get("entry") or 0.0)
    stop = float(meta.get("stop") or meta.get("stop_loss") or kwargs.get("stop_loss") or 0.0)
    target = float(meta.get("target") or kwargs.get("target") or 0.0)
    prebuy_id = str(meta.get("prebuy_id") or kwargs.get("prebuy_id") or "")

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
    r.raise_for_status()
    res = r.json()

    filled_qty = float(res.get("filledAmount", 0) or 0.0)
    price = float(res.get("price", 0) or 0.0)

    # fallback als API geen price teruggeeft (soms)
    if price <= 0:
        price = entry_hint

    if filled_qty <= 0:
        return {"ok": False, "reason": "NO_FILL", "raw": res}

    state = _load_state()
    state.setdefault("positions", {})
    state.setdefault("open_trades", [])

    state["positions"][symbol] = {
        "qty": filled_qty,
        "entry": price,
        "stop_loss": stop,
        "target": target,
        "opened_at": int(time.time()),
        "prebuy_id": prebuy_id,
        "live": True,
    }

    trade_obj: Dict[str, Any] = {
        "symbol": symbol,
        "entry": price,
        "stop_loss": stop,
        "target": target,
        "opened_at": int(time.time()),
        "prebuy_id": prebuy_id,
        "live": True,
        # context (optioneel)
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
    _log_csv(symbol, "BUY", price, filled_qty, pnl=0.0, meta=f"live prebuy={prebuy_id}")

    return {"ok": True, "symbol": symbol, "qty": filled_qty, "price": price, "raw": res}


def sell(symbol: str, fraction: float = 1.0, **kwargs) -> Dict[str, Any]:
    """
    Live SELL op Bitvavo (market), fractioneel.
    - returnt dict met ok, price, qty, pnl (zoals trade_monitor verwacht)
    """
    _require_keys()

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
    r.raise_for_status()
    res = r.json()

    exit_price = float(res.get("price", 0) or 0.0)
    if exit_price <= 0:
        # als Bitvavo geen price teruggeeft, houden we hem op entry (pnl 0) ipv crashen
        exit_price = entry

    pnl = (exit_price - entry) * qty

    if fraction >= 1.0:
        # remove position + open_trade
        try:
            del state["positions"][symbol]
        except Exception:
            pass
        state["open_trades"] = [t for t in (state.get("open_trades") or []) if t.get("symbol") != symbol]
    else:
        pos["qty"] = qty_total - qty
        state["positions"][symbol] = pos

    _save_state(state)
    _log_csv(symbol, "SELL", exit_price, qty, pnl=pnl, meta="live_sell")

    return {"ok": True, "price": float(exit_price), "qty": float(qty), "pnl": float(pnl), "raw": res}

