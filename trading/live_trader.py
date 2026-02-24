# trading/live_trader.py
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional

import requests

BITVAVO_API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()
BITVAVO_ACCESS_WINDOW = (os.getenv("BITVAVO_ACCESS_WINDOW") or "10000").strip()

# Belangrijk: Bitvavo vereist dit nu soms bij orders (errorCode 203)
# Zet dit in Render env: BITVAVO_OPERATOR_ID=crypto_ai_bot
BITVAVO_OPERATOR_ID = (os.getenv("BITVAVO_OPERATOR_ID") or "crypto_ai_bot").strip()

BASE_URL = "https://api.bitvavo.com"
API_PATH_ORDER = "/v2/order"
TIMEOUT = 20


def _ts_ms() -> str:
    return str(int(time.time() * 1000))


def _sign(timestamp: str, method: str, path: str, body: str) -> str:
    # Bitvavo signing string: timestamp + method + path + body
    msg = f"{timestamp}{method.upper()}{path}{body}"
    return hmac.new(
        BITVAVO_API_SECRET.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _headers(timestamp: str, signature: str) -> Dict[str, str]:
    return {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": signature,
        "Bitvavo-Access-Timestamp": timestamp,
        "Bitvavo-Access-Window": BITVAVO_ACCESS_WINDOW,
        "Content-Type": "application/json",
    }


def _as_market(symbol: str) -> str:
    """
    Jij krijgt symbolen als 'ATUSDT' uit Binance-wereld.
    Op Bitvavo is het meestal 'AT-EUR' (of soms 'AT-USDT' als dat bestaat).
    Simpel: we traden EUR, dus map naar -EUR.
    """
    s = (symbol or "").strip().upper()
    if not s:
        return ""
    if "-" in s:
        return s
    base = s.replace("USDT", "").replace("EUR", "")
    return f"{base}-EUR"


def buy_eur(symbol: str, amount_eur: float, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Plaatst een MARKET BUY op Bitvavo met amountQuote (EUR).
    Vereist nu operatorId -> wordt meegestuurd.
    """
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        return {"ok": False, "status": 500, "error": "Missing BITVAVO_API_KEY/SECRET"}

    market = _as_market(symbol)
    if not market:
        return {"ok": False, "status": 400, "error": "Invalid symbol/market"}

    # Body: operatorId + order velden
    body_obj: Dict[str, Any] = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        "amountQuote": str(float(amount_eur)),
        "operatorId": BITVAVO_OPERATOR_ID,
    }

    # Handig voor logging/debug (geen invloed op Bitvavo)
    if meta:
        body_obj["clientOrderId"] = str(meta.get("prebuy_id") or "")[:40] or None
        # clientOrderId mag niet None in body, dus haal weg als leeg
        if not body_obj.get("clientOrderId"):
            body_obj.pop("clientOrderId", None)

    body = json.dumps(body_obj, separators=(",", ":"))

    ts = _ts_ms()
    sig = _sign(ts, "POST", API_PATH_ORDER, body)
    headers = _headers(ts, sig)

    try:
        r = requests.post(
            BASE_URL + API_PATH_ORDER,
            data=body,
            headers=headers,
            timeout=TIMEOUT,
        )
        status = r.status_code
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}

        if 200 <= status < 300:
            return {"ok": True, "status": status, "data": data, "market": market}

        return {"ok": False, "status": status, "error": data, "market": market}

    except Exception as e:
        return {"ok": False, "status": 500, "error": f"{type(e).__name__}: {e}", "market": market}
