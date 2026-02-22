# analysis/multi_coin_score.py
from __future__ import annotations

import os
import math
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

# ==========================================================
# ENV (alles met defaults, zodat hij nooit crasht op missende env)
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
EXCHANGE = (os.getenv("EXCHANGE") or "binance").strip()

TF_MAIN = (os.getenv("TF_MAIN") or "4h").strip()   # hoofd timeframe
TF_CTX = (os.getenv("TF_CTX") or "1h").strip()     # context timeframe

UNIVERSE_LIMIT = int(os.getenv("UNIVERSE_LIMIT") or "250")     # max coins scannen
LOOKBACK_MAIN = int(os.getenv("LOOKBACK_MAIN") or "320")       # 4h candles
LOOKBACK_CTX = int(os.getenv("LOOKBACK_CTX") or "200")         # 1h candles

MAX_PREBUY_PER_DAY = int(os.getenv("MAX_PREBUY_PER_DAY") or "5")

MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "80")   # GO
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "70")           # WATCH

# cooldown: zelfde trade niet opnieuw pushen binnen deze tijd
TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS") or str(6 * 60 * 60))

# hoe lang is een prebuy geldig (expiry in DB)
PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))

# simpele “setups”
SETUP_TYPES = (os.getenv("SETUP_TYPES") or "TREND_PULLBACK,BREAKOUT_RETEST").split(",")

# ==========================================================
# Helpers
# ==========================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def log(msg: str) -> None:
    print(msg, flush=True)

def db_connect():
    # Render Postgres
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def clamp(n: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, n))

# ==========================================================
# Indicators (simpel maar bruikbaar)
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

def slope(values: List[float], period: int = 20) -> Optional[float]:
    # heel simpele slope: (laatste - eerste) / period
    if len(values) < period:
        return None
    return (values[-1] - values[-period]) / float(period)

# ==========================================================
# DB: universe + candles + regime
# ==========================================================
def fetch_universe(conn, limit: int) -> List[str]:
    """
    Pakt symbolen uit candles table (dus geen API calls).
    Minimale DB: één query.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol
            FROM public.candles
            WHERE exchange = %s AND timeframe = %s
            GROUP BY symbol
            ORDER BY MAX(open_time) DESC
            LIMIT %s
            """,
            (EXCHANGE, TF_MAIN, limit),
        )
        rows = cur.fetchall()
    return [r[0] for r in rows]

def fetch_closes(conn, symbol: str, timeframe: str, limit: int) -> Tuple[List[float], Optional[datetime]]:
    """
    Haalt alleen close + open_time (minimale data).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT open_time, close
            FROM public.candles
            WHERE exchange=%s AND symbol=%s AND timeframe=%s
            ORDER BY open_time DESC
            LIMIT %s
            """,
            (EXCHANGE, symbol, timeframe, limit),
        )
        rows = cur.fetchall()

    if not rows:
        return [], None

    # rows zijn DESC, we willen ASC voor indicatoren
    rows = list(reversed(rows))
    closes = [safe_float(r[1]) for r in rows]
    asof_ts = rows[-1][0]
    return closes, asof_ts

def get_regime(conn, symbol: str, timeframe: str, asof_ts: Optional[datetime]) -> str:
    """
    Probeert regime op te halen uit market_regime table.
    Als er niks is: 'UNKNOWN'
    """
    if asof_ts is None:
        return "UNKNOWN"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT regime
            FROM public.market_regime
            WHERE exchange=%s AND symbol=%s AND timeframe=%s AND asof_ts=%s
            LIMIT 1
            """,
            (EXCHANGE, symbol, timeframe, asof_ts),
        )
        row = cur.fetchone()

    if not row:
        return "UNKNOWN"
    return (row.get("regime") or "UNKNOWN").upper()

# ==========================================================
# Slimme scoring (zonder exit aan te passen)
# ==========================================================
def compute_score(closes_main: List[float], closes_ctx: List[float], regime: str) -> Tuple[int, int]:
    """
    Geeft (score, chance) terug (0-100).
    Score = technische kwaliteit.
    Chance = score + kleine regime correctie.

    Doel: betere winst% zonder exit te veranderen:
    - in BEAR strenger (minder rommel trades)
    - in BULL iets toleranter
    """
    if len(closes_main) < 60 or len(closes_ctx) < 60:
        return 0, 0

    sma20 = sma(closes_main, 20)
    sma50 = sma(closes_main, 50)
    rsi14 = rsi(closes_main, 14)
    slp20 = slope(closes_main, 20)

    if sma20 is None or sma50 is None or rsi14 is None or slp20 is None:
        return 0, 0

    last = closes_main[-1]

    # basis score
    score = 50

    # trend filter
    if sma20 > sma50:
        score += 15
    else:
        score -= 10

    # prijs boven sma20 (momentum)
    if last > sma20:
        score += 10
    else:
        score -= 8

    # slope positief
    if slp20 > 0:
        score += 10
    else:
        score -= 8

    # RSI sweetspot
    if 45 <= rsi14 <= 70:
        score += 15
    elif rsi14 < 35:
        score -= 10
    elif rsi14 > 78:
        score -= 12

    score = int(clamp(score, 0, 100))

    # chance = score + regime correctie (slimmer)
    regime = (regime or "UNKNOWN").upper()
    chance = score

    # in BEAR: strenger (minder slechte trades -> hoger win%)
    if regime == "BEAR":
        chance = int(clamp(score - 10, 0, 100))
    elif regime == "BULL":
        chance = int(clamp(score + 5, 0, 100))
    elif regime == "RANGE":
        chance = int(clamp(score - 3, 0, 100))
    else:
        chance = score

    return score, chance

def decide_label(score: int) -> str:
    if score >= MIN_SCORE_TO_PREBUY:
        return "GO"
    if score >= WATCH_MIN_SCORE:
        return "WATCH"
    return "SKIP"

# ==========================================================
# Entry/Stop/Target (simpel)
# ==========================================================
def build_trade_levels(entry: float) -> Tuple[float, float]:
    """
    Default stop: 2% onder entry
    Default target: 2R (entry + 2*(entry-stop))
    """
    stop = entry * 0.98
    risk = entry - stop
    target = entry + 2.0 * risk
    return stop, target

# ==========================================================
# DEDUP / FINGERPRINTS (voorkom dubbele Pre-BUY)
# ==========================================================
def ensure_tables(conn) -> None:
    """
    Maakt trade_fingerprints table aan als die nog niet bestaat.
    Dit is super klein en voorkomt spam + scheelt DB/WhatsApp.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.trade_fingerprints (
              fp TEXT PRIMARY KEY,
              symbol TEXT,
              setup_type TEXT,
              entry DOUBLE PRECISION,
              target DOUBLE PRECISION,
              last_created_at TIMESTAMPTZ
            );
            """
        )
    conn.commit()

def make_fp(symbol: str, setup_type: str, entry: float, target: float) -> str:
    # ronding zodat mini prijsverschil niet elke keer nieuwe trade wordt
    e = round(entry, 6)
    t = round(target, 6)
    return f"{symbol}|{setup_type}|{e}|{t}"

def fp_recent(conn, fp: str) -> bool:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT last_created_at FROM public.trade_fingerprints WHERE fp=%s",
            (fp,),
        )
        row = cur.fetchone()
    if not row or not row.get("last_created_at"):
        return False
    last_created_at: datetime = row["last_created_at"]
    return (now_utc() - last_created_at).total_seconds() < TRADE_COOLDOWN_SECONDS

def fp_touch(conn, fp: str, symbol: str, setup_type: str, entry: float, target: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.trade_fingerprints(fp, symbol, setup_type, entry, target, last_created_at)
            VALUES (%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (fp)
            DO UPDATE SET last_created_at=NOW()
            """,
            (fp, symbol, setup_type, entry, target),
        )
    conn.commit()

# ==========================================================
# PENDING APPROVALS insert (robust)
# ==========================================================
def insert_pending(conn, payload: Dict[str, Any]) -> None:
    """
    Insert in pending_approvals.
    We committen per insert.
    Bij fout: rollback -> voorkomt 'transaction aborted' spam.
    """
    cols = [
        "id", "symbol", "setup_type", "timeframe", "regime",
        "score", "chance", "confidence",
        "entry", "stop", "target",
        "status", "created_at", "expires_at"
    ]
    values = [payload.get(c) for c in cols]

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO public.pending_approvals ({",".join(cols)})
            VALUES ({",".join(["%s"]*len(cols))})
            ON CONFLICT (id) DO NOTHING
            """,
            values,
        )
    conn.commit()

# ==========================================================
# PREBUY STATE (dag teller)
# ==========================================================
def get_day_key() -> str:
    return now_utc().date().isoformat()

def get_created_today(conn) -> int:
    day = get_day_key()
    with conn.cursor() as cur:
        cur.execute("SELECT created_count FROM public.prebuy_state WHERE day=%s", (day,))
        row = cur.fetchone()
    return int(row[0]) if row else 0

def bump_created_today(conn, inc: int = 1) -> None:
    day = get_day_key()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.prebuy_state(day, created_count, updated_at)
            VALUES (%s,%s,NOW())
            ON CONFLICT (day)
            DO UPDATE SET created_count = public.prebuy_state.created_count + EXCLUDED.created_count,
                         updated_at = NOW()
            """,
            (day, inc),
        )
    conn.commit()

# ==========================================================
# MAIN
# ==========================================================
def main() -> None:
    if not DATABASE_URL:
        log("FATAAL: DATABASE_URL ontbreekt.")
        return

    start = time.time()

    with db_connect() as conn:
        ensure_tables(conn)

        created_today = get_created_today(conn)
        log(f"🧠 multi_coin_score | day={get_day_key()} | created_today={created_today}/{MAX_PREBUY_PER_DAY}")

        universe = fetch_universe(conn, UNIVERSE_LIMIT)
        log(f"📌 universe={len(universe)} (limit={UNIVERSE_LIMIT}) tf_main={TF_MAIN} tf_ctx={TF_CTX}")

        created = 0
        skipped = 0

        for symbol in universe:
            if created_today + created >= MAX_PREBUY_PER_DAY:
                break

            try:
                closes_main, asof_main = fetch_closes(conn, symbol, TF_MAIN, LOOKBACK_MAIN)
                closes_ctx, _ = fetch_closes(conn, symbol, TF_CTX, LOOKBACK_CTX)

                if len(closes_main) < 60:
                    skipped += 1
                    continue

                regime = get_regime(conn, symbol, TF_MAIN, asof_main)

                score, chance = compute_score(closes_main, closes_ctx, regime)
                label = decide_label(score)

                if label == "SKIP":
                    skipped += 1
                    continue

                entry = float(closes_main[-1])
                stop, target = build_trade_levels(entry)

                # confidence = simpele mapping (0-100)
                confidence = int(clamp(chance, 0, 100))

                # Dedup: niet dezelfde trade opnieuw
                for setup_type in SETUP_TYPES:
                    setup_type = (setup_type or "").strip()
                    if not setup_type:
                        continue

                    fp = make_fp(symbol, setup_type, entry, target)
                    if fp_recent(conn, fp):
                        # te recent -> skip
                        skipped += 1
                        continue

                    prebuy_id = f"PB-{symbol}-{now_utc().strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}"

                    payload = {
                        "id": prebuy_id,
                        "symbol": symbol,
                        "setup_type": setup_type,
                        "timeframe": TF_MAIN,
                        "regime": regime,
                        "score": score,
                        "chance": chance,
                        "confidence": confidence,
                        "entry": entry,
                        "stop": stop,
                        "target": target,
                        "status": "PENDING",
                        "created_at": now_utc(),
                        "expires_at": now_utc() + timedelta(seconds=PREBUY_VALID_SECONDS),
                    }

                    # Insert pending
                    try:
                        insert_pending(conn, payload)
                    except Exception as e:
                        conn.rollback()
                        log(f"⚠️ skip {symbol} error={type(e).__name__}: {e}")
                        skipped += 1
                        continue

                    # fingerprint updaten + teller
                    try:
                        fp_touch(conn, fp, symbol, setup_type, entry, target)
                    except Exception as e:
                        conn.rollback()
                        log(f"⚠️ fingerprint fail {symbol} {type(e).__name__}: {e}")

                    try:
                        bump_created_today(conn, 1)
                    except Exception as e:
                        conn.rollback()
                        log(f"⚠️ prebuy_state bump fail {type(e).__name__}: {e}")

                    created += 1

                    emoji = "🟢" if label == "GO" else "🟡"
                    log(f"{emoji} {symbol}: {label} score={score} chance={chance} regime={regime} id={prebuy_id}")

                    # per symbol maar 1 prebuy (anders spam)
                    break

            except Exception as e:
                # super belangrijk: rollback zodat we niet in aborted transaction blijven
                try:
                    conn.rollback()
                except Exception:
                    pass
                log(f"⚠️ skip {symbol} error={type(e).__name__}: {e}")
                skipped += 1

        elapsed = round(time.time() - start, 1)
        log(f"✅ DONE created={created} skipped={skipped} seconds={elapsed}")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("❌ FATAAL in multi_coin_score.py")
        log(traceback.format_exc())
