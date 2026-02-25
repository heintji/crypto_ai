import os
import sys
import json
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import requests

import psycopg2
import psycopg2.extras

# =========================
# ✅ Project-root fix
# =========================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# =========================
# ✅ DATA PATH RESOLVER
# =========================
def _get_data_dir() -> str:
    d = (os.getenv("DATA_DIR") or "").strip()
    if d:
        return d
    return "/data" if os.path.isdir("/data") else "/tmp/data"

DATA_DIR = _get_data_dir()
LOGS_DIR = (os.getenv("LOGS_DIR") or os.path.join(DATA_DIR, "logs")).strip()

STATE_PATH = (os.getenv("PAPER_STATE_PATH") or os.path.join(DATA_DIR, "paper_state.json")).strip()
FORCE_EXIT_LOCK_PATH = (os.getenv("FORCE_EXIT_LOCK_PATH") or os.path.join(DATA_DIR, "force_test_exit.lock")).strip()

# =========================
# Binance endpoints (prijs/structuur)
# =========================
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
HTTP_TIMEOUT = 15

STRUCT_INTERVAL = "1h"
STRUCT_LIMIT = 50

DEFAULT_SLEEP_SECONDS = int(os.getenv("MONITOR_SLEEP_SECONDS", str(30 * 60)))  # 30 min
FORCE_TEST_EXIT = str(os.getenv("FORCE_TEST_EXIT", "0")).strip().lower() in {"1", "true", "yes", "on"}

# =========================
# ✅ TRADER MODE
# =========================
# "paper" -> altijd paper_trader
# "live"  -> altijd live_trader
# "auto"  -> per trade: als trade["live"]=True => live, anders paper
TRADER_MODE = (os.getenv("TRADER_MODE") or "auto").strip().lower()

# =========================
# ✅ DATABASE (Experience)
# =========================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

def db_ready() -> bool:
    return bool(DATABASE_URL)

def db_connect():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def now_utc_dt() -> datetime:
    return datetime.now(timezone.utc)

def _print(msg: str):
    print(msg, flush=True)

def _table_exists(conn, fq_table: str) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s);", (fq_table,))
            return cur.fetchone()[0] is not None
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False

def _has_column(conn, table: str, col: str, schema: str = "public") -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS(
                  SELECT 1
                  FROM information_schema.columns
                  WHERE table_schema=%s AND table_name=%s AND column_name=%s
                )
                """,
                (schema, table, col),
            )
            return bool(cur.fetchone()[0])
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False

def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default

def _infer_outcome(result_r: float) -> str:
    if abs(result_r) < 0.05:
        return "BREAKEVEN"
    return "WIN" if result_r > 0 else "LOSS"

def _infer_label(trade: Dict[str, Any]) -> str:
    if isinstance(trade.get("label"), str) and trade["label"].strip():
        return trade["label"].strip().upper()
    score = _safe_int(trade.get("score"), 0)
    if score >= 80:
        return "GO"
    if score >= 70:
        return "WATCH"
    return "SKIP"

def _experience_insert_trade(conn, trade: Dict[str, Any], *, close_price: float, result_r: float, outcome: str) -> None:
    if not _table_exists(conn, "public.experience_trades"):
        return

    cols: List[str] = []
    vals: List[Any] = []

    candidates = [
        ("symbol", trade.get("symbol")),
        ("setup_type", trade.get("setup_type") or "TREND_PULLBACK"),
        ("timeframe", trade.get("timeframe") or trade.get("tf") or "4h"),
        ("regime", (trade.get("regime") or "UNKNOWN")),
        ("label", _infer_label(trade)),
        ("score", _safe_int(trade.get("score"), 0)),
        ("raw_score", trade.get("raw_score")),
        ("chance", _safe_int(trade.get("chance"), 0)),
        ("confidence", _safe_int(trade.get("confidence"), trade.get("chance") or 0)),
        ("entry", _safe_float(trade.get("entry"), 0.0)),
        ("stop", _safe_float(trade.get("stop") or trade.get("stop_loss"), 0.0)),
        ("stop_loss", _safe_float(trade.get("stop_loss"), 0.0)),
        ("target", _safe_float(trade.get("target"), 0.0)),
        ("exit", close_price),
        ("exit_price", close_price),
        ("result_r", result_r),
        ("outcome", outcome),
        ("is_shadow", False),
        ("created_at", now_utc_dt()),
    ]

    for c, v in candidates:
        if _has_column(conn, "experience_trades", c):
            cols.append(c)
            vals.append(v)

    if not cols:
        return

    placeholders = ", ".join(["%s"] * len(cols))
    col_sql = ", ".join(cols)

    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO public.experience_trades ({col_sql}) VALUES ({placeholders})",
            tuple(vals),
        )

def _experience_upsert_scoreboard(conn, trade: Dict[str, Any]) -> None:
    if not _table_exists(conn, "public.experience_scoreboard"):
        return
    if not _table_exists(conn, "public.experience_trades"):
        return
    if not _has_column(conn, "experience_trades", "result_r") or not _has_column(conn, "experience_trades", "outcome"):
        return

    setup_type = (trade.get("setup_type") or "TREND_PULLBACK")
    regime = (trade.get("regime") or "UNKNOWN")
    timeframe = (trade.get("timeframe") or trade.get("tf") or "4h")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
              COUNT(*)::int AS n,
              AVG(result_r)::float AS avg_r,
              AVG(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END)::float AS win_rate,
              AVG(CASE WHEN outcome='WIN' THEN result_r END)::float AS avg_win_r,
              AVG(CASE WHEN outcome='LOSS' THEN result_r END)::float AS avg_loss_r
            FROM public.experience_trades
            WHERE setup_type=%s AND regime=%s AND timeframe=%s
            """,
            (setup_type, regime, timeframe),
        )
        stats = cur.fetchone() or {}

    payload = {
        "setup_type": setup_type,
        "regime": regime,
        "timeframe": timeframe,
        "n": _safe_int(stats.get("n"), 0),
        "avg_r": _safe_float(stats.get("avg_r"), 0.0),
        "win_rate": _safe_float(stats.get("win_rate"), 0.0),
        "avg_win_r": _safe_float(stats.get("avg_win_r"), 0.0),
        "avg_loss_r": _safe_float(stats.get("avg_loss_r"), 0.0),
        "updated_at": now_utc_dt(),
        "asof_ts": now_utc_dt(),
        "created_at": now_utc_dt(),
    }

    cols: List[str] = []
    vals: List[Any] = []
    for k, v in payload.items():
        if _has_column(conn, "experience_scoreboard", k):
            cols.append(k)
            vals.append(v)

    if not cols:
        return

    conflict_cols = [c for c in ["setup_type", "regime", "timeframe"] if _has_column(conn, "experience_scoreboard", c)]
    if len(conflict_cols) < 2:
        return

    col_sql = ", ".join(cols)
    placeholders = ", ".join(["%s"] * len(cols))
    set_sql = ", ".join([f"{c}=EXCLUDED.{c}" for c in cols if c not in conflict_cols]) or "updated_at=EXCLUDED.updated_at"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO public.experience_scoreboard ({col_sql})
            VALUES ({placeholders})
            ON CONFLICT ({", ".join(conflict_cols)})
            DO UPDATE SET {set_sql}
            """,
            tuple(vals),
        )

def experience_on_close(trade: Dict[str, Any], *, close_price: float, result_r: float) -> None:
    if not db_ready():
        return
    try:
        outcome = _infer_outcome(result_r)
        with db_connect() as conn:
            try:
                _experience_insert_trade(conn, trade, close_price=close_price, result_r=result_r, outcome=outcome)
                _experience_upsert_scoreboard(conn, trade)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except Exception as e:
        _print(f"⚠️ Experience update skipped: {type(e).__name__}: {e}")

# =========================
# WhatsApp (Twilio) - alleen voor meldingen
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
# State helpers
# =========================
def ensure_dir(path: str) -> None:
    if path and not os.path.exists(path):
        os.makedirs(path, exist_ok=True)

def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        ensure_dir(parent)

def load_state() -> Dict[str, Any]:
    ensure_parent_dir(STATE_PATH)
    if not os.path.isfile(STATE_PATH):
        return {"positions": {}, "open_trades": []}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        return {"positions": {}, "open_trades": []}

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

# =========================
# Market data
# =========================
def get_price(symbol: str) -> float:
    r = requests.get(BINANCE_TICKER_URL, params={"symbol": symbol}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return float(r.json()["price"])

def get_klines_lows(symbol: str, interval: str = STRUCT_INTERVAL, limit: int = STRUCT_LIMIT) -> List[float]:
    r = requests.get(BINANCE_KLINES_URL, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return [float(k[3]) for k in data]

def calc_r_multiple(price: float, entry: float, stop_loss: float) -> float:
    risk = (entry - stop_loss)
    if risk <= 0:
        return 0.0
    return (price - entry) / risk

# =========================
# ✅ SELL ROUTER
# =========================
_SELL_PAPER = None
_SELL_LIVE = None

def _get_sell_fn(trade: Dict[str, Any]):
    """
    Kiest SELL functie:
    - TRADER_MODE=paper -> paper_trader.sell
    - TRADER_MODE=live  -> live_trader.sell
    - TRADER_MODE=auto  -> als trade['live']=True -> live, anders paper
    """
    global _SELL_PAPER, _SELL_LIVE

    prefer = TRADER_MODE
    if prefer == "auto":
        prefer = "live" if bool(trade.get("live")) else "paper"

    if prefer == "live":
        if _SELL_LIVE is None:
            try:
                from trading.live_trader import sell as _s  # type: ignore
                _SELL_LIVE = _s
            except Exception as e:
                _print(f"⚠️ live_trader.sell niet beschikbaar -> fallback paper ({type(e).__name__}: {e})")
                prefer = "paper"

    if prefer == "paper":
        if _SELL_PAPER is None:
            from trading.paper_trader import sell as _s  # type: ignore
            _SELL_PAPER = _s

    return _SELL_LIVE if prefer == "live" else _SELL_PAPER

def _send_sell_close_message(symbol: str, reason: str, entry: float, stop_loss: float, target: float, r_now: float, sell_result: Dict[str, Any]):
    if not isinstance(sell_result, dict) or not sell_result.get("ok"):
        send_whatsapp(
            f"⚠️ SELL MISLUKT ({symbol})\nReden: {reason}\nDetails: {sell_result}"
        )
        return

    exit_price = float(sell_result.get("price", 0.0))
    pnl = float(sell_result.get("pnl", 0.0))

    msg = (
        f"✅ SELL uitgevoerd ({symbol})\n"
        f"Reden: {reason}\n"
        f"Entry: {entry:.6f}\n"
        f"Exit: {exit_price:.6f}\n"
        f"Stop: {stop_loss:.6f}\n"
        f"Target: {target:.6f}\n"
        f"R (bij exit): {r_now:.2f}\n"
        f"PnL: {pnl:.2f}"
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
    if symbol not in positions:
        _print(f"FORCE_TEST_EXIT: geen positie gevonden voor {symbol}.")
        return False

    try:
        price = get_price(symbol)
    except Exception as e:
        _print(f"FORCE_TEST_EXIT: price fetch error {symbol}: {e}")
        price = entry if entry > 0 else 0.0

    r_now = calc_r_multiple(price, entry, stop_loss) if entry > 0 and stop_loss > 0 else 0.0

    _print(f"🚨 FORCE_TEST_EXIT: SELL 100% for {symbol} (test)")
    sell_fn = _get_sell_fn(trade)
    sell_res = sell_fn(symbol, 1.0)

    trade["status"] = "CLOSED"
    trade["closed_reason"] = "FORCE_TEST_EXIT"
    trade["closed_at"] = now_ts()

    _send_sell_close_message(symbol, "FORCE TEST EXIT (debug)", entry, stop_loss, target, r_now, sell_res)

    if isinstance(sell_res, dict) and sell_res.get("ok"):
        exit_price = float(sell_res.get("price", price))
        r_exit = calc_r_multiple(exit_price, entry, stop_loss) if entry > 0 and stop_loss > 0 else 0.0
        experience_on_close(trade, close_price=exit_price, result_r=r_exit)

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
    symbol = trade.get("symbol")
    entry = float(trade.get("entry", 0.0))
    stop_loss = float(trade.get("stop_loss", 0.0))
    target = float(trade.get("target", 0.0))

    if not symbol or entry <= 0 or stop_loss <= 0:
        _print(f"⚠️ Trade overslaan (ongeldige data): {trade}")
        return False

    price = float(test_price) if test_price is not None else get_price(symbol)
    r = calc_r_multiple(price, entry, stop_loss)

    trade.setdefault("mode", "NORMAL")
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

    sell_fn = _get_sell_fn(trade)

    # 1) Stop-loss vóór 1R => SELL 100%
    if price <= stop_loss and r < 1.0:
        _print(f"🛑 {symbol} -> STOP-LOSS geraakt vóór 1R => SELL 100%")
        sell_res = sell_fn(symbol, 1.0)

        trade["status"] = "CLOSED"
        trade["closed_reason"] = "STOP_LOSS_BEFORE_1R"
        trade["closed_at"] = now_ts()

        _send_sell_close_message(symbol, "STOP-LOSS vóór 1R", entry, stop_loss, target, r, sell_res)

        if isinstance(sell_res, dict) and sell_res.get("ok"):
            exit_price = float(sell_res.get("price", price))
            r_exit = calc_r_multiple(exit_price, entry, stop_loss)
            experience_on_close(trade, close_price=exit_price, result_r=r_exit)

        return True

    # 2) Target reached -> STRUCTUUR mode
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

    # 3) STRUCTUUR mode: higher lows volgen, eerste lower low => close 100%
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
                        sell_res = sell_fn(symbol, 1.0)

                        trade["status"] = "CLOSED"
                        trade["closed_reason"] = "STRUCTURE_LOWER_LOW"
                        trade["closed_at"] = now_ts()

                        _send_sell_close_message(symbol, "STRUCTUUR: eerste lower-low", entry, stop_loss, target, r, sell_res)

                        if isinstance(sell_res, dict) and sell_res.get("ok"):
                            exit_price = float(sell_res.get("price", price))
                            r_exit = calc_r_multiple(exit_price, entry, stop_loss)
                            experience_on_close(trade, close_price=exit_price, result_r=r_exit)

                        return True
        except Exception as e:
            _print(f"⚠️ STRUCTUUR fetch error {symbol}: {e}")

        trade["last_check"] = now_ts()
        return False

    # 4) Als R >= 1 => wachten
    if r >= 1.0:
        trade["below_1r_count"] = 0
        trade["last_check"] = now_ts()
        return False

    # 5) Was >1R en nu <1R => SELL 40%
    if trade.get("had_over_1r") and r < 1.0 and not trade.get("partial_sold_40"):
        _print(f"⚠️ {symbol} was >1R, nu <1R -> SELL 40%")
        sell_fn(symbol, 0.40)
        trade["partial_sold_40"] = True
        trade["below_1r_count"] = 1
        trade["last_check"] = now_ts()
        return False

    # 6) Na partial: 3 checks onder 1R => close rest
    if trade.get("partial_sold_40") and r < 1.0:
        trade["below_1r_count"] = int(trade.get("below_1r_count", 0)) + 1
        _print(f"⏳ {symbol} onder 1R count={trade['below_1r_count']}/3")
        if trade["below_1r_count"] >= 3:
            _print(f"🔚 {symbol} 3x onder 1R gebleven -> SELL 100% (rest sluiten)")
            sell_res = sell_fn(symbol, 1.0)

            trade["status"] = "CLOSED"
            trade["closed_reason"] = "UNDER_1R_3X_CLOSE_REST"
            trade["closed_at"] = now_ts()

            _send_sell_close_message(symbol, "Na >1R: 3x onder 1R (rest gesloten)", entry, stop_loss, target, r, sell_res)

            if isinstance(sell_res, dict) and sell_res.get("ok"):
                exit_price = float(sell_res.get("price", price))
                r_exit = calc_r_multiple(exit_price, entry, stop_loss)
                experience_on_close(trade, close_price=exit_price, result_r=r_exit)

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
        if symbol not in positions:
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
    _print(f"DATA_DIR: {DATA_DIR}")
    _print(f"LOGS_DIR: {LOGS_DIR}")
    _print(f"State path: {STATE_PATH}")
    _print(f"FORCE_TEST_EXIT: {'ON' if FORCE_TEST_EXIT else 'OFF'}")
    _print(f"TRADER_MODE: {TRADER_MODE}")
    _print(f"DB experience: {'ON' if db_ready() else 'OFF (DATABASE_URL ontbreekt)'}")

    ensure_dir(LOGS_DIR)

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
