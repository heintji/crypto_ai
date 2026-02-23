# trading/live_trader.py
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional, Tuple

import requests

# ==========================================================
# BITVAVO ENV
# ==========================================================
BITVAVO_API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()

# Bitvavo vereist in jouw account blijkbaar een operatorId bij orders.
# Zet deze in Render Environment:
# BITVAVO_OPERATOR_ID=...
BITVAVO_OPERATOR_ID = (os.getenv("BITVAVO_OPERATOR_ID") or "").strip()

# Access window (ms). 10000 is prima.
BITVAVO_ACCESS_WINDOW = (os.getenv("BITVAVO_ACCESS_WINDOW") or "10000").strip()

# Base URL (LET OP: base eindigt op /v2)
BASE_URL = "https://api.bitvavo.com/v2"

# Signing path moet beginnen met /v2/...
# Voor POST order is endpoint in base: /order
# maar in signature string moet EXACT "/v2/order" staan.
SIGN_PREFIX = "/v2"


# ==========================================================
# UTIL
# ==========================================================
def _ts_ms() -> str:
    return str(int(time.time() * 1000))


def _json_body(payload: Dict[str, Any]) -> str:
    """
    Body moet exact hetzelfde zijn als wat je verstuurt.
    Daarom: compacte JSON, geen spaties.
    """
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _sign(timestamp_ms: str, method: str, sign_path: str, body: str) -> str:
    """
    Bitvavo signature = HMAC_SHA256(secret, timestamp + method + path + body)
    """
    msg = f"{timestamp_ms}{method.upper()}{sign_path}{body}"
    mac = hmac.new(BITVAVO_API_SECRET.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()


def _headers(timestamp_ms: str, signature: str) -> Dict[str, str]:
    return {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": signature,
        "Bitvavo-Access-Timestamp": timestamp_ms,
        "Bitvavo-Access-Window": BITVAVO_ACCESS_WINDOW,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _require_keys() -> Optional[str]:
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        return "BITVAVO_API_KEY / BITVAVO_API_SECRET ontbreekt."
    return None


def _map_symbol_to_market(symbol: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Jouw bot gebruikt vaak BINANCE symbols zoals:
    - INJUSDT, ATUSDT, ENSOUSDT, ...
    Bitvavo gebruikt markets zoals:
    - INJ-EUR, AT-EUR, ENSO-EUR, ...

    We mappen:
    <COIN>USDT  -> <COIN>-EUR
    <COIN>EUR   -> <COIN>-EUR (als iemand al EUR stuurt)
    Anders: fout terug.

    (Bitvavo heeft niet elke coin. Als market niet bestaat -> Bitvavo geeft error.)
    """
    s = (symbol or "").strip().upper()

    if not s:
        return None, "symbol ontbreekt"

    # Als iemand al market geeft (met -)
    if "-" in s:
        return s, None

    # USDT suffix -> EUR market
    if s.endswith("USDT"):
        coin = s[:-4]
        if not coin:
            return None, f"kan coin niet bepalen uit {s}"
        return f"{coin}-EUR", None

    # EUR suffix als string zonder dash (bv BTCEUR) -> BTC-EUR
    if s.endswith("EUR"):
        coin = s[:-3]
        if not coin:
            return None, f"kan coin niet bepalen uit {s}"
        return f"{coin}-EUR", None

    return None, f"symbol formaat niet herkend voor Bitvavo market: {s}"


# ==========================================================
# CORE REQUEST
# ==========================================================
def _request(method: str, api_path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    api_path = "/order" of "/balance" etc (zonder /v2)
    sign_path = "/v2" + api_path
    """
    missing = _require_keys()
    if missing:
        return {"ok": False, "status": 0, "error": missing}

    method_u = method.upper()
    api_path = api_path if api_path.startswith("/") else f"/{api_path}"
    sign_path = f"{SIGN_PREFIX}{api_path}"

    body = ""
    if payload is not None:
        body = _json_body(payload)

    ts = _ts_ms()
    sig = _sign(ts, method_u, sign_path, body)
    headers = _headers(ts, sig)

    url = f"{BASE_URL}{api_path}"

    try:
        if method_u == "GET":
            r = requests.get(url, headers=headers, timeout=20)
        else:
            r = requests.request(method_u, url, headers=headers, data=body.encode("utf-8"), timeout=20)

        # Probeer JSON te lezen
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}

        # Bitvavo errors zijn vaak dicts met errorCode/error
        if r.status_code >= 400:
            return {"ok": False, "status": r.status_code, "error": data}

        return {"ok": True, "status": r.status_code, "data": data}

    except Exception as e:
        return {"ok": False, "status": 0, "error": f"{type(e).__name__}: {e}"}


# ==========================================================
# PUBLIC API (wordt door whatsapp_webhook gebruikt)
# ==========================================================
def buy_eur(symbol: str, amount_eur: float, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Plaatst een MARKET BUY op Bitvavo voor een bedrag in EUR.

    Bitvavo market order met quote bedrag:
      {
        "market": "INJ-EUR",
        "side": "buy",
        "orderType": "market",
        "amountQuote": "5"
        "operatorId": "..."
      }

    Return:
      {"ok": True, "status": 200, "data": {...}} of {"ok": False, "status": 400/403, "error": {...}}
    """
    market, err = _map_symbol_to_market(symbol)
    if err:
        return {"ok": False, "status": 0, "error": err, "symbol": symbol}

    if not BITVAVO_OPERATOR_ID:
        # Dit is exact jouw huidige errorCode 203.
        return {
            "ok": False,
            "status": 0,
            "error": "BITVAVO_OPERATOR_ID ontbreekt. Bitvavo vereist operatorId bij orders.",
            "market": market,
            "prebuy_id": (meta or {}).get("prebuy_id") if meta else None,
        }

    payload: Dict[str, Any] = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        # quote bedrag = EUR bedrag
        "amountQuote": str(float(amount_eur)),
        "operatorId": BITVAVO_OPERATOR_ID,
    }

    res = _request("POST", "/order", payload=payload)

    # voeg context toe (handig in logs/whatsapp)
    res["market"] = market
    if meta:
        res["prebuy_id"] = meta.get("prebuy_id")
    return res


def sell_market(symbol: str, amount_base: float, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    MARKET SELL van een hoeveelheid base coin (bv 0.12 INJ).
    """
    market, err = _map_symbol_to_market(symbol)
    if err:
        return {"ok": False, "status": 0, "error": err, "symbol": symbol}

    if not BITVAVO_OPERATOR_ID:
        return {
            "ok": False,
            "status": 0,
            "error": "BITVAVO_OPERATOR_ID ontbreekt. Bitvavo vereist operatorId bij orders.",
            "market": market,
        }

    payload: Dict[str, Any] = {
        "market": market,
        "side": "sell",
        "orderType": "market",
        "amount": str(float(amount_base)),
        "operatorId": BITVAVO_OPERATOR_ID,
    }

    res = _request("POST", "/order", payload=payload)
    res["market"] = market
    if meta:
        res["prebuy_id"] = meta.get("prebuy_id")
    return res


def ping_auth() -> Dict[str, Any]:
    """
    Snelle check of signing/keys goed zijn.
    Balance endpoint is vaak /balance.
    Als dit 403 signature invalid geeft -> signing nog fout / key fout.
    """
    return _request("GET", "/balance")
