# analysis/multi_coin_score.py
from __future__ import annotations

import os
import time
import math
import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
import psycopg2
import psycopg2.extras

# ==========================================================
# ENV
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL ontbreekt (Render Postgres).")

# Optioneel: als je een interne endpoint hebt (bijv. webhook service) om prebuys direct te pushen
WEBHOOK_BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or "").strip().rstrip("/")
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()

FORCE_TEST_PREBUY = (os.getenv("FORCE_TEST_PREBUY") or "0").strip() == "1"

PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))
MAX_PREBUY_PER_DAY = int(os.getenv("MAX_PREBUY_PER_DAY") or "5")

MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "80")
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "70")

TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS") or str(6 * 60 * 60))

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT") or "15")

# Scoreboard (van simulator)
SCOREBOARD_PATH = (os.getenv("SCOREBOARD_PATH") or "/data/scoreboard.json").strip()

# ==========================================================
# MARKETS / DATA
# ==========================================================
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
EXCHANGE = (os.getenv("EXCHANGE") or "binance").strip().lower()

# ✅ DB universe scanning settings
ROTATE_BATCH_SIZE = int(os.getenv("ROTATE_BATCH_SIZE") or "50")
CORE_LIMIT = int(os.getenv("CORE_LIMIT") or "28")  # jij hebt nu CORE=28 in DB

# Live fallback interval (alleen als candles-table geen data heeft)
INTERVAL_1H = (os.getenv("INTERVAL_1H") or "1h").strip()
INTERVAL_4H = (os.getenv("INTERVAL_4H") or "4h").strip()

# hoeveel candles je nodig hebt
LIMIT_1H = int(os.getenv("LIMIT_1H") or "300")
LIMIT_4H = int(os.getenv("LIMIT_4H") or "300")

# ==========================================================
# PROFESSIONAL UPGRADE KNOBS
# ==========================================================
# Volatility filter (ATR percentile): geen trades bij extreme low-vol
ATR_PERIOD = int(os.getenv("ATR_PERIOD") or "14")
ATR_PERCENTILE_WINDOW = int(os.getenv("ATR_PERCENTILE_WINDOW") or "300")
ATR_MIN_PERCENTILE = float(os.getenv("ATR_MIN_PERCENTILE") or "0.20")  # 20e percentiel

# Trend strength filter (EMA200 slope)
EMA_TREND_PERIOD = int(os.getenv("EMA_TREND_PERIOD") or "200")
EMA_SLOPE_LOOKBACK = int(os.getenv("EMA_SLOPE_LOOKBACK") or "30")
EMA_MIN_ABS_SLOPE = float(os.getenv("EMA_MIN_ABS_SLOPE") or "0.0")  # 0.0 = alleen info, geen harde block

# Score normalisatie factors
SETUP_FACTOR_TREND_PULLBACK = float(os.getenv("SETUP_FACTOR_TREND_PULLBACK") or "1.05")
SETUP_FACTOR_BREAKOUT_RETEST = float(os.getenv("SETUP_FACTOR_BREAKOUT_RETEST") or "1.00")

REGIME_FACTOR_BULL = float(os.getenv("REGIME_FACTOR_BULL") or "1.05")
REGIME_FACTOR_RANGE = float(os.getenv("REGIME_FACTOR_RANGE") or "0.95")
REGIME_FACTOR_BEAR = float(os.getenv("REGIME_FACTOR_BEAR") or "0.85")

HTF_FACTOR_BULL = float(os.getenv("HTF_FACTOR_BULL") or "1.05")
HTF_FACTOR_RANGE = float(os.getenv("HTF_FACTOR_RANGE") or "0.95")
HTF_FACTOR_BEAR = float(os.getenv("HTF_FACTOR_BEAR") or "0.85")

BTC_FACTOR_BULL = float(os.getenv("BTC_FACTOR_BULL") or "1.05")
BTC_FACTOR_RANGE = float(os.getenv("BTC_FACTOR_RANGE") or "0.95")
BTC_FACTOR_BEAR = float(os.getenv("BTC_FACTOR_BEAR") or "0.80")

# Als volatility filter faalt: downgrade score naar max deze waarde (i.p.v. skippen)
LOWVOL_MAX_SCORE = int(os.getenv("LOWVOL_MAX_SCORE") or "72")

# ==========================================================
# DB
# ==========================================================
def db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def db_init() -> None:
    """
    Let op: jij hebt al schema fixes gedaan.
    We doen hier alleen 'veilig' aanmaken + add-column IF NOT EXISTS.
    Nooit drop.
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            # pending approvals (centrale queue)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS pending_approvals (
                id              TEXT PRIMARY KEY,
                symbol          TEXT NOT NULL,
                setup_type      TEXT,
                label           TEXT,
                score           INTEGER,
                grade           TEXT,
                entry           DOUBLE PRECISION,
                stop_loss       DOUBLE PRECISION,
                target          DOUBLE PRECISION,
                regime          TEXT,

                status          TEXT NOT NULL DEFAULT 'PENDING',
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at      TIMESTAMPTZ NOT NULL,

                approved_amount DOUBLE PRECISION,
                approved_at     TIMESTAMPTZ,
                consumed_at     TIMESTAMPTZ,
                rejected_at     TIMESTAMPTZ
            );
            """)
            cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending_approvals_status_expires
            ON pending_approvals(status, expires_at);
            """)

            # ✅ uitbreidingen (zonder te breken als ze al bestaan)
            cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS chance DOUBLE PRECISION;")
            cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS confidence INTEGER;")
            cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS details JSONB;")

            # Dedupe fingerprints
            cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_fingerprints (
                fingerprint     TEXT PRIMARY KEY,
                last_created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)

            # Daily state (daglimiet)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS prebuy_state (
                day           DATE PRIMARY KEY,
                created_count INTEGER NOT NULL DEFAULT 0
            );
            """)

            # ✅ Coin universe + scan cursor (voor 400+ coins batching)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS coin_universe (
                symbol TEXT PRIMARY KEY,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                tier TEXT NOT NULL DEFAULT 'ROTATE', -- CORE / ROTATE / LONGTAIL
                priority INTEGER NOT NULL DEFAULT 100
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS scan_cursor (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """)
            cur.execute("""
            INSERT INTO scan_cursor(key, value)
            VALUES ('rotate_batch_index', 0)
            ON CONFLICT (key) DO NOTHING;
            """)

        conn.commit()

# ==========================================================
# Helpers: time + logging
# ==========================================================
def now_ts() -> int:
    return int(time.time())

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def log(msg: str) -> None:
    print(msg, flush=True)

# ==========================================================
# ✅ Universe selection (CORE + ROTATE batch)
# ==========================================================
def db_get_core_symbols(limit: int) -> List[str]:
    q = """
    SELECT symbol
    FROM coin_universe
    WHERE is_active = TRUE AND tier = 'CORE'
    ORDER BY priority ASC, symbol ASC
    LIMIT %s
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (int(limit),))
            rows = cur.fetchall() or []
    return [r[0] for r in rows]

def db_get_rotate_batch(batch_size: int) -> Tuple[List[str], int, int]:
    """
    Returns: (batch_symbols, batch_index, num_batches)
    """
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT value FROM scan_cursor WHERE key='rotate_batch_index' LIMIT 1;")
            row = cur.fetchone()
            idx = int(row["value"]) if row else 0

            cur.execute("""
                SELECT symbol
                FROM coin_universe
                WHERE is_active = TRUE AND tier = 'ROTATE'
                ORDER BY priority ASC, symbol ASC
            """)
            all_syms = [r["symbol"] for r in (cur.fetchall() or [])]

            total = len(all_syms)
            if total == 0:
                return [], 0, 0

            num_batches = (total + batch_size - 1) // batch_size
            idx = idx % max(num_batches, 1)

            start = idx * batch_size
            end = min(start + batch_size, total)
            batch = all_syms[start:end]

            next_idx = (idx + 1) % max(num_batches, 1)
            cur.execute("""
                INSERT INTO scan_cursor(key, value, updated_at)
                VALUES ('rotate_batch_index', %s, NOW())
                ON CONFLICT (key)
                DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """, (int(next_idx),))
        conn.commit()

    return batch, idx, num_batches

def get_scan_symbols() -> Tuple[List[str], Dict[str, Any]]:
    """
    Bepaalt de coins die deze run worden gescand.
    CORE altijd + 1 ROTATE batch.
    """
    core = db_get_core_symbols(CORE_LIMIT)
    rotate_batch, batch_idx, num_batches = db_get_rotate_batch(ROTATE_BATCH_SIZE)

    # voorkom dubbel
    rotate_batch = [s for s in rotate_batch if s not in set(core)]
    scan = core + rotate_batch

    info = {
        "core_count": len(core),
        "rotate_batch_count": len(rotate_batch),
        "rotate_batch_idx": int(batch_idx),
        "rotate_num_batches": int(num_batches),
        "total_scan": len(scan),
        "batch_size": int(ROTATE_BATCH_SIZE),
    }
    return scan, info

# ==========================================================
# Data fetch: DB candles first, Binance fallback
# ==========================================================
def db_fetch_candles(symbol: str, timeframe: str, limit: int) -> List[Dict[str, Any]]:
    """
    Haalt candles uit Postgres candles-table (die jij met history_fetcher vult).
    Vereist: candles(exchange,symbol,timeframe,open_time,...)
    """
    q = """
    SELECT
      open_time,
      open, high, low, close, volume,
      close_time,
      trades
    FROM candles
    WHERE exchange=%s AND symbol=%s AND timeframe=%s
    ORDER BY open_time DESC
    LIMIT %s
    """
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(q, (EXCHANGE, symbol, timeframe, int(limit)))
            rows = cur.fetchall() or []
    rows.reverse()
    return rows

def binance_fetch_klines(symbol: str, interval: str, limit: int) -> List[List[Any]]:
    r = requests.get(
        BINANCE_KLINES_URL,
        params={"symbol": symbol, "interval": interval, "limit": int(limit)},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return []
    return data

def get_ohlcv(symbol: str, timeframe: str, limit: int) -> Tuple[List[float], List[float], List[float], List[float]]:
    """
    Returns highs,lows,closes,opens
    DB-first, Binance fallback.
    """
    try:
        rows = db_fetch_candles(symbol, timeframe, limit)
        if rows and len(rows) >= max(60, min(120, limit // 2)):
            opens = [float(r["open"]) for r in rows]
            highs = [float(r["high"]) for r in rows]
            lows = [float(r["low"]) for r in rows]
            closes = [float(r["close"]) for r in rows]
            return highs, lows, closes, opens
    except Exception as e:
        log(f"⚠️ DB candles fetch failed {symbol} {timeframe}: {e}")

    kl = binance_fetch_klines(symbol, timeframe, limit)
    if not kl:
        return [], [], [], []
    opens = [float(k[1]) for k in kl]
    highs = [float(k[2]) for k in kl]
    lows = [float(k[3]) for k in kl]
    closes = [float(k[4]) for k in kl]
    return highs, lows, closes, opens

# ==========================================================
# Indicators
# ==========================================================
def sma(values: List[float], period: int) -> float:
    if len(values) < period:
        return float("nan")
    return sum(values[-period:]) / float(period)

def rsi14(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return float("nan")
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += abs(diff)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))

def ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out: List[float] = []
    ema_prev = sum(values[:period]) / period
    out.append(ema_prev)
    for v in values[period:]:
        ema_prev = v * k + ema_prev * (1 - k)
        out.append(ema_prev)
    return out

def linear_slope(values: List[float], lookback: int = 20) -> float:
    if len(values) < lookback:
        return 0.0
    y = values[-lookback:]
    n = lookback
    x_mean = (n - 1) / 2
    y_mean = sum(y) / n
    num = 0.0
    den = 0.0
    for i in range(n):
        dx = i - x_mean
        dy = y[i] - y_mean
        num += dx * dy
        den += dx * dx
    if den == 0:
        return 0.0
    return num / den

def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))

def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
    if len(closes) < period + 1:
        return []
    trs: List[float] = []
    for i in range(1, len(closes)):
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))
    out: List[float] = []
    atr_prev = sum(trs[:period]) / period
    out.append(atr_prev)
    for tr in trs[period:]:
        atr_prev = (atr_prev * (period - 1) + tr) / period
        out.append(atr_prev)
    return out

def atr_percentile_filter(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14,
    percentile_window: int = 300,
    min_percentile: float = 0.20,
) -> Tuple[bool, Dict[str, Any]]:
    a = atr(highs, lows, closes, period=period)
    if len(a) < 10:
        return True, {"atr_ok": True, "atr_reason": "not_enough_data"}

    last_atr = a[-1]
    window = a[-percentile_window:] if len(a) >= percentile_window else a[:]
    sorted_w = sorted(window)
    idx = int((len(sorted_w) - 1) * min_percentile)
    threshold = sorted_w[idx]
    ok = last_atr >= threshold

    return ok, {
        "atr_ok": ok,
        "atr_last": float(last_atr),
        "atr_threshold": float(threshold),
        "atr_min_percentile": float(min_percentile),
        "atr_window": int(len(window)),
    }

def trend_strength_filter(
    closes: List[float],
    ema_period: int = 200,
    slope_lb: int = 30,
    min_abs_slope: float = 0.0,
) -> Tuple[bool, Dict[str, Any]]:
    e = ema(closes, ema_period)
    if len(e) < slope_lb + 5:
        return True, {"trend_ok": True, "trend_reason": "not_enough_data"}

    slope = linear_slope(e, lookback=slope_lb)
    ok = abs(slope) >= min_abs_slope

    return ok, {
        "trend_ok": ok,
        "ema_period": int(ema_period),
        "slope_lb": int(slope_lb),
        "ema_slope": float(slope),
        "min_abs_slope": float(min_abs_slope),
    }

def detect_regime_4h(closes_4h: List[float]) -> str:
    """
    Robuuste HTF regime:
    - Bull: close > EMA200 en EMA200 stijgend
    - Bear: close < EMA200 en EMA200 dalend
    - Anders: RANGE
    """
    e200 = ema(closes_4h, EMA_TREND_PERIOD)
    if len(e200) < 30:
        return "RANGE"

    last_close = closes_4h[-1]
    last_ema = e200[-1]
    slope = linear_slope(e200, lookback=min(EMA_SLOPE_LOOKBACK, len(e200)))

    if last_close > last_ema and slope > 0:
        return "BULL"
    if last_close < last_ema and slope < 0:
        return "BEAR"
    return "RANGE"

def determine_setup_type(regime_4h: str, rsi_1h: float, s20_1h: float, s50_1h: float) -> str:
    """
    Jij wilt 2 setups aanhouden:
    - TREND_PULLBACK
    - BREAKOUT_RETEST
    """
    if regime_4h == "BULL" and (not math.isnan(rsi_1h) and rsi_1h >= 50) and (not math.isnan(s20_1h) and not math.isnan(s50_1h) and s20_1h >= s50_1h):
        return "TREND_PULLBACK"
    return "BREAKOUT_RETEST"

def overextended_pct(closes: List[float], period: int = 20) -> float:
    s = sma(closes, period)
    if math.isnan(s) or s <= 0:
        return 0.0
    return float(round(((closes[-1] - s) / s) * 100.0, 3))

# ==========================================================
# Score Model (base)
# ==========================================================
def compute_base_score(closes_1h: List[float]) -> Tuple[int, Dict[str, Any]]:
    s20 = sma(closes_1h, 20)
    s50 = sma(closes_1h, 50)
    rsi = rsi14(closes_1h, 14)

    score = 0
    if not math.isnan(s20) and not math.isnan(s50):
        if s20 > s50:
            score += 40
        else:
            score += 15

    if not math.isnan(rsi):
        if rsi >= 55:
            score += 35
        elif rsi >= 45:
            score += 20
        else:
            score += 10

    if len(closes_1h) >= 6 and closes_1h[-1] > closes_1h[-6]:
        score += 15
    else:
        score += 5

    score = max(0, min(100, score))
    meta = {"sma20": float(s20), "sma50": float(s50), "rsi14": float(rsi)}
    return int(score), meta

# ==========================================================
# Factors + normalization
# ==========================================================
def htf_alignment_factor(regime_4h: str) -> float:
    if regime_4h == "BULL":
        return HTF_FACTOR_BULL
    if regime_4h == "RANGE":
        return HTF_FACTOR_RANGE
    return HTF_FACTOR_BEAR

def btc_heat_factor(btc_regime_4h: str) -> float:
    if btc_regime_4h == "BULL":
        return BTC_FACTOR_BULL
    if btc_regime_4h == "RANGE":
        return BTC_FACTOR_RANGE
    return BTC_FACTOR_BEAR

def normalize_score(
    base_score: float,
    regime_4h: str,
    setup_type: str,
    htf_factor: float,
    btc_factor: float,
) -> Tuple[int, Dict[str, Any]]:
    setup_factor = SETUP_FACTOR_TREND_PULLBACK if setup_type == "TREND_PULLBACK" else SETUP_FACTOR_BREAKOUT_RETEST

    if regime_4h == "BULL":
        regime_factor = REGIME_FACTOR_BULL
    elif regime_4h == "BEAR":
        regime_factor = REGIME_FACTOR_BEAR
    else:
        regime_factor = REGIME_FACTOR_RANGE

    raw = base_score * regime_factor * setup_factor * htf_factor * btc_factor
    final = max(0, min(100, int(round(raw))))

    return final, {
        "base_score": float(base_score),
        "regime_factor": float(regime_factor),
        "setup_factor": float(setup_factor),
        "htf_factor": float(htf_factor),
        "btc_factor": float(btc_factor),
        "raw": float(raw),
        "final": int(final),
        "regime_4h": regime_4h,
        "setup_type": setup_type,
    }

# ==========================================================
# Scoreboard (historische edge)
# ==========================================================
def load_scoreboard() -> Dict[str, Any]:
    try:
        if not os.path.exists(SCOREBOARD_PATH):
            return {}
        with open(SCOREBOARD_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def scoreboard_factor(scoreboard: Dict[str, Any], setup_type: str, regime_4h: str) -> Tuple[float, Dict[str, Any]]:
    node: Dict[str, Any] = {}

    try:
        if isinstance(scoreboard.get(setup_type), dict):
            node = scoreboard.get(setup_type, {}).get(regime_4h, {}) or {}
    except Exception:
        node = {}

    if not node:
        try:
            key = f"{setup_type}|{regime_4h}"
            gl = scoreboard.get("global", {}) if isinstance(scoreboard.get("global", {}), dict) else {}
            node = gl.get(key, {}) if isinstance(gl, dict) else {}
        except Exception:
            node = {}

    winrate = float(node.get("winrate", node.get("wr", 0.50)) or 0.50)
    expectancy = float(node.get("expectancy", node.get("avg_r", 0.0)) or 0.0)
    n = int(node.get("n", node.get("trades", node.get("count", 0))) or 0)

    sample_boost = 1.0
    if n >= 300:
        sample_boost = 1.03
    elif n >= 150:
        sample_boost = 1.02
    elif n >= 75:
        sample_boost = 1.01

    f = 1.0
    if winrate >= 0.60 and expectancy > 0:
        f = 1.06
    elif winrate >= 0.55 and expectancy >= 0:
        f = 1.03
    elif winrate < 0.45 or expectancy < 0:
        f = 0.92

    f *= sample_boost

    return float(f), {
        "sb_winrate": float(winrate),
        "sb_expectancy": float(expectancy),
        "sb_n": int(n),
        "sb_factor": float(f),
    }

# ==========================================================
# Label + stop/target + chance
# ==========================================================
def decide_label(score: int) -> Optional[str]:
    if score >= MIN_SCORE_TO_PREBUY:
        return "GO"
    if score >= WATCH_MIN_SCORE:
        return "WATCH"
    return None

def calc_stop_target(entry: float) -> Tuple[float, float]:
    stop = entry * 0.98
    risk = entry - stop
    target = entry + 2.0 * risk
    return stop, target

def fingerprint(symbol: str, setup_type: str, entry: float, target: float) -> str:
    return f"{symbol}|{setup_type}|{round(entry, 8)}|{round(target, 8)}"

def chance_from_score(final_score: int) -> float:
    x = max(0, min(100, int(final_score)))
    return float(round(x / 100.0, 3))

# ==========================================================
# DB functions (state + dedupe + insert)
# ==========================================================
def db_get_daily_count() -> int:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT created_count
                FROM prebuy_state
                WHERE day = CURRENT_DATE
                LIMIT 1
            """)
            row = cur.fetchone()
            return int(row[0]) if row else 0

def db_inc_daily_count() -> None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO prebuy_state(day, created_count)
                VALUES (CURRENT_DATE, 1)
                ON CONFLICT (day)
                DO UPDATE SET created_count = prebuy_state.created_count + 1
            """)
        conn.commit()

def db_dedupe_allowed(fp: str) -> bool:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT EXTRACT(EPOCH FROM (NOW() - last_created_at))::BIGINT AS age_seconds
                FROM trade_fingerprints
                WHERE fingerprint=%s
                LIMIT 1
            """, (fp,))
            row = cur.fetchone()

    if not row:
        return True

    age = int(row[0] or 0)
    return age >= TRADE_COOLDOWN_SECONDS

def db_touch_fingerprint(fp: str) -> None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trade_fingerprints(fingerprint, last_created_at)
                VALUES (%s, NOW())
                ON CONFLICT (fingerprint)
                DO UPDATE SET last_created_at = NOW()
            """, (fp,))
        conn.commit()

def db_insert_prebuy(row: Dict[str, Any]) -> bool:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pending_approvals(
                    id, symbol, setup_type, label, score, grade,
                    entry, stop_loss, target, regime,
                    status, created_at, expires_at,
                    chance, confidence, details
                )
                VALUES (
                    %(id)s, %(symbol)s, %(setup_type)s, %(label)s, %(score)s, %(grade)s,
                    %(entry)s, %(stop_loss)s, %(target)s, %(regime)s,
                    'PENDING', NOW(), %(expires_at)s,
                    %(chance)s, %(confidence)s, %(details)s
                )
                ON CONFLICT (id) DO NOTHING
            """, row)
            inserted = (cur.rowcount == 1)
        conn.commit()
    return inserted

# ==========================================================
# Optional: internal push
# ==========================================================
def send_to_webhook(payload: Dict[str, Any]) -> bool:
    if not WEBHOOK_BASE_URL or not INTERNAL_TOKEN:
        return False
    try:
        r = requests.post(
            f"{WEBHOOK_BASE_URL}/internal/prebuy",
            json=payload,
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=HTTP_TIMEOUT,
        )
        return r.status_code < 300
    except Exception:
        return False

# ==========================================================
# Main
# ==========================================================
def make_id(symbol: str) -> str:
    return f"PB-{symbol}-{now_ts()}"

def main() -> None:
    db_init()

    scoreboard = load_scoreboard()
    if scoreboard:
        log(f"📘 Scoreboard loaded: {SCOREBOARD_PATH}")
    else:
        log(f"📘 Scoreboard missing/empty: {SCOREBOARD_PATH} (ok, continue)")

    created_today = db_get_daily_count()
    created = 0

    # ✅ coins voor deze run: CORE + ROTATE batch
    COINS, uni = get_scan_symbols()
    if uni.get("rotate_num_batches", 0) > 0:
        log(
            f"🧠 universe: core={uni['core_count']} + rotate_batch={uni['rotate_batch_count']} "
            f"(batch {uni['rotate_batch_idx']+1}/{uni['rotate_num_batches']}, size={uni['batch_size']}) "
            f"=> scan_total={uni['total_scan']}"
        )
    else:
        log(f"🧠 universe: core={uni['core_count']} rotate_batch=0 => scan_total={uni['total_scan']}")

    log(f"🚀 multi_coin_score start | created_today={created_today}/{MAX_PREBUY_PER_DAY} | scan_coins={len(COINS)}")

    # BTC heat (4h)
    _, _, btc_closes_4h, _ = get_ohlcv("BTCUSDT", INTERVAL_4H, LIMIT_4H)
    btc_regime = detect_regime_4h(btc_closes_4h) if btc_closes_4h else "RANGE"
    btc_factor = btc_heat_factor(btc_regime)

    for sym in COINS:
        if created_today + created >= MAX_PREBUY_PER_DAY and not FORCE_TEST_PREBUY:
            break

        try:
            highs_1h, lows_1h, closes_1h, _ = get_ohlcv(sym, INTERVAL_1H, LIMIT_1H)
            if not closes_1h or len(closes_1h) < 80:
                log(f"⏭️ skip {sym}: not enough 1h data")
                continue

            entry = float(closes_1h[-1])

            base_score, meta = compute_base_score(closes_1h)
            rsi_1h = float(meta.get("rsi14", float("nan")))
            s20_1h = float(meta.get("sma20", float("nan")))
            s50_1h = float(meta.get("sma50", float("nan")))

            highs_4h, lows_4h, closes_4h, _ = get_ohlcv(sym, INTERVAL_4H, LIMIT_4H)
            if not closes_4h or len(closes_4h) < 120:
                regime_4h = "RANGE"
            else:
                regime_4h = detect_regime_4h(closes_4h)

            setup_type = determine_setup_type(regime_4h, rsi_1h, s20_1h, s50_1h)

            atr_ok, atr_info = atr_percentile_filter(
                highs_1h, lows_1h, closes_1h,
                period=ATR_PERIOD,
                percentile_window=ATR_PERCENTILE_WINDOW,
                min_percentile=ATR_MIN_PERCENTILE,
            )
            trend_ok, trend_info = trend_strength_filter(
                closes_4h if closes_4h else closes_1h,
                ema_period=EMA_TREND_PERIOD,
                slope_lb=EMA_SLOPE_LOOKBACK,
                min_abs_slope=EMA_MIN_ABS_SLOPE,
            )

            htf_factor = htf_alignment_factor(regime_4h)

            sb_factor, sb_info = scoreboard_factor(scoreboard, setup_type, regime_4h) if scoreboard else (1.0, {
                "sb_winrate": 0.0, "sb_expectancy": 0.0, "sb_n": 0, "sb_factor": 1.0
            })

            final_score, norm_info = normalize_score(
                base_score=float(base_score) * float(sb_factor),
                regime_4h=regime_4h,
                setup_type=setup_type,
                htf_factor=htf_factor,
                btc_factor=btc_factor,
            )

            if not atr_ok:
                final_score = min(int(final_score), int(LOWVOL_MAX_SCORE))

            if (not trend_ok) and regime_4h in {"BULL", "BEAR"}:
                final_score = min(int(final_score), 75)

            label = decide_label(int(final_score))

            if FORCE_TEST_PREBUY and label is None:
                label = "WATCH"
                final_score = max(int(final_score), WATCH_MIN_SCORE)

            if label is None:
                continue

            stop_loss, target = calc_stop_target(entry)

            fp = fingerprint(sym, setup_type, entry, target)
            if not db_dedupe_allowed(fp) and not FORCE_TEST_PREBUY:
                log(f"⏭️ dedupe {sym} {setup_type}")
                continue

            prebuy_id = make_id(sym)
            expires_at_dt = utc_now() + timedelta(seconds=PREBUY_VALID_SECONDS)

            grade = "A" if final_score >= 90 else "B" if final_score >= 80 else "C"
            chance = chance_from_score(int(final_score))
            confidence = int(final_score)

            details: Dict[str, Any] = {}
            details.update(meta)
            details.update({"overextended": overextended_pct(closes_1h, 20)})
            details.update(atr_info)
            details.update(trend_info)
            details.update(norm_info)
            details.update(sb_info)
            details.update({
                "btc_regime_4h": btc_regime,
                "btc_factor": btc_factor,
                "regime_4h": regime_4h,
                "setup_type": setup_type,
                "base_score": int(base_score),
            })

            row = {
                "id": prebuy_id,
                "symbol": sym,
                "setup_type": setup_type,
                "label": label,
                "score": int(final_score),
                "grade": grade,
                "entry": float(entry),
                "stop_loss": float(stop_loss),
                "target": float(target),
                "regime": regime_4h,
                "expires_at": expires_at_dt,
                "chance": float(chance),
                "confidence": int(confidence),
                "details": json.dumps(details),
            }

            inserted = db_insert_prebuy(row)
            if inserted:
                db_touch_fingerprint(fp)
                db_inc_daily_count()
                created += 1

                _ = send_to_webhook({
                    "id": prebuy_id,
                    "symbol": sym,
                    "setup_type": setup_type,
                    "label": label,
                    "score": int(final_score),
                    "chance": chance,
                    "entry": float(entry),
                    "stop_loss": float(stop_loss),
                    "target": float(target),
                    "regime": regime_4h,
                    "expires_at": expires_at_dt.isoformat(),
                })

                log(
                    f"✅ created: {prebuy_id} | {sym} | {label} | score={final_score} | chance={chance} | "
                    f"regime={regime_4h} setup={setup_type} | sb_factor={details.get('sb_factor')}"
                )

        except Exception as e:
            log(f"⚠️ ERROR {sym}: {e}")
            continue

    log(f"done. created={created}")

if __name__ == "__main__":
    main()
