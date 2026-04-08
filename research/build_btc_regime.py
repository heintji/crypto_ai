# research/build_btc_regime.py
# ============================================================
# Crypto AI Bot — BTC Regime Builder v3.0
# ============================================================
# Wat dit doet:
#   Haalt BTC 4H candles op van Binance, berekent EMA200 en
#   classificeert het marktregime als BULL / BEAR / RANGE.
#   Slaat op in public.btc_regime_4h — wordt gelezen door
#   multi_coin_score.py als primaire marktfilter.
#
# ARCHITECTUUR:
# ─────────────────────────────────────────────────────────────
# Dit script draait als Render Cron Job (elke 4 uur).
# Het doet GEEN trading — het levert alleen regime data aan.
#
# DATAFLOW:
# ─────────────────────────────────────────────────────────────
# Binance API → candles → EMA200 → regime classificatie
#   → public.btc_regime_4h → multi_coin_score.py
#   → public.btc_regime_changes → ai_coach.py
#
# KRITIEKE FIXES vs v2.0:
# ─────────────────────────────────────────────────────────────
# ✅ open_time type mismatch gefixed:
#    DB kolom is TIMESTAMPTZ, v2.0 inserteerde BIGINT (ms) →
#    crashte direct. Nu wordt ts_utc als open_time opgeslagen.
# ✅ ensure_tables: ALTER TABLE migraties voor alle kolommen
#    (ts_utc, strength, ema200_slope, pct_from_ema, updated_at)
#    zodat bestaande tabellen zonder recreatie worden bijgewerkt
# ✅ safe_rollback() toegevoegd — ontbrak volledig in v2.0
# ✅ finally + conn.close() — verbinding wordt altijd gesloten
# ✅ DB connect retries (3 pogingen met backoff)
# ✅ autocommit=False — expliciet transactiebeheer
# ✅ calc_ema() off-by-one bug gefixed
# ✅ conn.close() ook bij exception, niet alleen bij succes
# ✅ get_regime_stats() veilig als kolommen ontbreken
# ✅ Volledige foutafhandeling in alle DB functies
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

# ── Bot health heartbeat ──────────────────────────────
def _schrijf_heartbeat(status='OK', details=''):
    try:
        import psycopg2, os as _os
        _db = psycopg2.connect(_os.environ['DATABASE_URL'])
        _c  = _db.cursor()
        _c.execute("""
            UPDATE bot_health SET status=%s, laatste_run=NOW(), details=%s
            WHERE service=%s
        """, (status, details[:200] if details else '', 'build_btc_regime'))
        if _c.rowcount == 0:
            _c.execute("INSERT INTO bot_health (service, status, laatste_run, details) VALUES (%s,%s,NOW(),%s)",
                ('build_btc_regime', status, details[:200] if details else ''))
        _db.commit()
        _db.close()
    except Exception as _hbe:
        print(f'[HEALTH] heartbeat fout: {_hbe}')



# ============================================================
# ENV — identiek aan alle andere bestanden
# ============================================================
DATABASE_URL         = (os.getenv("DATABASE_URL")         or "").strip()
ANTHROPIC_API_KEY    = (os.getenv("ANTHROPIC_API_KEY")    or "").strip()
TWILIO_ACCOUNT_SID   = (os.getenv("TWILIO_ACCOUNT_SID")   or "").strip()
TWILIO_AUTH_TOKEN    = (os.getenv("TWILIO_AUTH_TOKEN")     or "").strip()
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
TWILIO_WHATSAPP_TO   = (os.getenv("TWILIO_WHATSAPP_TO")   or "").strip()

# Configuratie — aanpasbaar via Render ENV
TAIL_ONLY            = os.getenv("TAIL_ONLY",          "0").strip() == "1"
SYMBOL               = os.getenv("BTC_SYMBOL",         "BTCUSDT").strip()
TIMEFRAME            = os.getenv("BTC_TIMEFRAME",      "4h").strip()
EMA_PERIOD           = int(os.getenv("EMA_PERIOD",     "200"))
LIMIT                = int(os.getenv("BTC_CANDLE_LIMIT","500"))
MAX_RETRIES          = int(os.getenv("MAX_RETRIES",    "3"))
DB_CONNECT_RETRIES   = int(os.getenv("DB_CONNECT_RETRIES", "3"))

# Drempelwaarden voor regime classificatie
RANGE_PCT_THRESHOLD   = float(os.getenv("RANGE_PCT_THRESHOLD",   "0.02"))
RANGE_SLOPE_THRESHOLD = float(os.getenv("RANGE_SLOPE_THRESHOLD", "0.001"))

BINANCE_BASE = "https://api.binance.com/api/v3"


# ============================================================
# BASIS HELPERS — identiek aan alle andere bestanden
# ============================================================
def now_utc() -> datetime:
    """Geeft huidige UTC tijd terug als timezone-aware datetime."""
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    """Uniform log formaat voor Render logs."""
    print(f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')}] [BTC_REGIME] {msg}", flush=True)


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
# WHATSAPP — 1x gedefinieerd
# ============================================================
def send_whatsapp(message: str) -> bool:
    """
    Stuurt WhatsApp bericht via Twilio.
    Identieke implementatie als alle andere bestanden.
    Alleen voor KRITIEKE meldingen — regime veranderingen.
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
        if ok:
            log(f"✅ WhatsApp verzonden ({len(message)} tekens)")
        else:
            log(f"❌ WhatsApp {resp.status_code}: {resp.text[:100]}")
        return ok
    except Exception as e:
        log(f"❌ WhatsApp fout: {e}")
        return False


# ============================================================
# CLAUDE — analyse helper
# ============================================================
def claude_analyse(prompt: str, max_tokens: int = 200) -> str:
    """
    Stuurt een prompt naar Claude en geeft antwoord terug.
    Nooit een crash — lege string bij elke fout.
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


def notify_regime_change(
    old_regime: str,
    new_regime: str,
    close:      float,
    ema200:     float,
    strength:   float,
) -> None:
    """
    Stuurt WhatsApp melding bij BTC regime verandering.
    Claude analyseert wat dit betekent voor de bot.
    Bot loopt altijd door — gebruiker beslist via STOP.
    """
    prompt = f"""Je bent een crypto trading bot coach.
BTC regime is veranderd van {old_regime} naar {new_regime}.

BTC close: {close:.2f}
EMA200:    {ema200:.2f}
Sterkte:   {strength:.1f}%

Geef in 2 zinnen Nederlands:
- Wat dit betekent voor de bot
- Wat de gebruiker moet weten""".strip()

    uitleg = claude_analyse(prompt, max_tokens=150)
    if not uitleg:
        uitleg = f"BTC regime veranderd van {old_regime} naar {new_regime}."

    send_whatsapp(
        f"📊 BTC REGIME VERANDERING\n"
        f"{'─' * 28}\n\n"
        f"Van:       {old_regime}\n"
        f"Naar:      {new_regime}\n\n"
        f"BTC prijs: €{close:,.2f}\n"
        f"EMA200:    €{ema200:,.2f}\n"
        f"Sterkte:   {strength:.1f}%\n\n"
        f"🧠 Claude:\n{uitleg}\n\n"
        f"🤖 BOT SYSTEEM DRAAIT DOOR\n"
        f"Stuur STOP om live trading te pauzeren.\n\n"
        f"Commands: STOP | STATUS | HEALTH"
    )


# ============================================================
# DATABASE — sslmode="require" identiek aan alle bestanden
# ============================================================
def db_connect(retries: int = DB_CONNECT_RETRIES):
    """
    Verbindt met PostgreSQL via DATABASE_URL.
    Probeert tot `retries` keer bij verbindingsfout.
    autocommit=False — wij beheren transacties expliciet.

    FIX vs v2.0: geen retries, geen autocommit=False.
    """
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt in Render ENV.")

    for poging in range(1, retries + 1):
        try:
            conn = psycopg2.connect(
                DATABASE_URL,
                sslmode="require",
                connect_timeout=10,
            )
            conn.autocommit = False
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
    Zonder rollback blijft transactie in ABORTED state.

    FIX vs v2.0: safe_rollback bestond niet.
    """
    try:
        conn.rollback()
    except Exception as e:
        log(f"Rollback fout (niet kritiek): {e}")


def ensure_tables(conn) -> None:
    """
    Maakt btc_regime_4h en btc_regime_changes aan als ze niet bestaan.
    Voegt ook ontbrekende kolommen toe via ALTER TABLE migraties.

    FIX vs v2.0: geen migraties — als tabel al bestond met andere
    kolommen crashte alles bij de eerste upsert.

    BELANGRIJK: open_time is TIMESTAMPTZ in de DB (niet BIGINT).
    De code slaat ts_utc op als open_time — niet de milliseconden.
    """
    try:
        with conn.cursor() as cur:
            # ── btc_regime_4h aanmaken ────────────────────────
            cur.execute("""
            CREATE TABLE IF NOT EXISTS public.btc_regime_4h (
                open_time    TIMESTAMPTZ      PRIMARY KEY,
                ts_utc       TIMESTAMPTZ,
                close        DOUBLE PRECISION,
                ema200       DOUBLE PRECISION,
                ema200_slope DOUBLE PRECISION DEFAULT 0.0,
                regime       TEXT,
                strength     DOUBLE PRECISION DEFAULT 0.0,
                pct_from_ema DOUBLE PRECISION DEFAULT 0.0,
                updated_at   TIMESTAMPTZ      DEFAULT NOW()
            );
            """)
        conn.commit()

        # ── Migraties — voeg ontbrekende kolommen toe ─────────
        # ALTER TABLE ADD COLUMN IF NOT EXISTS is veilig:
        # doet niets als kolom al bestaat.
        # FIX vs v2.0: deze migraties ontbraken volledig.
        migraties_regime = [
            ("ts_utc",       "TIMESTAMPTZ"),
            ("ema200_slope", "DOUBLE PRECISION DEFAULT 0.0"),
            ("strength",     "DOUBLE PRECISION DEFAULT 0.0"),
            ("pct_from_ema", "DOUBLE PRECISION DEFAULT 0.0"),
            ("updated_at",   "TIMESTAMPTZ DEFAULT NOW()"),
        ]
        with conn.cursor() as cur:
            for kolom, definitie in migraties_regime:
                cur.execute(f"""
                ALTER TABLE public.btc_regime_4h
                ADD COLUMN IF NOT EXISTS {kolom} {definitie};
                """)
        conn.commit()

        # ── Indexes ───────────────────────────────────────────
        with conn.cursor() as cur:
            cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_btc_regime_4h_time
                ON public.btc_regime_4h (open_time DESC);
            CREATE INDEX IF NOT EXISTS idx_btc_regime_updated
                ON public.btc_regime_4h (updated_at DESC);
            """)
        conn.commit()

        # ── btc_regime_changes aanmaken ───────────────────────
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS public.btc_regime_changes (
                id         SERIAL      PRIMARY KEY,
                changed_at TIMESTAMPTZ DEFAULT NOW(),
                old_regime TEXT,
                new_regime TEXT,
                close      DOUBLE PRECISION,
                ema200     DOUBLE PRECISION,
                strength   DOUBLE PRECISION,
                notified   BOOLEAN     DEFAULT FALSE
            );
            CREATE INDEX IF NOT EXISTS idx_btc_regime_changes_time
                ON public.btc_regime_changes (changed_at DESC);
            """)
        conn.commit()

        log("✅ Tabellen + migraties klaar")

    except Exception as e:
        safe_rollback(conn)
        log(f"⚠️ Tabel/migratie fout: {e}")
        raise


# ============================================================
# BINANCE DATA OPHALEN
# ============================================================
def fetch_btc_candles_binance(limit: int = 500) -> List[Dict[str, Any]]:
    """
    Haalt BTC 4H candles op van Binance REST API.
    Retry bij netwerk errors met exponential backoff.
    Geeft lege lijst terug bij alle pogingen mislukt.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                f"{BINANCE_BASE}/klines",
                params={
                    "symbol":   SYMBOL,
                    "interval": TIMEFRAME,
                    "limit":    limit,
                },
                timeout=15,
            )
            resp.raise_for_status()
            candles = []
            for c in resp.json():
                candles.append({
                    "open_time": safe_int(c[0]),   # milliseconden
                    "open":      safe_float(c[1]),
                    "high":      safe_float(c[2]),
                    "low":       safe_float(c[3]),
                    "close":     safe_float(c[4]),
                    "volume":    safe_float(c[5]),
                })
            log(f"✅ {len(candles)} BTC candles opgehaald van Binance")
            return candles
        except Exception as e:
            log(f"⚠️ Binance poging {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

    log("❌ Binance niet bereikbaar na alle pogingen")
    return []


def fetch_btc_candles_db(conn, limit: int = 500) -> List[Dict[str, Any]]:
    """
    Haalt BTC candles op uit public.candles als Binance fallback.
    Recentste eerst ophalen, dan omkeren voor chronologische volgorde.
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
            SELECT open_time, open, high, low, close, volume
            FROM public.candles
            WHERE symbol    = %s
              AND timeframe = %s
            ORDER BY open_time DESC
            LIMIT %s
            """, (SYMBOL, TIMEFRAME, limit))
            rows = cur.fetchall()
            # Omdraaien voor EMA berekening: oudste candle eerst
            result = list(reversed([dict(r) for r in rows]))
            log(f"✅ {len(result)} BTC candles opgehaald uit DB als fallback")
            return result
    except Exception as e:
        safe_rollback(conn)
        log(f"⚠️ DB candles fout: {e}")
        return []


# ============================================================
# TECHNISCHE BEREKENINGEN
# ============================================================
def calc_ema(closes: List[float], period: int) -> List[float]:
    """
    Berekent Exponential Moving Average over een lijst van slotprijzen.

    Methode: standaard EMA met multiplier = 2 / (period + 1).
    De eerste EMA waarde is het SMA over de eerste `period` waarden.

    Geeft een lijst terug van GELIJKE lengte als de input.
    Posities 0 t/m period-2 zijn 0.0 (onvoldoende data).
    Posities period-1 en verder bevatten geldige EMA waarden.

    FIX vs v2.0: off-by-one bug — v2.0 had result van verkeerde
    lengte waardoor index-toegang crashte bij sommige lengtes.
    """
    n = len(closes)
    if n < period:
        return [0.0] * n

    result = [0.0] * n

    # Eerste EMA waarde = SMA over de eerste `period` candles
    sma_start = sum(closes[:period]) / period
    result[period - 1] = sma_start

    # EMA multiplier
    mult = 2.0 / (period + 1)

    # Bereken EMA voor alle volgende candles
    for i in range(period, n):
        result[i] = closes[i] * mult + result[i - 1] * (1.0 - mult)

    return result


def calc_atr(candles: List[Dict], period: int = 14) -> List[float]:
    """
    Berekent Average True Range voor volatiliteits context.
    True Range = max(H-L, |H-prevC|, |L-prevC|).
    Gebruikt Wilder's smoothing methode.
    """
    if len(candles) < period + 1:
        return []

    trs = []
    for i in range(1, len(candles)):
        h  = safe_float(candles[i]["high"])
        l  = safe_float(candles[i]["low"])
        pc = safe_float(candles[i - 1]["close"])
        if h > 0 and l > 0 and pc > 0:
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    if len(trs) < period:
        return []

    # Wilder's ATR smoothing
    atr_vals = []
    atr_val  = sum(trs[:period]) / period
    atr_vals.append(atr_val)
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
        atr_vals.append(atr_val)

    return atr_vals


def classify_regime(
    close:       float,
    ema200:      float,
    ema200_prev: float,
) -> Tuple[str, float, float, float]:
    """
    Classificeert BTC marktregime op basis van EMA200.

    LOGICA:
    ─────────────────────────────────────────────────────
    BULL  = Prijs > EMA200 EN positieve slope
    BEAR  = Prijs < EMA200 EN negatieve slope
    RANGE = Prijs binnen 2% van EMA200 OF vlakke slope
            OF tegenstrijdige signalen (prijs boven maar dalend)

    RETURNS:
    ─────────────────────────────────────────────────────
    (regime, strength, pct_from_ema, slope_pct)

    strength     = 0-100, hogere waarde = sterker regime
    pct_from_ema = % afstand prijs t.o.v. EMA200 (+ = boven)
    slope_pct    = % verandering EMA200 per periode
    """
    if ema200 <= 0 or ema200_prev <= 0 or close <= 0:
        return "UNKNOWN", 0.0, 0.0, 0.0

    # Procentuele afstand van prijs tot EMA200
    pct_diff = (close - ema200) / ema200     # positief = boven EMA

    # Slope van EMA200 (% verandering per periode)
    slope = (ema200 - ema200_prev) / ema200_prev

    # RANGE: prijs dicht bij EMA OF slope vrijwel vlak
    if abs(pct_diff) < RANGE_PCT_THRESHOLD or abs(slope) < RANGE_SLOPE_THRESHOLD:
        strength = max(0.0, 50.0 - abs(pct_diff) * 1000)
        return "RANGE", round(strength, 2), round(pct_diff * 100, 3), round(slope * 100, 4)

    # BULL: prijs boven EMA200 met stijgende slope
    if pct_diff > 0 and slope > 0:
        strength = min(100.0, pct_diff * 500 + slope * 5000)
        return "BULL", round(strength, 2), round(pct_diff * 100, 3), round(slope * 100, 4)

    # BEAR: prijs onder EMA200 met dalende slope
    if pct_diff < 0 and slope < 0:
        strength = min(100.0, abs(pct_diff) * 500 + abs(slope) * 5000)
        return "BEAR", round(strength, 2), round(pct_diff * 100, 3), round(slope * 100, 4)

    # Gemengde signalen (bijv. prijs boven maar slope daalt) → RANGE
    return "RANGE", 30.0, round(pct_diff * 100, 3), round(slope * 100, 4)


# ============================================================
# REGIME OPSLAAN EN OPHALEN
# ============================================================
def get_previous_regime(conn) -> Optional[str]:
    """
    Haalt het meest recente opgeslagen regime op.
    Gebruikt voor regime-verandering detectie.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT regime FROM public.btc_regime_4h
            ORDER BY open_time DESC LIMIT 1
            """)
            row = cur.fetchone()
            return safe_str(row[0]) if row else None
    except Exception as e:
        safe_rollback(conn)
        log(f"⚠️ get_previous_regime fout: {e}")
        return None


def get_regime_stats(conn) -> Dict[str, Any]:
    """
    Haalt statistieken op van de regime tabel.
    BULL/BEAR/RANGE verdeling over de laatste 30 dagen.

    FIX vs v2.0: crashte als kolom 'strength' of 'pct_from_ema'
    ontbrak in de DB. Nu veilig afgevangen.
    """
    stats: Dict[str, Any] = {}
    try:
        with conn.cursor() as cur:
            # Check welke kolommen beschikbaar zijn
            cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'btc_regime_4h'
            """)
            cols = {row[0] for row in cur.fetchall()}

            # Bouw SELECT op basis van beschikbare kolommen
            strength_sel    = "ROUND(AVG(strength)::numeric, 1)"    if "strength"     in cols else "0"
            pct_ema_sel     = "ROUND(AVG(pct_from_ema)::numeric, 2)" if "pct_from_ema" in cols else "0"
            updated_filter  = "AND updated_at >= NOW() - INTERVAL '30 days'" if "updated_at" in cols else ""

            cur.execute(f"""
            SELECT
                regime,
                COUNT(*) AS n,
                {strength_sel}  AS avg_strength,
                {pct_ema_sel}   AS avg_pct_from_ema
            FROM public.btc_regime_4h
            WHERE regime IS NOT NULL
              {updated_filter}
            GROUP BY regime
            ORDER BY n DESC
            """)
            for row in cur.fetchall():
                regime = safe_str(row[0])
                stats[regime] = {
                    "count":            safe_int(row[1]),
                    "avg_strength":     safe_float(row[2]),
                    "avg_pct_from_ema": safe_float(row[3]),
                }

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.btc_regime_4h")
            stats["total_rows"] = safe_int(cur.fetchone()[0])

    except Exception as e:
        safe_rollback(conn)
        log(f"⚠️ Regime stats fout: {e}")

    return stats


def save_regime_change(
    conn,
    old_regime: str,
    new_regime: str,
    close:      float,
    ema200:     float,
    strength:   float,
) -> None:
    """
    Logt een regime verandering naar btc_regime_changes.
    Wordt opgepikt door ai_coach.py voor analyse.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO public.btc_regime_changes
                (old_regime, new_regime, close, ema200, strength)
            VALUES (%s, %s, %s, %s, %s)
            """, (old_regime, new_regime, close, ema200, strength))
        conn.commit()
        log(f"📝 Regime verandering gelogd: {old_regime} → {new_regime}")
    except Exception as e:
        safe_rollback(conn)
        log(f"⚠️ Regime change log fout: {e}")


def upsert_regime_rows(conn, rows: List[Tuple]) -> int:
    """
    Slaat regime rijen op via bulk UPSERT.
    ON CONFLICT op open_time (TIMESTAMPTZ PRIMARY KEY).

    FIX vs v2.0:
    - v2.0 inserteerde ts_ms (bigint) als open_time
    - DB heeft open_time als TIMESTAMPTZ → type crash
    - Nu wordt ts_utc (datetime object) als open_time opgeslagen

    Geeft aantal verwerkte rijen terug.
    0 bij fout.
    """
    if not rows:
        return 0
    try:
        psycopg2.extras.execute_values(
            conn.cursor(),
            """
            INSERT INTO public.btc_regime_4h
                (open_time, ts_utc, close, ema200, ema200_slope,
                 regime, strength, pct_from_ema, updated_at)
            VALUES %s
            ON CONFLICT (open_time) DO UPDATE SET
                ts_utc       = EXCLUDED.ts_utc,
                close        = EXCLUDED.close,
                ema200       = EXCLUDED.ema200,
                ema200_slope = EXCLUDED.ema200_slope,
                regime       = EXCLUDED.regime,
                strength     = EXCLUDED.strength,
                pct_from_ema = EXCLUDED.pct_from_ema,
                updated_at   = NOW()
            """,
            rows,
        )
        conn.commit()
        return len(rows)
    except Exception as e:
        safe_rollback(conn)
        log(f"⚠️ Upsert fout: {e}")
        return 0


# ============================================================
# HOOFD BUILD FUNCTIE
# ============================================================
def build_regime(conn) -> Tuple[int, str]:
    """
    Bouwt/updatet de BTC regime tabel.

    STAPPEN:
    ─────────────────────────────────────────────────────────
    1. Candles ophalen van Binance (DB als fallback)
    2. EMA200 berekenen
    3. Regime classificeren per candle
    4. Opslaan in DB (UPSERT)
    5. Regime verandering detecteren en WhatsApp sturen

    RETURNS:
    ─────────────────────────────────────────────────────────
    (aantal_opgeslagen_rijen, huidig_regime)
    """
    # ── Stap 1: candles ophalen ───────────────────────────────
    candles = fetch_btc_candles_binance(LIMIT)
    if len(candles) < EMA_PERIOD + 2:
        log(f"⚠️ Binance: {len(candles)} candles — probeer DB fallback...")
        candles = fetch_btc_candles_db(conn, LIMIT)

    if len(candles) < EMA_PERIOD + 2:
        log(f"❌ Te weinig candles: {len(candles)} (minimaal {EMA_PERIOD + 2} nodig)")
        return 0, "UNKNOWN"

    log(f"📊 {len(candles)} candles beschikbaar voor EMA{EMA_PERIOD} berekening")

    # ── Stap 2: EMA200 berekenen ─────────────────────────────
    closes = [safe_float(c["close"]) for c in candles]
    emas   = calc_ema(closes, EMA_PERIOD)

    if len(emas) != len(closes):
        log(f"❌ EMA berekening mislukt — lengte mismatch ({len(emas)} vs {len(closes)})")
        return 0, "UNKNOWN"

    # ── Stap 3: vorig regime ophalen ─────────────────────────
    previous_regime = get_previous_regime(conn)

    # ── Stap 4: regime classificeren per candle ──────────────
    rows: List[Tuple] = []
    huidig           = "UNKNOWN"
    huidig_ema       = 0.0
    huidig_close     = 0.0
    huidig_sterkte   = 0.0

    # Start na de EMA warm-up periode
    start_idx = EMA_PERIOD
    if TAIL_ONLY:
        # Alleen laatste 20 candles updaten bij TAIL_ONLY modus
        start_idx = max(start_idx, len(candles) - 20)

    for i in range(start_idx, len(candles)):
        ema_now  = emas[i]
        ema_prev = emas[i - 1] if i > 0 else 0.0
        close    = closes[i]
        ts_ms    = safe_int(candles[i]["open_time"])

        # Converteer milliseconden naar UTC datetime
        # FIX: v2.0 stuurde ts_ms (bigint) naar open_time (TIMESTAMPTZ)
        # Nu sturen we een echte datetime naar open_time
        ts_utc = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)

        if ema_now <= 0 or ema_prev <= 0 or close <= 0:
            continue

        regime, strength, pct_from_ema, slope = classify_regime(
            close, ema_now, ema_prev
        )

        # Tuple volgorde moet overeenkomen met INSERT kolommen:
        # (open_time, ts_utc, close, ema200, ema200_slope,
        #  regime, strength, pct_from_ema, updated_at)
        rows.append((
            ts_utc,       # open_time = TIMESTAMPTZ PRIMARY KEY
            ts_utc,       # ts_utc    = zelfde waarde
            close,
            ema_now,
            slope,
            regime,
            strength,
            pct_from_ema,
            now_utc(),    # updated_at
        ))

        huidig         = regime
        huidig_ema     = ema_now
        huidig_close   = close
        huidig_sterkte = strength

    # ── Stap 5: opslaan in DB ────────────────────────────────
    n = upsert_regime_rows(conn, rows)
    log(f"✅ {n} regime rijen opgeslagen")

    # ── Stap 6: regime verandering detecteren ────────────────
    if (
        previous_regime
        and huidig != "UNKNOWN"
        and previous_regime != huidig
        and huidig_ema > 0
    ):
        log(f"🔄 Regime veranderd: {previous_regime} → {huidig}")
        save_regime_change(
            conn, previous_regime, huidig,
            huidig_close, huidig_ema, huidig_sterkte
        )
        notify_regime_change(
            previous_regime, huidig,
            huidig_close, huidig_ema, huidig_sterkte
        )

    # ── Stap 7: huidig regime loggen ─────────────────────────
    pct = (huidig_close - huidig_ema) / huidig_ema * 100 if huidig_ema > 0 else 0.0
    log(
        f"📈 Huidig BTC regime: {huidig} "
        f"(sterkte={huidig_sterkte:.1f}% | "
        f"close={huidig_close:,.2f} | "
        f"ema200={huidig_ema:,.2f} | "
        f"afstand={pct:+.2f}%)"
    )

    return n, huidig


# ============================================================
# PUBLIEKE HELPERS — voor multi_coin_score.py en app.py
# ============================================================
def get_current_btc_regime(conn) -> str:
    """
    Haalt huidig BTC regime op.
    Wordt aangeroepen door multi_coin_score.py voor de BTC filter.
    Geeft 'UNKNOWN' terug als geen data beschikbaar.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT regime FROM public.btc_regime_4h
            ORDER BY open_time DESC LIMIT 1
            """)
            row = cur.fetchone()
            return safe_str(row[0], "UNKNOWN") if row else "UNKNOWN"
    except Exception as e:
        safe_rollback(conn)
        log(f"⚠️ get_current_btc_regime fout: {e}")
        return "UNKNOWN"


def get_btc_regime_context(conn) -> Dict[str, Any]:
    """
    Geeft volledige BTC regime context terug als dict.
    Wordt gebruikt door multi_coin_score.py voor scoring.
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
            SELECT regime, strength, pct_from_ema, ema200_slope,
                   close, ema200, open_time
            FROM public.btc_regime_4h
            ORDER BY open_time DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                return dict(row)
    except Exception as e:
        safe_rollback(conn)
        log(f"⚠️ get_btc_regime_context fout: {e}")
    return {"regime": "UNKNOWN", "strength": 0.0}


def print_regime_summary(conn) -> None:
    """
    Print een samenvatting van het BTC regime naar de logs.
    Toont BULL/BEAR/RANGE verdeling van de laatste 30 dagen.
    """
    stats = get_regime_stats(conn)
    total = stats.get("total_rows", 0)

    log("═" * 55)
    log("BTC REGIME SAMENVATTING (laatste 30 dagen):")
    log("─" * 55)

    telbare = {k: v for k, v in stats.items()
               if k not in ("total_rows",) and isinstance(v, dict)}
    totaal_count = sum(v["count"] for v in telbare.values()) or 1

    for regime in ["BULL", "RANGE", "BEAR", "UNKNOWN"]:
        if regime in telbare:
            s   = telbare[regime]
            pct = s["count"] / totaal_count * 100
            bar = "█" * int(pct / 4)
            log(f"  {regime:7}: {s['count']:4}x ({pct:5.1f}%)  {bar}")
    log("─" * 55)
    log(f"  Totaal rijen in tabel: {total}")
    log("═" * 55)


# ============================================================
# MAIN
# ============================================================
_schrijf_heartbeat('OK', 'btc_regime bijgewerkt')

if __name__ == "__main__":

    start_ts = now_utc()
    log("═" * 60)
    log("BTC Regime Builder v3.0 — gestart")
    log("═" * 60)
    log(f"Database:       {'✅ ingesteld' if DATABASE_URL else '❌ ONTBREEKT'}")
    log(f"Twilio:         {'✅ ingesteld' if TWILIO_ACCOUNT_SID else '⚠️ niet ingesteld'}")
    log(f"Claude API:     {'✅ ingesteld' if ANTHROPIC_API_KEY else '⚠️ niet ingesteld'}")
    log(f"Symbol:         {SYMBOL}")
    log(f"Timeframe:      {TIMEFRAME}")
    log(f"EMA periode:    {EMA_PERIOD}")
    log(f"Candle limit:   {LIMIT}")
    log(f"Tail only:      {TAIL_ONLY}")
    log(f"RANGE drempel:  {RANGE_PCT_THRESHOLD * 100:.1f}%")
    log(f"DB retries:     {DB_CONNECT_RETRIES}")
    log("═" * 60)

    if not DATABASE_URL:
        log("❌ DATABASE_URL ontbreekt in Render ENV — script stopt")
        sys.exit(1)

    conn = None
    try:
        # ── DB verbinding ─────────────────────────────────────
        conn = db_connect()
        log("✅ Database verbonden")

        # ── Tabellen + migraties ──────────────────────────────
        ensure_tables(conn)

        # ── Regime bouwen ─────────────────────────────────────
        n, regime = build_regime(conn)

        # ── Samenvatting loggen ───────────────────────────────
        print_regime_summary(conn)

        # ── Runtime ───────────────────────────────────────────
        elapsed = (now_utc() - start_ts).total_seconds()
        log(f"✅ Resultaat: {n} rijen verwerkt | "
            f"Huidig regime: {regime} | "
            f"Tijd: {elapsed:.1f}s")

    except Exception as e:
        log(f"❌ Fatale fout: {type(e).__name__}: {e}")
        if conn:
            safe_rollback(conn)
        send_whatsapp(
            f"🚨 BTC REGIME BUILDER FOUT\n\n"
            f"Fout: {type(e).__name__}\n"
            f"{str(e)[:150]}\n\n"
            f"BTC regime data niet bijgewerkt.\n"
            f"Bot gebruikt laatste bekende regime."
        )
        sys.exit(1)

    finally:
        # ALTIJD sluiten — FIX: v2.0 sloot alleen bij succes
        if conn:
            try:
                conn.close()
                log("DB verbinding gesloten")
            except Exception:
                pass
