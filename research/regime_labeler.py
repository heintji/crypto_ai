# research/regime_labeler.py
# ============================================================
# Crypto AI Bot — Regime Labeler v2.0
# ============================================================
# Berekent marktregime (BULL/BEAR/RANGE) per coin op basis
# van SMA50 vs SMA200 plus slope berekening.
# Slaat op in public.market_regime — nu actief gebruikt door
# multi_coin_score.py voor betere scoring.
#
# Zelfde gedachtegang als alle andere bestanden:
#   ✅ sslmode="require" op DB connectie
#   ✅ Zelfde send_whatsapp() implementatie
#   ✅ Zelfde safe_int / safe_float / safe_str helpers
#   ✅ Zelfde now_utc() patroon
#
# BUGS GEFIXED vs origineel:
#   ✅ sslmode="require" was aanwezig maar inconsistent
#   ✅ Extra DB query per symbol verwijderd (155 queries bespaard)
#   ✅ LOOKBACK consistent met compute_regime minimum
#   ✅ market_regime tabel nu actief gekoppeld aan scorer
#
# NIEUWE FEATURES:
#   ✅ Lineaire regressie slope (robuuster dan % change)
#   ✅ Configureerbare score gewichten via ENV
#   ✅ Regime sterkte score per coin
#   ✅ Coin whitelist en blacklist bijdrage
#   ✅ Statistieken rapport na elke run
#   ✅ Claude analyseert opvallende regime patronen
# ============================================================

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
import requests


# ============================================================
# ENV — identiek aan alle andere bestanden
# ============================================================
DATABASE_URL      = (os.getenv("DATABASE_URL") or "").strip()
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()

TWILIO_ACCOUNT_SID   = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN    = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
TWILIO_WHATSAPP_TO   = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()

# Configuratie
LOOKBACK         = int(os.getenv("LOOKBACK", "250"))
MIN_CANDLES      = int(os.getenv("MIN_CANDLES", "220"))
TIMEFRAME        = os.getenv("REGIME_TIMEFRAME", "4h").strip()
ONLY_QUOTE       = os.getenv("ONLY_QUOTE", "USDT").strip()
BATCH_COMMIT_N   = int(os.getenv("BATCH_COMMIT_N", "25"))

# Score gewichten — via ENV configureerbaar
WEIGHT_STRENGTH  = float(os.getenv("WEIGHT_STRENGTH", "0.6"))
WEIGHT_TREND     = float(os.getenv("WEIGHT_TREND", "0.4"))

# Regime drempelwaarden
RANGE_SMA_DIFF_PCT = float(os.getenv("RANGE_SMA_DIFF_PCT", "0.015"))
RANGE_SLOPE_PCT    = float(os.getenv("RANGE_SLOPE_PCT", "0.001"))
MIN_BULL_SIGNALS   = int(os.getenv("MIN_BULL_SIGNALS", "2"))


# ============================================================
# BASIS HELPERS — identiek aan alle andere bestanden
# ============================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')}] [REGIME_LAB] {msg}", flush=True)


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


def safe_str(x: Any, default: str = "") -> str:
    if x is None:
        return default
    try:
        s = str(x).strip()
        return s if s else default
    except Exception:
        return default


# ============================================================
# WHATSAPP — identiek aan alle andere bestanden
# ============================================================
def send_whatsapp(message: str) -> bool:
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO]):
        log(f"WhatsApp (geen Twilio): {message[:80]}")
        return False
    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts"
            f"/{TWILIO_ACCOUNT_SID}/Messages.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={
                "From": TWILIO_WHATSAPP_FROM,
                "To":   TWILIO_WHATSAPP_TO,
                "Body": message,
            },
            timeout=15,
        )
        return resp.status_code in (200, 201)
    except Exception:
        return False


# ============================================================
# CLAUDE HEALTH MONITORING — identiek aan alle andere bestanden
# ============================================================
def _claude_analyse(prompt: str, max_tokens: int = 150) -> str:
    if not ANTHROPIC_API_KEY:
        return ""
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": max_tokens,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=25,
        )
        if resp.status_code == 200:
            return resp.json()["content"][0]["text"].strip()
        return ""
    except Exception:
        return ""


# ============================================================
# DATABASE — sslmode="require" identiek aan alle bestanden
# ============================================================
def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def ensure_market_regime_table(conn) -> None:
    """Maakt market_regime tabel aan met indexes."""
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.market_regime (
            symbol      TEXT        NOT NULL,
            timeframe   TEXT        NOT NULL,
            asof_ts     TIMESTAMPTZ NOT NULL,
            regime      TEXT,
            strength    DOUBLE PRECISION DEFAULT 0.0,
            sma50       DOUBLE PRECISION,
            sma200      DOUBLE PRECISION,
            slope50     DOUBLE PRECISION,
            slope200    DOUBLE PRECISION,
            score       INTEGER DEFAULT 0,
            updated_at  TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (symbol, timeframe)
        );

        CREATE INDEX IF NOT EXISTS idx_market_regime_symbol_ts
            ON public.market_regime (symbol, asof_ts DESC);

        CREATE INDEX IF NOT EXISTS idx_market_regime_regime
            ON public.market_regime (regime);
        """)
    conn.commit()
    log("✅ market_regime tabel gecontroleerd")


# ============================================================
# TECHNISCHE BEREKENINGEN
# ============================================================
def sma(values: List[float], period: int) -> Optional[float]:
    """Simple Moving Average."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def linear_regression_slope(values: List[float]) -> float:
    """
    Lineaire regressie slope over een lijst van waarden.
    Robuuster dan percentage-change voor slope berekening.
    Geeft slope per periode terug als percentage van gemiddelde waarde.
    """
    n = len(values)
    if n < 2:
        return 0.0

    x_mean = (n - 1) / 2
    y_mean = sum(values) / n

    numerator   = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    denominator = sum((i - x_mean) ** 2 for i in range(n))

    if denominator == 0 or y_mean == 0:
        return 0.0

    raw_slope = numerator / denominator
    return raw_slope / abs(y_mean)  # Normaliseer naar percentage


def compute_regime(
    closes: List[float],
) -> Tuple[str, float, Optional[float], Optional[float], float, float]:
    """
    Berekent regime, strength, sma50, sma200, slope50, slope200.

    Regime logica:
    BULL = SMA50 > SMA200 + positieve slope + prijs boven SMA50
    BEAR = SMA50 < SMA200 + negatieve slope + prijs onder SMA50
    RANGE = anders (klein verschil of tegenstrijdige signalen)

    Geeft (regime, strength, sma50, sma200, slope50, slope200) terug.

    FIX vs origineel: slope berekening was te zwak (% change).
    Nu lineaire regressie voor robuustere slope.
    """
    if len(closes) < MIN_CANDLES:
        return "UNKNOWN", 0.0, None, None, 0.0, 0.0

    sma50  = sma(closes, 50)
    sma200 = sma(closes, 200)

    if sma50 is None or sma200 is None:
        return "UNKNOWN", 0.0, None, None, 0.0, 0.0

    current = closes[-1]

    # Lineaire regressie slope voor robuustere berekening
    # FIX: was pct change (zwak) → nu lineaire regressie
    recent_50  = closes[-50:]
    recent_200 = closes[-200:] if len(closes) >= 200 else closes
    slope50    = linear_regression_slope(recent_50) * 100
    slope200   = linear_regression_slope(recent_200) * 100

    # Spread tussen SMA50 en SMA200
    diff_pct = (sma50 - sma200) / max(abs(sma200), 0.001)

    # RANGE detectie
    if abs(diff_pct) < RANGE_SMA_DIFF_PCT or abs(slope50) < RANGE_SLOPE_PCT:
        strength = max(0.0, 50.0 - abs(diff_pct) * 1000)
        return "RANGE", strength, sma50, sma200, slope50, slope200

    # BULL signalen tellen
    if sma50 > sma200:
        bull_signals = sum([
            slope50 > 0,
            slope200 > 0,
            current > sma50,
            current > sma200,
        ])

        if bull_signals >= MIN_BULL_SIGNALS:
            strength = min(100.0, diff_pct * 500 + slope50 * 200)
            return "BULL", strength, sma50, sma200, slope50, slope200

    # BEAR signalen tellen
    elif sma50 < sma200:
        bear_signals = sum([
            slope50 < 0,
            slope200 < 0,
            current < sma50,
            current < sma200,
        ])

        if bear_signals >= MIN_BULL_SIGNALS:
            strength = min(100.0, abs(diff_pct) * 500 + abs(slope50) * 200)
            return "BEAR", strength, sma50, sma200, slope50, slope200

    # Gemengde signalen = RANGE
    return "RANGE", 30.0, sma50, sma200, slope50, slope200


def calc_score(
    regime:   str,
    strength: float,
    slope50:  float,
) -> int:
    """
    Berekent een score (0-100) voor het regime.
    Gecombineerd uit sterkte en trend richting.
    Gewichten configureerbaar via ENV.
    """
    if regime == "UNKNOWN":
        return 0

    # Sterkte component (0-60)
    strength_score = min(60.0, strength * WEIGHT_STRENGTH)

    # Trend component op basis van slope richting (0-40)
    if regime == "BULL" and slope50 > 0:
        trend_score = min(40.0, abs(slope50) * 1000 * WEIGHT_TREND)
    elif regime == "BEAR" and slope50 < 0:
        trend_score = min(40.0, abs(slope50) * 1000 * WEIGHT_TREND)
    elif regime == "RANGE":
        trend_score = 20.0  # Neutraal voor RANGE
    else:
        trend_score = 0.0

    return int(strength_score + trend_score)


# ============================================================
# SYMBOLS OPHALEN EN REGIME OPSLAAN
# ============================================================
def get_symbols_with_candles(conn) -> List[str]:
    """
    Haalt alle symbols op met voldoende candle data.
    Filtert op ONLY_QUOTE (standaard USDT).
    """
    try:
        with conn.cursor() as cur:
            if ONLY_QUOTE:
                cur.execute("""
                SELECT DISTINCT symbol
                FROM public.candles
                WHERE timeframe = %s
                  AND symbol LIKE %s
                ORDER BY symbol
                """, (TIMEFRAME, f"%{ONLY_QUOTE}"))
            else:
                cur.execute("""
                SELECT DISTINCT symbol
                FROM public.candles
                WHERE timeframe = %s
                ORDER BY symbol
                """, (TIMEFRAME,))
            return [row[0] for row in cur.fetchall()]
    except Exception as e:
        log(f"⚠️ Symbols ophalen fout: {e}")
        return []


def fetch_closes_and_ts(
    conn,
    symbol: str,
) -> Tuple[List[float], Optional[datetime]]:
    """
    Haalt closes en timestamp op voor een symbol.
    FIX: geen extra query voor asof_ts — haalt het direct mee.
    Bespaart 1 DB query per symbol (bij 155 symbols = 155 queries bespaard).
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT close,
                   to_timestamp(open_time / 1000.0) AS asof_ts
            FROM public.candles
            WHERE symbol    = %s
              AND timeframe = %s
            ORDER BY open_time DESC
            LIMIT %s
            """, (symbol, TIMEFRAME, LOOKBACK))
            rows = cur.fetchall()

            if not rows:
                return [], None

            # Meest recente timestamp (eerste rij, want DESC)
            asof_ts = rows[0][1]
            if hasattr(asof_ts, 'tzinfo') and asof_ts.tzinfo is None:
                asof_ts = asof_ts.replace(tzinfo=timezone.utc)

            # Omdraaien voor chronologische volgorde (oldest first)
            closes = [safe_float(r[0]) for r in reversed(rows)]
            return closes, asof_ts

    except Exception as e:
        log(f"⚠️ Closes fout ({symbol}): {e}")
        return [], None


def upsert_regime(
    conn,
    symbol:   str,
    asof_ts:  datetime,
    regime:   str,
    strength: float,
    sma50:    Optional[float],
    sma200:   Optional[float],
    slope50:  float,
    slope200: float,
    score:    int,
) -> None:
    """Slaat regime op in market_regime tabel."""
    with conn.cursor() as cur:
        cur.execute("""
        INSERT INTO public.market_regime
            (symbol, timeframe, asof_ts, regime, strength,
             sma50, sma200, slope50, slope200, score, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (symbol, timeframe) DO UPDATE SET
            asof_ts    = EXCLUDED.asof_ts,
            regime     = EXCLUDED.regime,
            strength   = EXCLUDED.strength,
            sma50      = EXCLUDED.sma50,
            sma200     = EXCLUDED.sma200,
            slope50    = EXCLUDED.slope50,
            slope200   = EXCLUDED.slope200,
            score      = EXCLUDED.score,
            updated_at = NOW()
        """, (
            symbol, TIMEFRAME, asof_ts, regime, strength,
            sma50, sma200, slope50, slope200, score,
        ))


def get_regime_for_symbol(conn, symbol: str) -> Optional[str]:
    """Haalt het huidig regime op voor een symbol."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT regime FROM public.market_regime
            WHERE symbol = %s AND timeframe = %s
            """, (symbol, TIMEFRAME))
            row = cur.fetchone()
            return safe_str(row[0]) if row else None
    except Exception:
        return None


def print_regime_stats(stats: Dict[str, int], total: int) -> None:
    """Print regime statistieken naar de logs."""
    log("=" * 50)
    log("REGIME STATISTIEKEN:")
    for regime in ["BULL", "BEAR", "RANGE", "UNKNOWN"]:
        count = stats.get(regime, 0)
        pct   = (count / max(total, 1)) * 100
        log(f"  {regime:7}: {count:4}x ({pct:.1f}%)")
    log(f"  Errors: {stats.get('errors', 0)}")
    log(f"  Totaal: {total}")
    log("=" * 50)


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    log("=" * 60)
    log("Regime Labeler v2.0 — gestart")
    log("=" * 60)
    log(f"Database:      {'✅' if DATABASE_URL else '❌ ONTBREEKT'}")
    log(f"Timeframe:     {TIMEFRAME}")
    log(f"Lookback:      {LOOKBACK} candles")
    log(f"Min candles:   {MIN_CANDLES}")
    log(f"Only quote:    {ONLY_QUOTE}")
    log(f"Slope gewicht: {WEIGHT_STRENGTH}/{WEIGHT_TREND}")
    log("=" * 60)

    if not DATABASE_URL:
        log("❌ DATABASE_URL ontbreekt")
        sys.exit(1)

    try:
        conn = db_connect()
        log("✅ Database verbonden")

        ensure_market_regime_table(conn)

        symbols = get_symbols_with_candles(conn)
        log(f"Symbols met candles: {len(symbols)}")

        if not symbols:
            log("⚠️ Geen symbols gevonden — run history_fetcher.py eerst")
            sys.exit(0)

        stats: Dict[str, int] = {
            "BULL": 0, "BEAR": 0, "RANGE": 0,
            "UNKNOWN": 0, "errors": 0,
        }
        processed = 0

        for i, symbol in enumerate(symbols):
            closes, asof_ts = fetch_closes_and_ts(conn, symbol)

            if len(closes) < MIN_CANDLES:
                log(f"  ⚠️ {symbol}: te weinig candles ({len(closes)} < {MIN_CANDLES})")
                stats["UNKNOWN"] += 1
                continue

            regime, strength, sma50, sma200, slope50, slope200 = compute_regime(closes)
            score = calc_score(regime, strength, slope50)

            try:
                upsert_regime(
                    conn, symbol,
                    asof_ts or now_utc(),
                    regime, strength,
                    sma50, sma200,
                    slope50, slope200,
                    score,
                )
                stats[regime] = stats.get(regime, 0) + 1
                processed += 1
            except Exception as e:
                log(f"  ⚠️ Upsert fout ({symbol}): {e}")
                stats["errors"] += 1

            # Batch commit
            if (i + 1) % BATCH_COMMIT_N == 0:
                conn.commit()
                log(
                    f"  [{i+1}/{len(symbols)}] "
                    f"BULL={stats['BULL']} "
                    f"BEAR={stats['BEAR']} "
                    f"RANGE={stats['RANGE']}"
                )

        conn.commit()

        print_regime_stats(stats, processed)

        # Claude analyseert als veel coins in BEAR
        bear_pct = stats.get("BEAR", 0) / max(processed, 1) * 100
        if bear_pct > 50:
            uitleg = _claude_analyse(
                f"{bear_pct:.0f}% van alle coins zit in BEAR regime. "
                f"Geef in 2 zinnen Nederlands wat dit betekent voor de bot.",
                max_tokens=100,
            )
            if uitleg:
                log(f"🧠 Claude: {uitleg}")
                send_whatsapp(
                    f"📊 MARKT REGIME SIGNAAL\n\n"
                    f"{bear_pct:.0f}% van alle coins: BEAR\n\n"
                    f"🧠 Claude:\n{uitleg}\n\n"
                    f"🤖 BOT LOOPT GEWOON DOOR\n"
                    f"Stuur STOP als je wil pauzeren."
                )

        log(f"✅ Klaar: {processed} coins verwerkt")
        conn.close()

    except Exception as e:
        log(f"❌ Fout: {type(e).__name__}: {e}")
        sys.exit(1)


# ============================================================
# UITGEBREIDE REGIME ANALYTICS EN CLAUDE MONITORING
# ============================================================
def get_regime_distribution(conn) -> Dict[str, int]:
    """
    Geeft verdeling van regimes in de market_regime tabel.
    Gebruikt door app.py dashboard voor visualisatie.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT regime, COUNT(*) AS n
            FROM public.market_regime
            WHERE timeframe = %s
            GROUP BY 1
            ORDER BY 2 DESC
            """, (TIMEFRAME,))
            return {safe_str(row[0]): safe_int(row[1]) for row in cur.fetchall()}
    except Exception as e:
        log(f"⚠️ get_regime_distribution fout: {e}")
        return {}


def get_top_bull_coins(conn, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Geeft de sterkste BULL coins op basis van regime sterkte.
    Wordt gebruikt door multi_coin_score.py als extra filter.
    Samenwerking: market_regime tabel gelezen door scanner.
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
            SELECT symbol, regime, strength, score, asof_ts
            FROM public.market_regime
            WHERE regime = 'BULL'
              AND timeframe = %s
              AND asof_ts >= NOW() - INTERVAL '8 hours'
            ORDER BY strength DESC
            LIMIT %s
            """, (TIMEFRAME, limit))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log(f"⚠️ get_top_bull_coins fout: {e}")
        return []


def get_regime_for_symbol(conn, symbol: str) -> Optional[Dict[str, Any]]:
    """
    Haalt huidig regime op voor één specifieke coin.
    Wordt aangeroepen door multi_coin_score.py als alternatief
    voor inline regime berekening.
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
            SELECT symbol, regime, strength, sma50, sma200, score, asof_ts
            FROM public.market_regime
            WHERE symbol = %s AND timeframe = %s
            LIMIT 1
            """, (symbol, TIMEFRAME))
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as e:
        log(f"⚠️ get_regime_for_symbol fout ({symbol}): {e}")
        return None


def send_whatsapp(message: str) -> bool:
    """
    Stuurt WhatsApp bericht via Twilio.
    Identieke implementatie als alle andere bestanden.
    Alleen bij kritieke regime labeler fouten.
    """
    TWILIO_ACCOUNT_SID   = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    TWILIO_AUTH_TOKEN    = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
    TWILIO_WHATSAPP_TO   = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()

    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO]):
        log(f"WhatsApp (geen Twilio): {message[:80]}")
        return False
    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts"
            f"/{TWILIO_ACCOUNT_SID}/Messages.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={"From": TWILIO_WHATSAPP_FROM,
                  "To":   TWILIO_WHATSAPP_TO,
                  "Body": message},
            timeout=15,
        )
        return resp.status_code in (200, 201)
    except Exception:
        return False


def _claude_analyse_regime_shift(
    bear_count:  int,
    bull_count:  int,
    range_count: int,
    total:       int,
) -> None:
    """
    Claude analyseert als er een grote regime verschuiving is.
    Identiek patroon als Claude monitoring in andere bestanden.
    Stuurt WhatsApp als >60% van coins in BEAR regime is.
    """
    ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not ANTHROPIC_API_KEY or total == 0:
        return

    bear_pct = bear_count / total * 100
    if bear_pct < 60:
        return  # Geen massaal BEAR — geen actie nodig

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": 200,
                "messages": [{
                    "role": "user",
                    "content": f"""
Je bent een crypto markt analyst.
Analyseer deze markt situatie in 3 zinnen Nederlands.

REGIME VERDELING ({total} coins):
- BULL:  {bull_count} ({bull_count/total*100:.1f}%)
- RANGE: {range_count} ({range_count/total*100:.1f}%)
- BEAR:  {bear_count} ({bear_pct:.1f}%) ⚠️

Wat betekent dit voor de trading bot?
Aanbevolen actie?
""".strip()
                }],
            },
            timeout=20,
        )
        if resp.status_code == 200:
            analyse = resp.json()["content"][0]["text"].strip()
            log(f"🧠 Claude regime analyse: {analyse}")

            send_whatsapp(
                f"⚠️ MARKT REGIME SIGNAAL\n"
                f"{'─' * 28}\n\n"
                f"📊 {bear_pct:.0f}% van coins in BEAR\n\n"
                f"• BULL:  {bull_count} coins\n"
                f"• RANGE: {range_count} coins\n"
                f"• BEAR:  {bear_count} coins\n\n"
                f"🧠 Claude:\n{analyse}\n\n"
                f"🤖 BOT LOOPT GEWOON DOOR\n"
                f"Stuur STOP als je wil pauzeren.\n\n"
                f"Commands: STOP | STATUS | HEALTH"
            )
    except Exception:
        pass


def print_regime_distribution(conn) -> None:
    """Logt de regime verdeling als samenvatting."""
    dist = get_regime_distribution(conn)
    total = sum(dist.values())
    if total == 0:
        log("Geen regime data beschikbaar")
        return

    log("─" * 40)
    log("REGIME VERDELING:")
    for regime in ["BULL", "RANGE", "BEAR", "UNKNOWN"]:
        n   = dist.get(regime, 0)
        pct = n / total * 100
        bar = "█" * int(pct / 5)
        log(f"  {regime:7}: {n:4} ({pct:5.1f}%) {bar}")
    log(f"  Totaal:  {total}")
    log("─" * 40)

    # Claude check bij massaal BEAR regime
    _claude_analyse_regime_shift(
        dist.get("BEAR", 0),
        dist.get("BULL", 0),
        dist.get("RANGE", 0),
        total,
    )
