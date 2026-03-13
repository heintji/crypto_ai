from __future__ import annotations

import os
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

# ==========================================================
# FIX PYTHON PATH (Render issue)
# ==========================================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import psycopg2
import psycopg2.extras

from data.fix_experience_schema import sync_schema


# ==========================================================
# ENV
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing for history_simulator.py")

# Candles
TF_ENTRY = (os.getenv("SIM_ENTRY_INTERVAL") or "1h").strip().lower()
TF_REGIME = (os.getenv("SIM_REGIME_INTERVAL") or "4h").strip().lower()

# Review van echte trades
REAL_TRADES_TABLES = [
    x.strip()
    for x in (os.getenv("REAL_TRADES_TABLES") or "paper_trades,live_trades").split(",")
    if x.strip()
]

# Veiligheid
DB_STATEMENT_TIMEOUT_MS = int(os.getenv("SIM_DB_TIMEOUT_MS") or "120000")
PRINT_EVERY = int(os.getenv("SIM_PRINT_EVERY") or "250")

# Reset alléén review-data, niet alles blind slopen
SIM_RESET = (os.getenv("SIM_RESET") or "0").strip() == "1"

# Fallbacks
DEFAULT_STOP_PCT = float(os.getenv("SIM_STOP_PCT") or "0.02")
DEFAULT_RR_TARGET = float(os.getenv("SIM_RR") or "2.0")
MAX_HOLD_CANDLES = int(os.getenv("SIM_MAX_HOLD") or "72")

GO_MIN_SCORE = int(os.getenv("GO_MIN_SCORE") or "80")
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "60")


# ==========================================================
# DATA STRUCTURES
# ==========================================================
@dataclass
class Candle:
    ts: datetime
    ts_ms: int
    open: float
    high: float
    low: float
    close: float


@dataclass
class RealTrade:
    source_table: str
    source_trade_id: str
    coin: str
    raw_symbol: str
    entry_time: datetime
    exit_time: Optional[datetime]
    entry: float
    stop: Optional[float]
    target: Optional[float]
    exit_price: Optional[float]
    pnl_eur: Optional[float]
    pnl_pct: Optional[float]
    setup_type: str
    market_regime: str
    grade: str
    bot_confidence: int
    why: str


# ==========================================================
# DB HELPERS
# ==========================================================
def pg_connect():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def set_db_safety(conn):
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = {DB_STATEMENT_TIMEOUT_MS};")
        cur.execute("SET lock_timeout = 10000;")


def table_exists(conn, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = %s
            );
            """,
            (table_name,),
        )
        row = cur.fetchone()
        return bool(row[0]) if row else False


def get_table_columns(conn, table_name: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s;
            """,
            (table_name,),
        )
        return {r[0] for r in cur.fetchall()}


def choose_expr(columns: set[str], candidates: List[str], fallback_sql: str = "NULL") -> str:
    for c in candidates:
        if c in columns:
            return c
    return fallback_sql


def ensure_tables(conn):
    """
    experience_trades schema wordt door sync_schema() geregeld.
    Hier alleen scoreboard + indexes.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.experience_scoreboard (
                score_key TEXT PRIMARY KEY,
                setup_type TEXT NOT NULL,
                market_regime TEXT NOT NULL,
                grade TEXT NOT NULL,
                n INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                timeouts INTEGER NOT NULL DEFAULT 0,
                avg_mfe DOUBLE PRECISION NOT NULL DEFAULT 0,
                avg_mae DOUBLE PRECISION NOT NULL DEFAULT 0,
                avg_time_minutes DOUBLE PRECISION NOT NULL DEFAULT 0,
                win_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_experience_trades_coin
            ON public.experience_trades(coin);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_experience_trades_timestamp
            ON public.experience_trades("timestamp" DESC);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_experience_trades_setup_regime
            ON public.experience_trades(setup_type, market_regime, grade);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_experience_trades_outcome
            ON public.experience_trades(outcome);
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_candles_symbol_tf_time
            ON public.candles(symbol, timeframe, open_time);
            """
        )

    conn.commit()


def reset_review_rows(conn):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM public.experience_trades WHERE source = 'REAL_REVIEW';")
        cur.execute("DELETE FROM public.experience_scoreboard WHERE score_key LIKE 'REAL_REVIEW|%';")
    conn.commit()


# ==========================================================
# HELPERS
# ==========================================================
def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def _safe_round(v: Any, ndigits: int = 4) -> float:
    try:
        return round(float(v), ndigits)
    except Exception:
        return 0.0


def _normalize_symbol_to_coin(raw_symbol: str) -> str:
    """
    Doel: matchen met public.candles.symbol.
    Verwacht meestal Binance-achtige notatie zoals BTCUSDT.
    Live trades kunnen bijvoorbeeld BTC-EUR zijn.
    In dat geval mappen we naar BTCUSDT als fallback candle-symbool.
    """
    s = (raw_symbol or "").upper().strip()
    s = s.replace("/", "-").replace("_", "-")

    if "-" in s:
        base, quote = s.split("-", 1)
        base = base.strip()
        quote = quote.strip()
        if quote == "USDT":
            return f"{base}USDT"
        if quote == "EUR":
            return f"{base}USDT"
        return f"{base}{quote}"

    if s.endswith("EUR"):
        base = s[:-3]
        return f"{base}USDT"

    return s


def _grade_from_score(score: int) -> str:
    if score >= GO_MIN_SCORE:
        return "GO"
    if score >= WATCH_MIN_SCORE:
        return "WATCH"
    return "IGNORE"


# ==========================================================
# LOAD CANDLES
# ==========================================================
def load_candles(conn, symbol: str, timeframe: str) -> List[Candle]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT open_time, open, high, low, close
            FROM public.candles
            WHERE symbol = %s
              AND timeframe = %s
            ORDER BY open_time ASC;
            """,
            (symbol, timeframe),
        )
        rows = cur.fetchall()

    out: List[Candle] = []
    for r in rows:
        ts = r["open_time"]
        ts_ms = int(ts.timestamp() * 1000)
        out.append(
            Candle(
                ts=ts,
                ts_ms=ts_ms,
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
            )
        )
    return out


def load_candles_map_for_needed_symbols(conn, symbols: List[str], timeframe: str) -> Dict[str, List[Candle]]:
    unique_symbols = sorted(set([s for s in symbols if s]))
    result: Dict[str, List[Candle]] = {}
    for s in unique_symbols:
        result[s] = load_candles(conn, s, timeframe)
        print(f"📈 loaded candles {s} {timeframe}: {len(result[s])}")
    return result


# ==========================================================
# INDICATORS / CONTEXT
# ==========================================================
def _sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None

    gains = 0.0
    losses = 0.0

    for i in range(-period, 0):
        d = values[i] - values[i - 1]
        if d >= 0:
            gains += d
        else:
            losses += abs(d)

    if losses == 0:
        return 100.0

    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def _slope(values: List[float], lookback: int = 5) -> Optional[float]:
    if len(values) < lookback + 1:
        return None
    return values[-1] - values[-1 - lookback]


def _compute_regime_map(c4h: List[Candle]) -> Dict[int, str]:
    closes: List[float] = []
    sma20_list: List[float] = []
    regime_map: Dict[int, str] = {}

    for c in c4h:
        closes.append(c.close)
        s20 = _sma(closes, 20)
        s50 = _sma(closes, 50)

        if s20 is None or s50 is None:
            regime_map[c.ts_ms] = "UNKNOWN"
            continue

        sma20_list.append(s20)
        sl = _slope(sma20_list, lookback=5)

        if sl is None:
            regime_map[c.ts_ms] = "UNKNOWN"
            continue

        if s20 > s50 and sl > 0:
            regime_map[c.ts_ms] = "BULL"
        elif s20 < s50 and sl < 0:
            regime_map[c.ts_ms] = "BEAR"
        else:
            regime_map[c.ts_ms] = "RANGE"

    return regime_map


def _regime_at_ts(regime_map: Dict[int, str], c4h: List[Candle], ts_ms: int) -> str:
    last = "UNKNOWN"
    for c in c4h:
        if c.ts_ms <= ts_ms:
            last = regime_map.get(c.ts_ms, "UNKNOWN")
        else:
            break
    return last


def _market_condition_from_atr_like(c1h: List[Candle], idx: int, period: int = 14) -> str:
    if idx < period:
        return "unknown"

    ranges = []
    for j in range(idx - period + 1, idx + 1):
        c = c1h[j]
        if c.close <= 0:
            continue
        ranges.append((c.high - c.low) / c.close)

    if not ranges:
        return "unknown"

    avg = sum(ranges) / len(ranges)

    if avg < 0.006:
        return "rustig"
    if avg < 0.015:
        return "normaal"
    return "volatiel"


def _overextended_pct(c1h: List[Candle], idx: int, ma_period: int = 20) -> float:
    if idx < ma_period:
        return 0.0

    closes = [c.close for c in c1h[idx - ma_period + 1: idx + 1]]
    ma = sum(closes) / len(closes)
    if ma <= 0:
        return 0.0

    return (c1h[idx].close - ma) / ma * 100.0


def _score_at_trade_entry(c1h: List[Candle], idx: int) -> Tuple[int, str]:
    closes = [c.close for c in c1h[: idx + 1]]

    s20 = _sma(closes, 20)
    s50 = _sma(closes, 50)
    r14 = _rsi(closes, 14)

    if s20 is None or s50 is None or r14 is None:
        return 50, "Indicator fallback"

    score = 50
    score += 20 if s20 > s50 else -10

    if r14 >= 60:
        score += 20
    elif r14 <= 40:
        score -= 10

    score = max(0, min(100, score))
    why = f"SMA20/50 + RSI ({round(r14, 1)})"
    return score, why


# ==========================================================
# DYNAMIC REAL TRADE LOADER
# ==========================================================
def load_real_trades_from_table(conn, table_name: str) -> List[RealTrade]:
    if not table_exists(conn, table_name):
        print(f"⚠️ tabel bestaat niet, overgeslagen: {table_name}")
        return []

    cols = get_table_columns(conn, table_name)
    if not cols:
        print(f"⚠️ geen kolommen gevonden voor tabel: {table_name}")
        return []

    id_expr = choose_expr(cols, ["id", "trade_id"], "'unknown'")
    symbol_expr = choose_expr(cols, ["symbol", "coin", "market", "pair"], "NULL")
    entry_time_expr = choose_expr(cols, ["entry_time", "opened_at", "buy_time", "created_at", "timestamp"], "NULL")
    exit_time_expr = choose_expr(cols, ["exit_time", "closed_at", "sold_at", "sell_time", "updated_at"], "NULL")
    entry_expr = choose_expr(cols, ["entry", "entry_price", "buy_price", "price"], "NULL")
    stop_expr = choose_expr(cols, ["stop", "stop_loss", "stop_price"], "NULL")
    target_expr = choose_expr(cols, ["target", "target_price", "take_profit"], "NULL")
    exit_price_expr = choose_expr(cols, ["exit_price", "sell_price", "close_price"], "NULL")
    pnl_eur_expr = choose_expr(cols, ["pnl_eur", "pnl", "profit_eur"], "NULL")
    pnl_pct_expr = choose_expr(cols, ["pnl_pct", "profit_pct"], "NULL")
    setup_expr = choose_expr(cols, ["setup_type"], "'REAL_TRADE'")
    regime_expr = choose_expr(cols, ["market_regime"], "'UNKNOWN'")
    grade_expr = choose_expr(cols, ["grade"], "'REAL'")
    conf_expr = choose_expr(cols, ["confidence", "bot_confidence", "chance", "score"], "50")
    why_expr = choose_expr(cols, ["why", "reason"], "'real trade review'")

    where_parts = []
    if symbol_expr != "NULL":
        where_parts.append(f"{symbol_expr} IS NOT NULL")
    if entry_time_expr != "NULL":
        where_parts.append(f"{entry_time_expr} IS NOT NULL")
    if entry_expr != "NULL":
        where_parts.append(f"{entry_expr} IS NOT NULL")

    if not where_parts:
        print(f"⚠️ te weinig bruikbare kolommen in {table_name}")
        return []

    where_sql = " AND ".join(where_parts)

    sql = f"""
        SELECT
            {id_expr}::text AS source_trade_id,
            {symbol_expr}::text AS raw_symbol,
            {entry_time_expr} AS entry_time,
            {exit_time_expr} AS exit_time,
            {entry_expr}::double precision AS entry,
            {stop_expr}::double precision AS stop,
            {target_expr}::double precision AS target,
            {exit_price_expr}::double precision AS exit_price,
            {pnl_eur_expr}::double precision AS pnl_eur,
            {pnl_pct_expr}::double precision AS pnl_pct,
            {setup_expr}::text AS setup_type,
            {regime_expr}::text AS market_regime,
            {grade_expr}::text AS grade,
            {conf_expr}::int AS bot_confidence,
            {why_expr}::text AS why
        FROM public.{table_name}
        WHERE {where_sql}
        ORDER BY {entry_time_expr} ASC;
    """

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    out: List[RealTrade] = []
    for r in rows:
        raw_symbol = str(r["raw_symbol"]).upper().strip()
        coin = _normalize_symbol_to_coin(raw_symbol)

        entry_time = r["entry_time"]
        if not entry_time:
            continue

        entry = _safe_float(r["entry"])
        if entry is None or entry <= 0:
            continue

        bot_conf = int(r["bot_confidence"]) if r["bot_confidence"] is not None else 50
        bot_conf = max(0, min(100, bot_conf))

        out.append(
            RealTrade(
                source_table=table_name,
                source_trade_id=str(r["source_trade_id"]),
                coin=coin,
                raw_symbol=raw_symbol,
                entry_time=entry_time,
                exit_time=r["exit_time"],
                entry=float(entry),
                stop=_safe_float(r["stop"]),
                target=_safe_float(r["target"]),
                exit_price=_safe_float(r["exit_price"]),
                pnl_eur=_safe_float(r["pnl_eur"]),
                pnl_pct=_safe_float(r["pnl_pct"]),
                setup_type=(r["setup_type"] or "REAL_TRADE").upper(),
                market_regime=(r["market_regime"] or "UNKNOWN").upper(),
                grade=(r["grade"] or "REAL").upper(),
                bot_confidence=bot_conf,
                why=(r["why"] or "real trade review"),
            )
        )

    print(f"📄 loaded real trades from {table_name}: {len(out)}")
    return out


def load_all_real_trades(conn) -> List[RealTrade]:
    all_trades: List[RealTrade] = []
    for table_name in REAL_TRADES_TABLES:
        try:
            all_trades.extend(load_real_trades_from_table(conn, table_name))
        except Exception as e:
            print(f"⚠️ error loading {table_name}: {e}")
            traceback.print_exc()

    return all_trades


# ==========================================================
# CANDLE POSITION HELPERS
# ==========================================================
def _find_last_index_le(candles: List[Candle], ts: datetime) -> int:
    target_ms = int(ts.timestamp() * 1000)
    last_idx = -1
    for i, c in enumerate(candles):
        if c.ts_ms <= target_ms:
            last_idx = i
        else:
            break
    return last_idx


# ==========================================================
# REAL TRADE REVIEW LOGIC
# ==========================================================
def _simulate_real_trade_outcome(
    c1h: List[Candle],
    entry_idx: int,
    entry: float,
    stop: Optional[float],
    target: Optional[float],
    exit_time: Optional[datetime],
    exit_price: Optional[float],
    pnl_eur: Optional[float],
    pnl_pct: Optional[float],
) -> Dict[str, Any]:
    """
    Logica:
    - stop eerst geraakt => loss
    - target eerst geraakt => win
    - beide in zelfde candle => conservatief loss
    - als geen stop/target geraakt maar wel closed:
        exit_price > entry => win
        anders loss
    - anders pnl fallback
    - anders timeout/open => timeout
    """

    if entry <= 0:
        return {
            "outcome": "invalid",
            "mfe": 0.0,
            "mae": 0.0,
            "time_minutes": 0,
            "exit_time": exit_time,
            "result_reason": "invalid_entry",
        }

    risk = None
    if stop is not None and stop > 0 and stop < entry:
        risk = entry - stop
    else:
        risk = entry * DEFAULT_STOP_PCT

    if risk <= 0:
        risk = max(entry * 0.01, 1e-9)

    mfe_r = 0.0
    mae_r = 0.0

    start_ts = c1h[entry_idx].ts_ms

    last_idx = len(c1h) - 1
    if exit_time is not None:
        idx_exit = _find_last_index_le(c1h, exit_time)
        if idx_exit >= entry_idx:
            last_idx = idx_exit
        else:
            last_idx = min(len(c1h) - 1, entry_idx + MAX_HOLD_CANDLES)
    else:
        last_idx = min(len(c1h) - 1, entry_idx + MAX_HOLD_CANDLES)

    max_price_seen = entry
    min_price_seen = entry

    for i in range(entry_idx + 1, last_idx + 1):
        c = c1h[i]

        max_price_seen = max(max_price_seen, c.high)
        min_price_seen = min(min_price_seen, c.low)

        best = (c.high - entry) / risk
        worst = (entry - c.low) / risk

        mfe_r = max(mfe_r, best)
        mae_r = max(mae_r, worst)

        stop_hit = stop is not None and c.low <= stop
        target_hit = target is not None and c.high >= target

        if stop_hit and target_hit:
            tmin = int((c.ts_ms - start_ts) / 60000)
            return {
                "outcome": "loss",
                "mfe": _safe_round(mfe_r, 4),
                "mae": _safe_round(mae_r, 4),
                "time_minutes": tmin,
                "exit_time": c.ts,
                "result_reason": "stop_and_target_same_candle_conservative_loss",
                "max_price_seen": _safe_round(max_price_seen, 8),
                "min_price_seen": _safe_round(min_price_seen, 8),
            }

        if stop_hit:
            tmin = int((c.ts_ms - start_ts) / 60000)
            return {
                "outcome": "loss",
                "mfe": _safe_round(mfe_r, 4),
                "mae": _safe_round(mae_r, 4),
                "time_minutes": tmin,
                "exit_time": c.ts,
                "result_reason": "stop_hit_first",
                "max_price_seen": _safe_round(max_price_seen, 8),
                "min_price_seen": _safe_round(min_price_seen, 8),
            }

        if target_hit:
            tmin = int((c.ts_ms - start_ts) / 60000)
            return {
                "outcome": "win",
                "mfe": _safe_round(mfe_r, 4),
                "mae": _safe_round(mae_r, 4),
                "time_minutes": tmin,
                "exit_time": c.ts,
                "result_reason": "target_hit_first",
                "max_price_seen": _safe_round(max_price_seen, 8),
                "min_price_seen": _safe_round(min_price_seen, 8),
            }

    final_ts = c1h[last_idx].ts
    time_minutes = int((c1h[last_idx].ts_ms - start_ts) / 60000)

    if exit_price is not None:
        return {
            "outcome": "win" if exit_price > entry else "loss",
            "mfe": _safe_round(mfe_r, 4),
            "mae": _safe_round(mae_r, 4),
            "time_minutes": time_minutes,
            "exit_time": exit_time or final_ts,
            "result_reason": "closed_trade_exit_above_entry" if exit_price > entry else "closed_trade_exit_at_or_below_entry",
            "max_price_seen": _safe_round(max_price_seen, 8),
            "min_price_seen": _safe_round(min_price_seen, 8),
        }

    if pnl_eur is not None:
        return {
            "outcome": "win" if pnl_eur > 0 else "loss",
            "mfe": _safe_round(mfe_r, 4),
            "mae": _safe_round(mae_r, 4),
            "time_minutes": time_minutes,
            "exit_time": exit_time or final_ts,
            "result_reason": "closed_trade_positive_pnl" if pnl_eur > 0 else "closed_trade_non_positive_pnl",
            "max_price_seen": _safe_round(max_price_seen, 8),
            "min_price_seen": _safe_round(min_price_seen, 8),
        }

    if pnl_pct is not None:
        return {
            "outcome": "win" if pnl_pct > 0 else "loss",
            "mfe": _safe_round(mfe_r, 4),
            "mae": _safe_round(mae_r, 4),
            "time_minutes": time_minutes,
            "exit_time": exit_time or final_ts,
            "result_reason": "closed_trade_positive_pnl_pct" if pnl_pct > 0 else "closed_trade_non_positive_pnl_pct",
            "max_price_seen": _safe_round(max_price_seen, 8),
            "min_price_seen": _safe_round(min_price_seen, 8),
        }

    return {
        "outcome": "timeout",
        "mfe": _safe_round(mfe_r, 4),
        "mae": _safe_round(mae_r, 4),
        "time_minutes": time_minutes,
        "exit_time": exit_time or final_ts,
        "result_reason": "insufficient_close_data_timeout",
        "max_price_seen": _safe_round(max_price_seen, 8),
        "min_price_seen": _safe_round(min_price_seen, 8),
    }


# ==========================================================
# EXPERIENCE WRITER
# ==========================================================
def _trade_key_from_real_trade(trade: RealTrade) -> str:
    return f"REAL_REVIEW|{trade.source_table}|{trade.source_trade_id}"


def insert_experience_trade(conn, row: Dict[str, Any]) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.experience_trades (
                trade_key,
                source,
                timestamp,
                entry_time,
                exit_time,
                coin,
                entry_timeframe,
                regime_timeframe,
                setup_type,
                market_regime,
                grade,
                entry,
                stop,
                target,
                decision,
                outcome,
                mfe,
                mae,
                time_minutes,
                why,
                market_condition,
                bot_confidence,
                overextended,
                created_at,
                updated_at
            )
            VALUES (
                %s,
                'REAL_REVIEW',
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                'REAL',
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                NOW(),
                NOW()
            )
            ON CONFLICT (trade_key)
            DO UPDATE SET
                timestamp = EXCLUDED.timestamp,
                entry_time = EXCLUDED.entry_time,
                exit_time = EXCLUDED.exit_time,
                coin = EXCLUDED.coin,
                entry_timeframe = EXCLUDED.entry_timeframe,
                regime_timeframe = EXCLUDED.regime_timeframe,
                setup_type = EXCLUDED.setup_type,
                market_regime = EXCLUDED.market_regime,
                grade = EXCLUDED.grade,
                entry = EXCLUDED.entry,
                stop = EXCLUDED.stop,
                target = EXCLUDED.target,
                outcome = EXCLUDED.outcome,
                mfe = EXCLUDED.mfe,
                mae = EXCLUDED.mae,
                time_minutes = EXCLUDED.time_minutes,
                why = EXCLUDED.why,
                market_condition = EXCLUDED.market_condition,
                bot_confidence = EXCLUDED.bot_confidence,
                overextended = EXCLUDED.overextended,
                updated_at = NOW();
            """
            ,
            (
                row["trade_key"],
                row["timestamp"],
                row["entry_time"],
                row["exit_time"],
                row["coin"],
                row["entry_timeframe"],
                row["regime_timeframe"],
                row["setup_type"],
                row["market_regime"],
                row["grade"],
                row["entry"],
                row["stop"],
                row["target"],
                row["outcome"],
                row["mfe"],
                row["mae"],
                row["time_minutes"],
                row["why"],
                row["market_condition"],
                row["bot_confidence"],
                row["overextended"],
            ),
        )
        return True


# ==========================================================
# SCOREBOARD
# ==========================================================
def _scoreboard_key(row: Dict[str, Any]) -> str:
    return f"REAL_REVIEW|{row['setup_type']}|{row['market_regime']}|{row['grade']}"


def _update_scoreboard_local(sb: Dict[str, Any], row: Dict[str, Any]) -> None:
    setup = str(row["setup_type"])
    regime = str(row["market_regime"])
    grade = str(row["grade"])
    outcome = str(row["outcome"])
    mfe = float(row["mfe"])
    mae = float(row["mae"])
    tmin = int(row["time_minutes"])

    key = _scoreboard_key(row)

    rec = sb.get(
        key,
        {
            "score_key": key,
            "setup_type": setup,
            "market_regime": regime,
            "grade": grade,
            "n": 0,
            "wins": 0,
            "losses": 0,
            "timeouts": 0,
            "avg_mfe": 0.0,
            "avg_mae": 0.0,
            "avg_time_minutes": 0.0,
            "win_rate": 0.0,
        },
    )

    n0 = int(rec["n"])
    n1 = n0 + 1
    rec["n"] = n1

    if outcome == "win":
        rec["wins"] = int(rec["wins"]) + 1
    elif outcome == "loss":
        rec["losses"] = int(rec["losses"]) + 1
    else:
        rec["timeouts"] = int(rec["timeouts"]) + 1

    rec["avg_mfe"] = (float(rec["avg_mfe"]) * n0 + mfe) / n1
    rec["avg_mae"] = (float(rec["avg_mae"]) * n0 + mae) / n1
    rec["avg_time_minutes"] = (float(rec["avg_time_minutes"]) * n0 + tmin) / n1
    rec["win_rate"] = (int(rec["wins"]) / n1) * 100.0 if n1 > 0 else 0.0

    sb[key] = rec


def upsert_scoreboard(conn, combined_sb: Dict[str, Any]) -> None:
    with conn.cursor() as cur:
        for rec in combined_sb.values():
            cur.execute(
                """
                INSERT INTO public.experience_scoreboard (
                    score_key,
                    setup_type,
                    market_regime,
                    grade,
                    n,
                    wins,
                    losses,
                    timeouts,
                    avg_mfe,
                    avg_mae,
                    avg_time_minutes,
                    win_rate,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    NOW()
                )
                ON CONFLICT (score_key)
                DO UPDATE SET
                    setup_type = EXCLUDED.setup_type,
                    market_regime = EXCLUDED.market_regime,
                    grade = EXCLUDED.grade,
                    n = EXCLUDED.n,
                    wins = EXCLUDED.wins,
                    losses = EXCLUDED.losses,
                    timeouts = EXCLUDED.timeouts,
                    avg_mfe = EXCLUDED.avg_mfe,
                    avg_mae = EXCLUDED.avg_mae,
                    avg_time_minutes = EXCLUDED.avg_time_minutes,
                    win_rate = EXCLUDED.win_rate,
                    updated_at = NOW();
                """,
                (
                    rec["score_key"],
                    rec["setup_type"],
                    rec["market_regime"],
                    rec["grade"],
                    int(rec["n"]),
                    int(rec["wins"]),
                    int(rec["losses"]),
                    int(rec["timeouts"]),
                    float(rec["avg_mfe"]),
                    float(rec["avg_mae"]),
                    float(rec["avg_time_minutes"]),
                    float(rec["win_rate"]),
                ),
            )

    conn.commit()


def merge_scoreboards(all_scoreboards: List[Dict[str, Any]]) -> Dict[str, Any]:
    combined: Dict[str, Any] = {}

    for sb in all_scoreboards:
        for key, b in sb.items():
            if key not in combined:
                combined[key] = dict(b)
                continue

            a = combined[key]
            nA = int(a["n"])
            nB = int(b["n"])
            nT = nA + nB

            if nT == 0:
                continue

            def wavg(xa, xb):
                return (float(xa) * nA + float(xb) * nB) / nT

            a["n"] = nT
            a["wins"] = int(a["wins"]) + int(b["wins"])
            a["losses"] = int(a["losses"]) + int(b["losses"])
            a["timeouts"] = int(a["timeouts"]) + int(b["timeouts"])
            a["avg_mfe"] = wavg(a["avg_mfe"], b["avg_mfe"])
            a["avg_mae"] = wavg(a["avg_mae"], b["avg_mae"])
            a["avg_time_minutes"] = wavg(a["avg_time_minutes"], b["avg_time_minutes"])
            a["win_rate"] = (int(a["wins"]) / int(a["n"])) * 100.0 if int(a["n"]) > 0 else 0.0

    return combined


# ==========================================================
# MAIN REVIEW
# ==========================================================
def review_real_trades(conn) -> Tuple[int, Dict[str, Any]]:
    real_trades = load_all_real_trades(conn)
    if not real_trades:
        print("⚠️ geen echte trades gevonden")
        return 0, {}

    needed_symbols = [t.coin for t in real_trades]
    candles_1h_map = load_candles_map_for_needed_symbols(conn, needed_symbols, TF_ENTRY)
    candles_4h_map = load_candles_map_for_needed_symbols(conn, needed_symbols, TF_REGIME)

    total_inserted = 0
    processed = 0
    sb: Dict[str, Any] = {}

    for trade in real_trades:
        try:
            c1h = candles_1h_map.get(trade.coin, [])
            c4h = candles_4h_map.get(trade.coin, [])

            if not c1h:
                print(f"⚠️ geen {TF_ENTRY} candles voor {trade.coin} | source={trade.source_table} id={trade.source_trade_id}")
                continue

            entry_idx = _find_last_index_le(c1h, trade.entry_time)
            if entry_idx < 0:
                print(f"⚠️ entry_time buiten candle range | {trade.coin} | {trade.source_table} {trade.source_trade_id}")
                continue

            market_regime = trade.market_regime
            if market_regime == "UNKNOWN" and c4h:
                regime_map = _compute_regime_map(c4h)
                market_regime = _regime_at_ts(regime_map, c4h, c1h[entry_idx].ts_ms)

            bot_confidence = trade.bot_confidence
            why = trade.why
            if bot_confidence == 50 and why == "real trade review":
                score, why_auto = _score_at_trade_entry(c1h, entry_idx)
                bot_confidence = score
                why = why_auto

            grade = trade.grade
            if grade in ("REAL", "", None):
                grade = _grade_from_score(bot_confidence)

            if trade.target is None and trade.stop is not None:
                risk = trade.entry - trade.stop
                trade_target = trade.entry + (risk * DEFAULT_RR_TARGET) if risk > 0 else None
            else:
                trade_target = trade.target

            sim = _simulate_real_trade_outcome(
                c1h=c1h,
                entry_idx=entry_idx,
                entry=trade.entry,
                stop=trade.stop,
                target=trade_target,
                exit_time=trade.exit_time,
                exit_price=trade.exit_price,
                pnl_eur=trade.pnl_eur,
                pnl_pct=trade.pnl_pct,
            )

            if sim["outcome"] == "invalid":
                continue

            market_condition = _market_condition_from_atr_like(c1h, entry_idx)
            overext = _overextended_pct(c1h, entry_idx)

            why_full = (
                f"{why} | source={trade.source_table} | trade_id={trade.source_trade_id} | "
                f"reason={sim['result_reason']}"
            )

            row = {
                "trade_key": _trade_key_from_real_trade(trade),
                "timestamp": trade.entry_time,
                "entry_time": trade.entry_time,
                "exit_time": sim["exit_time"],
                "coin": trade.coin,
                "entry_timeframe": TF_ENTRY,
                "regime_timeframe": TF_REGIME,
                "setup_type": trade.setup_type or "REAL_TRADE",
                "market_regime": market_regime or "UNKNOWN",
                "grade": grade or "REAL",
                "entry": _safe_round(trade.entry, 8),
                "stop": _safe_round(trade.stop, 8) if trade.stop is not None else None,
                "target": _safe_round(trade_target, 8) if trade_target is not None else None,
                "outcome": sim["outcome"],
                "mfe": float(sim["mfe"]),
                "mae": float(sim["mae"]),
                "time_minutes": int(sim["time_minutes"]),
                "why": why_full[:1000],
                "market_condition": market_condition,
                "bot_confidence": int(bot_confidence),
                "overextended": _safe_round(overext, 4),
            }

            insert_experience_trade(conn, row)
            _update_scoreboard_local(sb, row)

            processed += 1
            total_inserted += 1

            if processed % PRINT_EVERY == 0:
                conn.commit()
                print(f"✅ reviewed={processed}")

        except Exception as trade_err:
            conn.rollback()
            print(f"⚠️ error reviewing trade {trade.source_table}:{trade.source_trade_id} -> {trade_err}")
            traceback.print_exc()

    conn.commit()
    print(f"✅ real trade review klaar | processed={processed}")
    return total_inserted, sb


# ==========================================================
# MAIN
# ==========================================================
def main():
    print("🚀 history_simulator REAL TRADE REVIEW START")
    print(f"REAL_TRADES_TABLES={REAL_TRADES_TABLES}")
    print(f"TF_ENTRY={TF_ENTRY}")
    print(f"TF_REGIME={TF_REGIME}")
    print(f"SIM_RESET={SIM_RESET}")
    print(f"DEFAULT_STOP_PCT={DEFAULT_STOP_PCT}")
    print(f"DEFAULT_RR_TARGET={DEFAULT_RR_TARGET}")
    print("")

    conn = None

    try:
        conn = pg_connect()
        set_db_safety(conn)

        print("🔧 syncing experience_trades schema...")
        sync_schema()

        ensure_tables(conn)

        if SIM_RESET:
            print("🧹 SIM_RESET=1 -> alleen REAL_REVIEW rows worden geleegd")
            reset_review_rows(conn)

        total_inserted, sb = review_real_trades(conn)
        combined_sb = merge_scoreboards([sb])
        upsert_scoreboard(conn, combined_sb)

        print("")
        print(f"✅ history_simulator DONE | total_reviewed={total_inserted}")
        print("📄 table: public.experience_trades")
        print("📊 table: public.experience_scoreboard")

    except Exception as e:
        if conn:
            conn.rollback()
        print(f"❌ history_simulator FAILED: {e}")
        traceback.print_exc()
        raise
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    main()
