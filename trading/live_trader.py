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

# Bitvavo vereist dit soms bij orders
BITVAVO_OPERATOR_ID = (os.getenv("BITVAVO_OPERATOR_ID") or "").strip()

BASE_URL = "https://api.bitvavo.com"
API_PATH_ORDER = "/v2/order"
TIMEOUT = 20


def _ts_ms() -> str:
    return str(int(time.time() * 1000))


def _sign(timestamp: str, method: str, path: str, body: str) -> str:
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
    Jij krijgt symbolen als 'JSTUSDT' uit Binance-wereld.
    Op Bitvavo willen we EUR-markten: 'JST-EUR'
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

    Belangrijk:
    - operatorId moet geldig zijn
    - clientOrderId geeft bij jou errorCode 205 -> dus we sturen die NIET mee (meest stabiel)
    """
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        return {"ok": False, "status": 500, "error": "Missing BITVAVO_API_KEY/SECRET"}

    if not BITVAVO_OPERATOR_ID:
        return {"ok": False, "status": 500, "error": "Missing BITVAVO_OPERATOR_ID (Render env)"}

    market = _as_market(symbol)
    if not market:
        return {"ok": False, "status": 400, "error": "Invalid symbol/market"}

    try:
        amount_eur = float(amount_eur)
    except Exception:
        return {"ok": False, "status": 400, "error": "Invalid amount_eur"}

    if amount_eur <= 0:
        return {"ok": False, "status": 400, "error": "amount_eur must be > 0"}

    body_obj: Dict[str, Any] = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        "amountQuote": str(amount_eur),
        "operatorId": str(BITVAVO_OPERATOR_ID),
    }

    # LET OP: GEEN clientOrderId sturen -> voorkomt errorCode 205

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
            return {"ok": True, "status": status, "data": data, "market": market, "sent": body_obj}

        return {"ok": False, "status": status, "error": data, "market": market, "sent": body_obj}

    except Exception as e:
        return {"ok": False, "status": 500, "error": f"{type(e).__name__}: {e}", "market": market, "sent": body_obj}


# Optional (later): sell() kan pas goed als je ook positie/amount opslaat uit de buy response.
def sell(symbol: str, fraction: float = 1.0, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "ok": False,
        "status": 501,
        "error": "sell() not implemented safely yet (needs position amount from live fills).",
        "symbol": symbol,
        "fraction": fraction,
    }
