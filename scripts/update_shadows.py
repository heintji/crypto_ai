# scripts/update_shadows.py
import os
import sys
import time
import requests

# Zorg dat project-root (crypto_ai) in sys.path staat
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from trading.shadow_trades import load_shadows, save_shadows  # noqa: E402

BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"


def get_price(symbol: str) -> float:
    r = requests.get(BINANCE_TICKER, params={"symbol": symbol}, timeout=15)
    r.raise_for_status()
    return float(r.json()["price"])


def update_one(shadow: dict, now: int) -> dict:
    if str(shadow.get("status", "")).upper() != "OPEN":
        return shadow

    symbol = shadow.get("symbol")
    if not symbol:
        shadow["status"] = "ERROR"
        shadow["result"] = "MISSING_SYMBOL"
        shadow["updated_at"] = now
        return shadow

    entry = float(shadow.get("entry", 0.0))
    stop_loss = float(shadow.get("stop_loss", 0.0))
    target = float(shadow.get("target", 0.0))
    expires_at = int(shadow.get("expires_at", 0) or 0)

    price = get_price(symbol)

    # tracking
    shadow["last_price"] = float(price)
    shadow["mfe_price"] = float(max(float(shadow.get("mfe_price", entry)), price))
    shadow["mae_price"] = float(min(float(shadow.get("mae_price", entry)), price))
    shadow["updated_at"] = now

    # Sluitregels: eerst SL/TP, daarna expiry
    if stop_loss > 0 and price <= stop_loss:
        shadow["status"] = "STOP_HIT"
        shadow["result"] = "SL"
        shadow["closed_at"] = now
        shadow["closed_price"] = float(price)
        return shadow

    if target > 0 and price >= target:
        shadow["status"] = "TARGET_HIT"
        shadow["result"] = "TP"
        shadow["closed_at"] = now
        shadow["closed_price"] = float(price)
        return shadow

    if expires_at and now > expires_at:
        shadow["status"] = "EXPIRED"
        shadow["result"] = "EXPIRED"
        shadow["closed_at"] = now
        shadow["closed_price"] = float(price)
        return shadow

    return shadow


def main():
    now = int(time.time())
    shadows = load_shadows()

    if not shadows:
        print("No shadow trades yet.")
        return

    changed = 0
    for i in range(len(shadows)):
        before = dict(shadows[i])
        shadows[i] = update_one(shadows[i], now)
        if shadows[i] != before:
            changed += 1

    save_shadows(shadows)
    print(f"Shadow update done. Updated/closed: {changed} | Total: {len(shadows)}")


if __name__ == "__main__":
    main()
