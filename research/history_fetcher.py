# research/history_fetcher.py
from __future__ import annotations

import os
import time
import csv
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

import requests


BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
HTTP_TIMEOUT = 25

DATA_DIR = os.getenv("DATA_DIR", "/data").strip() or "/data"
HISTORY_DIR = os.path.join(DATA_DIR, "history")

# Binance max 1000 candles per request
BINANCE_LIMIT = 1000


@dataclass
class FetchConfig:
    symbol: str = "BTCUSDT"
    interval: str = "1h"
    years: int = 3
    sleep_s: float = 0.25  # rate-limit vriendelijk
    out_path: Optional[str] = None


def _utc_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _ensure_dirs() -> None:
    os.makedirs(HISTORY_DIR, exist_ok=True)


def _default_out_path(symbol: str, interval: str) -> str:
    return os.path.join(HISTORY_DIR, f"{symbol}_{interval}.csv")


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


def fetch_history(cfg: FetchConfig) -> str:
    """
    Download candles vanaf (nu - years) tot nu, in chunks van 1000.
    Schrijft naar CSV in /data/history.
    """
    _ensure_dirs()

    out_path = cfg.out_path or _default_out_path(cfg.symbol, cfg.interval)

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=365 * cfg.years)

    start_ms = _utc_ms(start_dt)
    end_ms = _utc_ms(end_dt)

    print(f"🚀 HISTORY FETCH START")
    print(f"Symbol: {cfg.symbol} | Interval: {cfg.interval} | Years: {cfg.years}")
    print(f"From: {start_dt.isoformat()} -> To: {end_dt.isoformat()}")
    print(f"Output: {out_path}")
    print("")

    # CSV header
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
    ]

    # we schrijven opnieuw (bewust) zodat je altijd “clean” hebt
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)

        cursor = start_ms
        total_rows = 0
        loops = 0

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
                # Binance format:
                # [0 open_time, 1 open, 2 high, 3 low, 4 close, 5 volume,
                #  6 close_time, 7 quote_asset_volume, 8 number_of_trades,
                #  9 taker_buy_base, 10 taker_buy_quote, 11 ignore]
                open_time_ms = int(k[0])
                close_time_ms = int(k[6])
                open_time_utc = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).isoformat()

                writer.writerow([
                    open_time_ms,
                    open_time_utc,
                    k[1], k[2], k[3], k[4], k[5],
                    close_time_ms,
                    k[8],
                ])
                total_rows += 1

            # cursor -> volgende candle na laatste close_time
            last_open_ms = int(chunk[-1][0])
            last_close_ms = int(chunk[-1][6])

            # als Binance dezelfde chunk blijft geven: safety break
            if last_open_ms <= cursor:
                print("⚠️ Cursor loopt niet meer door (safety stop).")
                break

            cursor = last_close_ms + 1

            if loops % 5 == 0:
                print(f"✅ chunks={loops} | rows={total_rows} | cursor={datetime.fromtimestamp(cursor/1000, tz=timezone.utc).isoformat()}")

            time.sleep(cfg.sleep_s)

    print("")
    print(f"✅ HISTORY FETCH DONE | rows={total_rows}")
    return out_path


def main():
    symbol = os.getenv("HIST_SYMBOL", "BTCUSDT").strip() or "BTCUSDT"
    interval = os.getenv("HIST_INTERVAL", "1h").strip() or "1h"
    years_s = (os.getenv("HIST_YEARS", "3") or "3").strip()
    try:
        years = int(years_s)
    except Exception:
        years = 3

    out = fetch_history(FetchConfig(symbol=symbol, interval=interval, years=years))
    print(f"📄 Saved: {out}")


if __name__ == "__main__":
    main()
