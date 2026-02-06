import os
import sys
import json
import time
from typing import Dict, Any, List, Optional
import requests

# =========================
# ✅ Project-root fix (imports werken altijd)
# trade_monitor.py staat in project-root
# =========================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ✅ BELANGRIJK:
# paper_trader.sell() moet straks LIVE verkopen (Bitvavo) als jij paper_trader ombouwt.
from trading.paper_trader import sell  # noqa: E402

# =========================
# ✅ Render Disk paths
# =========================
STATE_PATH = os.getenv("PAPER_STATE_PATH", "/data/paper_state.json")
FORCE_EXIT_LOCK_PATH = os.getenv("FORCE_EXIT_LOCK_PATH", "/data/force_test_exit.lock")

# =========================
# Binance endpoints (voor prijs / structuur)
# =========================
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
HTTP_TIMEOUT = 15

STRUCT_INTERVAL = "1h"
STRUCT_LIMIT = 50

DEFAULT_SLEEP_SECONDS = int(os.getenv("MONITOR_SLEEP_SECONDS", str(30 * 60)))  # 30 min

FORCE_TEST_EXIT = str(os.getenv("FORCE_TEST_EXIT", "0")).strip().lower() in {"1", "true", "yes", "on"}

# =========================
# WhatsApp (Twilio)
# =========================
def twilio_ready() -> bool:
    return all([
        os.getenv("TWILIO_ACCOUNT_SID"),
        os.getenv("TWILIO_AUTH_TOKEN"),
        os.getenv("TWILIO_WHATSAPP_FROM"),
        os.getenv("TWILIO_WHATSAPP_TO"),
    ])

def send_whatsapp(message: str) -> bool:
    if not twilio_ready():
        _print("📭 WhatsApp melding overgeslagen (Twilio env vars ontbreken).")
        return False

    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    wa_from = os.getenv("TWILIO_WHATSAPP_FROM")
    wa_to = os.getenv("TWILIO_WHATSAPP_TO")

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = {"From": wa_from, "To": wa_to, "Body": message}

    try:
        r = requests.post(url, data=data, auth=(sid, token), timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            _print(f"⚠️ Twilio send failed ({r.status_code}): {r.text[:200]}")
            return False
        return True
    except Exception as e:
        _print(f"⚠️ Twilio send exception: {e}")
        return False

# =========================
# Helpers
# =========================
def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

def load_state() -> Dict[str, Any]:
    ensure_parent_dir(STATE_PATH)
    if not os.path.isfile(STATE_PATH):
        return {"balance": 1000.0, "positions": {}, "open_trades": []}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        return {"balance": 1000.0, "positions": {}, "open_trades": []}

    s.setdefault("balance", 1000.0)
    s.setdefault("positions", {})
    s.setdefault("open_trades", [])
    return s

def save_state(state: Dict[str, Any]) -> None:
    ensure_parent_dir(STATE_PATH)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)

def now_ts() -> int:
    return int(time.time())

def _print(msg: str):
    print(msg, flush=True)

# =========================
# ✅ Symbol mapping voor LIVE Bitvavo
# =========================
def to_bitvavo_market(symbol: str) -> str:
    """
    Jij gebruikt Binance symbols zoals BTCUSDT.
    Bitvavo werkt meestal met EUR-markten zoals BTC-EUR.

    - BTCUSDT -> BTC-EUR
    - ETHUSDT -> ETH-EUR

    Als jij later liever USDC of andere quote wilt, pas dit hier aan.
    """
    s = (symbol or "").upper().strip()
    if s.endswith("USDT"):
        base = s.replace("USDT", "")
        return f"{base}-EUR"
    # fallback: als je al BTC-EUR geeft, laat hem door
    if "-" in s:
        return s
    # anders: gok EUR
    return f"{s}-EUR"

# =========================
# Market data (voor R + structuur)
# =========================
def get_price(symbol: str) -> float:
    r = requests.get(BINANCE_TICKER_URL, params={"symbol": symbol}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return float(r.json()["price"])

def get_klines_lows(symbol: str, interval: str = STRUCT_INTERVAL, limit: int = STRUCT_LIMIT) -> List[float]:
    r = requests.get(
        BINANCE_KLINES_URL,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    data = r.json()
    return [float(k[3]) for k in data]

def calc_r_multiple(price: float, entry: float, stop_loss: float) -> float:
    risk = (entry - stop_loss)
    if risk <= 0:
        return 0.0
    return (price - entry) / risk

def _send_sell_close_message(
    symbol: str,
    reason: str,
    entry: float,
    stop_loss: float,
    target: float,
    r_now: float,
    sell_result: Dict[str, Any]
):
    """
    WhatsApp melding alleen bij 100% close.
    """
    if not isinstance(sell_result, dict) or not sell_result.get("ok"):
        # als live sell faalt: stuur ook WA fout zodat je het ziet
        send_whatsapp(
            f"⚠️ SELL MISLUKT ({symbol})\n"
            f"Reden: {reason}\n"
            f"Details: {sell_result}"
        )
        return

    exit_price = float(sell_result.get("price", 0.0))
    pnl = float(sell_result.get("pnl", 0.0))
    balance = float(sell_result.get("balance", 0.0))

    msg = (
        f"✅ SELL 100% uitgevoerd ({symbol})\n"
        f"Reden: {reason}\n"
        f"Entry: {entry:.6f}\n"
        f"Exit: {exit_price:.6f}\n"
        f"Stop: {stop_loss:.6f}\n"
        f"Target: {target:.6f}\n"
        f"R (bij exit): {r_now:.2f}\n"
        f"PnL: {pnl:.2f}\n"
        f"Saldo: €{balance:.2f}"
    )
    send_whatsapp(msg)

# =========================
# ✅ FORCE EXIT (1x)
# =========================
def _force_exit_once_if_enabled(state: Dict[str, Any]) -> bool:
    if not FORCE_TEST_EXIT:
        return False

    ensure_parent_dir(FORCE_EXIT_LOCK_PATH)

    if os.path.exists(FORCE_EXIT_LOCK_PATH):
        _print("FORCE_TEST_EXIT=ON maar lock bestaat al → geen tweede forced exit.")
        return False

    open_trades = state.get("open_trades", []) or []
    if not open_trades:
        _print("FORCE_TEST_EXIT=ON maar geen open_trades gevonden.")
        return False

    trade = open_trades[0]
    symbol = trade.get("symbol")
    entry = float(trade.get("entry", 0.0))
    stop_loss = float(trade.get("stop_loss", 0.0))
    target = float(trade.get("target", 0.0))

    if not symbol:
        _print("FORCE_TEST_EXIT: trade mist symbol.")
        return False

    positions = state.get("positions", {}) or {}
    qty = float(positions.get(symbol, 0.0))
    if qty <= 0:
        _print(f"FORCE_TEST_EXIT: geen positie gevonden voor {symbol} (qty<=0).")
        return False

    try:
        price = get_price(symbol)
    except Exception as e:
        _print(f"FORCE_TEST_EXIT: price fetch error {symbol}: {e}")
        price = entry if entry > 0 else 0.0

    r_now = calc_r_multiple(price, entry, stop_loss) if entry > 0 and stop_loss > 0 else 0.0

    _print(f"🚨 FORCE_TEST_EXIT: SELL 100% for {symbol} (test)")

    # ✅ Live mapping: verkoop op Bitvavo market
    market = to_bitvavo_market(symbol)
    sell_res = sell(market, 1.0)

    trade["status"] = "CLOSED"
    trade["closed_reason"] = "FORCE_TEST_EXIT"
    trade["closed_at"] = now_ts()

    _send_sell_close_message(
        symbol=symbol,
        reason="FORCE TEST EXIT (debug)",
        entry=entry,
        stop_loss=stop_loss,
        target=target,
        r_now=r_now,
        sell_result=sell_res,
    )

    state["open_trades"] = [t for t in state.get("open_trades", []) if t.get("symbol") != symbol]
    save_state(state)

    with open(FORCE_EXIT_LOCK_PATH, "w", encoding="utf-8") as f:
        f.write(str(now_ts()))

    _print(f"✅ FORCE_TEST_EXIT uitgevoerd en gelocked. (lock: {FORCE_EXIT_LOCK_PATH})")
    return True

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

    trade.setdefault("mode", "NORMAL")  # NORMAL / STRUCTUUR
    trade.setdefault("status", "OPEN")
    trade.setdefault("max_r", 0.0)
    trade.setdefault("had_over_1r", False)
    trade.setdefault("partial_sold_40", False)
    trade.setdefault("below_1r_count", 0)
    trade.setdefault("target_reached_notified", False)
    trade.setdefault("last_check", now_ts())

    trade["max_r"] = max(float(trade.get("max_r", 0.0)), r)
    if r >= 1.0:
        trade["had_over_1r"] = True

    _print(f"📊 {symbol} | price={price:.6f} | entry={entry:.6f} | SL={stop_loss:.6f} | R={r:.2f} | mode={trade['mode']}")

    # ✅ market voor live sell
    market = to_bitvavo_market(symbol)

    # RULE 1: Stop-loss vóór 1R => SELL 100%
    if price <= stop_loss and r < 1.0:
        _print(f"🛑 {symbol} -> STOP-LOSS geraakt vóór 1R => SELL 100%")
        sell_res = sell(market, 1.0)

        trade["status"] = "CLOSED"
        trade["closed_reason"] = "STOP_LOSS_BEFORE_1R"
        trade["closed_at"] = now_ts()

        _send_sell_close_message(
            symbol=symbol,
            reason="STOP-LOSS vóór 1R",
            entry=entry,
            stop_loss=stop_loss,
            target=target,
            r_now=r,
            sell_result=sell_res
        )
        return True

    # BONUS ROUTE: target reached => message + STRUCTUUR-MODE
    if target > 0 and price >= target and not trade.get("target_reached_notified", False):
        _print(f"🎯 {symbol} TARGET REACHED! -> switch naar STRUCTUUR-MODE")
        trade["target_reached_notified"] = True
        trade["mode"] = "STRUCTUUR"
        trade.setdefault("struct_trailing_low", None)

        send_whatsapp(
            f"🎯 Target bereikt ({symbol})\n"
            f"Price: {price:.6f}\n"
            f"Entry: {entry:.6f}\n"
            f"Target: {target:.6f}\n"
            f"Mode: STRUCTUUR-MODE"
        )

    # STRUCTUUR-MODE
    if trade.get("mode") == "STRUCTUUR":
        try:
            lows = get_klines_lows(symbol)
            if len(lows) >= 3:
                last_closed_low = lows[-2]

                trailing = trade.get("struct_trailing_low")
                if trailing is None:
                    trade["struct_trailing_low"] = last_closed_low
                    _print(f"🧱 {symbol} STRUCTUUR init trailing_low={last_closed_low:.6f}")
                else:
                    trailing = float(trailing)
                    if last_closed_low > trailing:
                        trade["struct_trailing_low"] = last_closed_low
                        _print(f"🧱 {symbol} STRUCTUUR higher-low -> trailing_low={last_closed_low:.6f}")
                    elif last_closed_low < trailing:
                        _print(f"🔻 {symbol} STRUCTUUR lower-low DETECTED -> SELL 100%")
                        sell_res = sell(market, 1.0)

                        trade["status"] = "CLOSED"
                        trade["closed_reason"] = "STRUCTURE_LOWER_LOW"
                        trade["closed_at"] = now_ts()

                        _send_sell_close_message(
                            symbol=symbol,
                            reason="STRUCTUUR: eerste lower-low",
                            entry=entry,
                            stop_loss=stop_loss,
                            target=target,
                            r_now=r,
                            sell_result=sell_res
                        )
                        return True
        except Exception as e:
            _print(f"⚠️ STRUCTUUR fetch error {symbol}: {e}")

        trade["last_check"] = now_ts()
        return False

    # RULE 2: als >1R -> wachten
    if r >= 1.0:
        trade["below_1r_count"] = 0
        trade["last_check"] = now_ts()
        return False

    # RULE 3: na >1R, zakt hij <1R -> SELL 40%
    if trade.get("had_over_1r") and r < 1.0 and not trade.get("partial_sold_40"):
        _print(f"⚠️ {symbol} was >1R, nu <1R -> SELL 40%")
        # 40% live sell
        sell(market, 0.40)
        trade["partial_sold_40"] = True
        trade["below_1r_count"] = 1
        trade["last_check"] = now_ts()
        return False

    # RULE 4: 3 checks onder 1R -> SELL rest
    if trade.get("partial_sold_40") and r < 1.0:
        trade["below_1r_count"] = int(trade.get("below_1r_count", 0)) + 1
        _print(f"⏳ {symbol} onder 1R count={trade['below_1r_count']}/3")
        if trade["below_1r_count"] >= 3:
            _print(f"🔚 {symbol} 3x onder 1R gebleven -> SELL 100% (rest sluiten)")
            sell_res = sell(market, 1.0)

            trade["status"] = "CLOSED"
            trade["closed_reason"] = "UNDER_1R_3X_CLOSE_REST"
            trade["closed_at"] = now_ts()

            _send_sell_close_message(
                symbol=symbol,
                reason="Na >1R: 3x onder 1R (rest gesloten)",
                entry=entry,
                stop_loss=stop_loss,
                target=target,
                r_now=r,
                sell_result=sell_res
            )
            return True
    else:
        trade["below_1r_count"] = 0

    trade["last_check"] = now_ts()
    return False

# =========================
# Main loop
# =========================
def run_once(test_price: Optional[float] = None, only_symbol: Optional[str] = None):
    state = load_state()

    # ✅ FORCE EXIT check (1x)
    if _force_exit_once_if_enabled(state):
        return

    open_trades = state.get("open_trades", []) or []
    if not open_trades:
        _print("ℹ️ Geen open_trades gevonden. Niets te monitoren.")
        return

    closed_symbols: List[str] = []

    for trade in list(open_trades):
        symbol = trade.get("symbol")
        if only_symbol and symbol != only_symbol:
            continue

        positions = state.get("positions", {}) or {}
        if float(positions.get(symbol, 0.0)) <= 0:
            trade["status"] = "CLOSED"
            trade["closed_reason"] = "NO_POSITION_FOUND"
            trade["closed_at"] = now_ts()
            closed_symbols.append(symbol)
            continue

        closed = process_trade(state, trade, test_price=test_price)
        if closed:
            closed_symbols.append(symbol)

    if closed_symbols:
        state["open_trades"] = [t for t in state.get("open_trades", []) if t.get("symbol") not in closed_symbols]
        save_state(state)
        _print(f"✅ Closed trades removed: {closed_symbols}")
    else:
        save_state(state)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Crypto_AI trade monitor (exit checks).")
    parser.add_argument("--once", action="store_true", help="Run 1 check and exit.")
    parser.add_argument("--sleep", type=int, default=DEFAULT_SLEEP_SECONDS, help="Seconds between cycles.")
    parser.add_argument("--symbol", type=str, default=None, help="Only monitor one symbol (e.g., BTCUSDT).")
    parser.add_argument("--test-price", type=float, default=None, help="Override price for testing.")
    args = parser.parse_args()

    _print("🚦 trade_monitor gestart")
    _print(f"Project root: {PROJECT_ROOT}")
    _print(f"State path: {STATE_PATH}")
    _print(f"FORCE_TEST_EXIT: {'ON' if FORCE_TEST_EXIT else 'OFF'}")

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
