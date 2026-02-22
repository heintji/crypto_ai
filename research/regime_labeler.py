# research/regime_labeler.py
from __future__ import annotations

import os
import sys
import math
import time
from typing import List, Tuple, Optional

import psycopg2


# =========================
# ENV / DEFAULTS
# =========================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

EXCHANGE = (os.getenv("EXCHANGE") or "binance").strip()
TIMEFRAME = (os.getenv("TIMEFRAME") or "4h").strip()

# Hoeveel candles ophalen per symbol (moet genoeg zijn voor SMA200 + slope)
LOOKBACK = int(os.getenv("LOOKBACK") or "320")

# Max symbols per run (0 = geen limiet)
SYMBOL_LIMIT = int(os.getenv("SYMBOL_LIMIT") or "155")

# Extra: alleen symbols die candles hebben
ONLY_QUOTE = (os.getenv("ONLY_QUOTE") or "USDT").strip().upper()  # optioneel filter op symbol eindigt met USDT

# Kleine throttle om DB te ontzien (sec)
SLEEP_BETWEEN = float(os.getenv("SLEEP_BETWEEN") or "0")


# =========================
# DB HELPERS
# =========================
def connect_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is empty (set it in Render env)")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def ensure_market_regime_table(conn):
    """
    Zorgt dat public.market_regime bestaat (zoals jij 'm al gemaakt hebt).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.market_regime (
              exchange   TEXT NOT NULL DEFAULT 'binance',
              symbol     TEXT NOT NULL,
              timeframe  TEXT NOT NULL DEFAULT '4h',
              asof_ts    TIMESTAMPTZ NOT NULL,
              regime     TEXT NOT NULL,
              score      INTEGER NOT NULL DEFAULT 0,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              PRIMARY KEY (exchange, symbol, timeframe, asof_ts)
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS market_regime_symbol_ts_idx
            ON public.market_regime(symbol, asof_ts DESC);
            """
        )


def fetch_symbols(conn) -> List[str]:
    """
    Haalt symbols uit candles table. Filtert optioneel op USDT.
    """
    q = """
        SELECT DISTINCT symbol
        FROM public.candles
        WHERE exchange = %s
          AND timeframe = %s
    """
    params = [EXCHANGE, TIMEFRAME]

    if ONLY_QUOTE:
        q += " AND symbol LIKE %s"
        params.append(f"%{ONLY_QUOTE}")

    q += " ORDER BY symbol ASC"

    if SYMBOL_LIMIT and SYMBOL_LIMIT > 0:
        q += " LIMIT %s"
        params.append(SYMBOL_LIMIT)

    with conn.cursor() as cur:
        cur.execute(q, tuple(params))
        rows = cur.fetchall()

    return [r[0] for r in rows]


def fetch_closes(conn, symbol: str) -> Tuple[List[float], Optional[str]]:
    """
    Haalt de laatste LOOKBACK closes op + de meest recente open_time (asof_ts).
    Let op: jouw candles table heeft open_time, geen ts.
    """
    sql = """
        SELECT open_time, close
        FROM public.candles
        WHERE exchange = %s
          AND symbol = %s
          AND timeframe = %s
        ORDER BY open_time DESC
        LIMIT %s;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (EXCHANGE, symbol, TIMEFRAME, LOOKBACK))
        rows = cur.fetchall()

    if not rows:
        return [], None

    # rows zijn DESC (nieuwste eerst). Voor indicatoren willen we ASC (oudste -> nieuwste).
    rows.reverse()

    closes = [float(r[1]) for r in rows]
    asof_ts = str(rows[-1][0])  # laatste open_time als string (psycopg2 geeft vaak datetime terug; str is safe voor logging)
    return closes, asof_ts


# =========================
# INDICATORS / REGIME
# =========================
def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / float(period)


def slope(values: List[float], period: int) -> Optional[float]:
    """
    Eenvoudige slope: (laatste - eerste) / periode
    """
    if len(values) < period + 1:
        return None
    start = values[-(period + 1)]
    end = values[-1]
    if start == 0:
        return None
    return (end - start) / abs(start)


def compute_regime(closes: List[float]) -> Tuple[str, int]:
    """
    Simpele, robuuste regime:
    - SMA50 vs SMA200 + slope SMA50
    - Score 0-100 als 'sterkte' (afstand & trend)
    """
    if len(closes) < 220:
        return "UNKNOWN", 0

    c = closes[-1]
    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200)
    if sma50 is None or sma200 is None:
        return "UNKNOWN", 0

    # slope op SMA50-achtige beweging: neem slope van closes over 20 candles (4h => ~3.3 dagen)
    s = slope(closes, 20)
    s = 0.0 if s is None else s

    # afstand t.o.v. SMA200 (percent)
    dist = 0.0
    if sma200 != 0:
        dist = (c - sma200) / abs(sma200)

    # regime rules
    if c > sma200 and sma50 > sma200 and s > 0:
        regime = "BULL"
    elif c < sma200 and sma50 < sma200 and s < 0:
        regime = "BEAR"
    else:
        regime = "RANGE"

    # score (0-100): combineer trend + afstand
    # clamp
    strength = min(1.0, abs(dist) * 3.0)  # dist 0.10 => ~0.30 => best ok
    trend = min(1.0, abs(s) * 10.0)       # slope 0.01 => 0.10
    raw = 0.6 * strength + 0.4 * trend
    score = int(max(0, min(100, round(raw * 100))))

    return regime, score


def upsert_regime(conn, symbol: str, asof_ts, regime: str, score: int):
    """
    Upsert op (exchange, symbol, timeframe, asof_ts)
    """
    sql = """
        INSERT INTO public.market_regime(exchange, symbol, timeframe, asof_ts, regime, score, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (exchange, symbol, timeframe, asof_ts)
        DO UPDATE SET
          regime = EXCLUDED.regime,
          score = EXCLUDED.score,
          updated_at = now();
    """
    with conn.cursor() as cur:
        cur.execute(sql, (EXCHANGE, symbol, TIMEFRAME, asof_ts, regime, score))


# =========================
# MAIN
# =========================
def main():
    t0 = time.time()
    conn = connect_db()
    ensure_market_regime_table(conn)

    symbols = fetch_symbols(conn)
    print(f"== regime_labeler B == exchange={EXCHANGE} tf={TIMEFRAME} symbols={len(symbols)} lookback={LOOKBACK}")

    ok = 0
    skipped = 0

    for i, sym in enumerate(symbols, start=1):
        closes, asof_ts = fetch_closes(conn, sym)
        if not closes or not asof_ts:
            skipped += 1
            print(f"{sym}: skip (no data)")
            continue

        regime, score = compute_regime(closes)
        if regime == "UNKNOWN":
            skipped += 1
            print(f"{sym}: skip (not enough candles: {len(closes)})")
            continue

        # Gebruik echte datetime object voor asof_ts als psycopg2 dat terug gaf
        # fetch_closes geeft str(rows[-1][0]) voor logging, maar we willen de originele waarde.
        # Daarom: haal 'm nog 1 keer direct op als datetime:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT open_time
                FROM public.candles
                WHERE exchange=%s AND symbol=%s AND timeframe=%s
                ORDER BY open_time DESC
                LIMIT 1;
                """,
                (EXCHANGE, sym, TIMEFRAME),
            )
            row = cur.fetchone()
            dt_asof = row[0] if row else None

        if dt_asof is None:
            skipped += 1
            print(f"{sym}: skip (no asof)")
            continue

        upsert_regime(conn, sym, dt_asof, regime, score)
        ok += 1
        print(f"{sym}: {regime} score={score} asof={dt_asof}")

        if SLEEP_BETWEEN > 0:
            time.sleep(SLEEP_BETWEEN)

    dt = time.time() - t0
    print(f"DONE ok={ok} skipped={skipped} seconds={dt:.1f}")


if __name__ == "__main__":
    main()
