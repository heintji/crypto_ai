# analysis/multi_coin_score.py
from __future__ import annotations

import os
import sys
import json
import time
import math
import traceback
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras


# =============================================================================
# PATH / DIR HELPERS
# =============================================================================

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _is_writable_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        test_file = os.path.join(path, ".write_test")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_file)
        return True
    except Exception:
        return False


def resolve_data_dir() -> str:
    """
    Prefer DATA_DIR if writable, else fallback to /data if writable, else /tmp/crypto_ai_data.
    This avoids PermissionError when Cron shell has no disk mount.
    """
    env_dir = (os.getenv("DATA_DIR") or "").strip()
    candidates = []
    if env_dir:
        candidates.append(env_dir)
    candidates.append("/data")
    candidates.append("/tmp/crypto_ai_data")

    for c in candidates:
        if _is_writable_dir(c):
            return c
    # last resort: current working dir
    return os.getcwd()


DATA_DIR = resolve_data_dir()

LOGS_DIR_ENV = (os.getenv("LOGS_DIR") or "").strip()
LOGS_DIR = LOGS_DIR_ENV if LOGS_DIR_ENV else os.path.join(DATA_DIR, "logs")
if not _is_writable_dir(LOGS_DIR):
    # fallback if LOGS_DIR points to unwritable location
    LOGS_DIR = os.path.join("/tmp", "crypto_ai_logs")
    os.makedirs(LOGS_DIR, exist_ok=True)

SCOREBOARD_PATH = os.path.join(DATA_DIR, "scoreboard.json")


# =============================================================================
# ENV / SETTINGS
# =============================================================================

DATABASE_URL = (os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL".lower()) or "").strip()
if not DATABASE_URL:
    # You use DATABASE_URL in Render; but keep also DATABASE_URL env check
    DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

USE_DB_APPROVALS = (os.getenv("USE_DB_APPROVALS") or "1").strip() == "1"

# Universe controls
AUTO_UNIVERSE = (os.getenv("AUTO_UNIVERSE") or "0").strip() == "1"

# Accept legacy env names too (in case you used older ones)
CORE_LIMIT = int((os.getenv("CORE_LIMIT") or os.getenv("CORE") or "28").strip())
ROTATE_BATCH_SIZE = int((os.getenv("ROTATE_BATCH_SIZE") or os.getenv("ROTATE_BATCH") or "50").strip())

# Scan candle history
TF_MAIN = (os.getenv("TF_MAIN") or "4h").strip()
TF_CONTEXT = (os.getenv("TF_CONTEXT") or "1h").strip()
LIMIT_MAIN = int((os.getenv("LIMIT_MAIN") or "240").strip())       # 4h candles
LIMIT_CONTEXT = int((os.getenv("LIMIT_CONTEXT") or "240").strip()) # 1h candles

# Prebuy controls
FORCE_TEST_PREBUY = (os.getenv("FORCE_TEST_PREBUY") or "0").strip() == "1"
MAX_PREBUY_PER_DAY = int((os.getenv("MAX_PREBUY_PER_DAY") or "5").strip())
MIN_SCORE_TO_PREBUY = int((os.getenv("MIN_SCORE_TO_PREBUY") or "80").strip())
WATCH_MIN_SCORE = int((os.getenv("WATCH_MIN_SCORE") or "70").strip())

# If AUTO_UNIVERSE is off, fallback list:
DEFAULT_COINS = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "SOLUSDT",
    "DOGEUSDT",
]


# =============================================================================
# LOGGING
# =============================================================================

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"{ts} | {msg}"
    print(line, flush=True)

    # best-effort file log
    try:
        fp = os.path.join(LOGS_DIR, "multi_coin_score.log")
        with open(fp, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# =============================================================================
# DB HELPERS
# =============================================================================

def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is empty. Set DATABASE_URL in Render.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def table_columns(cur, table: str) -> List[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position
        """,
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


# =============================================================================
# INDICATORS (simple + stable)
# =============================================================================

def sma(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    s = sum(values[:period])
    out[period - 1] = s / period
    for i in range(period, len(values)):
        s += values[i] - values[i - period]
        out[i] = s / period
    return out


def rsi(values: List[float], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period + 1:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d

    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))

    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        gain = max(d, 0.0)
        loss = max(-d, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))

    return out


def slope(values: List[Optional[float]], lookback: int = 10) -> float:
    """Simple slope on last lookback points (ignores None)."""
    pts = [v for v in values if v is not None]
    if len(pts) < lookback:
        return 0.0
    y = pts[-lookback:]
    # normalized slope (end - start)
    return float(y[-1] - y[0])


# =============================================================================
# DATA FETCHING (DB candles)
# =============================================================================

def fetch_candles_db(symbol: str, timeframe: str, limit: int) -> List[Dict]:
    """
    Reads candles from public.candles.
    Your schema uses: open_time (timestamptz), open/high/low/close/volume.
    """
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT open_time, open, high, low, close, volume
                FROM candles
                WHERE symbol = %s AND timeframe = %s
                ORDER BY open_time DESC
                LIMIT %s
                """,
                (symbol, timeframe, limit),
            )
            rows = cur.fetchall()

    rows = list(reversed(rows))
    out: List[Dict] = []
    for r in rows:
        out.append(
            {
                "open_time": r["open_time"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"]),
            }
        )
    return out


# =============================================================================
# UNIVERSE
# =============================================================================

def get_auto_universe(core_limit: int, rotate_batch: int) -> List[str]:
    """
    Universe based on candles table.
    - Core: top symbols by MAX(volume) on 1h timeframe
    - Rotate: other symbols (alphabetical) batch
    """
    core: List[str] = []
    rotate: List[str] = []

    with db_conn() as conn:
        with conn.cursor() as cur:
            # Core (liquid by volume) — uses your existing 'volume' column
            cur.execute(
                """
                SELECT symbol
                FROM candles
                WHERE timeframe = %s
                GROUP BY symbol
                ORDER BY MAX(volume) DESC
                LIMIT %s
                """,
                (TF_CONTEXT, core_limit),
            )
            core = [r[0] for r in cur.fetchall()]

            if rotate_batch <= 0:
                rotate = []
            else:
                # Rotate all remaining symbols (stable order), take a moving offset
                cur.execute(
                    """
                    SELECT DISTINCT symbol
                    FROM candles
                    WHERE timeframe = %s
                    ORDER BY symbol ASC
                    """,
                    (TF_CONTEXT,),
                )
                all_syms = [r[0] for r in cur.fetchall()]
                remaining = [s for s in all_syms if s not in set(core)]

                # store offset in DB-less way: use day + hour bucket, deterministic rotation
                # (no disk needed)
                now = datetime.now(timezone.utc)
                seed = int(now.strftime("%Y%m%d%H"))
                if remaining:
                    offset = seed % len(remaining)
                else:
                    offset = 0

                rotate_total = len(remaining)
                batch = []
                for i in range(min(rotate_batch, rotate_total)):
                    batch.append(remaining[(offset + i) % rotate_total])
                rotate = batch

    universe = core + rotate
    log(f"🧠 universe: core={len(core)} + rotate_batch={len(rotate)} (core_limit={core_limit}, rotate_batch_size={rotate_batch}) => scan_total={len(universe)}")
    return universe


# =============================================================================
# APPROVALS (DB)
# =============================================================================

def insert_pending_approval(payload: Dict) -> bool:
    """
    Insert into pending_approvals, but only for columns that exist in your DB.
    This makes it robust across schema changes.
    """
    if not USE_DB_APPROVALS:
        return False

    with db_conn() as conn:
        with conn.cursor() as cur:
            cols = table_columns(cur, "pending_approvals")
            log(f"🧩 pending_approvals columns detected: {len(cols)}")

            insert_data = {}
            for k, v in payload.items():
                if k in cols:
                    insert_data[k] = v

            # minimal required guess: id + symbol + status + created_at
            if "id" in cols and "id" not in insert_data:
                insert_data["id"] = payload.get("id")

            if "status" in cols and "status" not in insert_data:
                insert_data["status"] = "PENDING"

            if "created_at" in cols and "created_at" not in insert_data:
                insert_data["created_at"] = datetime.now(timezone.utc)

            if not insert_data:
                log("⚠️ No matching columns to insert into pending_approvals.")
                return False

            keys = list(insert_data.keys())
            values = [insert_data[k] for k in keys]

            q_cols = ", ".join(keys)
            q_vals = ", ".join(["%s"] * len(keys))

            # If your table has UNIQUE(id), ON CONFLICT(id) DO NOTHING works.
            # If not, we still try; if it fails we fallback to plain insert.
            try:
                cur.execute(
                    f"""
                    INSERT INTO pending_approvals ({q_cols})
                    VALUES ({q_vals})
                    ON CONFLICT (id) DO NOTHING
                    """,
                    values,
                )
            except Exception:
                conn.rollback()
                cur.execute(
                    f"""
                    INSERT INTO pending_approvals ({q_cols})
                    VALUES ({q_vals})
                    """,
                    values,
                )

            conn.commit()
            return True


# =============================================================================
# SCORING (example baseline)
# =============================================================================

def score_symbol(symbol: str) -> Tuple[int, Dict]:
    """
    Very simple scoring placeholder:
    - Trend: SMA20 above SMA50 on 4h
    - RSI between 40-65 better
    - SMA20 slope positive
    Returns (score, debug_info)
    """
    candles = fetch_candles_db(symbol, TF_MAIN, LIMIT_MAIN)
    if len(candles) < 60:
        return 0, {"reason": "not_enough_candles"}

    closes = [c["close"] for c in candles]
    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    rsi14 = rsi(closes, 14)

    last = len(closes) - 1
    s = 0

    if sma20[last] is not None and sma50[last] is not None and sma20[last] > sma50[last]:
        s += 45
    if rsi14[last] is not None:
        if 40 <= rsi14[last] <= 65:
            s += 35
        elif 30 <= rsi14[last] < 40 or 65 < rsi14[last] <= 75:
            s += 20
        else:
            s += 5

    sl = slope(sma20, 10)
    if sl > 0:
        s += 20
    elif sl < 0:
        s += 5

    s = max(0, min(100, int(s)))

    info = {
        "sma20": sma20[last],
        "sma50": sma50[last],
        "rsi14": rsi14[last],
        "slope_sma20": sl,
        "last_close": closes[last],
    }
    return s, info


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    # scoreboard optional
    if os.path.exists(SCOREBOARD_PATH):
        try:
            with open(SCOREBOARD_PATH, "r", encoding="utf-8") as f:
                _ = json.load(f)
            log(f"📘 Scoreboard loaded: {SCOREBOARD_PATH}")
        except Exception:
            log(f"⚠️ Scoreboard unreadable: {SCOREBOARD_PATH} (ok, continue)")
    else:
        log(f"📘 Scoreboard missing/empty: {SCOREBOARD_PATH} (ok, continue)")

    # Choose coins
    if AUTO_UNIVERSE:
        if not DATABASE_URL:
            log("❌ AUTO_UNIVERSE=1 but DATABASE_URL missing.")
            coins = DEFAULT_COINS
        else:
            coins = get_auto_universe(CORE_LIMIT, ROTATE_BATCH_SIZE)
    else:
        coins = DEFAULT_COINS

    # Daily cap (simple, DB-less): reset each UTC day using a stamp file in writable dir
    day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    counter_path = os.path.join(DATA_DIR, "prebuy_state.json")
    state = {"day": day_key, "created_today": 0}

    try:
        if os.path.exists(counter_path):
            with open(counter_path, "r", encoding="utf-8") as f:
                state = json.load(f) or state
    except Exception:
        pass

    if state.get("day") != day_key:
        state = {"day": day_key, "created_today": 0}

    created_today = int(state.get("created_today") or 0)
    log(f"🚀 multi_coin_score start | created_today={created_today}/{MAX_PREBUY_PER_DAY} | coins={len(coins)}")

    created_now = 0

    for sym in coins:
        if created_today + created_now >= MAX_PREBUY_PER_DAY:
            break

        try:
            if FORCE_TEST_PREBUY:
                sc = 85
                info = {"forced": True}
            else:
                sc, info = score_symbol(sym)

            label = "GO" if sc >= MIN_SCORE_TO_PREBUY else ("WATCH" if sc >= WATCH_MIN_SCORE else "SKIP")
            if label == "SKIP":
                continue

            # Build payload (keep it DB-column-safe by using keys that likely exist)
            prebuy_id = f"PB-{sym}-{int(time.time())}"

            payload = {
                "id": prebuy_id,
                "symbol": sym,
                "chance": sc,              # your DB uses 'chance' in some places
                "confidence": sc,          # and 'confidence' in others
                "label": label,
                "status": "PENDING",
                "created_at": datetime.now(timezone.utc),
                "setup_type": "TREND_PULLBACK",  # placeholder
                "timeframe": TF_MAIN,
                "note": json.dumps(info, default=str),
            }

            ok = insert_pending_approval(payload)
            if ok:
                created_now += 1
                log(f"✅ Pre-BUY created: {prebuy_id} | {sym} | score={sc} | {label}")
            else:
                log(f"⚠️ Pre-BUY not inserted (schema mismatch or USE_DB_APPROVALS=0): {sym}")

        except Exception as e:
            log(f"⚠️ ERROR {sym}: {e}")
            log(traceback.format_exc())

    state["created_today"] = created_today + created_now
    try:
        with open(counter_path, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass

    log(f"done. created={created_now}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        log(traceback.format_exc())
        raise
