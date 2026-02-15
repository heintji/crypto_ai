from __future__ import annotations

import os
import time
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests
import psycopg2
import psycopg2.extras

# ==========================================================
# ENV
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL ontbreekt (Render Postgres).")

WEBHOOK_BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or "").strip().rstrip("/")  # optioneel als je direct WhatsApp wil pushen
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()

FORCE_TEST_PREBUY = (os.getenv("FORCE_TEST_PREBUY") or "0").strip() == "1"

PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))
MAX_PREBUY_PER_DAY = int(os.getenv("MAX_PREBUY_PER_DAY") or "5")

MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "80")
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "70")

TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS") or str(6 * 60 * 60))

HTTP_TIMEOUT = 15

# ==========================================================
# MARKETS / DATA
# ==========================================================
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

COINS = (os.getenv("COINS") or "BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT,ADAUSDT,SOLUSDT,DOGEUSDT").split(",")
COINS = [c.strip().upper() for c in COINS if c.strip()]

INTERVAL = os.getenv("INTERVAL", "1h").strip()
LIMIT = int(os.getenv("LIMIT") or "200")

# ==========================================================
# DB
# ==========================================================
def db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def db_init() -> None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            # Prebuy queue
            cur.execute("""
            CREATE TABLE IF NOT EXISTS pending_approvals (
                id              TEXT PRIMARY KEY,
                symbol          TEXT NOT NULL,
                setup_type      TEXT,
                label           TEXT,
                score           INTEGER,
                grade           TEXT,
                entry           DOUBLE PRECISION,
                stop_loss       DOUBLE PRECISION,
                target          DOUBLE PRECISION,
                regime          TEXT,

                status          TEXT NOT NULL DEFAULT 'PENDING',
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at      TIMESTAMPTZ NOT NULL,

                approved_amount DOUBLE PRECISION,
                approved_at     TIMESTAMPTZ,
                consumed_at     TIMESTAMPTZ,
                rejected_at     TIMESTAMPTZ
            );
            """)
            cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending_approvals_status_expires
            ON pending_approvals(status, expires_at);
            """)

            # Dedupe fingerprints
            cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_fingerprints (
                fingerprint     TEXT PRIMARY KEY,
                last_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """)

            # Daily state (daglimiet)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS prebuy_state (
                day           DATE PRIMARY KEY,
                created_count INTEGER NOT NULL DEFAULT 0
            );
            """)

        conn.commit()

# ==========================================================
# Helpers
# ==========================================================
def now_ts() -> int:
    return int(time.time())

def get_klines(symbol: str) -> List[List[Any]]:
    r = requests.get(
        BINANCE_KLINES_URL,
        params={"symbol": symbol, "interval": INTERVAL, "limit": LIMIT},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()

def sma(values: List[float], period: int) -> float:
    if len(values) < period:
        return float("nan")
    return sum(values[-period:]) / float(period)

def rsi14(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return float("nan")
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += abs(diff)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))

def compute_score(symbol: str, closes: List[float]) -> Tuple[int, Dict[str, Any]]:
    """
    Simpel scoremodel (basis). Later pluggen we jouw echte setup-types + regime in.
    """
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    rsi = rsi14(closes, 14)

    score = 0
    if not math.isnan(s20) and not math.isnan(s50):
        if s20 > s50:
            score += 40
        else:
            score += 15

    if not math.isnan(rsi):
        if rsi >= 55:
            score += 35
        elif rsi >= 45:
            score += 20
        else:
            score += 10

    # lichte momentum check
    if len(closes) >= 6 and closes[-1] > closes[-6]:
        score += 15
    else:
        score += 5

    score = max(0, min(100, score))

    meta = {"sma20": s20, "sma50": s50, "rsi14": rsi}
    return score, meta

def decide_label(score: int) -> Optional[str]:
    if score >= MIN_SCORE_TO_PREBUY:
        return "GO"
    if score >= WATCH_MIN_SCORE:
        return "WATCH"
    return None

def calc_stop_target(entry: float) -> Tuple[float, float]:
    # Default: stop 2% onder entry, target 2R
    stop = entry * 0.98
    risk = entry - stop
    target = entry + 2.0 * risk
    return stop, target

def fingerprint(symbol: str, setup_type: str, entry: float, target: float) -> str:
    return f"{symbol}|{setup_type}|{round(entry, 8)}|{round(target, 8)}"

# ==========================================================
# DB functions
# ==========================================================
def db_get_daily_count() -> int:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT created_count
                FROM prebuy_state
                WHERE day = CURRENT_DATE
                LIMIT 1
            """)
            row = cur.fetchone()
            return int(row[0]) if row else 0

def db_inc_daily_count() -> None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO prebuy_state(day, created_count)
                VALUES (CURRENT_DATE, 1)
                ON CONFLICT (day)
                DO UPDATE SET created_count = prebuy_state.created_count + 1
            """)
        conn.commit()

def db_dedupe_allowed(fp: str) -> bool:
    """
    True als:
    - fingerprint niet bestaat -> ok
    - bestaat maar cooldown voorbij -> ok
    - bestaat en cooldown nog actief -> NO
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXTRACT(EPOCH FROM (NOW() - last_created_at))::BIGINT AS age_seconds
                FROM trade_fingerprints
                WHERE fingerprint=%s
                LIMIT 1
            """, (fp,))
            row = cur.fetchone()

    if not row:
        return True

    age = int(row[0] or 0)
    return age >= TRADE_COOLDOWN_SECONDS

def db_touch_fingerprint(fp: str) -> None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trade_fingerprints(fingerprint, last_created_at)
                VALUES (%s, NOW())
                ON CONFLICT (fingerprint)
                DO UPDATE SET last_created_at = NOW()
            """, (fp,))
        conn.commit()

def db_insert_prebuy(row: Dict[str, Any]) -> bool:
    """
    Insert als ID nog niet bestaat.
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pending_approvals(
                    id, symbol, setup_type, label, score, grade,
                    entry, stop_loss, target, regime,
                    status, created_at, expires_at
                )
                VALUES (
                    %(id)s, %(symbol)s, %(setup_type)s, %(label)s, %(score)s, %(grade)s,
                    %(entry)s, %(stop_loss)s, %(target)s, %(regime)s,
                    'PENDING', NOW(), %(expires_at)s
                )
                ON CONFLICT (id) DO NOTHING
            """, row)
            inserted = (cur.rowcount == 1)
        conn.commit()
    return inserted

# ==========================================================
# Main
# ==========================================================
def make_id(symbol: str) -> str:
    return f"PB-{symbol}-{now_ts()}"

def main() -> None:
    db_init()

    created_today = db_get_daily_count()
    created = 0

    print(f"🚀 multi_coin_score start | created_today={created_today}/{MAX_PREBUY_PER_DAY}", flush=True)

    for sym in COINS:
        if created_today + created >= MAX_PREBUY_PER_DAY and not FORCE_TEST_PREBUY:
            break

        try:
            kl = get_klines(sym)
            closes = [float(k[4]) for k in kl]  # close
            entry = closes[-1]

            score, meta = compute_score(sym, closes)
            label = decide_label(score)

            if FORCE_TEST_PREBUY and label is None:
                label = "WATCH"
                score = max(score, WATCH_MIN_SCORE)

            if label is None:
                continue

            # placeholder setup/regime (hier pluggen we straks jouw echte TREND_PULLBACK/BREAKOUT + regime in)
            setup_type = "TREND_PULLBACK"
            regime = "UNKNOWN"
            stop_loss, target = calc_stop_target(entry)

            fp = fingerprint(sym, setup_type, entry, target)
            if not db_dedupe_allowed(fp) and not FORCE_TEST_PREBUY:
                continue

            prebuy_id = make_id(sym)
            expires_at = time.strftime("%Y-%m-%d %H:%M:%S%z", time.localtime(now_ts() + PREBUY_VALID_SECONDS))

            row = {
                "id": prebuy_id,
                "symbol": sym,
                "setup_type": setup_type,
                "label": label,
                "score": int(score),
                "grade": "A" if score >= 90 else "B" if score >= 80 else "C",
                "entry": float(entry),
                "stop_loss": float(stop_loss),
                "target": float(target),
                "regime": regime,
                "expires_at": expires_at,
            }

            inserted = db_insert_prebuy(row)
            if inserted:
                db_touch_fingerprint(fp)
                db_inc_daily_count()
                created += 1
                print(
                    f"✅ created: {prebuy_id} | {sym} | {label} | score={score} | entry={entry} stop={stop_loss} target={target}",
                    flush=True,
                )

        except Exception as e:
            print(f"⚠️ ERROR {sym}: {e}", flush=True)
            continue

    print(f"done. created={created}", flush=True)

if __name__ == "__main__":
    main()
