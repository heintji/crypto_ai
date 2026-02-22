# trading/live_trader.py
from __future__ import annotations

import json
import os
import time
import hmac
import hashlib
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests


# ==========================================================
# ENV
# ==========================================================
BITVAVO_API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()
BITVAVO_ACCESS_WINDOW = (os.getenv("BITVAVO_ACCESS_WINDOW") or "10000").strip()
BITVAVO_OPERATOR_ID = (os.getenv("BITVAVO_OPERATOR_ID") or "").strip()  # <--- BELANGRIJK (optioneel)

DATA_DIR = (os.getenv("DATA_DIR") or "/data").strip()
STATE_PATH = os.getenv("PAPER_STATE_PATH") or os.path.join(DATA_DIR, "paper_state.json")

BASE_URL = "https://api.bitvavo.com"
API_PREFIX = "/v2"

TIMEOUT = float(os.getenv("BITVAVO_TIMEOUT") or "15")


# ==========================================================
# UTIL
# ==========================================================
def _now_ms() -> str:
    return str(int(time.time() * 1000))


def _log(msg: str) -> None:
    print(msg, flush=True)


def _ensure_state_dir() -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)


def _load_state() -> Dict[str, Any]:
    _ensure_state_dir()
    if not os.path.exists(STATE_PATH):
        return {"open_trades": [], "closed_trades": []}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"open_trades": [], "closed_trades": []}


def _save_state(state: Dict[str, Any]) -> None:
    _ensure_state_dir()
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def _market_from_symbol(symbol: str) -> str:
    """
    Jij komt van Binance symbols zoals ATUSDT / BTCUSDT.
    Bitvavo is meestal BASE-EUR.
    """
    s = symbol.upper().strip()
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}-EUR"
    if "-" in s:
        return s
    # fallback: probeer alsnog EUR
    return f"{s}-EUR"


def _sign(timestamp: str, method: str, path_with_query: str, body: str) -> str:
    """
    Bitvavo signature: HMAC_SHA256(secret, timestamp + method + path + body)
    BELANGRIJK: path MUST include '/v2/...'
    """
    msg = f"{timestamp}{method}{path_with_query}{body}"
    return hmac.new(BITVAVO_API_SECRET.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _request(method: str, endpoint: str, params: Optional[Dict[str, Any]] = None, body: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        return 0, {"ok": False, "error": "Missing BITVAVO_API_KEY/SECRET env vars"}

    method_u = method.upper()
    params = params or {}

    # OperatorId toevoegen als Bitvavo dit vereist
    if BITVAVO_OPERATOR_ID and "operatorId" not in params:
        params["operatorId"] = BITVAVO_OPERATOR_ID

    # Build query string (moet mee in signature als hij op de URL staat)
    query = ""
    if params:
        # requests maakt encoding, maar voor signature willen we exact dezelfde query
        # -> eenvoudige, stabiele build (Bitvavo accepteert dit)
        items = [f"{k}={params[k]}" for k in sorted(params.keys())]
        query = "?" + "&".join(items)

    path_with_query = f"{API_PREFIX}{endpoint}{query}"
    url = f"{BASE_URL}{path_with_query}"

    body_str = ""
    if body is not None:
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    ts = _now_ms()
    sig = _sign(ts, method_u, path_with_query, body_str)

    headers = {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": sig,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Window": BITVAVO_ACCESS_WINDOW,
        "Content-Type": "application/json",
    }

    try:
        resp = requests.request(
            method_u,
            url,
            headers=headers,
            data=body_str if body is not None else None,
            timeout=TIMEOUT,
        )
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data
    except Exception as e:
        return 0, {"ok": False, "error": f"Request error: {type(e).__name__}: {e}"}


# ==========================================================
# PUBLIC API
# ==========================================================
def buy_eur(symbol: str, amount_eur: float, prebuy_id: str = "", entry: float = 0.0, stop: float = 0.0, target: float = 0.0, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Plaatst een MARKET BUY op Bitvavo voor amountQuote (EUR).
    Slaat daarna een open_trade op in /data/paper_state.json zodat trade_monitor hem kan monitoren.
    """
    market = _market_from_symbol(symbol)

    payload = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        "amountQuote": str(float(amount_eur)),  # EUR bedrag
    }

    status, data = _request("POST", "/order", params=None, body=payload)

    # Bitvavo errors komen als dict met errorCode/error
    if status != 200:
        return {"ok": False, "status": status, "error": data, "market": market, "prebuy_id": prebuy_id}

    # Succes: order object terug
    order = data

    # Probeer gemiddelde fill prijs te pakken (niet altijd aanwezig)
    avg_price = 0.0
    for k in ("averagePrice", "avgPrice", "filledPrice"):
        try:
            if k in order and order[k]:
                avg_price = float(order[k])
                break
        except Exception:
            pass

    trade = {
        "prebuy_id": prebuy_id,
        "symbol": symbol,
        "market": market,
        "amount_eur": float(amount_eur),
        "entry": float(entry) if entry else avg_price,
        "stop": float(stop),
        "target": float(target),
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "OPEN",
        "exchange": "bitvavo",
        "order": order,
        "meta": meta or {},
    }

    state = _load_state()
    state.setdefault("open_trades", [])
    state.setdefault("closed_trades", [])
    state["open_trades"].append(trade)
    _save_state(state)

    _log(f"✅ LIVE BUY OK: {market} €{amount_eur} prebuy_id={prebuy_id}")
    return {"ok": True, "status": 200, "market": market, "prebuy_id": prebuy_id, "order": order}


def sell_market(market: str, amount_base: float) -> Dict[str, Any]:
    """
    MARKET SELL. (Voor later wanneer trade_monitor SELL wil doen)
    """
    payload = {
        "market": market,
        "side": "sell",
        "orderType": "market",
        "amount": str(float(amount_base)),  # base amount
    }
    status, data = _request("POST", "/order", params=None, body=payload)
    if status != 200:
        return {"ok": False, "status": status, "error": data, "market": market}
    return {"ok": True, "status": 200, "market": market, "order": data}
