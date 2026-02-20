# analysis/multi_coin_score.py
from __future__ import annotations

import os
import sys
import time
import uuid
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

# ==========================================================
# PROJECT ROOT (zodat imports altijd werken)
# ==========================================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ==========================================================
# ENV / SETTINGS
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

# Universe / scan instellingen
AUTO_UNIVERSE = (os.getenv("AUTO_UNIVERSE") or "1").strip() == "1"
CORE_LIMIT = int(os.getenv("CORE_LIMIT") or "28")
ROTATE_BATCH_SIZE = int(os.getenv("ROTATE_BATCH_SIZE") or "50")

# Timeframes
TIMEFRAME_CORE = (os.getenv("TIMEFRAME_CORE") or "1h").strip()   # snelle scan / universe
TIMEFRAME_TREND = (os.getenv("TIMEFRAME_TREND") or "4h").strip() # context / regime

# Candle limieten
CANDLE_LIMIT_1H = int(os.getenv("CANDLE_LIMIT_1H") or "120")
CANDLE_LIMIT_4H = int(os.getenv("CANDLE_LIMIT_4H") or "180")

# Prebuy beperkingen
MAX_PREBUY_PER_DAY = int(os.getenv("MAX_PREBUY_PER_DAY") or "5")
PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))
TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS") or str(6 * 60 * 60))

# Scores
MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "80")   # GO
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "70")           # WATCH
FORCE_TEST_PREBUY = (os.getenv("FORCE_TEST_PREBUY") or "0").strip() == "1"

# Manual fallback (als AUTO_UNIVERSE=0)
DEFAULT_COINS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "SOLUSDT", "DOGEUSDT"]
COINS_ENV = (os.getenv("COINS") or "").strip()

# ====== BELANGRIJK: GEEN DISK ======
# Alles DB-only. Scoreboard komt uit DB table "scoreboards".

# ==========================================================
# LOGGING
# ==========================================================
def log(msg: str) -> None:
    print(msg, flush=True)

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def today_utc_date() -> datetime.date:
    return now_utc().date()

# ==========================================================
# DB CONNECT
# ==========================================================
def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt. Zet DATABASE_URL in Render Environment.")
    # sslmode=require is prima op Render Postgres
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
              SELECT 1
              FROM information_schema.tables
              WHERE table_schema='public' AND table_name=%s
            )
            """,
            (table,),
        )
        return bool(cur.fetchone()[0])

def get_table_columns(conn, table: str) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        rows = cur.fetchall()
    return [r[0] for r in rows]

# ==========================================================
# TABLES (DB-only)
# ==========================================================
PREBUY_DAILY_TABLE = "prebuy_daily_state"  # <-- NIEUW (botst niet met jouw oude prebuy_state)
FINGERPRINTS_TABLE = "trade_fingerprints"
PENDING_TABLE = "pending_approvals"
SCOREBOARDS_TABLE = "scoreboards"
CANDLES_TABLE = "candles"

def ensure_tables(conn) -> None:
    """
    Maak alleen tabellen aan die we echt nodig hebben.
    Let op: we gebruiken prebuy_daily_state (nieuw) om conflicts met jouw oude prebuy_state schema te vermijden.
    """
    with conn.cursor() as cur:
        # Dag-telling (nieuw, schoon)
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS public.{PREBUY_DAILY_TABLE} (
              day DATE PRIMARY KEY,
              created_count INTEGER NOT NULL DEFAULT 0,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        # Fingerprints (dedupe)
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS public.{FINGERPRINTS_TABLE} (
              fingerprint TEXT PRIMARY KEY,
              last_created_at TIMESTAMPTZ
            )
            """
        )

        # Pending approvals (minimaal; jouw schema mag groter zijn)
        # We maken 'm alleen aan als die nog niet bestaat.
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS public.{PENDING_TABLE} (
              id TEXT PRIMARY KEY,
              symbol TEXT,
              setup_type TEXT,
              timeframe TEXT,
              regime TEXT,
              score INTEGER,
              chance INTEGER,
              confidence INTEGER,
              entry DOUBLE PRECISION,
              stop DOUBLE PRECISION,
              target DOUBLE PRECISION,
              status TEXT,
              created_at TIMESTAMPTZ,
              expires_at TIMESTAMPTZ,
              approved_at TIMESTAMPTZ,
              rejected_at TIMESTAMPTZ,
              consumed_at TIMESTAMPTZ
            )
            """
        )

        # Scoreboards (jij hebt deze al aangemaakt, maar safe)
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS public.{SCOREBOARDS_TABLE} (
              key TEXT PRIMARY KEY,
              payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

    conn.commit()

# ==========================================================
# PREBUY DAILY STATE
# ==========================================================
def get_created_today(conn) -> int:
    d = today_utc_date()
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT created_count FROM public.{PREBUY_DAILY_TABLE} WHERE day=%s",
            (d,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0

def inc_created_today(conn, inc: int = 1) -> int:
    d = today_utc_date()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO public.{PREBUY_DAILY_TABLE}(day, created_count, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (day)
            DO UPDATE SET
              created_count = public.{PREBUY_DAILY_TABLE}.created_count + EXCLUDED.created_count,
              updated_at = NOW()
            RETURNING created_count
            """,
            (d, inc),
        )
        new_val = int(cur.fetchone()[0])
    conn.commit()
    return new_val

# ==========================================================
# DEDUPE (FINGERPRINTS)
# ==========================================================
def fingerprint_exists_recent(conn, fingerprint: str, cooldown_seconds: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT last_created_at FROM public.{FINGERPRINTS_TABLE} WHERE fingerprint=%s",
            (fingerprint,),
        )
        row = cur.fetchone()

    if not row or not row[0]:
        return False

    last_ts: datetime = row[0]
    return (now_utc() - last_ts) < timedelta(seconds=cooldown_seconds)

def upsert_fingerprint(conn, fingerprint: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO public.{FINGERPRINTS_TABLE}(fingerprint, last_created_at)
            VALUES (%s, %s)
            ON CONFLICT (fingerprint)
            DO UPDATE SET last_created_at = EXCLUDED.last_created_at
            """,
            (fingerprint, now_utc()),
        )
    conn.commit()

# ==========================================================
# SCOREBOARD (DB-only, optional)
# ==========================================================
def get_scoreboard_payload(conn, key: str = "default") -> Optional[Dict[str, Any]]:
    # Als er geen record is: ok
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT payload FROM public.{SCOREBOARDS_TABLE} WHERE key=%s",
            (key,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return dict(row["payload"]) if row["payload"] is not None else None

# ==========================================================
# CANDLES FETCH
# DB schema (jij hebt): exchange, symbol, timeframe, open_time, open, high, low, close, volume, close_time, ...
# ==========================================================
def fetch_candles(conn, symbol: str, timeframe: str, limit: int) -> List[Dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT open_time, open, high, low, close, volume
            FROM public.{CANDLES_TABLE}
            WHERE symbol=%s AND timeframe=%s
            ORDER BY open_time DESC
            LIMIT %s
            """,
            (symbol, timeframe, limit),
        )
        rows = cur.fetchall()

    rows = list(reversed(rows))  # oud -> nieuw
    return [dict(r) for r in rows]

# ==========================================================
# INDICATORS
# ==========================================================
def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / float(period)

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

def slope(values: List[float], window: int = 10) -> Optional[float]:
    if len(values) < window:
        return None
    return (values[-1] - values[-window]) / float(window)

def pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0

# ==========================================================
# REGIME (4h)
# ==========================================================
def detect_regime(closes_4h: List[float]) -> str:
    s20 = sma(closes_4h, 20)
    s50 = sma(closes_4h, 50)
    if s20 is None or s50 is None:
        return "UNKNOWN"
    diff = pct(s20, s50)
    if diff > 0.4:
        return "BULL"
    if diff < -0.4:
        return "BEAR"
    return "RANGE"

# ==========================================================
# SCORING
# ==========================================================
@dataclass
class ScoreResult:
    score: int
    label: str        # GO / WATCH / SKIP
    chance: int       # 0-100
    confidence: int   # 0-100
    reason: str

def compute_score(c1h: List[Dict[str, Any]], c4h: List[Dict[str, Any]]) -> ScoreResult:
    closes_1h = [float(c["close"]) for c in c1h]
    closes_4h = [float(c["close"]) for c in c4h]

    s20 = sma(closes_1h, 20)
    s50 = sma(closes_1h, 50)
    r = rsi(closes_1h, 14)
    sl = slope(closes_1h, 10)
    regime = detect_regime(closes_4h)

    if s20 is None or s50 is None or r is None or sl is None:
        return ScoreResult(0, "SKIP", 0, 0, "Te weinig candles voor indicators")

    score = 50

    # Trend (SMA20 vs SMA50)
    score += 15 if s20 > s50 else -15

    # RSI band
    if 45 <= r <= 65:
        score += 15
    elif r < 35:
        score += 8
    elif r > 72:
        score -= 12

    # Momentum slope
    score += 10 if sl > 0 else -10

    # Regime bias
    if regime == "BULL":
        score += 8
    elif regime == "BEAR":
        score -= 10
    elif regime == "RANGE":
        score -= 4

    score = max(0, min(100, int(round(score))))

    chance = score
    confidence = min(100, max(0, int(round((score * 0.9) + (10 if regime == "BULL" else 0)))))

    if score >= MIN_SCORE_TO_PREBUY:
        label = "GO"
    elif score >= WATCH_MIN_SCORE:
        label = "WATCH"
    else:
        label = "SKIP"

    reason = f"s20={'>' if s20 > s50 else '<='}s50 | rsi={r:.1f} | slope={sl:.4f} | regime={regime}"
    return ScoreResult(score, label, chance, confidence, reason)

# ==========================================================
# UNIVERSE (DB-only, bulletproof)
# - core: top symbols op MAX(volume) binnen timeframe_core
# - rotate: alle symbols (alphabetisch) met offset, batchsize
# ==========================================================
def get_auto_universe(conn) -> Tuple[List[str], List[str], List[str], Dict[str, int]]:
    # 1) core op volume
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT symbol
            FROM public.{CANDLES_TABLE}
            WHERE timeframe=%s
            GROUP BY symbol
            ORDER BY MAX(volume) DESC
            LIMIT %s
            """,
            (TIMEFRAME_CORE, CORE_LIMIT),
        )
        core = [r[0] for r in cur.fetchall()]

    # 2) alle symbols
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT symbol
            FROM public.{CANDLES_TABLE}
            WHERE timeframe=%s
            ORDER BY symbol ASC
            """,
            (TIMEFRAME_CORE,),
        )
        all_syms = [r[0] for r in cur.fetchall()]

    rotate_total = len(all_syms)
    if rotate_total == 0:
        meta = {"offset": 0, "rotate_total": 0, "core_count": len(core), "rotate_count": 0, "scan_total": len(core)}
        return core, [], core, meta

    # offset verandert elke 30 min
    offset_steps = int(time.time() // (30 * 60))
    offset = (offset_steps * ROTATE_BATCH_SIZE) % rotate_total

    rotate: List[str] = []
    if ROTATE_BATCH_SIZE > 0:
        for i in range(min(ROTATE_BATCH_SIZE, rotate_total)):
            rotate.append(all_syms[(offset + i) % rotate_total])

    # universe unique, volgorde behouden
    universe = list(dict.fromkeys(core + rotate))

    meta = {
        "offset": offset,
        "rotate_total": rotate_total,
        "core_count": len(core),
        "rotate_count": len(rotate),
        "scan_total": len(universe),
    }
    return core, rotate, universe, meta

def get_manual_universe() -> List[str]:
    if COINS_ENV:
        coins = [c.strip().upper() for c in COINS_ENV.split(",") if c.strip()]
        return coins if coins else DEFAULT_COINS
    return DEFAULT_COINS

# ==========================================================
# INSERT PENDING (schema-robust)
# ==========================================================
def insert_pending(conn, payload: Dict[str, Any]) -> None:
    cols = get_table_columns(conn, PENDING_TABLE)
    if not cols:
        raise RuntimeError(f"Tabel {PENDING_TABLE} bestaat niet of is niet zichtbaar.")

    filtered = {k: v for k, v in payload.items() if k in cols}
    keys = list(filtered.keys())
    values = [filtered[k] for k in keys]

    if not keys:
        raise RuntimeError("Payload heeft geen matching kolommen in pending_approvals.")

    placeholders = ", ".join(["%s"] * len(keys))
    colnames = ", ".join(keys)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO public.{PENDING_TABLE} ({colnames})
            VALUES ({placeholders})
            ON CONFLICT (id) DO NOTHING
            """,
            tuple(values),
        )
    conn.commit()

# ==========================================================
# MAIN
# ==========================================================
def main() -> int:
    start_ts = now_utc()

    try:
        with db_connect() as conn:
            # Belangrijk: als er iets fout gaat in een transactie, kunnen volgende statements falen.
            # Daarom: bij errors doen we rollback in de coin-loop.
            ensure_tables(conn)

            sb = get_scoreboard_payload(conn, key="default")
            if sb is None:
                log("🟦 Scoreboard DB: geen record gevonden (ok, continue)")
            else:
                log("🟩 Scoreboard DB: record gevonden (ok)")

            created_today = get_created_today(conn)

            # Universe bepalen
            if AUTO_UNIVERSE:
                core, rotate, universe, meta = get_auto_universe(conn)
                log(
                    f"🧠 universe: core={meta['core_count']} + rotate_batch={meta['rotate_count']} "
                    f"(offset={meta['offset']}, rotate_total={meta['rotate_total']}) => scan_total={meta['scan_total']}"
                )
            else:
                core, rotate = [], []
                universe = get_manual_universe()
                meta = {"core_count": len(universe), "rotate_count": 0, "scan_total": len(universe)}

            log(f"🚀 multi_coin_score start | created_today={created_today}/{MAX_PREBUY_PER_DAY} | coins={len(universe)}")

            # Daglimiet
            if not FORCE_TEST_PREBUY and created_today >= MAX_PREBUY_PER_DAY:
                log("✅ Daglimiet bereikt. Stop run.")
                return 0

            created_now = 0

            for sym in universe:
                # Daglimiet check
                created_today = get_created_today(conn)
                if not FORCE_TEST_PREBUY and created_today >= MAX_PREBUY_PER_DAY:
                    break

                try:
                    c1h = fetch_candles(conn, sym, TIMEFRAME_CORE, CANDLE_LIMIT_1H)
                    c4h = fetch_candles(conn, sym, TIMEFRAME_TREND, CANDLE_LIMIT_4H)

                    if len(c1h) < 60 or len(c4h) < 60:
                        continue

                    sr = compute_score(c1h, c4h)

                    if FORCE_TEST_PREBUY and sr.label == "SKIP":
                        sr = ScoreResult(
                            score=WATCH_MIN_SCORE,
                            label="WATCH",
                            chance=WATCH_MIN_SCORE,
                            confidence=WATCH_MIN_SCORE,
                            reason="FORCE_TEST_PREBUY",
                        )

                    if sr.label == "SKIP":
                        continue

                    entry = float(c1h[-1]["close"])
                    stop = entry * 0.98
                    target = entry + (entry - stop) * 2.0  # 2R

                    setup_type = "TREND_PULLBACK" if sr.label == "GO" else "WATCHLIST"
                    regime = detect_regime([float(x["close"]) for x in c4h])

                    fingerprint = f"{sym}|{setup_type}|{round(entry, 6)}|{round(target, 6)}"
                    if fingerprint_exists_recent(conn, fingerprint, TRADE_COOLDOWN_SECONDS):
                        continue

                    pb_id = f"PB-{sym}-{int(time.time())}-{uuid.uuid4().hex[:6]}"

                    payload = {
                        "id": pb_id,
                        "symbol": sym,
                        "setup_type": setup_type,
                        "timeframe": TIMEFRAME_CORE,
                        "regime": regime,
                        "score": int(sr.score),
                        "chance": int(sr.chance),
                        "confidence": int(sr.confidence),
                        "entry": float(entry),
                        "stop": float(stop),
                        "target": float(target),
                        "status": "PENDING",
                        "created_at": now_utc(),
                        "expires_at": now_utc() + timedelta(seconds=PREBUY_VALID_SECONDS),
                    }

                    insert_pending(conn, payload)
                    upsert_fingerprint(conn, fingerprint)
                    new_created_today = inc_created_today(conn, 1)

                    created_now += 1
                    log(f"✅ PREBUY {sr.label} | {sym} | score={sr.score} | chance={sr.chance} | created_today={new_created_today}/{MAX_PREBUY_PER_DAY}")
                    log(f"   ↳ entry={entry:.6f} stop={stop:.6f} target={target:.6f} | {sr.reason}")

                except Exception as e:
                    # heel belangrijk: rollback zodat je niet blijft hangen in "transaction aborted"
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    log(f"⚠️ ERROR {sym}: {e}")
                    # log(traceback.format_exc())
                    continue

            log(f"done. created={created_now}")
            return 0

    except Exception as e:
        log("❌ FATAAL in multi_coin_score.py")
        log(str(e))
        log(traceback.format_exc())
        return 1
    finally:
        dur = (now_utc() - start_ts).total_seconds()
        log(f"⏱ runtime={dur:.1f}s")

if __name__ == "__main__":
    raise SystemExit(main())
