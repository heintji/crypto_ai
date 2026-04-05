"""
market_sentiment.py  Fase 2/4 externe data
Haalt Fear & Greed Index + CryptoPanic nieuws op.
Gratis, geen API key nodig voor Fear & Greed.
"""
from __future__ import annotations
import os, requests, json
from datetime import datetime, timezone
from functools import lru_cache

CRYPTOPANIC_KEY = os.getenv("CRYPTOPANIC_API_KEY", "")
_CACHE: dict = {}
_CACHE_TS: dict = {}
CACHE_TTL = 900  # 15 min

def _cache_get(key: str):
    if key in _CACHE and (datetime.now().timestamp() - _CACHE_TS.get(key, 0)) < CACHE_TTL:
        return _CACHE[key]
    return None

def _cache_set(key: str, val):
    _CACHE[key] = val
    _CACHE_TS[key] = datetime.now().timestamp()

def get_fear_greed() -> dict:
    """Fear & Greed Index (0=extreme fear, 100=extreme greed). Gratis API."""
    cached = _cache_get("fng")
    if cached: return cached
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        data = r.json()["data"][0]
        result = {
            "value": int(data["value"]),
            "label": data["value_classification"],
            "ok": True,
        }
        _cache_set("fng", result)
        return result
    except Exception as e:
        return {"value": 50, "label": "Neutral", "ok": False, "error": str(e)}

def get_cryptopanic_sentiment(symbol: str) -> dict:
    """CryptoPanic nieuws sentiment voor een coin. Gratis tier: 50 calls/dag."""
    if not CRYPTOPANIC_KEY:
        return {"sentiment": "neutral", "bullish": 0, "bearish": 0, "ok": False}
    coin = symbol.replace("USDT", "").replace("EUR", "")
    cache_key = f"cp_{coin}"
    cached = _cache_get(cache_key)
    if cached: return cached
    try:
        r = requests.get(
            f"https://cryptopanic.com/api/v1/posts/",
            params={"auth_token": CRYPTOPANIC_KEY, "currencies": coin,
                    "filter": "hot", "limit": 10},
            timeout=8,
        )
        posts = r.json().get("results", [])
        bullish = sum(1 for p in posts if p.get("votes", {}).get("positive", 0) > 2)
        bearish = sum(1 for p in posts if p.get("votes", {}).get("negative", 0) > 2)
        total = len(posts)
        if total == 0:
            sentiment = "neutral"
        elif bullish > bearish * 1.5:
            sentiment = "bullish"
        elif bearish > bullish * 1.5:
            sentiment = "bearish"
        else:
            sentiment = "neutral"
        result = {"sentiment": sentiment, "bullish": bullish,
                  "bearish": bearish, "total": total, "ok": True}
        _cache_set(cache_key, result)
        return result
    except Exception as e:
        return {"sentiment": "neutral", "bullish": 0, "bearish": 0, "ok": False, "error": str(e)}

def get_market_context(symbol: str = "") -> dict:
    """Combineer alle externe data in n dict voor risk_gate."""
    fng = get_fear_greed()
    cp  = get_cryptopanic_sentiment(symbol) if symbol else {}
    return {"fear_greed": fng, "cryptopanic": cp,
            "timestamp": datetime.now(timezone.utc).isoformat()}

if __name__ == "__main__":
    print("F&G:", get_fear_greed())
    print("Context:", get_market_context("BTCUSDT"))
