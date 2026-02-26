# trading/live_trader.py
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

import requests

# =========================
# ENV
# =========================
BITVAVO_API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()
BITVAVO_ACCESS_WINDOW = (os.getenv("BITVAVO_ACCESS_WINDOW") or "10000").strip()

# OperatorId is verplicht in jouw setup (je ziet errorCode 203/205 rondom operator/clientOrderId).
BITVAVO_OPERATOR_ID = (os.getenv("BITVAVO_OPERATOR_ID") or "").strip()

# Heel belangrijk: Bitvavo faalt bij jou op clientOrderId parameter is invalid (errorCode 205)
# Daarom zetten we clientOrderId standaard UIT.
BITVAVO_SEND_CLIENT_ORDER_ID = (os.getenv("BITVAVO_SEND_CLIENT_ORDER_ID") or "0").strip() in {"1", "true", "yes", "on"}

BASE_URL = "https://api.bitvavo.com"
TIMEOUT = 20

# Endpoints
API_PATH_ORDER = "/v2/order"
API_PATH_BALANCE = "/v2/balance"


# =========================
# Helpers
# =========================
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
    Jij gebruikt symbols zoals 'JSTUSDT' (Binance stijl).
    Bitvavo werkt met 'JST-EUR' (als jij in EUR trade).
    """
    s = (symbol or "").strip().upper()
    if not s:
        return ""
    if "-" in s:
        return s
    base = s.replace("USDT", "").replace("EUR", "")
    return f"{base}-EUR"


def _market_to_base_asset(market: str) -> str:
    # "JST-EUR" -> "JST"
    m = (market or "").strip().upper()
    if "-" in m:
        return m.split("-", 1)[0]
    return m


def _sanitize_client_order_id(raw: str, max_len: int = 36) -> str:
    """
    Bitvavo accepteert bij sommige accounts alleen bepaalde chars.
    Veiligste: alleen A-Z0-9 en max lengte.
    """
    s = (raw or "").strip().upper()
    s = re.sub(r"[^A-Z0-9]", "", s)  # alleen letters/cijfers
    return s[:max_len]


def _require_keys() -> Optional[str]:
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        return "Missing BITVAVO_API_KEY/SECRET"
    if not BITVAVO_OPERATOR_ID:
        return "Missing BITVAVO_OPERATOR_ID"
    return None


def _request(method: str, path: str, body_obj: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
    """
    Signed request naar Bitvavo.
    Geeft terug: (status_code, json_or_text)
    """
    body_obj = body_obj or {}
    body = json.dumps(body_obj, separators=(",", ":")) if body_obj else ""
    ts = _ts_ms()
    sig = _sign(ts, method, path, body)
    headers = _headers(ts, sig)

    url = BASE_URL + path
    try:
        if method.upper() == "GET":
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
        elif method.upper() == "POST":
            r = requests.post(url, data=body, headers=headers, timeout=TIMEOUT)
        else:
            r = requests.request(method.upper(), url, data=body, headers=headers, timeout=TIMEOUT)

        status = r.status_code
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}
        return status, data
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


# =========================
# Public API: BUY / SELL
# =========================
def buy_eur(symbol: str, amount_eur: float, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    MARKET BUY op Bitvavo met amountQuote (EUR).
    OperatorId wordt meegestuurd.
    clientOrderId is standaard UIT omdat jij errorCode 205 kreeg.
    """
    err = _require_keys()
    if err:
        return {"ok": False, "status": 500, "error": err}

    market = _as_market(symbol)
    if not market:
        return {"ok": False, "status": 400, "error": "Invalid symbol/market"}

    body_obj: Dict[str, Any] = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        "amountQuote": str(float(amount_eur)),
        "operatorId": str(BITVAVO_OPERATOR_ID),
    }

    # ✅ clientOrderId alleen als je het expliciet aanzet via env
    if BITVAVO_SEND_CLIENT_ORDER_ID and meta:
        raw_id = str(meta.get("prebuy_id") or "")
        safe_id = _sanitize_client_order_id(raw_id, max_len=36)
        if safe_id:
            body_obj["clientOrderId"] = safe_id

    status, data = _request("POST", API_PATH_ORDER, body_obj)

    if 200 <= status < 300:
        return {"ok": True, "status": status, "data": data, "market": market, "sent": body_obj}

    return {"ok": False, "status": status or 500, "error": data, "market": market, "sent": body_obj}


def _get_balance_map() -> Dict[str, float]:
    """
    Leest je Bitvavo balances en geeft {symbol: available_float}.
    Vereist READ perms op API key.
    """
    status, data = _request("GET", API_PATH_BALANCE, None)
    if not (200 <= status < 300) or not isinstance(data, list):
        return {}

    out: Dict[str, float] = {}
    for row in data:
        try:
            sym = str(row.get("symbol") or "").upper()
            avail = float(row.get("available") or 0)
            out[sym] = avail
        except Exception:
            continue
    return out


def sell(symbol: str, fraction: float = 1.0, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    MARKET SELL op Bitvavo.
    - fraction = 1.0 = alles verkopen
    - fraction = 0.4 = 40% van je beschikbare asset verkopen

    Werkt door:
    1) market bepalen (JST-EUR)
    2) base asset bepalen (JST)
    3) balance lezen
    4) amount (base) = available * fraction
    5) MARKET SELL plaatsen
    """
    err = _require_keys()
    if err:
        return {"ok": False, "status": 500, "error": err}

    market = _as_market(symbol)
    if not market:
        return {"ok": False, "status": 400, "error": "Invalid symbol/market"}

    frac = float(fraction)
    if frac <= 0:
        return {"ok": False, "status": 400, "error": "fraction must be > 0"}

    base_asset = _market_to_base_asset(market)
    balances = _get_balance_map()
    available = float(balances.get(base_asset, 0.0))
    if available <= 0:
        return {"ok": False, "status": 400, "error": f"No available balance for {base_asset}", "market": market, "available": available}

    amount = available * (1.0 if frac >= 1.0 else frac)

    # Bitvavo wil meestal string amount
    body_obj: Dict[str, Any] = {
        "market": market,
        "side": "sell",
        "orderType": "market",
        "amount": str(amount),
        "operatorId": str(BITVAVO_OPERATOR_ID),
    }

    # clientOrderId voor SELL: ook standaard uit
    if BITVAVO_SEND_CLIENT_ORDER_ID and meta:
        raw_id = str(meta.get("prebuy_id") or "")
        safe_id = _sanitize_client_order_id(raw_id, max_len=36)
        if safe_id:
            body_obj["clientOrderId"] = safe_id

    status, data = _request("POST", API_PATH_ORDER, body_obj)

    if 200 <= status < 300:
        return {"ok": True, "status": status, "data": data, "market": market, "sent": body_obj}

    return {"ok": False, "status": status or 500, "error": data, "market": market, "sent": body_obj}
