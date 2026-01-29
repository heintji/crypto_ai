import os
import sys
import json
import time
from typing import Dict, Any, List, Optional
import requests

# =========================
# ✅ Project-root fix (imports werken altijd)
# =========================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from trading.paper_trader import sell  # noqa: E402

STATE_PATH = os.path.join("data", "paper_state.json")

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
HTTP_TIMEOUT = 15

# Candle interval voor "STRUCTUUR-MODE"
STRUCT_INTERVAL = "1h"
STRUCT_LIMIT = 50

# Hoe vaak monitor draait (voor info/logging)
DEFAULT_SLEEP_SECONDS = 30 * 60  # 30 min

# =========================
# Helpers
# =========================
def _ensure_dirs():
    os.makedirs("data", exist_ok=True)
    os.makedirs("logs", exist_ok=True)

def load_state() -> Dict[str, Any]:
    _ensure_dirs()
    if not os.path.isfile(STATE_PATH):
        return {"balance": 1000.0, "positions": {}, "open_trades": []}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        s = json.load(f)
    s.setdefault("balance", 1000.0)
    s.setdefault("positions", {})
    s.setdefault("open_trades", [])
    return s

def save_state(state: Dict[str, Any]):
    _ensure_dirs()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def get_price(symbol: str) -> float:
    r = requests.get(BINANCE_TICKER_URL, params={"symbol": symbol}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return float(r.json()["price"])

def get_klines_lows(symbol: str, interval: str = STRUCT_INTERVAL, limit: int = STRUCT_LIMIT) -> List[float]:
    """
    Haalt lows van candles op. Binance kline format:
    [open_time, open, high, low, close, volume, close_time, ...]
    """
    r = requests.get(
        BINANCE_KLINES_URL,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    data = r.json()
    lows = []
    for k in data:
        lows.append(float(k[3]))
    return lows

def calc_r_multiple(price: float, entry: float, stop_loss: float) -> float:
    """
    R = (price - entry) / (entry - stop_loss)  (LONG)
    """
    risk = (entry - stop_loss)
    if risk <= 0:
        return 0.0
    return (price - entry) / risk

def now_ts() -> int:
    return int(time.time())

def _find_trade(open_trades: List[Dict[str, Any]], symbol: str) -> Optional[Dict[str, Any]]:
    for t in open_trades:
        if t.get("symbol") == symbol:
            return t
    return None

def _print(msg: str):
    print(msg, flush=True)

# =========================
# Core rules (jouw set)
# =========================
def process_trade(state: Dict[str, Any], trade: Dict[str, Any], test_price: Optional[float] = None) -> bool:
    """
    Returns True if trade is CLOSED (positie volledig verkocht), else False.
    """
    symbol = trade.get("symbol")
    entry = float(trade.get("entry", 0.0))
    stop_loss = float(trade.get("stop_loss", 0.0))
    target = float(trade.get("target", 0.0))

    if not symbol or entry <= 0 or stop_loss <= 0:
        _print(f"⚠️ Trade overslaan (ongeldige data): {trade}")
        return False

    price = float(test_price) if test_price is not None else get_price(symbol)
    r = calc_r_multiple(price, entry, stop_loss)

    # Init velden (stateful logic)
    trade.setdefault("mode", "NORMAL")  # NORMAL / STRUCTUUR
    trade.setdefault("status", "OPEN")
    trade.setdefault("max_r", 0.0)
    trade.setdefault("had_over_1r", False)
    trade.setdefault("partial_sold_40", False)
    trade.setdefault("below_1r_count", 0)
    trade.setdefault("target_reached_notified", False)
    trade.setdefault("last_check", now_ts())

    # Update max_r / flags
    trade["max_r"] = max(float(trade.get("max_r", 0.0)), r)
    if r >= 1.0:
        trade["had_over_1r"] = True

    _print(f"📊 {symbol} | price={price:.6f} | entry={entry:.6f} | SL={stop_loss:.6f} | R={r:.2f} | mode={trade['mode']}")

    # ====== RULE 1: Stop-loss vóór 1R => SELL 100%
    if price <= stop_loss and r < 1.0:
        _print(f"🛑 {symbol} -> STOP-LOSS geraakt vóór 1R => SELL 100%")
        sell(symbol, 1.0)
        trade["status"] = "CLOSED"
        trade["closed_reason"] = "STOP_LOSS_BEFORE_1R"
        trade["closed_at"] = now_ts()
        return True

    # ====== BONUS ROUTE: target reached => send message + STRUCTUUR-MODE
    # (we loggen hier alleen; later kun je WhatsApp push toevoegen als je wil)
    if target > 0 and price >= target and not trade.get("target_reached_notified", False):
        _print(f"🎯 {symbol} TARGET REACHED! -> switch naar STRUCTUUR-MODE")
        trade["target_reached_notified"] = True
        trade["mode"] = "STRUCTUUR"
        # init structuur tracking
        trade.setdefault("struct_trailing_low", None)

    # ====== STRUCTUUR-MODE: follow higher lows; first lower low => SELL 100%
    if trade.get("mode") == "STRUCTUUR":
        try:
            lows = get_klines_lows(symbol)
            if len(lows) >= 3:
                # simpele "higher-lows" tracker:
                # we nemen de low van de laatst gesloten candle (1 candle terug)
                last_closed_low = lows[-2]

                trailing = trade.get("struct_trailing_low")
                if trailing is None:
                    trade["struct_trailing_low"] = last_closed_low
                    _print(f"🧱 {symbol} STRUCTUUR init trailing_low={last_closed_low:.6f}")
                else:
                    trailing = float(trailing)
                    # hogere low? dan trailing omhoog
                    if last_closed_low > trailing:
                        trade["struct_trailing_low"] = last_closed_low
                        _print(f"🧱 {symbol} STRUCTUUR higher-low -> trailing_low={last_closed_low:.6f}")
                    # lagere low? exit
                    elif last_closed_low < trailing:
                        _print(f"🔻 {symbol} STRUCTUUR lower-low DETECTED -> SELL 100%")
                        sell(symbol, 1.0)
                        trade["status"] = "CLOSED"
                        trade["closed_reason"] = "STRUCTURE_LOWER_LOW"
                        trade["closed_at"] = now_ts()
                        return True
        except Exception as e:
            _print(f"⚠️ STRUCTUUR fetch error {symbol}: {e}")

        # In STRUCTUUR-MODE doen we verder geen 1R/40% regels meer.
        trade["last_check"] = now_ts()
        return False

    # ====== RULE 2: als >1R -> wachten (niets verkopen)
    # (dus geen actie zolang hij >1R blijft)
    if r >= 1.0:
        trade["below_1r_count"] = 0
        trade["last_check"] = now_ts()
        return False

    # ====== RULE 3: na >1R, zakt hij <1R -> SELL 40%
    if trade.get("had_over_1r") and r < 1.0 and not trade.get("partial_sold_40"):
        _print(f"⚠️ {symbol} was >1R, nu <1R -> SELL 40%")
        sell(symbol, 0.40)
        trade["partial_sold_40"] = True
        trade["below_1r_count"] = 1
        trade["last_check"] = now_ts()
        return False

    # ====== RULE 4: als 3 checks/candles onder 1R blijven -> SELL remaining 60%
    # (we tellen monitor-cycles als 'candles' omdat jij elke 30 min draait)
    if trade.get("partial_sold_40") and r < 1.0:
        trade["below_1r_count"] = int(trade.get("below_1r_count", 0)) + 1
        _print(f"⏳ {symbol} onder 1R count={trade['below_1r_count']}/3")
        if trade["below_1r_count"] >= 3:
            _print(f"🔚 {symbol} 3x onder 1R gebleven -> SELL 100% (rest sluiten)")
            sell(symbol, 1.0)
            trade["status"] = "CLOSED"
            trade["closed_reason"] = "UNDER_1R_3X_CLOSE_REST"
            trade["closed_at"] = now_ts()
            return True
    else:
        # terug boven 1R? reset count (al wordt dit pad meestal eerder afgevangen)
        trade["below_1r_count"] = 0

    trade["last_check"] = now_ts()
    return False

# =========================
# Main loop
# =========================
def run_once(test_price: Optional[float] = None, only_symbol: Optional[str] = None):
    state = load_state()
    open_trades = state.get("open_trades", [])

    if not open_trades:
        _print("ℹ️ Geen open_trades gevonden. Niets te monitoren.")
        return

    closed_symbols = []

    for trade in list(open_trades):
        symbol = trade.get("symbol")
        if only_symbol and symbol != only_symbol:
            continue

        # Als er geen positie (meer) is, sluiten we trade administratief
        positions = state.get("positions", {})
        if float(positions.get(symbol, 0.0)) <= 0:
            trade["status"] = "CLOSED"
            trade["closed_reason"] = "NO_POSITION_FOUND"
            trade["closed_at"] = now_ts()
            closed_symbols.append(symbol)
            continue

        closed = process_trade(state, trade, test_price=test_price)
        if closed:
            closed_symbols.append(symbol)

    # remove closed trades
    if closed_symbols:
        state["open_trades"] = [t for t in state.get("open_trades", []) if t.get("symbol") not in closed_symbols]
        save_state(state)
        _print(f"✅ Closed trades removed: {closed_symbols}")
    else:
        save_state(state)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Crypto_AI trade monitor (step 4).")
    parser.add_argument("--once", action="store_true", help="Run 1 check and exit.")
    parser.add_argument("--sleep", type=int, default=DEFAULT_SLEEP_SECONDS, help="Seconds between cycles.")
    parser.add_argument("--symbol", type=str, default=None, help="Only monitor one symbol (e.g., BTCUSDT).")
    parser.add_argument("--test-price", type=float, default=None, help="Override price for testing.")
    args = parser.parse_args()

    _print("🚦 trade_monitor gestart")
    _print(f"Project root: {PROJECT_ROOT}")

    if args.once:
        run_once(test_price=args.test_price, only_symbol=args.symbol)
        return

    while True:
        try:
            run_once(test_price=args.test_price, only_symbol=args.symbol)
        except Exception as e:
            _print(f"⚠️ Monitor error: {e}")
        time.sleep(int(args.sleep))

if __name__ == "__main__":
    main()

