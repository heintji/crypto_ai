# research/history_fetcher.py
from __future__ import annotations

import os
import time
import csv
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

import requests

# Postgres
import psycopg2

# =========================
# Binance
# =========================
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_LIMIT = 1000
HTTP_TIMEOUT = 25

EXCHANGE = "binance"

# =========================
# Storage (CSV)
# =========================
DATA_DIR = (os.getenv("DATA_DIR", "/data") or "/data").strip()
HISTORY_DIR = os.path.join(DATA_DIR, "history")

# =========================
# Database
# =========================
DATABASE_URL = (os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL".lower()) or "").strip()

# =========================
# ENV controls
# =========================
# Voorbeeld:
# HIST_SYMBOLS=BTCUSDT,ETHUSDT,XRPUSDT
# HIST_TIMEFRAMES=1h,4h
# HIST_YEARS=3
HIST_SYMBOLS = (os.getenv("HIST_SYMBOLS") or os.getenv("HIST_SYMBOL") or "BTCUSDT").strip()
HIST_TIMEFRAMES = (os.getenv("HIST_TIMEFRAMES") or os.getenv("HIST_INTERVAL") or "1h,4h").strip()
HIST_YEARS = int((os.getenv("HIST_YEARS") or os.getenv("HIST_YEARS_BACK") or "3").strip() or "3")

# Rate-limit friendly
SLEEP_S = float((os.getenv("HIST_SLEEP_SECONDS") or os.getenv("HIST_SLEEP_S") or "0.25").strip() or "0.25")

# Write modes
WRITE_CSV = (os.getenv("HIST_WRITE_CSV") or "1").strip() == "1"
WRITE_DB = (os.getenv("HIST_WRITE_DB") or "1").strip() == "1"


@dataclass
class FetchConfig:
    symbol: str
    interval: str
    years: int = 3
    sleep_s: float = 0.25
    out_path: Optional[str] = None


def _ensure_dirs() -> None:
    os.makedirs(HISTORY_DIR, exist_ok=True)


def _default_out_path(symbol: str, interval: str) -> str:
    safe_tf = interval.replace("/", "_")
    return os.path.join(HISTORY_DIR, f"{symbol}_{safe_tf}.csv")


def _utc_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _request_klines(symbol: str, interval: str, start_ms: int, limit: int = BINANCE_LIMIT) -> List[List]:
    r = requests.get(
        BINANCE_KLINES,
        params={"symbol": symbol, "interval": interval, "startTime": start_ms, "limit": limit},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return []
    return data


def _db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt. Zet DATABASE_URL in Render Environment Variables.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


UPSERT_SQL = """
INSERT INTO candles (
  exchange, symbol, timeframe, open_time,
  open, high, low, close, volume,
  close_time, trades, quote_volume, taker_buy_base, taker_buy_quote,
  updated_at
)
VALUES (
  %(exchange)s, %(symbol)s, %(timeframe)s, %(open_time)s,
  %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s,
  %(close_time)s, %(trades)s, %(quote_volume)s, %(taker_buy_base)s, %(taker_buy_quote)s,
  NOW()
)
ON CONFLICT (exchange, symbol, timeframe, open_time)
DO UPDATE SET
  open = EXCLUDED.open,
  high = EXCLUDED.high,
  low  = EXCLUDED.low,
  close= EXCLUDED.close,
  volume = EXCLUDED.volume,
  close_time = EXCLUDED.close_time,
  trades = EXCLUDED.trades,
  quote_volume = EXCLUDED.quote_volume,
  taker_buy_base = EXCLUDED.taker_buy_base,
  taker_buy_quote = EXCLUDED.taker_buy_quote,
  updated_at = NOW();
"""


def _kline_to_dbrow(symbol: str, interval: str, k: list) -> Dict[str, Any]:
    # Binance kline:
    # [0 openTime, 1 open, 2 high, 3 low, 4 close, 5 volume,
    #  6 closeTime, 7 quoteAssetVolume, 8 numberOfTrades,
    #  9 takerBuyBase, 10 takerBuyQuote, 11 ignore]
    open_time = datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc)
    close_time = datetime.fromtimestamp(int(k[6]) / 1000, tz=timezone.utc)
    return {
        "exchange": EXCHANGE,
        "symbol": symbol,
        "timeframe": interval,
        "open_time": open_time,
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "volume": float(k[5]),
        "close_time": close_time,
        "quote_volume": float(k[7]),
        "trades": int(k[8]),
        "taker_buy_base": float(k[9]),
        "taker_buy_quote": float(k[10]),
    }


def _db_resume_start_ms(symbol: str, interval: str, fallback_start_ms: int) -> int:
    """
    Resume vanaf laatste open_time in DB (als aanwezig),
    anders fallback naar (nu - years).
    """
    if not WRITE_DB:
        return fallback_start_ms

    conn = _db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT MAX(open_time) FROM candles WHERE exchange=%s AND symbol=%s AND timeframe=%s",
        (EXCHANGE, symbol, interval),
    )
    mx = cur.fetchone()[0]
    cur.close()
    conn.close()

    if not mx:
        return fallback_start_ms

    # 1 candle overlap om gaten te voorkomen
    ms = int(mx.replace(tzinfo=timezone.utc).timestamp() * 1000)
    return max(ms - (5 * 60 * 1000), 0)


def fetch_history(cfg: FetchConfig) -> str:
    """
    Download candles vanaf (nu - years) tot nu, in chunks van 1000.

    - Schrijft optioneel naar CSV in /data/history (WRITE_CSV)
    - Schrijft optioneel naar Postgres table candles via UPSERT (WRITE_DB)
    """
    _ensure_dirs()

    out_path = cfg.out_path or _default_out_path(cfg.symbol, cfg.interval)

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=int(365.25 * cfg.years))

    fallback_start_ms = _utc_ms(start_dt)
    end_ms = _utc_ms(end_dt)

    # Resume vanaf DB als mogelijk
    cursor = _db_resume_start_ms(cfg.symbol, cfg.interval, fallback_start_ms)

    print("🚀 HISTORY FETCH START")
    print(f"Symbol: {cfg.symbol} | Interval: {cfg.interval} | Years: {cfg.years}")
    print(f"From: {datetime.fromtimestamp(cursor/1000, tz=timezone.utc).isoformat()} -> To: {end_dt.isoformat()}")
    print(f"CSV: {'ON' if WRITE_CSV else 'OFF'} | DB: {'ON' if WRITE_DB else 'OFF'}")
    if WRITE_CSV:
        print(f"Output CSV: {out_path}")
    print("")

    csv_file = None
    writer = None

    if WRITE_CSV:
        header = [
            "open_time_ms",
            "open_time_utc",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time_ms",
            "trades",
            "quote_volume",
            "taker_buy_base",
            "taker_buy_quote",
        ]
        csv_file = open(out_path, "w", newline="", encoding="utf-8")
        writer = csv.writer(csv_file)
        writer.writerow(header)

    conn = None
    cur = None
    if WRITE_DB:
        conn = _db_connect()
        cur = conn.cursor()

    total_rows = 0
    loops = 0

    try:
        while cursor < end_ms:
            loops += 1
            try:
                chunk = _request_klines(cfg.symbol, cfg.interval, cursor, BINANCE_LIMIT)
            except Exception as e:
                print(f"⚠️ Request error: {e} -> retry in 2s")
                time.sleep(2)
                continue

            if not chunk:
                print("ℹ️ Geen data meer terug van Binance. Stop.")
                break

            # schrijf rows
            for k in chunk:
                open_time_ms = int(k[0])
                close_time_ms = int(k[6])
                open_time_utc = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).isoformat()

                if WRITE_CSV and writer:
                    writer.writerow([
                        open_time_ms,
                        open_time_utc,
                        k[1], k[2], k[3], k[4], k[5],
                        close_time_ms,
                        k[8],
                        k[7],
                        k[9],
                        k[10],
                    ])

                if WRITE_DB and cur:
                    row = _kline_to_dbrow(cfg.symbol, cfg.interval, k)
                    cur.execute(UPSERT_SQL, row)

                total_rows += 1

            if WRITE_DB and conn:
                conn.commit()

            last_open_ms = int(chunk[-1][0])
            last_close_ms = int(chunk[-1][6])

            # safety: voorkomt infinite loop
            if last_open_ms <= cursor:
                print("⚠️ Cursor loopt niet meer door (safety stop).")
                break

            cursor = last_close_ms + 1

            if loops % 5 == 0:
                print(
                    f"✅ chunks={loops} | rows={total_rows} | cursor={datetime.fromtimestamp(cursor/1000, tz=timezone.utc).isoformat()}"
                )

            time.sleep(cfg.sleep_s)

    finally:
        if csv_file:
            csv_file.close()
        if cur:
            cur.close()
        if conn:
            conn.close()

    print("")
    print(f"✅ HISTORY FETCH DONE | rows={total_rows}")
    if WRITE_CSV:
        print(f"📄 Saved CSV: {out_path}")
    return out_path


def main():
    symbols = [s.strip().upper() for s in HIST_SYMBOLS.split(",") if s.strip()]
    tfs = [t.strip() for t in HIST_TIMEFRAMES.split(",") if t.strip()]

    print("=== HISTORY FETCHER ===")
    print("SYMBOLS:", symbols)
    print("TIMEFRAMES:", tfs)
    print("YEARS:", HIST_YEARS)
    print("")

    for sym in symbols:
        for tf in tfs:
            try:
                fetch_history(FetchConfig(symbol=sym, interval=tf, years=HIST_YEARS, sleep_s=SLEEP_S))
            except Exception as e:
                print(f"❌ ERROR {sym} {tf}: {e}")


if __name__ == "__main__":
    main()
