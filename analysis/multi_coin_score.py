from __future__ import annotations

import os
import sys
import json
import time
import csv
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

# WATCH drempel: trade wél melden + opslaan als ervaring, maar label als WATCH
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "70")

# cooldown in seconden (bijv. 6 uur)
TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS") or str(6 * 60 * 60))

# ==========================================================
# DATA PATHS (ALTIJD via ENV)
# ==========================================================
DATA_DIR = (os.getenv("DATA_DIR") or "/tmp/data").rstrip("/")
LOGS_DIR = (os.getenv("LOGS_DIR") or os.path.join(DATA_DIR, "logs")).rstrip("/")

FINGERPRINT_PATH = os.getenv(
    "FINGERPRINT_PATH",
    os.path.join(DATA_DIR, "fingerprints.json"),
)

# Experience log (CSV) – GO én WATCH worden opgeslagen
EXPERIENCE_CSV = os.getenv(
    "EXPERIENCE_CSV",
    os.path.join(DATA_DIR, "experience.csv"),
)

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

def now_utc() -> int:
    return int(time.time())

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def ensure_data_file(path: str, default: Any) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)

def load_json(path: str, default: Any) -> Any:
    ensure_data_file(path, default)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
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
        timeout=20,
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

def slope_sma(values: List[float], period: int, lookback: int = 6) -> Optional[float]:
    """Simpel slope signaal: SMA nu - SMA lookback candles geleden."""
    if len(values) < period + lookback:
        return None
    now_val = sum(values[-period:]) / period
    prev_val = sum(values[-period - lookback : -lookback]) / period
    return now_val - prev_val

# ==========================================================
# REGIME / SETUP & SCORING
# ==========================================================
def determine_market_regime(s20: float, s50: float, s20_slope: float) -> str:
    # simpel maar bruikbaar: bull/bear/range
    if s20 > s50 and s20_slope > 0:
        return "BULL"
    if s20 < s50 and s20_slope < 0:
        return "BEAR"
    return "RANGE"

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
    s20_sl = slope_sma(closes, 20, lookback=6)

    if s20 is None or s50 is None or r14 is None or s20_sl is None:
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
    regime = determine_market_regime(s20, s50, s20_sl)

    return max(0, min(100, score)), {
        "sma20": s20,
        "sma50": s50,
        "rsi14": r14,
        "setup_type": setup,
        "market_regime": regime,
        "sma20_slope": s20_sl,
    }

# ==========================================================
# FINGERPRINT LOGIC (ANTI-DUPLICATE)
# ==========================================================
def make_fingerprint(coin: str, setup: str, entry: float, target: float) -> str:
    raw = f"{coin}|{setup}|{round(entry,3)}|{round(target,3)}"
    return hashlib.sha256(raw.encode()).hexdigest()

def is_duplicate(fp: str, fingerprints: Dict[str, Any]) -> bool:
    rec = fingerprints.get(fp)
    if not rec:
        return False
    return (now_utc() - rec["ts"]) < TRADE_COOLDOWN_SECONDS

def store_fingerprint(fp: str, coin: str, setup: str, grade: str) -> None:
    fingerprints = load_json(FINGERPRINT_PATH, {})
    fingerprints[fp] = {
        "coin": coin,
        "setup": setup,
        "grade": grade,
        "ts": now_utc(),
    }
    save_json(FINGERPRINT_PATH, fingerprints)

# ==========================================================
# EXPERIENCE CSV (GO + WATCH opslaan)
# ==========================================================
EXPERIENCE_HEADERS = [
    "timestamp",
    "coin",
    "setup_type",
    "market_regime",
    "grade",             # GO / WATCH
    "entry",
    "stop",
    "target",
    "decision",          # user decision later (BUY/NO/...) -> nu leeg
    "outcome",           # later invullen door simulatie/monitor
    "mfe",               # later
    "mae",               # later
    "time_minutes",      # later
    "why",
    "market_condition",
    "bot_confidence",
    "overextended",
]

def ensure_experience_csv() -> None:
    ensure_dir(os.path.dirname(EXPERIENCE_CSV) or ".")
    if not os.path.exists(EXPERIENCE_CSV):
        with open(EXPERIENCE_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(EXPERIENCE_HEADERS)

def append_experience_row(row: Dict[str, Any]) -> None:
    ensure_experience_csv()
    with open(EXPERIENCE_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([row.get(h, "") for h in EXPERIENCE_HEADERS])

def calc_overextended(price: float, sma20_val: float) -> float:
    if sma20_val <= 0:
        return 0.0
    return round(((price - sma20_val) / sma20_val) * 100.0, 3)  # % boven SMA20

# ==========================================================
# PREBUY / WEBHOOK (optioneel)
# ==========================================================
def build_prebuy(symbol: str, score: int, details: Dict[str, Any], price: float) -> Dict[str, Any]:
    created = now_utc()
    stop = price * 0.98
    target = price + (price - stop) * 2

    grade = "GO" if score >= MIN_SCORE_TO_PREBUY else "WATCH"

    return {
        "id": f"PB-{symbol}-{created}",
        "coin": symbol,
        "setup_type": details.get("setup_type", "UNKNOWN"),
        "market_regime": details.get("market_regime", ""),
        "score": score,
        "grade": grade,
        "status": "PENDING",
        "created_at": created,
        "expires_at": created + PREBUY_VALID_SECONDS,
        "entry": round(price, 8),
        "stop_loss": round(stop, 8),
        "target": round(target, 8),
        "details": details,
    }

def send_to_webservice(prebuy: Dict[str, Any]) -> bool:
    # Als je webhook nog niet wilt gebruiken, laat WEBHOOK_BASE_URL leeg -> dan slaat hij dit over
    if not WEBHOOK_BASE_URL or not INTERNAL_TOKEN:
        return False

    try:
        r = requests.post(
            f"{WEBHOOK_BASE_URL}/internal/prebuy",
            json=prebuy,
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=20,
        )
        return r.status_code < 300
    except Exception:
        return False

# ==========================================================
# MAIN
# ==========================================================
def main() -> None:
    log("🚀 multi_coin_score gestart")

    # folders + files
    ensure_dir(DATA_DIR)
    ensure_dir(LOGS_DIR)
    ensure_data_file(FINGERPRINT_PATH, {})
    ensure_experience_csv()

    fingerprints = load_json(FINGERPRINT_PATH, {})

    # Force test: maakt 1 fake prebuy + schrijft ervaring weg
    if FORCE_TEST_PREBUY:
        fake_details = {
            "setup_type": "TEST",
            "market_regime": "RANGE",
            "sma20": 100.0,
            "sma50": 100.0,
            "rsi14": 55.0,
            "sma20_slope": 0.0,
        }
        prebuy = build_prebuy("BTCUSDT", 99, fake_details, 100.0)
        append_experience_row({
            "timestamp": prebuy["created_at"],
            "coin": prebuy["coin"],
            "setup_type": prebuy["setup_type"],
            "market_regime": prebuy["market_regime"],
            "grade": prebuy["grade"],
            "entry": prebuy["entry"],
            "stop": prebuy["stop_loss"],
            "target": prebuy["target"],
            "decision": "",
            "outcome": "",
            "mfe": "",
            "mae": "",
            "time_minutes": "",
            "why": "FORCE_TEST_PREBUY",
            "market_condition": "test",
            "bot_confidence": prebuy["score"],
            "overextended": 0.0,
        })
        send_to_webservice(prebuy)
        log("✅ FORCE_TEST_PREBUY: 1 test Pre-BUY + experience opgeslagen")
        return

    for symbol in COINS:
        try:
            klines = get_klines(symbol, INTERVAL, LIMIT)
            closes = closes_from_klines(klines)
            if len(closes) < 60:
                continue

            score, details = score_symbol(closes)
            if not details:
                continue

            # Alleen loggen/melden als score minimaal WATCH niveau haalt
            if score < WATCH_MIN_SCORE:
                continue

            price = closes[-1]
            prebuy = build_prebuy(symbol, score, details, price)

            fp = make_fingerprint(symbol, prebuy["setup_type"], prebuy["entry"], prebuy["target"])
            if is_duplicate(fp, fingerprints):
                log(f"⏭️ Skip duplicate {symbol} {prebuy['setup_type']} ({prebuy['grade']})")
                continue

            # 1) Opslaan als ervaring (GO én WATCH)
            overext = calc_overextended(price, float(details["sma20"]))
            append_experience_row({
                "timestamp": prebuy["created_at"],
                "coin": prebuy["coin"],
                "setup_type": prebuy["setup_type"],
                "market_regime": prebuy["market_regime"],
                "grade": prebuy["grade"],
                "entry": prebuy["entry"],
                "stop": prebuy["stop_loss"],
                "target": prebuy["target"],
                "decision": "",          # wordt later gevuld bij WhatsApp YES/NO
                "outcome": "",           # wordt later gevuld door simulatie/monitor
                "mfe": "",
                "mae": "",
                "time_minutes": "",
                "why": prebuy["setup_type"],
                "market_condition": "normal",
                "bot_confidence": score,  # 0-100
                "overextended": overext,
            })

            # 2) Optioneel naar WhatsApp-webservice sturen (als env gevuld is)
            sent = send_to_webservice(prebuy)

            # 3) Fingerprint opslaan (anti-duplicate)
            store_fingerprint(fp, symbol, prebuy["setup_type"], prebuy["grade"])

            if sent:
                log(f"✅ Pre-BUY verzonden ({prebuy['grade']}): {symbol} {prebuy['setup_type']} score={score}")
            else:
                log(f"✅ Experience opgeslagen ({prebuy['grade']}): {symbol} {prebuy['setup_type']} score={score}")

        except Exception:
            traceback.print_exc()

    log("✅ Run klaar")

if __name__ == "__main__":
    main()
