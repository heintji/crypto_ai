# trading/live_trader.py
# ==========================================================
# ✅ Bitvavo LIVE trader — BUY/SELL + harde debug + veilige defaults
#
# Wat is hier “hetzelfde” als bij trade_monitor:
# ✅ super duidelijke debug (wat sturen we / wat krijgen we terug)
# ✅ zero-surprises defaults (clientOrderId standaard UIT)
# ✅ operatorId HARD required (anders meteen duidelijke error)
# ✅ market mapping (BINANCE sym -> BITVAVO market)
# ✅ extra checks zodat je niet “stil” faalt:
#    - check env keys
#    - check operatorId
#    - check amount > 0
#    - check market bestaat (optioneel validate step)
# ✅ SELL werkt netjes met balance->amount (fraction support)
#
# Extra: je kunt nu met ENV toggles testen zonder code te slopen:
# - BITVAVO_VALIDATE_MARKET=1  -> check of market bestaat via /v2/markets
# - BITVAVO_DEBUG=1            -> print request+response (zonder secrets)
# - BITVAVO_SEND_CLIENT_ORDER_ID=1 -> clientOrderId aan (sanitized)
# - BITVAVO_CLIENT_ORDER_PREFIX=BOT -> prefix voor id (sanitized)
# ==========================================================

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from typing import Any, Dict, Optional, Tuple, Union

import requests

# =========================
# ENV
# =========================
BITVAVO_API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()
BITVAVO_ACCESS_WINDOW = (os.getenv("BITVAVO_ACCESS_WINDOW") or "10000").strip()

# OperatorId is verplicht in jouw setup (errorCode 203/205 gerelateerd)
BITVAVO_OPERATOR_ID = (os.getenv("BITVAVO_OPERATOR_ID") or "").strip()

# clientOrderId standaard UIT (jij had errorCode 205 "invalid")
BITVAVO_SEND_CLIENT_ORDER_ID = (os.getenv("BITVAVO_SEND_CLIENT_ORDER_ID") or "0").strip().lower() in {
    "1", "true", "yes", "on"
}

# Debug/validate toggles
BITVAVO_DEBUG = (os.getenv("BITVAVO_DEBUG") or "0").strip().lower() in {"1", "true", "yes", "on"}
BITVAVO_VALIDATE_MARKET = (os.getenv("BITVAVO_VALIDATE_MARKET") or "0").strip().lower() in {"1", "true", "yes", "on"}

# Optioneel prefix voor clientOrderId
BITVAVO_CLIENT_ORDER_PREFIX = (os.getenv("BITVAVO_CLIENT_ORDER_PREFIX") or "").strip()

BASE_URL = "https://api.bitvavo.com"
TIMEOUT = 20

# Endpoints
API_PATH_ORDER = "/v2/order"
API_PATH_BALANCE = "/v2/balance"
API_PATH_MARKETS = "/v2/markets"


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


def _debug(msg: str) -> None:
    if BITVAVO_DEBUG:
        print(msg, flush=True)


def _as_market(symbol: str) -> str:
    """
    Binance style: 'JSTUSDT'
    Bitvavo:      'JST-EUR'  (we traden EUR)
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
    Bitvavo is streng. Veiligste: alleen A-Z0-9, max lengte.
    """
    s = (raw or "").strip().upper()
    s = re.sub(r"[^A-Z0-9]", "", s)
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
    Return: (status_code, json_or_text)
    """
    body_obj = body_obj or {}
    body = json.dumps(body_obj, separators=(",", ":")) if body_obj else ""
    ts = _ts_ms()
    sig = _sign(ts, method, path, body)
    headers = _headers(ts, sig)

    # debug zonder secrets
    if BITVAVO_DEBUG:
        _debug(f"--- BITVAVO REQUEST {method.upper()} {path} ---")
        _debug(f"timestamp={ts} window={BITVAVO_ACCESS_WINDOW}")
        _debug(f"body={body}")
        _debug(f"operatorId={(BITVAVO_OPERATOR_ID or '')[:60]}")
        _debug("-------------------------------------------")

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

        if BITVAVO_DEBUG:
            _debug(f"--- BITVAVO RESPONSE {status} {path} ---")
            _debug(str(data)[:1200])
            _debug("--------------------------------------")

        return status, data
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


# =========================
# Optional: market validate (handig om snel te zien of je -EUR market bestaat)
# =========================
_MARKETS_CACHE: Optional[set] = None
_MARKETS_CACHE_TS: float = 0.0
_MARKETS_TTL_SECONDS = 10 * 60  # 10 min


def _load_markets_cache() -> set:
    global _MARKETS_CACHE, _MARKETS_CACHE_TS
    now = time.time()
    if _MARKETS_CACHE is not None and (now - _MARKETS_CACHE_TS) < _MARKETS_TTL_SECONDS:
        return _MARKETS_CACHE

    status, data = _request("GET", API_PATH_MARKETS, None)
    s: set = set()
    if 200 <= status < 300 and isinstance(data, list):
        for row in data:
            try:
                m = str(row.get("market") or "").upper()
                if m:
                    s.add(m)
            except Exception:
                continue

    _MARKETS_CACHE = s
    _MARKETS_CACHE_TS = now
    return s


def _validate_market_exists(market: str) -> Optional[str]:
    if not BITVAVO_VALIDATE_MARKET:
        return None
    markets = _load_markets_cache()
    if market.upper() not in markets:
        return f"Market not found on Bitvavo: {market} (tip: bestaat {market.replace('-EUR','-USDT')} misschien wel?)"
    return None


# =========================
# Public API: BUY / SELL
# =========================
def buy_eur(symbol: str, amount_eur: float, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    MARKET BUY op Bitvavo met amountQuote (EUR).
    OperatorId verplicht.
    clientOrderId standaard UIT (kan aan via env).
    """
    err = _require_keys()
    if err:
        return {"ok": False, "status": 500, "error": err}

    try:
        amount = float(amount_eur)
    except Exception:
        return {"ok": False, "status": 400, "error": f"Invalid amount_eur: {amount_eur}"}
    if amount <= 0:
        return {"ok": False, "status": 400, "error": "amount_eur must be > 0"}

    market = _as_market(symbol)
    if not market:
        return {"ok": False, "status": 400, "error": "Invalid symbol/market"}

    m_err = _validate_market_exists(market)
    if m_err:
        return {"ok": False, "status": 400, "error": m_err, "market": market}

    body_obj: Dict[str, Any] = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        "amountQuote": str(amount),
        "operatorId": str(BITVAVO_OPERATOR_ID),
    }

    # ✅ clientOrderId alleen als je het expliciet aanzet via env
    if BITVAVO_SEND_CLIENT_ORDER_ID:
        raw = ""
        if meta:
            raw = str(meta.get("prebuy_id") or "")
        if BITVAVO_CLIENT_ORDER_PREFIX:
            raw = f"{BITVAVO_CLIENT_ORDER_PREFIX}-{raw}" if raw else BITVAVO_CLIENT_ORDER_PREFIX

        safe_id = _sanitize_client_order_id(raw, max_len=36)
        if safe_id:
            body_obj["clientOrderId"] = safe_id

    status, data = _request("POST", API_PATH_ORDER, body_obj)

    if 200 <= status < 300:
        return {"ok": True, "status": status, "data": data, "market": market, "sent": body_obj}

    return {"ok": False, "status": status or 500, "error": data, "market": market, "sent": body_obj}


def _get_balance_map() -> Dict[str, float]:
    """
    Leest Bitvavo balances en geeft {symbol: available_float}.
    Let op: hiervoor moet READ permissions aan staan.
    Als dit leeg terugkomt -> SELL kan niet bepalen hoeveel.
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
    fraction:
      - 1.0 = alles verkopen
      - 0.4 = 40% verkopen
    """
    err = _require_keys()
    if err:
        return {"ok": False, "status": 500, "error": err}

    market = _as_market(symbol)
    if not market:
        return {"ok": False, "status": 400, "error": "Invalid symbol/market"}

    m_err = _validate_market_exists(market)
    if m_err:
        return {"ok": False, "status": 400, "error": m_err, "market": market}

    try:
        frac = float(fraction)
    except Exception:
        return {"ok": False, "status": 400, "error": f"Invalid fraction: {fraction}"}
    if frac <= 0:
        return {"ok": False, "status": 400, "error": "fraction must be > 0"}

    base_asset = _market_to_base_asset(market)
    balances = _get_balance_map()

    if not balances:
        # Dit is belangrijk om te zien: zonder balance kan sell niet werken
        return {
            "ok": False,
            "status": 400,
            "error": "Could not read balances from /v2/balance (SELL cannot compute amount). Check API READ permissions.",
            "market": market,
        }

    available = float(balances.get(base_asset, 0.0))
    if available <= 0:
        return {
            "ok": False,
            "status": 400,
            "error": f"No available balance for {base_asset}",
            "market": market,
            "available": available,
        }

    amount = available * (1.0 if frac >= 1.0 else frac)

    # extra sanity: niet te klein
    if amount <= 0:
        return {"ok": False, "status": 400, "error": "Computed sell amount <= 0", "market": market, "available": available}

    body_obj: Dict[str, Any] = {
        "market": market,
        "side": "sell",
        "orderType": "market",
        "amount": str(amount),
        "operatorId": str(BITVAVO_OPERATOR_ID),
    }

    # clientOrderId ook hier alleen als env aan staat
    if BITVAVO_SEND_CLIENT_ORDER_ID:
        raw = ""
        if meta:
            raw = str(meta.get("prebuy_id") or "")
        if BITVAVO_CLIENT_ORDER_PREFIX:
            raw = f"{BITVAVO_CLIENT_ORDER_PREFIX}-{raw}" if raw else BITVAVO_CLIENT_ORDER_PREFIX

        safe_id = _sanitize_client_order_id(raw, max_len=36)
        if safe_id:
            body_obj["clientOrderId"] = safe_id

    status, data = _request("POST", API_PATH_ORDER, body_obj)

    if 200 <= status < 300:
        return {
            "ok": True,
            "status": status,
            "data": data,
            "market": market,
            "available": available,
            "amount": amount,
            "sent": body_obj,
        }

    return {
        "ok": False,
        "status": status or 500,
        "error": data,
        "market": market,
        "available": available,
        "amount": amount,
        "sent": body_obj,
    }
