# research/history_fetcher.py
from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple

import requests
import psycopg2
from psycopg2.extras import execute_values

# =========================
# ENV
# =========================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

AUTO_UNIVERSE = (os.getenv("AUTO_UNIVERSE") or "1").strip() == "1"
QUOTE_ASSET = (os.getenv("QUOTE_ASSET") or "USDT").strip().upper()

TIMEFRAMES_RAW = (os.getenv("TIMEFRAMES") or "1h,4h").strip()
TIMEFRAMES = [x.strip() for x in TIMEFRAMES_RAW.split(",") if x.strip()]
if not TIMEFRAMES:
    TIMEFRAMES = ["1h", "4h"]

YEARS = int(os.getenv("YEARS") or "3")
BATCH_SIZE = int(os.getenv("BATCH_SIZE") or "50")

# filter op 24h quote-volume in USDT (optioneel)
MIN_QUOTE_VOLUME_24H_USDT = float(os.getenv("MIN_QUOTE_VOLUME_24H_USDT") or "0")

BINANCE_BASE = "https://api.binance.com"
UA = {"User-Agent": "crypto-ai-history-fetcher/1.1"}

# =========================
# Helpers
# =========================
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _since_ts_ms(years: int) -> int:
    dt = _utc_now() - timedelta(days=int(365.25 * years))
    return int(dt.timestamp() * 1000)

def tf_to_binance_interval(tf: str) -> str:
    tf = tf.lower().strip()
    if tf in ("1h", "1hr", "60m"):
        return "1h"
    if tf in ("4h", "4hr", "240m"):
        return "4h"
    raise ValueError(f"Unsupported timeframe: {tf}")

# =========================
# Postgres (state + write)
# =========================
def pg_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is empty (set it in Render env)")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True  # voorkomt 'transaction aborted' kettingreactie
    return conn

def ensure_state_table(conn):
    """
    LET OP: 'offset' is een SQL keyword in Postgres.
    Daarom gebruiken we 'offset_idx'.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.fetcher_state (
              key TEXT PRIMARY KEY,
              offset_idx INTEGER NOT NULL DEFAULT 0,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

def get_and_rotate_offset(conn, key: str, total: int, batch_size: int) -> int:
    """
    Haalt huidige offset op en schuift door voor volgende run.
    """
    ensure_state_table(conn)

    lock_id = abs(hash(key)) % (2**31)
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s);", (lock_id,))
        locked = cur.fetchone()[0]
        if not locked:
            raise RuntimeError("Another history_fetcher run is in progress (advisory lock)")

        try:
            cur.execute("SELECT offset_idx FROM public.fetcher_state WHERE key=%s;", (key,))
            row = cur.fetchone()
            current = int(row[0]) if row else 0

            if total <= 0:
                next_offset = 0
            else:
                next_offset = (current + batch_size) % total

            cur.execute(
                """
                INSERT INTO public.fetcher_state(key, offset_idx, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE SET
                  offset_idx = EXCLUDED.offset_idx,
                  updated_at = NOW();
                """,
                (key, next_offset),
            )
            return current
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s);", (lock_id,))

def ensure_candles_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.candles (
              exchange TEXT NOT NULL DEFAULT 'binance',
              symbol TEXT NOT NULL,
              timeframe TEXT NOT NULL,
              open_time TIMESTAMPTZ NOT NULL,
              open DOUBLE PRECISION NOT NULL,
              high DOUBLE PRECISION NOT NULL,
              low DOUBLE PRECISION NOT NULL,
              close DOUBLE PRECISION NOT NULL,
              volume DOUBLE PRECISION NOT NULL,
              close_time TIMESTAMPTZ,
              trades BIGINT,
              quote_volume DOUBLE PRECISION,
              taker_buy_base DOUBLE PRECISION,
              taker_buy_quote DOUBLE PRECISION,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              CONSTRAINT candles_unique_key UNIQUE(exchange, symbol, timeframe, open_time)
            );
            """
        )

def insert_candles(conn, rows: List[Tuple]):
    """
    rows:
    (exchange, symbol, timeframe, open_time, open, high, low, close, volume, close_time, trades, quote_volume, taker_buy_base, taker_buy_quote)
    """
    if not rows:
        return 0

    ensure_candles_table(conn)
    sql = """
    INSERT INTO public.candles
      (exchange, symbol, timeframe, open_time, open, high, low, close, volume, close_time, trades, quote_volume, taker_buy_base, taker_buy_quote, updated_at)
    VALUES %s
    ON CONFLICT (exchange, symbol, timeframe, open_time) DO NOTHING;
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, [r + (_utc_now(),) for r in rows], page_size=1000)
    return len(rows)

# =========================
# Binance universe
# =========================
def get_binance_symbols(quote_asset: str) -> List[str]:
    r = requests.get(f"{BINANCE_BASE}/api/v3/exchangeInfo", headers=UA, timeout=30)
    r.raise_for_status()
    data = r.json()

    symbols = []
    for s in data.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        if s.get("quoteAsset") != quote_asset:
            continue
        if not s.get("isSpotTradingAllowed", True):
            continue
        symbols.append(s["symbol"])
    symbols.sort()
    return symbols

def get_24h_quote_volumes() -> Dict[str, float]:
    r = requests.get(f"{BINANCE_BASE}/api/v3/ticker/24hr", headers=UA, timeout=30)
    r.raise_for_status()
    data = r.json()
    out: Dict[str, float] = {}
    for item in data:
        sym = item.get("symbol")
        qv = item.get("quoteVolume")
        if sym and qv is not None:
            try:
                out[sym] = float(qv)
            except Exception:
                pass
    return out

# =========================
# Binance klines fetch
# =========================
def fetch_klines(symbol: str, interval: str, start_ms: int) -> List[List]:
    out: List[List] = []
    limit = 1000
    start = start_ms
    while True:
        params = {"symbol": symbol, "interval": interval, "startTime": start, "limit": limit}
        r = requests.get(f"{BINANCE_BASE}/api/v3/klines", params=params, headers=UA, timeout=30)
        r.raise_for_status()
        kl = r.json()
        if not kl:
            break
        out.extend(kl)
        last_open = int(kl[-1][0])
        start = last_open + 1
        if len(kl) < limit:
            break
        time.sleep(0.2)
    return out

def kline_rows(symbol: str, timeframe: str, klines: List[List]) -> List[Tuple]:
    rows: List[Tuple] = []
    for k in klines:
        open_ms = int(k[0])
        open_time = datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc)

        o = float(k[1]); h = float(k[2]); l = float(k[3]); c = float(k[4]); v = float(k[5])
        close_ms = int(k[6])
        close_time = datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc)

        quote_vol = float(k[7]) if k[7] is not None else None
        trades = int(k[8]) if k[8] is not None else None
        taker_buy_base = float(k[9]) if k[9] is not None else None
        taker_buy_quote = float(k[10]) if k[10] is not None else None

        rows.append((
            "binance",
            symbol,
            timeframe,
            open_time,
            o, h, l, c, v,
            close_time,
            trades,
            quote_vol,
            taker_buy_base,
            taker_buy_quote,
        ))
    return rows

# =========================
# Main
# =========================
def main():
    print("== history_fetcher (DB-only + auto offset rotation) ==", flush=True)
    print(f"AUTO_UNIVERSE={AUTO_UNIVERSE} QUOTE_ASSET={QUOTE_ASSET} TIMEFRAMES={TIMEFRAMES} YEARS={YEARS}", flush=True)
    print(f"BATCH_SIZE={BATCH_SIZE} MIN_QUOTE_VOLUME_24H_USDT={MIN_QUOTE_VOLUME_24H_USDT}", flush=True)

    conn = pg_connect()

    symbols = get_binance_symbols(QUOTE_ASSET)
    if MIN_QUOTE_VOLUME_24H_USDT > 0:
        vols = get_24h_quote_volumes()
        symbols = [s for s in symbols if vols.get(s, 0.0) >= MIN_QUOTE_VOLUME_24H_USDT]
        symbols.sort()

    total = len(symbols)
    print(f"Universe total={total}", flush=True)

    key = f"binance:{QUOTE_ASSET}:minqv={int(MIN_QUOTE_VOLUME_24H_USDT)}:tfs={','.join(TIMEFRAMES)}:years={YEARS}"
    offset = get_and_rotate_offset(conn, key=key, total=total, batch_size=BATCH_SIZE)

    if total == 0:
        print("No symbols after filtering. Stop.", flush=True)
        return

    start_i = offset
    end_i = min(offset + BATCH_SIZE, total)
    batch = symbols[start_i:end_i]
    print(f"Offset={offset} -> fetching batch size={len(batch)} (range {start_i}:{end_i})", flush=True)

    since_ms = _since_ts_ms(YEARS)

    for sym in batch:
        for tf in TIMEFRAMES:
            try:
                interval = tf_to_binance_interval(tf)
                kl = fetch_klines(sym, interval, since_ms)
                rows = kline_rows(sym, tf, kl)
                wrote = insert_candles(conn, rows)
                print(f"{sym} {tf}: fetched={len(kl)} wrote~{wrote}", flush=True)
            except Exception as e:
                print(f"⚠️ {sym} {tf} error: {e}", flush=True)
                traceback.print_exc()
                continue

    print("✅ DONE", flush=True)

if __name__ == "__main__":
    main()
