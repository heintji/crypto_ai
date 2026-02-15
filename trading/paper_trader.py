# trading/paper_trader.py
# =========================================
# LIVE TRADER (Bitvavo) – single source of truth
# Compatible with whatsapp_webhook calls (eur_amount/amount_eur + optional entry)
# =========================================

from __future__ import annotations

import os
import time
import json
import hmac
import hashlib
import requests
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

# =========================================
# ENV
# =========================================
BITVAVO_API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()

# Optional but recommended
BITVAVO_ACCESS_WINDOW = (os.getenv("BITVAVO_ACCESS_WINDOW") or "10000").strip()

if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
    raise RuntimeError("❌ BITVAVO_API_KEY / BITVAVO_API_SECRET ontbreken")

BASE_URL = "https://api.bitvavo.com/v2"

STATE_PATH = "/data/paper_state.json"      # legacy name; used as bot state file
LOG_PATH = "/data/paper_trades.csv"        # legacy name; used as trade log
APPROVALS_PATH = "/data/pending_approvals.json"  # if you still use file approvals

HTTP_TIMEOUT = 15


# =========================================
# HELPERS
# =========================================
def _sign(timestamp: str, method: str, path: str, body: str = "") -> str:
    msg = timestamp + method + path + body
    return hmac.new(
        BITVAVO_API_SECRET.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def _headers(method: str, path: str, body: str = "") -> Dict[str, str]:
    ts = str(int(time.time() * 1000))
    return {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": _sign(ts, method, path, body),
        "Bitvavo-Access-Timestamp": ts,
        # Access-Window is often required/expected by Bitvavo setups
        "Bitvavo-Access-Window": BITVAVO_ACCESS_WINDOW,
        "Content-Type": "application/json",
    }


def ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def _to_market(symbol: str) -> str:
    # Your bot uses USDT symbols. For Bitvavo we map to EUR market.
    # BTCUSDT -> BTC-EUR
    if symbol.endswith("USDT"):
        return symbol.replace("USDT", "-EUR")
    if "-" in symbol:
        return symbol
    # fallback: assume already base asset
    return f"{symbol}-EUR"


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _extract_fill(res: Dict[str, Any]) -> Tuple[float, float]:
    """
    Try to derive (filled_qty, avg_price) from Bitvavo order response.
    Response fields can differ per orderType; we do best-effort.
    """
    filled_qty = _safe_float(res.get("filledAmount"), 0.0)

    # Common possibilities
    avg_price = _safe_float(res.get("averagePrice"), 0.0)
    if avg_price <= 0:
        avg_price = _safe_float(res.get("avgPrice"), 0.0)
    if avg_price <= 0:
        avg_price = _safe_float(res.get("price"), 0.0)

    # If we have quote filled and qty, compute
    filled_quote = _safe_float(res.get("filledAmountQuote"), 0.0)
    if avg_price <= 0 and filled_qty > 0 and filled_quote > 0:
        avg_price = filled_quote / filled_qty

    return filled_qty, avg_price


# =========================================
# MARKET DATA
# =========================================
def get_price(symbol: str) -> float:
    r = requests.get(
        f"{BASE_URL}/ticker/price",
        params={"market": _to_market(symbol)},
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    data = r.json()
    # Sometimes Bitvavo returns {"market":"BTC-EUR","price":"..."}
    return float(data["price"])


# =========================================
# APPROVAL (optional file-consume)
# =========================================
def _consume_approval(prebuy_id: str) -> bool:
    """
    If you still use approvals in /data/pending_approvals.json, this will consume it.
    If you have fully migrated approvals to Postgres, this will simply return True
    when the file doesn't exist (so live trading won't be blocked).
    """
    if not prebuy_id:
        return False

    if not os.path.exists(APPROVALS_PATH):
        # IMPORTANT:
        # If you're on DB approvals now, don't block live orders on missing file.
        return True

    with open(APPROVALS_PATH, "r") as f:
        data = json.load(f)

    now = int(time.time())
    new = []
    ok = False

    for a in data:
        if a.get("id") == prebuy_id:
            if a.get("status") == "APPROVED" and int(a.get("expires_at", 0) or 0) > now:
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
def _log(symbol: str, side: str, price: float, qty: float, pnl: float = 0.0, meta: str = ""):
    ensure_dir(LOG_PATH)
    new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        if new:
            f.write("datetime,symbol,side,price,qty,pnl,meta\n")
        f.write(
            f"{datetime.utcnow().isoformat()},"
            f"{symbol},{side},{price},{qty},{pnl},{meta}\n"
        )


# =========================================
# LIVE BUY (compat signature)
# =========================================
def buy_eur(
    symbol: str,
    eur_amount: Optional[float] = None,
    entry: Optional[float] = None,            # accepted for compatibility; ignored for market orders
    stop_loss: Optional[float] = None,
    target: Optional[float] = None,
    prebuy_id: Optional[str] = None,
    # alias support (some callers use amount_eur)
    amount_eur: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Compatibility:
      - whatsapp_webhook may call: buy_eur(symbol=..., eur_amount=..., entry=..., stop_loss=..., target=..., prebuy_id=...)
      - older code may call:      buy_eur(symbol, amount_eur, stop_loss, target, prebuy_id)

    We accept BOTH eur_amount and amount_eur.
    """

    # Resolve amount (eur_amount OR amount_eur)
    amount = eur_amount if eur_amount is not None else amount_eur
    if amount is None:
        return {"ok": False, "reason": "AMOUNT_MISSING"}

    try:
        amount = float(amount)
    except Exception:
        return {"ok": False, "reason": "AMOUNT_INVALID"}

    if amount <= 0:
        return {"ok": False, "reason": "AMOUNT_INVALID"}

    if stop_loss is None or target is None or not prebuy_id:
        return {"ok": False, "reason": "MISSING_FIELDS"}

    stop_loss_f = float(stop_loss)
    target_f = float(target)

    if not _consume_approval(str(prebuy_id)):
        return {"ok": False, "reason": "APPROVAL_INVALID"}

    market = _to_market(symbol)

    body = json.dumps({
        "market": market,
        "side": "buy",
        "orderType": "market",
        # Spend quote currency (EUR)
        "amountQuote": f"{amount:.2f}",
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

    filled_qty, avg_price = _extract_fill(res)

    # Fallback: if API doesn't give avg price, query ticker
    if avg_price <= 0:
        avg_price = get_price(symbol)

    state = _load_state()
    state.setdefault("positions", {})
    state["positions"][symbol] = {
        "qty": filled_qty,
        "entry": avg_price,
        "stop_loss": stop_loss_f,
        "target": target_f,
        "opened_at": int(time.time()),
        "prebuy_id": str(prebuy_id),
        "market": market,
        "order_id": res.get("orderId") or res.get("id") or "",
    }
    _save_state(state)

    _log(symbol, "BUY", avg_price, filled_qty, meta=f"prebuy={prebuy_id} market={market}")

    return {
        "ok": True,
        "symbol": symbol,
        "market": market,
        "qty": filled_qty,
        "price": avg_price,
        "order_id": state["positions"][symbol].get("order_id", ""),
    }


# =========================================
# LIVE SELL
# =========================================
def sell(symbol: str, fraction: float = 1.0) -> Dict[str, Any]:
    state = _load_state()
    pos = state.get("positions", {}).get(symbol)

    if not pos:
        return {"ok": False, "reason": "NO_POSITION"}

    if fraction <= 0:
        return {"ok": False, "reason": "FRACTION_INVALID"}

    if fraction > 1.0:
        fraction = 1.0

    qty_total = float(pos.get("qty", 0.0) or 0.0)
    if qty_total <= 0:
        return {"ok": False, "reason": "NO_QTY"}

    qty = qty_total * fraction
    market = pos.get("market") or _to_market(symbol)

    body = json.dumps({
        "market": market,
        "side": "sell",
        "orderType": "market",
        # Sell base amount
        "amount": f"{qty:.8f}",
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

    filled_qty, exit_price = _extract_fill(res)

    # For sell, qty may be in response or we use our requested qty
    if filled_qty <= 0:
        filled_qty = qty

    if exit_price <= 0:
        exit_price = get_price(symbol)

    entry_price = float(pos.get("entry", 0.0) or 0.0)
    pnl = (exit_price - entry_price) * filled_qty

    if fraction >= 1.0:
        del state["positions"][symbol]
    else:
        pos["qty"] = max(0.0, qty_total - filled_qty)

    _save_state(state)
    _log(symbol, "SELL", exit_price, filled_qty, pnl=pnl, meta=f"market={market}")

    return {
        "ok": True,
        "symbol": symbol,
        "market": market,
        "price": exit_price,
        "qty": filled_qty,
        "pnl": pnl,
        "order_id": res.get("orderId") or res.get("id") or "",
    }
