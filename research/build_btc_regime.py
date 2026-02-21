# research/build_btc_regime.py
from __future__ import annotations

import os
import sys
import math
import traceback
from datetime import datetime, timezone
from typing import List, Tuple, Optional

import psycopg2
from psycopg2.extras import execute_values


# ==========================================================
# SETTINGS (DB-only, no disk)
# ==========================================================
SYMBOL = os.getenv("BTC_REGIME_SYMBOL", "BTCUSDT").strip().upper()
TIMEFRAME = os.getenv("BTC_REGIME_TIMEFRAME", "4h").strip().lower()  # candles.timeframe is text
EMA_PERIOD = int(os.getenv("BTC_REGIME_EMA_PERIOD", "200"))

# How many candles to load each run (enough for ema warmup + recent)
# 4h candles: 3 years ~ 6500 rows, so 8000 is safe
LIMIT = int(os.getenv("BTC_REGIME_LIMIT", "9000"))

# If you want: only compute last N rows and keep older as is (optional)
# 0 = compute all loaded rows
TAIL_ONLY = int(os.getenv("BTC_REGIME_TAIL_ONLY", "0"))


# ==========================================================
# DB
# ==========================================================
def get_db_url() -> str:
    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if not db_url:
        raise ValueError("DATABASE_URL not found in environment variables (Render Cron must have it).")
    return db_url


def connect():
    return psycopg2.connect(get_db_url(), sslmode="require")


# ==========================================================
# MATH (EMA + simple regime)
# ==========================================================
def ema_series(values: List[float], period: int) -> List[Optional[float]]:
    """
    Returns EMA list same length as values.
    First (period-1) values are None, then EMA starts at SMA(period).
    """
    if period <= 1:
        return [float(v) for v in values]

    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out

    sma = sum(values[:period]) / period
    out[period - 1] = sma
    k = 2 / (period + 1)

    prev = sma
    for i in range(period, len(values)):
        prev = (values[i] * k) + (prev * (1 - k))
        out[i] = prev
    return out


def classify_regime(close: float, ema200: float) -> str:
    """
    Super-simpel (zoals jij vroeg): regime op basis van close vs EMA200.
    - BULL: close >= ema200
    - BEAR: close < ema200
    (Later kunnen we RANGE toevoegen met slope/volatility, maar nu houden we het strak.)
    """
    return "BULL" if close >= ema200 else "BEAR"


# ==========================================================
# MAIN
# ==========================================================
def ensure_table(cur) -> None:
    # Past bij jullie eerder aangemaakte table (screenshot liet dit al zien).
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS btc_regime_4h (
            open_time timestamptz PRIMARY KEY,
            regime text NOT NULL,
            ema200 double precision,
            close double precision
        );
        """
    )


def load_candles(cur) -> List[Tuple[datetime, float]]:
    """
    Load BTC 4h candles from public.candles
    candles schema in jouw screenshot:
      open_time timestamptz, close double precision, symbol text, timeframe text
    """
    cur.execute(
        """
        SELECT open_time, close
        FROM candles
        WHERE symbol = %s
          AND timeframe = %s
        ORDER BY open_time ASC
        LIMIT %s;
        """,
        (SYMBOL, TIMEFRAME, LIMIT),
    )
    rows = cur.fetchall()
    # rows: [(datetime, close), ...]
    return rows


def upsert_regimes(cur, items: List[Tuple[datetime, str, float, float]]) -> int:
    """
    items: (open_time, regime, ema200, close)
    Upsert on open_time.
    """
    if not items:
        return 0

    query = """
        INSERT INTO btc_regime_4h (open_time, regime, ema200, close)
        VALUES %s
        ON CONFLICT (open_time)
        DO UPDATE SET
            regime = EXCLUDED.regime,
            ema200 = EXCLUDED.ema200,
            close = EXCLUDED.close;
    """
    execute_values(cur, query, items, page_size=2000)
    return len(items)


def main() -> int:
    started = datetime.now(timezone.utc)

    print("=== build_btc_regime.py starting ===")
    print(f"UTC start: {started.isoformat()}")
    print(f"SYMBOL={SYMBOL} TIMEFRAME={TIMEFRAME} EMA_PERIOD={EMA_PERIOD} LIMIT={LIMIT} TAIL_ONLY={TAIL_ONLY}")

    conn = None
    try:
        conn = connect()
        conn.autocommit = False

        with conn.cursor() as cur:
            ensure_table(cur)

            candles = load_candles(cur)
            if not candles:
                print("No candles found. Nothing to do.")
                conn.rollback()
                return 0

            times = [c[0] for c in candles]
            closes = [float(c[1]) for c in candles]

            ema200_list = ema_series(closes, EMA_PERIOD)

            # Decide which indices to compute
            start_idx = 0
            if TAIL_ONLY and TAIL_ONLY > 0:
                start_idx = max(0, len(closes) - TAIL_ONLY)

            payload: List[Tuple[datetime, str, float, float]] = []
            skipped = 0

            for i in range(start_idx, len(closes)):
                ema_val = ema200_list[i]
                if ema_val is None:
                    skipped += 1
                    continue
                reg = classify_regime(closes[i], float(ema_val))
                payload.append((times[i], reg, float(ema_val), closes[i]))

            written = upsert_regimes(cur, payload)
            conn.commit()

            # Quick stats
            cur.execute("SELECT COUNT(*) FROM btc_regime_4h;")
            total = cur.fetchone()[0]

            cur.execute(
                """
                SELECT regime, COUNT(*)
                FROM btc_regime_4h
                GROUP BY regime
                ORDER BY COUNT(*) DESC;
                """
            )
            dist = cur.fetchall()

            ended = datetime.now(timezone.utc)
            dur = (ended - started).total_seconds()

            print("=== DONE ===")
            print(f"candles_loaded={len(candles)} skipped_no_ema={skipped} upserted={written} table_total={total}")
            print(f"regime_distribution={dist}")
            print(f"UTC end: {ended.isoformat()} duration_sec={dur:.2f}")

        return 0

    except Exception as e:
        print("!!! ERROR in build_btc_regime.py !!!")
        print(str(e))
        traceback.print_exc()
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
        return 1
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
