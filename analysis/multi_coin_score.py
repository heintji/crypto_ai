# analysis/multi_coin_score.py
# ------------------------------------------------------------
# Doel:
# - Alleen ANALYSE + Pre-BUY aanmaken (GEEN BUY/SELL).
# - Schrijft Pre-BUY's naar Postgres (pending_approvals).
# - Optioneel: AUTO_UNIVERSE = scan over hele USDT markt via candles-table.
#
# Belangrijk:
# - Als AUTO_UNIVERSE=1 moet je in logs zien:
#   "🧠 universe: core=28 + rotate_batch=50 ... => scan_total=78"
#   en NIET meer "coins=7"
# ------------------------------------------------------------

from __future__ import annotations

import os
import sys
import math
import time
import json
import uuid
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

# -----------------------------
# Project root in sys.path
# -----------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# -----------------------------
# ENV
# -----------------------------
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

AUTO_UNIVERSE = (os.getenv("AUTO_UNIVERSE") or "0").strip() == "1"
CORE_LIMIT = int(os.getenv("CORE_LIMIT") or "28")
ROTATE_BATCH_SIZE = int(os.getenv("ROTATE_BATCH_SIZE") or "50")

# fallback coins als AUTO_UNIVERSE=0
DEFAULT_COINS = (os.getenv("COINS") or "BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT,ADAUSDT,SOLUSDT,DOGEUSDT").strip()

# scoring thresholds
MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "80")   # GO
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "70")           # WATCH
MAX_PREBUY_PER_DAY = int(os.getenv("MAX_PREBUY_PER_DAY") or "5")

# timeframe / candles
TF_SIGNAL = (os.getenv("TF_SIGNAL") or "4h").strip()   # hoofd timeframe voor setup
TF_CONTEXT = (os.getenv("TF_CONTEXT") or "1h").strip() # context timeframe
CANDLE_LIMIT = int(os.getenv("CANDLE_LIMIT") or "200")

# cooldown / expiry
TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS") or str(6 * 60 * 60))
PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))

# force test
FORCE_TEST_PREBUY = (os.getenv("FORCE_TEST_PREBUY") or "0").strip() == "1"

# logging
LOGS_DIR = (os.getenv("LOGS_DIR") or "/data").strip()
os.makedirs(LOGS_DIR, exist_ok=True)

# optional scoreboard op disk (mag ontbreken)
SCOREBOARD_PATH = (os.getenv("SCOREBOARD_PATH") or "/data/scoreboard.json").strip()


# -----------------------------
# Utils
# -----------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def log(msg: str) -> None:
    print(msg, flush=True)

def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# -----------------------------
# DB helpers
# -----------------------------
def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt in env.")
    return psycopg2.connect(DATABASE_URL)

def db_fetchone(q: str, params: tuple = ()) -> Optional[tuple]:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(q, params)
            return cur.fetchone()
    finally:
        conn.close()

def db_fetchall(q: str, params: tuple = ()) -> List[tuple]:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(q, params)
            return cur.fetchall()
    finally:
        conn.close()

def db_execute(q: str, params: tuple = ()) -> None:
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(q, params)
        conn.commit()
    finally:
        conn.close()

def get_table_columns(table_name: str) -> List[str]:
    rows = db_fetchall(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position
        """,
        (table_name,),
    )
    return [r[0] for r in rows]


# -----------------------------
# Scoreboard (optioneel)
# -----------------------------
def load_scoreboard() -> Dict[str, Any]:
    try:
        if os.path.exists(SCOREBOARD_PATH) and os.path.getsize(SCOREBOARD_PATH) > 0:
            with open(SCOREBOARD_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            log(f"📘 Scoreboard loaded: {SCOREBOARD_PATH}")
            return data if isinstance(data, dict) else {}
        log(f"📘 Scoreboard missing/empty: {SCOREBOARD_PATH} (ok, continue)")
        return {}
    except Exception:
        log(f"⚠️ Scoreboard load failed: {SCOREBOARD_PATH} (ok, continue)")
        return {}


# -----------------------------
# Universe (AUTO_UNIVERSE)
# -----------------------------
def get_rotate_offset(day_key: str) -> int:
    """
    Houd rotatie-offset bij in prebuy_state (DB).
    We maken hem super-robust: als table/kolom niet bestaat -> offset=0.
    """
    try:
        row = db_fetchone("SELECT rotate_offset FROM prebuy_state WHERE day=%s", (day_key,))
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        return 0
    return 0

def set_rotate_offset(day_key: str, new_offset: int) -> None:
    try:
        # probeer update, anders insert (zonder ON CONFLICT afhankelijkheid)
        updated = False
        try:
            conn = db_conn()
            with conn.cursor() as cur:
                cur.execute("UPDATE prebuy_state SET rotate_offset=%s WHERE day=%s", (new_offset, day_key))
                updated = cur.rowcount > 0
            conn.commit()
            conn.close()
        except Exception:
            updated = False

        if not updated:
            conn = db_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO prebuy_state(day, rotate_offset) VALUES (%s, %s)",
                        (day_key, new_offset),
                    )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        # als schema geen rotate_offset heeft: ignore
        pass

def get_auto_universe(day_key: str) -> List[str]:
    """
    pakt:
    - core: top volume (1h) => CORE_LIMIT
    - rotate: batch van overige USDT symbols => ROTATE_BATCH_SIZE, met offset
    """
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            # Core coins: hoogste volume
            cur.execute(
                """
                SELECT symbol
                FROM candles
                WHERE timeframe='1h'
                GROUP BY symbol
                ORDER BY MAX(volume) DESC
                LIMIT %s
                """,
                (CORE_LIMIT,),
            )
            core = [r[0] for r in cur.fetchall()]

            # Rotating pool: alle symbols (stable order) - meestal USDT
            offset = get_rotate_offset(day_key)

            cur.execute(
                """
                SELECT DISTINCT symbol
                FROM candles
                WHERE timeframe='1h'
                ORDER BY symbol
                OFFSET %s
                LIMIT %s
                """,
                (offset, ROTATE_BATCH_SIZE),
            )
            rotate_all = [r[0] for r in cur.fetchall()]
            rotate = [s for s in rotate_all if s not in set(core)]

            universe = core + rotate

            # update offset voor volgende run (wrap)
            try:
                total_row = db_fetchone(
                    "SELECT COUNT(DISTINCT symbol) FROM candles WHERE timeframe='1h'"
                )
                rotate_total = int(total_row[0] or 0) if total_row else 0
                new_offset = offset + ROTATE_BATCH_SIZE
                if rotate_total > 0 and new_offset >= rotate_total:
                    new_offset = 0
                set_rotate_offset(day_key, new_offset)

                log(
                    f"🧠 universe: core={len(core)} + rotate_batch={len(rotate)} "
                    f"(offset={offset}, size={ROTATE_BATCH_SIZE}, rotate_total={rotate_total}) "
                    f"=> scan_total={len(universe)}"
                )
            except Exception:
                log(
                    f"🧠 universe: core={len(core)} + rotate_batch={len(rotate)} "
                    f"(offset={offset}, size={ROTATE_BATCH_SIZE}) => scan_total={len(universe)}"
                )

            return universe
    finally:
        conn.close()


# -----------------------------
# Candles + Indicators (DB)
# -----------------------------
def get_recent_candles(symbol: str, timeframe: str, limit: int = 200) -> List[Dict[str, Any]]:
    rows = db_fetchall(
        """
        SELECT ts, open, high, low, close, volume
        FROM candles
        WHERE symbol=%s AND timeframe=%s
        ORDER BY ts DESC
        LIMIT %s
        """,
        (symbol, timeframe, limit),
    )
    # reverse naar oud->nieuw
    out = []
    for r in reversed(rows):
        out.append(
            {
                "ts": r[0],
                "open": safe_float(r[1]),
                "high": safe_float(r[2]),
                "low": safe_float(r[3]),
                "close": safe_float(r[4]),
                "volume": safe_float(r[5]),
            }
        )
    return out

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

def slope(values: List[float], n: int = 10) -> Optional[float]:
    if len(values) < n:
        return None
    # simpele slope: last - first over n
    return (values[-1] - values[-n]) / float(n)


# -----------------------------
# Scoring (simpel, robuust)
# -----------------------------
@dataclass
class Signal:
    symbol: str
    label: str         # GO / WATCH
    score: int
    chance: float
    entry: float
    stop: float
    target: float
    setup_type: str
    regime: str
    timeframe: str
    why: str

def detect_regime(closes_4h: List[float], sma_fast: Optional[float], sma_slow: Optional[float]) -> str:
    if sma_fast is None or sma_slow is None:
        return "UNKNOWN"
    if sma_fast > sma_slow:
        return "BULL"
    if sma_fast < sma_slow:
        return "BEAR"
    return "RANGE"

def build_signal(symbol: str, scoreboard: Dict[str, Any]) -> Optional[Signal]:
    # Pak candles
    c4 = get_recent_candles(symbol, TF_SIGNAL, CANDLE_LIMIT)
    if len(c4) < 60 and not FORCE_TEST_PREBUY:
        return None

    closes4 = [x["close"] for x in c4]
    entry = closes4[-1] if closes4 else 0.0
    if entry <= 0:
        return None

    sma20 = sma(closes4, 20)
    sma50 = sma(closes4, 50)
    rsi14 = rsi(closes4, 14)
    slp20 = slope(closes4, 20)

    # basis score
    score = 0

    # trend component
    if sma20 is not None and sma50 is not None:
        if sma20 > sma50:
            score += 40
        else:
            score += 20

    # momentum component
    if rsi14 is not None:
        if 45 <= rsi14 <= 65:
            score += 25
        elif rsi14 > 65:
            score += 15
        else:
            score += 10

    # slope component
    if slp20 is not None:
        if slp20 > 0:
            score += 20
        else:
            score += 10

    # force test: minimaal WATCH laten zien
    if FORCE_TEST_PREBUY:
        score = max(score, WATCH_MIN_SCORE)

    # clamp 0..100
    score = int(clamp(score, 0, 100))

    # label
    if score >= MIN_SCORE_TO_PREBUY:
        label = "GO"
    elif score >= WATCH_MIN_SCORE:
        label = "WATCH"
    else:
        return None

    # setup_type (simpel nu; later uitbreiden)
    setup_type = "TREND_PULLBACK"

    # regime
    regime = detect_regime(closes4, sma20, sma50)

    # risk/targets (default: stop 2% onder entry, target 2R)
    stop = entry * 0.98
    risk = entry - stop
    target = entry + 2.0 * risk

    chance = round(score / 100.0, 2)
    why = f"SMA20>{'SMA50' if (sma20 and sma50 and sma20 > sma50) else 'SMA50?'} | RSI={rsi14:.1f}" if rsi14 else "Basic trend/momentum"

    return Signal(
        symbol=symbol,
        label=label,
        score=score,
        chance=chance,
        entry=entry,
        stop=stop,
        target=target,
        setup_type=setup_type,
        regime=regime,
        timeframe=TF_SIGNAL,
        why=why,
    )


# -----------------------------
# Dedup / Fingerprints (DB)
# -----------------------------
def make_fingerprint(sig: Signal) -> str:
    # dezelfde trade = zelfde coin + entry + target + setup_type
    # rond af om microverschillen te vermijden
    e = round(sig.entry, 6)
    t = round(sig.target, 6)
    return f"{sig.symbol}|{sig.setup_type}|{e}|{t}"

def is_duplicate_recent(fp: str) -> bool:
    """
    Check in trade_fingerprints of deze fingerprint recent is aangemaakt.
    Geen ON CONFLICT afhankelijkheid; puur SELECT.
    """
    try:
        row = db_fetchone(
            "SELECT last_created_at FROM trade_fingerprints WHERE fingerprint=%s",
            (fp,),
        )
        if not row or not row[0]:
            return False
        last_dt = row[0]
        # last_dt kan naive/tz zijn, psycopg2 geeft meestal tz-aware
        cutoff = now_utc() - timedelta(seconds=TRADE_COOLDOWN_SECONDS)
        return last_dt >= cutoff
    except Exception:
        return False

def upsert_fingerprint(fp: str) -> None:
    """
    Geen ON CONFLICT, want jouw error liet zien dat constraints soms ontbreken.
    We doen: UPDATE -> als 0 rows dan INSERT. Bij INSERT-fail -> ignore.
    """
    try:
        conn = db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE trade_fingerprints SET last_created_at=%s WHERE fingerprint=%s",
                    (now_utc(), fp),
                )
                updated = cur.rowcount > 0
            conn.commit()

            if not updated:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO trade_fingerprints(fingerprint, last_created_at) VALUES (%s, %s)",
                        (fp, now_utc()),
                    )
                conn.commit()
        finally:
            conn.close()
    except Exception:
        # als table/kolom (nog) niet klopt: liever geen crash
        pass


# -----------------------------
# Prebuy counter per dag (DB)
# -----------------------------
def get_day_key() -> str:
    # UTC day key
    return now_utc().strftime("%Y-%m-%d")

def get_created_today(day_key: str) -> int:
    try:
        row = db_fetchone("SELECT created_count FROM prebuy_state WHERE day=%s", (day_key,))
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        return 0
    return 0

def set_created_today(day_key: str, new_val: int) -> None:
    try:
        conn = db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE prebuy_state SET created_count=%s WHERE day=%s", (new_val, day_key))
                updated = cur.rowcount > 0
            conn.commit()

            if not updated:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO prebuy_state(day, created_count) VALUES (%s, %s)",
                        (day_key, new_val),
                    )
                conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# -----------------------------
# Insert pending_approvals (robust)
# -----------------------------
def insert_pending(sig: Signal, prebuy_id: str) -> None:
    cols = get_table_columns("pending_approvals")

    created_at = now_utc()
    expires_at = created_at + timedelta(seconds=PREBUY_VALID_SECONDS)

    payload: Dict[str, Any] = {
        "id": prebuy_id,
        "symbol": sig.symbol,
        "label": sig.label,
        "score": sig.score,
        "chance": sig.chance,
        "confidence": int(round(sig.chance * 100)),
        "status": "PENDING",
        "created_at": created_at,
        "expires_at": expires_at,
        "approved_at": None,
        "rejected_at": None,
        "consumed_at": None,
        "timeframe": sig.timeframe,
        "setup_type": sig.setup_type,
        "regime": sig.regime,
        "entry": sig.entry,
        "stop": sig.stop,
        "target": sig.target,
        "why": sig.why,
        "source": "multi_coin_score",
    }

    # Filter alleen bestaande kolommen
    clean = {k: v for k, v in payload.items() if k in cols}

    if not clean:
        raise RuntimeError("pending_approvals schema niet herkend (geen overlap in kolommen).")

    keys = list(clean.keys())
    values = [clean[k] for k in keys]
    placeholders = ", ".join(["%s"] * len(keys))

    q = f"INSERT INTO pending_approvals ({', '.join(keys)}) VALUES ({placeholders})"

    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(q, tuple(values))
        conn.commit()
    finally:
        conn.close()


# -----------------------------
# Main
# -----------------------------
def main() -> int:
    scoreboard = load_scoreboard()

    day_key = get_day_key()
    created_today = get_created_today(day_key)

    # Universe bepalen
    if AUTO_UNIVERSE:
        coins = get_auto_universe(day_key)
    else:
        coins = [c.strip().upper() for c in DEFAULT_COINS.split(",") if c.strip()]

    log(f"🚀 multi_coin_score start | created_today={created_today}/{MAX_PREBUY_PER_DAY} | coins={len(coins)}")

    created = 0
    for sym in coins:
        if created_today + created >= MAX_PREBUY_PER_DAY and not FORCE_TEST_PREBUY:
            break

        try:
            sig = build_signal(sym, scoreboard)
            if not sig:
                continue

            fp = make_fingerprint(sig)
            if is_duplicate_recent(fp) and not FORCE_TEST_PREBUY:
                continue

            prebuy_id = f"PB-{sig.symbol}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
            insert_pending(sig, prebuy_id)
            upsert_fingerprint(fp)

            created += 1
            log(f"✅ created: {prebuy_id} | {sig.symbol} | {sig.label} | score={sig.score} | chance={sig.chance}")

        except Exception as e:
            # No crash: log en door
            log(f"⚠️ ERROR {sym}: {e}")
            # optioneel: log stacktrace (uitcommenten als te noisy)
            # log(traceback.format_exc())
            continue

    # update day counter
    if created > 0:
        set_created_today(day_key, created_today + created)

    log(f"done. created={created}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        log("💥 FATAL:\n" + traceback.format_exc())
        raise
