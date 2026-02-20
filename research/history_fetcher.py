from __future__ import annotations

import os
import time
import math
import json
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple, Optional

import requests
import psycopg2
import psycopg2.extras

# =========================
# ENV
# =========================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

AUTO_UNIVERSE = (os.getenv("AUTO_UNIVERSE") or "0").strip() == "1"
QUOTE_ASSET = (os.getenv("QUOTE_ASSET") or "USDT").strip().upper()

TIMEFRAMES = (os.getenv("TIMEFRAMES") or "1h,4h").strip()
YEARS = int(os.getenv("YEARS") or "3")
BATCH_SIZE = int(os.getenv("BATCH_SIZE") or "50")

HIST_WRITE_DB = (os.getenv("HIST_WRITE_DB") or "1").strip() == "1"
HIST_WRITE_CSV = (os.getenv("HIST_WRITE_CSV") or "0").strip() == "1"  # default uit

MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS") or "0")  # 0 = unlimited
SYMBOLS_ENV = (os.getenv("SYMBOLS") or "").strip()  # optional manual list "BTCUSDT,ETHUSDT"

BINANCE_BASE = "https://api.binance.com"
KLINES_LIMIT = 1000
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_CALLS = float(os.getenv("SLEEP_BETWEEN_CALLS") or "0.2")  # rate safety

EXCLUDE_SUBSTRINGS = [
    "UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT",
    "USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT"
]

# =========================
# Helpers
# =========================
def log(msg: str) -> None:
    print(msg, flush=True)

def utc_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def interval_to_ms(tf: str) -> int:
    tf = tf.strip().lower()
    if tf.endswith("m"):
        return int(tf[:-1]) * 60_000
    if tf.endswith("h"):
        return int(tf[:-1]) * 60 * 60_000
    if tf.endswith("d"):
        return int(tf[:-1]) * 24 * 60 * 60_000
    raise ValueError(f"Unsupported timeframe: {tf}")

def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt in env.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.candles (
            exchange TEXT DEFAULT 'binance',
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            open_time TIMESTAMPTZ NOT NULL,
            open DOUBLE PRECISION,
            high DOUBLE PRECISION,
            low DOUBLE PRECISION,
            close DOUBLE PRECISION,
            volume DOUBLE PRECISION,
            close_time TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (symbol, timeframe, open_time)
        );
        """)
    conn.commit()

def fetch_exchange_info() -> Dict[str, Any]:
    url = f"{BINANCE_BASE}/api/v3/exchangeInfo"
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

def is_good_symbol(s: str) -> bool:
    s = s.upper()
    if not s.endswith(QUOTE_ASSET):
        return False
    for bad in EXCLUDE_SUBSTRINGS:
        if bad in s:
            return False
    return True

def get_universe_symbols() -> List[str]:
    if SYMBOLS_ENV:
        syms = [x.strip().upper() for x in SYMBOLS_ENV.split(",") if x.strip()]
        return syms

    if not AUTO_UNIVERSE:
        # fallback minimal
        return ["BTCUSDT", "ETHUSDT", "BNBUSDT"]

    info = fetch_exchange_info()
    out: List[str] = []
    for sym in info.get("symbols", []):
        if sym.get("status") != "TRADING":
            continue
        if sym.get("quoteAsset", "").upper() != QUOTE_ASSET:
            continue
        # spot symbols only
        if sym.get("isSpotTradingAllowed") is False:
            continue
        name = sym.get("symbol", "").upper()
        if not name:
            continue
        if not is_good_symbol(name):
            continue
        out.append(name)

    out = sorted(set(out))
    if MAX_SYMBOLS and len(out) > MAX_SYMBOLS:
        out = out[:MAX_SYMBOLS]
    return out

def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[List[Any]]:
    url = f"{BINANCE_BASE}/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": KLINES_LIMIT,
    }
    r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

def insert_candles(conn, symbol: str, timeframe: str, rows: List[List[Any]]) -> int:
    if not rows:
        return 0

    data = []
    for k in rows:
        # Binance kline format:
        # 0 openTime, 1 open, 2 high, 3 low, 4 close, 5 volume, 6 closeTime, ...
        open_time = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)
        close_time = datetime.fromtimestamp(k[6] / 1000, tz=timezone.utc)

        data.append((
            "binance",
            symbol,
            timeframe,
            open_time,
            float(k[1]),
            float(k[2]),
            float(k[3]),
            float(k[4]),
            float(k[5]),
            close_time,
        ))

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO public.candles
            (exchange, symbol, timeframe, open_time, open, high, low, close, volume, close_time)
            VALUES %s
            ON CONFLICT (symbol, timeframe, open_time) DO NOTHING
            """,
            data,
            page_size=1000
        )
    conn.commit()
    return len(data)

# =========================
# Main
# =========================
def main() -> int:
    start = time.time()
    try:
        if not HIST_WRITE_DB:
            raise RuntimeError("HIST_WRITE_DB=1 vereist (we doen DB-only).")

        tfs = [t.strip() for t in TIMEFRAMES.split(",") if t.strip()]
        if not tfs:
            raise RuntimeError("TIMEFRAMES leeg.")

        symbols = get_universe_symbols()
        log(f"✅ universe symbols={len(symbols)} | quote={QUOTE_ASSET} | timeframes={tfs} | years={YEARS} | batch={BATCH_SIZE}")

        with db_connect() as conn:
            ensure_tables(conn)

            end_dt = now_utc()
            start_dt = end_dt.replace(year=end_dt.year - YEARS)
            end_ms = utc_ms(end_dt)

            for i in range(0, len(symbols), BATCH_SIZE):
                batch = symbols[i:i+BATCH_SIZE]
                log(f"--- batch {i//BATCH_SIZE + 1}/{math.ceil(len(symbols)/BATCH_SIZE)} size={len(batch)}")

                for sym in batch:
                    for tf in tfs:
                        try:
                            tf_ms = interval_to_ms(tf)
                            # loop pages from start->end
                            cur_start_ms = utc_ms(start_dt)

                            inserted_total = 0
                            while cur_start_ms < end_ms:
                                cur_end_ms = min(end_ms, cur_start_ms + tf_ms * KLINES_LIMIT)

                                kl = fetch_klines(sym, tf, cur_start_ms, cur_end_ms)
                                if not kl:
                                    break

                                inserted = insert_candles(conn, sym, tf, kl)
                                inserted_total += inserted

                                # next page: last openTime + interval
                                last_open = kl[-1][0]
                                cur_start_ms = last_open + tf_ms

                                time.sleep(SLEEP_BETWEEN_CALLS)

                            log(f"   {sym} {tf}: inserted≈{inserted_total}")

                        except Exception as e:
                            log(f"⚠️ {sym} {tf} error: {e}")
                            continue

                # small pause between batches
                time.sleep(1.0)

        dur = time.time() - start
        log(f"✅ DONE in {dur:.1f}s")
        return 0

    except Exception as e:
        log("❌ FATAL history_fetcher")
        log(str(e))
        log(traceback.format_exc())
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
