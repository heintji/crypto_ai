from __future__ import annotations

import os
import math
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import psycopg2


# =========================
# CONFIG (env + defaults)
# =========================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

EXCHANGE = (os.getenv("REGIME_EXCHANGE") or "binance").strip()
TIMEFRAME = (os.getenv("REGIME_TIMEFRAME") or "4h").strip()

# Hoeveel candles per coin pakken (300 is ruim genoeg voor EMA200 stabiliteit)
LOOKBACK = int(os.getenv("REGIME_LOOKBACK") or "320")

# Slope window: EMA50 % verandering over laatste N candles
SLOPE_WINDOW = int(os.getenv("REGIME_SLOPE_WINDOW") or "10")

# Strength threshold (% afstand EMA50 vs EMA200) om bull/bear serieus te nemen
MIN_STRENGTH_PCT = float(os.getenv("REGIME_MIN_STRENGTH_PCT") or "0.8")  # 0.8%

# Max coins per run (0 = alles)
MAX_SYMBOLS = int(os.getenv("REGIME_MAX_SYMBOLS") or "0")

# Alleen USDT pairs?
ONLY_USDT = (os.getenv("REGIME_ONLY_USDT") or "1").strip() == "1"


# =========================
# Helpers
# =========================
def ema(values: List[float], span: int) -> List[float]:
    """Simple EMA list."""
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def clamp(n: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, n))


@dataclass
class RegimeResult:
    symbol: str
    asof_ts: str  # ISO-ish string from DB
    regime: str   # BULL/BEAR/RANGE
    score: int    # 0-100


def connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is empty (set it in Render env)")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def fetch_symbols(conn) -> List[str]:
    where = "WHERE exchange=%s AND timeframe=%s"
    params = [EXCHANGE, TIMEFRAME]
    if ONLY_USDT:
        where += " AND symbol LIKE %s"
        params.append("%USDT")

    sql = f"""
        SELECT DISTINCT symbol
        FROM public.candles
        {where}
        ORDER BY symbol ASC
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    symbols = [r[0] for r in rows]
    if MAX_SYMBOLS and MAX_SYMBOLS > 0:
        symbols = symbols[:MAX_SYMBOLS]
    return symbols


def fetch_closes(conn, symbol: str) -> Tuple[List[float], Optional[str]]:
    sql = """
        SELECT ts, close
        FROM public.candles
        WHERE exchange=%s AND symbol=%s AND timeframe=%s
        ORDER BY ts DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (EXCHANGE, symbol, TIMEFRAME, LOOKBACK))
        rows = cur.fetchall()

    if not rows:
        return [], None

    # rows are DESC; reverse to ASC
    rows.reverse()
    closes = [float(r[1]) for r in rows]
    asof_ts = str(rows[-1][0])
    return closes, asof_ts


def classify_regime(closes: List[float]) -> Tuple[str, int]:
    """
    B = EMA50/EMA200 + slope + strength score
    - strength_pct = abs(EMA50-EMA200)/EMA200 * 100
    - slope_pct = (EMA50_now - EMA50_prev)/EMA50_prev * 100 (over SLOPE_WINDOW)
    """
    if len(closes) < 220:
        return "RANGE", 0

    e50 = ema(closes, 50)
    e200 = ema(closes, 200)

    ema50_now = e50[-1]
    ema200_now = e200[-1]

    if ema200_now == 0:
        return "RANGE", 0

    strength_pct = abs(ema50_now - ema200_now) / ema200_now * 100.0

    # slope over window
    w = min(SLOPE_WINDOW, len(e50) - 2)
    ema50_prev = e50[-1 - w]
    slope_pct = 0.0
    if ema50_prev != 0:
        slope_pct = (ema50_now - ema50_prev) / ema50_prev * 100.0

    # Score (0-100): combine strength + slope magnitude
    # strength contributes up to 70, slope up to 30
    strength_score = clamp((strength_pct / 3.0) * 70.0, 0.0, 70.0)  # 3% afstand ~ max 70
    slope_score = clamp((abs(slope_pct) / 1.5) * 30.0, 0.0, 30.0)    # 1.5% move ~ max 30
    score = int(round(strength_score + slope_score))

    # Regime rules (strenger):
    if ema50_now > ema200_now and slope_pct > 0 and strength_pct >= MIN_STRENGTH_PCT:
        return "BULL", score
    if ema50_now < ema200_now and slope_pct < 0 and strength_pct >= MIN_STRENGTH_PCT:
        return "BEAR", score

    return "RANGE", score


def upsert_market_regime(conn, r: RegimeResult) -> None:
    sql = """
        INSERT INTO public.market_regime (exchange, symbol, timeframe, asof_ts, regime, score)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (exchange, symbol, timeframe, asof_ts)
        DO UPDATE SET regime = EXCLUDED.regime, score = EXCLUDED.score, updated_at = now()
    """
    with conn.cursor() as cur:
        cur.execute(sql, (EXCHANGE, r.symbol, TIMEFRAME, r.asof_ts, r.regime, r.score))


def main():
    t0 = time.time()
    conn = connect()

    symbols = fetch_symbols(conn)
    print(f"== regime_labeler B == exchange={EXCHANGE} tf={TIMEFRAME} symbols={len(symbols)} lookback={LOOKBACK}")

    wrote = 0
    skipped = 0

    for i, sym in enumerate(symbols, 1):
        closes, asof_ts = fetch_closes(conn, sym)
        if not closes or not asof_ts:
            skipped += 1
            continue

        regime, score = classify_regime(closes)
        upsert_market_regime(conn, RegimeResult(symbol=sym, asof_ts=asof_ts, regime=regime, score=score))
        wrote += 1

        if i % 25 == 0:
            print(f"... {i}/{len(symbols)} wrote={wrote} skipped={skipped}")

    dt = time.time() - t0
    print(f"DONE wrote={wrote} skipped={skipped} seconds={dt:.1f}")


if __name__ == "__main__":
    main()
