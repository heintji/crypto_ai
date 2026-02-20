# analysis/multi_coin_score.py
from __future__ import annotations

import os
import sys
import json
import math
import time
import uuid
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

AUTO_UNIVERSE = (os.getenv("AUTO_UNIVERSE") or "0").strip() == "1"
CORE_LIMIT = int(os.getenv("CORE_LIMIT") or "28")
ROTATE_BATCH_SIZE = int(os.getenv("ROTATE_BATCH_SIZE") or "50")

# Analyse scope
TIMEFRAME_CORE = (os.getenv("TIMEFRAME_CORE") or "1h").strip()   # voor universe selectie + snelle scan
TIMEFRAME_TREND = (os.getenv("TIMEFRAME_TREND") or "4h").strip() # context (regime/trend)

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

# Fallback coins (alleen als AUTO_UNIVERSE=0 en COINS env leeg)
DEFAULT_COINS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "SOLUSDT", "DOGEUSDT"
]
COINS_ENV = (os.getenv("COINS") or "").strip()

# ==========================================================
# DATA DIRECTORY (Render Disk first, fallback to /tmp)
# ==========================================================
DATA_DIR = os.getenv("DATA_DIR", "/data").strip()

# Als /data niet bestaat (geen disk gekoppeld), val terug naar /tmp
if not os.path.isdir(DATA_DIR):
    print("⚠️  /data niet beschikbaar — fallback naar /tmp/crypto_ai_data")
    DATA_DIR = "/tmp/crypto_ai_data"

Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

SCOREBOARD_PATH = os.path.join(DATA_DIR, "scoreboard.json")  # optioneel (kan leeg/missing zijn)
LOGS_DIR = os.path.join(DATA_DIR, "logs")
Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)

print(f"📁 DATA_DIR actief: {DATA_DIR}")


# ==========================================================
# LOGGING HELPERS
# ==========================================================
def log(msg: str) -> None:
    print(msg, flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_utc_date() -> str:
    return now_utc().date().isoformat()


# ==========================================================
# DB HELPERS
# ==========================================================
def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt. Zet DATABASE_URL in Render Environment.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def get_table_columns(conn, table: str) -> List[str]:
    # table: "pending_approvals" (zonder schema) -> we kijken in public
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
    We maken minimale tabellen aan ALS ze nog niet bestaan.
    Als jij al uitgebreid schema hebt: geen probleem, CREATE IF NOT EXISTS doet niks stuk.
    """
    with conn.cursor() as cur:
        # prebuy_state (dagtelling)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.prebuy_state (
                day DATE PRIMARY KEY,
                created_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )

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

    conn.commit()


def get_created_today(conn) -> int:
    d = today_utc_date()
    with conn.cursor() as cur:
        cur.execute("SELECT created_count FROM public.prebuy_state WHERE day=%s", (d,))
        row = cur.fetchone()
        return int(row[0]) if row else 0


def inc_created_today(conn, inc: int = 1) -> int:
    d = today_utc_date()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.prebuy_state(day, created_count)
            VALUES (%s, %s)
            ON CONFLICT (day) DO UPDATE SET created_count = public.prebuy_state.created_count + EXCLUDED.created_count
            RETURNING created_count
            """,
            (d, inc),
        )
        new_val = int(cur.fetchone()[0])
    conn.commit()
    return new_val


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
# CANDLES FETCH (jouw schema gebruikt open_time)
# columns: exchange, symbol, timeframe, open_time, open, high, low, close, volume, close_time, ...
# ==========================================================
def fetch_candles(
    conn,
    symbol: str,
    timeframe: str,
    limit: int,
) -> List[Dict[str, Any]]:
    """
    Return candles in chronologische volgorde (oud -> nieuw)
    """
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

    # rows zijn nieuw->oud, draai om naar oud->nieuw
    rows = list(reversed(rows))
    return [dict(r) for r in rows]


# ==========================================================
# INDICATORS (simpel, stabiel, snel)
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
    # simpele slope: (laatste - eerste)/window
    return (values[-1] - values[-window]) / float(window)


def pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0


# ==========================================================
# REGIME DETECT (simpel: bull/bear/range op 4h)
# ==========================================================
def detect_regime(closes: List[float]) -> str:
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    if s20 is None or s50 is None:
        return "UNKNOWN"

    diff = pct(s20, s50)
    # kleine drempels (kan later verfijnd)
    if diff > 0.4:
        return "BULL"
    if diff < -0.4:
        return "BEAR"
    return "RANGE"


# ==========================================================
# SCORING (GO/WATCH)
# - bewust simpel, zodat stabiel draait en later uitbreidbaar
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
        return ScoreResult(
            score=0,
            label="SKIP",
            chance=0,
            confidence=0,
            reason="Te weinig candles voor indicators",
        )

    # Basis score
    score = 50

    # Trend alignment
    if s20 > s50:
        score += 15
    else:
        score -= 15

    # RSI (liever niet overbought)
    if 45 <= r <= 65:
        score += 15
    elif r < 35:
        score += 8  # oversold kan kans geven
    elif r > 72:
        score -= 12

    # Slope (momentum)
    if sl > 0:
        score += 10
    else:
        score -= 10

    # Regime bias (simple)
    if regime == "BULL":
        score += 8
    elif regime == "BEAR":
        score -= 10
    elif regime == "RANGE":
        score -= 4

    score = max(0, min(100, int(round(score))))

    # Chance + confidence (heuristic)
    chance = score
    confidence = min(100, max(0, int(round((score * 0.9) + (10 if regime == "BULL" else 0)))))

    if score >= MIN_SCORE_TO_PREBUY:
        label = "GO"
    elif score >= WATCH_MIN_SCORE:
        label = "WATCH"
    else:
        label = "SKIP"

    reason = f"s20>{'s50' if s20 > s50 else 's50'} | rsi={r:.1f} | slope={sl:.4f} | regime={regime}"
    return ScoreResult(score=score, label=label, chance=chance, confidence=confidence, reason=reason)


# ==========================================================
# UNIVERSE SELECTIE (DB volume ranking + rotate)
# ==========================================================
def get_auto_universe(conn) -> Tuple[List[str], List[str], List[str], Dict[str, int]]:
    """
    Return (core, rotate, universe, meta)
    - core = top CORE_LIMIT symbols op basis van MAX(volume) binnen timeframe 1h
    - rotate = volgende ROTATE_BATCH_SIZE symbols (alphabetisch) met offset op basis van huidige tijd (simpel)
    """
    # 1) core op volume
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT symbol
            FROM public.candles
            WHERE timeframe=%s
            GROUP BY symbol
            ORDER BY MAX(volume) DESC
            LIMIT %s
            """,
            (TIMEFRAME_CORE, CORE_LIMIT),
        )
        core = [r[0] for r in cur.fetchall()]

    # 2) rotate lijst (alle symbols die bestaan in timeframe)
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
        all_syms = [r[0] for r in cur.fetchall()]

    rotate_total = len(all_syms)
    if rotate_total == 0:
        return core, [], core, {"offset": 0, "rotate_total": 0}

    # offset draait langzaam mee per run (op basis van tijd / batch)
    # hierdoor “scrollen” we door de lijst zonder state-file nodig.
    offset = int(time.time() // (30 * 60))  # verandert elke 30 min
    offset = (offset * ROTATE_BATCH_SIZE) % rotate_total

    rotate = []
    if ROTATE_BATCH_SIZE > 0 and rotate_total > 0:
        # slice met wrap
        for i in range(ROTATE_BATCH_SIZE):
            rotate.append(all_syms[(offset + i) % rotate_total])

    universe = list(dict.fromkeys(core + rotate))  # unique, behoud volgorde

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
# PENDING INSERT (kolommen detectie => nooit stuk door schema verschil)
# ==========================================================
def insert_pending(conn, payload: Dict[str, Any]) -> None:
    cols = get_table_columns(conn, "pending_approvals")
    if not cols:
        raise RuntimeError("pending_approvals tabel bestaat niet of is niet zichtbaar.")

    filtered = {k: v for k, v in payload.items() if k in cols}

    keys = list(filtered.keys())
    values = [filtered[k] for k in keys]

    placeholders = ", ".join(["%s"] * len(keys))
    colnames = ", ".join(keys)

    with conn.cursor() as cur:
        # id is PK: als id bestaat, doen we niets
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
# MAIN RUN
# ==========================================================
def main() -> int:
    start_ts = now_utc()

    try:
        with db_connect() as conn:
            ensure_min_tables(conn)

            # scoreboard file (mag ontbreken)
            if not os.path.exists(SCOREBOARD_PATH):
                log(f"🟦 Scoreboard missing/empty: {SCOREBOARD_PATH} (ok, continue)")

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

            # HARD stop als daglimiet bereikt is (tenzij FORCE_TEST_PREBUY)
            if not FORCE_TEST_PREBUY and created_today >= MAX_PREBUY_PER_DAY:
                log("✅ Daglimiet bereikt. Stop run.")
                return 0

            created_now = 0

            # We scannen universe
            for sym in universe:
                # Daglimiet check
                created_today = get_created_today(conn)
                if not FORCE_TEST_PREBUY and created_today >= MAX_PREBUY_PER_DAY:
                    break

                try:
                    # candles ophalen
                    c1h = fetch_candles(conn, sym, TIMEFRAME_CORE, CANDLE_LIMIT_1H)
                    c4h = fetch_candles(conn, sym, TIMEFRAME_TREND, CANDLE_LIMIT_4H)

                    if len(c1h) < 60 or len(c4h) < 60:
                        continue

                    sr = compute_score(c1h, c4h)
                    if FORCE_TEST_PREBUY:
                        # in testmode: forceer minimaal WATCH zodat we flow kunnen testen
                        if sr.label == "SKIP":
                            sr = ScoreResult(score=WATCH_MIN_SCORE, label="WATCH", chance=WATCH_MIN_SCORE, confidence=WATCH_MIN_SCORE, reason="FORCE_TEST_PREBUY")

                    if sr.label == "SKIP":
                        continue

                    # Entry/stop/target (simpel; execution & strategie zitten elders)
                    entry = float(c1h[-1]["close"])
                    stop = entry * 0.98
                    target = entry + (entry - stop) * 2.0  # 2R

                    # Setup type (nu basic; later uitbreiden naar TREND_PULLBACK / BREAKOUT_RETEST)
                    setup_type = "TREND_PULLBACK" if sr.label == "GO" else "WATCHLIST"
                    regime = detect_regime([float(x["close"]) for x in c4h])

                    # Fingerprint = dezelfde trade niet opnieuw
                    # (zelfde coin + entry + target + setup_type)
                    fingerprint = f"{sym}|{setup_type}|{round(entry, 6)}|{round(target, 6)}"

                    if fingerprint_exists_recent(conn, fingerprint, TRADE_COOLDOWN_SECONDS):
                        continue

                    # Prebuy id
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
                    # per coin fout mag run niet killen
                    log(f"⚠️ ERROR {sym}: {e}")
                    # optioneel: traceback voor debug
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
