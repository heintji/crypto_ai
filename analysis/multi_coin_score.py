from __future__ import annotations

import os
import sys
import json
import time
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

# =========================================================
# PROJECT ROOT FIX
# - dit bestand zit in /analysis/
# - root is dus 1 map omhoog
# =========================================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# =========================================================
# PATHS (BELANGRIJK)
# 1) Standaard: <project_root>/data/pending_approvals.json
# 2) Override mogelijk via ENV: PENDING_FILE
# =========================================================
DEFAULT_PENDING = os.path.join(PROJECT_ROOT, "data", "pending_approvals.json")
PENDING_FILE = os.getenv("PENDING_FILE", DEFAULT_PENDING)

DEFAULT_LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
LOG_DIR = os.getenv("LOG_DIR", DEFAULT_LOG_DIR)
AI_ADVICE_LOG = os.path.join(LOG_DIR, "ai_advice.csv")

# =========================================================
# SETTINGS
# =========================================================
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

TF_MAIN = os.getenv("TF_MAIN", "4h")   # hoofd timeframe voor score
TF_CTX = os.getenv("TF_CTX", "1h")     # context timeframe
LIMIT_MAIN = int(os.getenv("LIMIT_MAIN", "200"))
LIMIT_CTX = int(os.getenv("LIMIT_CTX", "200"))

MAX_PREBUY_PER_RUN = int(os.getenv("MAX_PREBUY_PER_RUN", "1"))   # veilig: max 1 per run
PREBUY_TTL_SECONDS = int(os.getenv("PREBUY_TTL_SECONDS", str(4 * 60 * 60)))  # 4 uur

MIN_SCORE_TO_NOTIFY = float(os.getenv("MIN_SCORE_TO_NOTIFY", "80"))

# Test switch (Render env var: FORCE_TEST_PREBUY=1)
FORCE_TEST_PREBUY = os.getenv("FORCE_TEST_PREBUY", "0") == "1"

# =========================================================
# DATA STRUCTURES
# =========================================================
@dataclass
class Candle:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float


# =========================================================
# UTILS
# =========================================================
def utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_dirs() -> None:
    os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def safe_float(x: Any) -> float:
    try:
        return float(x)
    except Exception:
        return float("nan")


# =========================================================
# FILE IO
# =========================================================
def load_pending() -> List[Dict[str, Any]]:
    ensure_dirs()
    if not os.path.isfile(PENDING_FILE):
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)
        return []

    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_pending(items: List[Dict[str, Any]]) -> None:
    ensure_dirs()
    tmp = PENDING_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    os.replace(tmp, PENDING_FILE)


def pending_has_test_pending(items: List[Dict[str, Any]]) -> bool:
    for it in items:
        if it.get("setup") == "TEST" and str(it.get("status", "")).upper() == "PENDING":
            return True
    return False


# =========================================================
# BINANCE FETCH
# =========================================================
def fetch_klines(symbol: str, interval: str, limit: int) -> List[Candle]:
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(BINANCE_KLINES, params=params, timeout=20)
    r.raise_for_status()
    raw = r.json()
    out: List[Candle] = []
    for row in raw:
        # row = [openTime, open, high, low, close, volume, closeTime, ...]
        out.append(
            Candle(
                t=int(row[0]),
                o=float(row[1]),
                h=float(row[2]),
                l=float(row[3]),
                c=float(row[4]),
                v=float(row[5]),
            )
        )
    return out


# =========================================================
# INDICATORS (SMA + RSI)
# =========================================================
def sma(values: List[float], length: int) -> Optional[float]:
    if len(values) < length or length <= 0:
        return None
    return sum(values[-length:]) / float(length)


def rsi(values: List[float], length: int = 14) -> Optional[float]:
    if len(values) < length + 1:
        return None

    gains = 0.0
    losses = 0.0
    for i in range(-length, 0):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += abs(diff)

    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


# =========================================================
# SCORING LOGIC (simpel, stabiel, debugbaar)
# =========================================================
def trend_label(sma20: Optional[float], sma50: Optional[float]) -> str:
    if sma20 is None or sma50 is None:
        return "UNKNOWN"
    if sma20 > sma50:
        return "UP"
    if sma20 < sma50:
        return "DOWN"
    return "FLAT"


def score_symbol(candles_main: List[Candle], candles_ctx: List[Candle]) -> Tuple[float, Dict[str, Any]]:
    closes_main = [c.c for c in candles_main]
    closes_ctx = [c.c for c in candles_ctx]

    sma20 = sma(closes_main, 20)
    sma50 = sma(closes_main, 50)
    rsi14 = rsi(closes_main, 14)

    sma20_ctx = sma(closes_ctx, 20)
    sma50_ctx = sma(closes_ctx, 50)

    t_main = trend_label(sma20, sma50)
    t_ctx = trend_label(sma20_ctx, sma50_ctx)

    score = 50.0  # baseline

    # Trend bonus
    if t_main == "UP":
        score += 20
    elif t_main == "DOWN":
        score -= 20

    # Context bonus
    if t_ctx == "UP":
        score += 10
    elif t_ctx == "DOWN":
        score -= 10

    # RSI heuristics
    if rsi14 is not None:
        if 45 <= rsi14 <= 65:
            score += 10  # “gezond”
        elif rsi14 < 35:
            score -= 5   # zwak / risk
        elif rsi14 > 75:
            score -= 5   # oververhit

    # Clamp 0..100
    score = max(0.0, min(100.0, score))

    debug = {
        "sma20": sma20,
        "sma50": sma50,
        "rsi14": rsi14,
        "trend_main": t_main,
        "trend_ctx": t_ctx,
        "last_close": closes_main[-1] if closes_main else None,
    }
    return score, debug


# =========================================================
# PRE-BUY CREATION
# =========================================================
def make_prebuy(symbol: str, score: float, debug: Dict[str, Any]) -> Dict[str, Any]:
    now_s = int(time.time())
    expires = now_s + PREBUY_TTL_SECONDS

    entry = float(debug.get("last_close") or 0.0)
    # simpele placeholders; jouw trade_monitor/exit logica bepaalt later de echte regels
    stop_loss = entry * 0.98 if entry > 0 else 0.0
    target = entry * 1.04 if entry > 0 else 0.0

    prebuy_id = f"PB-{symbol}-{now_s}"

    return {
        "id": prebuy_id,
        "coin": symbol,
        "setup": "SCORE",
        "score": round(float(score), 2),
        "kans": "hoog" if score >= 90 else "boven_gemiddeld" if score >= 80 else "gemiddeld" if score >= 70 else "laag",
        "entry": round(entry, 8),
        "stop_loss": round(stop_loss, 8),
        "target": round(target, 8),
        "status": "PENDING",
        "created_at": utc_iso(),
        "expires_at": expires,
        "debug": debug,
    }


def append_prebuy(prebuy: Dict[str, Any]) -> None:
    items = load_pending()
    items.insert(0, prebuy)
    save_pending(items)


# =========================================================
# AI ADVICE LOG (simpel CSV)
# =========================================================
def append_ai_log(symbol: str, score: float, debug: Dict[str, Any]) -> None:
    ensure_dirs()
    header_needed = not os.path.isfile(AI_ADVICE_LOG)

    line = (
        f"{utc_iso()},"
        f"{symbol},"
        f"{score:.2f},"
        f"{debug.get('trend_main','')},"
        f"{debug.get('trend_ctx','')},"
        f"{'' if debug.get('rsi14') is None else round(float(debug['rsi14']), 2)}\n"
    )

    if header_needed:
        with open(AI_ADVICE_LOG, "w", encoding="utf-8") as f:
            f.write("utc,symbol,score,trend_main,trend_ctx,rsi14\n")
            f.write(line)
    else:
        with open(AI_ADVICE_LOG, "a", encoding="utf-8") as f:
            f.write(line)


# =========================================================
# TEST PRE-BUY (via env FORCE_TEST_PREBUY=1)
# =========================================================
def force_test_prebuy() -> None:
    items = load_pending()

    # voorkom spam: max 1 PENDING TEST tegelijk
    if pending_has_test_pending(items):
        print("🟡 TEST prebuy bestaat al (PENDING) — geen nieuwe gemaakt.")
        return

    now_s = int(time.time())
    test = {
        "id": f"PB-TEST-{now_s}",
        "coin": "BTCUSDT",
        "setup": "TEST",
        "score": 99.0,
        "kans": "test",
        "entry": 100.0,
        "stop_loss": 95.0,
        "target": 110.0,
        "status": "PENDING",
        "created_at": utc_iso(),
        "expires_at": now_s + PREBUY_TTL_SECONDS,
    }

    items.insert(0, test)
    save_pending(items)

    print(f"✅ TEST PRE-BUY gemaakt: {test['id']} (PENDING)")


# =========================================================
# MAIN
# =========================================================
def main() -> None:
    ensure_dirs()

    print("📌 multi_coin_score gestart (Pre-BUY only)")
    print(f"🕒 UTC time: {utc_iso()}")
    print(f"📌 Pending file: {PENDING_FILE}")
    print(f"📌 AI log: {AI_ADVICE_LOG}")

    # 1) Run echte scoring (mag 0 prebuys opleveren)
    new_prebuys = 0
    scored: List[Tuple[str, float, Dict[str, Any]]] = []

    for sym in COINS:
        try:
            candles_main = fetch_klines(sym, TF_MAIN, LIMIT_MAIN)
            candles_ctx = fetch_klines(sym, TF_CTX, LIMIT_CTX)

            score, debug = score_symbol(candles_main, candles_ctx)
            scored.append((sym, score, debug))

            append_ai_log(sym, score, debug)

        except Exception as e:
            print(f"⚠️ {sym} fout: {e}")

    # beste eerst
    scored.sort(key=lambda x: x[1], reverse=True)

    for sym, score, debug in scored:
        if new_prebuys >= MAX_PREBUY_PER_RUN:
            break
        if score < MIN_SCORE_TO_NOTIFY:
            continue

        prebuy = make_prebuy(sym, score, debug)
        append_prebuy(prebuy)
        new_prebuys += 1

        print(f"✅ Pre-BUY toegevoegd: {prebuy['id']} | {sym} | score={score:.2f}")

    print(f"✅ Pre-BUY run klaar — {new_prebuys} nieuwe Pre-BUY(s).")

    # 2) Force test prebuy (alleen als env aan staat)
    if FORCE_TEST_PREBUY:
        force_test_prebuy()


if __name__ == "__main__":
    main()
