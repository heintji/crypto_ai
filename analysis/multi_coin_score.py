# analysis/multi_coin_score.py
from __future__ import annotations

import os
import math
import time
import hashlib
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple, Set

import psycopg2
import psycopg2.extras
import requests

# ==========================================================
# CONFIG (ENV)
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
EXCHANGE = (os.getenv("EXCHANGE") or "binance").strip().lower()

UNIVERSE_LIMIT = int(os.getenv("UNIVERSE_LIMIT") or "250")

TF_MAIN = (os.getenv("TF_MAIN") or "4h").strip()
TF_CTX = (os.getenv("TF_CTX") or "1h").strip()

# ✅ agressiever zoeken, maar spam voorkomen via MAX_ACTIVE_PREBUYS
MAX_PREBUY_PER_DAY = int(os.getenv("MAX_PREBUY_PER_DAY") or "50")     # mag hoog
MAX_ACTIVE_PREBUYS = int(os.getenv("MAX_ACTIVE_PREBUYS") or "10")     # ✅ jouw wens: max 10 tegelijk

MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "78")
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "68")

# ✅ WATCH wel leren, maar niet in pending tenzij jij dat wil
INCLUDE_WATCH_IN_PENDING = (os.getenv("INCLUDE_WATCH_IN_PENDING") or "0").strip() == "1"

PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))
TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS") or str(6 * 60 * 60))

MIN_CANDLES = int(os.getenv("MIN_CANDLES") or "120")
REGIME_CANDLES = int(os.getenv("REGIME_CANDLES") or "300")

FORCE_TEST_PREBUY = (os.getenv("FORCE_TEST_PREBUY") or "0").strip() == "1"
SCOREBOARD_REFRESH = (os.getenv("SCOREBOARD_REFRESH") or "1").strip() == "1"

# ==========================================================
# HELPERS
# ==========================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(msg, flush=True)


def db_connect():
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
        if sym.endswith(("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")):
            continue
        syms.append(sym)

    syms = sorted(set(syms))
    return syms[:limit]


# ==========================================================
# INDICATORS
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
    return (values[-1] - values[-(lookback + 1)]) / float(lookback)


# ==========================================================
# REGIME
# ==========================================================
def detect_regime_4h(closes_4h: List[float]) -> str:
    if len(closes_4h) < 205:
        return "RANGE"

    ma200_now = sum(closes_4h[-200:]) / 200
    ma200_prev = sum(closes_4h[-205:-5]) / 200
    price = closes_4h[-1]
    slope_up = ma200_now > ma200_prev

    if price > ma200_now and slope_up:
        return "BULL"
    if price < ma200_now and not slope_up:
        return "BEAR"
    return "RANGE"


def normalize_score(raw_score: float, regime: str) -> int:
    reg = (regime or "RANGE").upper()
    if reg == "BEAR":
        raw_score -= 6
    elif reg == "RANGE":
        raw_score -= 3
    raw_score = max(0, min(100, raw_score))
    return int(round(raw_score))


# ==========================================================
# DB SCHEMA DETECTION
# ==========================================================
@dataclass
class SchemaInfo:
    fingerprints_pk_col: str
    pending_has_timeframe: bool
    pending_has_raw_score: bool
    prebuy_state_key_col: str
    has_experience_trades: bool
    has_experience_scoreboard: bool


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


def table_exists(conn, table: str, schema: str = "public") -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS(
              SELECT 1
              FROM information_schema.tables
              WHERE table_schema=%s AND table_name=%s
            )
            """,
            (schema, table),
        )
        return bool(cur.fetchone()[0])


def detect_trade_fingerprints_pk_col(conn) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
            WHERE n.nspname = 'public'
              AND c.relname = 'trade_fingerprints'
              AND i.indisprimary
            LIMIT 1;
            """
        )
        row = cur.fetchone()

    if row and row[0]:
        return str(row[0])

    if table_has_column(conn, "trade_fingerprints", "fingerprint"):
        return "fingerprint"
    if table_has_column(conn, "trade_fingerprints", "fp"):
        return "fp"

    with conn.cursor() as cur:
        cur.execute("ALTER TABLE public.trade_fingerprints ADD COLUMN IF NOT EXISTS fingerprint TEXT;")
    conn.commit()
    return "fingerprint"


def detect_schema(conn) -> SchemaInfo:
    pending_has_timeframe = table_has_column(conn, "pending_approvals", "timeframe")
    pending_has_raw_score = table_has_column(conn, "pending_approvals", "raw_score")

    if table_has_column(conn, "prebuy_state", "day"):
        key_col = "day"
    elif table_has_column(conn, "prebuy_state", "key"):
        key_col = "key"
    else:
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

    pk_col = detect_trade_fingerprints_pk_col(conn)

    has_exp_trades = table_exists(conn, "experience_trades")
    has_exp_score = table_exists(conn, "experience_scoreboard")

    return SchemaInfo(
        fingerprints_pk_col=pk_col,
        pending_has_timeframe=pending_has_timeframe,
        pending_has_raw_score=pending_has_raw_score,
        prebuy_state_key_col=key_col,
        has_experience_trades=has_exp_trades,
        has_experience_scoreboard=has_exp_score,
    )


# ==========================================================
# DB READ: candles
# ==========================================================
def fetch_candles(conn, symbol: str, timeframe: str, limit: int) -> List[Dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT open_time, open, high, low, close, volume
            FROM public.candles
            WHERE exchange=%s AND symbol=%s AND timeframe=%s
            ORDER BY open_time DESC
            LIMIT %s
            """,
            (EXCHANGE, symbol, timeframe, limit),
        )
        rows = cur.fetchall()

    data = [dict(r) for r in rows]
    data.reverse()
    return data


def fetch_regime_closes_4h(conn, symbol: str) -> List[float]:
    candles_4h = fetch_candles(conn, symbol, "4h", REGIME_CANDLES)
    return [safe_float(c["close"]) for c in candles_4h]


# ==========================================================
# SCORING
# ==========================================================
def compute_score_and_levels(closes_main: List[float], closes_ctx: List[float]) -> Tuple[int, float, float, float]:
    entry = float(closes_main[-1])
    stop = float(entry * 0.98)
    r = entry - stop
    target = float(entry + 2.0 * r)

    sma20 = sma(closes_main, 20)
    sma50 = sma(closes_main, 50)
    rsi14 = rsi(closes_ctx, 14)
    s20_slope = slope(closes_main, 10)

    score = 50

    if sma20 is not None and sma50 is not None:
        score += 15 if sma20 > sma50 else -10

    if rsi14 is not None:
        if 45 <= rsi14 <= 65:
            score += 10
        elif rsi14 > 70:
            score -= 10
        elif rsi14 < 35:
            score -= 5

    if s20_slope is not None:
        score += 10 if s20_slope > 0 else -10

    score = max(0, min(100, score))
    return int(score), entry, stop, target


def label_from_score(score: int) -> str:
    if score >= MIN_SCORE_TO_PREBUY:
        return "GO"
    if score >= WATCH_MIN_SCORE:
        return "WATCH"
    return "SKIP"


def chance_from_score(score: int, regime: str) -> int:
    chance = int(score)
    reg = (regime or "RANGE").upper()
    if reg == "BULL":
        chance = min(100, chance + 3)
    elif reg == "BEAR":
        chance = max(0, chance - 3)
    return int(chance)


# ==========================================================
# DEDUP
# ==========================================================
def make_fingerprint(symbol: str, setup_type: str, entry: float, target: float) -> str:
    key = f"{symbol}|{setup_type}|{round_sig(entry, 8)}|{round_sig(target, 8)}"
    return sha1(key)


def fingerprint_recent(conn, schema: SchemaInfo, fp_value: str, cooldown_seconds: int) -> bool:
    if not fp_value:
        return False

    col = schema.fingerprints_pk_col
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
    if not fp_value:
        return

    col = schema.fingerprints_pk_col
    with conn.cursor() as cur:
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
# DAILY LIMIT
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
# ACTIVE CAP (anti spam)
# ==========================================================
def count_active_prebuys(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)::int
            FROM public.pending_approvals
            WHERE COALESCE(status,'PENDING') IN ('PENDING','APPROVED')
              AND (expires_at IS NULL OR expires_at > NOW())
            """
        )
        return int(cur.fetchone()[0] or 0)


# ==========================================================
# INSERT pending_approvals
# ==========================================================
def insert_pending(conn, schema: SchemaInfo, payload: Dict[str, Any]) -> None:
    base_cols = [
        "id", "symbol", "setup_type",
        "regime", "score", "chance", "confidence",
        "entry", "stop", "target",
        "status", "created_at", "expires_at",
    ]

    if schema.pending_has_timeframe:
        base_cols.insert(3, "timeframe")

    if schema.pending_has_raw_score and "raw_score" in payload:
        if "raw_score" not in base_cols:
            base_cols.insert(base_cols.index("score"), "raw_score")

    cols: List[str] = []
    vals: List[Any] = []
    for c in base_cols:
        if c in payload:
            cols.append(c)
            vals.append(payload[c])

    placeholders = ", ".join(["%s"] * len(cols))
    col_sql = ", ".join(cols)
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
# EXPERIENCE (optioneel)
# ==========================================================
def insert_experience_trade(conn, payload: Dict[str, Any]) -> None:
    if not payload:
        return

    cols = [
        "id", "exchange", "symbol", "timeframe", "setup_type", "regime",
        "label", "score", "raw_score", "chance", "confidence",
        "entry", "stop", "target",
        "outcome", "is_shadow", "created_at",
    ]

    use_cols: List[str] = []
    use_vals: List[Any] = []
    for c in cols:
        if c in payload and payload[c] is not None:
            use_cols.append(c)
            use_vals.append(payload[c])

    if not use_cols:
        return

    placeholders = ", ".join(["%s"] * len(use_cols))
    col_sql = ", ".join(use_cols)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO public.experience_trades ({col_sql})
            VALUES ({placeholders})
            ON CONFLICT (id) DO NOTHING
            """,
            tuple(use_vals),
        )


# ==========================================================
# MAIN
# ==========================================================
def main() -> None:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt")

    start = time.time()

    with db_connect() as conn:
        schema = detect_schema(conn)

        created_today = get_created_today(conn, schema)
        day = get_day_key()
        active_now = count_active_prebuys(conn)

        log(f"🟣 multi_coin_score | day={day} | created_today={created_today}/{MAX_PREBUY_PER_DAY}")
        log(f"🧯 ACTIVE CAP | active_now={active_now}/{MAX_ACTIVE_PREBUYS}")
        log(f"🔧 trade_fingerprints PK column = {schema.fingerprints_pk_col}")
        log(f"⚙️ INCLUDE_WATCH_IN_PENDING={INCLUDE_WATCH_IN_PENDING}")

        universe = fetch_binance_usdt_symbols(UNIVERSE_LIMIT)
        log(f"📌 universe={len(universe)} (limit={UNIVERSE_LIMIT}) tf_main={TF_MAIN} tf_ctx={TF_CTX}")

        created = 0
        skipped = 0
        candidates = 0

        for symbol in universe:
            # 1) daily cap
            if created_today >= MAX_PREBUY_PER_DAY:
                skipped += 1
                continue

            # 2) active cap (anti spam)
            active_now = count_active_prebuys(conn)
            if active_now >= MAX_ACTIVE_PREBUYS:
                log(f"🛑 STOP: active_now={active_now} >= MAX_ACTIVE_PREBUYS={MAX_ACTIVE_PREBUYS}")
                break

            try:
                candles_main = fetch_candles(conn, symbol, TF_MAIN, MIN_CANDLES)
                candles_ctx = fetch_candles(conn, symbol, TF_CTX, MIN_CANDLES)

                if len(candles_main) < MIN_CANDLES or len(candles_ctx) < MIN_CANDLES:
                    skipped += 1
                    continue

                closes_main = [safe_float(c["close"]) for c in candles_main]
                closes_ctx = [safe_float(c["close"]) for c in candles_ctx]

                closes_4h = fetch_regime_closes_4h(conn, symbol)
                regime = detect_regime_4h(closes_4h)

                raw_score, entry, stop, target = compute_score_and_levels(closes_main, closes_ctx)
                score = normalize_score(raw_score, regime)

                if FORCE_TEST_PREBUY:
                    score = max(score, MIN_SCORE_TO_PREBUY)

                label = label_from_score(score)
                if label == "SKIP":
                    skipped += 1
                    continue

                # ✅ alleen GO naar pending, WATCH alleen als jij INCLUDE_WATCH_IN_PENDING aanzet
                if label == "WATCH" and not INCLUDE_WATCH_IN_PENDING:
                    # wel tellen als candidate (voor logging)
                    candidates += 1
                    skipped += 1
                    continue

                candidates += 1
                chance = chance_from_score(score, regime)
                confidence = chance

                setup_type = "TREND_PULLBACK"
                fp_value = make_fingerprint(symbol, setup_type, entry, target)

                if fingerprint_recent(conn, schema, fp_value, TRADE_COOLDOWN_SECONDS):
                    skipped += 1
                    continue

                created_at = now_utc()
                expires_at = created_at + timedelta(seconds=PREBUY_VALID_SECONDS)
                prebuy_id = f"PB-{symbol}-{created_at.strftime('%Y%m%d-%H%M%S')}-{fp_value[:6]}"

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
                    "created_at": created_at,
                    "expires_at": expires_at,
                }
                if schema.pending_has_raw_score:
                    payload["raw_score"] = int(raw_score)

                exp_payload: Dict[str, Any] = {
                    "id": prebuy_id,
                    "exchange": EXCHANGE,
                    "symbol": symbol,
                    "timeframe": TF_MAIN,
                    "setup_type": setup_type,
                    "regime": regime,
                    "label": label,
                    "score": int(score),
                    "raw_score": int(raw_score),
                    "chance": int(chance),
                    "confidence": int(confidence),
                    "entry": float(entry),
                    "stop": float(stop),
                    "target": float(target),
                    "outcome": "UNKNOWN",
                    "is_shadow": True,
                    "created_at": created_at,
                }

                try:
                    insert_pending(conn, schema, payload)
                    upsert_fingerprint(conn, schema, fp_value)
                    inc_created_today(conn, schema, 1)

                    if schema.has_experience_trades:
                        insert_experience_trade(conn, exp_payload)

                    conn.commit()
                except Exception as db_e:
                    conn.rollback()
                    skipped += 1
                    log(f"⚠️ skip {symbol} db_error={type(db_e).__name__}: {db_e}")
                    continue

                created += 1
                created_today += 1

                dot = "🟢" if label == "GO" else "🟡"
                log(f"{dot} CREATED {symbol} {label} score={score} chance={chance} regime={regime} id={prebuy_id}")

            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                skipped += 1
                log(f"⚠️ skip {symbol} error={type(e).__name__}: {e}")

        elapsed = round(time.time() - start, 1)
        log(f"✅ DONE created={created} candidates={candidates} scanned={len(universe)} skipped={skipped} seconds={elapsed}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("❌ FATAAL in multi_coin_score.py")
        log(str(e))
        log(traceback.format_exc())
        raise
