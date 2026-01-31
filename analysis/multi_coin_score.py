from __future__ import annotations

import os
import sys
import json
import time
import csv
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

# ============================================================
# PROJECT ROOT (zodat imports + paths altijd kloppen)
# multi_coin_score.py staat in crypto_ai/analysis/
# -> project root is 1 map omhoog: crypto_ai/
# -> /opt/render/project/src (Render)
# ============================================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ============================================================
# PATHS
# ============================================================
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")

PENDING_FILE = os.path.join(DATA_DIR, "pending_approvals.json")
AI_ADVICE_LOG = os.path.join(LOGS_DIR, "ai_advice.csv")  # simpel logje
PREBUY_PAYLOAD_LOG = os.path.join(LOGS_DIR, "prebuy_payload.json")
PREBUY_STATE_LOG = os.path.join(LOGS_DIR, "prebuy_state.json")

# ============================================================
# SETTINGS
# ============================================================
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

COINS = [
    "BTCUSDT",
    "ETHUSDT",
    # voeg gerust meer toe
]

INTERVAL_4H = "4h"
INTERVAL_1H = "1h"
KLINES_LIMIT = 200

MAX_PREBUY_PER_RUN = 3               # safety
PREBUY_EXPIRES_SECONDS = 4 * 60 * 60 # 4 uur

# Force test prebuy via Render env var:
# FORCE_TEST_PREBUY=1  -> maakt 1 test PENDING prebuy aan (en daarna niet nog 100x)
FORCE_TEST_PREBUY = os.getenv("FORCE_TEST_PREBUY", "0") == "1"

# ============================================================
# UTILS
# ============================================================
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)

def load_json_list(path: str) -> List[Dict[str, Any]]:
    ensure_dirs()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def save_json_list(path: str, data: List[Dict[str, Any]]) -> None:
    ensure_dirs()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def save_json(path: str, data: Dict[str, Any]) -> None:
    ensure_dirs()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def append_csv_row(path: str, row: Dict[str, Any], fieldnames: List[str]) -> None:
    ensure_dirs()
    file_exists = os.path.isfile(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

def now_s() -> int:
    return int(time.time())

def is_expired(expires_at: Any) -> bool:
    try:
        x = int(expires_at)
    except Exception:
        return False
    if x > 10**12:
        x = int(x / 1000)
    return x < now_s()

def normalize_status(x: Any) -> str:
    return str(x or "").strip().upper()

# ============================================================
# BINANCE DATA
# ============================================================
def fetch_klines(symbol: str, interval: str, limit: int = 200) -> List[List[Any]]:
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(BINANCE_KLINES_URL, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def closes_from_klines(klines: List[List[Any]]) -> List[float]:
    # kline[4] is close
    out: List[float] = []
    for k in klines:
        try:
            out.append(float(k[4]))
        except Exception:
            continue
    return out

def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / float(period)

def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += abs(diff)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))

# ============================================================
# SCORE / PREBUY LOGIC (simpel, stabiel, audit-proof)
# ============================================================
def trend_label(sma20: Optional[float], sma50: Optional[float]) -> str:
    if sma20 is None or sma50 is None:
        return "UNKNOWN"
    if sma20 > sma50:
        return "UP"
    if sma20 < sma50:
        return "DOWN"
    return "FLAT"

def compute_score(
    sma20_4h: Optional[float],
    sma50_4h: Optional[float],
    rsi_4h: Optional[float],
    sma20_1h: Optional[float],
    sma50_1h: Optional[float],
    rsi_1h: Optional[float],
) -> Tuple[float, str]:
    """
    Score 0..100.
    Dit is bewust simpel en voorspelbaar (geen magie).
    """
    score = 50.0

    # Trend 4h
    if sma20_4h is not None and sma50_4h is not None:
        if sma20_4h > sma50_4h:
            score += 20
        elif sma20_4h < sma50_4h:
            score -= 20

    # Trend 1h
    if sma20_1h is not None and sma50_1h is not None:
        if sma20_1h > sma50_1h:
            score += 10
        elif sma20_1h < sma50_1h:
            score -= 10

    # RSI filters
    if rsi_4h is not None:
        if 45 <= rsi_4h <= 65:
            score += 10
        elif rsi_4h > 75:
            score -= 10

    if rsi_1h is not None:
        if 45 <= rsi_1h <= 65:
            score += 5
        elif rsi_1h > 75:
            score -= 5

    # clamp
    score = max(0.0, min(100.0, score))

    if score >= 90:
        label = "kans groot"
    elif score >= 80:
        label = "kans boven gemiddeld"
    elif score >= 70:
        label = "kans gemiddeld"
    else:
        label = "laag"
    return score, label

def make_prebuy(symbol: str, score: float, label: str, entry: float) -> Dict[str, Any]:
    pb_id = f"PB-{symbol}-{int(time.time())}"
    return {
        "id": pb_id,
        "coin": symbol,
        "setup": "SYSTEM",
        "score": round(score, 2),
        "kans": label,
        "entry": round(entry, 8),
        "stop_loss": None,
        "target": None,
        "status": "PENDING",
        "created_at": utc_now_iso(),
        "expires_at": now_s() + PREBUY_EXPIRES_SECONDS,
    }

def already_has_test_pending(pending: List[Dict[str, Any]]) -> bool:
    for p in pending:
        if str(p.get("setup")) == "TEST" and normalize_status(p.get("status")) == "PENDING" and not is_expired(p.get("expires_at", 0)):
            return True
    return False

def create_test_prebuy(pending: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Maakt 1 test-prebuy aan, maar alleen als er nog geen actieve TEST pending bestaat.
    """
    if already_has_test_pending(pending):
        print("🟡 TEST prebuy bestaat al (PENDING) — geen nieuwe gemaakt.")
        return None

    test = {
        "id": f"PB-TEST-{int(time.time())}",
        "coin": "BTCUSDT",
        "setup": "TEST",
        "score": 99.0,
        "kans": "test",
        "entry": 100.0,
        "stop_loss": 95.0,
        "target": 110.0,
        "status": "PENDING",
        "created_at": utc_now_iso(),
        "expires_at": now_s() + PREBUY_EXPIRES_SECONDS,
    }
    pending.append(test)
    return test

# ============================================================
# MAIN
# ============================================================
def main() -> None:
    ensure_dirs()
    print(f"🚀 multi_coin_score gestart (Pre-BUY only)")
    print(f"UTC time: {utc_now_iso()}")
    print(f"Pending file: {PENDING_FILE}")
    print(f"AI log: {AI_ADVICE_LOG}")

    pending = load_json_list(PENDING_FILE)
    created_prebuys: List[Dict[str, Any]] = []
    debug_payload: Dict[str, Any] = {"run_at": utc_now_iso(), "items": []}

    # 1) FORCE TEST PREBUY (alleen voor flow test)
    if FORCE_TEST_PREBUY:
        test = create_test_prebuy(pending)
        if test:
            save_json_list(PENDING_FILE, pending)
            print(f"✅ TEST PRE-BUY gemaakt: {test['id']} (PENDING)")
        else:
            # bestaat al → niets doen
            pass

    # 2) Normale analyse
    new_prebuys = 0
    for symbol in COINS:
        if new_prebuys >= MAX_PREBUY_PER_RUN:
            break

        try:
            k4h = fetch_klines(symbol, INTERVAL_4H, KLINES_LIMIT)
            k1h = fetch_klines(symbol, INTERVAL_1H, KLINES_LIMIT)
            c4h = closes_from_klines(k4h)
            c1h = closes_from_klines(k1h)

            sma20_4h = sma(c4h, 20)
            sma50_4h = sma(c4h, 50)
            rsi_4h = rsi(c4h, 14)

            sma20_1h = sma(c1h, 20)
            sma50_1h = sma(c1h, 50)
            rsi_1h = rsi(c1h, 14)

            score, label = compute_score(sma20_4h, sma50_4h, rsi_4h, sma20_1h, sma50_1h, rsi_1h)
            entry = float(c1h[-1]) if c1h else (float(c4h[-1]) if c4h else 0.0)

            debug_payload["items"].append({
                "symbol": symbol,
                "score": score,
                "label": label,
                "entry": entry,
                "trend_4h": trend_label(sma20_4h, sma50_4h),
                "trend_1h": trend_label(sma20_1h, sma50_1h),
                "rsi_4h": rsi_4h,
                "rsi_1h": rsi_1h,
            })

            # Voorbeeld: alleen prebuy vanaf 80
            if score >= 80 and entry > 0:
                pb = make_prebuy(symbol, score, label, entry)
                pending.append(pb)
                created_prebuys.append(pb)
                new_prebuys += 1

                append_csv_row(
                    AI_ADVICE_LOG,
                    {
                        "utc_time": utc_now_iso(),
                        "prebuy_id": pb["id"],
                        "symbol": symbol,
                        "score": round(score, 2),
                        "label": label,
                        "entry": round(entry, 8),
                    },
                    fieldnames=["utc_time", "prebuy_id", "symbol", "score", "label", "entry"],
                )

        except Exception as e:
            print(f"⚠️ {symbol} fout: {e}")
            traceback.print_exc()

    # 3) Save pending
    save_json_list(PENDING_FILE, pending)

    # 4) Save payload/state debug
    save_json(PREBUY_PAYLOAD_LOG, debug_payload)
    save_json(PREBUY_STATE_LOG, {"utc_time": utc_now_iso(), "new_prebuys": new_prebuys})

    print(f"✅ Pre-BUY run klaar — {new_prebuys} nieuwe Pre-BUY(s).")

if __name__ == "__main__":
    main()
