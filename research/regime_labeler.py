# research/regime_labeler.py
# ============================================================
# Crypto AI Bot — Regime Labeler v3.0
# ============================================================
# Berekent marktregime (BULL/BEAR/RANGE) per coin op basis van
# SMA50 vs SMA200 plus lineaire regressie slope.
# Slaat op in public.market_regime — gebruikt door
# multi_coin_score.py voor betere scoring.
#
# ARCHITECTUUR (BEGRIJP DIT GOED):
# ─────────────────────────────────
# Dit script draait als Render Cron Job.
# Het leest candles uit de DB en berekent per coin het regime.
# Zelf doet het GEEN trading — het levert alleen data aan.
#
# KRITIEKE FIXES vs v2.0:
# ─────────────────────────────────────────────────────────────
# ✅ conn.rollback() na ELKE DB fout — was de hoofdoorzaak van
#    "transaction is aborted" cascade die alle volgende coins
#    deed falen
# ✅ Per-coin try/except met rollback in main loop
# ✅ fetch_closes_and_ts: rollback bij exception
# ✅ send_whatsapp() maar 1x gedefinieerd (was dubbel)
# ✅ get_regime_for_symbol() maar 1x gedefinieerd (was dubbel)
# ✅ _claude_analyse_regime_shift() nu WEL aangeroepen vanuit main
# ✅ print_regime_distribution() nu WEL aangeroepen vanuit main
# ✅ conn.close() in finally blok — altijd sluiten
# ✅ Candles query: detecteert automatisch kolom 'timeframe'
#    OF 'interval_' afhankelijk van DB schema
# ✅ Pre-filter: haal alleen symbols op die >= MIN_CANDLES hebben
#    (voorkomt te-weinig-candles spam in logs)
# ✅ Batch commit: enkel als batch foutloos verliep
# ✅ Verbinding retry: 3 pogingen voor DB verbinding
# ✅ Runtime bescherming: max 1 uur totale runtime
#
# SAMENWERKING MET ANDERE BESTANDEN:
# ─────────────────────────────────────────────────────────────
# ← history_fetcher.py vult candles tabel
# → market_regime tabel → multi_coin_score.py leest dit
# → market_regime tabel → app.py dashboard toont dit
# → coach_events tabel → ai_coach.py analyseert dit
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
DATABASE_URL         = (os.getenv("DATABASE_URL")         or "").strip()
ANTHROPIC_API_KEY    = (os.getenv("ANTHROPIC_API_KEY")    or "").strip()
TWILIO_ACCOUNT_SID   = (os.getenv("TWILIO_ACCOUNT_SID")   or "").strip()
TWILIO_AUTH_TOKEN    = (os.getenv("TWILIO_AUTH_TOKEN")     or "").strip()
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
TWILIO_WHATSAPP_TO   = (os.getenv("TWILIO_WHATSAPP_TO")   or "").strip()

# Configuratie — allemaal via Render ENV aanpasbaar
LOOKBACK           = int(os.getenv("LOOKBACK",           "250"))
MIN_CANDLES        = int(os.getenv("MIN_CANDLES",        "220"))
TIMEFRAME          = os.getenv("REGIME_TIMEFRAME",       "4h").strip()
ONLY_QUOTE         = os.getenv("ONLY_QUOTE",             "USDT").strip()
BATCH_COMMIT_N     = int(os.getenv("BATCH_COMMIT_N",     "25"))
MAX_RUNTIME_MIN    = int(os.getenv("MAX_RUNTIME_MIN",    "55"))   # max minuten
DB_CONNECT_RETRIES = int(os.getenv("DB_CONNECT_RETRIES", "3"))

# Score gewichten
WEIGHT_STRENGTH    = float(os.getenv("WEIGHT_STRENGTH",  "0.6"))
WEIGHT_TREND       = float(os.getenv("WEIGHT_TREND",     "0.4"))

# Regime drempelwaarden
RANGE_SMA_DIFF_PCT = float(os.getenv("RANGE_SMA_DIFF_PCT", "0.015"))
RANGE_SLOPE_PCT    = float(os.getenv("RANGE_SLOPE_PCT",    "0.001"))
MIN_BULL_SIGNALS   = int(os.getenv("MIN_BULL_SIGNALS",     "2"))

# Wanneer WhatsApp sturen
BEAR_ALERT_PCT     = float(os.getenv("BEAR_ALERT_PCT",    "50.0"))


# ============================================================
# BASIS HELPERS — identiek aan alle andere bestanden
# ============================================================
def now_utc() -> datetime:
    """Geeft huidige UTC tijd terug als timezone-aware datetime."""
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    """Uniform log formaat voor Render logs."""
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
# WHATSAPP — 1x gedefinieerd (was dubbel in v2.0)
# ============================================================
def send_whatsapp(message: str) -> bool:
    """
    Stuurt WhatsApp bericht via Twilio.
    Identieke implementatie als alle andere bestanden.
    Geeft True terug als succesvol, anders False.
    """
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO]):
        log(f"WhatsApp (geen Twilio ingesteld): {message[:80]}")
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
        ok = resp.status_code in (200, 201)
        if not ok:
            log(f"WhatsApp HTTP {resp.status_code}: {resp.text[:80]}")
        return ok
    except Exception as e:
        log(f"WhatsApp fout: {e}")
        return False


# ============================================================
# CLAUDE — analyse helper (1x gedefinieerd)
# ============================================================
def claude_analyse(prompt: str, max_tokens: int = 150) -> str:
    """
    Stuurt een prompt naar Claude en geeft het antwoord terug.
    Geeft lege string terug bij elke fout — nooit een crash.
    """
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
        log(f"Claude API {resp.status_code}: {resp.text[:80]}")
        return ""
    except Exception as e:
        log(f"Claude fout: {e}")
        return ""


# ============================================================
# DATABASE — sslmode="require" identiek aan alle bestanden
# ============================================================
def db_connect(retries: int = DB_CONNECT_RETRIES):
    """
    Verbindt met PostgreSQL via DATABASE_URL.
    Probeert tot `retries` keer bij verbindingsfout.
    Geeft altijd autocommit=False terug.
    """
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt in Render ENV.")

    for poging in range(1, retries + 1):
        try:
            conn = psycopg2.connect(DATABASE_URL, sslmode="require",
                                    connect_timeout=10)
            conn.autocommit = False   # Expliciet — wij beheren commits
            return conn
        except Exception as e:
            log(f"DB verbinding poging {poging}/{retries} mislukt: {e}")
            if poging < retries:
                time.sleep(3)
    raise RuntimeError(f"DB verbinding mislukt na {retries} pogingen.")


def safe_rollback(conn) -> None:
    """
    Voert rollback uit zonder te crashen.
    Altijd aanroepen na een DB fout voor je doorgaat.
    Zonder dit blijft de transactie in ABORTED state en
    mislukken ALLE volgende queries.
    """
    try:
        conn.rollback()
    except Exception as e:
        log(f"Rollback fout (niet kritiek): {e}")


def ensure_market_regime_table(conn) -> None:
    """
    Maakt market_regime tabel aan als die nog niet bestaat.
    Inclusief indexes voor snelle opzoekacties.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS public.market_regime (
                symbol      TEXT             NOT NULL,
                timeframe   TEXT             NOT NULL,
                asof_ts     TIMESTAMPTZ      NOT NULL,
                regime      TEXT,
                strength    DOUBLE PRECISION DEFAULT 0.0,
                sma50       DOUBLE PRECISION,
                sma200      DOUBLE PRECISION,
                slope50     DOUBLE PRECISION,
                slope200    DOUBLE PRECISION,
                score       INTEGER          DEFAULT 0,
                updated_at  TIMESTAMPTZ      DEFAULT NOW(),
                PRIMARY KEY (symbol, timeframe)
            );
            CREATE INDEX IF NOT EXISTS idx_market_regime_symbol_ts
                ON public.market_regime (symbol, asof_ts DESC);
            CREATE INDEX IF NOT EXISTS idx_market_regime_regime
                ON public.market_regime (regime);
            CREATE INDEX IF NOT EXISTS idx_market_regime_updated
                ON public.market_regime (updated_at DESC);
            """)
        conn.commit()
        log("✅ market_regime tabel gecontroleerd/aangemaakt")
    except Exception as e:
        safe_rollback(conn)
        log(f"⚠️ Tabel aanmaken fout: {e}")
        raise


def detect_candles_timeframe_column(conn) -> str:
    """
    Detecteert automatisch of de candles tabel 'timeframe' OF
    'interval_' als kolomnaam gebruikt.
    FIX: app.py gebruikt interval_='1h' maar regime_labeler
    gebruikte timeframe='4h' — nu automatisch gedetecteerd.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'candles'
              AND column_name IN ('timeframe', 'interval_', 'interval')
            ORDER BY ordinal_position
            LIMIT 1;
            """)
            row = cur.fetchone()
            if row:
                col = row[0]
                log(f"Candles tijdframe kolom: '{col}'")
                return col
            log("⚠️ Geen timeframe/interval kolom gevonden in candles — gebruik 'timeframe'")
            return "timeframe"
    except Exception as e:
        safe_rollback(conn)
        log(f"⚠️ Schema detectie fout: {e} — gebruik 'timeframe'")
        return "timeframe"


def log_coach_event(conn, categorie: str, event_type: str,
                    omschrijving: str, ernst: str = "LAAG") -> None:
    """
    Logt een event naar coach_events tabel.
    Wordt door ai_coach.py opgepikt voor analyse.
    Stille fout — niet kritiek als dit mislukt.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO public.coach_events
                (tijdstip, categorie, event_type, omschrijving, ernst)
            VALUES (NOW(), %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """, (categorie, event_type, omschrijving[:500], ernst))
        conn.commit()
    except Exception:
        safe_rollback(conn)


# ============================================================
# TECHNISCHE BEREKENINGEN
# ============================================================
def sma(values: List[float], period: int) -> Optional[float]:
    """
    Simple Moving Average over de laatste `period` waarden.
    Geeft None terug als er te weinig data is.
    """
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def linear_regression_slope(values: List[float]) -> float:
    """
    Lineaire regressie slope over een lijst van waarden.
    Robuuster dan percentage-change voor trend bepaling.

    Geeft slope per periode terug als percentage van gemiddelde waarde.
    Positief = stijgend, negatief = dalend.

    FIX vs v1: was pct-change (zwak, gevoelig voor outliers)
    Nu: echte lineaire regressie over alle waarden.
    """
    n = len(values)
    if n < 2:
        return 0.0

    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n

    numerator   = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    denominator = sum((i - x_mean) ** 2 for i in range(n))

    if denominator == 0 or y_mean == 0:
        return 0.0

    raw_slope = numerator / denominator
    return raw_slope / abs(y_mean)   # Genormaliseerd naar % van gemiddelde prijs


def compute_regime(
    closes: List[float],
) -> Tuple[str, float, Optional[float], Optional[float], float, float]:
    """
    Berekent regime op basis van SMA50/SMA200 en slope.

    REGIME LOGICA:
    ─────────────
    BULL  = SMA50 > SMA200 + voldoende positieve signalen
    BEAR  = SMA50 < SMA200 + voldoende negatieve signalen
    RANGE = tegenstrijdige signalen of klein SMA-verschil

    RETURNS:
    ─────────────
    (regime, strength, sma50, sma200, slope50, slope200)

    strength = 0-100, hogere waarde = sterker regime
    """
    if len(closes) < MIN_CANDLES:
        return "UNKNOWN", 0.0, None, None, 0.0, 0.0

    sma50  = sma(closes, 50)
    sma200 = sma(closes, 200)

    if sma50 is None or sma200 is None:
        return "UNKNOWN", 0.0, None, None, 0.0, 0.0

    current = closes[-1]
    if current <= 0:
        return "UNKNOWN", 0.0, sma50, sma200, 0.0, 0.0

    # Lineaire regressie slopes — robuuster dan % change
    slope50  = linear_regression_slope(closes[-50:]) * 100
    slope200 = linear_regression_slope(
        closes[-200:] if len(closes) >= 200 else closes
    ) * 100

    # Procentueel verschil SMA50 vs SMA200
    diff_pct = (sma50 - sma200) / max(abs(sma200), 0.001)

    # RANGE — klein verschil of vlakke slope
    if abs(diff_pct) < RANGE_SMA_DIFF_PCT or abs(slope50) < RANGE_SLOPE_PCT:
        strength = max(0.0, 50.0 - abs(diff_pct) * 1000)
        return "RANGE", round(strength, 2), sma50, sma200, slope50, slope200

    # BULL — SMA50 boven SMA200
    if sma50 > sma200:
        bull_signals = sum([
            slope50  > 0,
            slope200 > 0,
            current  > sma50,
            current  > sma200,
        ])
        if bull_signals >= MIN_BULL_SIGNALS:
            strength = min(100.0, diff_pct * 500 + slope50 * 200)
            return "BULL", round(strength, 2), sma50, sma200, slope50, slope200

    # BEAR — SMA50 onder SMA200
    elif sma50 < sma200:
        bear_signals = sum([
            slope50  < 0,
            slope200 < 0,
            current  < sma50,
            current  < sma200,
        ])
        if bear_signals >= MIN_BULL_SIGNALS:
            strength = min(100.0, abs(diff_pct) * 500 + abs(slope50) * 200)
            return "BEAR", round(strength, 2), sma50, sma200, slope50, slope200

    # Gemengde signalen → RANGE
    return "RANGE", 30.0, sma50, sma200, slope50, slope200


def calc_score(regime: str, strength: float, slope50: float) -> int:
    """
    Berekent een regime score 0-100.
    Gecombineerd uit sterkte (60%) en slope richting (40%).
    Gewichten configureerbaar via ENV: WEIGHT_STRENGTH, WEIGHT_TREND.
    """
    if regime == "UNKNOWN":
        return 0

    strength_score = min(60.0, strength * WEIGHT_STRENGTH)

    if regime == "BULL" and slope50 > 0:
        trend_score = min(40.0, abs(slope50) * 1000 * WEIGHT_TREND)
    elif regime == "BEAR" and slope50 < 0:
        trend_score = min(40.0, abs(slope50) * 1000 * WEIGHT_TREND)
    elif regime == "RANGE":
        trend_score = 20.0
    else:
        trend_score = 0.0

    return int(strength_score + trend_score)


# ============================================================
# DATA OPHALEN
# ============================================================
def get_symbols_with_enough_candles(conn, tf_col: str) -> List[str]:
    """
    Haalt symbols op die VOLDOENDE candles hebben (>= MIN_CANDLES).

    FIX vs v2.0: v2.0 haalde ALLE symbols op en loopte daarna door
    coins met te weinig candles — dat veroorzaakte de spam in de logs.
    Nu pre-filteren we direct in SQL.

    Filtert ook op ONLY_QUOTE (standaard 'USDT').
    """
    try:
        with conn.cursor() as cur:
            # tf_col is een kolom naam — mag NIET als %s parameter,
            # moet direct in de query string via f-string.
            # ONLY_QUOTE filtert op bijv. 'USDT' aan het einde van symbol.
            if ONLY_QUOTE:
                cur.execute(f"""
                SELECT symbol, COUNT(*) AS n
                FROM public.candles
                WHERE {tf_col} = %s
                  AND symbol LIKE %s
                GROUP BY symbol
                HAVING COUNT(*) >= %s
                ORDER BY symbol
                """, (TIMEFRAME, f"%{ONLY_QUOTE}", MIN_CANDLES))
            else:
                cur.execute(f"""
                SELECT symbol, COUNT(*) AS n
                FROM public.candles
                WHERE {tf_col} = %s
                GROUP BY symbol
                HAVING COUNT(*) >= %s
                ORDER BY symbol
                """, (TIMEFRAME, MIN_CANDLES))

            rows = cur.fetchall()
            symbols = [row[0] for row in rows]
            log(f"Pre-filter: {len(symbols)} symbols met >= {MIN_CANDLES} candles "
                f"(timeframe={TIMEFRAME}, kolom={tf_col})")
            return symbols

    except Exception as e:
        safe_rollback(conn)
        log(f"⚠️ Symbols ophalen fout: {e}")
        return []


def detect_open_time_type(conn) -> str:
    """
    Detecteert het datatype van de open_time kolom in candles.
    Geeft 'timestamp' terug als het al een TIMESTAMPTZ is,
    of 'bigint' als het milliseconden zijn.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT data_type
            FROM information_schema.columns
            WHERE table_name  = 'candles'
              AND column_name = 'open_time'
            LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                dtype = str(row[0]).lower()
                if "timestamp" in dtype:
                    log(f"open_time kolom type: TIMESTAMP (direct bruikbaar)")
                    return "timestamp"
                else:
                    log(f"open_time kolom type: {dtype} (converteren via /1000)")
                    return "bigint"
    except Exception as e:
        safe_rollback(conn)
        log(f"⚠️ open_time type detectie fout: {e} — aanname: timestamp")
    return "timestamp"


def fetch_closes_and_ts(
    conn,
    symbol:      str,
    tf_col:      str,
    ot_is_ts:    bool = True,
) -> Tuple[List[float], Optional[datetime]]:
    """
    Haalt closes en meest recente timestamp op voor een symbol.

    FIX: open_time kan TIMESTAMPTZ zijn (direct gebruiken)
    OF bigint in milliseconden (converteren via to_timestamp(x/1000)).
    ot_is_ts=True → open_time is al een timestamp
    ot_is_ts=False → open_time is milliseconden (bigint)

    FIX vs v2.0: bij een exception deed v2.0 geen rollback.
    """
    # Bouw de timestamp expressie op basis van het kolom type
    if ot_is_ts:
        ts_expr = "open_time"
    else:
        ts_expr = "to_timestamp(open_time::bigint / 1000.0)"

    try:
        with conn.cursor() as cur:
            cur.execute(f"""
            SELECT close,
                   {ts_expr} AS asof_ts
            FROM public.candles
            WHERE symbol    = %s
              AND {tf_col}  = %s
            ORDER BY open_time DESC
            LIMIT %s
            """, (symbol, TIMEFRAME, LOOKBACK))

            rows = cur.fetchall()
            if not rows:
                return [], None

            # Meest recente timestamp (eerste rij = DESC)
            asof_ts = rows[0][1]
            if hasattr(asof_ts, "tzinfo") and asof_ts.tzinfo is None:
                asof_ts = asof_ts.replace(tzinfo=timezone.utc)

            # Omdraaien voor chronologische volgorde (oldest first)
            closes = [safe_float(r[0]) for r in reversed(rows)]
            # Filter nul-waarden
            closes = [c for c in closes if c > 0]
            return closes, asof_ts

    except Exception as e:
        # KRITIEK: rollback zodat de volgende coin niet ook faalt
        safe_rollback(conn)
        log(f"  ⚠️ Closes fout ({symbol}): {e}")
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
) -> bool:
    """
    Slaat regime op in market_regime tabel via UPSERT.
    Geeft True terug als succesvol, anders False.
    Doet GEEN commit — dat doet de aanroeper (batch commit logica).
    """
    try:
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
        return True
    except Exception as e:
        # KRITIEK: rollback na elke upsert fout
        safe_rollback(conn)
        log(f"  ⚠️ Upsert fout ({symbol}): {e}")
        return False


# ============================================================
# REGIME DATA OPHALEN (voor analytics en dashboard)
# ============================================================
def get_regime_distribution(conn) -> Dict[str, int]:
    """
    Geeft verdeling van regimes in de market_regime tabel.
    Gebruikt door app.py dashboard en Claude analyse.
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
        safe_rollback(conn)
        log(f"⚠️ get_regime_distribution fout: {e}")
        return {}


def get_regime_for_symbol(conn, symbol: str) -> Optional[Dict[str, Any]]:
    """
    Haalt huidig regime op voor één specifieke coin.
    Wordt aangeroepen door multi_coin_score.py.
    Geeft dict terug of None als niet gevonden.

    FIX vs v2.0: was dubbel gedefinieerd (2x dezelfde functie
    met andere cursor — nu 1x met RealDictCursor).
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
            SELECT symbol, regime, strength, sma50, sma200,
                   slope50, slope200, score, asof_ts, updated_at
            FROM public.market_regime
            WHERE symbol    = %s
              AND timeframe = %s
            LIMIT 1
            """, (symbol, TIMEFRAME))
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as e:
        safe_rollback(conn)
        log(f"⚠️ get_regime_for_symbol fout ({symbol}): {e}")
        return None


def get_top_bull_coins(conn, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Geeft de sterkste BULL coins op basis van regime sterkte.
    Gebruikt door multi_coin_score.py als extra filter.
    Alleen coins met data < 8 uur oud.
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
            SELECT symbol, regime, strength, score, asof_ts
            FROM public.market_regime
            WHERE regime    = 'BULL'
              AND timeframe = %s
              AND asof_ts   >= NOW() - INTERVAL '8 hours'
            ORDER BY strength DESC
            LIMIT %s
            """, (TIMEFRAME, limit))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        safe_rollback(conn)
        log(f"⚠️ get_top_bull_coins fout: {e}")
        return []


# ============================================================
# STATISTIEKEN EN RAPPORTAGE
# ============================================================
def print_regime_stats(stats: Dict[str, int], total: int) -> None:
    """
    Print regime statistieken naar de logs.
    Geeft een duidelijk overzicht per regime met percentages.
    """
    log("═" * 55)
    log("REGIME STATISTIEKEN DEZE RUN:")
    log("─" * 55)
    for regime in ["BULL", "BEAR", "RANGE", "UNKNOWN"]:
        count = stats.get(regime, 0)
        pct   = (count / max(total, 1)) * 100
        bar   = "█" * int(pct / 4)
        log(f"  {regime:8}: {count:4}x  ({pct:5.1f}%)  {bar}")
    log("─" * 55)
    log(f"  Verwerkt:  {total}")
    log(f"  Fouten:    {stats.get('errors', 0)}")
    log(f"  Overgesla: {stats.get('skipped', 0)}")
    log("═" * 55)


def print_regime_distribution(conn) -> None:
    """
    Logt de huidige totale regime verdeling uit de DB.
    Aanroepen NA de main loop voor een volledig beeld.

    FIX vs v2.0: deze functie werd nooit aangeroepen vanuit main.
    """
    dist  = get_regime_distribution(conn)
    total = sum(dist.values())
    if total == 0:
        log("Geen regime data beschikbaar in DB")
        return

    log("─" * 50)
    log(f"TOTALE REGIME VERDELING IN DB ({TIMEFRAME}):")
    for regime in ["BULL", "RANGE", "BEAR", "UNKNOWN"]:
        n   = dist.get(regime, 0)
        pct = n / total * 100
        bar = "█" * int(pct / 4)
        log(f"  {regime:8}: {n:4} ({pct:5.1f}%)  {bar}")
    log(f"  Totaal:   {total}")
    log("─" * 50)


# ============================================================
# CLAUDE REGIME ANALYSE
# ============================================================
def analyse_regime_shift(
    conn,
    stats:     Dict[str, int],
    processed: int,
) -> None:
    """
    Analyseert de regime verdeling na de main loop.
    Stuurt WhatsApp als >= BEAR_ALERT_PCT% van coins in BEAR is.
    Logt altijd een Claude analyse in coach_events.

    FIX vs v2.0: _claude_analyse_regime_shift() werd nooit
    aangeroepen vanuit main. Nu direct aangeroepen na de loop.
    """
    if processed == 0:
        return

    bear_count  = stats.get("BEAR",  0)
    bull_count  = stats.get("BULL",  0)
    range_count = stats.get("RANGE", 0)
    bear_pct    = bear_count / processed * 100

    prompt = f"""Je bent een crypto markt analyst.
Analyseer deze markt situatie in 2-3 zinnen Nederlands.

REGIME VERDELING ({processed} coins, timeframe {TIMEFRAME}):
- BULL:  {bull_count} ({bull_count/processed*100:.1f}%)
- RANGE: {range_count} ({range_count/processed*100:.1f}%)
- BEAR:  {bear_count} ({bear_pct:.1f}%){'  ⚠️ MASSAAL BEAR' if bear_pct >= BEAR_ALERT_PCT else ''}

Wat betekent dit voor een crypto trading bot?
Geef een concrete aanbeveling in 1 zin."""

    uitleg = claude_analyse(prompt, max_tokens=150)

    if uitleg:
        log(f"🧠 Claude: {uitleg}")

        # Log naar coach_events voor ai_coach.py
        log_coach_event(
            conn,
            categorie   = "REGIME",
            event_type  = "REGIME_UPDATE",
            omschrijving= (
                f"Regime run klaar: BULL={bull_count} "
                f"RANGE={range_count} BEAR={bear_count} "
                f"({bear_pct:.1f}% BEAR). Claude: {uitleg[:200]}"
            ),
            ernst = "HOOG" if bear_pct >= BEAR_ALERT_PCT else "LAAG",
        )

    # WhatsApp alleen bij massaal BEAR
    if bear_pct >= BEAR_ALERT_PCT:
        send_whatsapp(
            f"📊 MARKT REGIME SIGNAAL\n"
            f"{'─' * 28}\n\n"
            f"⚠️ {bear_pct:.0f}% van alle coins in BEAR\n\n"
            f"• BULL:  {bull_count} coins\n"
            f"• RANGE: {range_count} coins\n"
            f"• BEAR:  {bear_count} coins\n\n"
            + (f"🧠 Claude:\n{uitleg}\n\n" if uitleg else "")
            + f"🤖 BOT SYSTEEM DRAAIT DOOR\n"
            f"Stuur STOP om live trading te pauzeren.\n\n"
            f"Commands: STOP | STATUS | HEALTH"
        )


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":

    start_ts = now_utc()
    log("═" * 60)
    log("Regime Labeler v3.0 — gestart")
    log("═" * 60)
    log(f"Database:      {'✅ ingesteld' if DATABASE_URL else '❌ ONTBREEKT'}")
    log(f"Claude API:    {'✅ ingesteld' if ANTHROPIC_API_KEY else '⚠️ niet ingesteld'}")
    log(f"Timeframe:     {TIMEFRAME}")
    log(f"Lookback:      {LOOKBACK} candles")
    log(f"Min candles:   {MIN_CANDLES}")
    log(f"Only quote:    {ONLY_QUOTE}")
    log(f"Batch commit:  elke {BATCH_COMMIT_N} coins")
    log(f"Max runtime:   {MAX_RUNTIME_MIN} minuten")
    log("═" * 60)

    if not DATABASE_URL:
        log("❌ DATABASE_URL ontbreekt in Render ENV — script stopt")
        sys.exit(1)

    conn = None
    try:
        # ── DB verbinding (met retries) ───────────────────────
        conn = db_connect()
        log("✅ Database verbonden")

        # ── Tabel aanmaken als die nog niet bestaat ───────────
        ensure_market_regime_table(conn)

        # ── Detecteer automatisch de juiste kolom naam ────────
        tf_col = detect_candles_timeframe_column(conn)

        # ── Detecteer open_time kolom type ───────────────────
        # FIX: open_time kan TIMESTAMPTZ zijn (direct gebruiken)
        # OF bigint in milliseconden (to_timestamp(x/1000))
        ot_type  = detect_open_time_type(conn)
        ot_is_ts = (ot_type == "timestamp")

        # ── Haal alleen symbols op met VOLDOENDE candles ──────
        symbols = get_symbols_with_enough_candles(conn, tf_col)

        if not symbols:
            log("⚠️ Geen symbols met voldoende candles gevonden.")
            log("   → Zorg dat history_fetcher.py eerst heeft gedraaid")
            log("   → Check of REGIME_TIMEFRAME overeenkomt met candle timeframe in DB")
            sys.exit(0)

        log(f"Start verwerking: {len(symbols)} symbols")
        log("─" * 60)

        stats: Dict[str, int] = {
            "BULL": 0, "BEAR": 0, "RANGE": 0,
            "UNKNOWN": 0, "errors": 0, "skipped": 0,
        }
        processed        = 0
        batch_has_errors = False

        for i, symbol in enumerate(symbols):

            # ── Runtime check — stop na MAX_RUNTIME_MIN minuten ─
            elapsed_min = (now_utc() - start_ts).total_seconds() / 60
            if elapsed_min > MAX_RUNTIME_MIN:
                log(f"⏱️ Max runtime ({MAX_RUNTIME_MIN}m) bereikt na {i} symbols — stoppen")
                break

            # ── Per-coin try/except met rollback ─────────────
            # KRITIEK: zonder dit crasht 1 fout de rest van de run
            try:
                # Closes ophalen
                closes, asof_ts = fetch_closes_and_ts(conn, symbol, tf_col, ot_is_ts)

                if len(closes) < MIN_CANDLES:
                    # Pre-filter had dit al gefilterd, maar voor de zekerheid
                    stats["skipped"] += 1
                    continue

                # Regime berekenen
                regime, strength, sma50, sma200, slope50, slope200 = compute_regime(closes)
                score = calc_score(regime, strength, slope50)

                # Opslaan in DB
                ok = upsert_regime(
                    conn, symbol,
                    asof_ts or now_utc(),
                    regime, strength,
                    sma50, sma200,
                    slope50, slope200,
                    score,
                )

                if ok:
                    stats[regime] = stats.get(regime, 0) + 1
                    processed += 1
                else:
                    stats["errors"] += 1
                    batch_has_errors = True

            except Exception as e:
                # Extra vangnets — safe_rollback zodat volgende coin
                # een schone transactie heeft
                safe_rollback(conn)
                log(f"  ❌ Onverwachte fout ({symbol}): {type(e).__name__}: {e}")
                stats["errors"] += 1
                batch_has_errors = True
                continue

            # ── Batch commit elke BATCH_COMMIT_N coins ────────
            # Alleen committen als batch foutloos was
            if (i + 1) % BATCH_COMMIT_N == 0:
                if not batch_has_errors:
                    try:
                        conn.commit()
                    except Exception as e:
                        safe_rollback(conn)
                        log(f"  ⚠️ Batch commit fout: {e}")
                else:
                    log(f"  ⚠️ Batch {i+1} had fouten — geen commit")
                    safe_rollback(conn)

                batch_has_errors = False
                log(
                    f"  [{i+1:4}/{len(symbols)}] "
                    f"BULL={stats['BULL']} "
                    f"BEAR={stats['BEAR']} "
                    f"RANGE={stats['RANGE']} "
                    f"ERR={stats['errors']}"
                )

        # ── Finale commit voor resterende records ─────────────
        try:
            conn.commit()
            log("✅ Finale commit gedaan")
        except Exception as e:
            safe_rollback(conn)
            log(f"⚠️ Finale commit fout: {e}")

        # ── Statistieken loggen ───────────────────────────────
        print_regime_stats(stats, processed)

        # ── Totale regime verdeling uit DB loggen ─────────────
        # FIX: werd nooit aangeroepen in v2.0
        print_regime_distribution(conn)

        # ── Claude analyse + WhatsApp bij massaal BEAR ────────
        # FIX: werd nooit aangeroepen in v2.0
        analyse_regime_shift(conn, stats, processed)

        # ── Runtime samenvatting ──────────────────────────────
        elapsed = (now_utc() - start_ts).total_seconds()
        log(f"✅ Klaar: {processed} coins in {elapsed:.0f}s "
            f"({elapsed/max(processed,1):.2f}s per coin)")

    except Exception as e:
        log(f"❌ Fatale fout: {type(e).__name__}: {e}")
        if conn:
            safe_rollback(conn)
            try:
                conn.close()
            except Exception:
                pass
        sys.exit(1)

    finally:
        # ALTIJD sluiten — FIX: was niet aanwezig in v2.0
        if conn:
            try:
                conn.close()
                log("DB verbinding gesloten")
            except Exception:
                pass
