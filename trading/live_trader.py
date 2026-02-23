# trading/live_trader.py
from __future__ import annotations

import hmac
import hashlib
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

BITVAVO_BASE_URL = (os.getenv("BITVAVO_BASE_URL") or "https://api.bitvavo.com").strip().rstrip("/")
BITVAVO_ACCESS_WINDOW = str(int(os.getenv("BITVAVO_ACCESS_WINDOW") or "10000")).strip()

# Belangrijk: dit is een integer die jij ZELF kiest (bijv. 1)
BITVAVO_OPERATOR_ID = int(os.getenv("BITVAVO_OPERATOR_ID") or "1")

# API path
API_PATH_ORDER = "/v2/order"


# ==========================================================
# UTIL
# ==========================================================
def _log(msg: str) -> None:
    print(msg, flush=True)


def _canonical_json(data: Dict[str, Any]) -> str:
    """
    Maak EXACT dezelfde JSON string voor:
    - request body
    - signature
    (geen spaties, vaste key-order)
    """
    return json.dumps(data, separators=(",", ":"), sort_keys=True)


def _sign(timestamp_ms: str, method: str, path: str, body_str: str) -> str:
    """
    Bitvavo signature: HMAC SHA256 hex van:
    timestamp + method + path + body
    """
    payload = f"{timestamp_ms}{method.upper()}{path}{body_str}"
    return hmac.new(
        BITVAVO_API_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _headers(method: str, path: str, body_str: str) -> Dict[str, str]:
    ts = str(int(time.time() * 1000))
    sig = _sign(ts, method, path, body_str)
    return {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": sig,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Window": BITVAVO_ACCESS_WINDOW,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _symbol_to_market(symbol: str) -> str:
    """
    Jij krijgt vaak symbols als ATUSDT / ENOUSDT.
    Bitvavo gebruikt markets als AT-EUR / ENO-EUR.
    """
    s = (symbol or "").strip().upper()

    # Als iemand al "BTC-EUR" meegeeft: direct gebruiken
    if "-" in s:
        return s

    # Vaak uit Binance-universe: XXXUSDT
    if s.endswith("USDT"):
        base = s[:-4]  # strip USDT
        return f"{base}-EUR"

    # Als het al eindigt op EUR (zeldzaam): strip en maak -EUR
    if s.endswith("EUR"):
        base = s[:-3]
        return f"{base}-EUR"

    # fallback: neem hele string als base
    return f"{s}-EUR"


def _post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        return {"ok": False, "status": 0, "error": "BITVAVO_API_KEY/SECRET ontbreekt in env."}

    body_str = _canonical_json(body)
    url = f"{BITVAVO_BASE_URL}{path}"

    try:
        resp = requests.post(
            url,
            data=body_str,  # IMPORTANT: exact dezelfde string als signature
            headers=_headers("POST", path, body_str),
            timeout=20,
        )
        # Bitvavo antwoord is JSON
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        if resp.status_code >= 200 and resp.status_code < 300:
            return {"ok": True, "status": resp.status_code, "data": data}
        return {"ok": False, "status": resp.status_code, "error": data}
    except Exception as e:
        return {"ok": False, "status": 0, "error": f"{type(e).__name__}: {e}"}


# ==========================================================
# PUBLIC API (wordt aangeroepen door whatsapp_webhook / trade_monitor)
# ==========================================================
def buy_eur(symbol: str, amount_eur: float, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Plaatst een MARKET BUY met amountQuote (EUR bedrag).
    Cruciaal:
    - operatorId MOET mee (Bitvavo errorCode 203 als die ontbreekt)
    - signature MOET exact matchen met body-string
    """
    market = _symbol_to_market(symbol)

    # operatorId is verplicht (jij kiest zelf een integer, bv. 1)
    body: Dict[str, Any] = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        "amountQuote": str(float(amount_eur)),
        "operatorId": int(BITVAVO_OPERATOR_ID),
    }

    # Optioneel: jouw prebuy_id meegeven als clientOrderId (handig voor trace)
    # Alleen doen als je zeker weet dat Bitvavo dit accepteert.
    if meta and isinstance(meta, dict):
        prebuy_id = (meta.get("prebuy_id") or "").strip()
        if prebuy_id:
            # Als Bitvavo dit niet accepteert krijg je netjes een fout terug (crasht niet)
            body["clientOrderId"] = prebuy_id[:36]  # houd 'm kort/veilig

    _log(f"🟦 LIVE BUY -> market={market} amountEUR={amount_eur} operatorId={BITVAVO_OPERATOR_ID}")
    res = _post(API_PATH_ORDER, body)

    # Maak response compact voor WhatsApp
    if res.get("ok") is True:
        return {
            "ok": True,
            "mode": "live",
            "market": market,
            "amount_eur": float(amount_eur),
            "operatorId": BITVAVO_OPERATOR_ID,
            "data": res.get("data"),
        }

    return {
        "ok": False,
        "mode": "live",
        "market": market,
        "amount_eur": float(amount_eur),
        "operatorId": BITVAVO_OPERATOR_ID,
        "status": res.get("status"),
        "error": res.get("error"),
    }
