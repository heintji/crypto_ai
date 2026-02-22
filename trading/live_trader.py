# trading/live_trader.py
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
BITVAVO_ACCESS_WINDOW = (os.getenv("BITVAVO_ACCESS_WINDOW") or "10000").strip()  # ms
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT") or "20")

if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
    raise RuntimeError("❌ BITVAVO_API_KEY / BITVAVO_API_SECRET ontbreken")

# BELANGRIJK: base ZONDER /v2
BASE_URL = "https://api.bitvavo.com"

# =========================
# SIGNING
# =========================
def _sign(timestamp_ms: str, method: str, path: str, body: str = "") -> str:
    # Bitvavo verwacht: timestamp + method + path + body
    msg = f"{timestamp_ms}{method}{path}{body}"
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
        "Bitvavo-Access-Window": BITVAVO_ACCESS_WINDOW,
        "Content-Type": "application/json",
    }


def _req(method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = BASE_URL + path

    body = ""
    data = None
    if payload is not None:
        # Exacte JSON string (zonder random spaces/formatting issues)
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        data = body

    headers = _headers(method, path, body)

    r = requests.request(method, url, headers=headers, data=data, timeout=HTTP_TIMEOUT)

    # Probeer altijd JSON terug te geven (ook bij errors)
    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text}

    if r.status_code >= 400:
        return {"ok": False, "status": r.status_code, "error": j}

    return {"ok": True, "data": j}


# =========================
# HELPERS
# =========================
def _to_market(symbol: str) -> str:
    # Jij stuurt symbol als "BTCUSDT" etc.
    # Bitvavo wil "BTC-EUR"
    coin = symbol.replace("USDT", "").replace("EUR", "")
    return f"{coin}-EUR"


# =========================
# LIVE BUY/SELL
# =========================
def buy_eur(symbol: str, amount_eur: float, prebuy_id: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    """
    Market BUY met amountQuote in EUR.
    """
    market = _to_market(symbol)
    path = "/v2/order"

    payload = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        "amountQuote": f"{float(amount_eur):.2f}",
    }

    res = _req("POST", path, payload)
    if not res.get("ok"):
        return res

    data = res.get("data", {}) or {}

    # Bitvavo kan velden verschillend teruggeven per account/orderType
    filled_amount = float(data.get("filledAmount") or 0.0)
    price = float(data.get("price") or 0.0)

    return {
        "ok": True,
        "symbol": symbol,
        "market": market,
        "qty": filled_amount,
        "price": price,
        "prebuy_id": prebuy_id,
        "raw": data,
    }


def sell(symbol: str, fraction: float = 1.0, qty_total: Optional[float] = None, **kwargs) -> Dict[str, Any]:
    """
    Market SELL.
    Als qty_total wordt meegegeven, verkopen we qty_total * fraction.
    Anders MOET jouw monitor/paper_state de qty bepalen (daarom is qty_total optioneel).
    """
    if qty_total is None:
        return {"ok": False, "reason": "QTY_TOTAL_REQUIRED_FOR_LIVE_SELL"}

    market = _to_market(symbol)
    qty = float(qty_total) * float(fraction)

    path = "/v2/order"
    payload = {
        "market": market,
        "side": "sell",
        "orderType": "market",
        "amount": f"{qty:.8f}",
    }

    res = _req("POST", path, payload)
    if not res.get("ok"):
        return res

    data = res.get("data", {}) or {}
    price = float(data.get("price") or 0.0)

    return {
        "ok": True,
        "symbol": symbol,
        "market": market,
        "qty": qty,
        "price": price,
        "raw": data,
    }
