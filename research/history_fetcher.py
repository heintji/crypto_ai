# research/history_fetcher.py
from __future__ import annotations

import os
import sys
import time
import math
import json
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Dict, Tuple, Optional

import requests

import psycopg2
import psycopg2.extras


# ==========================================================
# PATH / PROJECT ROOT
# ==========================================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ==========================================================
# ENV
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

AUTO_UNIVERSE = (os.getenv("AUTO_UNIVERSE") or "1").strip() == "1"
QUOTE_ASSET = (os.getenv("QUOTE_ASSET") or "USDT").strip().upper()
TIMEFRAMES = [x.strip() for x in (os.getenv("TIMEFRAMES") or "1h,4h").split(",") if x.strip()]

# support both names (you showed both in Render)
YEARS = int((os.getenv("HIST_YEARS") or os.getenv("YEARS") or "3").strip())
BATCH_SIZE = int((os.getenv("BATCH_SIZE") or "50").strip())
HIST_WRITE_DB = (os.getenv("HIST_WRITE_DB") or "1").strip() == "1"

# Optional: limit universe for tests
MAX_SYMBOLS = int((os.getenv("MAX_SYMBOLS") or "0").strip())  # 0 = no limit
ONLY_SYMBOLS = [x.strip().upper() for x in (os.getenv("ONLY_SYMBOLS") or "").split(",") if x.strip()]

# Binance public API
BINANCE_BASE = (os.getenv("BINANCE_BASE") or "https://api.binance.com").strip().rstrip("/")
REQ_TIMEOUT = int((os.getenv("REQ_TIMEOUT") or "20").strip())
REQ_SLEEP = float((os.getenv("REQ_SLEEP") or "0.15").strip())  # small delay to be nice

LOGS_DIR = (os.getenv("LOGS_DIR") or "").strip()
if not LOGS_DIR:
    LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)


# ==========================================================
# LOG
# ==========================================================
def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {msg}"
    print(line, flush=True)


# ==========================================================
# DB
# ==========================================================
def get_db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing (set Render Environment Variable DATABASE_URL)")
    # Render Postgres usually needs SSL
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    conn.autocommit = False
    return conn


# ==========================================================
# BINANCE HELPERS
# ==========================================================
def http_get_json(url: str, params: Optional[dict] = None, retries: int = 4) -> dict:
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=REQ_TIMEOUT)
            if r.status_code == 429:
                # rate limit: backoff
                sleep_s = 1.0 + (i * 1.5)
                time.sleep(sleep_s)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(0.5 + i * 0.5)
    raise RuntimeError(f"HTTP failed after retries: {url} | err={last_err}")


def http_get_klines(symbol: str, interval: str, start_ms: int, end_ms: int, limit: int = 1000) -> List[list]:
    url = f"{BINANCE_BASE}/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": limit,
    }
    # Binance returns list of lists
    r = requests.get(url, params=params, timeout=REQ_TIMEOUT)
    if r.status_code == 429:
        time.sleep(1.25)
        r = requests.get(url, params=params, timeout=REQ_TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_usdt_symbols() -> List[str]:
    # Use exchangeInfo and filter for spot trading pairs ending with QUOTE_ASSET
    data = http_get_json(f"{BINANCE_BASE}/api/v3/exchangeInfo")
    symbols = []
    for s in data.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        if s.get("quoteAsset") != QUOTE_ASSET:
            continue
        # keep spot symbols only
        if s.get("isSpotTradingAllowed") is False:
            continue
        sym = (s.get("symbol") or "").upper()
        if sym:
            symbols.append(sym)

    symbols = sorted(set(symbols))
    if ONLY_SYMBOLS:
        only = set(ONLY_SYMBOLS)
        symbols = [s for s in symbols if s in only]
    if MAX_SYMBOLS and MAX_SYMBOLS > 0:
        symbols = symbols[:MAX_SYMBOLS]
    return symbols


# ==========================================================
# TIME / RANGE
# ==========================================================
def now_ms() -> int:
    return int(time.time() * 1000)


def years_ago_ms(years: int) -> int:
    # Approx: 365.25 days per year
    days = years * 365.25
    return int((time.time() - days * 24 * 3600) * 1000)


def interval_to_seconds(tf: str) -> int:
    tf = tf.strip().lower()
    if tf.endswith("m"):
        return int(tf[:-1]) * 60
    if tf.endswith("h"):
        return int(tf[:-1]) * 3600
    if tf.endswith("d"):
        return int(tf[:-1]) * 86400
    raise ValueError(f"Unsupported timeframe: {tf}")


# ==========================================================
# DB INSERT (FIX: ON CONFLICT + rollback)
# ==========================================================
INSERT_SQL = """
INSERT INTO public.candles (
    exchange,
    symbol,
    timeframe,
    open_time,
    open,
    high,
    low,
    close,
    volume,
    close_time,
    trades,
    quote_volume,
    taker_buy_base,
    taker_buy_quote
)
VALUES %s
ON CONFLICT (exchange, symbol, timeframe, open_time) DO NOTHING
"""


def insert_candles(conn, rows: List[Tuple]) -> int:
    """
    Returns inserted rowcount (best-effort; rowcount may be -1 in some drivers).
    """
    if not rows:
        return 0
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            INSERT_SQL,
            rows,
            template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            page_size=1000,
        )
        # rowcount is usually inserted count for INSERT..DO NOTHING.
        inserted = cur.rowcount if cur.rowcount is not None else 0
    return max(0, inserted)


# ==========================================================
# KLINES -> ROWS
# ==========================================================
def kline_to_row(symbol: str, tf: str, k: list) -> Tuple:
    # Binance kline fields:
    # 0 open_time(ms), 1 open, 2 high, 3 low, 4 close, 5 volume,
    # 6 close_time(ms), 7 quote_asset_volume, 8 number_of_trades,
    # 9 taker_buy_base_asset_volume, 10 taker_buy_quote_asset_volume, 11 ignore
    open_time = datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc)
    close_time = datetime.fromtimestamp(int(k[6]) / 1000, tz=timezone.utc)

    return (
        "binance",                 # exchange
        symbol,                    # symbol
        tf,                        # timeframe
        open_time,                 # open_time (timestamptz)
        float(k[1]),               # open
        float(k[2]),               # high
        float(k[3]),               # low
        float(k[4]),               # close
        float(k[5]),               # volume
        close_time,                # close_time
        int(k[8]),                 # trades
        float(k[7]),               # quote_volume
        float(k[9]),               # taker_buy_base
        float(k[10]),              # taker_buy_quote
    )


# ==========================================================
# FETCH ONE SYMBOL/TF
# ==========================================================
def fetch_and_store_symbol_tf(conn, symbol: str, tf: str, start_ms: int, end_ms: int) -> Dict[str, int]:
    step_sec = interval_to_seconds(tf)
    step_ms = step_sec * 1000

    # Binance max 1000 candles per request
    max_per_call = 1000
    window_ms = step_ms * max_per_call

    cur_start = start_ms
    attempted = 0
    inserted_total = 0
    calls = 0

    while cur_start < end_ms:
        cur_end = min(end_ms, cur_start + window_ms)
        try:
            klines = http_get_klines(symbol, tf, cur_start, cur_end, limit=max_per_call)
            calls += 1
            if not klines:
                cur_start = cur_end + step_ms
                time.sleep(REQ_SLEEP)
                continue

            rows = [kline_to_row(symbol, tf, k) for k in klines]
            attempted += len(rows)

            if HIST_WRITE_DB:
                ins = insert_candles(conn, rows)
                conn.commit()
                inserted_total += ins

            # move forward from last returned kline open time
            last_open_ms = int(klines[-1][0])
            cur_start = last_open_ms + step_ms

            time.sleep(REQ_SLEEP)

        except Exception as e:
            # IMPORTANT FIX: rollback so transaction isn't stuck aborted
            try:
                conn.rollback()
            except Exception:
                pass
            log(f"⚠️ {symbol} {tf} error: {e}")
            # small backoff and keep going
            time.sleep(0.75)

            # advance a little to avoid infinite loop on same window
            cur_start = min(cur_end + step_ms, end_ms)

    return {"attempted": attempted, "inserted": inserted_total, "calls": calls}


# ==========================================================
# MAIN
# ==========================================================
def chunked(lst: List[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def main() -> int:
    log(f"history_fetcher start | AUTO_UNIVERSE={AUTO_UNIVERSE} | QUOTE_ASSET={QUOTE_ASSET} | TIMEFRAMES={TIMEFRAMES} | YEARS={YEARS} | BATCH_SIZE={BATCH_SIZE}")

    if not HIST_WRITE_DB:
        log("HIST_WRITE_DB=0 -> running in dry-run mode (no DB writes)")

    symbols = get_usdt_symbols() if AUTO_UNIVERSE else ONLY_SYMBOLS
    if not symbols:
        log("No symbols found. Check AUTO_UNIVERSE / ONLY_SYMBOLS / QUOTE_ASSET.")
        return 1

    log(f"Universe symbols={len(symbols)} (example: {symbols[:5]})")

    start_ms = years_ago_ms(YEARS)
    end_ms = now_ms()

    conn = get_db_conn()
    try:
        total = len(symbols)
        batches = list(chunked(symbols, BATCH_SIZE))
        log(f"batches={len(batches)} (size={BATCH_SIZE})")

        for bi, batch in enumerate(batches, start=1):
            log(f"--- batch {bi}/{len(batches)} size={len(batch)}")
            for sym in batch:
                for tf in TIMEFRAMES:
                    stats = fetch_and_store_symbol_tf(conn, sym, tf, start_ms, end_ms)
                    log(f"{sym} {tf}: attempted={stats['attempted']} inserted~{stats['inserted']} calls={stats['calls']}")

        log("✅ DONE")
        return 0

    except KeyboardInterrupt:
        log("Interrupted.")
        return 130
    except Exception as e:
        log(f"FATAL: {e}")
        log(traceback.format_exc())
        try:
            conn.rollback()
        except Exception:
            pass
        return 2
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
