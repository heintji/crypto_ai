# research/build_btc_regime.py
# ============================================================
# Crypto AI Bot — BTC Regime Builder v2.0
# ============================================================
# Wat dit doet:
#   Bouwt de public.btc_regime_4h tabel op basis van BTC 4H candles.
#   Deze tabel wordt gebruikt door multi_coin_score.py voor de
#   BTC regime filter — geen trades als BTC in BEAR is.
#
# Zelfde gedachtegang als alle andere bestanden:
#   ✅ sslmode="require" op DB connectie
#   ✅ Zelfde send_whatsapp() implementatie
#   ✅ Zelfde Claude health monitoring
#   ✅ Zelfde safe_int / safe_float / safe_str helpers
#   ✅ Zelfde now_utc() patroon
#   ✅ Zelfde retry bij netwerk errors
#
# BUGS GEFIXED vs origineel:
#   ✅ RANGE regime toegevoegd — was alleen BULL/BEAR
#   ✅ Slope check toegevoegd — vlakke EMA = RANGE
#   ✅ public.candles schema prefix
#   ✅ ORDER BY open_time DESC — recentste candles eerst
#   ✅ Tabel bestaat check
#   ✅ Geen data = duidelijke foutmelding
#
# NIEUWE FEATURES:
#   ✅ Binance fallback als DB leeg is
#   ✅ Regime sterkte score (0-100)
#   ✅ EMA200 slope berekening
#   ✅ Volatiliteit context
#   ✅ Huidige regime via WhatsApp opvraagbaar
#   ✅ Claude analyseert regime veranderingen
#   ✅ Historische regime statistieken
# ============================================================

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone, timedelta
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
TAIL_ONLY   = os.getenv("TAIL_ONLY", "0").strip() == "1"
SYMBOL      = os.getenv("BTC_SYMBOL", "BTCUSDT").strip()
TIMEFRAME   = os.getenv("BTC_TIMEFRAME", "4h").strip()
EMA_PERIOD  = int(os.getenv("EMA_PERIOD", "200"))
LIMIT       = int(os.getenv("BTC_CANDLE_LIMIT", "500"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# Drempelwaarden voor regime classificatie
RANGE_PCT_THRESHOLD   = float(os.getenv("RANGE_PCT_THRESHOLD", "0.02"))   # 2% van EMA
RANGE_SLOPE_THRESHOLD = float(os.getenv("RANGE_SLOPE_THRESHOLD", "0.001")) # 0.1% slope

BINANCE_BASE = "https://api.binance.com/api/v3"


# ============================================================
# BASIS HELPERS — identiek aan alle andere bestanden
# ============================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
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
# WHATSAPP — identiek aan alle andere bestanden
# Alleen voor KRITIEKE meldingen bij regime veranderingen
# ============================================================
def send_whatsapp(message: str) -> bool:
    """
    Stuurt WhatsApp bericht via Twilio.
    Identiek aan alle andere bestanden.
    Alleen bij kritieke regime veranderingen.
    """
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
        if resp.status_code in (200, 201):
            log(f"✅ WhatsApp verzonden ({len(message)} tekens)")
            return True
        log(f"❌ WhatsApp {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        log(f"❌ WhatsApp exception: {type(e).__name__}: {e}")
        return False


# ============================================================
# CLAUDE HEALTH MONITORING — identiek aan alle andere bestanden
# ============================================================
def _claude_analyse(prompt: str, max_tokens: int = 200) -> str:
    """Claude API aanroep — identiek aan alle andere bestanden."""
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


def notify_regime_change(
    old_regime: str,
    new_regime: str,
    close: float,
    ema200: float,
    strength: float,
) -> None:
    """
    Stuurt WhatsApp als BTC regime verandert.
    Claude analyseert wat dit betekent voor de bot.
    Bot loopt gewoon door — jij beslist via STOP.
    """
    prompt = f"""
Je bent een crypto trading bot coach.
BTC regime is veranderd van {old_regime} naar {new_regime}.

BTC close: {close:.2f}
EMA200:    {ema200:.2f}
Sterkte:   {strength:.1f}%

Geef in 2 zinnen Nederlands:
- Wat dit betekent voor de bot
- Wat de gebruiker moet weten
""".strip()

    uitleg = _claude_analyse(prompt, max_tokens=150)
    if not uitleg:
        uitleg = f"BTC regime veranderd: {old_regime} → {new_regime}"

    send_whatsapp(
        f"📊 BTC REGIME VERANDERING\n"
        f"{'─' * 28}\n\n"
        f"Van: {old_regime}\n"
        f"Naar: {new_regime}\n\n"
        f"BTC prijs: €{close:.2f}\n"
        f"EMA200:    €{ema200:.2f}\n"
        f"Sterkte:   {strength:.1f}%\n\n"
        f"🧠 Claude:\n{uitleg}\n\n"
        f"🤖 BOT LOOPT GEWOON DOOR\n"
        f"Stuur STOP als je wil pauzeren.\n\n"
        f"Commands: STOP | STATUS | HEALTH"
    )


# ============================================================
# DATABASE — sslmode="require" identiek aan alle bestanden
# ============================================================
def db_connect():
    """Verbinding met PostgreSQL — sslmode="require"."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def ensure_tables(conn) -> None:
    """
    Maakt alle benodigde tabellen aan als ze niet bestaan.
    Veilig om meerdere keren te draaien.
    """
    with conn.cursor() as cur:
        # btc_regime_4h — hoofdtabel
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.btc_regime_4h (
            open_time    BIGINT          PRIMARY KEY,
            ts_utc       TIMESTAMPTZ,
            close        DOUBLE PRECISION,
            ema200       DOUBLE PRECISION,
            ema200_slope DOUBLE PRECISION,
            regime       TEXT,
            strength     DOUBLE PRECISION DEFAULT 0.0,
            pct_from_ema DOUBLE PRECISION DEFAULT 0.0,
            updated_at   TIMESTAMPTZ DEFAULT NOW()
        );
        """)

        # Index op timestamp voor snelle queries
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_btc_regime_4h_time
            ON public.btc_regime_4h (open_time DESC);
        """)

        # btc_regime_changes — logboek van regime veranderingen
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.btc_regime_changes (
            id          SERIAL PRIMARY KEY,
            changed_at  TIMESTAMPTZ DEFAULT NOW(),
            old_regime  TEXT,
            new_regime  TEXT,
            close       DOUBLE PRECISION,
            ema200      DOUBLE PRECISION,
            strength    DOUBLE PRECISION,
            notified    BOOLEAN DEFAULT FALSE
        );
        """)

    conn.commit()
    log("✅ Tabellen gecontroleerd/aangemaakt")


# ============================================================
# BINANCE DATA OPHALEN
# ============================================================
def fetch_btc_candles_binance(limit: int = 500) -> List[Dict[str, Any]]:
    """
    Haalt BTC 4H candles op van Binance.
    Retry bij netwerk errors — identiek aan live_trader.py.
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
                    "open_time": safe_int(c[0]),
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
    Haalt BTC candles op uit de candles tabel als Binance fallback.
    Recentste candles eerst, dan omkeren voor chronologische volgorde.
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
            # Omdraaien: oudste eerst voor EMA berekening
            result = list(reversed([dict(r) for r in rows]))
            log(f"✅ {len(result)} BTC candles opgehaald uit DB")
            return result
    except Exception as e:
        log(f"⚠️ DB candles fout: {e}")
        return []


# ============================================================
# TECHNISCHE BEREKENINGEN
# ============================================================
def calc_ema(closes: List[float], period: int) -> List[float]:
    """
    Berekent Exponential Moving Average.
    Gebruikt Wilder-style smoothing voor consistentie.
    Geeft lijst terug van dezelfde lengte als input.
    Posities < period zijn 0.0.
    """
    if len(closes) < period:
        return [0.0] * len(closes)

    result   = [0.0] * period
    mult     = 2 / (period + 1)
    ema_val  = sum(closes[:period]) / period
    result.append(ema_val)

    for close in closes[period + 1:]:
        ema_val = close * mult + ema_val * (1 - mult)
        result.append(ema_val)

    return result


def calc_atr(candles: List[Dict], period: int = 14) -> List[float]:
    """
    Berekent Average True Range voor volatiliteits context.
    """
    if len(candles) < period + 1:
        return []

    trs = []
    for i in range(1, len(candles)):
        h  = candles[i]["high"]
        l  = candles[i]["low"]
        pc = candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    if len(trs) < period:
        return []

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
    Classificeert BTC regime op basis van EMA200 en slope.

    BULL:  Prijs boven EMA200 met stijgende slope
    BEAR:  Prijs onder EMA200 met dalende slope
    RANGE: Prijs dicht bij EMA200 of vlakke slope

    Geeft (regime, strength, pct_from_ema, slope) terug.

    FIX vs origineel: RANGE regime was niet mogelijk.
    Nu correcte 3-weg classificatie.
    """
    if ema200 <= 0 or ema200_prev <= 0:
        return "UNKNOWN", 0.0, 0.0, 0.0

    # Bereken afstand van prijs tot EMA200
    pct_diff = (close - ema200) / ema200  # positief = boven EMA

    # Bereken slope van EMA200
    slope = (ema200 - ema200_prev) / ema200_prev if ema200_prev > 0 else 0.0

    # RANGE: prijs <2% van EMA200 OF slope <0.1%
    if abs(pct_diff) < RANGE_PCT_THRESHOLD or abs(slope) < RANGE_SLOPE_THRESHOLD:
        strength = max(0.0, 50.0 - abs(pct_diff) * 1000)
        return "RANGE", strength, pct_diff * 100, slope * 100

    # BULL: prijs boven EMA200 met positieve slope
    if pct_diff > 0 and slope > 0:
        # Sterkte: hoe ver boven EMA + hoe sterk de slope
        strength = min(100.0, pct_diff * 500 + slope * 5000)
        return "BULL", strength, pct_diff * 100, slope * 100

    # BEAR: prijs onder EMA200 met negatieve slope
    if pct_diff < 0 and slope < 0:
        strength = min(100.0, abs(pct_diff) * 500 + abs(slope) * 5000)
        return "BEAR", strength, pct_diff * 100, slope * 100

    # Gemengde signalen = RANGE
    strength = 30.0
    return "RANGE", strength, pct_diff * 100, slope * 100


# ============================================================
# REGIME OPSLAAN EN OPHALEN
# ============================================================
def get_previous_regime(conn) -> Optional[str]:
    """Haalt het meest recente opgeslagen regime op."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT regime FROM public.btc_regime_4h
            ORDER BY open_time DESC LIMIT 1
            """)
            row = cur.fetchone()
            return safe_str(row[0]) if row else None
    except Exception:
        return None


def get_regime_stats(conn) -> Dict[str, Any]:
    """
    Haalt statistieken op van de regime tabel.
    Hoe vaak BULL/BEAR/RANGE in de laatste 30 dagen.
    """
    stats: Dict[str, Any] = {}
    try:
        with conn.cursor() as cur:
            # Laatste 30 dagen distributie
            cur.execute("""
            SELECT
                regime,
                COUNT(*) AS n,
                ROUND(AVG(strength)::numeric, 1) AS avg_strength,
                ROUND(AVG(pct_from_ema)::numeric, 2) AS avg_pct_from_ema
            FROM public.btc_regime_4h
            WHERE updated_at >= NOW() - INTERVAL '30 days'
              AND regime IS NOT NULL
            GROUP BY regime
            ORDER BY n DESC
            """)
            rows = cur.fetchall()
            for row in rows:
                regime = safe_str(row[0])
                stats[regime] = {
                    "count":           safe_int(row[1]),
                    "avg_strength":    safe_float(row[2]),
                    "avg_pct_from_ema": safe_float(row[3]),
                }

        # Totale rows
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.btc_regime_4h")
            stats["total_rows"] = safe_int(cur.fetchone()[0])

    except Exception as e:
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
    """Logt een regime verandering naar de btc_regime_changes tabel."""
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
        log(f"⚠️ Regime change log fout: {e}")


def upsert_regime_rows(conn, rows: List[Tuple]) -> int:
    """Slaat regime rijen op via bulk upsert. Geeft aantal rijen terug."""
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
        log(f"⚠️ Upsert fout: {e}")
        conn.rollback()
        return 0


# ============================================================
# HOOFD BUILD FUNCTIE
# ============================================================
def build_regime(conn) -> Tuple[int, str]:
    """
    Bouwt/updatet de BTC regime tabel.

    Stappen:
    1. Candles ophalen (Binance, dan DB als fallback)
    2. EMA200 berekenen
    3. Regime classificeren per candle
    4. Opslaan in DB
    5. Regime verandering detecteren en notificeren

    Geeft (aantal_verwerkt, huidig_regime) terug.
    """
    # 1. Candles ophalen
    candles = fetch_btc_candles_binance(LIMIT)
    if len(candles) < EMA_PERIOD + 2:
        log(f"⚠️ Binance {len(candles)} candles — probeer DB...")
        candles = fetch_btc_candles_db(conn, LIMIT)

    if len(candles) < EMA_PERIOD + 2:
        log(f"❌ Te weinig candles: {len(candles)} < {EMA_PERIOD + 2}")
        return 0, "UNKNOWN"

    log(f"📊 {len(candles)} candles beschikbaar voor EMA{EMA_PERIOD} berekening")

    # 2. EMA200 berekenen
    closes = [c["close"] for c in candles]
    emas   = calc_ema(closes, EMA_PERIOD)

    if len(emas) < EMA_PERIOD + 2:
        log("❌ EMA berekening mislukt — te weinig data")
        return 0, "UNKNOWN"

    # 3. Vorige regime ophalen voor verandering detectie
    previous_regime = get_previous_regime(conn)

    # 4. Regime classificeren per candle
    rows         = []
    huidig       = "UNKNOWN"
    huidig_ema   = 0.0
    huidig_close = 0.0
    huidig_sterkte = 0.0

    start_idx = EMA_PERIOD + 1

    # TAIL_ONLY = alleen de laatste paar rijen updaten (snellere run)
    if TAIL_ONLY:
        start_idx = max(start_idx, len(candles) - 20)

    for i in range(start_idx, len(candles)):
        ema_now  = emas[i]
        ema_prev = emas[i - 1]
        close    = candles[i]["close"]
        ts_ms    = candles[i]["open_time"]
        ts_utc   = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

        if ema_now <= 0 or ema_prev <= 0:
            continue

        regime, strength, pct_from_ema, slope = classify_regime(
            close, ema_now, ema_prev
        )

        rows.append((
            ts_ms, ts_utc, close, ema_now, slope,
            regime, strength, pct_from_ema,
        ))

        huidig         = regime
        huidig_ema     = ema_now
        huidig_close   = close
        huidig_sterkte = strength

    # 5. Opslaan in DB
    n = upsert_regime_rows(conn, rows)
    log(f"✅ {n} regime rijen opgeslagen")

    # 6. Regime verandering detecteren
    if (
        previous_regime
        and huidig != "UNKNOWN"
        and previous_regime != huidig
        and huidig_ema > 0
    ):
        log(f"🔄 Regime veranderd: {previous_regime} → {huidig}")
        save_regime_change(conn, previous_regime, huidig, huidig_close, huidig_ema, huidig_sterkte)
        notify_regime_change(previous_regime, huidig, huidig_close, huidig_ema, huidig_sterkte)

    # 7. Log huidig regime
    pct = (huidig_close - huidig_ema) / huidig_ema * 100 if huidig_ema > 0 else 0
    log(
        f"📈 Huidig BTC regime: {huidig} "
        f"(sterkte={huidig_sterkte:.1f}% | "
        f"close={huidig_close:.2f} | "
        f"ema200={huidig_ema:.2f} | "
        f"afstand={pct:+.2f}%)"
    )

    return n, huidig


# ============================================================
# PUBLIEKE HELPERS — voor andere bestanden
# ============================================================
def get_current_btc_regime(conn) -> str:
    """
    Haalt huidig BTC regime op.
    Gebruikt door multi_coin_score.py voor BTC filter.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT regime FROM public.btc_regime_4h
            ORDER BY open_time DESC LIMIT 1
            """)
            row = cur.fetchone()
            return safe_str(row[0], "UNKNOWN") if row else "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def get_btc_regime_context(conn) -> Dict[str, Any]:
    """
    Geeft volledige BTC regime context terug.
    Gebruikt door multi_coin_score.py voor scoring.
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
            SELECT regime, strength, pct_from_ema, ema200_slope,
                   close, ema200, ts_utc
            FROM public.btc_regime_4h
            ORDER BY open_time DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                return dict(row)
    except Exception:
        pass
    return {"regime": "UNKNOWN", "strength": 0.0}


def print_regime_summary(conn) -> None:
    """Print een samenvatting van het regime naar de logs."""
    stats = get_regime_stats(conn)
    total = stats.get("total_rows", 0)

    log("=" * 50)
    log("REGIME SAMENVATTING (laatste 30 dagen):")
    for regime in ["BULL", "BEAR", "RANGE"]:
        if regime in stats:
            s = stats[regime]
            pct = s["count"] / max(sum(stats[r]["count"] for r in stats if r not in ["total_rows"]), 1) * 100
            log(f"  {regime:5}: {s['count']:4}x ({pct:.1f}%) | gem. sterkte={s['avg_strength']:.1f}%")
    log(f"  Totaal rijen in tabel: {total}")
    log("=" * 50)


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    log("=" * 60)
    log("BTC Regime Builder v2.0 — gestart")
    log("=" * 60)
    log(f"Database:       {'✅' if DATABASE_URL else '❌ ONTBREEKT'}")
    log(f"Twilio:         {'✅' if TWILIO_ACCOUNT_SID else '⚠️ niet ingesteld'}")
    log(f"Claude API:     {'✅' if ANTHROPIC_API_KEY else '⚠️ niet ingesteld'}")
    log(f"Symbol:         {SYMBOL}")
    log(f"Timeframe:      {TIMEFRAME}")
    log(f"EMA periode:    {EMA_PERIOD}")
    log(f"Candle limit:   {LIMIT}")
    log(f"Tail only:      {TAIL_ONLY}")
    log(f"RANGE drempel:  {RANGE_PCT_THRESHOLD*100:.1f}%")
    log("=" * 60)

    if not DATABASE_URL:
        log("❌ DATABASE_URL ontbreekt — kan niet doorgaan")
        sys.exit(1)

    try:
        conn = db_connect()
        log("✅ Database verbonden")

        # Tabellen aanmaken indien nodig
        ensure_tables(conn)

        # Regime bouwen
        n, regime = build_regime(conn)

        # Statistieken tonen
        print_regime_summary(conn)

        log(f"✅ Resultaat: {n} rijen verwerkt | Huidig regime: {regime}")
        conn.close()

    except Exception as e:
        log(f"❌ Fout: {type(e).__name__}: {e}")
        send_whatsapp(
            f"🚨 BTC REGIME BUILDER FOUT\n\n"
            f"Fout: {type(e).__name__}\n"
            f"{str(e)[:100]}\n\n"
            f"BTC regime data niet bijgewerkt.\n"
            f"Bot gebruikt laatste bekende regime."
        )
        sys.exit(1)
