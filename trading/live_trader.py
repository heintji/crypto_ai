# trading/live_trader.py
from __future__ import annotations

import os
import time
import hmac
import json
import hashlib
from typing import Any, Dict, Optional

import requests

# =========================
# ENV
# =========================
BITVAVO_BASE_URL = (os.getenv("BITVAVO_BASE_URL") or "https://api.bitvavo.com").rstrip("/")
BITVAVO_API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()
BITVAVO_OPERATOR_ID = (os.getenv("BITVAVO_OPERATOR_ID") or "crypto_ai_bot").strip()

BITVAVO_ACCESS_WINDOW = (os.getenv("BITVAVO_ACCESS_WINDOW") or "10000").strip()
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT") or "20")

# markets cache
_MARKETS_CACHE: Dict[str, Any] = {"ts": 0.0, "set": set()}
_MARKETS_TTL_SECONDS = 60 * 30  # 30 min


# =========================
# Helpers
# =========================
def _canonical_json(obj: Any) -> str:
    """
    JSON zonder spaties voor stabiele Bitvavo-signature.
    """
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _sign(timestamp_ms: str, method: str, path: str, body: str) -> str:
    """
    Bitvavo signing string = timestamp + method + path + body
    """
    msg = f"{timestamp_ms}{method.upper()}{path}{body}".encode("utf-8")
    secret = BITVAVO_API_SECRET.encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def _headers(timestamp_ms: str, signature: str) -> Dict[str, str]:
    return {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": signature,
        "Bitvavo-Access-Timestamp": timestamp_ms,
        "Bitvavo-Access-Window": BITVAVO_ACCESS_WINDOW,
        "Content-Type": "application/json",
    }


def _request_signed(method: str, path: str, body_obj: Optional[dict] = None) -> Dict[str, Any]:
    """
    Signed request naar Bitvavo.
    """
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY/SECRET ontbreken in env.")

    body = _canonical_json(body_obj) if body_obj else ""
    ts = str(int(time.time() * 1000))
    sig = _sign(ts, method, path, body)

    url = f"{BITVAVO_BASE_URL}{path}"
    response = requests.request(
        method=method.upper(),
        url=url,
        headers=_headers(ts, sig),
        data=body if body else None,
        timeout=HTTP_TIMEOUT,
    )

    try:
        data = response.json()
    except Exception:
        data = {
            "ok": False,
            "status": response.status_code,
            "text": response.text,
        }

    return {
        "ok": response.ok,
        "status": response.status_code,
        "data": data,
    }


# =========================
# Public markets (no signing)
# =========================
def _get_tradable_markets() -> set[str]:
    """
    Haalt actieve/tradable Bitvavo markets op en cachet ze 30 minuten.
    """
    now = time.time()
    if _MARKETS_CACHE["set"] and (now - _MARKETS_CACHE["ts"] < _MARKETS_TTL_SECONDS):
        return _MARKETS_CACHE["set"]

    url = f"{BITVAVO_BASE_URL}/v2/markets"
    response = requests.get(url, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    items = response.json()

    tradable = set()
    for market_info in items:
        market = (market_info.get("market") or "").strip()
        status = (market_info.get("status") or "").strip().lower()
        if market and status == "trading":
            tradable.add(market)

    _MARKETS_CACHE["ts"] = now
    _MARKETS_CACHE["set"] = tradable
    return tradable


def symbol_usdt_to_bitvavo_market(symbol_usdt: str) -> str:
    """
    Zet Binance-style symbool om naar Bitvavo market.

    Voorbeeld:
    ETHFIUSDT -> ETHFI-EUR
    """
    symbol = (symbol_usdt or "").upper().strip()
    if not symbol.endswith("USDT"):
        raise ValueError(f"symbol_usdt moet eindigen op USDT, ontvangen: {symbol_usdt}")

    base = symbol[:-4].strip()  # verwijdert alleen de eindigende USDT
    if not base:
        raise ValueError(f"Ongeldig symbol_usdt ontvangen: {symbol_usdt}")

    return f"{base}-EUR"


def ensure_market_tradable(symbol_usdt: str) -> str:
    """
    Controleert of de coin ook echt tradable is op Bitvavo.
    """
    market = symbol_usdt_to_bitvavo_market(symbol_usdt)
    tradable_markets = _get_tradable_markets()

    if market not in tradable_markets:
        raise ValueError(f"UNSUPPORTED_MARKET: {market} (niet tradable/geen listing op Bitvavo).")

    return market


# =========================
# Trading
# =========================
def place_market_buy_eur(symbol_usdt: str, amount_eur: float) -> Dict[str, Any]:
    """
    Market BUY met EUR-bedrag via amountQuote.

    Voorbeeld:
    place_market_buy_eur("ETHFIUSDT", 5)
    """
    if amount_eur <= 0:
        raise ValueError("amount_eur moet > 0 zijn")

    market = ensure_market_tradable(symbol_usdt)

    body = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        "amountQuote": str(float(amount_eur)),
        "operatorId": BITVAVO_OPERATOR_ID,
    }

    result = _request_signed("POST", "/v2/order", body)

    if not result["ok"]:
        return {
            "ok": False,
            "status": result.get("status"),
            "error": result.get("data"),
            "market": market,
            "sent": body,
        }

    return {
        "ok": True,
        "status": result.get("status"),
        "order": result.get("data"),
        "market": market,
    }


def place_market_sell_base(market: str, amount_base: float) -> Dict[str, Any]:
    """
    Market SELL met hoeveelheid van de base asset.

    Voorbeeld:
    place_market_sell_base("BTC-EUR", 0.001)
    """
    if amount_base <= 0:
        raise ValueError("amount_base moet > 0 zijn")

    body = {
        "market": market,
        "side": "sell",
        "orderType": "market",
        "amount": str(float(amount_base)),
        "operatorId": BITVAVO_OPERATOR_ID,
    }

    result = _request_signed("POST", "/v2/order", body)

    if not result["ok"]:
        return {
            "ok": False,
            "status": result.get("status"),
            "error": result.get("data"),
            "market": market,
            "sent": body,
        }

    return {
        "ok": True,
        "status": result.get("status"),
        "order": result.get("data"),
        "market": market,
    }


# =========================
# Compatibility aliases
# =========================
def buy_eur(symbol_usdt: str, amount_eur: float) -> Dict[str, Any]:
    """
    Compatibiliteitsfunctie voor code die buy_eur(...) verwacht.
    Stuurt intern door naar place_market_buy_eur(...).
    """
    return place_market_buy_eur(symbol_usdt, amount_eur)


def sell_base(market: str, amount_base: float) -> Dict[str, Any]:
    """
    Compatibiliteitsfunctie voor code die sell_base(...) verwacht.
    Stuurt intern door naar place_market_sell_base(...).
    """
    return place_market_sell_base(market, amount_base)


# =========================
# Debug helpers
# =========================
def get_tradable_markets_cached() -> Dict[str, Any]:
    """
    Handig voor debug / filtering.
    """
    markets = _get_tradable_markets()
    return {
        "count": len(markets),
        "sample": sorted(list(markets))[:20],
    }
