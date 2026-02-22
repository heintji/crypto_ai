# trading/live_trader.py
# =========================================
# LIVE TRADER (Bitvavo) – execute only (NO strategy)
# Fix: signature invalid (errorCode 309) -> sign with /v2/... path
# =========================================

from __future__ import annotations

import os
import time
import json
import hmac
import hashlib
from datetime import datetime
from typing import Any, Dict, Optional

import requests

# =========================================
# ENV
# =========================================
BITVAVO_API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()

if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
    raise RuntimeError("❌ BITVAVO_API_KEY / BITVAVO_API_SECRET ontbreken")

HTTP_TIMEOUT = 15

# Bitvavo base host (zonder /v2)
HOST = "https://api.bitvavo.com"

# =========================================
# HELPERS
# =========================================
def _sign(timestamp_ms: str, method: str, path: str, body: str = "") -> str:
    """
    Bitvavo signature:
      signature = HMAC_SHA256(secret, timestamp + method + path + body)
    BELANGRIJK: path moet EXACT "/v2/..." zijn als je endpoint /v2 gebruikt.
    """
    msg = timestamp_ms + method.upper() + path + (body or "")
    return hmac.new(
        BITVAVO_API_SECRET.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _headers(method: str, path: str, body: str = "") -> Dict[str, str]:
    ts = str(int(time.time() * 1000))
    sig = _sign(ts, method, path, body)
    return {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": sig,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json",
    }


def _market_usdt_to_eur(symbol: str) -> str:
    """
    Bot gebruikt vaak Binance symbols zoals INJUSDT.
    Bitvavo markets zijn bv: INJ-EUR.
    """
    s = (symbol or "").strip().upper()
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}-EUR"
    if "-" in s:
        return s
    # fallback: als iemand bv "INJ" geeft
    return f"{s}-EUR"


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


# =========================================
# MARKET DATA (optional)
# =========================================
def get_price(symbol: str) -> Dict[str, Any]:
    """
    Haal prijs op via Bitvavo ticker/price.
    Return dict zodat caller kan loggen.
    """
    market = _market_usdt_to_eur(symbol)
    path = "/v2/ticker/price"
    url = HOST + path
    try:
        r = requests.get(url, params={"market": market}, timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            return {"ok": False, "status": r.status_code, "error": r.text[:300], "market": market}
        data = r.json()
        return {"ok": True, "market": market, "price": _safe_float(data.get("price"))}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "market": market}


# =========================================
# LIVE BUY (market, quote amount in EUR)
# =========================================
def buy_eur(
    symbol: str,
    amount_eur: float,
    prebuy_id: Optional[str] = None,
    entry: Optional[float] = None,
    stop_loss: Optional[float] = None,
    target: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Plaatst een MARKET BUY op Bitvavo met amountQuote (EUR).
    Deze module doet alleen execution (geen strategie).
    """
    market = _market_usdt_to_eur(symbol)

    # Body exact als string gebruiken voor signing
    body_obj = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        "amountQuote": f"{float(amount_eur):.2f}",
    }
    body = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)

    path = "/v2/order"  # ✅ BELANGRIJK: signen met /v2/...
    url = HOST + path

    try:
        r = requests.post(url, headers=_headers("POST", path, body), data=body, timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            return {
                "ok": False,
                "status": r.status_code,
                "error": r.text[:500],
                "market": market,
                "prebuy_id": prebuy_id,
            }

        res = r.json()

        # Bitvavo response velden kunnen verschillen; we pakken safe
        filled_qty = _safe_float(res.get("filledAmount") or res.get("amount"))
        price = _safe_float(res.get("price"))

        return {
            "ok": True,
            "market": market,
            "symbol": symbol,
            "filled_qty": filled_qty,
            "price": price,
            "raw": res,
            "prebuy_id": prebuy_id,
            "meta": {"entry": entry, "stop_loss": stop_loss, "target": target, "ts": _now_iso()},
        }

    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "market": market,
            "prebuy_id": prebuy_id,
        }


# =========================================
# LIVE SELL (market, base amount)
# =========================================
def sell_amount(symbol: str, amount_base: float) -> Dict[str, Any]:
    """
    Plaatst MARKET SELL met amount (base).
    """
    market = _market_usdt_to_eur(symbol)

    body_obj = {
        "market": market,
        "side": "sell",
        "orderType": "market",
        "amount": f"{float(amount_base):.8f}",
    }
    body = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)

    path = "/v2/order"  # ✅ signen met /v2/...
    url = HOST + path

    try:
        r = requests.post(url, headers=_headers("POST", path, body), data=body, timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            return {"ok": False, "status": r.status_code, "error": r.text[:500], "market": market}

        res = r.json()
        price = _safe_float(res.get("price"))
        filled_qty = _safe_float(res.get("filledAmount") or res.get("amount"))

        return {"ok": True, "market": market, "price": price, "filled_qty": filled_qty, "raw": res}

    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "market": market}


# Convenience wrapper (trade_monitor gebruikt vaak sell(symbol, fraction))
def sell(symbol: str, fraction: float = 1.0, position_qty: Optional[float] = None) -> Dict[str, Any]:
    """
    Als je trade_monitor een qty weet (position_qty), kan je fraction toepassen.
    Anders moet caller sell_amount direct gebruiken.
    """
    if position_qty is None:
        return {"ok": False, "reason": "position_qty_required_for_fraction_sell"}
    qty = float(position_qty) * float(fraction)
    return sell_amount(symbol, qty)
