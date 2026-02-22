from __future__ import annotations

import os
import time
import math
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras


# ==========================================================
# ENV / SETTINGS
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL ontbreekt. Zet DATABASE_URL in Render Environment Variables.")

EXCHANGE = (os.getenv("EXCHANGE") or "binance").strip()

# Universe
AUTO_UNIVERSE = (os.getenv("AUTO_UNIVERSE") or "1").strip() == "1"
UNIVERSE_LIMIT = int(os.getenv("UNIVERSE_LIMIT") or "250")  # max symbols per run
SYMBOLS_ENV = (os.getenv("SYMBOLS") or "").strip()          # fallback als AUTO_UNIVERSE=0
ONLY_QUOTE = (os.getenv("ONLY_QUOTE") or "USDT").strip().upper()

# Timeframes
TF_MAIN = (os.getenv("TF_MAIN") or "4h").strip()     # scoring + regime basis
TF_CTX = (os.getenv("TF_CTX") or "1h").strip()       # entry context

LOOKBACK_MAIN = int(os.getenv("LOOKBACK_MAIN") or "320")  # genoeg voor SMA200 + checks
LOOKBACK_CTX = int(os.getenv("LOOKBACK_CTX") or "220")

# Prebuy caps
MAX_PREBUY_PER_DAY = int(os.getenv("MAX_PREBUY_PER_DAY") or "5")
PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))
TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS") or str(6 * 60 * 60))

# Thresholds
MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "82")  # GO
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "72")          # WATCH (default niet opslaan)
INCLUDE_WATCH_IN_PENDING = (os.getenv("INCLUDE_WATCH_IN_PENDING") or "0").strip() == "1"
FORCE_TEST_PREBUY = (os.getenv("FORCE_TEST_PREBUY") or "0").strip() == "1"

SETUP_TYPE_DEFAULT = (os.getenv("SETUP_TYPE_DEFAULT") or "TREND_PULLBACK").strip()

# Smart filters (kwaliteit verhogen)
# Liquidity: gebruik quote_volume als beschikbaar, anders volume
MIN_AVG_QUOTE_VOL_MAIN = float(os.getenv("MIN_AVG_QUOTE_VOL_MAIN") or "0")  # bv 500000 (0 = uit)
VOLATILITY_MAX = float(os.getenv("VOLATILITY_MAX") or "0.05")               # gemiddeld abs return over N candles
VOLATILITY_WINDOW = int(os.getenv("VOLATILITY_WINDOW") or "20")

# Overextended filter (geen FOMO): last te ver boven SMA20 => skip
OVEREXT_MAX = float(os.getenv("OVEREXT_MAX") or "0.08")  # 8%

# Trend alignment: 4h trend moet up, 1h trend liefst ook up
REQUIRE_4H_TREND_UP = (os.getenv("REQUIRE_4H_TREND_UP") or "1").strip() == "1"
REQUIRE_1H_ALIGN = (os.getenv("REQUIRE_1H_ALIGN") or "1").strip() == "1"

# RSI sweet spot
RSI_IDEAL_LOW = float(os.getenv("RSI_IDEAL_LOW") or "45")
RSI_IDEAL_HIGH = float(os.getenv("RSI_IDEAL_HIGH") or "60")
RSI_ACCEPT_LOW = float(os.getenv("RSI_ACCEPT_LOW") or "40")
RSI_ACCEPT_HIGH = float(os.getenv("RSI_ACCEPT_HIGH") or "65")

# Regime impact (zonder trades hard te blokkeren)
REGIME_BEAR_PENALTY = int(os.getenv("REGIME_BEAR_PENALTY") or "15")
REGIME_RANGE_PENALTY = int(os.getenv("REGIME_RANGE_PENALTY") or "5")
REGIME_BULL_BONUS = int(os.getenv("REGIME_BULL_BONUS") or "5")

# Stop/Target (alleen voor Pre-BUY voorstel; exit logic blijft in trade_monitor)
STOP_PCT = float(os.getenv("STOP_PCT") or "0.02")      # 2% stop
TARGET_PCT = float(os.getenv("TARGET_PCT") or "0.04")  # 4% target ~ 2R bij 2% stop

# DB efficiency
# We halen candles in 2 queries (TF_MAIN + TF_CTX) met window function.
# Let op: bij heel veel symbols kan dit zwaar zijn; UNIVERSE_LIMIT houdt dit beheersbaar.


# ==========================================================
# UTIL
# ==========================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(msg, flush=True)


def db_connect():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def clamp_int(x: float, lo: int = 0, hi: int = 100) -> int:
    return int(max(lo, min(hi, round(x))))


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def utc_day_key(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def make_prebuy_id(symbol: str) -> str:
    # kort, maar uniek genoeg
    return f"PB-{symbol}-{int(time.time())}"


# ==========================================================
# INDICATORS
# ==========================================================
def sma(values: List[float], n: int) -> Optional[float]:
    if n <= 0 or len(values) < n:
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


def avg_abs_return(values: List[float], window: int) -> Optional[float]:
    """
    simpele volatility proxy: gemiddelde absolute return over laatste window candles
    """
    if window <= 1 or len(values) < window + 1:
        return None
    rets = []
    for i in range(-window, 0):
        prev = values[i - 1]
        cur = values[i]
        if prev == 0:
            continue
        rets.append(abs((cur - prev) / prev))
    if not rets:
        return None
    return sum(rets) / len(rets)


def overextended(last: float, s20: float) -> float:
    if s20 == 0:
        return 0.0
    return (last - s20) / abs(s20)


# ==========================================================
# DB: ensure minimal tables
# ==========================================================
def ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        # daily cap table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.prebuy_state (
              day TEXT PRIMARY KEY,
              created_count INTEGER NOT NULL DEFAULT 0,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # fingerprints
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.trade_fingerprints (
              fingerprint TEXT PRIMARY KEY,
              last_created_at TIMESTAMPTZ
            );
            """
        )

        # pending_approvals (minimal)
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
            );
            """
        )

        # market_regime table wordt door regime_labeler gebouwd,
        # maar we maken hier geen harde dependency van (alleen lezen).
    conn.commit()


# ==========================================================
# DB: universe + batch candles fetch
# ==========================================================
def fetch_universe(conn) -> List[str]:
    """
    AUTO_UNIVERSE:
      haalt symbols uit candles (TF_MAIN) + quote filter (USDT)
    """
    if not AUTO_UNIVERSE:
        if not SYMBOLS_ENV:
            return []
        return [s.strip().upper() for s in SYMBOLS_ENV.split(",") if s.strip()]

    like = f"%{ONLY_QUOTE}" if ONLY_QUOTE else "%"

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT symbol
            FROM public.candles
            WHERE exchange = %s
              AND timeframe = %s
              AND symbol LIKE %s
            ORDER BY symbol ASC
            LIMIT %s;
            """,
            (EXCHANGE, TF_MAIN, like, UNIVERSE_LIMIT),
        )
        rows = cur.fetchall()
    return [r[0] for r in rows if r and r[0]]


def fetch_candles_batch(
    conn,
    symbols: List[str],
    timeframe: str,
    limit_per_symbol: int,
) -> Dict[str, Dict[str, List[float]]]:
    """
    1 query: haalt per symbol de laatste N candles op (DESC), via window function.
    We halen columns: close, volume, quote_volume (als aanwezig; anders None), open_time.

    Return:
      {symbol: {"close":[...], "volume":[...], "quote_volume":[...]}}
    """
    if not symbols:
        return {}

    sql = """
    WITH ranked AS (
      SELECT
        symbol,
        close,
        volume,
        quote_volume,
        open_time,
        ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY open_time DESC) AS rn
      FROM public.candles
      WHERE exchange = %s
        AND timeframe = %s
        AND symbol = ANY(%s)
    )
    SELECT symbol, close, volume, quote_volume
    FROM ranked
    WHERE rn <= %s
    ORDER BY symbol ASC, rn DESC;
    """
    # rn DESC => oudste -> nieuwste binnen de N (want rn=limit is oudste, rn=1 is nieuwste)

    out: Dict[str, Dict[str, List[float]]] = {}

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (EXCHANGE, timeframe, symbols, limit_per_symbol))
        rows = cur.fetchall()

    for r in rows:
        sym = r["symbol"]
        out.setdefault(sym, {"close": [], "volume": [], "quote_volume": []})
        out[sym]["close"].append(float(r["close"]))
        out[sym]["volume"].append(float(r["volume"]) if r["volume"] is not None else 0.0)
        out[sym]["quote_volume"].append(float(r["quote_volume"]) if r["quote_volume"] is not None else 0.0)

    return out


def fetch_latest_regimes(conn, symbols: List[str], timeframe: str = "4h") -> Dict[str, Dict[str, Any]]:
    """
    1 query: haal latest regime per symbol.
    Return {symbol: {"regime":..., "score":..., "asof_ts":...}}
    """
    if not symbols:
        return {}

    # DISTINCT ON haalt nieuwste per symbol
    sql = """
    SELECT DISTINCT ON (symbol)
      symbol, regime, score, asof_ts
    FROM public.market_regime
    WHERE exchange = %s
      AND timeframe = %s
      AND symbol = ANY(%s)
    ORDER BY symbol, asof_ts DESC;
    """

    out: Dict[str, Dict[str, Any]] = {}
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (EXCHANGE, timeframe, symbols))
            rows = cur.fetchall()
        for r in rows:
            out[r["symbol"]] = {
                "regime": (r.get("regime") or "UNKNOWN"),
                "score": r.get("score"),
                "asof_ts": r.get("asof_ts"),
            }
    except Exception:
        # market_regime bestaat misschien nog niet → geen hard fail
        return {}

    return out


# ==========================================================
# DB: daily cap + dedupe + insert
# ==========================================================
def get_created_today(conn, day_key: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT created_count FROM public.prebuy_state WHERE day=%s", (day_key,))
        row = cur.fetchone()
    return int(row[0]) if row else 0


def set_created_today(conn, day_key: str, created_count: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.prebuy_state(day, created_count, updated_at)
            VALUES(%s, %s, NOW())
            ON CONFLICT(day) DO UPDATE SET
              created_count = EXCLUDED.created_count,
              updated_at = NOW();
            """,
            (day_key, created_count),
        )
    conn.commit()


def make_fingerprint(symbol: str, setup_type: str, entry: float, target: float) -> str:
    return f"{symbol}|{setup_type}|{round(entry, 8)}|{round(target, 8)}"


def fingerprint_exists_recent(conn, fp: str, cooldown_seconds: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT last_created_at FROM public.trade_fingerprints WHERE fingerprint=%s", (fp,))
        row = cur.fetchone()
    if not row or not row[0]:
        return False
    last_ts: datetime = row[0]
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    age = (now_utc() - last_ts.astimezone(timezone.utc)).total_seconds()
    return age < cooldown_seconds


def upsert_fingerprint(conn, fp: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.trade_fingerprints(fingerprint, last_created_at)
            VALUES(%s, NOW())
            ON CONFLICT(fingerprint) DO UPDATE SET last_created_at = NOW();
            """,
            (fp,),
        )
    conn.commit()


def insert_pending(conn, payload: Dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.pending_approvals(
              id, symbol, setup_type, timeframe, regime,
              score, chance, confidence,
              entry, stop, target,
              status, created_at, expires_at
            )
            VALUES(
              %(id)s, %(symbol)s, %(setup_type)s, %(timeframe)s, %(regime)s,
              %(score)s, %(chance)s, %(confidence)s,
              %(entry)s, %(stop)s, %(target)s,
              %(status)s, %(created_at)s, %(expires_at)s
            )
            ON CONFLICT (id) DO NOTHING;
            """,
            payload,
        )
    conn.commit()


# ==========================================================
# SCORING (slimmer + winrate)
# ==========================================================
@dataclass
class ScoreDetail:
    score: int
    confidence: int
    label: str  # GO / WATCH / SKIP
    reason: str
    regime: str


def compute_score_smart(
    closes4h: List[float],
    closes1h: List[float],
    vols4h: List[float],
    qvols4h: List[float],
    regime: str,
) -> ScoreDetail:
    """
    Doel:
      - hogere winrate door betere selectie
      - geen exit/stop wijziging (alleen selectie)
    """

    if len(closes4h) < 80 or len(closes1h) < 80:
        return ScoreDetail(0, 0, "SKIP", "not enough candles", regime)

    last4 = closes4h[-1]
    last1 = closes1h[-1]

    s20_4 = sma(closes4h, 20)
    s50_4 = sma(closes4h, 50)
    s20_1 = sma(closes1h, 20)
    s50_1 = sma(closes1h, 50)
    r_4 = rsi(closes4h, 14)

    if s20_4 is None or s50_4 is None or s20_1 is None or s50_1 is None or r_4 is None:
        return ScoreDetail(0, 0, "SKIP", "indicator missing", regime)

    # -----------------------------
    # 1) Trend gating (winrate ↑)
    # -----------------------------
    trend4_up = s20_4 > s50_4
    trend1_up = s20_1 > s50_1

    if REQUIRE_4H_TREND_UP and not trend4_up:
        return ScoreDetail(0, 0, "SKIP", "4h trend down", regime)

    if REQUIRE_1H_ALIGN and not trend1_up:
        return ScoreDetail(0, 0, "SKIP", "1h not aligned", regime)

    # -----------------------------
    # 2) Liquidity filter
    # -----------------------------
    # quote_volume is in USDT; als leeg -> gebruik volume als proxy.
    if MIN_AVG_QUOTE_VOL_MAIN > 0:
        if any(q != 0.0 for q in qvols4h[-min(30, len(qvols4h)):]):
            avg_qv = sum(qvols4h[-min(30, len(qvols4h)):]) / float(min(30, len(qvols4h)))
        else:
            avg_qv = sum(vols4h[-min(30, len(vols4h)):]) / float(min(30, len(vols4h)))
        if avg_qv < MIN_AVG_QUOTE_VOL_MAIN:
            return ScoreDetail(0, 0, "SKIP", f"liquidity low avg={avg_qv:.0f}", regime)

    # -----------------------------
    # 3) Volatility filter (casino eruit)
    # -----------------------------
    vol = avg_abs_return(closes4h, VOLATILITY_WINDOW)
    if vol is not None and vol > VOLATILITY_MAX:
        return ScoreDetail(0, 0, "SKIP", f"vol too high {vol*100:.2f}%", regime)

    # -----------------------------
    # 4) Overextended filter (geen FOMO)
    # -----------------------------
    ext = overextended(last4, s20_4)
    if ext > OVEREXT_MAX:
        return ScoreDetail(0, 0, "SKIP", f"overextended {ext*100:.1f}%", regime)

    # -----------------------------
    # 5) RSI sweet spot (timing)
    # -----------------------------
    # Ideal: 45-60. Accept: 40-65.
    if RSI_IDEAL_LOW <= r_4 <= RSI_IDEAL_HIGH:
        rsi_score = 35
        rsi_reason = "rsi ideal"
    elif RSI_ACCEPT_LOW <= r_4 <= RSI_ACCEPT_HIGH:
        rsi_score = 22
        rsi_reason = "rsi ok"
    else:
        return ScoreDetail(0, 0, "SKIP", f"rsi bad {r_4:.1f}", regime)

    # -----------------------------
    # 6) Build score (0-100)
    # -----------------------------
    score = 0

    # Trend strength (basis)
    score += 40  # gated al, dus je “verdient” dit

    # RSI
    score += rsi_score

    # Volatility good (als vol bekend is)
    if vol is not None:
        # hoe lager vol, hoe beter (tot een punt)
        # vol=0.01 -> +15, vol=0.04 -> +6, vol=0.05 -> +0
        bonus = clamp_int((VOLATILITY_MAX - vol) / VOLATILITY_MAX * 15.0, 0, 15)
        score += bonus
    else:
        score += 5

    # Overextension good (hoe dichter bij sma20, hoe beter)
    # ext 0% => +10, ext 8% => +0
    ext_bonus = clamp_int((OVEREXT_MAX - ext) / OVEREXT_MAX * 10.0, 0, 10)
    score += ext_bonus

    # -----------------------------
    # 7) Regime adjustment (B)
    # -----------------------------
    reg = (regime or "UNKNOWN").upper()
    if reg == "BEAR":
        score -= REGIME_BEAR_PENALTY
    elif reg == "RANGE":
        score -= REGIME_RANGE_PENALTY
    elif reg == "BULL":
        score += REGIME_BULL_BONUS

    score = clamp_int(score, 0, 100)

    # confidence: iets conservatiever dan score
    confidence = clamp_int(score * 0.9, 0, 100)

    # label
    if FORCE_TEST_PREBUY:
        label = "GO"
    elif score >= MIN_SCORE_TO_PREBUY:
        label = "GO"
    elif score >= WATCH_MIN_SCORE:
        label = "WATCH"
    else:
        label = "SKIP"

    reason = (
        f"trend4={'UP' if trend4_up else 'DOWN'} trend1={'UP' if trend1_up else 'DOWN'} | "
        f"{rsi_reason} rsi={r_4:.1f} | "
        f"vol={(vol*100):.2f}% | ext={(ext*100):.1f}% | regime={reg}"
    )

    return ScoreDetail(score=score, confidence=confidence, label=label, reason=reason, regime=reg)


# ==========================================================
# MAIN
# ==========================================================
def main() -> int:
    t0 = now_utc()
    day_key = utc_day_key(t0)

    try:
        with db_connect() as conn:
            ensure_tables(conn)

            created_today = get_created_today(conn, day_key)
            log(f"🧠 multi_coin_score | day={day_key} | created_today={created_today}/{MAX_PREBUY_PER_DAY}")

            if not FORCE_TEST_PREBUY and created_today >= MAX_PREBUY_PER_DAY:
                log("✅ Daglimiet bereikt. Stop run.")
                return 0

            # 1) universe
            symbols = fetch_universe(conn)
            if not symbols:
                log("❌ Geen symbols gevonden (candles leeg of AUTO_UNIVERSE=0 en SYMBOLS leeg).")
                return 1

            log(f"📌 universe={len(symbols)} (limit={UNIVERSE_LIMIT}) tf_main={TF_MAIN} tf_ctx={TF_CTX}")

            # 2) batch fetch candles (DB efficient: 2 queries)
            c_main = fetch_candles_batch(conn, symbols, TF_MAIN, LOOKBACK_MAIN)
            c_ctx = fetch_candles_batch(conn, symbols, TF_CTX, LOOKBACK_CTX)

            # 3) batch fetch latest regimes (1 query)
            regimes = fetch_latest_regimes(conn, symbols, timeframe=TF_MAIN)

            created_now = 0
            scanned = 0
            skipped_cap = 0
            skipped_dupe = 0
            skipped_low = 0

            for sym in symbols:
                scanned += 1

                if not FORCE_TEST_PREBUY and (created_today + created_now) >= MAX_PREBUY_PER_DAY:
                    skipped_cap += 1
                    break

                data4 = c_main.get(sym)
                data1 = c_ctx.get(sym)
                if not data4 or not data1:
                    skipped_low += 1
                    continue

                closes4 = data4["close"]
                closes1 = data1["close"]
                vols4 = data4["volume"]
                qvol4 = data4["quote_volume"]

                if len(closes4) < 80 or len(closes1) < 80:
                    skipped_low += 1
                    continue

                reg = regimes.get(sym, {}).get("regime") or "UNKNOWN"

                sd = compute_score_smart(
                    closes4h=closes4,
                    closes1h=closes1,
                    vols4h=vols4,
                    qvols4h=qvol4,
                    regime=reg,
                )

                if sd.label == "SKIP":
                    continue

                if sd.label == "WATCH" and not INCLUDE_WATCH_IN_PENDING and not FORCE_TEST_PREBUY:
                    # we loggen WATCH niet standaard als pending (scheelt noise)
                    continue

                # Entry/stop/target (alleen voorstel; echte trade logic blijft elders)
                entry = float(closes1[-1])
                stop = entry * (1.0 - STOP_PCT)
                target = entry * (1.0 + TARGET_PCT)

                # Dedup (zelfde coin+setup+entry+target niet opnieuw in cooldown)
                fp = make_fingerprint(sym, SETUP_TYPE_DEFAULT, entry, target)
                if not FORCE_TEST_PREBUY and fingerprint_exists_recent(conn, fp, TRADE_COOLDOWN_SECONDS):
                    skipped_dupe += 1
                    continue

                pb_id = make_prebuy_id(sym)

                payload = {
                    "id": pb_id,
                    "symbol": sym,
                    "setup_type": SETUP_TYPE_DEFAULT if sd.label == "GO" else "WATCHLIST",
                    "timeframe": TF_MAIN,
                    "regime": sd.regime,
                    "score": int(sd.score),
                    "chance": int(sd.score),        # simpel: chance = score (kan later scoreboard worden)
                    "confidence": int(sd.confidence),
                    "entry": float(entry),
                    "stop": float(stop),
                    "target": float(target),
                    "status": "PENDING",
                    "created_at": now_utc(),
                    "expires_at": now_utc() + timedelta(seconds=PREBUY_VALID_SECONDS),
                }

                insert_pending(conn, payload)
                upsert_fingerprint(conn, fp)

                created_now += 1
                log(
                    f"✅ PREBUY {sd.label} | {sym} | score={sd.score} conf={sd.confidence} "
                    f"| regime={sd.regime} | entry={entry:.6f} stop={stop:.6f} target={target:.6f}"
                )
                log(f"   ↳ {sd.reason}")

            # update daily counter
            set_created_today(conn, day_key, created_today + created_now)

            dt = (now_utc() - t0).total_seconds()
            log(
                f"🏁 DONE scanned={scanned} created={created_now} "
                f"skipped_dupe={skipped_dupe} skipped_low={skipped_low} skipped_cap={skipped_cap} "
                f"runtime={dt:.1f}s"
            )
            return 0

    except Exception as e:
        log("❌ FATAAL in multi_coin_score.py")
        log(f"{type(e).__name__}: {e}")
        log(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
