# analysis/multi_coin_score.py
from __future__ import annotations

import os
import sys
import math
import time
import json
import uuid
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras


# ==========================================================
# CONFIG / ENV
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
EXCHANGE = (os.getenv("EXCHANGE") or "binance").strip()

TF_MAIN = (os.getenv("TF_MAIN") or "4h").strip()  # main signal tf
TF_CTX = (os.getenv("TF_CTX") or "1h").strip()    # context tf

UNIVERSE_LIMIT = int(os.getenv("UNIVERSE_LIMIT") or "250")  # max symbols to analyze
MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "80")  # GO threshold
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "70")          # WATCH threshold

MAX_PREBUY_PER_DAY = int(os.getenv("MAX_PREBUY_PER_DAY") or "5")
PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))
TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS") or str(6 * 60 * 60))

FORCE_TEST_PREBUY = (os.getenv("FORCE_TEST_PREBUY") or "0").strip() == "1"

# Lookback candles (keep modest for efficiency)
LOOKBACK_MAIN = int(os.getenv("LOOKBACK_MAIN") or "320")  # for 4h regime labeler used 320
LOOKBACK_CTX = int(os.getenv("LOOKBACK_CTX") or "240")

# Overextension thresholds
OVEREXT_PENALTY_START = float(os.getenv("OVEREXT_PENALTY_START") or "1.02")  # >2% above MA
OVEREXT_PENALTY_MAX = float(os.getenv("OVEREXT_PENALTY_MAX") or "1.08")      # >8% above MA

# Basic stop/target defaults (bot exit logic stays elsewhere; this is just Pre-BUY proposal)
DEFAULT_STOP_PCT = float(os.getenv("DEFAULT_STOP_PCT") or "0.02")  # 2% stop below entry
DEFAULT_R_MULT = float(os.getenv("DEFAULT_R_MULT") or "2.0")       # 2R target

# If you want to “always store WATCH for learning”, keep True
STORE_WATCH_ROWS = (os.getenv("STORE_WATCH_ROWS") or "1").strip() == "1"

# ==========================================================
# LOGGING
# ==========================================================
def log(msg: str) -> None:
    print(msg, flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_utc_str() -> str:
    return now_utc().strftime("%Y-%m-%d")


# ==========================================================
# DB CONNECT
# ==========================================================
def db_connect():
    # Render Postgres: sslmode=require
    return psycopg2.connect(DATABASE_URL, sslmode="require")


# ==========================================================
# SMALL MATH HELPERS (no numpy to keep deps light)
# ==========================================================
def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / float(period)


def slope(values: List[float], n: int = 10) -> Optional[float]:
    """
    Simple slope approximation: (last - first)/n on last n points.
    """
    if len(values) < n + 1:
        return None
    first = values[-(n + 1)]
    last = values[-1]
    return (last - first) / float(n)


def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = values[i] - values[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses += abs(diff)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ==========================================================
# DATA MODELS
# ==========================================================
@dataclass
class RegimeInfo:
    regime: str   # BULL/BEAR/RANGE
    score: int
    asof_ts: datetime


# ==========================================================
# SCHEMA INTROSPECTION (robust inserts)
# ==========================================================
def get_table_columns(conn, table: str, schema: str = "public") -> List[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        return [r[0] for r in cur.fetchall()]


# ==========================================================
# UNIVERSE
# ==========================================================
def fetch_universe_symbols(conn, limit: int) -> List[str]:
    """
    Pull symbols from candles table.
    You can later replace this with an "auto_universe" table or exchange metadata.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT symbol
            FROM public.candles
            WHERE exchange = %s
            ORDER BY symbol ASC
            LIMIT %s
            """,
            (EXCHANGE, limit),
        )
        rows = cur.fetchall()
    return [r[0] for r in rows]


# ==========================================================
# MARKET REGIME CACHE (single query)
# ==========================================================
def fetch_regime_map(conn, timeframe: str = "4h") -> Dict[str, RegimeInfo]:
    """
    Build {symbol -> RegimeInfo} from market_regime latest asof_ts.
    """
    out: Dict[str, RegimeInfo] = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (symbol)
              symbol, regime, score, asof_ts
            FROM public.market_regime
            WHERE exchange = %s AND timeframe = %s
            ORDER BY symbol, asof_ts DESC
            """,
            (EXCHANGE, timeframe),
        )
        rows = cur.fetchall()
    for r in rows:
        try:
            out[str(r["symbol"])] = RegimeInfo(
                regime=str(r["regime"] or "RANGE").upper(),
                score=int(r["score"] or 0),
                asof_ts=r["asof_ts"],
            )
        except Exception:
            continue
    return out


# ==========================================================
# CANDLES FETCH (tight query: only what we need)
# ==========================================================
def fetch_closes(conn, symbol: str, timeframe: str, lookback: int) -> Tuple[List[float], Optional[datetime]]:
    """
    Returns closes in chronological order + latest open_time asof.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT open_time, close
            FROM public.candles
            WHERE exchange = %s AND symbol = %s AND timeframe = %s
            ORDER BY open_time DESC
            LIMIT %s
            """,
            (EXCHANGE, symbol, timeframe, lookback),
        )
        rows = cur.fetchall()

    if not rows:
        return [], None

    # rows are desc; reverse to chronological
    rows_rev = list(reversed(rows))
    closes = [float(r[1]) for r in rows_rev if r[1] is not None]
    asof_ts = rows_rev[-1][0] if rows_rev[-1][0] is not None else None
    return closes, asof_ts


# ==========================================================
# FINGERPRINT DEDUP (no duplicate trades)
# ==========================================================
def ensure_fingerprint_table(conn) -> None:
    """
    Safe create-if-not-exists (won't fail if table already exists).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.trade_fingerprints (
              fingerprint TEXT PRIMARY KEY,
              last_created_at TIMESTAMPTZ,
              created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )
    conn.commit()


def fingerprint_for(symbol: str, setup_type: str, entry: float, target: float) -> str:
    return f"{EXCHANGE}|{symbol}|{setup_type}|{round(entry,8)}|{round(target,8)}"


def is_duplicate_fingerprint(conn, fp: str, cooldown_seconds: int) -> bool:
    """
    True if fingerprint exists and is within cooldown window.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT last_created_at FROM public.trade_fingerprints WHERE fingerprint=%s",
            (fp,),
        )
        row = cur.fetchone()
    if not row or not row.get("last_created_at"):
        return False

    last_ts = row["last_created_at"]
    if isinstance(last_ts, datetime):
        age = (now_utc() - last_ts).total_seconds()
        return age < cooldown_seconds
    return False


def upsert_fingerprint(conn, fp: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.trade_fingerprints (fingerprint, last_created_at)
            VALUES (%s, NOW())
            ON CONFLICT (fingerprint)
            DO UPDATE SET last_created_at=EXCLUDED.last_created_at
            """,
            (fp,),
        )


# ==========================================================
# PREBUY STATE (daily cap)
# ==========================================================
def ensure_prebuy_state_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.prebuy_state (
              day TEXT PRIMARY KEY,
              created_count INTEGER DEFAULT 0,
              updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )
    conn.commit()


def get_created_today(conn) -> int:
    day = today_utc_str()
    with conn.cursor() as cur:
        cur.execute("SELECT created_count FROM public.prebuy_state WHERE day=%s", (day,))
        row = cur.fetchone()
    if not row:
        return 0
    return int(row[0] or 0)


def inc_created_today(conn, n: int = 1) -> None:
    day = today_utc_str()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.prebuy_state (day, created_count, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (day)
            DO UPDATE SET created_count = public.prebuy_state.created_count + EXCLUDED.created_count,
                          updated_at = NOW()
            """,
            (day, n),
        )


# ==========================================================
# SMARTER SCORING (win% ↑ without touching exits)
# ==========================================================
def compute_overextension_penalty(price: float, ma: Optional[float]) -> float:
    """
    Returns penalty factor in [0..1]. 1 = no penalty.
    If price is far above MA, chances of pullback increase -> lower chance/score.
    """
    if ma is None or ma <= 0:
        return 1.0
    ratio = price / ma
    if ratio <= OVEREXT_PENALTY_START:
        return 1.0
    if ratio >= OVEREXT_PENALTY_MAX:
        return 0.6  # heavy penalty
    # linear penalty between start and max
    t = (ratio - OVEREXT_PENALTY_START) / (OVEREXT_PENALTY_MAX - OVEREXT_PENALTY_START)
    return 1.0 - 0.4 * clamp(t, 0.0, 1.0)  # down to 0.6


def base_score_from_indicators(closes_main: List[float], closes_ctx: List[float]) -> Tuple[int, Dict[str, Any]]:
    """
    Produce a 0-100 score + debug info.
    Keeps it simple but meaningful.
    """
    info: Dict[str, Any] = {}

    if len(closes_main) < 60 or len(closes_ctx) < 60:
        return 0, {"reason": "not_enough_candles"}

    price = closes_main[-1]
    sma20 = sma(closes_main, 20)
    sma50 = sma(closes_main, 50)
    rsi14 = rsi(closes_ctx, 14)
    slp = slope(closes_main, 10)

    info.update({"price": price, "sma20": sma20, "sma50": sma50, "rsi14": rsi14, "slope10": slp})

    score = 50

    # Trend bias (main TF)
    if sma20 and sma50:
        if sma20 > sma50:
            score += 15
        else:
            score -= 10

    # Price position vs sma20 (main TF)
    if sma20:
        if price > sma20:
            score += 10
        else:
            score -= 8

    # Slope (momentum)
    if slp is not None:
        if slp > 0:
            score += 8
        else:
            score -= 6

    # RSI (context TF)
    if rsi14 is not None:
        if 45 <= rsi14 <= 65:
            score += 10  # healthy zone
        elif rsi14 < 35:
            score -= 8   # weak / falling knife risk
        elif rsi14 > 75:
            score -= 10  # too hot -> pullback risk

    # Overextension penalty
    penalty = compute_overextension_penalty(price, sma20)
    score = int(round(score * penalty))

    score = int(clamp(float(score), 0.0, 100.0))
    info["overext_penalty"] = penalty
    info["score_raw"] = score
    return score, info


def regime_adjust(score: int, regime: str) -> Tuple[int, int]:
    """
    Adjust score and return (adj_score, chance).
    - In BEAR: long setups are harder -> reduce
    - In BULL: slightly boost
    - In RANGE: neutral
    """
    r = (regime or "RANGE").upper()
    adj = score

    if r == "BEAR":
        adj = int(round(score * 0.85))
    elif r == "BULL":
        adj = int(round(score * 1.05))
    else:
        adj = score

    adj = int(clamp(float(adj), 0.0, 100.0))

    # chance: map score -> probability-ish (still heuristic)
    # Keep conservative.
    chance = int(clamp(adj, 0, 100))
    return adj, chance


def confidence_from_signals(info: Dict[str, Any]) -> int:
    """
    Simple confidence based on consistency.
    """
    conf = 50

    price = float(info.get("price") or 0)
    sma20 = info.get("sma20")
    sma50 = info.get("sma50")
    rsi14 = info.get("rsi14")
    slp = info.get("slope10")
    pen = float(info.get("overext_penalty") or 1.0)

    # alignment checks
    if sma20 and sma50 and sma20 > sma50:
        conf += 10
    if sma20 and price > sma20:
        conf += 8
    if slp is not None and slp > 0:
        conf += 6
    if rsi14 is not None and 45 <= rsi14 <= 65:
        conf += 8

    # penalty reduces confidence
    if pen < 0.9:
        conf -= 10

    return int(clamp(conf, 0, 100))


# ==========================================================
# PENDING APPROVALS INSERT (robust columns + SAVEPOINT-safe)
# ==========================================================
def build_prebuy_id(symbol: str) -> str:
    # stable, human-readable
    ts = now_utc().strftime("%Y%m%d-%H%M%S")
    short = uuid.uuid4().hex[:6]
    return f"PB-{symbol}-{ts}-{short}"


def insert_pending(conn, payload: Dict[str, Any]) -> None:
    """
    Insert into public.pending_approvals with only columns that exist.
    This prevents schema mismatch crashes.
    """
    cols = payload.keys()
    table_cols = set(get_table_columns(conn, "pending_approvals", "public"))

    use_cols = [c for c in cols if c in table_cols]
    if not use_cols:
        raise RuntimeError("pending_approvals table has no matching columns for payload")

    placeholders = ", ".join([f"%({c})s" for c in use_cols])
    col_list = ", ".join(use_cols)

    # if your table has id primary key: upsert on id (safe)
    sql = f"""
    INSERT INTO public.pending_approvals ({col_list})
    VALUES ({placeholders})
    ON CONFLICT (id) DO NOTHING
    """
    with conn.cursor() as cur:
        cur.execute(sql, payload)


# ==========================================================
# MAIN LOOP (transaction-safe + efficient)
# ==========================================================
def main() -> int:
    if not DATABASE_URL:
        log("❌ DATABASE_URL ontbreekt.")
        return 2

    t0 = time.time()

    with db_connect() as conn:
        conn.autocommit = False  # we control transaction

        # ensure helper tables exist
        ensure_fingerprint_table(conn)
        ensure_prebuy_state_table(conn)

        created_today = get_created_today(conn)
        log(f"💡 multi_coin_score | day={today_utc_str()} | created_today={created_today}/{MAX_PREBUY_PER_DAY}")

        # Universe once
        universe = fetch_universe_symbols(conn, UNIVERSE_LIMIT)
        log(f"📌 universe={len(universe)} (limit={UNIVERSE_LIMIT}) tf_main={TF_MAIN} tf_ctx={TF_CTX}")

        # Regime cache once (fast)
        regime_map = fetch_regime_map(conn, timeframe=TF_MAIN)

        created = 0
        skipped = 0

        for sym in universe:
            if created_today + created >= MAX_PREBUY_PER_DAY and not FORCE_TEST_PREBUY:
                break

            sp_name = f"sp_{sym.replace('-', '_')}"
            try:
                # SAVEPOINT => 1 coin fail won't poison whole run
                with conn.cursor() as cur:
                    cur.execute(f"SAVEPOINT {sp_name}")

                closes_main, asof_main = fetch_closes(conn, sym, TF_MAIN, LOOKBACK_MAIN)
                closes_ctx, asof_ctx = fetch_closes(conn, sym, TF_CTX, LOOKBACK_CTX)

                if not closes_main or not closes_ctx or asof_main is None:
                    skipped += 1
                    with conn.cursor() as cur:
                        cur.execute(f"RELEASE SAVEPOINT {sp_name}")
                    continue

                # basic indicators -> score
                score_raw, info = base_score_from_indicators(closes_main, closes_ctx)

                # regime (fallback RANGE)
                reg = regime_map.get(sym)
                regime = (reg.regime if reg else "RANGE")
                adj_score, chance = regime_adjust(score_raw, regime)
                confidence = confidence_from_signals(info)

                # classify
                label = "NO"
                if FORCE_TEST_PREBUY:
                    label = "GO"
                    adj_score = max(adj_score, MIN_SCORE_TO_PREBUY)
                    chance = max(chance, MIN_SCORE_TO_PREBUY)
                elif adj_score >= MIN_SCORE_TO_PREBUY:
                    label = "GO"
                elif adj_score >= WATCH_MIN_SCORE:
                    label = "WATCH"

                # Decide whether to store
                if label == "NO":
                    skipped += 1
                    with conn.cursor() as cur:
                        cur.execute(f"RELEASE SAVEPOINT {sp_name}")
                    continue

                # entry/stop/target (proposal only)
                entry = float(closes_ctx[-1])
                stop = float(entry * (1.0 - DEFAULT_STOP_PCT))
                risk = entry - stop
                target = float(entry + DEFAULT_R_MULT * risk)

                setup_type = "TREND_PULLBACK"  # keep as your primary setup
                fp = fingerprint_for(sym, setup_type, entry, target)

                # Dedup: do NOT create same trade again
                if is_duplicate_fingerprint(conn, fp, TRADE_COOLDOWN_SECONDS) and not FORCE_TEST_PREBUY:
                    skipped += 1
                    with conn.cursor() as cur:
                        cur.execute(f"RELEASE SAVEPOINT {sp_name}")
                    continue

                # Build payload for pending_approvals
                prebuy_id = build_prebuy_id(sym)
                payload: Dict[str, Any] = {
                    "id": prebuy_id,
                    "exchange": EXCHANGE,
                    "symbol": sym,
                    "setup_type": setup_type,
                    "timeframe": TF_MAIN,  # <--- this column caused your earlier crash when missing
                    "regime": regime,
                    "score": int(adj_score),
                    "chance": int(chance),
                    "confidence": int(confidence),
                    "entry": float(entry),
                    "stop": float(stop),
                    "target": float(target),
                    "status": ("PENDING" if label == "GO" else "WATCH"),
                    "created_at": now_utc(),
                    "expires_at": (now_utc() + timedelta(seconds=PREBUY_VALID_SECONDS)),
                }

                # Insert GO rows; WATCH rows optionally stored too
                if label == "GO" or STORE_WATCH_ROWS:
                    insert_pending(conn, payload)
                    upsert_fingerprint(conn, fp)

                    if label == "GO":
                        inc_created_today(conn, 1)
                        created += 1

                # release savepoint + commit per successful symbol
                with conn.cursor() as cur:
                    cur.execute(f"RELEASE SAVEPOINT {sp_name}")
                conn.commit()

                if label == "GO":
                    log(f"✅ {sym}: GO score={adj_score} chance={chance} regime={regime} id={prebuy_id}")
                else:
                    log(f"🟡 {sym}: WATCH score={adj_score} chance={chance} regime={regime} id={prebuy_id}")

            except Exception as e:
                # Rollback to savepoint so the transaction continues clean
                try:
                    with conn.cursor() as cur:
                        cur.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                        cur.execute(f"RELEASE SAVEPOINT {sp_name}")
                    conn.commit()
                except Exception:
                    # If even savepoint rollback fails, do full rollback then continue
                    conn.rollback()

                skipped += 1
                log(f"⚠️ skip {sym} error={type(e).__name__}: {e}")

        dt = time.time() - t0
        log(f"✅ DONE created={created} skipped={skipped} seconds={dt:.1f}")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        log("❌ FATAAL in multi_coin_score.py")
        log(traceback.format_exc())
        raise
