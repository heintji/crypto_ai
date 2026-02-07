from __future__ import annotations

import os
import sys
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import requests

# ==========================================================
# PROJECT ROOT
# ==========================================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ==========================================================
# ENV
# ==========================================================
WEBHOOK_BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or "").strip().rstrip("/")
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()

FORCE_TEST_PREBUY = (os.getenv("FORCE_TEST_PREBUY") or "0").strip() == "1"
PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))
MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "70")

ENV_DATA_DIR = (os.getenv("DATA_DIR") or "").strip()

# 👇 BELANGRIJK
# EXACTE DEDUPE — alleen identieke trade setups blokkeren
ACTIVE_STATUSES = {"PENDING", "APPROVED", "PROCESSING"}

# ==========================================================
# MARKET SETTINGS
# ==========================================================
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

COINS = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "SOLUSDT",
]

INTERVAL = "1h"
LIMIT = 200

# ==========================================================
# HELPERS
# ==========================================================
def log(msg: str) -> None:
    print(msg, flush=True)

def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def get_data_dir() -> str:
    candidates = []
    if ENV_DATA_DIR:
        candidates.append(ENV_DATA_DIR)
    candidates.append("/data")
    candidates.append(os.path.join(PROJECT_ROOT, "data"))

    for c in candidates:
        try:
            os.makedirs(c, exist_ok=True)
            return c
        except Exception:
            pass

    return os.path.join(PROJECT_ROOT, "data")

def is_expired(expires_at: Any) -> bool:
    try:
        return int(expires_at) < int(time.time())
    except Exception:
        return False

# ==========================================================
# PENDING APPROVALS (DEDUP SOURCE)
# ==========================================================
def load_pending() -> List[Dict[str, Any]]:
    try:
        path = os.path.join(get_data_dir(), "pending_approvals.json")
        if not os.path.isfile(path):
            return []
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def has_exact_same_trade(symbol: str, entry: float, stop: float, target: float) -> bool:
    """
    ❌ Blokkeert alleen als:
    - zelfde coin
    - entry exact gelijk
    - stop exact gelijk
    - target exact gelijk
    - status actief
    - niet verlopen
    """
    pending = load_pending()
    symbol = symbol.upper()

    for p in pending:
        try:
            if str(p.get("status", "")).upper() not in ACTIVE_STATUSES:
                continue
            if is_expired(p.get("expires_at")):
                continue
            if str(p.get("coin", "")).upper() != symbol:
                continue

            if (
                float(p.get("entry", 0)) == entry and
                float(p.get("stop_loss", 0)) == stop and
                float(p.get("target", 0)) == target
            ):
                return True
        except Exception:
            continue

    return False

# ==========================================================
# BINANCE DATA
# ==========================================================
def get_klines(symbol: str, interval: str, limit: int) -> List[List[Any]]:
    r = requests.get(
        BINANCE_KLINES,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()

def closes_from_klines(klines: List[List[Any]]) -> List[float]:
    return [float(k[4]) for k in klines if len(k) > 4]

# ==========================================================
# INDICATORS
# ==========================================================
def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses += abs(delta)
    if losses == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + gains / losses))

# ==========================================================
# SCORING
# ==========================================================
def score_symbol(closes: List[float]) -> Tuple[int, Dict[str, Any]]:
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    r14 = rsi(closes, 14)

    details = {"sma20": s20, "sma50": s50, "rsi14": r14}

    if s20 is None or s50 is None or r14 is None:
        return 0, details

    score = 50
    if s20 > s50:
        score += 20
    else:
        score -= 10

    if r14 >= 60:
        score += 15
    elif r14 <= 45:
        score -= 10

    return max(0, min(100, score)), details

# ==========================================================
# PRE-BUY
# ==========================================================
def build_prebuy(symbol: str, score: int, details: Dict[str, Any]) -> Dict[str, Any]:
    created = int(time.time())
    price = float(details["last_price"])

    stop = round(price * 0.98, 8)
    target = round(price + (price - stop) * 2, 8)

    return {
        "id": f"PB-{symbol}-{created}",
        "coin": symbol,
        "score": score,
        "status": "PENDING",
        "created_at": created,
        "created_at_utc": now_utc_iso(),
        "expires_at": created + PREBUY_VALID_SECONDS,
        "entry": round(price, 8),
        "stop_loss": stop,
        "target": target,
        "details": details,
    }

# ==========================================================
# SEND TO WEBSERVICE
# ==========================================================
def send_to_webservice(prebuy: Dict[str, Any]) -> bool:
    url = f"{WEBHOOK_BASE_URL}/internal/prebuy"
    r = requests.post(
        url,
        json=prebuy,
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        timeout=20,
    )
    return 200 <= r.status_code < 300

# ==========================================================
# MAIN
# ==========================================================
def main() -> None:
    log("🚀 multi_coin_score gestart (Pre-BUY only)")

    for symbol in COINS:
        try:
            klines = get_klines(symbol, INTERVAL, LIMIT)
            closes = closes_from_klines(klines)
            if len(closes) < 60:
                continue

            score, details = score_symbol(closes)
            details["last_price"] = closes[-1]
            details["interval"] = INTERVAL

            if score < MIN_SCORE_TO_PREBUY:
                continue

            price = round(details["last_price"], 8)
            stop = round(price * 0.98, 8)
            target = round(price + (price - stop) * 2, 8)

            # ✅ DE BESLISSENDE REGEL
            if has_exact_same_trade(symbol, price, stop, target):
                log(f"🔁 {symbol} zelfde trade-setup → GEEN nieuwe Pre-BUY")
                continue

            prebuy = build_prebuy(symbol, score, details)
            if send_to_webservice(prebuy):
                log(f"✅ Nieuwe Pre-BUY: {prebuy['id']}")

        except Exception:
            traceback.print_exc()

    log("✅ multi_coin_score klaar")

if __name__ == "__main__":
    main()
