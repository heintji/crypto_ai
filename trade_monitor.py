from __future__ import annotations

import os
import sys
import json
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple, Set

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
# ✅ Shadow trades import (legacy fallback)
# =========================
try:
    from trading.shadow_trades import (
        evaluate_open_shadows_for_symbol,
        get_open_shadows,
        load_shadows,
    )
except Exception:
    evaluate_open_shadows_for_symbol = None
    get_open_shadows = None
    load_shadows = None

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
SHADOW_MONITOR_STATE_PATH = (
    os.getenv("SHADOW_MONITOR_STATE_PATH") or os.path.join(DATA_DIR, "shadow_monitor_state.json")
).strip()

# =========================
# ✅ TRADER MODE
# =========================
TRADER_MODE = (os.getenv("TRADER_MODE") or "auto").strip().lower()

# =========================
# ✅ STATE PATHS (paper vs live)
# =========================
PAPER_STATE_PATH = (os.getenv("PAPER_STATE_PATH") or os.path.join(DATA_DIR, "paper_state.json")).strip()
LIVE_STATE_PATH = (os.getenv("LIVE_STATE_PATH") or os.path.join(DATA_DIR, "live_state.json")).strip()


def _choose_state_path() -> str:
    if TRADER_MODE == "paper":
        return PAPER_STATE_PATH
    if TRADER_MODE == "live":
        return LIVE_STATE_PATH
    return LIVE_STATE_PATH


STATE_PATH = _choose_state_path()
FORCE_EXIT_LOCK_PATH = (os.getenv("FORCE_TEST_EXIT_LOCK_PATH") or os.path.join(DATA_DIR, "force_test_exit.lock")).strip()

# =========================
# Binance endpoints (prijs/structuur)
# =========================
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

# =========================
# Bitvavo endpoints (prijs/structuur)
# =========================
BITVAVO_BASE_URL = "https://api.bitvavo.com"
BITVAVO_TICKER_URL = BITVAVO_BASE_URL + "/v2/ticker/price"
BITVAVO_CANDLES_URL = BITVAVO_BASE_URL + "/v2/candles"

HTTP_TIMEOUT = 15
STRUCT_INTERVAL = "1h"
STRUCT_LIMIT = 50

DEFAULT_SLEEP_SECONDS = int(os.getenv("MONITOR_SLEEP_SECONDS", str(30 * 60)))
FORCE_TEST_EXIT = str(os.getenv("FORCE_TEST_EXIT", "0")).strip().lower() in {"1", "true", "yes", "on"}

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


def _safe_str(x: Any, default: str = "") -> str:
    if x is None:
        return default
    try:
        s = str(x).strip()
        return s if s else default
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


def _holding_minutes(trade: Dict[str, Any]) -> int:
    started = _safe_int(trade.get("monitor_started_at") or trade.get("opened_at") or trade.get("created_at_ts"), 0)
    if started <= 0:
        return 0
    return max(0, int((now_ts() - started) / 60))


def _update_prebuy_experience_outcome(
    conn,
    trade: Dict[str, Any],
    *,
    close_price: float,
    result_r: float,
    outcome: str,
) -> None:
    if not _table_exists(conn, "public.prebuy_experience"):
        return

    prebuy_id = _safe_str(trade.get("prebuy_id"))
    if not prebuy_id:
        return

    updates: List[str] = []
    vals: List[Any] = []

    possible = {
        "outcome": outcome,
        "close_reason": _safe_str(trade.get("closed_reason")),
        "exit_price": close_price,
        "result_r": result_r,
        "mfe_r": _safe_float(trade.get("mfe_r"), 0.0),
        "mae_r": _safe_float(trade.get("mae_r"), 0.0),
        "max_price_seen": _safe_float(trade.get("max_price_seen"), 0.0),
        "min_price_seen": _safe_float(trade.get("min_price_seen"), 0.0),
        "time_to_outcome_minutes": _holding_minutes(trade),
        "updated_at": now_utc_dt(),
    }

    for col, val in possible.items():
        if _has_column(conn, "prebuy_experience", col):
            updates.append(f"{col}=%s")
            vals.append(val)

    if not updates:
        return

    vals.append(prebuy_id)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE public.prebuy_experience
            SET {", ".join(updates)}
            WHERE prebuy_id=%s
            """,
            tuple(vals),
        )


def _experience_insert_trade(conn, trade: Dict[str, Any], *, close_price: float, result_r: float, outcome: str) -> None:
    if not _table_exists(conn, "public.experience_trades"):
        return

    cols: List[str] = []
    vals: List[Any] = []

    candidates = [
        ("prebuy_id", trade.get("prebuy_id")),
        ("symbol", trade.get("symbol")),
        ("setup_type", trade.get("setup_type") or "TREND_PULLBACK"),
        ("timeframe", trade.get("timeframe") or trade.get("tf") or "4h"),
        ("regime", trade.get("regime") or "UNKNOWN"),
        ("label", _infer_label(trade)),
        ("bot_decision", _infer_label(trade)),
        ("user_decision", trade.get("user_decision") or "YES"),
        ("score", _safe_int(trade.get("score"), 0)),
        ("raw_score", trade.get("raw_score")),
        ("chance", _safe_int(trade.get("chance"), 0)),
        ("confidence", _safe_int(trade.get("confidence"), trade.get("chance") or 0)),
        ("why_tag", trade.get("why_tag")),
        ("market_condition", trade.get("market_condition")),
        ("overextended_pct", _safe_float(trade.get("overextended_pct"), 0.0)),
        ("entry", _safe_float(trade.get("entry"), 0.0)),
        ("stop", _safe_float(trade.get("stop") or trade.get("stop_loss"), 0.0)),
        ("stop_loss", _safe_float(trade.get("stop_loss"), 0.0)),
        ("target", _safe_float(trade.get("target"), 0.0)),
        ("exit", close_price),
        ("exit_price", close_price),
        ("result_r", result_r),
        ("outcome", outcome),
        ("close_reason", trade.get("closed_reason")),
        ("mfe_r", _safe_float(trade.get("mfe_r"), 0.0)),
        ("mae_r", _safe_float(trade.get("mae_r"), 0.0)),
        ("max_price_seen", _safe_float(trade.get("max_price_seen"), 0.0)),
        ("min_price_seen", _safe_float(trade.get("min_price_seen"), 0.0)),
        ("holding_minutes", _holding_minutes(trade)),
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

    setup_type = trade.get("setup_type") or "TREND_PULLBACK"
    regime = trade.get("regime") or "UNKNOWN"
    timeframe = trade.get("timeframe") or trade.get("tf") or "4h"

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
                _update_prebuy_experience_outcome(conn, trade, close_price=close_price, result_r=result_r, outcome=outcome)
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


def load_json_file(path: str, default: Any) -> Any:
    ensure_parent_dir(path)
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_file(path: str, payload: Any) -> None:
    ensure_parent_dir(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_state() -> Dict[str, Any]:
    s = load_json_file(STATE_PATH, {"positions": {}, "open_trades": []})
    if not isinstance(s, dict):
        s = {"positions": {}, "open_trades": []}
    s.setdefault("positions", {})
    s.setdefault("open_trades", [])
    return s


def save_state(state: Dict[str, Any]) -> None:
    save_json_file(STATE_PATH, state)


def load_shadow_monitor_state() -> Dict[str, Any]:
    s = load_json_file(SHADOW_MONITOR_STATE_PATH, {"trades": {}})
    if not isinstance(s, dict):
        s = {"trades": {}}
    if not isinstance(s.get("trades"), dict):
        s["trades"] = {}
    return s


def save_shadow_monitor_state(state: Dict[str, Any]) -> None:
    save_json_file(SHADOW_MONITOR_STATE_PATH, state)


def now_ts() -> int:
    return int(time.time())


# =========================
# ✅ Symbol mapping helpers
# =========================
def is_bitvavo_market(symbol: str) -> bool:
    s = (symbol or "").strip()
    return "-" in s and s.upper().endswith(("-EUR", "-USDT"))


def binance_to_bitvavo_market(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if not s:
        return ""
    if "-" in s:
        return s
    base = s.replace("USDT", "").replace("EUR", "")
    return f"{base}-EUR"


def bitvavo_to_binance_symbol(market: str) -> str:
    m = (market or "").strip().upper()
    if not m:
        return ""
    if "-" not in m:
        return m
    base = m.split("-")[0]
    return f"{base}USDT"


def is_live_trade(trade: Dict[str, Any]) -> bool:
    if TRADER_MODE == "live":
        return True
    if TRADER_MODE == "paper":
        return False
    return bool(trade.get("live", False))


# =========================
# Market data (live=Bitvavo, paper=Binance)
# =========================
def get_price_binance(symbol: str) -> float:
    r = requests.get(BINANCE_TICKER_URL, params={"symbol": symbol}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return float(r.json()["price"])


def get_price_bitvavo(market: str) -> float:
    r = requests.get(BITVAVO_TICKER_URL, params={"market": market}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    if isinstance(data, dict) and "price" in data:
        return float(data["price"])

    if isinstance(data, list) and data:
        return float(data[0].get("price"))

    raise RuntimeError(f"Unexpected bitvavo ticker response: {data}")


def get_price(symbol: str, trade: Optional[Dict[str, Any]] = None) -> Tuple[float, str]:
    if trade and is_live_trade(trade):
        market = binance_to_bitvavo_market(symbol)
        price = get_price_bitvavo(market)
        return price, f"bitvavo({market})"

    sym = symbol if symbol.endswith("USDT") else bitvavo_to_binance_symbol(symbol)
    price = get_price_binance(sym)
    return price, f"binance({sym})"


def get_klines_lows_binance(symbol: str, interval: str = STRUCT_INTERVAL, limit: int = STRUCT_LIMIT) -> List[float]:
    r = requests.get(
        BINANCE_KLINES_URL,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return [float(k[3]) for k in data]


def get_klines_lows_bitvavo(market: str, interval: str = STRUCT_INTERVAL, limit: int = STRUCT_LIMIT) -> List[float]:
    url = BITVAVO_CANDLES_URL + f"/{market}"
    r = requests.get(url, params={"interval": interval, "limit": str(limit)}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    lows: List[float] = []
    if isinstance(data, list):
        for row in data:
            try:
                lows.append(float(row[3]))
            except Exception:
                pass
    return lows


def get_klines_lows(symbol: str, trade: Dict[str, Any], interval: str = STRUCT_INTERVAL, limit: int = STRUCT_LIMIT) -> Tuple[List[float], str]:
    if is_live_trade(trade):
        market = binance_to_bitvavo_market(symbol)
        try:
            lows = get_klines_lows_bitvavo(market, interval=interval, limit=limit)
            if lows:
                return lows, f"bitvavo_candles({market},{interval})"
        except Exception as e:
            _print(f"⚠️ Bitvavo candles fail {market}: {type(e).__name__}: {e} -> fallback Binance")

    sym = symbol if symbol.endswith("USDT") else bitvavo_to_binance_symbol(symbol)
    lows = get_klines_lows_binance(sym, interval=interval, limit=limit)
    return lows, f"binance_klines({sym},{interval})"


def calc_r_multiple(price: float, entry: float, stop_loss: float) -> float:
    risk = entry - stop_loss
    if risk <= 0:
        return 0.0
    return (price - entry) / risk


# =========================
# ✅ SHADOW STATE + DB HELPERS
# =========================
def _shadow_trade_id(trade: Dict[str, Any]) -> str:
    for key in ("trade_key", "prebuy_id", "id"):
        val = _safe_str(trade.get(key))
        if val:
            return val
    coin = _safe_str(trade.get("coin") or trade.get("symbol"), "UNKNOWN")
    entry = _safe_str(trade.get("entry"), "0")
    entry_time = _safe_str(trade.get("entry_time") or trade.get("created_at") or trade.get("created_at_ts"), "0")
    return f"{coin}|{entry}|{entry_time}"


def _normalize_shadow_trade(raw: Dict[str, Any], shadow_state_map: Dict[str, Any]) -> Dict[str, Any]:
    trade = dict(raw or {})
    trade_id = _shadow_trade_id(trade)
    stored = shadow_state_map.get(trade_id, {}) if isinstance(shadow_state_map, dict) else {}

    symbol = _safe_str(trade.get("symbol") or trade.get("coin"))
    if symbol and "-" in symbol:
        symbol = bitvavo_to_binance_symbol(symbol)

    trade["shadow_id"] = trade_id
    trade["symbol"] = symbol
    trade["coin"] = symbol
    trade["source"] = "SHADOW"
    trade["live"] = (TRADER_MODE == "live")

    trade["entry"] = _safe_float(trade.get("entry"), 0.0)
    stop_val = trade.get("stop") if trade.get("stop") is not None else trade.get("stop_loss")
    trade["stop_loss"] = _safe_float(stop_val, 0.0)
    trade["stop"] = trade["stop_loss"]
    trade["target"] = _safe_float(trade.get("target"), 0.0)
    trade["status"] = _safe_str(trade.get("status"), "OPEN").upper() or "OPEN"
    trade["outcome"] = _safe_str(trade.get("outcome"), "UNKNOWN").upper() or "UNKNOWN"
    trade["timeframe"] = _safe_str(trade.get("timeframe") or trade.get("tf"), "4h")

    entry_time_raw = trade.get("entry_time") or trade.get("created_at")
    if entry_time_raw:
        try:
            if isinstance(entry_time_raw, datetime):
                dt = entry_time_raw
            else:
                dt = datetime.fromisoformat(str(entry_time_raw).replace("Z", "+00:00"))
            trade["created_at_ts"] = int(dt.timestamp())
        except Exception:
            pass

    merged_defaults = {
        "mode": stored.get("mode", trade.get("mode", "NORMAL")),
        "max_r": _safe_float(stored.get("max_r"), _safe_float(trade.get("max_r"), 0.0)),
        "had_over_1r": bool(stored.get("had_over_1r", trade.get("had_over_1r", False))),
        "partial_sold_40": bool(stored.get("partial_sold_40", trade.get("partial_sold_40", False))),
        "below_1r_count": _safe_int(stored.get("below_1r_count"), _safe_int(trade.get("below_1r_count"), 0)),
        "target_reached_notified": bool(stored.get("target_reached_notified", trade.get("target_reached_notified", False))),
        "struct_trailing_low": stored.get("struct_trailing_low", trade.get("struct_trailing_low")),
        "monitor_started_at": _safe_int(stored.get("monitor_started_at"), _safe_int(trade.get("monitor_started_at") or trade.get("created_at_ts"), now_ts())),
        "max_price_seen": _safe_float(stored.get("max_price_seen"), _safe_float(trade.get("max_price_seen"), trade["entry"])),
        "min_price_seen": _safe_float(stored.get("min_price_seen"), _safe_float(trade.get("min_price_seen"), trade["entry"])),
        "mfe_r": _safe_float(stored.get("mfe_r"), _safe_float(trade.get("mfe_r"), 0.0)),
        "mae_r": _safe_float(stored.get("mae_r"), _safe_float(trade.get("mae_r"), 0.0)),
        "user_decision": stored.get("user_decision") or trade.get("user_decision") or "YES",
        "bot_decision": stored.get("bot_decision") or trade.get("bot_decision") or _infer_label(trade),
        "realized_r_weighted": _safe_float(stored.get("realized_r_weighted"), 0.0),
        "remaining_fraction": _safe_float(stored.get("remaining_fraction"), 1.0),
        "last_check": _safe_int(stored.get("last_check"), _safe_int(trade.get("last_check"), 0)),
    }
    trade.update(merged_defaults)
    return trade


def _shadow_state_snapshot(trade: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "mode": trade.get("mode", "NORMAL"),
        "max_r": _safe_float(trade.get("max_r"), 0.0),
        "had_over_1r": bool(trade.get("had_over_1r", False)),
        "partial_sold_40": bool(trade.get("partial_sold_40", False)),
        "below_1r_count": _safe_int(trade.get("below_1r_count"), 0),
        "target_reached_notified": bool(trade.get("target_reached_notified", False)),
        "struct_trailing_low": trade.get("struct_trailing_low"),
        "monitor_started_at": _safe_int(trade.get("monitor_started_at"), now_ts()),
        "max_price_seen": _safe_float(trade.get("max_price_seen"), 0.0),
        "min_price_seen": _safe_float(trade.get("min_price_seen"), 0.0),
        "mfe_r": _safe_float(trade.get("mfe_r"), 0.0),
        "mae_r": _safe_float(trade.get("mae_r"), 0.0),
        "user_decision": trade.get("user_decision") or "YES",
        "bot_decision": trade.get("bot_decision") or _infer_label(trade),
        "realized_r_weighted": _safe_float(trade.get("realized_r_weighted"), 0.0),
        "remaining_fraction": _safe_float(trade.get("remaining_fraction"), 1.0),
        "last_check": _safe_int(trade.get("last_check"), now_ts()),
    }


def _shadow_close_outcome(total_result_r: float) -> str:
    return "WIN" if total_result_r > 0 else "LOSS"


def _shadow_closed_result_r(trade: Dict[str, Any], final_r: float) -> float:
    realized = _safe_float(trade.get("realized_r_weighted"), 0.0)
    remaining = _safe_float(trade.get("remaining_fraction"), 1.0)
    if remaining < 0:
        remaining = 0.0
    return realized + (remaining * final_r)


def _get_open_shadow_trades_db() -> List[Dict[str, Any]]:
    if not db_ready():
        return []

    try:
        with db_connect() as conn:
            if not _table_exists(conn, "public.experience_trades"):
                return []
            if not _has_column(conn, "experience_trades", "source") or not _has_column(conn, "experience_trades", "outcome"):
                return []

            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM public.experience_trades
                    WHERE UPPER(COALESCE(source, '')) = 'SHADOW'
                      AND (
                        outcome IS NULL
                        OR TRIM(COALESCE(outcome, '')) = ''
                        OR UPPER(COALESCE(outcome, '')) IN ('UNKNOWN', 'OPEN')
                      )
                    ORDER BY entry_time DESC NULLS LAST, created_at DESC NULLS LAST
                    """
                )
                return [dict(r) for r in (cur.fetchall() or [])]
    except Exception as e:
        _print(f"⚠️ Open shadow trades uit DB ophalen mislukt: {type(e).__name__}: {e}")
        return []


def _update_shadow_trade_db(trade: Dict[str, Any], updates: Dict[str, Any]) -> bool:
    if not db_ready():
        return False

    try:
        with db_connect() as conn:
            if not _table_exists(conn, "public.experience_trades"):
                return False

            where_sql = ""
            where_vals: List[Any] = []
            if _safe_str(trade.get("trade_key")) and _has_column(conn, "experience_trades", "trade_key"):
                where_sql = "trade_key=%s"
                where_vals = [_safe_str(trade.get("trade_key"))]
            elif _safe_str(trade.get("prebuy_id")) and _has_column(conn, "experience_trades", "prebuy_id"):
                where_sql = "prebuy_id=%s"
                where_vals = [_safe_str(trade.get("prebuy_id"))]
            elif trade.get("id") is not None and _has_column(conn, "experience_trades", "id"):
                where_sql = "id=%s"
                where_vals = [trade.get("id")]
            elif _has_column(conn, "experience_trades", "coin") and _has_column(conn, "experience_trades", "entry") and _has_column(conn, "experience_trades", "entry_time"):
                where_sql = "coin=%s AND entry=%s AND entry_time=%s"
                where_vals = [
                    _safe_str(trade.get("coin") or trade.get("symbol")),
                    _safe_float(trade.get("entry"), 0.0),
                    trade.get("entry_time"),
                ]
            else:
                return False

            set_parts: List[str] = []
            set_vals: List[Any] = []
            for col, val in updates.items():
                if _has_column(conn, "experience_trades", col):
                    set_parts.append(f"{col}=%s")
                    set_vals.append(val)

            if not set_parts:
                return False

            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE public.experience_trades SET {', '.join(set_parts)} WHERE {where_sql}",
                    tuple(set_vals + where_vals),
                )
            conn.commit()
            return True
    except Exception as e:
        _print(f"⚠️ Shadow DB update mislukt: {type(e).__name__}: {e}")
        return False


def _update_shadow_scoreboard(trade: Dict[str, Any]) -> None:
    if not db_ready():
        return
    try:
        with db_connect() as conn:
            _experience_upsert_scoreboard(conn, trade)
            conn.commit()
    except Exception as e:
        _print(f"⚠️ Shadow scoreboard update mislukt: {type(e).__name__}: {e}")


# =========================
# ✅ SHADOW EVALUATION (new = same lifecycle as real trades)
# =========================
def _legacy_evaluate_shadow_for_symbol(symbol: str, price: float) -> None:
    if evaluate_open_shadows_for_symbol is None:
        return
    try:
        results = evaluate_open_shadows_for_symbol(symbol, price)
        if results:
            for res in results:
                if isinstance(res, dict) and res.get("ok"):
                    _print(
                        f"👻 Shadow closed | id={res.get('id')} "
                        f"outcome={res.get('outcome')} result_r={res.get('result_r')}"
                    )
    except Exception as e:
        _print(f"⚠️ Legacy shadow evaluation failed for {symbol}: {type(e).__name__}: {e}")


def evaluate_shadow_for_symbol(symbol: str, price: float) -> None:
    """
    Nieuwe shadow-evaluatie:
    - leest open shadow trades uit experience_trades
    - gebruikt dezelfde lifecycle-regels als echte trades
    - sluit virtueel af met WIN/LOSS
    Legacy import blijft alleen fallback.
    """
    handled = False
    shadow_store = load_shadow_monitor_state()
    shadow_map = shadow_store.get("trades", {}) if isinstance(shadow_store, dict) else {}

    raw_trades = [t for t in _get_open_shadow_trades_db() if _safe_str(t.get("coin") or t.get("symbol")) == symbol]
    if raw_trades:
        handled = True
        for raw in raw_trades:
            trade = _normalize_shadow_trade(raw, shadow_map)
            closed = process_shadow_trade_record(trade, price=price, price_src="pre_fetched")
            trade_id = trade["shadow_id"]
            if closed:
                shadow_map.pop(trade_id, None)
            else:
                shadow_map[trade_id] = _shadow_state_snapshot(trade)
        shadow_store["trades"] = shadow_map
        save_shadow_monitor_state(shadow_store)

    if not handled:
        _legacy_evaluate_shadow_for_symbol(symbol, price)


def get_open_shadow_symbols() -> List[str]:
    """
    Eerst echte open SHADOW rows uit DB.
    Daarna legacy fallback.
    """
    symbols: Set[str] = set()

    for sh in _get_open_shadow_trades_db():
        symbol = _safe_str(sh.get("coin") or sh.get("symbol"))
        if symbol:
            if "-" in symbol:
                symbol = bitvavo_to_binance_symbol(symbol)
            symbols.add(symbol)

    if not symbols and get_open_shadows is not None:
        try:
            db_shadows = get_open_shadows()
            for sh in db_shadows:
                if not isinstance(sh, dict):
                    continue
                symbol = _safe_str(sh.get("symbol"))
                status = _safe_str(sh.get("status"), "OPEN").upper()
                if symbol and status == "OPEN":
                    symbols.add(symbol)
        except Exception as e:
            _print(f"⚠️ Open shadow symbols uit legacy DB ophalen mislukt: {type(e).__name__}: {e}")

    if not symbols and load_shadows is not None:
        try:
            file_shadows = load_shadows()
            for sh in file_shadows:
                if not isinstance(sh, dict):
                    continue
                symbol = _safe_str(sh.get("symbol"))
                status = _safe_str(sh.get("status"), "OPEN").upper()
                if symbol and status == "OPEN":
                    symbols.add(symbol)
        except Exception as e:
            _print(f"⚠️ Open shadow symbols uit file ophalen mislukt: {type(e).__name__}: {e}")

    return sorted(symbols)


def evaluate_all_open_shadows() -> None:
    """
    Evalueert automatisch alle open shadow trades.
    """
    symbols = get_open_shadow_symbols()
    if not symbols:
        _print("👻 Geen open shadow symbols gevonden.")
        return

    _print(f"👻 Open shadow symbols gevonden: {len(symbols)}")

    for symbol in symbols:
        try:
            dummy_trade = {"symbol": symbol, "live": (TRADER_MODE == "live")}
            price, src = get_price(symbol, dummy_trade)
            _print(f"👻 Auto shadow check for {symbol} | price={price:.6f} ({src})")
            evaluate_shadow_for_symbol(symbol, price)
        except Exception as e:
            _print(f"⚠️ Auto shadow check failed for {symbol}: {type(e).__name__}: {e}")


# =========================
# ✅ SELL ROUTER
# =========================
_SELL_PAPER = None
_SELL_LIVE = None


def _resolve_sell_callable(module_path: str):
    mod = __import__(module_path, fromlist=["sell", "sell_base", "place_market_sell_base"])

    for name in ("sell", "sell_base", "place_market_sell_base"):
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn, name

    raise AttributeError(f"{module_path}.sell / sell_base / place_market_sell_base ontbreekt")


def _call_sell_compat(sell_fn, sell_name: str, trade: Dict[str, Any], fraction: float):
    symbol = trade.get("symbol")
    position = trade.get("position_size") or trade.get("amount") or trade.get("base_amount")

    if sell_name in ("sell_base", "place_market_sell_base"):
        market = binance_to_bitvavo_market(symbol)
        if not position:
            raise ValueError("Trade mist base amount / position_size voor live sell_base.")
        amount_base = float(position) * float(fraction)
        return sell_fn(market, amount_base)

    return sell_fn(symbol, fraction)


def _get_sell_fn(trade: Dict[str, Any]):
    global _SELL_PAPER, _SELL_LIVE

    prefer = TRADER_MODE
    if prefer == "auto":
        prefer = "live" if bool(trade.get("live")) else "paper"

    if prefer == "live":
        if _SELL_LIVE is None:
            try:
                _SELL_LIVE = _resolve_sell_callable("trading.live_trader")
            except Exception as e:
                _print(f"⚠️ live_trader sell niet beschikbaar -> fallback paper ({type(e).__name__}: {e})")
                prefer = "paper"

    if prefer == "paper":
        if _SELL_PAPER is None:
            _SELL_PAPER = _resolve_sell_callable("trading.paper_trader")

    return _SELL_LIVE if prefer == "live" else _SELL_PAPER


def _execute_sell(trade: Dict[str, Any], fraction: float):
    sell_fn, sell_name = _get_sell_fn(trade)
    return _call_sell_compat(sell_fn, sell_name, trade, fraction)


def _send_sell_close_message(symbol: str, reason: str, entry: float, stop_loss: float, target: float, r_now: float, sell_result: Dict[str, Any]):
    if not isinstance(sell_result, dict) or not sell_result.get("ok"):
        send_whatsapp(f"⚠️ SELL MISLUKT ({symbol})\nReden: {reason}\nDetails: {sell_result}")
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
        price, src = get_price(symbol, trade)
    except Exception as e:
        _print(f"FORCE_TEST_EXIT: price fetch error {symbol}: {type(e).__name__}: {e}")
        price = entry if entry > 0 else 0.0
        src = "fallback(entry)"

    r_now = calc_r_multiple(price, entry, stop_loss) if entry > 0 and stop_loss > 0 else 0.0

    _print(f"🚨 FORCE_TEST_EXIT: SELL 100% for {symbol} (test) | price={price:.6f} src={src}")
    sell_res = _execute_sell(trade, 1.0)

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
# Trade learning helpers
# =========================
def _ensure_learning_fields(trade: Dict[str, Any], entry: float) -> None:
    trade.setdefault("monitor_started_at", now_ts())
    trade.setdefault("max_price_seen", entry)
    trade.setdefault("min_price_seen", entry)
    trade.setdefault("mfe_r", 0.0)
    trade.setdefault("mae_r", 0.0)
    trade.setdefault("user_decision", "YES")
    trade.setdefault("bot_decision", _infer_label(trade))


def _update_learning_snapshot(trade: Dict[str, Any], price: float, entry: float, stop_loss: float) -> None:
    _ensure_learning_fields(trade, entry)

    trade["max_price_seen"] = max(_safe_float(trade.get("max_price_seen"), entry), price)
    trade["min_price_seen"] = min(_safe_float(trade.get("min_price_seen"), entry), price)

    r_now = calc_r_multiple(price, entry, stop_loss)
    trade["mfe_r"] = max(_safe_float(trade.get("mfe_r"), 0.0), r_now)
    trade["mae_r"] = min(_safe_float(trade.get("mae_r"), 0.0), r_now)


# =========================
# SHADOW persistence helpers
# =========================
def _close_shadow_trade(trade: Dict[str, Any], *, exit_price: float, result_r: float, close_reason: str) -> None:
    trade["status"] = "CLOSED"
    trade["outcome"] = _shadow_close_outcome(result_r)
    trade["closed_reason"] = close_reason
    trade["close_reason"] = close_reason
    trade["closed_at"] = now_ts()
    trade["exit_price"] = float(exit_price)
    trade["exit"] = float(exit_price)
    trade["result_r"] = float(result_r)
    trade["remaining_fraction"] = 0.0

    updates: Dict[str, Any] = {
        "outcome": trade["outcome"],
        "status": "CLOSED",
        "close_reason": close_reason,
        "exit_price": float(exit_price),
        "exit": float(exit_price),
        "result_r": float(result_r),
        "mfe_r": _safe_float(trade.get("mfe_r"), 0.0),
        "mae_r": _safe_float(trade.get("mae_r"), 0.0),
        "max_price_seen": _safe_float(trade.get("max_price_seen"), 0.0),
        "min_price_seen": _safe_float(trade.get("min_price_seen"), 0.0),
        "holding_minutes": _holding_minutes(trade),
    }

    now_dt = now_utc_dt()
    if trade.get("entry_time") is not None:
        updates["exit_time"] = now_dt
    if trade.get("created_at") is not None:
        updates["updated_at"] = now_dt

    updated = _update_shadow_trade_db(trade, updates)
    if not updated:
        _print(f"⚠️ Shadow close DB update niet gelukt voor {trade.get('shadow_id')}")

    try:
        if db_ready():
            with db_connect() as conn:
                _update_prebuy_experience_outcome(
                    conn,
                    trade,
                    close_price=float(exit_price),
                    result_r=float(result_r),
                    outcome=trade["outcome"],
                )
                conn.commit()
        _update_shadow_scoreboard(trade)
    except Exception as e:
        _print(f"⚠️ Shadow post-close update skipped: {type(e).__name__}: {e}")

    _print(
        f"👻 Shadow closed | id={trade.get('shadow_id')} symbol={trade.get('symbol')} "
        f"outcome={trade.get('outcome')} result_r={float(result_r):.2f} reason={close_reason}"
    )


def _shadow_partial_sell_40(trade: Dict[str, Any], current_r: float) -> None:
    if trade.get("partial_sold_40"):
        return
    trade["realized_r_weighted"] = _safe_float(trade.get("realized_r_weighted"), 0.0) + (0.40 * current_r)
    trade["remaining_fraction"] = 0.60
    trade["partial_sold_40"] = True
    trade["below_1r_count"] = 1
    _print(
        f"👻 Shadow partial 40% | id={trade.get('shadow_id')} symbol={trade.get('symbol')} "
        f"r={current_r:.2f} realized_r_weighted={trade['realized_r_weighted']:.2f}"
    )


# =========================
# Core rules (jouw set) — real trades ongewijzigd
# =========================
def process_trade(state: Dict[str, Any], trade: Dict[str, Any], test_price: Optional[float] = None) -> bool:
    symbol = trade.get("symbol")
    entry = float(trade.get("entry", 0.0))
    stop_loss = float(trade.get("stop_loss", 0.0))
    target = float(trade.get("target", 0.0))

    if not symbol or entry <= 0 or stop_loss <= 0:
        _print(f"⚠️ Trade overslaan (ongeldige data): {trade}")
        return False

    if test_price is not None:
        price = float(test_price)
        src = "test_price"
    else:
        price, src = get_price(symbol, trade)

    # shadow trades voor dit symbool ook evalueren
    evaluate_shadow_for_symbol(symbol, price)

    r = calc_r_multiple(price, entry, stop_loss)

    trade.setdefault("mode", "NORMAL")
    trade.setdefault("status", "OPEN")
    trade.setdefault("max_r", 0.0)
    trade.setdefault("had_over_1r", False)
    trade.setdefault("partial_sold_40", False)
    trade.setdefault("below_1r_count", 0)
    trade.setdefault("target_reached_notified", False)
    trade.setdefault("last_check", now_ts())

    _update_learning_snapshot(trade, price, entry, stop_loss)

    trade["max_r"] = max(float(trade.get("max_r", 0.0)), r)
    if r >= 1.0:
        trade["had_over_1r"] = True

    _print(f"📊 {symbol} | price={price:.6f} ({src}) | entry={entry:.6f} | SL={stop_loss:.6f} | R={r:.2f} | mode={trade['mode']}")

    if price <= stop_loss and r < 1.0:
        _print(f"🛑 {symbol} -> STOP-LOSS geraakt vóór 1R => SELL 100%")
        sell_res = _execute_sell(trade, 1.0)

        trade["status"] = "CLOSED"
        trade["closed_reason"] = "STOP_LOSS_BEFORE_1R"
        trade["closed_at"] = now_ts()

        _send_sell_close_message(symbol, "STOP-LOSS vóór 1R", entry, stop_loss, target, r, sell_res)

        if isinstance(sell_res, dict) and sell_res.get("ok"):
            exit_price = float(sell_res.get("price", price))
            r_exit = calc_r_multiple(exit_price, entry, stop_loss)
            experience_on_close(trade, close_price=exit_price, result_r=r_exit)

        return True

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

    if trade.get("mode") == "STRUCTUUR":
        try:
            lows, lows_src = get_klines_lows(symbol, trade)
            if len(lows) >= 3:
                last_closed_low = lows[-2]
                trailing = trade.get("struct_trailing_low")
                if trailing is None:
                    trade["struct_trailing_low"] = last_closed_low
                    _print(f"🧱 {symbol} STRUCTUUR init trailing_low={last_closed_low:.6f} ({lows_src})")
                else:
                    trailing = float(trailing)
                    if last_closed_low > trailing:
                        trade["struct_trailing_low"] = last_closed_low
                        _print(f"🧱 {symbol} STRUCTUUR higher-low -> trailing_low={last_closed_low:.6f} ({lows_src})")
                    elif last_closed_low < trailing:
                        _print(f"🔻 {symbol} STRUCTUUR lower-low DETECTED -> SELL 100% ({lows_src})")
                        sell_res = _execute_sell(trade, 1.0)

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
            _print(f"⚠️ STRUCTUUR fetch error {symbol}: {type(e).__name__}: {e}")

        trade["last_check"] = now_ts()
        return False

    if r >= 1.0:
        trade["below_1r_count"] = 0
        trade["last_check"] = now_ts()
        return False

    if trade.get("had_over_1r") and r < 1.0 and not trade.get("partial_sold_40"):
        _print(f"⚠️ {symbol} was >1R, nu <1R -> SELL 40%")
        _execute_sell(trade, 0.40)
        trade["partial_sold_40"] = True
        trade["below_1r_count"] = 1
        trade["last_check"] = now_ts()
        return False

    if trade.get("partial_sold_40") and r < 1.0:
        trade["below_1r_count"] = int(trade.get("below_1r_count", 0)) + 1
        _print(f"⏳ {symbol} onder 1R count={trade['below_1r_count']}/3")
        if trade["below_1r_count"] >= 3:
            _print(f"🔚 {symbol} 3x onder 1R gebleven -> SELL 100% (rest sluiten)")
            sell_res = _execute_sell(trade, 1.0)

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
# SHADOW rules — same lifecycle as real trades, virtual only
# =========================
def process_shadow_trade_record(trade: Dict[str, Any], *, price: Optional[float] = None, price_src: str = "") -> bool:
    symbol = trade.get("symbol")
    entry = float(trade.get("entry", 0.0))
    stop_loss = float(trade.get("stop_loss", 0.0))
    target = float(trade.get("target", 0.0))

    if not symbol or entry <= 0 or stop_loss <= 0:
        _print(f"⚠️ Shadow overslaan (ongeldige data): {trade}")
        return False

    if price is None:
        price, fetched_src = get_price(symbol, trade)
        price_src = fetched_src
    elif not price_src:
        price_src = "pre_fetched"

    r = calc_r_multiple(price, entry, stop_loss)

    trade.setdefault("mode", "NORMAL")
    trade.setdefault("status", "OPEN")
    trade.setdefault("max_r", 0.0)
    trade.setdefault("had_over_1r", False)
    trade.setdefault("partial_sold_40", False)
    trade.setdefault("below_1r_count", 0)
    trade.setdefault("target_reached_notified", False)
    trade.setdefault("last_check", now_ts())
    trade.setdefault("realized_r_weighted", 0.0)
    trade.setdefault("remaining_fraction", 1.0)

    _update_learning_snapshot(trade, price, entry, stop_loss)

    trade["max_r"] = max(float(trade.get("max_r", 0.0)), r)
    if r >= 1.0:
        trade["had_over_1r"] = True

    _print(
        f"👻 {symbol} | price={price:.6f} ({price_src}) | entry={entry:.6f} | "
        f"SL={stop_loss:.6f} | R={r:.2f} | mode={trade['mode']}"
    )

    # 1) Stop vóór 1R = direct sluiten als LOSS
    if price <= stop_loss and r < 1.0:
        total_result_r = _shadow_closed_result_r(trade, r)
        _close_shadow_trade(trade, exit_price=price, result_r=total_result_r, close_reason="STOP_LOSS_BEFORE_1R")
        return True

    # 2) Target geraakt = switch naar STRUCTUUR-MODE (zelfde als echte trade)
    if target > 0 and price >= target and not trade.get("target_reached_notified", False):
        trade["target_reached_notified"] = True
        trade["mode"] = "STRUCTUUR"
        trade.setdefault("struct_trailing_low", None)
        _print(f"👻 {symbol} TARGET REACHED! -> switch naar STRUCTUUR-MODE")

    # 3) STRUCTUUR-MODE = virtueel exact dezelfde close-logica
    if trade.get("mode") == "STRUCTUUR":
        try:
            lows, lows_src = get_klines_lows(symbol, trade)
            if len(lows) >= 3:
                last_closed_low = lows[-2]
                trailing = trade.get("struct_trailing_low")
                if trailing is None:
                    trade["struct_trailing_low"] = last_closed_low
                    _print(f"👻 {symbol} STRUCTUUR init trailing_low={last_closed_low:.6f} ({lows_src})")
                else:
                    trailing = float(trailing)
                    if last_closed_low > trailing:
                        trade["struct_trailing_low"] = last_closed_low
                        _print(f"👻 {symbol} STRUCTUUR higher-low -> trailing_low={last_closed_low:.6f} ({lows_src})")
                    elif last_closed_low < trailing:
                        total_result_r = _shadow_closed_result_r(trade, r)
                        _close_shadow_trade(trade, exit_price=price, result_r=total_result_r, close_reason="STRUCTURE_LOWER_LOW")
                        return True
        except Exception as e:
            _print(f"⚠️ Shadow STRUCTUUR fetch error {symbol}: {type(e).__name__}: {e}")

        trade["last_check"] = now_ts()
        return False

    # 4) Boven 1R geweest = wachten
    if r >= 1.0:
        trade["below_1r_count"] = 0
        trade["last_check"] = now_ts()
        return False

    # 5) Na >1R terug <1R = virtuele 40% exit
    if trade.get("had_over_1r") and r < 1.0 and not trade.get("partial_sold_40"):
        _shadow_partial_sell_40(trade, r)
        trade["last_check"] = now_ts()
        return False

    # 6) Na partial 40%: 3 candles onder 1R = rest sluiten
    if trade.get("partial_sold_40") and r < 1.0:
        trade["below_1r_count"] = int(trade.get("below_1r_count", 0)) + 1
        _print(f"👻 {symbol} onder 1R count={trade['below_1r_count']}/3")
        if trade["below_1r_count"] >= 3:
            total_result_r = _shadow_closed_result_r(trade, r)
            _close_shadow_trade(trade, exit_price=price, result_r=total_result_r, close_reason="UNDER_1R_3X_CLOSE_REST")
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
    closed_symbols: List[str] = []

    # 1) echte trades monitoren
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

    # 2) shadow-only handmatige check
    if only_symbol and not open_trades:
        try:
            if test_price is not None:
                price = float(test_price)
                src = "test_price"
            else:
                dummy_trade = {"symbol": only_symbol, "live": (TRADER_MODE == "live")}
                price, src = get_price(only_symbol, dummy_trade)

            _print(f"👻 Shadow-only check for {only_symbol} | price={price:.6f} ({src})")
            evaluate_shadow_for_symbol(only_symbol, price)
        except Exception as e:
            _print(f"⚠️ Shadow-only symbol check failed for {only_symbol}: {type(e).__name__}: {e}")

    # 3) automatische shadow scan als er géén handmatig symbol is opgegeven
    if not only_symbol:
        evaluate_all_open_shadows()

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
    _print(f"TRADER_MODE: {TRADER_MODE}")
    _print(f"State path: {STATE_PATH}")
    _print(f"Shadow state path: {SHADOW_MONITOR_STATE_PATH}")
    _print(f"FORCE_TEST_EXIT: {'ON' if FORCE_TEST_EXIT else 'OFF'}")
    _print(f"DB experience: {'ON' if db_ready() else 'OFF (DATABASE_URL ontbreekt)'}")
    _print(f"Shadow monitoring: {'ON' if (db_ready() or evaluate_open_shadows_for_symbol is not None) else 'OFF'}")

    ensure_dir(LOGS_DIR)

    if args.once:
        run_once(test_price=args.test_price, only_symbol=args.symbol)
        return

    while True:
        try:
            run_once(test_price=args.test_price, only_symbol=args.symbol)
        except Exception as e:
            _print(f"⚠️ Monitor error: {type(e).__name__}: {e}")
        time.sleep(int(args.sleep))


if __name__ == "__main__":
    main()
