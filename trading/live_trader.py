# trading/live_trader.py
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional

import requests

# ==========================================================
# ENV
# ==========================================================
BITVAVO_API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()

# Optional: alleen invullen als Bitvavo het echt eist (meestal NIET nodig voor particuliere accounts)
BITVAVO_OPERATOR_ID = (os.getenv("BITVAVO_OPERATOR_ID") or "").strip()

# Bitvavo base url (NIET veranderen)
BITVAVO_BASE_URL = (os.getenv("BITVAVO_BASE_URL") or "https://api.bitvavo.com").strip().rstrip("/")

# Access Window in ms (Bitvavo default vaak 10000)
BITVAVO_ACCESS_WINDOW = str(int(os.getenv("BITVAVO_ACCESS_WINDOW") or "10000"))

# Timeout
HTTP_TIMEOUT = float(os.getenv("BITVAVO_HTTP_TIMEOUT") or "15")


# ==========================================================
# HELPERS
# ==========================================================
def _now_ms() -> str:
    return str(int(time.time() * 1000))


def _canonical_json(data: Dict[str, Any]) -> str:
    """
    Belangrijk: body-string moet EXACT hetzelfde zijn als wat je verstuurt.
    Daarom gebruiken we compacte JSON zonder extra spaties.
    """
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _sign(secret: str, timestamp_ms: str, method: str, path: str, body: str) -> str:
    """
    Bitvavo signature:
    HMAC_SHA256(secret, timestamp + method + path + body)
    """
    msg = (timestamp_ms + method.upper() + path + body).encode("utf-8")
    key = secret.encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def _headers(signature: str, timestamp_ms: str) -> Dict[str, str]:
    return {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": signature,
        "Bitvavo-Access-Timestamp": timestamp_ms,
        "Bitvavo-Access-Window": BITVAVO_ACCESS_WINDOW,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _symbol_to_market(symbol: str) -> str:
    """
    Bot werkt met Binance-symbols zoals ENSOUSDT.
    Bitvavo gebruikt markets zoals ENSO-EUR.
    """
    s = (symbol or "").strip().upper()
    if not s:
        return ""

    if "-" in s:
        # al in Bitvavo format (BTC-EUR)
        return s

    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}-EUR"

    # fallback: neem aan dat het al base is
    return f"{s}-EUR"


def _post(path: str, body_dict: Dict[str, Any]) -> Dict[str, Any]:
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        return {"ok": False, "status": 0, "error": "Missing BITVAVO_API_KEY/BITVAVO_API_SECRET"}

    url = f"{BITVAVO_BASE_URL}{path}"

    timestamp_ms = _now_ms()
    body = _canonical_json(body_dict)

    signature = _sign(BITVAVO_API_SECRET, timestamp_ms, "POST", path, body)
    headers = _headers(signature, timestamp_ms)

    try:
        r = requests.post(url, headers=headers, data=body.encode("utf-8"), timeout=HTTP_TIMEOUT)
        txt = r.text or ""
        try:
            data = r.json()
        except Exception:
            data = {"raw": txt}

        if 200 <= r.status_code < 300:
            return {"ok": True, "status": r.status_code, "data": data}

        # Bitvavo errors zitten vaak in: {"errorCode":..., "error": "..."}
        return {"ok": False, "status": r.status_code, "error": data, "raw": txt}

    except Exception as e:
        return {"ok": False, "status": 0, "error": f"request error: {type(e).__name__}: {e}"}


# ==========================================================
# PUBLIC API (wordt aangeroepen door whatsapp_webhook / trade_monitor)
# ==========================================================
def buy_eur(
    symbol: str,
    amount_eur: float,
    meta: Optional[Dict[str, Any]] = None,
    *,
    prebuy_id: Optional[str] = None,
    entry: Optional[float] = None,
    stop: Optional[float] = None,
    target: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Plaats een MARKET BUY op Bitvavo voor een EUR bedrag.
    We gebruiken amountQuote (EUR) zodat je precies €X koopt.

    Return format:
    { ok: True/False, status, market, ... }
    """
    market = _symbol_to_market(symbol)
    if not market:
        return {"ok": False, "status": 0, "error": "symbol/market missing"}

    # payload volgens Bitvavo /v2/order
    payload: Dict[str, Any] = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        # amountQuote = bedrag in quote currency (EUR)
        "amountQuote": str(float(amount_eur)),
    }

    # Optioneel: operatorId ALLEEN als Bitvavo het eist (meestal niet bij privé accounts)
    # Als BITVAVO_OPERATOR_ID leeg is, sturen we dit NIET mee.
    if BITVAVO_OPERATOR_ID:
        payload["operatorId"] = BITVAVO_OPERATOR_ID

    # Meta/debug info (niet naar Bitvavo, wel terug in response)
    meta_out = {
        "prebuy_id": prebuy_id or (meta or {}).get("prebuy_id"),
        "entry": entry if entry is not None else (meta or {}).get("entry"),
        "stop": stop if stop is not None else (meta or {}).get("stop"),
        "target": target if target is not None else (meta or {}).get("target"),
    }

    res = _post("/v2/order", payload)

    if res.get("ok") is True:
        return {
            "ok": True,
            "status": res.get("status"),
            "market": market,
            "amount_eur": float(amount_eur),
            "meta": meta_out,
            "data": res.get("data"),
        }

    return {
        "ok": False,
        "status": res.get("status"),
        "market": market,
        "amount_eur": float(amount_eur),
        "meta": meta_out,
        "error": res.get("error"),
        "raw": res.get("raw"),
        "hint": "Als je errorCode 309 krijgt: signature klopt niet -> check dat BITVAVO_API_SECRET exact is en dat base url https://api.bitvavo.com is.",
    }


def sell_amount(
    market: str,
    amount_base: float,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    MARKET SELL op Bitvavo van een base-amount (bijv. 0.01 BTC).
    """
    m = _symbol_to_market(market)
    if not m:
        return {"ok": False, "status": 0, "error": "market missing"}

    payload: Dict[str, Any] = {
        "market": m,
        "side": "sell",
        "orderType": "market",
        "amount": str(float(amount_base)),
    }

    if BITVAVO_OPERATOR_ID:
        payload["operatorId"] = BITVAVO_OPERATOR_ID

    res = _post("/v2/order", payload)

    if res.get("ok") is True:
        return {"ok": True, "status": res.get("status"), "market": m, "amount": float(amount_base), "data": res.get("data"), "meta": meta or {}}

    return {"ok": False, "status": res.get("status"), "market": m, "amount": float(amount_base), "error": res.get("error"), "raw": res.get("raw"), "meta": meta or {}}
