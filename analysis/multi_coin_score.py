from __future__ import annotations

import os
import math
import time
import hashlib
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
import requests

# ==========================================================
# CONFIG (ENV)
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

EXCHANGE = (os.getenv("EXCHANGE") or "binance").strip().lower()

# Universe (hoeveel coins je pakt uit Binance USDT lijst)
UNIVERSE_LIMIT = int(os.getenv("UNIVERSE_LIMIT") or "250")

# Timeframes
TF_MAIN = (os.getenv("TF_MAIN") or "4h").strip()
TF_CTX = (os.getenv("TF_CTX") or "1h").strip()

# Pre-buy regels
MAX_PREBUY_PER_DAY = int(os.getenv("MAX_PREBUY_PER_DAY") or "5")
MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "80")  # GO
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "70")          # WATCH

PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))

# Dedup / cooldown
TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS") or str(6 * 60 * 60))

# Performance / candles
MIN_CANDLES = int(os.getenv("MIN_CANDLES") or "120")  # genoeg voor SMA/RSI etc

# Optional: test mode (force prebuy)
FORCE_TEST_PREBUY = (os.getenv("FORCE_TEST_PREBUY") or "0").strip() == "1"


# ==========================================================
# HELPERS
# ==========================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(msg, flush=True)


def db_connect():
    # Render Postgres: sslmode=require
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def round_sig(x: float, sig: int = 6) -> float:
    if x == 0:
        return 0.0
    return round(x, sig - int(math.floor(math.log10(abs(x)))) - 1)


# ==========================================================
# BINANCE UNIVERSE
# ==========================================================
BINANCE_EXCHANGE_INFO = "https://api.binance.com/api/v3/exchangeInfo"


def fetch_binance_usdt_symbols(limit: int) -> List[str]:
    r = requests.get(BINANCE_EXCHANGE_INFO, timeout=20)
    r.raise_for_status()
    data = r.json()

    syms: List[str] = []
    for s in data.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        sym = s.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        # filter rare leveraged tokens if you want (optional)
        if sym.endswith(("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")):
            continue
        syms.append(sym)

    # simple deterministic pick
    syms = sorted(set(syms))
    return syms[:limit]


# ==========================================================
# INDICATORS (SMA, RSI)
# ==========================================================
def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += abs(diff)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def slope(values: List[float], lookback: int = 10) -> Optional[float]:
    if len(values) < lookback + 1:
        return None
    # simple slope: last - first / lookback
    return (values[-1] - values[-(lookback + 1)]) / float(lookback)


# ==========================================================
# DB SCHEMA DETECTION (fix fp vs fingerprint, key vs day)
# ==========================================================
@dataclass
class SchemaInfo:
    fingerprints_col: str          # "fingerprint" or "fp"
    pending_has_timeframe: bool
    prebuy_state_key_col: str      # "day" or "key"


def table_has_column(conn, table: str, col: str, schema: str = "public") -> bool:
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


def detect_schema(conn) -> SchemaInfo:
    # trade_fingerprints: prefer fingerprint, else fp
    if table_has_column(conn, "trade_fingerprints", "fingerprint"):
        fp_col = "fingerprint"
    elif table_has_column(conn, "trade_fingerprints", "fp"):
        fp_col = "fp"
    else:
        # last resort: create fingerprint col
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE public.trade_fingerprints ADD COLUMN IF NOT EXISTS fingerprint TEXT;")
        conn.commit()
        fp_col = "fingerprint"

    pending_has_timeframe = table_has_column(conn, "pending_approvals", "timeframe")

    # prebuy_state: your latest DB uses day + created_count
    if table_has_column(conn, "prebuy_state", "day"):
        key_col = "day"
    elif table_has_column(conn, "prebuy_state", "key"):
        key_col = "key"
    else:
        # create minimal prebuy_state if missing
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS public.prebuy_state (
                  day TEXT PRIMARY KEY,
                  created_count INTEGER NOT NULL DEFAULT 0,
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        conn.commit()
        key_col = "day"

    return SchemaInfo(
        fingerprints_col=fp_col,
        pending_has_timeframe=pending_has_timeframe,
        prebuy_state_key_col=key_col,
    )


# ==========================================================
# DB READ: candles from Postgres (table public.candles)
# ==========================================================
def fetch_candles(conn, symbol: str, timeframe: str, limit: int) -> List[Dict[str, Any]]:
    # Your candles schema: exchange, symbol, timeframe, open_time, open, high, low, close, volume, close_time ...
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT open_time, open, high, low, close, volume
            FROM public.candles
            WHERE exchange=%s AND symbol=%s AND timeframe=%s
            ORDER BY open_time ASC
            LIMIT %s
            """,
            (EXCHANGE, symbol, timeframe, limit),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


# ==========================================================
# REGIME (optional) from table public.market_regime
# ==========================================================
def fetch_regime(conn, symbol: str, timeframe: str) -> Tuple[str, int]:
    # returns (regime, score)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT regime, score
                FROM public.market_regime
                WHERE exchange=%s AND symbol=%s AND timeframe=%s
                ORDER BY asof_ts DESC
                LIMIT 1
                """,
                (EXCHANGE, symbol, timeframe),
            )
            row = cur.fetchone()
        if not row:
            return "UNKNOWN", 0
        return (row.get("regime") or "UNKNOWN", safe_int(row.get("score"), 0))
    except Exception:
        # never crash scoring on regime
        conn.rollback()
        return "UNKNOWN", 0


# ==========================================================
# SCORING (simple, stable)
# ==========================================================
def compute_score_and_levels(closes_main: List[float], closes_ctx: List[float]) -> Tuple[int, float, float, float]:
    """
    Returns: score(0-100), entry, stop, target
    Very simple version:
    - entry = last close (main)
    - stop = 2% below entry
    - target = entry + 2R (R = entry-stop)
    """
    entry = closes_main[-1]
    stop = entry * 0.98
    r = entry - stop
    target = entry + 2.0 * r

    sma20 = sma(closes_main, 20)
    sma50 = sma(closes_main, 50)
    rsi14 = rsi(closes_ctx, 14)
    s20_slope = slope(closes_main, 10)

    score = 50

    # trend bias
    if sma20 is not None and sma50 is not None:
        if sma20 > sma50:
            score += 15
        else:
            score -= 10

    # momentum
    if rsi14 is not None:
        if 45 <= rsi14 <= 65:
            score += 10
        elif rsi14 > 70:
            score -= 10  # overbought penalty
        elif rsi14 < 35:
            score -= 5   # weak / oversold

    # slope
    if s20_slope is not None:
        if s20_slope > 0:
            score += 10
        else:
            score -= 10

    # clamp
    score = max(0, min(100, score))

    return score, entry, stop, target


def label_from_score(score: int) -> str:
    if score >= MIN_SCORE_TO_PREBUY:
        return "GO"
    if score >= WATCH_MIN_SCORE:
        return "WATCH"
    return "SKIP"


def chance_from_score(score: int, regime: str) -> int:
    """
    “Slimmer zonder exit aanpassen”:
    - zelfde score, maar kleine boost/penalty per regime.
    """
    chance = score
    reg = (regime or "UNKNOWN").upper()
    if reg == "BULL":
        chance = min(100, chance + 5)
    elif reg == "BEAR":
        chance = max(0, chance - 8)
    elif reg == "RANGE":
        chance = max(0, chance - 2)
    return int(chance)


# ==========================================================
# DEDUP (trade_fingerprints)
# ==========================================================
def make_fingerprint(symbol: str, setup_type: str, entry: float, target: float) -> str:
    # stable formatting so duplicates match
    key = f"{symbol}|{setup_type}|{round_sig(entry, 8)}|{round_sig(target, 8)}"
    return sha1(key)


def fingerprint_recent(conn, schema: SchemaInfo, fp_value: str, cooldown_seconds: int) -> bool:
    """
    Returns True if fingerprint exists and is still in cooldown.
    """
    col = schema.fingerprints_col  # fingerprint OR fp
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT last_created_at
            FROM public.trade_fingerprints
            WHERE {col} = %s
            LIMIT 1
            """,
            (fp_value,),
        )
        row = cur.fetchone()

    if not row:
        return False

    last_ts = row.get("last_created_at")
    if not isinstance(last_ts, datetime):
        return False

    return (now_utc() - last_ts) < timedelta(seconds=cooldown_seconds)


def upsert_fingerprint(conn, schema: SchemaInfo, fp_value: str) -> None:
    col = schema.fingerprints_col
    with conn.cursor() as cur:
        # Keep symbol/setup/entry/target fields if you have them; we only need the fingerprint + last_created_at
        # If your table has extra cols, this still works if they allow NULL.
        cur.execute(
            f"""
            INSERT INTO public.trade_fingerprints ({col}, last_created_at)
            VALUES (%s, NOW())
            ON CONFLICT ({col})
            DO UPDATE SET last_created_at = EXCLUDED.last_created_at
            """,
            (fp_value,),
        )


# ==========================================================
# PREBUY DAILY LIMIT (prebuy_state)
# ==========================================================
def get_day_key() -> str:
    return now_utc().date().isoformat()


def get_created_today(conn, schema: SchemaInfo) -> int:
    day = get_day_key()
    key_col = schema.prebuy_state_key_col
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT created_count FROM public.prebuy_state WHERE {key_col}=%s LIMIT 1",
            (day,),
        )
        row = cur.fetchone()
    if not row:
        return 0
    return safe_int(row.get("created_count"), 0)


def inc_created_today(conn, schema: SchemaInfo, inc: int = 1) -> None:
    day = get_day_key()
    key_col = schema.prebuy_state_key_col
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO public.prebuy_state ({key_col}, created_count, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT ({key_col})
            DO UPDATE SET created_count = public.prebuy_state.created_count + EXCLUDED.created_count,
                          updated_at = NOW()
            """,
            (day, inc),
        )


# ==========================================================
# INSERT pending_approvals (dynamic for missing columns)
# ==========================================================
def insert_pending(conn, schema: SchemaInfo, payload: Dict[str, Any]) -> None:
    """
    Writes into pending_approvals.
    Must not crash if some columns don't exist.
    """
    base_cols = [
        "id", "symbol", "setup_type",
        "regime", "score", "chance", "confidence",
        "entry", "stop", "target",
        "status", "created_at", "expires_at",
    ]
    if schema.pending_has_timeframe:
        base_cols.insert(3, "timeframe")  # after setup_type

    # Build lists
    cols: List[str] = []
    vals: List[Any] = []
    for c in base_cols:
        if c not in payload:
            continue
        cols.append(c)
        vals.append(payload[c])

    placeholders = ", ".join(["%s"] * len(cols))
    col_sql = ", ".join(cols)

    # Upsert on id
    set_sql = ", ".join([f"{c}=EXCLUDED.{c}" for c in cols if c != "id"])

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO public.pending_approvals ({col_sql})
            VALUES ({placeholders})
            ON CONFLICT (id) DO UPDATE SET {set_sql}
            """,
            tuple(vals),
        )


# ==========================================================
# MAIN
# ==========================================================
def main() -> None:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt")

    start = time.time()

    with db_connect() as conn:
        # detect schema once
        schema = detect_schema(conn)

        created_today = get_created_today(conn, schema)
        day = get_day_key()
        log(f"🟣 multi_coin_score | day={day} | created_today={created_today}/{MAX_PREBUY_PER_DAY}")

        # universe
        universe = fetch_binance_usdt_symbols(UNIVERSE_LIMIT)
        log(f"📌 universe={len(universe)} (limit={UNIVERSE_LIMIT}) tf_main={TF_MAIN} tf_ctx={TF_CTX}")

        created = 0
        skipped = 0

        for symbol in universe:
            # Daily cap
            if created_today >= MAX_PREBUY_PER_DAY:
                skipped += 1
                continue

            try:
                # 1) candles
                candles_main = fetch_candles(conn, symbol, TF_MAIN, MIN_CANDLES)
                candles_ctx = fetch_candles(conn, symbol, TF_CTX, MIN_CANDLES)

                if len(candles_main) < MIN_CANDLES or len(candles_ctx) < MIN_CANDLES:
                    skipped += 1
                    continue

                closes_main = [safe_float(c["close"]) for c in candles_main]
                closes_ctx = [safe_float(c["close"]) for c in candles_ctx]

                # 2) regime
                regime, _reg_score = fetch_regime(conn, symbol, TF_MAIN)

                # 3) score
                score, entry, stop, target = compute_score_and_levels(closes_main, closes_ctx)

                # Optional: force test prebuy
                if FORCE_TEST_PREBUY:
                    score = max(score, MIN_SCORE_TO_PREBUY)

                label = label_from_score(score)
                if label == "SKIP":
                    skipped += 1
                    continue

                chance = chance_from_score(score, regime)
                confidence = chance  # simple mapping; you can refine later

                setup_type = "TREND_PULLBACK"  # your current default
                fp_value = make_fingerprint(symbol, setup_type, entry, target)

                # 4) dedup check
                if fingerprint_recent(conn, schema, fp_value, TRADE_COOLDOWN_SECONDS):
                    skipped += 1
                    continue

                # 5) build payload
                prebuy_id = f"PB-{symbol}-{now_utc().strftime('%Y%m%d-%H%M%S')}-{fp_value[:6]}"
                expires_at = now_utc() + timedelta(seconds=PREBUY_VALID_SECONDS)

                payload: Dict[str, Any] = {
                    "id": prebuy_id,
                    "symbol": symbol,
                    "setup_type": setup_type,
                    "timeframe": TF_MAIN,
                    "regime": regime,
                    "score": int(score),
                    "chance": int(chance),
                    "confidence": int(confidence),
                    "entry": float(entry),
                    "stop": float(stop),
                    "target": float(target),
                    "status": "PENDING",
                    "created_at": now_utc(),
                    "expires_at": expires_at,
                }

                # 6) write DB (IMPORTANT: rollback safety)
                try:
                    insert_pending(conn, schema, payload)
                    upsert_fingerprint(conn, schema, fp_value)
                    inc_created_today(conn, schema, 1)
                    conn.commit()
                except Exception as db_e:
                    conn.rollback()
                    skipped += 1
                    log(f"⚠️ skip {symbol} db_error={type(db_e).__name__}: {db_e}")
                    continue

                created += 1
                created_today += 1

                dot = "🟢" if label == "GO" else "🟡"
                log(f"{dot} {symbol}: {label} score={score} chance={chance} regime={regime} id={prebuy_id}")

            except Exception as e:
                # super important: rollback if anything happened in DB transaction
                try:
                    conn.rollback()
                except Exception:
                    pass
                skipped += 1
                log(f"⚠️ skip {symbol} error={type(e).__name__}: {e}")

        elapsed = round(time.time() - start, 1)
        log(f"✅ DONE created={created} skipped={skipped} seconds={elapsed}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("❌ FATAAL in multi_coin_score.py")
        log(str(e))
        log(traceback.format_exc())
        raise
