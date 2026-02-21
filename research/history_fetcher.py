# research/history_fetcher.py
from __future__ import annotations

import os
import time
import math
import traceback
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple, Optional

import requests

import psycopg2
from psycopg2.extras import execute_values


# =========================
# ENV
# =========================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

AUTO_UNIVERSE = (os.getenv("AUTO_UNIVERSE") or "1").strip() == "1"
QUOTE_ASSET = (os.getenv("QUOTE_ASSET") or "USDT").strip().upper()

# Comma-separated: "1h,4h"
TIMEFRAMES = (os.getenv("TIMEFRAMES") or "1h,4h").strip()
TIMEFRAMES_LIST = [x.strip() for x in TIMEFRAMES.split(",") if x.strip()]

# Years back
YEARS = int(os.getenv("YEARS") or os.getenv("HIST_YEARS") or "3")

# Batch symbols per run
BATCH_SIZE = int(os.getenv("BATCH_SIZE") or "50")
OFFSET = int(os.getenv("OFFSET") or "0")  # optional rotation offset

# Binance rate limits (conservative)
SLEEP_BETWEEN_REQUESTS = float(os.getenv("SLEEP_BETWEEN_REQUESTS") or "0.15")
SLEEP_BETWEEN_SYMBOLS = float(os.getenv("SLEEP_BETWEEN_SYMBOLS") or "0.05")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT") or "20")

# Only include symbols with quoteVolume >= this (USDT)
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H_USDT") or "0")

BINANCE_BASE = "https://api.binance.com"


# =========================
# HELPERS
# =========================
def utc_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(msg, flush=True)


def chunked(lst: List[str], n: int) -> List[List[str]]:
    return [lst[i : i + n] for i in range(0, len(lst), n)]


# =========================
# BINANCE
# =========================
def binance_get(path: str, params: Optional[dict] = None) -> dict:
    url = BINANCE_BASE + path
    r = requests.get(url, params=params or {}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    time.sleep(SLEEP_BETWEEN_REQUESTS)
    return r.json()


def get_all_usdt_symbols() -> List[str]:
    # 1) Get exchange info (all symbols)
    info = binance_get("/api/v3/exchangeInfo")
    symbols = []
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        base = s.get("baseAsset")
        quote = s.get("quoteAsset")
        sym = s.get("symbol")
        if quote != QUOTE_ASSET:
            continue
        # Spot symbols are fine; exclude leveraged tokens if you want (optional)
        if not sym.endswith(QUOTE_ASSET):
            continue
        symbols.append(sym)

    symbols = sorted(set(symbols))

    if MIN_QUOTE_VOLUME_24H <= 0:
        return symbols

    # 2) Filter by 24h quote volume
    tickers = binance_get("/api/v3/ticker/24hr")
    vol_map: Dict[str, float] = {}
    for t in tickers:
        sym = t.get("symbol")
        if not sym:
            continue
        try:
            vol_map[sym] = float(t.get("quoteVolume") or 0.0)
        except Exception:
            vol_map[sym] = 0.0

    filtered = []
    for sym in symbols:
        if vol_map.get(sym, 0.0) >= MIN_QUOTE_VOLUME_24H:
            filtered.append(sym)

    return filtered


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[list]:
    # Binance max 1000 rows per request
    out: List[list] = []
    cur = start_ms

    while True:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cur,
            "endTime": end_ms,
            "limit": 1000,
        }
        data = binance_get("/api/v3/klines", params=params)
        if not data:
            break

        out.extend(data)

        last_open_ms = int(data[-1][0])
        # advance
        next_cur = last_open_ms + 1
        if next_cur <= cur:
            break
        cur = next_cur

        # stop if we reached end
        if last_open_ms >= end_ms:
            break

        # safety: if returned less than limit, we’re done
        if len(data) < 1000:
            break

    return out


# =========================
# POSTGRES
# =========================
def pg_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is empty. Set DATABASE_URL in Render env.")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def ensure_schema(conn) -> None:
    # Creates table if missing (safe). Also creates unique constraint for upsert.
    # NOTE: You already have this table; this is just “ensure”.
    sql = """
    CREATE TABLE IF NOT EXISTS public.candles (
        exchange text NOT NULL DEFAULT 'binance',
        symbol text NOT NULL,
        timeframe text NOT NULL,
        open_time timestamptz NOT NULL,

        open double precision NOT NULL,
        high double precision NOT NULL,
        low double precision NOT NULL,
        close double precision NOT NULL,
        volume double precision NOT NULL,

        close_time timestamptz NULL,
        trades bigint NULL,
        quote_volume double precision NULL,
        taker_buy_base double precision NULL,
        taker_buy_quote double precision NULL,

        updated_at timestamptz NOT NULL DEFAULT now(),

        CONSTRAINT candles_unique UNIQUE (exchange, symbol, timeframe, open_time)
    );
    """
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def insert_candles(conn, rows: List[Tuple]) -> int:
    """
    rows tuple format:
    (exchange, symbol, timeframe, open_time, open, high, low, close, volume,
     close_time, trades, quote_volume, taker_buy_base, taker_buy_quote)
    """
    if not rows:
        return 0

    sql = """
    INSERT INTO public.candles (
        exchange, symbol, timeframe, open_time,
        open, high, low, close, volume,
        close_time, trades, quote_volume, taker_buy_base, taker_buy_quote
    )
    VALUES %s
    ON CONFLICT (exchange, symbol, timeframe, open_time) DO NOTHING;
    """

    with conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=1000)
    # NOTE: we can’t easily know exact inserted count with ON CONFLICT DO NOTHING
    # without RETURNING; we keep it simple and commit.
    conn.commit()
    return len(rows)


def kline_to_row(exchange: str, symbol: str, timeframe: str, k: list) -> Tuple:
    # Binance kline:
    # 0 open time (ms)
    # 1 open
    # 2 high
    # 3 low
    # 4 close
    # 5 volume
    # 6 close time (ms)
    # 7 quote asset volume
    # 8 number of trades
    # 9 taker buy base asset volume
    # 10 taker buy quote asset volume
    open_t = ms_to_dt(int(k[0]))
    close_t = ms_to_dt(int(k[6])) if k[6] is not None else None

    return (
        exchange,
        symbol,
        timeframe,
        open_t,
        float(k[1]),
        float(k[2]),
        float(k[3]),
        float(k[4]),
        float(k[5]),
        close_t,
        int(k[8]) if k[8] is not None else None,
        float(k[7]) if k[7] is not None else None,
        float(k[9]) if k[9] is not None else None,
        float(k[10]) if k[10] is not None else None,
    )


# =========================
# MAIN
# =========================
def main() -> None:
    log("== history_fetcher (DB-only) ==")
    log(f"AUTO_UNIVERSE={AUTO_UNIVERSE} QUOTE_ASSET={QUOTE_ASSET} TIMEFRAMES={TIMEFRAMES_LIST} YEARS={YEARS}")
    log(f"BATCH_SIZE={BATCH_SIZE} OFFSET={OFFSET} MIN_QUOTE_VOLUME_24H_USDT={MIN_QUOTE_VOLUME_24H}")

    conn = pg_connect()
    try:
        ensure_schema(conn)

        symbols = get_all_usdt_symbols() if AUTO_UNIVERSE else []
        if not symbols:
            log("No symbols found. (AUTO_UNIVERSE=0? or Binance returned empty)")
            return

        # rotate window (so each run fetches different slice)
        total = len(symbols)
        if total == 0:
            log("No symbols in universe.")
            return

        # apply rotation offset
        offset = OFFSET % total
        rotated = symbols[offset:] + symbols[:offset]

        batch = rotated[:BATCH_SIZE]
        log(f"Universe total={total} -> fetching batch size={len(batch)} (offset={offset})")

        end_dt = now_utc()
        start_dt = end_dt - timedelta(days=int(365.25 * YEARS))
        start_ms = utc_ms(start_dt)
        end_ms = utc_ms(end_dt)

        exchange = "binance"

        for i, sym in enumerate(batch, start=1):
            for tf in TIMEFRAMES_LIST:
                try:
                    klines = fetch_klines(sym, tf, start_ms, end_ms)
                    rows = [kline_to_row(exchange, sym, tf, k) for k in klines]
                    inserted_like = insert_candles(conn, rows)
                    log(f"{sym} {tf}: fetched={len(klines)} wrote~{inserted_like}")
                except Exception as e:
                    # IMPORTANT: rollback so “transaction is aborted” is fixed
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    log(f"⚠️ {sym} {tf} ERROR: {e}")
                    # keep going
                    continue

            time.sleep(SLEEP_BETWEEN_SYMBOLS)

        log("✅ DONE")

    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("FATAL in history_fetcher.py")
        log(str(e))
        log(traceback.format_exc())
        raise
