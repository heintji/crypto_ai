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
# ENV / SETTINGS (DB-only)
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL ontbreekt. Zet DATABASE_URL in Render Environment.")

AUTO_UNIVERSE = (os.getenv("AUTO_UNIVERSE") or "0").strip() == "1"

CORE_LIMIT = int(os.getenv("CORE_LIMIT") or "28")
ROTATE_BATCH_SIZE = int(os.getenv("ROTATE_BATCH_SIZE") or "50")

# Analyse scope
TIMEFRAME_CORE = (os.getenv("TIMEFRAME_CORE") or "1h").strip()
TIMEFRAME_TREND = (os.getenv("TIMEFRAME_TREND") or "4h").strip()

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

# Fallback coins (alleen als AUTO_UNIVERSE=0)
DEFAULT_COINS = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]
COINS_ENV = (os.getenv("COINS") or "").strip()

# Scoreboard DB key (optioneel)
SCOREBOARD_DB_KEY = (os.getenv("SCOREBOARD_DB_KEY") or "main").strip()

# BTC regime table (gemaakt door research/build_btc_regime.py)
BTC_REGIME_TABLE = (os.getenv("BTC_REGIME_TABLE") or "btc_regime_4h").strip()


# ==========================================================
# Helpers
# ==========================================================
def log(msg: str) -> None:
    print(msg, flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_day_bounds(ts: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    t = ts or now_utc()
    start = datetime(t.year, t.month, t.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


def db_connect():
    # sslmode=require is ok op Render Postgres
    return psycopg2.connect(DATABASE_URL, sslmode="require")


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


def ensure_min_tables(conn) -> None:
    """
    Minimale tabellen die multi_coin_score nodig heeft.
    We vermijden prebuy_state bewust (want jouw DB heeft daar al een ander schema voor).
    """
    with conn.cursor() as cur:
        # trade_fingerprints (dedupe)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.trade_fingerprints (
                fingerprint TEXT PRIMARY KEY,
                last_created_at TIMESTAMPTZ
            )
            """
        )

        # pending_approvals (minimal; jouw schema kan meer kolommen hebben)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.pending_approvals (
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

        # scoreboards (optioneel; jij hebt ‘m al aangemaakt)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.scoreboards (
                key TEXT PRIMARY KEY,
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        # BTC regime (wordt gevuld door build_btc_regime.py)
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS public.{BTC_REGIME_TABLE} (
                open_time TIMESTAMPTZ PRIMARY KEY,
                regime TEXT NOT NULL,
                ema200 DOUBLE PRECISION,
                close DOUBLE PRECISION
            )
            """
        )

    conn.commit()


# ==========================================================
# DB - daily created count (zonder prebuy_state)
# ==========================================================
def get_created_today(conn) -> int:
    start, end = utc_day_bounds()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM public.pending_approvals
            WHERE created_at >= %s AND created_at < %s
            """,
            (start, end),
        )
        return int(cur.fetchone()[0])


# ==========================================================
# BTC Regime snapshot (laatste status, 4h)
# ==========================================================
def fetch_latest_btc_regime(conn) -> Dict[str, Any]:
    """
    Leest de laatste row uit btc_regime_4h (of via BTC_REGIME_TABLE env).
    Dit is "context" voor Pre-BUY (geen trading logic change).
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT open_time, regime, ema200, close
                FROM public.{BTC_REGIME_TABLE}
                ORDER BY open_time DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if not row:
                return {"btc_regime_4h": None, "btc_regime_time": None, "btc_ema200": None, "btc_close": None}
            return {
                "btc_regime_4h": row.get("regime"),
                "btc_regime_time": row.get("open_time"),
                "btc_ema200": row.get("ema200"),
                "btc_close": row.get("close"),
            }
    except Exception:
        # Niet fataal; bot moet door kunnen blijven draaien
        return {"btc_regime_4h": None, "btc_regime_time": None, "btc_ema200": None, "btc_close": None}


# ==========================================================
# Dedupe
# ==========================================================
def fingerprint_exists_recent(conn, fingerprint: str, cooldown_seconds: int) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_created_at FROM public.trade_fingerprints WHERE fingerprint=%s",
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
            """
            INSERT INTO public.trade_fingerprints(fingerprint, last_created_at)
            VALUES (%s, %s)
            ON CONFLICT (fingerprint) DO UPDATE SET last_created_at = EXCLUDED.last_created_at
            """,
            (fingerprint, now_utc()),
        )
    conn.commit()


# ==========================================================
# Candles fetch (DB)
# ==========================================================
def fetch_candles(conn, symbol: str, timeframe: str, limit: int) -> List[Dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT open_time, open, high, low, close, volume
            FROM public.candles
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
# Indicators (simpel)
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
# Regime detect (4h)
# ==========================================================
def detect_regime(closes: List[float]) -> str:
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    if s20 is None or s50 is None:
        return "UNKNOWN"

    diff = pct(s20, s50)
    if diff > 0.4:
        return "BULL"
    if diff < -0.4:
        return "BEAR"
    return "RANGE"


# ==========================================================
# Scoring
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

    # Trend
    score += 15 if s20 > s50 else -15

    # RSI
    if 45 <= r <= 65:
        score += 15
    elif r < 35:
        score += 8
    elif r > 72:
        score -= 12

    # Momentum
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

    reason = f"s20>{'s50' if s20 > s50 else 's50'} | rsi={r:.1f} | slope={sl:.4f} | regime={regime}"
    return ScoreResult(score, label, chance, confidence, reason)


# ==========================================================
# Universe selectie (veilig, simpel, DB-only)
# ==========================================================
def get_auto_universe(conn) -> Tuple[List[str], List[str], List[str], Dict[str, int]]:
    """
    - core: top CORE_LIMIT symbols op basis van MAX(volume) in TIMEFRAME_CORE
    - rotate: 'scroll' door alle symbols (alphabetisch), batch ROTATE_BATCH_SIZE
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol
            FROM public.candles
            WHERE timeframe=%s
            GROUP BY symbol
            ORDER BY MAX(volume) DESC
            LIMIT %s
            """,
            (TIMEFRAME_CORE, CORE_LIMIT),
        )
        core = [r[0] for r in cur.fetchall() if r and r[0]]

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT symbol
            FROM public.candles
            WHERE timeframe=%s
            ORDER BY symbol ASC
            """,
            (TIMEFRAME_CORE,),
        )
        all_syms = [r[0] for r in cur.fetchall() if r and r[0]]

    rotate_total = len(all_syms)
    if rotate_total == 0:
        return core, [], core, {"offset": 0, "rotate_total": 0, "core_count": len(core), "rotate_count": 0, "scan_total": len(core)}

    offset = int(time.time() // (30 * 60))  # verandert elke 30 min
    offset = (offset * max(1, ROTATE_BATCH_SIZE)) % rotate_total

    rotate: List[str] = []
    if ROTATE_BATCH_SIZE > 0:
        for i in range(min(ROTATE_BATCH_SIZE, rotate_total)):
            rotate.append(all_syms[(offset + i) % rotate_total])

    # Unique + behoud volgorde
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
# Pending insert (schema-robust)
# ==========================================================
def insert_pending(conn, payload: Dict[str, Any]) -> None:
    cols = get_table_columns(conn, "pending_approvals")
    if not cols:
        raise RuntimeError("pending_approvals tabel bestaat niet of is niet zichtbaar.")

    filtered = {k: v for k, v in payload.items() if k in cols}
    keys = list(filtered.keys())
    values = [filtered[k] for k in keys]

    if not keys:
        raise RuntimeError("Payload heeft geen kolommen die matchen met pending_approvals schema.")

    placeholders = ", ".join(["%s"] * len(keys))
    colnames = ", ".join(keys)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO public.pending_approvals ({colnames})
            VALUES ({placeholders})
            ON CONFLICT (id) DO NOTHING
            """,
            tuple(values),
        )
    conn.commit()


# ==========================================================
# Optional: scoreboard presence check (DB-only)
# ==========================================================
def scoreboard_exists(conn) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM public.scoreboards WHERE key=%s LIMIT 1", (SCOREBOARD_DB_KEY,))
            return cur.fetchone() is not None
    except Exception:
        return False


# ==========================================================
# MAIN
# ==========================================================
def main() -> int:
    start_ts = now_utc()

    try:
        with db_connect() as conn:
            ensure_min_tables(conn)

            if not scoreboard_exists(conn):
                log("🟦 Scoreboard DB: geen record gevonden (ok, continue)")

            # BTC regime context (laatste snapshot)
            btc_ctx = fetch_latest_btc_regime(conn)
            if btc_ctx.get("btc_regime_4h"):
                log(
                    f"🟣 BTC regime snapshot: {btc_ctx.get('btc_regime_4h')} "
                    f"@ {btc_ctx.get('btc_regime_time')} | ema200={btc_ctx.get('btc_ema200')} close={btc_ctx.get('btc_close')}"
                )
            else:
                log("🟣 BTC regime snapshot: (nog leeg / nog niet gebouwd) (ok, continue)")

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

            created_today_db = get_created_today(conn)
            log(f"🚀 multi_coin_score start | created_today={created_today_db}/{MAX_PREBUY_PER_DAY} | coins={len(universe)}")

            if not FORCE_TEST_PREBUY and created_today_db >= MAX_PREBUY_PER_DAY:
                log("✅ Daglimiet bereikt. Stop run.")
                return 0

            created_now = 0
            created_today_local = created_today_db  # lokaal bijhouden om DB-spam te voorkomen

            for idx, sym in enumerate(universe, start=1):
                # Daglimiet check (lokaal)
                if not FORCE_TEST_PREBUY and created_today_local >= MAX_PREBUY_PER_DAY:
                    break

                # Af en toe her-sync met DB (veilig bij parallel runs)
                if idx % 25 == 0:
                    created_today_local = get_created_today(conn)
                    if not FORCE_TEST_PREBUY and created_today_local >= MAX_PREBUY_PER_DAY:
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

                    # Dedupe fingerprint: zelfde coin + setup + entry + target + timeframe = dezelfde trade
                    fingerprint = f"{sym}|{setup_type}|{TIMEFRAME_CORE}|{round(entry, 6)}|{round(target, 6)}"
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

                        # BTC context (alleen als kolommen bestaan -> insert_pending filtert)
                        "btc_regime_4h": btc_ctx.get("btc_regime_4h"),
                        "btc_regime_time": btc_ctx.get("btc_regime_time"),
                        "btc_ema200": btc_ctx.get("btc_ema200"),
                        "btc_close": btc_ctx.get("btc_close"),
                    }

                    insert_pending(conn, payload)
                    upsert_fingerprint(conn, fingerprint)

                    created_now += 1
                    created_today_local += 1

                    log(
                        f"✅ PREBUY {sr.label} | {sym} | score={sr.score} | chance={sr.chance} | "
                        f"created_today≈{created_today_local}/{MAX_PREBUY_PER_DAY}"
                    )
                    log(f"   ↳ entry={entry:.6f} stop={stop:.6f} target={target:.6f} | {sr.reason}")

                except Exception as e:
                    log(f"⚠️ ERROR {sym}: {e}")
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
