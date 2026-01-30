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
# ✅ WhatsApp (Twilio) helpers
# =========================
def _get_env(name: str, default: str = "") -> str:
    v = os.getenv(name, "")
    return v.strip() if isinstance(v, str) else default


TWILIO_ACCOUNT_SID = _get_env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = _get_env("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = _get_env("TWILIO_WHATSAPP_FROM")  # bv: "whatsapp:+14155238886"
MY_WHATSAPP_TO = _get_env("MY_WHATSAPP_TO")              # bv: "whatsapp:+316xxxxxxx"


def send_whatsapp(message: str) -> bool:
    """
    Stuurt WhatsApp via Twilio.
    Als env vars ontbreken: logt alleen en return False (bot blijft doorwerken).
    """
    msg = (message or "").strip()
    if not msg:
        return False

    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and MY_WHATSAPP_TO):
        print("ℹ️ WhatsApp melding overgeslagen (Twilio env vars ontbreken).", flush=True)
        return False

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    data = {
        "From": TWILIO_WHATSAPP_FROM,
        "To": MY_WHATSAPP_TO,
        "Body": msg,
    }

    try:
        r = requests.post(url, data=data, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=20)
        if r.status_code >= 200 and r.status_code < 300:
            print("✅ WhatsApp SELL melding verstuurd.", flush=True)
            return True
        print(f"⚠️ Twilio send failed: {r.status_code} {r.text[:200]}", flush=True)
        return False
    except Exception as e:
        print(f"⚠️ Twilio send error: {e}", flush=True)
        return False


def reason_nl(code: str) -> str:
    mapping = {
        "STOP_LOSS_BEFORE_1R": "Stop-loss geraakt vóór 1R (100% close).",
        "STRUCTURE_LOWER_LOW": "STRUCTUUR-MODE: eerste lower-low (100% close).",
        "UNDER_1R_3X_CLOSE_REST": "Na >1R: 3x onder 1R gebleven → rest sluiten (100% close).",
        "NO_POSITION_FOUND": "Geen positie gevonden (administratief gesloten).",
    }
    return mapping.get(code, code)


def format_sell_message(symbol: str, exit_price: Optional[float], pnl: Optional[float], r_multiple: Optional[float], reason_code: str) -> str:
    ep = f"{exit_price:.6f}" if isinstance(exit_price, (int, float)) else "?"
    pn = f"{pnl:.2f}" if isinstance(pnl, (int, float)) else "?"
    rm = f"{r_multiple:.2f}" if isinstance(r_multiple, (int, float)) else "?"
    return (
        "🔴 TRADE GESLOTEN (100%)\n\n"
        f"Coin: {symbol}\n"
        f"Exit prijs: {ep}\n"
        f"Resultaat (PnL): {pn} EUR\n"
        f"R-multiple: {rm}\n\n"
        f"Reden: {reason_nl(reason_code)}\n"
        f"Tijd: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )


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
        res = sell(symbol, 1.0)

        # alleen melding als SELL ok is
        if isinstance(res, dict) and res.get("ok"):
            msg = format_sell_message(
                symbol=symbol,
                exit_price=res.get("price"),
                pnl=res.get("pnl"),
                r_multiple=r,
                reason_code="STOP_LOSS_BEFORE_1R",
            )
            send_whatsapp(msg)

        trade["status"] = "CLOSED"
        trade["closed_reason"] = "STOP_LOSS_BEFORE_1R"
        trade["closed_at"] = now_ts()
        return True

    # ====== BONUS ROUTE: target reached => STRUCTUUR-MODE
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
                        res = sell(symbol, 1.0)

                        if isinstance(res, dict) and res.get("ok"):
                            msg = format_sell_message(
                                symbol=symbol,
                                exit_price=res.get("price"),
                                pnl=res.get("pnl"),
                                r_multiple=r,
                                reason_code="STRUCTURE_LOWER_LOW",
                            )
                            send_whatsapp(msg)

                        trade["status"] = "CLOSED"
                        trade["closed_reason"] = "STRUCTURE_LOWER_LOW"
                        trade["closed_at"] = now_ts()
                        return True
        except Exception as e:
            _print(f"⚠️ STRUCTUUR fetch error {symbol}: {e}")

        trade["last_check"] = now_ts()
        return False

    # ====== RULE 2: als >1R -> wachten
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
    if trade.get("partial_sold_40") and r < 1.0:
        trade["below_1r_count"] = int(trade.get("below_1r_count", 0)) + 1
        _print(f"⏳ {symbol} onder 1R count={trade['below_1r_count']}/3")
        if trade["below_1r_count"] >= 3:
            _print(f"🔚 {symbol} 3x onder 1R gebleven -> SELL 100% (rest sluiten)")
            res = sell(symbol, 1.0)

            if isinstance(res, dict) and res.get("ok"):
                msg = format_sell_message(
                    symbol=symbol,
                    exit_price=res.get("price"),
                    pnl=res.get("pnl"),
                    r_multiple=r,
                    reason_code="UNDER_1R_3X_CLOSE_REST",
                )
                send_whatsapp(msg)

            trade["status"] = "CLOSED"
            trade["closed_reason"] = "UNDER_1R_3X_CLOSE_REST"
            trade["closed_at"] = now_ts()
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
    open_trades = state.get("open_trades", [])

    if not open_trades:
        _print("ℹ️ Geen open_trades gevonden. Niets te monitoren.")
        return

    closed_symbols = []

    for trade in list(open_trades):
        symbol = trade.get("symbol")
        if only_symbol and symbol != only_symbol:
            continue

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
