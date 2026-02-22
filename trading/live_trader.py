# trading/live_trader.py
# =========================================
# LIVE TRADER (Bitvavo)
# - buy_eur: market buy met EUR (amountQuote)
# - sell: market sell op base amount
# - schrijft state naar /data/paper_state.json zodat trade_monitor kan exiten
# =========================================

from __future__ import annotations

import os
import time
import json
import hmac
import hashlib
from typing import Any, Dict, Optional

import requests

# =========================
# ENV
# =========================
BITVAVO_API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()

if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
    raise RuntimeError("BITVAVO_API_KEY / BITVAVO_API_SECRET ontbreken")

BASE_URL = "https://api.bitvavo.com/v2"
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "15"))

# =========================
# DATA DIR / STATE
# =========================
def _get_data_dir() -> str:
    d = (os.getenv("DATA_DIR") or "").strip()
    if d:
        return d
    return "/data" if os.path.isdir("/data") else "/tmp/data"

DATA_DIR = _get_data_dir()
STATE_PATH = (os.getenv("PAPER_STATE_PATH") or os.path.join(DATA_DIR, "paper_state.json")).strip()

def _ensure_dir_for_file(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _load_state() -> Dict[str, Any]:
    _ensure_dir_for_file(STATE_PATH)
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
    _ensure_dir_for_file(STATE_PATH)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)

# =========================
# BITVAVO SIGNING
# =========================
def _sign(timestamp_ms: str, method: str, path: str, body: str = "") -> str:
    msg = timestamp_ms + method.upper() + path + body
    return hmac.new(
        BITVAVO_API_SECRET.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

def _headers(method: str, path: str, body: str = "") -> Dict[str, str]:
    ts = str(int(time.time() * 1000))
    return {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": _sign(ts, method, path, body),
        "Bitvavo-Access-Timestamp": ts,
        "Content-Type": "application/json",
    }

def _symbol_to_market(symbol: str) -> str:
    """
    Verwacht input zoals: INJUSDT, BTCUSDT.
    Bitvavo market is: INJ-EUR, BTC-EUR.
    """
    s = (symbol or "").strip().upper()
    if "-" in s:
        # als iemand ooit INJ-EUR doorgeeft
        return s
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}-EUR"
    # fallback: probeer als base al is
    return f"{s}-EUR"

# =========================
# ORDER CALLS
# =========================
def _post_order(body_dict: Dict[str, Any]) -> Dict[str, Any]:
    body = json.dumps(body_dict)
    path = "/order"
    r = requests.post(
        BASE_URL + path,
        headers=_headers("POST", path, body),
        data=body,
        timeout=HTTP_TIMEOUT,
    )
    # als Bitvavo error geeft, wil je die tekst zien
    if r.status_code >= 400:
        return {"ok": False, "status": r.status_code, "error": r.text[:500]}
    return {"ok": True, "data": r.json()}

# =========================
# PUBLIC API (used by webhook + trade_monitor)
# =========================
def buy_eur(
    symbol: str,
    amount_eur: float,
    prebuy_id: Optional[str] = None,
    stop_loss: Optional[float] = None,
    target: Optional[float] = None,
    meta: Optional[Dict[str, Any]] = None,
    **kwargs,
) -> Dict[str, Any]:
    """
    Market BUY voor €amount_eur op Bitvavo.
    Schrijft state zodat trade_monitor later kan SELL’en.
    """
    market = _symbol_to_market(symbol)

    # meta/kwargs compat (jouw webhook stuurt soms stop/entry/target)
    if meta and isinstance(meta, dict):
        prebuy_id = prebuy_id or str(meta.get("prebuy_id") or "")
        stop_loss = stop_loss if stop_loss is not None else (meta.get("stop") or meta.get("stop_loss"))
        target = target if target is not None else meta.get("target")

    # ook accepteren we "stop" via kwargs
    if stop_loss is None and "stop" in kwargs:
        try:
            stop_loss = float(kwargs["stop"])
        except Exception:
            pass

    body = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        # amountQuote = euro bedrag
        "amountQuote": f"{float(amount_eur):.2f}",
    }

    res = _post_order(body)
    if not res.get("ok"):
        return res

    data = res["data"]
    filled_qty = float(data.get("filledAmount", 0) or 0.0)
    price = float(data.get("price", 0) or 0.0)

    # fallback als exchange niet direct price returned
    # (gebeurt soms bij market orders)
    if price <= 0:
        try:
            # laatste bekende avgPrice is niet altijd aanwezig
            price = float(data.get("averagePrice", 0) or 0.0)
        except Exception:
            price = 0.0

    # state bijwerken
    state = _load_state()
    state.setdefault("positions", {})
    state.setdefault("open_trades", [])

    state["positions"][symbol] = {
        "qty": filled_qty,
        "entry": price,
        "stop_loss": float(stop_loss or 0.0),
        "target": float(target or 0.0),
        "opened_at": int(time.time()),
        "prebuy_id": prebuy_id or "",
        "market": market,
        "mode": "NORMAL",
    }

    # trade_monitor verwacht open_trades lijst
    state["open_trades"] = [t for t in state["open_trades"] if t.get("symbol") != symbol]
    state["open_trades"].append(
        {
            "symbol": symbol,
            "entry": price,
            "stop_loss": float(stop_loss or 0.0),
            "target": float(target or 0.0),
            "opened_at": int(time.time()),
            "prebuy_id": prebuy_id or "",
            "mode": "NORMAL",
            "status": "OPEN",
        }
    )

    _save_state(state)

    return {
        "ok": True,
        "symbol": symbol,
        "market": market,
        "qty": filled_qty,
        "price": price,
        "prebuy_id": prebuy_id,
    }

def sell(symbol: str, fraction: float = 1.0, **kwargs) -> Dict[str, Any]:
    """
    Market SELL op Bitvavo voor de base amount.
    fraction=1.0 = alles.
    """
    state = _load_state()
    pos = (state.get("positions") or {}).get(symbol)
    if not pos:
        return {"ok": False, "reason": "NO_POSITION"}

    market = pos.get("market") or _symbol_to_market(symbol)
    qty_total = float(pos.get("qty") or 0.0)
    qty = qty_total * float(fraction)

    if qty <= 0:
        return {"ok": False, "reason": "QTY_ZERO"}

    body = {
        "market": market,
        "side": "sell",
        "orderType": "market",
        "amount": f"{qty:.8f}",
    }

    res = _post_order(body)
    if not res.get("ok"):
        return res

    data = res["data"]
    exit_price = float(data.get("price", 0) or 0.0)
    if exit_price <= 0:
        try:
            exit_price = float(data.get("averagePrice", 0) or 0.0)
        except Exception:
            exit_price = 0.0

    entry = float(pos.get("entry") or 0.0)
    pnl = (exit_price - entry) * qty if entry > 0 and exit_price > 0 else 0.0

    # state aanpassen
    if float(fraction) >= 1.0:
        # remove position + open_trade
        (state.get("positions") or {}).pop(symbol, None)
        state["open_trades"] = [t for t in (state.get("open_trades") or []) if t.get("symbol") != symbol]
    else:
        pos["qty"] = max(0.0, qty_total - qty)
        state["positions"][symbol] = pos

    _save_state(state)

    return {"ok": True, "symbol": symbol, "market": market, "price": exit_price, "qty": qty, "pnl": pnl}
