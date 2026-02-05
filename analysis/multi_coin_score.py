from __future__ import annotations

import os
import sys
import json
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
MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "80")

# 🔑 DATA (Render Disk)
DATA_DIR = os.getenv("DATA_DIR", "/data")
PENDING_APPROVALS_FILE = os.path.join(DATA_DIR, "pending_approvals.json")

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

def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(PENDING_APPROVALS_FILE):
        with open(PENDING_APPROVALS_FILE, "w", encoding="utf-8") as f:
            json.dump({"items": []}, f, indent=2)

def load_pending() -> Dict[str, Any]:
    try:
        with open(PENDING_APPROVALS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"items": []}

def save_pending(data: Dict[str, Any]) -> None:
    tmp = f"{PENDING_APPROVALS_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, PENDING_APPROVALS_FILE)

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
    return r.json() if isinstance(r.json(), list) else []

def closes_from_klines(klines: List[List[Any]]) -> List[float]:
    closes = []
    for k in klines:
        try:
            closes.append(float(k[4]))
        except Exception:
            pass
    return closes

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
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))

# ==========================================================
# SCORING
# ==========================================================
def score_symbol(closes: List[float]) -> Tuple[int, Dict[str, Any]]:
    details: Dict[str, Any] = {}

    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    r14 = rsi(closes, 14)

    details.update({"sma20": s20, "sma50": s50, "rsi14": r14})

    if s20 is None or s50 is None or r14 is None:
        return 0, details

    score = 50

    if s20 > s50:
        score += 20
        details["trend"] = "up"
    else:
        score -= 10
        details["trend"] = "down"

    if r14 >= 55:
        score += 15
        details["momentum"] = "bullish"
    elif r14 <= 45:
        score -= 10
        details["momentum"] = "bearish"
    else:
        details["momentum"] = "neutral"

    if s20 > s50 and r14 >= 60:
        score += 10
        details["bonus"] = "strong_uptrend"

    return max(0, min(100, int(score))), details

# ==========================================================
# PRE-BUY
# ==========================================================
def build_prebuy(symbol: str, score: int, details: Dict[str, Any]) -> Dict[str, Any]:
    created = int(time.time())
    price = float(details.get("last_price", 0.0))

    stop = price * 0.98 if price > 0 else 0.0
    target = price + (price - stop) * 2 if price > 0 else 0.0

    label = (
        "kans groot" if score >= 90 else
        "kans boven gemiddeld" if score >= 80 else
        "kans gemiddeld"
    )

    return {
        "id": f"PB-{symbol}-{created}",
        "symbol": symbol,
        "score": score,
        "kans": label,
        "status": "PENDING_APPROVAL",
        "created_at": created,
        "created_at_utc": now_utc_iso(),
        "expires_at_epoch": created + PREBUY_VALID_SECONDS,
        "entry": round(price, 8),
        "stop_loss": round(stop, 8),
        "target": round(target, 8),
        "details": details,
    }

# ==========================================================
# SEND TO WEBSERVICE
# ==========================================================
def send_to_webservice(prebuy: Dict[str, Any]) -> None:
    if not WEBHOOK_BASE_URL or not INTERNAL_TOKEN:
        log("⚠️ Webhook niet actief (env ontbreekt)")
        return

    try:
        requests.post(
            f"{WEBHOOK_BASE_URL}/internal/prebuy",
            json=prebuy,
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=15,
        )
    except Exception as e:
        log(f"⚠️ Webhook fout: {e}")

# ==========================================================
# MAIN
# ==========================================================
def main() -> None:
    log("🚀 multi_coin_score gestart (Pre-BUY only)")
    ensure_data_dir()

    pending = load_pending()
    now = time.time()

    # verlopen opruimen
    pending["items"] = [
        p for p in pending.get("items", [])
        if p.get("expires_at_epoch", 0) > now
    ]

    count = 0

    if FORCE_TEST_PREBUY:
        test = build_prebuy("BTCUSDT", 99, {"last_price": 100.0, "note": "forced test"})
        pending["items"].append(test)
        save_pending(pending)
        send_to_webservice(test)
        return

    for symbol in COINS:
        try:
            klines = get_klines(symbol, INTERVAL, LIMIT)
            closes = closes_from_klines(klines)
            if len(closes) < 60:
                continue

            score, details = score_symbol(closes)
            details["last_price"] = closes[-1]
            details["interval"] = INTERVAL

            log(f"📊 {symbol} score={score}")

            if score < MIN_SCORE_TO_PREBUY:
                continue

            prebuy = build_prebuy(symbol, score, details)

            pending["items"].append(prebuy)
            save_pending(pending)

            send_to_webservice(prebuy)
            count += 1

        except Exception:
            traceback.print_exc()

    log(f"✅ Run klaar — {count} Pre-BUY(s)")

if __name__ == "__main__":
    main()
