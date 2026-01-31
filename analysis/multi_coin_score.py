from __future__ import annotations

import os
import sys
import json
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import requests

# ==========================================================
# PROJECT ROOT (analysis/multi_coin_score.py -> parent is project-root)
# ==========================================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ==========================================================
# ENV
# ==========================================================
WEBHOOK_BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or "").strip().rstrip("/")  # bv: https://crypto-ai-xxxx.onrender.com
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()                 # moet exact gelijk zijn als in webservice

FORCE_TEST_PREBUY = (os.getenv("FORCE_TEST_PREBUY") or "0").strip() == "1"
PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))  # default 4 uur

# ==========================================================
# COINS / SETTINGS
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

# Scoring drempel (voorbeeld)
MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "80")

# ==========================================================
# (OPTIONEEL) lokale fallback file (alleen nuttig lokaal)
# LET OP: op Render moet je via Webhook sturen (Plan A)
# ==========================================================
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
LOCAL_PENDING_PATH = os.path.join(DATA_DIR, "pending_approvals.json")

# ==========================================================
# HELPERS: LOG
# ==========================================================
def log(msg: str) -> None:
    print(msg, flush=True)

def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

# ==========================================================
# BINANCE DATA
# ==========================================================
def get_klines(symbol: str, interval: str, limit: int) -> List[List[Any]]:
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(BINANCE_KLINES, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return []
    return data

def closes_from_klines(klines: List[List[Any]]) -> List[float]:
    closes: List[float] = []
    for k in klines:
        # close = index 4
        try:
            closes.append(float(k[4]))
        except Exception:
            continue
    return closes

# ==========================================================
# INDICATORS (simpel maar stabiel)
# ==========================================================
def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / float(period)

def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
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
# SCORE MODEL (jij kunt dit later uitbreiden)
# ==========================================================
def score_symbol(closes: List[float]) -> Tuple[int, Dict[str, Any]]:
    """
    Return: (score 0-100, details)
    """
    details: Dict[str, Any] = {}

    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    r14 = rsi(closes, 14)

    details["sma20"] = s20
    details["sma50"] = s50
    details["rsi14"] = r14

    if s20 is None or s50 is None or r14 is None:
        return 0, details

    score = 50

    # trend
    if s20 > s50:
        score += 20
        details["trend"] = "up"
    elif s20 < s50:
        score -= 10
        details["trend"] = "down"
    else:
        details["trend"] = "flat"

    # momentum via RSI
    if r14 >= 55:
        score += 15
        details["momentum"] = "bullish"
    elif r14 <= 45:
        score -= 10
        details["momentum"] = "bearish"
    else:
        details["momentum"] = "neutral"

    # bonus: sterke trend
    if s20 > s50 and r14 >= 60:
        score += 10
        details["bonus"] = "strong_uptrend"

    # clamp
    score = max(0, min(100, score))
    return int(score), details

# ==========================================================
# PREBUY BUILD
# ==========================================================
def build_prebuy(symbol: str, score: int, details: Dict[str, Any]) -> Dict[str, Any]:
    pid = f"PB-{symbol}-{int(time.time())}"
    created = int(time.time())
    expires = created + PREBUY_VALID_SECONDS

    # Entry/stop/target: hier “dummy/indicatief” op basis van laatste close
    last_price = float(details.get("last_price") or 0.0)

    # fallback als last_price niet gezet is
    if last_price <= 0:
        last_price = 0.0

    # simpele RR: stop 2%, target 2R (zelfde als webhook)
    stop = last_price * (1.0 - 0.02) if last_price > 0 else 0.0
    r = last_price - stop
    target = last_price + (2.0 * r) if last_price > 0 else 0.0

    label = "kans laag"
    if score >= 90:
        label = "kans groot"
    elif score >= 80:
        label = "kans boven gemiddeld"
    elif score >= 70:
        label = "kans gemiddeld"

    return {
        "id": pid,
        "coin": symbol,
        "setup": "LIVE",
        "score": score,
        "kans": label,
        "status": "PENDING",
        "created_at": created,
        "created_at_utc": now_utc_iso(),
        "expires_at": expires,
        "entry": round(last_price, 8),
        "stop_loss": round(stop, 8),
        "target": round(target, 8),
        "details": details,
    }

def build_test_prebuy() -> Dict[str, Any]:
    created = int(time.time())
    return {
        "id": f"PB-TEST-{created}",
        "coin": "BTCUSDT",
        "setup": "TEST",
        "score": 99,
        "kans": "test",
        "status": "PENDING",
        "created_at": created,
        "created_at_utc": now_utc_iso(),
        "expires_at": created + PREBUY_VALID_SECONDS,
        "entry": 100.0,
        "stop_loss": 98.0,
        "target": 104.0,
        "details": {"note": "forced test prebuy"},
    }

# ==========================================================
# SEND TO WEBHOOK (Plan A)
# ==========================================================
def send_to_webservice(prebuy: Dict[str, Any]) -> bool:
    """
    POST {WEBHOOK_BASE_URL}/internal/prebuy
    Header: X-Internal-Token
    """
    if not WEBHOOK_BASE_URL or not INTERNAL_TOKEN:
        log("❌ WEBHOOK_BASE_URL of INTERNAL_TOKEN ontbreekt in env vars.")
        return False

    url = f"{WEBHOOK_BASE_URL}/internal/prebuy"
    try:
        r = requests.post(
            url,
            json=prebuy,
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=20,
        )
        if r.status_code >= 200 and r.status_code < 300:
            log(f"✅ Pre-BUY doorgestuurd naar webservice: {prebuy.get('id')} ({prebuy.get('coin')})")
            return True
        log(f"❌ Webservice reject: {r.status_code} - {r.text[:200]}")
        return False
    except Exception as e:
        log(f"❌ Webservice send error: {e}")
        return False

# ==========================================================
# LOCAL FALLBACK (alleen handig lokaal)
# ==========================================================
def local_append(prebuy: Dict[str, Any]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    arr: List[Dict[str, Any]] = []
    try:
        if os.path.isfile(LOCAL_PENDING_PATH):
            with open(LOCAL_PENDING_PATH, "r", encoding="utf-8") as f:
                arr = json.load(f) if isinstance(json.load(f), list) else []
    except Exception:
        arr = []

    # avoid duplicate
    pid = str(prebuy.get("id", "")).strip()
    if any(str(x.get("id", "")).strip() == pid for x in arr):
        return

    arr.append(prebuy)
    with open(LOCAL_PENDING_PATH, "w", encoding="utf-8") as f:
        json.dump(arr, f, indent=2, ensure_ascii=False)

# ==========================================================
# MAIN
# ==========================================================
def main() -> None:
    log("🚀 multi_coin_score gestart (Pre-BUY only)")
    log(f"🕒 UTC time: {now_utc_iso()}")
    log(f"📌 Project root: {PROJECT_ROOT}")
    log(f"📌 Local pending file: {LOCAL_PENDING_PATH}")
    log(f"🌐 WEBHOOK_BASE_URL: {WEBHOOK_BASE_URL or '(not set)'}")
    log(f"🔐 INTERNAL_TOKEN set: {'yes' if bool(INTERNAL_TOKEN) else 'no'}")

    # 1) TEST PREBUY (alleen als env aan staat)
    if FORCE_TEST_PREBUY:
        test = build_test_prebuy()
        sent = send_to_webservice(test)
        if not sent:
            log("⚠️ TEST PRE-BUY niet verzonden (env ontbreekt of webservice faalt).")
            local_append(test)
            log(f"✅ TEST PRE-BUY lokaal opgeslagen: {test['id']}")
        else:
            log(f"✅ TEST PRE-BUY verzonden: {test['id']}")
        # Test is genoeg voor flow-check
        return

    new_prebuys = 0

    # 2) LIVE analyse
    for symbol in COINS:
        try:
            klines = get_klines(symbol, INTERVAL, LIMIT)
            closes = closes_from_klines(klines)
            if len(closes) < 60:
                log(f"⚠️ {symbol}: te weinig data")
                continue

            last_price = closes[-1]
            score, details = score_symbol(closes)
            details["last_price"] = last_price
            details["interval"] = INTERVAL

            log(f"📊 {symbol} score={score} last={last_price}")

            if score < MIN_SCORE_TO_PREBUY:
                continue

            prebuy = build_prebuy(symbol, score, details)
            sent = send_to_webservice(prebuy)
            if not sent:
                # fallback (voor lokaal debug)
                local_append(prebuy)
                log(f"✅ Pre-BUY lokaal opgeslagen: {prebuy['id']}")

            new_prebuys += 1

        except Exception as e:
            traceback.print_exc()
            log(f"❌ {symbol} fout: {e}")

    log(f"✅ Pre-BUY run klaar - {new_prebuys} nieuwe Pre-BUY(s).")

if __name__ == "__main__":
    main()

