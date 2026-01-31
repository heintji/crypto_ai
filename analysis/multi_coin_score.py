# analysis/multi_coin_score.py
from __future__ import annotations

import os
import sys
import json
import time
import requests
from typing import Any, Dict, List, Optional

# =========================================
# PROJECT ROOT (analysis/.. -> crypto_ai)
# =========================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# =========================================
# ENV
# =========================================
WEBHOOK_URL = (os.getenv("WEBHOOK_URL", "").strip().rstrip("/"))
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN", "").strip())
FORCE_TEST_PREBUY = os.getenv("FORCE_TEST_PREBUY", "0") == "1"

# =========================================
# SETTINGS
# =========================================
COINS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "SOLUSDT"]
BASE_URL = "https://api.binance.com/api/v3/klines"
INTERVAL = "1h"
LIMIT = 120

# =========================================
# HELPERS
# =========================================
def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def fetch_klines(symbol: str, interval: str = INTERVAL, limit: int = LIMIT) -> List[List[Any]]:
    r = requests.get(BASE_URL, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=20)
    r.raise_for_status()
    return r.json()

def make_prebuy(symbol: str, score: float, entry: float, stop_loss: float, target: float) -> Dict[str, Any]:
    return {
        "id": f"PB-{symbol}-{int(time.time())}",
        "coin": symbol,
        "setup": "SMA_RSI",
        "score": float(score),
        "entry": float(entry),
        "stop_loss": float(stop_loss),
        "target": float(target),
        "status": "PENDING",
        "created_at": now_utc_iso(),
        "expires_at": int(time.time()) + 4 * 60 * 60,  # 4 uur
    }

def post_prebuy_to_webservice(prebuy: Dict[str, Any]) -> bool:
    if not WEBHOOK_URL or not INTERNAL_TOKEN:
        print("❌ WEBHOOK_URL of INTERNAL_TOKEN ontbreekt in env vars.")
        return False

    url = f"{WEBHOOK_URL}/internal/prebuy"
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Token": INTERNAL_TOKEN,
    }
    try:
        resp = requests.post(url, headers=headers, data=json.dumps(prebuy), timeout=20)
        ok = resp.status_code == 200
        print(f"➡️ POST prebuy -> {url} status={resp.status_code} body={resp.text[:200]}")
        return ok
    except Exception as e:
        print(f"❌ POST prebuy failed: {e}")
        return False

# =========================================
# VERY SIMPLE SCORING (placeholder)
# jouw echte logic kan hier blijven bestaan
# =========================================
def simple_score_from_klines(klines: List[List[Any]]) -> float:
    # dummy score: altijd 99 in test, anders 0
    return 99.0

def main():
    print(f"🚀 multi_coin_score gestart (Pre-BUY only)")
    print(f"🕒 UTC time: {now_utc_iso()}")

    new_prebuys = 0

    # 1) TEST MODE: force 1 test prebuy
    if FORCE_TEST_PREBUY:
        test = {
            "id": f"PB-TEST-{int(time.time())}",
            "coin": "BTCUSDT",
            "setup": "TEST",
            "score": 99.0,
            "entry": 100.0,
            "stop_loss": 95.0,
            "target": 110.0,
            "status": "PENDING",
            "created_at": now_utc_iso(),
            "expires_at": int(time.time()) + 4 * 60 * 60,
        }
        if post_prebuy_to_webservice(test):
            new_prebuys += 1
            print(f"✅ TEST PRE-BUY verzonden: {test['id']}")
        else:
            print("❌ TEST PRE-BUY niet verzonden.")
        print(f"✅ Pre-BUY run klaar - {new_prebuys} nieuwe Pre-BUY(s).")
        return

    # 2) NORMAL RUN
    for symbol in COINS:
        try:
            klines = fetch_klines(symbol)
            score = simple_score_from_klines(klines)

            # jouw echte voorwaarden hier (bijv score >= 80)
            if score >= 90:
                # placeholder prijzen (in jouw echte code bereken je entry/SL/target)
                entry = 100.0
                stop = 95.0
                target = 110.0

                prebuy = make_prebuy(symbol, score, entry, stop, target)

                if post_prebuy_to_webservice(prebuy):
                    new_prebuys += 1
                    print(f"✅ Pre-BUY verzonden: {prebuy['id']} ({symbol})")
                else:
                    print(f"❌ Pre-BUY verzenden mislukt: {symbol}")

        except Exception as e:
            print(f"⚠️ {symbol} fout: {e}")

    print(f"✅ Pre-BUY run klaar - {new_prebuys} nieuwe Pre-BUY(s).")

if __name__ == "__main__":
    main()
