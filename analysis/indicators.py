import requests

BITVAVO_BASE = "https://api.bitvavo.com/v2"

def get_candles(market="BTC-EUR", interval="1h", limit=120):
    url = f"{BITVAVO_BASE}/{market}/candles"
    params = {"interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def rsi_wilder(values, period=14):
    """
    Standaard RSI (Wilder). Minder ruis dan een simpele RSI.
    """
    if len(values) < period + 1:
        return None

    # 1) price changes
    changes = [values[i] - values[i-1] for i in range(1, len(values))]

    gains = [max(c, 0) for c in changes]
    losses = [max(-c, 0) for c in changes]

    # 2) start averages (first period)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # 3) Wilder smoothing over remaining data
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def main():
    market = "BTC-EUR"
    interval = "1h"
    limit = 120

    candles = get_candles(market, interval, limit)
    # Bitvavo candle format: [timestamp, open, high, low, close, volume]
    closes = [float(c[4]) for c in candles]

    last_price = closes[-1]
    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    rsi14 = rsi_wilder(closes, 14)

    print("=== INDICATORS (BITVAVO) ===")
    print(f"Market: {market}")
    print(f"Last price: {last_price:.2f}")
    print(f"SMA20: {sma20:.2f}" if sma20 is not None else "SMA20: n/a")
    print(f"SMA50: {sma50:.2f}" if sma50 is not None else "SMA50: n/a")
    print(f"RSI14: {rsi14:.2f}" if rsi14 is not None else "RSI14: n/a")

if __name__ == "__main__":
    main()
