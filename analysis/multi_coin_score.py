from __future__ import annotations

import os
import sys
import json
import time
import math
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import psycopg2

# ==========================================================
# PROJECT ROOT
# ==========================================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ==========================================================
# ENV / SETTINGS
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL".lower()) or "").strip()
USE_DB_APPROVALS = (os.getenv("USE_DB_APPROVALS") or "1").strip().lower() in {"1", "true", "yes", "on"}

DATA_DIR = (os.getenv("DATA_DIR") or "/data").strip()
LOGS_DIR = (os.getenv("LOGS_DIR") or os.path.join(DATA_DIR, "logs")).strip()

INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()

# Test / Debug
FORCE_TEST_PREBUY = (os.getenv("FORCE_TEST_PREBUY") or "0").strip().lower() in {"1", "true", "yes", "on"}
DEBUG = (os.getenv("DEBUG") or "0").strip().lower() in {"1", "true", "yes", "on"}

# Pre-buy logic
PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))  # 4h
MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "80")  # GO
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "70")          # WATCH
MAX_PREBUY_PER_DAY = int(os.getenv("MAX_PREBUY_PER_DAY") or "5")     # cap per dag
TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS") or str(6 * 60 * 60))  # 6h

# Universe
COINS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "ADAUSDT", "DOGEUSDT"
]
COINS_ENV = (os.getenv("COINS") or "").strip()
if COINS_ENV:
    COINS = [c.strip().upper() for c in COINS_ENV.split(",") if c.strip()]

# Binance data
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT") or "15")

# Timeframes
ENTRY_INTERVAL = (os.getenv("ENTRY_INTERVAL") or "1h").strip()
ENTRY_LIMIT = int(os.getenv("ENTRY_LIMIT") or "200")
REGIME_INTERVAL = (os.getenv("REGIME_INTERVAL") or "4h").strip()
REGIME_LIMIT = int(os.getenv("REGIME_LIMIT") or "200")

# Setup labels (voor nu simpel; later uitbreiden met jouw TREND_PULLBACK/BREAKOUT_RETEST)
DEFAULT_SETUP_TYPE = (os.getenv("DEFAULT_SETUP_TYPE") or "TREND_PULLBACK").strip()

# ==========================================================
# UTILS
# ==========================================================
def _now_ts() -> int:
    return int(time.time())

def _utc_iso(ts: Optional[int] = None) -> str:
    if ts is None:
        ts = _now_ts()
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

def _ensure_dir(path: str) -> None:
    if not path:
        return
    os.makedirs(path, exist_ok=True)

def _log(msg: str) -> None:
    print(msg, flush=True)

def _dbg(msg: str) -> None:
    if DEBUG:
        _log(f"DEBUG: {msg}")

def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

# ==========================================================
# DB
# ==========================================================
def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt (Render env var).")
    return psycopg2.connect(DATABASE_URL)

def db_init_if_needed() -> None:
    """
    Maakt tabel aan als die nog niet bestaat.
    (Jij had 'pending_approvals' al in Postgres; dit is veilig.)
    """
    if not USE_DB_APPROVALS:
        raise RuntimeError("USE_DB_APPROVALS moet aan staan voor DB-only flow.")

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pending_approvals (
        id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        coin TEXT,
        setup_type TEXT,
        regime TEXT,
        market_regime TEXT,
        score INTEGER NOT NULL,
        grade TEXT,
        label TEXT,
        entry DOUBLE PRECISION NOT NULL,
        stop_loss DOUBLE PRECISION NOT NULL,
        target DOUBLE PRECISION NOT NULL,
        bot_confidence INTEGER,
        payload_json TEXT,
        status TEXT NOT NULL DEFAULT 'PENDING',
        created_at BIGINT NOT NULL,
        expires_at BIGINT NOT NULL
    );
    """)
    conn.commit()
    cur.close()
    conn.close()

def db_count_today_prebuys() -> int:
    """
    Telt hoeveel prebuys vandaag zijn aangemaakt (UTC).
    """
    conn = db_conn()
    cur = conn.cursor()
    # start of today UTC
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    start_ts = int(start.timestamp())
    cur.execute("SELECT COUNT(*) FROM pending_approvals WHERE created_at >= %s;", (start_ts,))
    n = int(cur.fetchone()[0] or 0)
    cur.close()
    conn.close()
    return n

def db_recent_similar_exists(symbol: str, setup_type: str, entry: float, target: float) -> bool:
    """
    Dedup rule: dezelfde trade (coin+setup_type+entry+target) mag niet opnieuw
    (ook niet met andere ID). We pakken rounding om floating-ruis te dempen.
    """
    entry_r = round(entry, 6)
    target_r = round(target, 6)

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT 1
        FROM pending_approvals
        WHERE symbol=%s
          AND COALESCE(setup_type,'')=%s
          AND ROUND(entry::numeric, 6)=ROUND(%s::numeric, 6)
          AND ROUND(target::numeric, 6)=ROUND(%s::numeric, 6)
        LIMIT 1;
    """, (symbol, setup_type, entry_r, target_r))
    exists = cur.fetchone() is not None
    cur.close()
    conn.close()
    return exists

def db_recent_cooldown_hit(symbol: str, cooldown_seconds: int) -> bool:
    """
    Cooldown: voorkom spam op dezelfde coin binnen X tijd.
    """
    if cooldown_seconds <= 0:
        return False
    cutoff = _now_ts() - cooldown_seconds

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT 1
        FROM pending_approvals
        WHERE symbol=%s AND created_at >= %s
        LIMIT 1;
    """, (symbol, cutoff))
    hit = cur.fetchone() is not None
    cur.close()
    conn.close()
    return hit

def db_insert_prebuy(row: Dict[str, Any]) -> None:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO pending_approvals
        (id, symbol, coin, setup_type, regime, market_regime, score, grade, label,
         entry, stop_loss, target, bot_confidence, payload_json, status, created_at, expires_at)
        VALUES
        (%(id)s, %(symbol)s, %(coin)s, %(setup_type)s, %(regime)s, %(market_regime)s, %(score)s, %(grade)s, %(label)s,
         %(entry)s, %(stop_loss)s, %(target)s, %(bot_confidence)s, %(payload_json)s, %(status)s, %(created_at)s, %(expires_at)s)
    """, row)
    conn.commit()
    cur.close()
    conn.close()

# ==========================================================
# MARKET DATA
# ==========================================================
def get_price(symbol: str) -> float:
    r = requests.get(BINANCE_TICKER_URL, params={"symbol": symbol}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return float(r.json()["price"])

def get_klines(symbol: str, interval: str, limit: int) -> List[List[Any]]:
    r = requests.get(
        BINANCE_KLINES_URL,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    return r.json()

def closes_from_klines(klines: List[List[Any]]) -> List[float]:
    return [float(k[4]) for k in klines]

def lows_from_klines(klines: List[List[Any]]) -> List[float]:
    return [float(k[3]) for k in klines]

# ==========================================================
# INDICATORS (simpel + stabiel)
# ==========================================================
def sma(values: List[float], period: int) -> float:
    if len(values) < period:
        return float("nan")
    return sum(values[-period:]) / period

def rsi(values: List[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return float("nan")
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))

def slope(values: List[float], lookback: int = 10) -> float:
    if len(values) < lookback:
        return 0.0
    start = values[-lookback]
    end = values[-1]
    if start == 0:
        return 0.0
    return (end - start) / abs(start)

# ==========================================================
# REGIME LABEL (bull/bear/range)
# ==========================================================
def detect_regime(symbol: str) -> str:
    """
    Simpel regime: SMA20 vs SMA50 op 4h + slope.
    """
    kl = get_klines(symbol, REGIME_INTERVAL, REGIME_LIMIT)
    c = closes_from_klines(kl)
    s20 = sma(c, 20)
    s50 = sma(c, 50)
    sl = slope(c, 20)

    if math.isnan(s20) or math.isnan(s50):
        return "UNKNOWN"

    if s20 > s50 and sl > 0:
        return "BULL"
    if s20 < s50 and sl < 0:
        return "BEAR"
    return "RANGE"

# ==========================================================
# SCORE / LABEL
# ==========================================================
@dataclass
class ScoreResult:
    score: int
    label: str       # GO / WATCH / SKIP
    grade: str
    bot_confidence: int
    stop_loss: float
    target: float
    entry: float
    payload: Dict[str, Any]

def compute_score(symbol: str, setup_type: str) -> ScoreResult:
    """
    1h entry timeframe score.
    - Trend: SMA20 vs SMA50
    - Momentum: RSI14
    - Slope: SMA20 slope
    """
    kl = get_klines(symbol, ENTRY_INTERVAL, ENTRY_LIMIT)
    closes = closes_from_klines(kl)

    entry = closes[-1]
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    r = rsi(closes, 14)
    sl = slope(closes, 20)

    score = 50

    # Trend
    if not math.isnan(s20) and not math.isnan(s50):
        if s20 > s50:
            score += 20
        else:
            score -= 10

    # RSI
    if not math.isnan(r):
        if 45 <= r <= 65:
            score += 15
        elif r < 35:
            score += 5  # oversold bounce mogelijk
        elif r > 75:
            score -= 10

    # Slope
    if sl > 0.01:
        score += 10
    elif sl < -0.01:
        score -= 10

    # Clamp
    score = max(0, min(100, int(score)))

    # Label
    if score >= MIN_SCORE_TO_PREBUY:
        label = "GO"
    elif score >= WATCH_MIN_SCORE:
        label = "WATCH"
    else:
        label = "SKIP"

    # Grade (simple)
    if score >= 90:
        grade = "A"
    elif score >= 80:
        grade = "B"
    elif score >= 70:
        grade = "C"
    else:
        grade = "D"

    bot_confidence = score  # voor nu gelijk aan score

    # Risk model (default):
    # stop_loss = 2% onder entry, target = 2R (4% boven entry)
    stop_loss = entry * 0.98
    risk = entry - stop_loss
    target = entry + (2.0 * risk)

    payload = {
        "symbol": symbol,
        "setup_type": setup_type,
        "timeframe": ENTRY_INTERVAL,
        "entry": entry,
        "sma20": s20,
        "sma50": s50,
        "rsi14": r,
        "slope20": sl,
        "score": score,
        "grade": grade,
        "label": label,
        "stop_loss": stop_loss,
        "target": target,
        "created_at": _utc_iso(),
    }

    return ScoreResult(
        score=score,
        label=label,
        grade=grade,
        bot_confidence=bot_confidence,
        stop_loss=stop_loss,
        target=target,
        entry=entry,
        payload=payload,
    )

# ==========================================================
# MAIN FLOW
# ==========================================================
def build_prebuy_row(symbol: str, setup_type: str, regime: str, res: ScoreResult) -> Dict[str, Any]:
    ts = _now_ts()
    prebuy_id = f"PB-{symbol}-{ts}"

    return {
        "id": prebuy_id,
        "symbol": symbol,
        "coin": symbol,
        "setup_type": setup_type,
        "regime": regime,
        "market_regime": regime,
        "score": int(res.score),
        "grade": res.grade,
        "label": res.label,
        "entry": float(res.entry),
        "stop_loss": float(res.stop_loss),
        "target": float(res.target),
        "bot_confidence": int(res.bot_confidence),
        "payload_json": json.dumps(res.payload, ensure_ascii=False),
        "status": "PENDING",
        "created_at": int(ts),
        "expires_at": int(ts + PREBUY_VALID_SECONDS),
    }

def run() -> None:
    _ensure_dir(LOGS_DIR)

    if not USE_DB_APPROVALS:
        raise RuntimeError("Deze versie is DB-only. Zet USE_DB_APPROVALS=1.")

    db_init_if_needed()

    todays = db_count_today_prebuys()
    _log(f"📊 Prebuys vandaag (DB): {todays}/{MAX_PREBUY_PER_DAY}")

    if todays >= MAX_PREBUY_PER_DAY and not FORCE_TEST_PREBUY:
        _log("⛔ MAX_PREBUY_PER_DAY bereikt → geen nieuwe prebuys.")
        return

    created = 0

    for symbol in COINS:
        try:
            if not FORCE_TEST_PREBUY:
                if db_recent_cooldown_hit(symbol, TRADE_COOLDOWN_SECONDS):
                    _dbg(f"Cooldown hit: {symbol}")
                    continue

            regime = detect_regime(symbol)
            setup_type = DEFAULT_SETUP_TYPE

            res = compute_score(symbol, setup_type)

            # force test can bypass SKIP
            if res.label == "SKIP" and not FORCE_TEST_PREBUY:
                _dbg(f"SKIP {symbol} score={res.score}")
                continue

            row = build_prebuy_row(symbol, setup_type, regime, res)

            # Dedup: exact dezelfde trade (coin+setup+entry+target) nooit nog een keer
            if not FORCE_TEST_PREBUY:
                if db_recent_similar_exists(symbol, setup_type, row["entry"], row["target"]):
                    _dbg(f"Dedup hit (zelfde trade): {symbol} {setup_type} entry/target")
                    continue

            db_insert_prebuy(row)
            created += 1
            _log(
                f"✅ PREBUY DB: {row['id']} | {symbol} | {row['label']} | score={row['score']} | "
                f"entry={row['entry']:.6f} | stop={row['stop_loss']:.6f} | target={row['target']:.6f} | exp={int((row['expires_at']-_now_ts())/60)}m"
            )

            if db_count_today_prebuys() >= MAX_PREBUY_PER_DAY and not FORCE_TEST_PREBUY:
                _log("⛔ MAX_PREBUY_PER_DAY bereikt tijdens run → stop.")
                break

        except Exception as e:
            _log(f"⚠️ Fout bij {symbol}: {e}")
            if DEBUG:
                _log(traceback.format_exc())

    _log(f"🏁 Klaar. Nieuwe prebuys aangemaakt: {created}")

if __name__ == "__main__":
    run()
