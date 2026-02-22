# analysis/multi_coin_score.py
from __future__ import annotations

import os
import math
import json
import hashlib
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

# ==========================================================
# ENV
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

EXCHANGE = (os.getenv("EXCHANGE") or "binance").strip().lower()

# Timeframes used by your data layer
TF_MAIN = (os.getenv("TF_MAIN") or "4h").strip()
TF_CTX = (os.getenv("TF_CTX") or "1h").strip()

# Universe control
AUTO_UNIVERSE = (os.getenv("AUTO_UNIVERSE") or "1").strip() == "1"
UNIVERSE_LIMIT = int(os.getenv("UNIVERSE_LIMIT") or "250")  # hard cap
UNIVERSE_MIN_RECENT_DAYS = int(os.getenv("UNIVERSE_MIN_RECENT_DAYS") or "7")

# Pre-BUY gating
MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "80")      # GO
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "70")              # WATCH
MAX_PREBUY_PER_DAY = int(os.getenv("MAX_PREBUY_PER_DAY") or "5")
PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))
TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS") or str(6 * 60 * 60))

# Lookback
LOOKBACK_MAIN = int(os.getenv("LOOKBACK_MAIN") or "320")  # enough for 4h indicators
LOOKBACK_CTX = int(os.getenv("LOOKBACK_CTX") or "200")

# Filters that increase win% WITHOUT touching exit logic
MIN_AVG_QUOTE_VOL = float(os.getenv("MIN_AVG_QUOTE_VOL") or "200000")  # liquidity floor (quote_volume avg)
MAX_OVEREXT_PCT = float(os.getenv("MAX_OVEREXT_PCT") or "6.0")         # avoid already-overextended moves
MAX_VOLATILITY = float(os.getenv("MAX_VOLATILITY") or "0.08")          # avoid ultra wild (std returns)
MIN_VOLATILITY = float(os.getenv("MIN_VOLATILITY") or "0.005")         # avoid dead pairs

# ==========================================================
# UTIL
# ==========================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def log(msg: str) -> None:
    print(msg, flush=True)

def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing")
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

# ==========================================================
# INDICATORS
# ==========================================================
def sma(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / float(n)

def rsi(values: List[float], n: int = 14) -> Optional[float]:
    if len(values) < n + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-n, 0):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += abs(diff)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))

def returns_std(values: List[float], n: int = 50) -> Optional[float]:
    if len(values) < n + 1:
        return None
    rets = []
    for i in range(-n, 0):
        prev = values[i - 1]
        cur = values[i]
        if prev <= 0:
            continue
        rets.append((cur / prev) - 1.0)
    if len(rets) < max(10, n // 3):
        return None
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / len(rets)
    return math.sqrt(var)

# ==========================================================
# DATA ACCESS
# ==========================================================
def get_table_columns(conn, table: str, schema: str = "public") -> List[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s
            """,
            (schema, table),
        )
        return [r[0] for r in cur.fetchall()]

def fetch_universe(conn) -> List[str]:
    """
    Auto universe from candles table: symbols that have data in last N days for TF_MAIN.
    """
    if not AUTO_UNIVERSE:
        raw = (os.getenv("UNIVERSE") or "").strip()
        if not raw:
            return []
        return [s.strip().upper() for s in raw.split(",") if s.strip()]

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT symbol
            FROM public.candles
            WHERE exchange=%s
              AND timeframe=%s
              AND open_time >= (NOW() - INTERVAL '{UNIVERSE_MIN_RECENT_DAYS} days')
            ORDER BY symbol
            LIMIT %s
            """,
            (EXCHANGE, TF_MAIN, UNIVERSE_LIMIT),
        )
        return [r[0] for r in cur.fetchall()]

def fetch_candles(conn, symbol: str, timeframe: str, lookback: int) -> List[Dict[str, Any]]:
    """
    Returns candles oldest->newest, limited by lookback.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT open_time, open, high, low, close, quote_volume
            FROM public.candles
            WHERE exchange=%s AND symbol=%s AND timeframe=%s
            ORDER BY open_time DESC
            LIMIT %s
            """,
            (EXCHANGE, symbol, timeframe, lookback),
        )
        rows = cur.fetchall()
    rows = list(reversed(rows))
    return [dict(r) for r in rows]

def fetch_latest_regimes(conn, symbols: List[str], timeframe: str) -> Dict[str, Dict[str, Any]]:
    """
    Efficient: one query to get latest regime per symbol.
    """
    if not symbols:
        return {}

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (symbol)
              symbol, regime, score, asof_ts
            FROM public.market_regime
            WHERE exchange=%s AND timeframe=%s AND symbol = ANY(%s)
            ORDER BY symbol, asof_ts DESC
            """,
            (EXCHANGE, timeframe, symbols),
        )
        rows = cur.fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        out[r["symbol"]] = dict(r)
    return out

# ==========================================================
# DEDUP (fingerprints)
# ==========================================================
def ensure_fingerprint_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.trade_fingerprints (
              fingerprint TEXT PRIMARY KEY,
              last_created_at TIMESTAMPTZ
            );
            """
        )
    conn.commit()

def fingerprint_exists_recent(conn, fp: str, cooldown_seconds: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT last_created_at
            FROM public.trade_fingerprints
            WHERE fingerprint=%s
            """,
            (fp,),
        )
        row = cur.fetchone()
    if not row or not row[0]:
        return False
    last: datetime = row[0]
    return (now_utc() - last).total_seconds() < cooldown_seconds

def upsert_fingerprint(conn, fp: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.trade_fingerprints(fingerprint, last_created_at)
            VALUES (%s, NOW())
            ON CONFLICT (fingerprint)
            DO UPDATE SET last_created_at=EXCLUDED.last_created_at
            """,
            (fp,),
        )
    conn.commit()

# ==========================================================
# DAILY LIMIT STATE (DB)
# ==========================================================
def ensure_prebuy_state(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.prebuy_state (
              day DATE PRIMARY KEY,
              created_count INTEGER NOT NULL DEFAULT 0,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
    conn.commit()

def get_created_today(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT created_count FROM public.prebuy_state WHERE day=CURRENT_DATE")
        row = cur.fetchone()
    return int(row[0]) if row else 0

def bump_created_today(conn, n: int = 1) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.prebuy_state(day, created_count)
            VALUES (CURRENT_DATE, %s)
            ON CONFLICT (day)
            DO UPDATE SET created_count = public.prebuy_state.created_count + EXCLUDED.created_count,
                         updated_at=NOW()
            """,
            (n,),
        )
    conn.commit()

# ==========================================================
# SCORING / SETUP
# ==========================================================
@dataclass
class Signal:
    symbol: str
    setup_type: str
    why: str
    timeframe: str
    regime: str
    score: int
    chance: int
    confidence: int
    entry: float
    stop: float
    target: float
    expires_at: datetime
    meta: Dict[str, Any]

def classify_setup(closes: List[float], highs: List[float], lows: List[float]) -> Tuple[str, str]:
    """
    Minimal but useful:
    - TREND_PULLBACK when SMA20 > SMA50 and price near SMA20
    - BREAKOUT_RETEST when close breaks recent high zone
    """
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    if s20 is None or s50 is None:
        return "UNKNOWN", "not enough data"

    last = closes[-1]
    dist20 = abs(last - s20) / s20 * 100.0

    recent_high = max(highs[-30:]) if len(highs) >= 30 else max(highs)
    breakout = last > recent_high * 0.995  # near/above recent high

    if s20 > s50 and dist20 <= 2.2:
        return "TREND_PULLBACK", "trend up + pullback near SMA20"
    if breakout:
        return "BREAKOUT_RETEST", "breakout near recent high zone"
    return "UNKNOWN", "no clean setup"

def build_signal(symbol: str, candles_main: List[Dict[str, Any]], regime_row: Optional[Dict[str, Any]]) -> Optional[Signal]:
    closes = [safe_float(c["close"]) for c in candles_main]
    highs = [safe_float(c["high"]) for c in candles_main]
    lows  = [safe_float(c["low"]) for c in candles_main]
    qv    = [safe_float(c.get("quote_volume"), 0.0) for c in candles_main]

    if len(closes) < 60:
        return None

    last = closes[-1]
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    r = rsi(closes, 14)
    vol = returns_std(closes, 50)

    if s20 is None or s50 is None or r is None or vol is None:
        return None

    # Liquidity filter (boost win%: avoid dead pairs)
    avg_qv = sum(qv[-50:]) / max(1, len(qv[-50:]))
    if avg_qv < MIN_AVG_QUOTE_VOL:
        return None

    # Volatility sanity (avoid too dead or too wild)
    if vol < MIN_VOLATILITY or vol > MAX_VOLATILITY:
        return None

    # Overextension filter (avoid "already ran" entries)
    overext_pct = abs(last - s20) / s20 * 100.0
    if overext_pct > MAX_OVEREXT_PCT:
        return None

    setup_type, why = classify_setup(closes, highs, lows)
    if setup_type == "UNKNOWN":
        return None

    regime = (regime_row or {}).get("regime") or "UNKNOWN"
    regime_score = int((regime_row or {}).get("score") or 0)

    # Base scoring
    score = 0
    score += 35 if s20 > s50 else 10
    score += 20 if 45 <= r <= 65 else 10
    score += 20 if overext_pct <= 2.5 else 10
    score += 15 if setup_type == "TREND_PULLBACK" else 10
    score += 10 if avg_qv >= (MIN_AVG_QUOTE_VOL * 2) else 5

    # Regime-aware penalty/boost (win% up, exits unchanged)
    # BEAR -> harder to long: penalty
    if regime == "BEAR":
        score -= 15
    elif regime == "BULL":
        score += 5
    elif regime == "RANGE":
        score -= 5

    # include regime strength a bit
    score += int(clamp(regime_score / 10.0, 0, 10))

    score = int(clamp(score, 0, 100))

    # chance/confidence (simple mapping)
    chance = int(clamp(score, 0, 100))
    confidence = int(clamp(score + (5 if regime == "BULL" else 0), 0, 100))

    # Entry/stop/target (basic; exits remain in trade_monitor)
    entry = last
    stop = entry * 0.98  # default 2% stop
    target = entry + 2 * (entry - stop)  # 2R

    expires_at = now_utc() + timedelta(seconds=PREBUY_VALID_SECONDS)

    meta = {
        "avg_quote_volume": avg_qv,
        "volatility_std": vol,
        "overext_pct": overext_pct,
        "sma20": s20,
        "sma50": s50,
        "rsi14": r,
        "regime_score": regime_score,
    }

    return Signal(
        symbol=symbol,
        setup_type=setup_type,
        why=why,
        timeframe=TF_MAIN,
        regime=regime,
        score=score,
        chance=chance,
        confidence=confidence,
        entry=float(entry),
        stop=float(stop),
        target=float(target),
        expires_at=expires_at,
        meta=meta,
    )

# ==========================================================
# INSERT pending_approvals (SCHEMA-PROOF)
# ==========================================================
def insert_pending_schema_proof(conn, pending_cols: List[str], s: Signal) -> Optional[str]:
    """
    Inserts a pending approval row.
    Only uses columns that exist to avoid crashes when schema differs.
    Returns created id or None.
    """
    prebuy_id = f"PB-{s.symbol}-{int(now_utc().timestamp())}"

    payload: Dict[str, Any] = {
        "id": prebuy_id,
        "symbol": s.symbol,
        "setup_type": s.setup_type,
        "timeframe": s.timeframe,
        "regime": s.regime,
        "score": s.score,
        "chance": s.chance,
        "confidence": s.confidence,
        "entry": s.entry,
        "stop": s.stop,
        "target": s.target,
        "status": "PENDING",
        "created_at": now_utc(),
        "expires_at": s.expires_at,
    }

    # Optional nice-to-have columns (only if exist)
    # (keeps future upgrades painless)
    if "why" in pending_cols:
        payload["why"] = s.why
    if "meta" in pending_cols:
        payload["meta"] = json.dumps(s.meta)

    # filter to existing columns
    cols = [k for k in payload.keys() if k in pending_cols]
    if "id" not in cols or "symbol" not in cols:
        raise RuntimeError("pending_approvals schema missing required columns (id/symbol)")

    values = [payload[c] for c in cols]
    placeholders = ", ".join(["%s"] * len(cols))
    col_sql = ", ".join(cols)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO public.pending_approvals ({col_sql})
            VALUES ({placeholders})
            ON CONFLICT (id) DO NOTHING
            """,
            values,
        )
    conn.commit()
    return prebuy_id

# ==========================================================
# MAIN
# ==========================================================
def main() -> int:
    if not DATABASE_URL:
        log("FATAAL: DATABASE_URL ontbreekt")
        return 2

    with db_connect() as conn:
        ensure_fingerprint_table(conn)
        ensure_prebuy_state(conn)

        created_today = get_created_today(conn)
        day = str(now_utc().date())
        log(f"🧠 multi_coin_score | day={day} | created_today={created_today}/{MAX_PREBUY_PER_DAY}")

        if created_today >= MAX_PREBUY_PER_DAY:
            log("⛔ Daglimiet bereikt, skip run.")
            return 0

        # read pending_approvals schema once (efficiency)
        pending_cols = get_table_columns(conn, "pending_approvals", "public")

        universe = fetch_universe(conn)
        log(f"📌 universe={len(universe)} (limit={UNIVERSE_LIMIT}) tf_main={TF_MAIN} tf_ctx={TF_CTX}")

        # regimes in 1 batch (efficiency)
        regimes = fetch_latest_regimes(conn, universe, TF_MAIN)

        created_now = 0
        for sym in universe:
            if created_today + created_now >= MAX_PREBUY_PER_DAY:
                break

            try:
                candles_main = fetch_candles(conn, sym, TF_MAIN, LOOKBACK_MAIN)
                sig = build_signal(sym, candles_main, regimes.get(sym))

                if not sig:
                    continue

                # GO/WATCH gating (do not block WATCH; store both for learning)
                if sig.score < WATCH_MIN_SCORE:
                    continue

                # fingerprint (no duplicates of same trade)
                fp = sha1(f"{sig.symbol}|{sig.setup_type}|{round(sig.entry, 6)}|{round(sig.target, 6)}")
                if fingerprint_exists_recent(conn, fp, TRADE_COOLDOWN_SECONDS):
                    continue

                # insert into pending approvals
                prebuy_id = insert_pending_schema_proof(conn, pending_cols, sig)
                if prebuy_id:
                    upsert_fingerprint(conn, fp)
                    bump_created_today(conn, 1)
                    created_now += 1

                    label = "GO" if sig.score >= MIN_SCORE_TO_PREBUY else "WATCH"
                    log(
                        f"✅ {label} {prebuy_id} {sig.symbol} "
                        f"{sig.setup_type} score={sig.score} regime={sig.regime} "
                        f"entry={sig.entry:.6f} stop={sig.stop:.6f} target={sig.target:.6f}"
                    )

            except Exception as e:
                log(f"⚠️ skip {sym} error={type(e).__name__}: {e}")

        log(f"DONE created={created_now}")
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
