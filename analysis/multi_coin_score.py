from __future__ import annotations

import os
import sys
import json
import time
import hashlib
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

# cooldown in seconden (bijv. 6 uur)
TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS") or str(6 * 60 * 60))

# ==========================================================
# DATA PATHS (Render)
# ✅ NOOIT hardcoded /data -> altijd via env
# ==========================================================
DATA_DIR = (os.getenv("DATA_DIR") or "/tmp/data").strip()
LOGS_DIR = (os.getenv("LOGS_DIR") or os.path.join(DATA_DIR, "logs")).strip()

# Als je deze via Render env invult: gebruikt hij die. Anders fallback naar DATA_DIR
FINGERPRINT_PATH = (os.getenv("FINGERPRINT_PATH") or os.path.join(DATA_DIR, "trade_fingerprints.json")).strip()

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

HTTP_TIMEOUT = 20

# ==========================================================
# HELPERS
# ==========================================================
def log(msg: str) -> None:
    print(msg, flush=True)

def now_utc() -> int:
    return int(time.time())

def ensure_dir(path: str) -> None:
    if path and not os.path.exists(path):
        os.makedirs(path, exist_ok=True)

def ensure_data_file(path: str, default: Any) -> None:
    parent = os.path.dirname(path)
    ensure_dir(parent)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2, ensure_ascii=False)

def load_json(path: str, default: Any) -> Any:
    ensure_data_file(path, default)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data: Any) -> None:
    parent = os.path.dirname(path)
    ensure_dir(parent)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

# ==========================================================
# BINANCE DATA
# ==========================================================
def get_klines(symbol: str, interval: str, limit: int) -> List[List[Any]]:
    r = requests.get(
        BINANCE_KLINES,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()

def closes_from_klines(klines: List[List[Any]]) -> List[float]:
    return [float(k[4]) for k in klines if k and len(k) > 4]

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
# SETUP & SCORING
# ==========================================================
def determine_setup(s20: float, s50: float, r14: float) -> str:
    if s20 > s50 and r14 >= 55:
        return "TREND_PULLBACK"
    if s20 < s50 and r14 <= 45:
        return "BEAR_RALLY"
    return "RANGE"

def score_symbol(closes: List[float]) -> Tuple[int, Dict[str, Any]]:
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    r14 = rsi(closes, 14)

    if s20 is None or s50 is None or r14 is None:
        return 0, {}

    score = 50
    if s20 > s50:
        score += 20
    else:
        score -= 10

    if r14 >= 60:
        score += 20
    elif r14 <= 40:
        score -= 10

    setup = determine_setup(s20, s50, r14)

    return max(0, min(100, score)), {
        "sma20": s20,
        "sma50": s50,
        "rsi14": r14,
        "setup_type": setup,
    }

# ==========================================================
# FINGERPRINT LOGIC (ANTI-DUPLICATE)
# ==========================================================
def make_fingerprint(coin: str, setup: str, entry: float, target: float) -> str:
    # Rond af naar zones zodat mini-verschillen niet zorgen voor nieuwe trades
    zone_entry = round(float(entry), 3)
    zone_target = round(float(target), 3)
    raw = f"{coin}|{setup}|{zone_entry}|{zone_target}"
    return hashlib.sha256(raw.encode()).hexdigest()

def is_duplicate(fp: str, fingerprints: Dict[str, Any]) -> bool:
    rec = fingerprints.get(fp)
    if not rec:
        return False
    ts = int(rec.get("ts") or 0)
    return (now_utc() - ts) < TRADE_COOLDOWN_SECONDS

def store_fingerprint(fp: str, coin: str, setup: str) -> None:
    fingerprints = load_json(FINGERPRINT_PATH, {})
    fingerprints[fp] = {
        "coin": coin,
        "setup": setup,
        "ts": now_utc(),
    }
    save_json(FINGERPRINT_PATH, fingerprints)

# ==========================================================
# PRE-BUY
# ==========================================================
def build_prebuy(symbol: str, score: int, details: Dict[str, Any], price: float) -> Dict[str, Any]:
    created = now_utc()
    stop = price * 0.98
    target = price + (price - stop) * 2

    return {
        "id": f"PB-{symbol}-{created}",
        "coin": symbol,
        "setup_type": details["setup_type"],
        "score": score,
        "status": "PENDING",
        "created_at": created,
        "expires_at": created + PREBUY_VALID_SECONDS,
        "entry": round(price, 8),
        "stop_loss": round(stop, 8),
        "target": round(target, 8),
        "details": details,
    }

# ==========================================================
# SEND TO WEBSERVICE
# ==========================================================
def send_to_webservice(prebuy: Dict[str, Any]) -> bool:
    if not WEBHOOK_BASE_URL or not INTERNAL_TOKEN:
        log("❌ WEBHOOK_BASE_URL of INTERNAL_TOKEN ontbreekt")
        return False

    try:
        r = requests.post(
            f"{WEBHOOK_BASE_URL}/internal/prebuy",
            json=prebuy,
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code >= 300:
            log(f"⚠️ Webservice error {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        log(f"⚠️ Webservice exception: {e}")
        return False

# ==========================================================
# MAIN
# ==========================================================
def main() -> None:
    log("🚀 multi_coin_score gestart")
    log(f"DATA_DIR: {DATA_DIR}")
    log(f"LOGS_DIR: {LOGS_DIR}")
    log(f"FINGERPRINT_PATH: {FINGERPRINT_PATH}")

    # Zorg dat directories bestaan (werkt op /tmp/data)
    ensure_dir(DATA_DIR)
    ensure_dir(LOGS_DIR)

    ensure_data_file(FINGERPRINT_PATH, {})

    if FORCE_TEST_PREBUY:
        test = build_prebuy("BTCUSDT", 99, {"setup_type": "TEST"}, 100.0)
        ok = send_to_webservice(test)
        log("✅ FORCE_TEST_PREBUY verzonden" if ok else "❌ FORCE_TEST_PREBUY kon niet verzonden worden")
        return

    fingerprints = load_json(FINGERPRINT_PATH, {})

    for symbol in COINS:
        try:
            klines = get_klines(symbol, INTERVAL, LIMIT)
            closes = closes_from_klines(klines)
            if len(closes) < 60:
                continue

            score, details = score_symbol(closes)
            if score < MIN_SCORE_TO_PREBUY:
                continue

            price = closes[-1]
            prebuy = build_prebuy(symbol, score, details, price)

            fp = make_fingerprint(
                symbol,
                prebuy["setup_type"],
                prebuy["entry"],
                prebuy["target"],
            )

            if is_duplicate(fp, fingerprints):
                log(f"⏭️ SKIP duplicate trade {symbol} {prebuy['setup_type']}")
                continue

            ok = send_to_webservice(prebuy)
            if ok:
                store_fingerprint(fp, symbol, prebuy["setup_type"])
                fingerprints[fp] = {"coin": symbol, "setup": prebuy["setup_type"], "ts": now_utc()}
                log(f"✅ Pre-BUY verzonden: {prebuy['id']}")

        except Exception:
            traceback.print_exc()

    log("✅ Run klaar")

if __name__ == "__main__":
    main()
