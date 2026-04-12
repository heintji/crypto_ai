#!/usr/bin/env python3
"""
Fetch candles voor alle coins uit pending_approvals die achterlopende data hebben.
Eenmalig script — vult de gaps zodat signal_replay meer trades kan evalueren.
"""
import os, sys, time, requests, psycopg2
from datetime import datetime, timezone, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
BITVAVO_BASE = "https://api.bitvavo.com/v2"

def log(msg):
    print(f"[FETCH {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)

def db_connect():
    return psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=10)

def get_coins_needing_candles(conn):
    """Coins uit signalen die candle gaps hebben."""
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT pa.symbol
        FROM pending_approvals pa
        WHERE pa.score >= 80
        AND NOT EXISTS (
            SELECT 1 FROM experience_trades et
            WHERE et.trade_key = 'REPLAY|' || pa.symbol || '|' || pa.id
        )
        ORDER BY pa.symbol
    """)
    return [r[0] for r in cur.fetchall()]

def get_last_candle_time(conn, symbol, timeframe="1h"):
    """Laatste candle tijd voor een symbol."""
    cur = conn.cursor()
    cur.execute(
        "SELECT MAX(open_time) FROM candles WHERE symbol=%s AND timeframe=%s",
        (symbol, timeframe))
    r = cur.fetchone()
    return r[0] if r and r[0] else None

def fetch_bitvavo_candles(symbol_usdt, timeframe="1h", start_ms=None, limit=1000):
    """Haal candles op van Bitvavo."""
    coin = symbol_usdt.replace("USDT", "").replace("BUSD", "")
    market = f"{coin}-EUR"
    params = {"interval": timeframe, "limit": limit}
    if start_ms:
        params["start"] = start_ms
    try:
        resp = requests.get(f"{BITVAVO_BASE}/{market}/candles", params=params, timeout=15)
        if resp.status_code in (400, 404):
            return []  # market doesn't exist
        resp.raise_for_status()
        data = resp.json()
        # Bitvavo returns: [[timestamp, open, high, low, close, volume], ...]
        # Convert to Binance-like format for save_candles compatibility
        result = []
        for c in data:
            result.append([
                c[0],           # open time ms
                c[1],           # open
                c[2],           # high
                c[3],           # low
                c[4],           # close
                c[5],           # volume
                c[0] + 3600000, # close time (approx)
                0,              # quote volume
                0,              # trades
                0,              # taker buy base
                0,              # taker buy quote
            ])
        return result
    except Exception as e:
        log(f"  Bitvavo fout {market}: {e}")
        return []

def save_candles(conn, symbol, timeframe, candles):
    """Sla candles op in DB."""
    if not candles:
        return 0
    cur = conn.cursor()
    saved = 0
    for c in candles:
        try:
            cur.execute("""
                INSERT INTO candles (exchange, symbol, timeframe, open_time, open, high, low, close,
                                    volume, close_time, trades, quote_volume, taker_buy_base, taker_buy_quote)
                VALUES ('bitvavo', %s, %s, to_timestamp(%s/1000.0), %s, %s, %s, %s,
                        %s, to_timestamp(%s/1000.0), %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (symbol, timeframe,
                  c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4]),
                  float(c[5]), c[6], int(c[8]) if len(c) > 8 else 0,
                  float(c[7]) if len(c) > 7 else 0,
                  float(c[9]) if len(c) > 9 else 0,
                  float(c[10]) if len(c) > 10 else 0))
            saved += cur.rowcount
        except Exception:
            conn.rollback()
    conn.commit()
    return saved

def fetch_coin(conn, symbol, timeframe="1h"):
    """Fetch alle ontbrekende candles voor een coin."""
    last = get_last_candle_time(conn, symbol, timeframe)
    if last:
        last = last.replace(tzinfo=timezone.utc) if last.tzinfo is None else last
        start_ms = int(last.timestamp() * 1000) + 1
        gap_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        if gap_hours < 2:
            return 0  # al up to date
    else:
        # Geen candles — haal laatste 30 dagen op
        start_ms = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000)

    total_saved = 0
    while True:
        candles = fetch_bitvavo_candles(symbol, timeframe, start_ms)
        if not candles:
            break
        saved = save_candles(conn, symbol, timeframe, candles)
        total_saved += saved
        if len(candles) < 1000:
            break  # geen meer data
        start_ms = candles[-1][0] + 1
        time.sleep(0.2)  # rate limit
    return total_saved

def main():
    log("=" * 60)
    log("Signal Coins Fetcher — vul candle gaps")
    log("=" * 60)

    if not DATABASE_URL:
        log("FOUT: DATABASE_URL niet ingesteld")
        return

    conn = db_connect()
    coins = get_coins_needing_candles(conn)
    conn.close()
    log(f"Coins met ontbrekende replay data: {len(coins)}")

    total_coins = 0
    total_candles = 0
    errors = 0

    for i, symbol in enumerate(coins):
        # Verse connectie per coin — voorkomt timeout bij lange runs
        try:
            conn = db_connect()
        except Exception as e:
            log(f"  [{i+1}/{len(coins)}] DB reconnect fout: {e}")
            time.sleep(5)
            continue

        try:
            saved_1h = fetch_coin(conn, symbol, "1h")
            saved_4h = fetch_coin(conn, symbol, "4h")
            total = saved_1h + saved_4h

            if total > 0:
                total_coins += 1
                total_candles += total
                log(f"  [{i+1}/{len(coins)}] {symbol}: +{saved_1h} 1h, +{saved_4h} 4h candles")
            elif (i + 1) % 20 == 0:
                log(f"  [{i+1}/{len(coins)}] {symbol}: up to date")

            time.sleep(0.3)
        except Exception as e:
            errors += 1
            log(f"  [{i+1}/{len(coins)}] {symbol}: FOUT — {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    log(f"\n{'=' * 60}")
    log(f"Klaar: {total_coins} coins bijgewerkt, {total_candles} nieuwe candles, {errors} fouten")
    log(f"{'=' * 60}")

    try:
        from bot_health_helper import health_update
        health_update("fetch_signal_coins", "OK",
                      f"{total_coins} coins, {total_candles} candles")
    except Exception:
        pass

    conn.close()

if __name__ == "__main__":
    main()
