# trading/live_trader.py
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests

# ==========================================================
# BITVAVO LIVE TRADER (PRODUCTION)
# - Correct signing: timestamp + METHOD + path + body
# - operatorId handling: Bitvavo expects an INT (not a string)
# - Safe, explicit request building + debug-friendly returns
# ==========================================================

# -----------------------------
# ENV
# -----------------------------
BITVAVO_API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()
BITVAVO_ACCESS_WINDOW = (os.getenv("BITVAVO_ACCESS_WINDOW") or "10000").strip()

# IMPORTANT:
# Render env should be a NUMBER (e.g. 1001). Bitvavo expects int.
# Example: BITVAVO_OPERATOR_ID=1001
BITVAVO_OPERATOR_ID_RAW = (os.getenv("BITVAVO_OPERATOR_ID") or "").strip()

# Optional:
# Force quote currency. You said you trade EUR on Bitvavo.
BITVAVO_QUOTE = (os.getenv("BITVAVO_QUOTE") or "EUR").strip().upper()

BASE_URL = (os.getenv("BITVAVO_BASE_URL") or "https://api.bitvavo.com").strip()
API_PATH_ORDER = "/v2/order"
TIMEOUT = int(os.getenv("BITVAVO_HTTP_TIMEOUT") or "20")


# ==========================================================
# Helpers
# ==========================================================
def _ts_ms() -> str:
    return str(int(time.time() * 1000))


def _sign(timestamp: str, method: str, path: str, body: str) -> str:
    """
    Bitvavo signing string: timestamp + METHOD + path + body
    where body is the EXACT JSON string sent in the request.
    """
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


def _operator_id() -> Optional[int]:
    """
    Bitvavo expects operatorId as an integer (int64).
    If env is missing or not numeric, return None and omit the field.
    """
    v = BITVAVO_OPERATOR_ID_RAW
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _as_market(symbol: str, quote: str = "EUR") -> str:
    """
    Convert Binance-style symbols like 'ATUSDT' or 'BTCUSDT' to Bitvavo market like 'AT-EUR' or 'BTC-EUR'.
    If input already contains '-', keep it.
    """
    s = (symbol or "").strip().upper()
    if not s:
        return ""

    if "-" in s:
        return s

    # Strip known quote suffixes from Binance-world
    base = s
    for q in ("USDT", "EUR", "USD"):
        if base.endswith(q):
            base = base[: -len(q)]
            break

    base = base.strip()
    if not base:
        return ""

    return f"{base}-{quote}"


def _json_body(obj: Dict[str, Any]) -> str:
    """
    Produce a compact JSON body string.
    separators remove spaces to stabilize signing.
    """
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _parse_json_response(r: requests.Response) -> Dict[str, Any]:
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}


def _require_keys() -> Optional[Dict[str, Any]]:
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        return {"ok": False, "status": 500, "error": "Missing BITVAVO_API_KEY/SECRET"}
    return None


# ==========================================================
# Core order functions
# ==========================================================
def buy_eur(symbol: str, amount_eur: float, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Place a MARKET BUY on Bitvavo using amountQuote in EUR.
    Includes operatorId as an INT if configured.
    """
    missing = _require_keys()
    if missing:
        return missing

    market = _as_market(symbol, quote=BITVAVO_QUOTE)
    if not market:
        return {"ok": False, "status": 400, "error": "Invalid symbol/market", "market": market}

    # Build body
    body_obj: Dict[str, Any] = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        "amountQuote": str(float(amount_eur)),
    }

    # operatorId as INT (Bitvavo expects numeric)
    op_id = _operator_id()
    if op_id is not None:
        body_obj["operatorId"] = op_id

    # Optional clientOrderId (useful for tracing; must not be None/empty)
    if meta:
        cid = str(meta.get("prebuy_id") or "")[:40].strip()
        if cid:
            body_obj["clientOrderId"] = cid

    body = _json_body(body_obj)

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
        data = _parse_json_response(r)

        if 200 <= status < 300:
            return {
                "ok": True,
                "status": status,
                "data": data,
                "market": market,
                "sent": {
                    "amountQuote": body_obj.get("amountQuote"),
                    "operatorId": body_obj.get("operatorId"),
                    "clientOrderId": body_obj.get("clientOrderId"),
                },
            }

        return {
            "ok": False,
            "status": status,
            "error": data,
            "market": market,
            "sent": {
                "amountQuote": body_obj.get("amountQuote"),
                "operatorId": body_obj.get("operatorId"),
                "clientOrderId": body_obj.get("clientOrderId"),
            },
        }

    except Exception as e:
        return {
            "ok": False,
            "status": 500,
            "error": f"{type(e).__name__}: {e}",
            "market": market,
            "sent": {"amountQuote": str(float(amount_eur)), "operatorId": _operator_id()},
        }


# Optional extension (if you later add sells)
def sell_base(symbol: str, amount_base: float, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Place a MARKET SELL on Bitvavo using amount in base currency.
    Not used now, but kept for completeness.
    """
    missing = _require_keys()
    if missing:
        return missing

    market = _as_market(symbol, quote=BITVAVO_QUOTE)
    if not market:
        return {"ok": False, "status": 400, "error": "Invalid symbol/market", "market": market}

    body_obj: Dict[str, Any] = {
        "market": market,
        "side": "sell",
        "orderType": "market",
        "amount": str(float(amount_base)),
    }

    op_id = _operator_id()
    if op_id is not None:
        body_obj["operatorId"] = op_id

    if meta:
        cid = str(meta.get("prebuy_id") or "")[:40].strip()
        if cid:
            body_obj["clientOrderId"] = cid

    body = _json_body(body_obj)

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
        data = _parse_json_response(r)

        if 200 <= status < 300:
            return {"ok": True, "status": status, "data": data, "market": market, "sent": {"amount": body_obj.get("amount")}}

        return {"ok": False, "status": status, "error": data, "market": market, "sent": {"amount": body_obj.get("amount")}}

    except Exception as e:
        return {"ok": False, "status": 500, "error": f"{type(e).__name__}: {e}", "market": market}


# ==========================================================
# Quick self-test helper (optional)
# ==========================================================
def debug_operator_id() -> Dict[str, Any]:
    """
    Handy to verify env parsing.
    """
    return {
        "raw": BITVAVO_OPERATOR_ID_RAW,
        "parsed": _operator_id(),
        "is_int": isinstance(_operator_id(), int) if _operator_id() is not None else False,
    }
