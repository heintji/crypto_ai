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

# Welke coins simuleren?
COINS = (os.getenv("HIST_COINS") or "BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT,ADAUSDT,SOLUSDT,DOGEUSDT").split(",")
COINS = [c.strip().upper() for c in COINS if c.strip()]

# Timeframes
TF_ENTRY = (os.getenv("SIM_ENTRY_INTERVAL") or "1h").strip().lower()
TF_REGIME = (os.getenv("SIM_REGIME_INTERVAL") or "4h").strip().lower()

# Simulatie regels
STOP_PCT = float(os.getenv("SIM_STOP_PCT") or "0.02")        # 2% stop
RR_TARGET = float(os.getenv("SIM_RR") or "2.0")              # target = 2R
MAX_HOLD_CANDLES = int(os.getenv("SIM_MAX_HOLD") or "72")    # max aantal entry-candles vooruit

# Grade regels
GO_MIN_SCORE = int(os.getenv("GO_MIN_SCORE") or "80")
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "60")

# Setups
SETUPS = (os.getenv("SIM_SETUPS") or "TREND_PULLBACK,BREAKOUT_RETEST").split(",")
SETUPS = [s.strip().upper() for s in SETUPS if s.strip()]

# Reset voor nieuwe run
SIM_RESET = (os.getenv("SIM_RESET") or "1").strip() == "1"

# Veiligheidsinstellingen
DB_STATEMENT_TIMEOUT_MS = int(os.getenv("SIM_DB_TIMEOUT_MS") or "120000")
PRINT_EVERY = int(os.getenv("SIM_PRINT_EVERY") or "500")


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


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.experience_trades (
                id BIGSERIAL PRIMARY KEY,
                trade_key TEXT UNIQUE,
                source TEXT NOT NULL DEFAULT 'SIM',

                timestamp TIMESTAMPTZ NOT NULL,
                entry_time TIMESTAMPTZ NOT NULL,
                exit_time TIMESTAMPTZ,

                coin TEXT NOT NULL,
                entry_timeframe TEXT NOT NULL,
                regime_timeframe TEXT NOT NULL,

                setup_type TEXT NOT NULL,
                market_regime TEXT NOT NULL,
                grade TEXT NOT NULL,

                entry DOUBLE PRECISION NOT NULL,
                stop DOUBLE PRECISION NOT NULL,
                target DOUBLE PRECISION NOT NULL,

                decision TEXT NOT NULL DEFAULT 'SIM',
                outcome TEXT NOT NULL,

                mfe DOUBLE PRECISION NOT NULL DEFAULT 0,
                mae DOUBLE PRECISION NOT NULL DEFAULT 0,
                time_minutes INTEGER NOT NULL DEFAULT 0,

                why TEXT,
                market_condition TEXT,
                bot_confidence INTEGER,
                overextended DOUBLE PRECISION,

                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

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
            ON public.experience_trades(timestamp DESC);
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


def reset_tables(conn):
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE public.experience_trades RESTART IDENTITY;")
        cur.execute("TRUNCATE TABLE public.experience_scoreboard;")
    conn.commit()


# ==========================================================
# LOAD CANDLES FROM POSTGRES
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


# ==========================================================
# MATH / INDICATORS
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


def _safe_round(v: Any, ndigits: int = 4) -> float:
    try:
        return round(float(v), ndigits)
    except Exception:
        return 0.0


# ==========================================================
# REGIME LABELING (4H)
# ==========================================================
def _compute_regime_map(c4h: List[Candle]) -> Dict[int, str]:
    """
    regime = BULL / BEAR / RANGE
    - BULL: SMA20 > SMA50 en slope(SMA20) positief
    - BEAR: SMA20 < SMA50 en slope(SMA20) negatief
    - anders RANGE
    """
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
    """
    Pak laatste 4h candle die <= ts_ms is.
    """
    last = "UNKNOWN"
    for c in c4h:
        if c.ts_ms <= ts_ms:
            last = regime_map.get(c.ts_ms, "UNKNOWN")
        else:
            break
    return last


# ==========================================================
# SETUP / SCORE LOGIC (1H)
# ==========================================================
def _score_and_setup_1h(closes: List[float]) -> Tuple[int, str, Dict[str, float]]:
    s20 = _sma(closes, 20)
    s50 = _sma(closes, 50)
    r14 = _rsi(closes, 14)

    if s20 is None or s50 is None or r14 is None:
        return 0, "NONE", {}

    score = 50
    score += 20 if s20 > s50 else -10

    if r14 >= 60:
        score += 20
    elif r14 <= 40:
        score -= 10

    score = max(0, min(100, score))

    if s20 > s50 and r14 >= 55:
        setup = "TREND_PULLBACK"
    elif s20 <= s50 and r14 >= 55:
        setup = "BREAKOUT_RETEST"
    else:
        setup = "NONE"

    return score, setup, {"sma20": s20, "sma50": s50, "rsi14": r14}


def _is_breakout_retest(c1h: List[Candle], idx: int, lookback: int = 48) -> bool:
    """
    Simpele breakout-retest:
    - koers breekt boven hoogste high van lookback
    - daarna retest
    - en sluit weer boven het niveau
    """
    if idx < lookback + 10:
        return False

    past = c1h[idx - lookback: idx - 9]
    lvl = max(x.high for x in past)

    breakout = any(c1h[j].close > lvl for j in range(idx - 8, idx - 4))
    if not breakout:
        return False

    for j in range(idx - 4, idx + 1):
        if c1h[j].low <= lvl * 1.002 and c1h[j].close > lvl:
            return True

    return False


def _grade_from_score(score: int) -> str:
    if score >= GO_MIN_SCORE:
        return "GO"
    if score >= WATCH_MIN_SCORE:
        return "WATCH"
    return "IGNORE"


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


# ==========================================================
# FORWARD SIMULATION
# ==========================================================
def _simulate_forward(
    c1h: List[Candle],
    entry_idx: int,
    entry: float,
    stop: float,
    target: float,
) -> Dict[str, Any]:
    """
    LONG logic:
    - low <= stop => loss
    - high >= target => win
    - timeout na MAX_HOLD_CANDLES

    MFE / MAE in R
    """
    risk = entry - stop
    if risk <= 0:
        return {
            "outcome": "invalid",
            "mfe": 0.0,
            "mae": 0.0,
            "time_minutes": 0,
            "exit_time": None,
        }

    mfe_r = 0.0
    mae_r = 0.0

    start_ts = c1h[entry_idx].ts_ms
    last_idx = min(len(c1h) - 1, entry_idx + MAX_HOLD_CANDLES)

    for i in range(entry_idx + 1, last_idx + 1):
        c = c1h[i]

        best = (c.high - entry) / risk
        worst = (entry - c.low) / risk

        mfe_r = max(mfe_r, best)
        mae_r = max(mae_r, worst)

        # Conservatief: eerst stop
        if c.low <= stop:
            tmin = int((c.ts_ms - start_ts) / 60000)
            return {
                "outcome": "loss",
                "mfe": _safe_round(mfe_r, 4),
                "mae": _safe_round(mae_r, 4),
                "time_minutes": tmin,
                "exit_time": c.ts,
            }

        if c.high >= target:
            tmin = int((c.ts_ms - start_ts) / 60000)
            return {
                "outcome": "win",
                "mfe": _safe_round(mfe_r, 4),
                "mae": _safe_round(mae_r, 4),
                "time_minutes": tmin,
                "exit_time": c.ts,
            }

    tmin = int((c1h[last_idx].ts_ms - start_ts) / 60000)
    return {
        "outcome": "timeout",
        "mfe": _safe_round(mfe_r, 4),
        "mae": _safe_round(mae_r, 4),
        "time_minutes": tmin,
        "exit_time": c1h[last_idx].ts,
    }


# ==========================================================
# EXPERIENCE WRITER (POSTGRES)
# ==========================================================
def _trade_key(
    symbol: str,
    ts_ms: int,
    setup_type: str,
    regime: str,
    grade: str,
    entry: float,
    stop: float,
    target: float,
) -> str:
    return (
        f"{symbol}|{ts_ms}|{setup_type}|{regime}|{grade}|"
        f"{entry:.8f}|{stop:.8f}|{target:.8f}|{TF_ENTRY}|{TF_REGIME}"
    )


def insert_experience_trade(conn, row: Dict[str, Any]) -> bool:
    """
    Returns True als row echt nieuw is geplaatst.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.experience_trades (
                trade_key, source,
                timestamp, entry_time, exit_time,
                coin, entry_timeframe, regime_timeframe,
                setup_type, market_regime, grade,
                entry, stop, target,
                decision, outcome,
                mfe, mae, time_minutes,
                why, market_condition, bot_confidence, overextended,
                created_at, updated_at
            )
            VALUES (
                %s, 'SIM',
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                'SIM', %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                NOW(), NOW()
            )
            ON CONFLICT (trade_key) DO NOTHING;
            """,
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
        inserted = cur.rowcount > 0

    return inserted


# ==========================================================
# SCOREBOARD
# ==========================================================
def _update_scoreboard_local(sb: Dict[str, Any], row: Dict[str, Any]) -> None:
    setup = str(row["setup_type"])
    regime = str(row["market_regime"])
    grade = str(row["grade"])
    outcome = str(row["outcome"])
    mfe = float(row["mfe"])
    mae = float(row["mae"])
    tmin = int(row["time_minutes"])

    key = f"{setup}|{regime}|{grade}"

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
                    setup_type, market_regime, grade,
                    n, wins, losses, timeouts,
                    avg_mfe, avg_mae, avg_time_minutes, win_rate,
                    updated_at
                )
                VALUES (
                    %s,
                    %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
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


# ==========================================================
# MAIN PER SYMBOL
# ==========================================================
def run_for_symbol(conn, symbol: str) -> Tuple[int, Dict[str, Any]]:
    c1h = load_candles(conn, symbol, TF_ENTRY)
    c4h = load_candles(conn, symbol, TF_REGIME)

    if not c1h or not c4h:
        print(f"⚠️ Missing DB candles for {symbol}: entry={len(c1h)} regime={len(c4h)}")
        return 0, {}

    print(f"📈 {symbol}: {TF_ENTRY} candles={len(c1h)} | {TF_REGIME} candles={len(c4h)}")

    regime_map = _compute_regime_map(c4h)

    closes_1h: List[float] = []
    sb: Dict[str, Any] = {}
    inserted_count = 0
    seen_candidates = 0

    for i in range(len(c1h)):
        closes_1h.append(c1h[i].close)

        if i < 60:
            continue

        score, base_setup, ind = _score_and_setup_1h(closes_1h)
        grade = _grade_from_score(score)
        if grade == "IGNORE":
            continue

        setup_type = "NONE"

        if "TREND_PULLBACK" in SETUPS and base_setup == "TREND_PULLBACK":
            setup_type = "TREND_PULLBACK"

        if "BREAKOUT_RETEST" in SETUPS and _is_breakout_retest(c1h, i):
            setup_type = "BREAKOUT_RETEST"

        if setup_type == "NONE":
            continue

        regime = _regime_at_ts(regime_map, c4h, c1h[i].ts_ms)
        if regime == "UNKNOWN":
            continue

        entry = c1h[i].close
        stop = entry * (1.0 - STOP_PCT)
        target = entry + (entry - stop) * RR_TARGET

        if stop <= 0 or target <= entry:
            continue

        sim = _simulate_forward(c1h, i, entry, stop, target)
        if sim["outcome"] == "invalid":
            continue

        market_condition = _market_condition_from_atr_like(c1h, i)
        overext = _overextended_pct(c1h, i)
        why = f"SMA20/50 + RSI ({round(ind.get('rsi14', 0.0), 1)})"

        row = {
            "trade_key": _trade_key(
                symbol=symbol,
                ts_ms=c1h[i].ts_ms,
                setup_type=setup_type,
                regime=regime,
                grade=grade,
                entry=entry,
                stop=stop,
                target=target,
            ),
            "timestamp": c1h[i].ts,
            "entry_time": c1h[i].ts,
            "exit_time": sim["exit_time"],
            "coin": symbol,
            "entry_timeframe": TF_ENTRY,
            "regime_timeframe": TF_REGIME,
            "setup_type": setup_type,
            "market_regime": regime,
            "grade": grade,
            "entry": _safe_round(entry, 8),
            "stop": _safe_round(stop, 8),
            "target": _safe_round(target, 8),
            "outcome": sim["outcome"],
            "mfe": float(sim["mfe"]),
            "mae": float(sim["mae"]),
            "time_minutes": int(sim["time_minutes"]),
            "why": why,
            "market_condition": market_condition,
            "bot_confidence": int(score),
            "overextended": _safe_round(overext, 4),
        }

        seen_candidates += 1

        inserted = insert_experience_trade(conn, row)
        if inserted:
            inserted_count += 1
            _update_scoreboard_local(sb, row)

            if inserted_count % PRINT_EVERY == 0:
                print(f"✅ {symbol}: inserted={inserted_count} candidates={seen_candidates}")

    conn.commit()
    print(f"✅ {symbol}: final_inserted={inserted_count} candidates={seen_candidates}")
    return inserted_count, sb


# ==========================================================
# MERGE SCOREBOARDS
# ==========================================================
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
# MAIN
# ==========================================================
def main():
    print("🚀 history_simulator DB-only START")
    print(f"COINS={COINS}")
    print(f"TF_ENTRY={TF_ENTRY}")
    print(f"TF_REGIME={TF_REGIME}")
    print(f"STOP_PCT={STOP_PCT}")
    print(f"RR_TARGET={RR_TARGET}")
    print(f"MAX_HOLD_CANDLES={MAX_HOLD_CANDLES}")
    print(f"GO_MIN_SCORE={GO_MIN_SCORE}")
    print(f"WATCH_MIN_SCORE={WATCH_MIN_SCORE}")
    print(f"SETUPS={SETUPS}")
    print(f"SIM_RESET={SIM_RESET}")
    print("")

    conn = None

    try:
        conn = pg_connect()
        set_db_safety(conn)
        ensure_tables(conn)

        if SIM_RESET:
            print("🧹 SIM_RESET=1 -> experience_trades + experience_scoreboard worden geleegd")
            reset_tables(conn)

        total_inserted = 0
        all_scoreboards: List[Dict[str, Any]] = []

        for coin in COINS:
            try:
                inserted, sb = run_for_symbol(conn, coin)
                total_inserted += inserted
                all_scoreboards.append(sb)
            except Exception as coin_err:
                conn.rollback()
                print(f"⚠️ Error for coin {coin}: {coin_err}")
                traceback.print_exc()

        combined_sb = merge_scoreboards(all_scoreboards)
        upsert_scoreboard(conn, combined_sb)

        print("")
        print(f"✅ history_simulator DONE | total_inserted={total_inserted}")
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
    print("🔧 syncing experience_trades schema...")
    sync_schema()
    main()
