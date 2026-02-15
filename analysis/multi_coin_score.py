# analysis/multi_coin_score.py
from __future__ import annotations

import os
import sys
import time
import json
import math
import traceback
from typing import Any, Dict, List, Optional, Tuple

import requests
import psycopg2
import psycopg2.extras

# ==========================================================
# PROJECT ROOT (imports)
# ==========================================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ==========================================================
# ENV
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
USE_DB_APPROVALS = (os.getenv("USE_DB_APPROVALS") or "1").strip().lower() in {"1", "true", "yes", "on"}

# Cron-safe: NIET /data gebruiken (cron heeft vaak geen disk)
# Als je toch logs wilt: gebruik /tmp
SAFE_LOG_DIR = (os.getenv("SAFE_LOG_DIR") or "/tmp/crypto_ai").strip()

FORCE_TEST_PREBUY = (os.getenv("FORCE_TEST_PREBUY") or "0").strip() == "1"

PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))  # 4h
TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS") or str(6 * 60 * 60))  # 6h
MAX_PREBUY_PER_DAY = int(os.getenv("MAX_PREBUY_PER_DAY") or "5")

MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "80")    # GO
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "70")            # WATCH

# Universe
COINS = (os.getenv("COINS") or "BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,SOLUSDT").split(",")
COINS = [c.strip().upper() for c in COINS if c.strip()]

# Binance
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
HTTP_TIMEOUT = 15

TF_ENTRY = os.getenv("TF_ENTRY") or "1h"
TF_REGIME = os.getenv("TF_REGIME") or "4h"
LIMIT = int(os.getenv("LIMIT") or "200")

DEFAULT_SETUP_TYPE = os.getenv("DEFAULT_SETUP_TYPE") or "TREND_PULLBACK"


# ==========================================================
# DB
# ==========================================================
def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def ensure_tables():
    if not USE_DB_APPROVALS:
        return

    sql = """
    CREATE TABLE IF NOT EXISTS pending_approvals (
        id              TEXT PRIMARY KEY,
        symbol          TEXT NOT NULL,
        coin            TEXT,
        setup_type      TEXT,
        label           TEXT,
        score           INTEGER,
        grade           TEXT,
        entry           DOUBLE PRECISION,
        stop_loss       DOUBLE PRECISION,
        target          DOUBLE PRECISION,
        regime          TEXT,
        bot_confidence  INTEGER,
        payload         JSONB,
        status          TEXT NOT NULL DEFAULT 'PENDING',
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at      TIMESTAMPTZ NOT NULL,
        approved_amount DOUBLE PRECISION,
        approved_by     TEXT,
        approved_at     TIMESTAMPTZ,
        consumed_at     TIMESTAMPTZ,
        note            TEXT
    );

    CREATE TABLE IF NOT EXISTS trade_fingerprints (
        fingerprint     TEXT PRIMARY KEY,
        last_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS prebuy_state (
        day             DATE PRIMARY KEY,
        created_count   INTEGER NOT NULL DEFAULT 0
    );
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


# ==========================================================
# Helpers
# ==========================================================
def now_ts() -> int:
    return int(time.time())


def safe_mkdir(path: str) -> None:
    # /tmp is altijd ok
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass


def get_klines(symbol: str, interval: str, limit: int) -> List[List[Any]]:
    r = requests.get(
        BINANCE_KLINES_URL,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def closes_from_klines(klines: List[List[Any]]) -> List[float]:
    return [float(k[4]) for k in klines]  # close


def sma(values: List[float], period: int) -> float:
    if len(values) < period:
        return float("nan")
    return sum(values[-period:]) / float(period)


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
            losses += abs(diff)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def slope(values: List[float], period: int = 20) -> float:
    if len(values) < period + 1:
        return 0.0
    a = sma(values[:-1], period)
    b = sma(values, period)
    if math.isnan(a) or math.isnan(b) or a == 0:
        return 0.0
    return (b - a) / a


def classify_regime(closes_4h: List[float]) -> str:
    # simpel regime: SMA20 vs SMA50
    s20 = sma(closes_4h, 20)
    s50 = sma(closes_4h, 50)
    if math.isnan(s20) or math.isnan(s50):
        return "UNKNOWN"
    if s20 > s50:
        return "BULL"
    if s20 < s50:
        return "BEAR"
    return "RANGE"


def score_symbol(closes_1h: List[float], closes_4h: List[float]) -> Tuple[int, str, int]:
    """
    Return: (score 0-100, label GO/WATCH/SKIP, confidence 0-100)
    """
    s20 = sma(closes_1h, 20)
    s50 = sma(closes_1h, 50)
    rs = rsi(closes_1h, 14)
    sl = slope(closes_1h, 20)

    sc = 50

    # trend
    if not math.isnan(s20) and not math.isnan(s50):
        if s20 > s50:
            sc += 20
        else:
            sc -= 10

    # RSI zone
    if not math.isnan(rs):
        if 45 <= rs <= 65:
            sc += 15
        elif rs < 35:
            sc -= 5
        elif rs > 75:
            sc -= 10

    # slope
    if sl > 0:
        sc += 10
    else:
        sc -= 5

    # regime bonus/penalty
    regime = classify_regime(closes_4h)
    if regime == "BULL":
        sc += 5
    elif regime == "BEAR":
        sc -= 10

    sc = max(0, min(100, int(sc)))

    if sc >= MIN_SCORE_TO_PREBUY:
        return sc, "GO", min(100, sc)
    if sc >= WATCH_MIN_SCORE:
        return sc, "WATCH", min(100, sc)
    return sc, "SKIP", min(100, sc)


def grade_from_score(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    return "D"


# ==========================================================
# DB actions
# ==========================================================
def db_get_daily_count() -> int:
    ensure_tables()
    q = """
    SELECT created_count
    FROM prebuy_state
    WHERE day = CURRENT_DATE
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q)
            row = cur.fetchone()
            return int(row[0]) if row else 0


def db_inc_daily_count() -> None:
    ensure_tables()
    q = """
    INSERT INTO prebuy_state(day, created_count)
    VALUES (CURRENT_DATE, 1)
    ON CONFLICT (day) DO UPDATE
    SET created_count = prebuy_state.created_count + 1
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q)
        conn.commit()


def db_dedupe_allowed(fingerprint: str) -> bool:
    """
    True als deze fingerprint NIET recent is, anders False.
    """
    ensure_tables()

    # als fingerprint bestaat en recent: block
    q = """
    SELECT EXTRACT(EPOCH FROM (NOW() - last_created_at))::INT AS age_sec
    FROM trade_fingerprints
    WHERE fingerprint=%s
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (fingerprint,))
            row = cur.fetchone()
            if row:
                age = int(row[0] or 0)
                if age < TRADE_COOLDOWN_SECONDS:
                    return False

    return True


def db_touch_fingerprint(fingerprint: str) -> None:
    ensure_tables()
    q = """
    INSERT INTO trade_fingerprints(fingerprint, last_created_at)
    VALUES (%s, NOW())
    ON CONFLICT (fingerprint) DO UPDATE
    SET last_created_at = NOW()
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (fingerprint,))
        conn.commit()


def db_insert_prebuy(row: Dict[str, Any]) -> None:
    ensure_tables()
    q = """
    INSERT INTO pending_approvals(
        id, symbol, coin, setup_type, label, score, grade,
        entry, stop_loss, target, regime, bot_confidence,
        payload, status, expires_at
    )
    VALUES(
        %(id)s, %(symbol)s, %(coin)s, %(setup_type)s, %(label)s, %(score)s, %(grade)s,
        %(entry)s, %(stop_loss)s, %(target)s, %(regime)s, %(bot_confidence)s,
        %(payload)s, 'PENDING', NOW() + (%(valid_seconds)s || ' seconds')::interval
    )
    ON CONFLICT (id) DO NOTHING
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, row)
        conn.commit()


# ==========================================================
# MAIN
# ==========================================================
def main():
    safe_mkdir(SAFE_LOG_DIR)

    if not USE_DB_APPROVALS:
        raise RuntimeError("USE_DB_APPROVALS=0 maar jij wil Postgres. Zet USE_DB_APPROVALS=1.")

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt (Render env).")

    ensure_tables()

    # daglimiet
    created_today = db_get_daily_count()
    if created_today >= MAX_PREBUY_PER_DAY and not FORCE_TEST_PREBUY:
        print(f"ℹ️ MAX_PREBUY_PER_DAY bereikt ({created_today}/{MAX_PREBUY_PER_DAY}).", flush=True)
        return

    created_now = 0

    for symbol in COINS:
        try:
            # data
            kl_1h = get_klines(symbol, TF_ENTRY, LIMIT)
            kl_4h = get_klines(symbol, TF_REGIME, LIMIT)

            closes_1h = closes_from_klines(kl_1h)
            closes_4h = closes_from_klines(kl_4h)

            if len(closes_1h) < 60 or len(closes_4h) < 60:
                continue

            score, label, confidence = score_symbol(closes_1h, closes_4h)
            grade = grade_from_score(score)
            regime = classify_regime(closes_4h)

            # alleen GO/WATCH maken (SKIP sla je over)
            if not FORCE_TEST_PREBUY and label == "SKIP":
                continue

            entry = float(closes_1h[-1])
            stop_loss = entry * 0.98
            target = entry + 2.0 * (entry - stop_loss)

            setup_type = DEFAULT_SETUP_TYPE

            # fingerprint: zelfde trade = coin + setup + entry + target (afgerond)
            fp = f"{symbol}|{setup_type}|{round(entry,6)}|{round(target,6)}"
            if not db_dedupe_allowed(fp) and not FORCE_TEST_PREBUY:
                # duplicate within cooldown
                continue

            # daglimiet check
            created_today = db_get_daily_count()
            if created_today >= MAX_PREBUY_PER_DAY and not FORCE_TEST_PREBUY:
                break

            prebuy_id = f"PB-{symbol}-{now_ts()}"

            payload = {
                "symbol": symbol,
                "setup_type": setup_type,
                "label": label,
                "score": score,
                "grade": grade,
                "regime": regime,
                "entry": entry,
                "stop_loss": stop_loss,
                "target": target,
                "bot_confidence": confidence,
                "created_at_ts": now_ts(),
                "valid_seconds": PREBUY_VALID_SECONDS,
            }

            db_insert_prebuy({
                "id": prebuy_id,
                "symbol": symbol,
                "coin": symbol,
                "setup_type": setup_type,
                "label": label,
                "score": score,
                "grade": grade,
                "entry": entry,
                "stop_loss": stop_loss,
                "target": target,
                "regime": regime,
                "bot_confidence": confidence,
                "payload": json.dumps(payload),
                "valid_seconds": PREBUY_VALID_SECONDS,
            })

            db_touch_fingerprint(fp)
            db_inc_daily_count()

            created_now += 1
            print(f"✅ PREBUY created: {prebuy_id} {symbol} {label} score={score} entry={entry}", flush=True)

        except Exception as e:
            print(f"⚠️ ERROR {symbol}: {e}", flush=True)
            print(traceback.format_exc(), flush=True)

    print(f"done. created={created_now}", flush=True)


if __name__ == "__main__":
    main()
