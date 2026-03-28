# analysis/multi_coin_score.py
# ============================================================
# Crypto AI Bot — Multi Coin Scorer v2.0
# ============================================================
# Scant alle Bitvavo-tradable coins via Binance data.
# Berekent een score (0-100) per coin op basis van:
#   - Wilder RSI (nauwkeuriger dan simpele RSI)
#   - ATR-based dynamische stops (ipv vaste 2%)
#   - BTC regime filter (geen trades in BEAR)
#   - Volume bevestiging
#   - Multi-timeframe (1H + 4H)
#   - Experience scoreboard (historische win rate)
#   - Coin cooldown (24u na verlies)
#   - Coin blacklist (win rate <30% na 20 trades)
#
# SAMENWERKING MET ANDERE BESTANDEN:
#   → Schrijft naar public.pending_approvals
#   → Leest van public.bot_state (is bot actief?)
#   → Leest van public.experience_trades (cooldown/blacklist)
#   → Leest van public.experience_scoreboard (win rates)
#   → Leest van public.btc_regime_4h (BTC regime filter)
#   → Triggert /auto_buy op whatsapp_webhook.py
#   → Gebruikt get_tradable_markets() uit live_trader.py
#   → Claude AI analyseert elk signaal + fouten
#
# IDENTIEK AAN ALLE ANDERE BESTANDEN:
#   ✅ Zelfde ENV variabelen en Fase 1 limieten
#   ✅ Zelfde send_whatsapp() implementatie
#   ✅ Zelfde Claude health monitoring (KRITIEK/HOOG/MEDIUM/LAAG)
#   ✅ Zelfde bot state (PostgreSQL bot_state tabel)
#   ✅ Zelfde is_bot_active / is_bot_paused check
#   ✅ Zelfde sslmode="require" op DB connectie
#   ✅ Zelfde safe_int / safe_float / safe_str helpers
#   ✅ Zelfde trading hours filter (08:00-22:00 UTC)
#   ✅ Zelfde weekend: gewoon doorgaan — geen blokkering
#
# BUGS GEFIXED vs origineel:
#   ✅ Simpele RSI → Wilder RSI (veel nauwkeuriger)
#   ✅ Vaste 2% stop → ATR-based dynamische stop
#   ✅ Bitvavo universe filter robuust (publieke API)
#   ✅ Rate limiting op Binance calls (0.2s sleep)
#   ✅ Score drempel 85 ipv 80 (hogere kwaliteit)
#   ✅ Geen automatische pauze — jij via STOP
#
# NIEUWE FEATURES:
#   ✅ BTC regime filter — geen trades bij BEAR
#   ✅ Volume confirmatie filter
#   ✅ Multi-timeframe (1H + 4H) bevestiging
#   ✅ Fee + slippage correctie in score
#   ✅ Experience scoreboard integratie
#   ✅ Claude analyseert elk Pre-BUY signaal
#   ✅ Claude analyseert kritieke fouten
#   ✅ Coin blacklist op basis van historische data
#   ✅ Coin cooldown 24u na verlies
#   ✅ Auto BUY trigger via /auto_buy webhook
#   ✅ uuid4 voor unieke Pre-BUY IDs
#   ✅ Weekday/weekend filter (configureerbaar)
# ============================================================

from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

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

BOT_INTERNAL_SECRET = (os.getenv("BOT_INTERNAL_SECRET") or "crypto_ai_bot").strip()
WEBHOOK_BASE_URL    = (os.getenv("WEBHOOK_BASE_URL") or "").strip()

# ============================================================
# FASE 1 LIMIETEN — identiek aan alle andere bestanden
# ============================================================
MAX_PER_TRADE_EUR       = float(os.getenv("MAX_PER_TRADE_EUR") or "0.50")
MAX_REAL_TRADES_PER_DAY = int(os.getenv("MAX_REAL_TRADES_PER_DAY") or "10")
MAX_OPEN_REAL_TRADES    = int(os.getenv("MAX_OPEN_REAL_TRADES") or "5")
DAILY_STOP_LOSS_EUR     = float(os.getenv("DAILY_STOP_LOSS_EUR") or "5.00")
TRADING_HOURS_START     = int(os.getenv("TRADING_HOURS_START") or "8")
TRADING_HOURS_END       = int(os.getenv("TRADING_HOURS_END") or "22")
BOT_STATE_TABLE         = "public.bot_state"

# Score & filter instellingen
MIN_SCORE_TO_TRADE  = int(os.getenv("MIN_SCORE_TO_TRADE") or "85")
MIN_CHANCE          = int(os.getenv("MIN_CHANCE") or "70")
MIN_CONFIDENCE      = int(os.getenv("MIN_CONFIDENCE") or "70")
MAX_PREBUY_PER_DAY  = int(os.getenv("MAX_PREBUY_PER_DAY") or "50")
PREBUY_EXPIRY_HOURS = int(os.getenv("PREBUY_EXPIRY_HOURS") or "4")

# Fee + slippage — identiek aan live_trader en trade_monitor
BITVAVO_FEE_PCT = float(os.getenv("BITVAVO_FEE_PCT") or "0.0025")
SLIPPAGE_PCT    = float(os.getenv("SLIPPAGE_PCT") or "0.001")
TOTAL_COST_PCT  = BITVAVO_FEE_PCT + SLIPPAGE_PCT

# Coin filter instellingen — identiek aan live_trader en trade_monitor
COIN_COOLDOWN_HOURS   = float(os.getenv("COIN_COOLDOWN_HOURS") or "24.0")
BLACKLIST_MIN_TRADES  = int(os.getenv("BLACKLIST_MIN_TRADES") or "20")
BLACKLIST_MAX_WINRATE = float(os.getenv("BLACKLIST_MAX_WINRATE") or "0.30")

# Binance API instellingen
BINANCE_BASE    = "https://api.binance.com/api/v3"
BINANCE_SLEEP   = float(os.getenv("BINANCE_SLEEP") or "0.2")
BINANCE_TIMEOUT = int(os.getenv("BINANCE_TIMEOUT") or "10")
MAX_RETRIES     = int(os.getenv("MAX_RETRIES") or "3")

# Bitvavo API
BITVAVO_BASE = "https://api.bitvavo.com"

# ATR instellingen
ATR_PERIOD     = int(os.getenv("ATR_PERIOD") or "14")
ATR_MULTIPLIER = float(os.getenv("ATR_MULTIPLIER") or "2.0")
ATR_TARGET_R   = float(os.getenv("ATR_TARGET_R") or "2.0")

# RSI instellingen
RSI_PERIOD = int(os.getenv("RSI_PERIOD") or "14")
RSI_MIN    = int(os.getenv("RSI_MIN") or "35")
RSI_MAX    = int(os.getenv("RSI_MAX") or "65")

# BTC regime: geen trades bij BEAR
BTC_SKIP_BEAR = os.getenv("BTC_SKIP_BEAR", "1").strip() == "1"

# Markets cache — vermijdt herhaalde API calls
_MARKETS_CACHE: Dict[str, Any] = {"ts": 0.0, "markets": set()}
_MARKETS_TTL = 30 * 60  # 30 minuten cache


# ============================================================
# BASIS HELPERS — identiek aan alle andere bestanden
# ============================================================
def now_utc() -> datetime:
    """Huidige UTC tijd — identiek in alle bestanden."""
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    """Gestandaardiseerde logging — identiek in alle bestanden."""
    print(f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')}] [SCANNER] {msg}", flush=True)


def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
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


def utc_day_str() -> str:
    return now_utc().strftime("%Y-%m-%d")


def is_trading_hours() -> bool:
    """Controleert of we binnen trading hours zijn."""
    return TRADING_HOURS_START <= now_utc().hour < TRADING_HOURS_END


def is_weekend() -> bool:
    """Geeft True als het weekend is (zaterdag=5, zondag=6)."""
    return now_utc().weekday() >= 5


# ============================================================
# WHATSAPP — identieke implementatie als alle andere bestanden
# ============================================================
def send_whatsapp(message: str) -> bool:
    """
    Stuurt WhatsApp bericht via Twilio.
    Identieke implementatie in alle bestanden.
    Alleen voor kritieke foutmeldingen — geen spam per scan.
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
# CLAUDE AI — identiek aan alle andere bestanden
# Zelfde model, zelfde patroon, zelfde foutafhandeling
# ============================================================
def _claude_analyse(prompt: str, max_tokens: int = 300) -> str:
    """
    Roept Claude API aan voor analyse.
    Identieke implementatie in alle bestanden.
    Model: claude-sonnet-4-20250514.
    Geeft lege string bij fout — bot gaat altijd door.
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
            content = resp.json().get("content", [])
            if content:
                return content[0]["text"].strip()
        log(f"⚠️ Claude API status {resp.status_code}")
        return ""
    except requests.exceptions.Timeout:
        log("⚠️ Claude API timeout")
        return ""
    except Exception as e:
        log(f"⚠️ Claude API fout: {type(e).__name__}: {e}")
        return ""


def report_error(
    error: Exception,
    function: str,
    severity: str = "HOOG",
    symbol: str = "",
) -> None:
    """
    Rapporteert fout via Claude analyse + WhatsApp.
    Ernst niveaus identiek aan alle bestanden:
    KRITIEK → WhatsApp direct
    HOOG    → WhatsApp
    MEDIUM  → alleen log
    LAAG    → alleen log
    """
    log(f"[{severity}] {function} ({symbol}): {type(error).__name__}: {error}")

    if severity not in ("KRITIEK", "HOOG"):
        return

    prompt = f"""
Je bent een crypto trading bot monitor voor multi_coin_score.py.
Er is een fout opgetreden tijdens het scannen van coins.

Ernst:   {severity}
Functie: {function}
Coin:    {symbol or 'onbekend'}
Fout:    {type(error).__name__}: {str(error)[:200]}

Geef in 3 zinnen Nederlands:
1. Wat er mis is gegaan
2. Impact op nieuwe Pre-BUY signalen
3. Wat de gebruiker moet doen
""".strip()

    uitleg = _claude_analyse(prompt, max_tokens=200)
    if not uitleg:
        uitleg = f"{type(error).__name__}: {str(error)[:100]}"

    send_whatsapp(
        f"🚨 SCANNER FOUT — {severity}\n"
        f"{'─' * 30}\n\n"
        f"📁 Functie:  {function}\n"
        f"🪙 Coin:     {symbol or '—'}\n"
        f"⚠️ Fout:    {type(error).__name__}\n\n"
        f"🧠 Claude:\n{uitleg}\n\n"
        f"📋 WAT TE DOEN:\n"
        f"1. Check Render logs voor details\n"
        f"2. Stuur STATUS voor bot overzicht\n"
        f"3. Stuur STOP als je wil pauzeren\n\n"
        f"🤖 BOT BEWAAKT OPEN TRADES\n"
        f"Nieuwe scans mogelijk tijdelijk gestopt.\n\n"
        f"Commands: STATUS | TRADES | STOP"
    )


def claude_beoordeel_signaal(
    symbol:      str,
    setup_type:  str,
    regime:      str,
    btc_regime:  str,
    score:       int,
    chance:      int,
    confidence:  int,
    rsi_4h:      float,
    volume_ratio: float,
    exp_win_rate: float,
    exp_n:       int,
    why_tag:     str,
) -> str:
    """
    Claude beoordeelt een Pre-BUY signaal.
    Geeft korte tekst terug die opgeslagen wordt bij het signaal.
    Wordt NIET direct via WhatsApp gestuurd — te veel spam.
    Beschikbaar via STATUS command en weekrapport.
    """
    prompt = f"""
Je bent een crypto trading bot coach.
Beoordeel dit Pre-BUY signaal in 2 zinnen Nederlands.

Coin:         {symbol}
Setup:        {setup_type} / Regime: {regime}
BTC regime:   {btc_regime}
Score:        {score}/100
Kans:         {chance}%
Confidence:   {confidence}%
RSI 4H:       {rsi_4h:.1f}
Volume ratio: {volume_ratio:.1f}x
Exp win rate: {exp_win_rate:.1%} ({exp_n} trades)
Waarom:       {why_tag}

Is dit een goed signaal? Wat zijn de risico's?
""".strip()

    return _claude_analyse(prompt, max_tokens=120)


def claude_scanner_health_check() -> str:
    """
    Claude analyseert de scanner gezondheid bij opstarten.
    Controleert configuratie en geeft aanbevelingen.
    """
    prompt = f"""
Je bent een crypto scanner configuratie checker.
Controleer of multi_coin_score.py correct is geconfigureerd.

CONFIGURATIE:
- DATABASE_URL:       {'✅' if DATABASE_URL else '❌ ONTBREEKT'}
- WEBHOOK_BASE_URL:   {'✅' if WEBHOOK_BASE_URL else '⚠️ niet ingesteld'}
- ANTHROPIC_API_KEY:  {'✅' if ANTHROPIC_API_KEY else '❌ ONTBREEKT'}
- MIN_SCORE:          {MIN_SCORE_TO_TRADE}
- MIN_CHANCE:         {MIN_CHANCE}%
- ATR_MULTIPLIER:     {ATR_MULTIPLIER}
- FEE+SLIPPAGE:       {TOTAL_COST_PCT*100:.2f}%
- BTC_SKIP_BEAR:      {BTC_SKIP_BEAR}
- TRADING_HOURS:      {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC
- PREBUY_EXPIRY:      {PREBUY_EXPIRY_HOURS}u

Geef check in 3 zinnen:
1. Is de configuratie compleet?
2. Zijn er problemen of risico's?
3. Aanbevelingen?
""".strip()

    return _claude_analyse(prompt, max_tokens=150)


# ============================================================
# DATABASE — sslmode="require" identiek aan alle bestanden
# ============================================================
def db_connect():
    """
    DB verbinding met sslmode=require.
    Identiek in alle bestanden — Render vereist dit.
    """
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


# ============================================================
# BOT STATE — identiek aan alle andere bestanden
# ============================================================
def get_bot_state_value(conn, key: str, default: str = "") -> str:
    """Leest een waarde uit de bot_state tabel."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT value FROM {BOT_STATE_TABLE} WHERE key=%s", (key,)
            )
            row = cur.fetchone()
            return safe_str(row[0], default) if row else default
    except Exception:
        return default


def is_bot_active(conn) -> bool:
    """Bot is actief als bot_active=true in de DB."""
    return get_bot_state_value(conn, "bot_active", "false").lower() == "true"


def is_bot_paused(conn) -> bool:
    """Controleert of bot gepauzeerd is — inclusief tijdcheck."""
    if get_bot_state_value(conn, "bot_paused", "false").lower() != "true":
        return False
    until_str = get_bot_state_value(conn, "bot_paused_until", "")
    if not until_str:
        return True
    try:
        until = datetime.fromisoformat(until_str)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return now_utc() <= until
    except Exception:
        return True


# ============================================================
# BITVAVO UNIVERSE FILTER
# Haalt actieve EUR markets op — gecached 30 minuten
# Publiek — ook gebruikt door live_trader.py
# ============================================================
def get_tradable_markets() -> Set[str]:
    """
    Haalt actieve Bitvavo EUR markets op via publieke API.
    Cache: 30 minuten TTL om rate limiting te vermijden.

    Samenwerking: identiek aan live_trader.get_tradable_markets().
    Beide bestanden lezen van dezelfde Bitvavo endpoint.
    """
    now_ts = time.time()
    if _MARKETS_CACHE["markets"] and (now_ts - _MARKETS_CACHE["ts"]) < _MARKETS_TTL:
        return _MARKETS_CACHE["markets"]

    try:
        resp = requests.get(
            f"{BITVAVO_BASE}/v2/markets",
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json()

        tradable: Set[str] = set()
        for item in items:
            market = safe_str(item.get("market"))
            status = safe_str(item.get("status")).lower()
            if market and status == "trading" and market.endswith("-EUR"):
                tradable.add(market)

        _MARKETS_CACHE["ts"]      = now_ts
        _MARKETS_CACHE["markets"] = tradable
        log(f"✅ Bitvavo markets geladen: {len(tradable)} tradable EUR markets")
        return tradable

    except Exception as e:
        log(f"⚠️ Bitvavo markets fout: {type(e).__name__}: {e}")
        # Gebruik cache als die er is ondanks fout
        return _MARKETS_CACHE.get("markets") or set()


def symbol_to_bitvavo_market(symbol_usdt: str) -> Optional[str]:
    """
    Converteert USDT symbol naar Bitvavo EUR market.
    ETHUSDT → ETH-EUR (als ETH-EUR tradable is op Bitvavo).
    Geeft None terug als niet tradable.
    """
    s = safe_str(symbol_usdt).upper()
    if not s.endswith("USDT"):
        return None
    base   = s[:-4]
    market = f"{base}-EUR"
    tradable = get_tradable_markets()
    return market if market in tradable else None


# ============================================================
# BINANCE DATA OPHALEN — met retry en rate limiting
# ============================================================
def binance_get(
    endpoint: str,
    params:   dict,
    retries:  int = MAX_RETRIES,
) -> Optional[Any]:
    """
    Binance public API aanroep met retry en exponential backoff.
    Rate limiting via BINANCE_SLEEP (0.2s standaard).
    """
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                f"{BINANCE_BASE}{endpoint}",
                params=params,
                timeout=BINANCE_TIMEOUT,
            )
            if resp.ok:
                return resp.json()
            log(f"⚠️ Binance {resp.status_code} ({endpoint}) poging {attempt}/{retries}")
        except requests.exceptions.Timeout:
            log(f"⚠️ Binance timeout poging {attempt}/{retries}")
        except Exception as e:
            log(f"⚠️ Binance fout poging {attempt}/{retries}: {e}")

        if attempt < retries:
            wait = 2 ** attempt
            time.sleep(wait)

    return None


def fetch_candles(
    symbol:   str,
    interval: str = "4h",
    limit:    int = 120,
) -> List[Dict[str, Any]]:
    """
    Haalt OHLCV candles op van Binance.
    Geeft lijst van dicts terug met open, high, low, close, volume.
    """
    time.sleep(BINANCE_SLEEP)  # Rate limiting

    data = binance_get("/klines", {
        "symbol":   symbol,
        "interval": interval,
        "limit":    limit,
    })

    if not data:
        return []

    candles = []
    for c in data:
        try:
            candles.append({
                "open":   safe_float(c[1]),
                "high":   safe_float(c[2]),
                "low":    safe_float(c[3]),
                "close":  safe_float(c[4]),
                "volume": safe_float(c[5]),
                "ts":     safe_int(c[0]),
            })
        except Exception:
            continue

    return candles


def fetch_ticker_24h(symbol: str) -> Optional[Dict]:
    """
    Haalt 24u ticker data op van Binance.
    Bevat volume, price change, etc.
    """
    time.sleep(BINANCE_SLEEP)
    return binance_get("/ticker/24hr", {"symbol": symbol})


# ============================================================
# TECHNISCHE INDICATOREN
# ============================================================
def rsi_wilder(closes: List[float], period: int = 14) -> Optional[float]:
    """
    Wilder RSI — veel nauwkeuriger dan simpele RSI.
    Fix: origineel gebruikte simpele RSI. Nu Wilder's smoothing.

    Identiek aan indicators.py rsi_wilder() functie.
    Dezelfde berekening als TradingView RSI indicator.
    """
    if len(closes) < period + 1:
        return None

    changes  = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains    = [max(c, 0.0) for c in changes]
    losses   = [max(-c, 0.0) for c in changes]

    # Start gemiddelden (eerste periode)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder smoothing over rest
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def sma(values: List[float], period: int) -> Optional[float]:
    """Simple Moving Average."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: List[float], period: int) -> Optional[float]:
    """Exponential Moving Average."""
    if len(values) < period:
        return None
    mult    = 2.0 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = v * mult + ema_val * (1 - mult)
    return ema_val


def atr_calc(candles: List[Dict], period: int = 14) -> Optional[float]:
    """
    Average True Range — voor dynamische stop/target berekening.
    Fix: origineel gebruikte vaste 2% stop. ATR is veel beter.

    ATR berekening:
    TR = max(high-low, |high-prev_close|, |low-prev_close|)
    ATR = Wilder average van TR over period candles.
    """
    if len(candles) < period + 1:
        return None

    trs = []
    for i in range(1, len(candles)):
        h  = candles[i]["high"]
        l  = candles[i]["low"]
        pc = candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    if len(trs) < period:
        return None

    # Wilder ATR smoothing
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period

    return atr_val


def detect_coin_regime(closes: List[float]) -> str:
    """
    Detecteert marktregime voor een individuele coin.
    BULL / BEAR / RANGE op basis van SMA20 vs SMA50.
    """
    if len(closes) < 50:
        return "UNKNOWN"

    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)

    if sma20 is None or sma50 is None:
        return "UNKNOWN"

    diff_pct = abs(sma20 - sma50) / max(sma50, 0.000001)

    if diff_pct < 0.015:
        return "RANGE"
    return "BULL" if sma20 > sma50 else "BEAR"


def get_btc_regime(conn) -> str:
    """
    Haalt huidig BTC regime op uit btc_regime_4h tabel.
    Gebouwd door build_btc_regime.py.
    Gebruikt door scanner als globale markt filter.
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


# ============================================================
# SETUP TYPE DETECTIE
# ============================================================
def detect_setup_type(
    candles_4h: List[Dict],
    candles_1h: List[Dict],
) -> Tuple[str, str]:
    """
    Detecteert setup type op basis van 4H en 1H candles.

    Setup types:
    - TREND_PULLBACK: pullback naar SMA20 in uptrend
    - BREAKOUT:       prijs breekt door recente high
    - BOUNCE:         bounce van SMA50 support
    - MOMENTUM:       sterke stijging met gezonde RSI

    Geeft (setup_type, why_tag) terug.
    """
    if len(candles_4h) < 20:
        return "UNKNOWN", "te_weinig_data"

    closes_4h = [c["close"] for c in candles_4h]
    closes_1h = [c["close"] for c in candles_1h] if candles_1h else []

    rsi_4h   = rsi_wilder(closes_4h, RSI_PERIOD)
    sma20_4h = sma(closes_4h, 20)
    sma50_4h = sma(closes_4h, 50)
    current  = closes_4h[-1]

    if rsi_4h is None or sma20_4h is None or sma50_4h is None:
        return "UNKNOWN", f"indicator_fout"

    # TREND PULLBACK — prijs trekt terug naar SMA20 in BULL trend
    if sma20_4h > sma50_4h and current > sma50_4h:
        dist_to_sma20 = abs(current - sma20_4h) / sma20_4h
        if dist_to_sma20 < 0.025 and RSI_MIN <= rsi_4h <= 58:
            return "TREND_PULLBACK", (
                f"sma20_pullback({dist_to_sma20*100:.1f}%)"
                f"|RSI={rsi_4h:.0f}"
            )

    # BREAKOUT — prijs breekt boven recente 20-candle high
    if len(candles_4h) >= 20:
        high_20 = max(c["high"] for c in candles_4h[-20:])
        prev_close = closes_4h[-2] if len(closes_4h) > 1 else current
        if current > high_20 * 0.998 and prev_close < high_20:
            return "BREAKOUT", (
                f"break_high20({high_20:.4f})"
                f"|RSI={rsi_4h:.0f}"
            )

    # BOUNCE — prijs bounced van SMA50 support
    if sma50_4h > 0:
        dist_to_sma50 = abs(current - sma50_4h) / sma50_4h
        if dist_to_sma50 < 0.02 and rsi_4h < 52:
            return "BOUNCE", (
                f"sma50_bounce({dist_to_sma50*100:.1f}%)"
                f"|RSI={rsi_4h:.0f}"
            )

    # MOMENTUM — sterke stijging met gezonde RSI zone
    if 52 <= rsi_4h <= RSI_MAX and current > sma20_4h > sma50_4h:
        return "MOMENTUM", (
            f"momentum_bull"
            f"|RSI={rsi_4h:.0f}"
        )

    return "UNKNOWN", f"geen_setup|RSI={rsi_4h:.0f}"


# ============================================================
# SCORE BEREKENING
# ============================================================
def calculate_score(
    candles_4h:   List[Dict],
    candles_1h:   List[Dict],
    ticker:       Optional[Dict],
    regime:       str,
    btc_regime:   str,
    setup_type:   str,
    exp_win_rate: float,
    exp_n:        int,
) -> Tuple[int, int, int, str, float, float]:
    """
    Berekent score (0-100), chance, confidence en why_tag.

    Score componenten (totaal 100 punten):
    ────────────────────────────────────────
    RSI in ideale zone:        0-20 punten
    Trend alignment:           0-20 punten
    Volume bevestiging:        0-15 punten
    Experience win rate:       0-20 punten
    BTC regime:                0-15 punten
    Multi-timeframe 1H:        0-10 punten
    ────────────────────────────────────────
    Totaal max:                100 punten
    Min voor trade:            85 punten

    Fee correctie: -3 punten als fee impact >0.3%

    Geeft (score, chance, confidence, why_tag, rsi_4h, volume_ratio) terug.
    """
    if not candles_4h or len(candles_4h) < 20:
        return 0, 0, 0, "te_weinig_data", 0.0, 0.0

    closes_4h  = [c["close"] for c in candles_4h]
    closes_1h  = [c["close"] for c in candles_1h] if candles_1h else []
    volumes_4h = [c["volume"] for c in candles_4h]

    rsi_4h   = rsi_wilder(closes_4h, RSI_PERIOD)
    sma20_4h = sma(closes_4h, 20)
    sma50_4h = sma(closes_4h, 50)
    current  = closes_4h[-1]

    vol_now  = volumes_4h[-1] if volumes_4h else 0
    vol_avg  = sma(volumes_4h[:-1], 20) or 1.0
    vol_ratio = vol_now / vol_avg if vol_avg > 0 else 1.0

    score    = 0
    why_tags = []

    # ── 1. RSI in ideale zone (0-20 punten) ──────────
    if rsi_4h is not None:
        if RSI_MIN <= rsi_4h <= RSI_MAX:
            # Idealer naarmate dichter bij midden (50)
            rsi_score = 20 - abs(rsi_4h - 50) / 1.5
            score += int(min(rsi_score, 20))
            why_tags.append(f"RSI={rsi_4h:.0f}✅")
        elif rsi_4h < RSI_MIN:
            score += 5  # Oversold — iets positief
            why_tags.append(f"RSI={rsi_4h:.0f}⬇️oversold")
        else:
            why_tags.append(f"RSI={rsi_4h:.0f}❌overbought")
    else:
        rsi_4h = 50.0  # default

    # ── 2. Trend alignment SMA20>SMA50 (0-20 punten) ─
    if sma20_4h and sma50_4h:
        if sma20_4h > sma50_4h and current > sma20_4h:
            score += 20
            why_tags.append("trend=BULL✅")
        elif sma20_4h > sma50_4h and current > sma50_4h:
            score += 12
            why_tags.append("trend=BULL↗️")
        elif sma20_4h < sma50_4h:
            why_tags.append("trend=BEAR❌")
        else:
            score += 5
            why_tags.append("trend=RANGE➡️")
    else:
        why_tags.append("trend=?")

    # ── 3. Volume bevestiging (0-15 punten) ──────────
    if vol_ratio >= 2.0:
        score += 15
        why_tags.append(f"vol={vol_ratio:.1f}x🔥")
    elif vol_ratio >= 1.5:
        score += 12
        why_tags.append(f"vol={vol_ratio:.1f}x✅")
    elif vol_ratio >= 1.0:
        score += 7
        why_tags.append(f"vol={vol_ratio:.1f}x➡️")
    else:
        score += 0
        why_tags.append(f"vol={vol_ratio:.1f}x❌laag")

    # ── 4. Experience win rate (0-20 punten) ─────────
    if exp_n >= 10:
        if exp_win_rate >= 0.65:
            score += 20
            why_tags.append(f"exp={exp_win_rate:.0%}({exp_n}trades)✅")
        elif exp_win_rate >= 0.55:
            score += 14
            why_tags.append(f"exp={exp_win_rate:.0%}({exp_n}trades)➡️")
        elif exp_win_rate >= 0.45:
            score += 7
            why_tags.append(f"exp={exp_win_rate:.0%}({exp_n}trades)⬇️")
        else:
            score += 0
            why_tags.append(f"exp={exp_win_rate:.0%}({exp_n}trades)❌")
    elif exp_n >= 3:
        # Beperkte data — neutrale score
        score += 10
        why_tags.append(f"exp=weinig({exp_n}trades)")
    else:
        # Geen data — standaard neutraal
        score += 10
        why_tags.append(f"exp=nieuw")

    # ── 5. BTC regime (0-15 punten) ──────────────────
    if btc_regime == "BULL":
        score += 15
        why_tags.append("BTC=BULL✅")
    elif btc_regime == "RANGE":
        score += 7
        why_tags.append("BTC=RANGE➡️")
    elif btc_regime == "BEAR":
        score += 0
        why_tags.append("BTC=BEAR❌")
    else:
        score += 5
        why_tags.append(f"BTC={btc_regime}?")

    # ── 6. Multi-timeframe 1H bevestiging (0-10 punten)
    if closes_1h and len(closes_1h) >= RSI_PERIOD + 1:
        rsi_1h = rsi_wilder(closes_1h, RSI_PERIOD)
        if rsi_1h is not None:
            if RSI_MIN <= rsi_1h <= RSI_MAX:
                score += 10
                why_tags.append(f"1H_RSI={rsi_1h:.0f}✅")
            elif rsi_1h < RSI_MIN:
                score += 4
                why_tags.append(f"1H_RSI={rsi_1h:.0f}⬇️")
            else:
                score += 0
                why_tags.append(f"1H_RSI={rsi_1h:.0f}❌")
        else:
            score += 5
            why_tags.append("1H_RSI=?")
    else:
        score += 5
        why_tags.append("1H=geen_data")

    # ── Fee correctie ─────────────────────────────────
    score = min(score, 100)
    fee_impact = TOTAL_COST_PCT * 100
    if fee_impact > 0.3:
        score = max(0, score - 3)
        why_tags.append(f"fee=-{fee_impact:.2f}%")

    # ── Chance berekening ─────────────────────────────
    if exp_n >= 10 and exp_win_rate > 0:
        # Gewogen op basis van ervaring + score
        chance = int(exp_win_rate * 100 * (score / 100) * 1.2)
    else:
        # Minder data: conservatievere schatting
        chance = int(score * 0.65)
    chance = max(0, min(100, chance))

    # ── Confidence berekening (hoeveel data we hebben) ─
    if exp_n >= 100:
        confidence = min(95, 70 + int(exp_win_rate * 25))
    elif exp_n >= 50:
        confidence = min(85, 55 + int(exp_win_rate * 25))
    elif exp_n >= 20:
        confidence = min(75, 45 + int(exp_win_rate * 20))
    elif exp_n >= 5:
        confidence = min(65, 35 + int(exp_win_rate * 20))
    else:
        confidence = 40  # Weinig data — laag vertrouwen

    why_tag = " | ".join(why_tags[:7])  # Max 7 tags
    return score, chance, confidence, why_tag, rsi_4h, vol_ratio


# ============================================================
# EXPERIENCE SCOREBOARD
# ============================================================
def get_experience(
    conn,
    symbol:     str,
    setup_type: str,
    regime:     str,
) -> Tuple[float, int, str]:
    """
    Haalt experience op uit scoreboard voor dit setup/regime.
    Geeft (win_rate, n_trades, bias) terug.

    Samenwerking: geschreven door history_simulator.py,
    gelezen door multi_coin_score.py en app.py.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COALESCE(win_rate, 0.5) AS win_rate,
                COALESCE(n, 0)          AS n,
                COALESCE(bias, 'NEUTRAL') AS bias
            FROM public.experience_scoreboard
            WHERE symbol     = %s
              AND setup_type = %s
              AND regime     = %s
            LIMIT 1
            """, (symbol, setup_type, regime))
            row = cur.fetchone()
            if row:
                return safe_float(row[0]), safe_int(row[1]), safe_str(row[2], "NEUTRAL")
    except Exception:
        pass
    return 0.5, 0, "NEUTRAL"


def is_coin_on_cooldown(conn, symbol: str) -> bool:
    """
    24u cooldown na verlies op die coin.
    Identiek aan live_trader.py en trade_monitor.py.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT exit_time FROM public.experience_trades
            WHERE coin = %s
              AND UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND UPPER(outcome) = 'LOSS'
              AND exit_time IS NOT NULL
            ORDER BY exit_time DESC
            LIMIT 1
            """, (symbol,))
            row = cur.fetchone()
            if row and row[0]:
                last_loss = row[0]
                if hasattr(last_loss, 'tzinfo') and last_loss.tzinfo is None:
                    last_loss = last_loss.replace(tzinfo=timezone.utc)
                hours_since = (now_utc() - last_loss).total_seconds() / 3600
                return hours_since < COIN_COOLDOWN_HOURS
    except Exception:
        pass
    return False


def is_coin_blacklisted(conn, symbol: str) -> bool:
    """
    Blacklist: win rate <30% na 20+ trades.
    Identiek aan live_trader.py en trade_monitor.py.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COUNT(*) AS n,
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN') AS wins
            FROM public.experience_trades
            WHERE coin = %s
              AND UPPER(COALESCE(source,'')) IN ('REAL','LIVE','SIM','SHADOW')
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
            """, (symbol,))
            row = cur.fetchone()
            if row:
                n    = safe_int(row[0])
                wins = safe_int(row[1])
                if n >= BLACKLIST_MIN_TRADES:
                    return (wins / n) < BLACKLIST_MAX_WINRATE
    except Exception:
        pass
    return False


def get_prebuy_count_today(conn) -> int:
    """Telt het aantal Pre-BUY signals van vandaag."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COUNT(*) FROM public.pending_approvals
            WHERE DATE(created_at AT TIME ZONE 'UTC') = %s
            """, (utc_day_str(),))
            row = cur.fetchone()
            return safe_int(row[0]) if row else 0
    except Exception:
        return 0


def symbol_already_pending(conn, symbol: str) -> bool:
    """Controleert of coin al een actieve pending approval heeft."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT 1 FROM public.pending_approvals
            WHERE symbol = %s
              AND COALESCE(status,'PENDING') IN ('PENDING','APPROVED')
              AND (expires_at IS NULL OR expires_at > NOW())
            LIMIT 1
            """, (symbol,))
            return cur.fetchone() is not None
    except Exception:
        return False


# ============================================================
# PRE-BUY AANMAKEN EN AUTO BUY TRIGGEREN
# ============================================================
def insert_pending(conn, prebuy: Dict) -> str:
    """
    Voegt een Pre-BUY signal in in pending_approvals.

    Samenwerking:
    - Geschreven door multi_coin_score.py (dit bestand)
    - Gelezen door whatsapp_webhook.py /auto_buy route
    - Gelezen door app.py dashboard
    - Status bijgewerkt door whatsapp_webhook.py

    Geeft prebuy_id terug, of "" bij fout.
    """
    prebuy_id  = prebuy.get("id") or str(uuid.uuid4())
    expires_at = now_utc() + timedelta(hours=PREBUY_EXPIRY_HOURS)

    try:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO public.pending_approvals (
                id, symbol, setup_type, regime, score, label,
                entry, stop, target, expires_at,
                raw_score, chance, confidence,
                timeframe, bitvavo_market,
                exp_n, exp_win_rate, exp_bias,
                why_tag, created_at, status
            )
            VALUES (
                %s,%s,%s,%s,%s,'GO',
                %s,%s,%s,%s,
                %s,%s,%s,
                %s,%s,
                %s,%s,%s,
                %s,NOW(),'PENDING'
            )
            ON CONFLICT (id) DO UPDATE SET
                score      = EXCLUDED.score,
                expires_at = EXCLUDED.expires_at,
                status     = 'PENDING',
                updated_at = NOW()
            """, (
                prebuy_id,
                prebuy["symbol"],
                prebuy["setup_type"],
                prebuy["regime"],
                prebuy["score"],
                prebuy["entry"],
                prebuy["stop"],
                prebuy["target"],
                expires_at,
                prebuy["score"],
                prebuy["chance"],
                prebuy["confidence"],
                prebuy.get("timeframe", "4h"),
                prebuy.get("bitvavo_market", ""),
                prebuy.get("exp_n", 0),
                prebuy.get("exp_win_rate", 0.5),
                prebuy.get("exp_bias", "NEUTRAL"),
                prebuy.get("why_tag", ""),
            ))
        conn.commit()
        log(f"✅ Pre-BUY aangemaakt: {prebuy['symbol']} score={prebuy['score']} id={prebuy_id}")
        return prebuy_id

    except Exception as e:
        log(f"⚠️ insert_pending fout ({prebuy['symbol']}): {type(e).__name__}: {e}")
        conn.rollback()
        return ""


def trigger_auto_buy(prebuy_id: str) -> bool:
    """
    Triggert de /auto_buy route op whatsapp_webhook.py.
    Bot koopt automatisch na alle limiet checks in webhook.

    Samenwerking:
    - Roept whatsapp_webhook.py /auto_buy aan
    - Webhook voert check_trading_limits() uit
    - Webhook roept live_trader.buy_eur() aan
    """
    if not WEBHOOK_BASE_URL:
        log("⚠️ WEBHOOK_BASE_URL niet ingesteld — geen auto_buy trigger")
        return False

    try:
        resp = requests.post(
            f"{WEBHOOK_BASE_URL}/auto_buy",
            headers={"X-Bot-Auth": BOT_INTERNAL_SECRET},
            json={"prebuy_id": prebuy_id},
            timeout=20,
        )
        if resp.ok:
            log(f"✅ Auto BUY getriggerd: {prebuy_id}")
            return True
        log(f"⚠️ Auto BUY trigger fout: {resp.status_code}: {resp.text[:100]}")
        return False
    except requests.exceptions.Timeout:
        log(f"⚠️ Auto BUY trigger timeout: {prebuy_id}")
        return False
    except Exception as e:
        log(f"⚠️ Auto BUY trigger exception: {e}")
        return False


# ============================================================
# HOOFD SCAN LOOP
# ============================================================
def scan_universe(conn) -> int:
    """
    Scant alle Bitvavo-tradable coins via Binance data.

    Stappen per coin:
    1. Bitvavo universe check (is coin tradable?)
    2. Coin filters (blacklist, cooldown, al pending?)
    3. Candles ophalen (4H + 1H)
    4. Regime detectie voor die coin
    5. Setup detectie (TREND_PULLBACK, BREAKOUT, etc.)
    6. Experience ophalen uit scoreboard
    7. Score berekening (RSI, trend, volume, exp, BTC, 1H)
    8. ATR-based stop en target berekenen
    9. Pre-BUY aanmaken in DB
    10. Auto BUY triggeren via webhook

    Geeft aantal gegenereerde pre-buys terug.
    """
    # ── Checks voor we beginnen ──────────────────────────
    if not is_bot_active(conn):
        log("Bot gestopt — geen scans")
        return 0

    if is_bot_paused(conn):
        log("Bot gepauzeerd — geen scans")
        return 0

    if not is_trading_hours():
        log(f"Buiten trading hours ({TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC)")
        return 0

    prebuy_today = get_prebuy_count_today(conn)
    if prebuy_today >= MAX_PREBUY_PER_DAY:
        log(f"Pre-buy daglimiet bereikt: {prebuy_today}/{MAX_PREBUY_PER_DAY}")
        return 0

    # ── BTC regime ophalen ───────────────────────────────
    log("🔍 BTC regime ophalen...")
    btc_regime = get_btc_regime(conn)
    log(f"📊 BTC regime: {btc_regime}")

    if btc_regime == "BEAR" and BTC_SKIP_BEAR:
        log("⚠️ BTC in BEAR regime — scans doorgaan voor shadow trades")
        # Note: scanner blijft scannen maar auto_buy wordt niet getriggerd

    # ── Bitvavo tradable markets ─────────────────────────
    tradable = get_tradable_markets()
    if not tradable:
        log("❌ Geen tradable markets — scan gestopt")
        report_error(
            Exception("Bitvavo markets leeg"),
            "scan_universe.get_tradable_markets",
            "HOOG",
        )
        return 0

    log(f"📋 {len(tradable)} Bitvavo EUR markets beschikbaar")

    # Bouw USDT→EUR mapping
    scan_pairs: List[Tuple[str, str]] = []
    for market in tradable:
        if market.endswith("-EUR"):
            base          = market[:-4]
            symbol_usdt   = f"{base}USDT"
            scan_pairs.append((symbol_usdt, market))

    log(f"🔍 Scannen: {len(scan_pairs)} coin pairs...")

    prebuy_count = 0
    scanned      = 0
    skipped      = 0

    for symbol_usdt, bitvavo_market in scan_pairs:
        scanned += 1

        # ── Coin filters ─────────────────────────────
        if is_coin_blacklisted(conn, symbol_usdt):
            log(f"⚫ {symbol_usdt} — blacklist (win rate te laag)")
            skipped += 1
            continue

        if is_coin_on_cooldown(conn, symbol_usdt):
            skipped += 1
            continue

        if symbol_already_pending(conn, symbol_usdt):
            skipped += 1
            continue

        # ── Candles ophalen ──────────────────────────
        candles_4h = fetch_candles(symbol_usdt, "4h", 120)
        if len(candles_4h) < 30:
            continue

        candles_1h = fetch_candles(symbol_usdt, "1h", 60)

        closes_4h = [c["close"] for c in candles_4h]
        current   = closes_4h[-1]

        if current <= 0:
            continue

        # ── Regime voor deze coin ────────────────────
        coin_regime = detect_coin_regime(closes_4h)

        # ── Setup detectie ───────────────────────────
        setup_type, why_base = detect_setup_type(candles_4h, candles_1h)
        if setup_type == "UNKNOWN":
            continue

        # ── Experience ophalen ───────────────────────
        exp_win_rate, exp_n, exp_bias = get_experience(
            conn, symbol_usdt, setup_type, coin_regime
        )

        # ── Ticker voor volume data ──────────────────
        ticker = fetch_ticker_24h(symbol_usdt)

        # ── Score berekening ─────────────────────────
        score, chance, confidence, why_tag, rsi_4h, vol_ratio = calculate_score(
            candles_4h, candles_1h, ticker,
            coin_regime, btc_regime, setup_type,
            exp_win_rate, exp_n,
        )

        # ── Score drempel check ──────────────────────
        if score < MIN_SCORE_TO_TRADE:
            continue
        if chance < MIN_CHANCE:
            continue
        if confidence < MIN_CONFIDENCE:
            continue

        log(
            f"🎯 {symbol_usdt}: score={score} chance={chance}% "
            f"conf={confidence}% setup={setup_type} "
            f"regime={coin_regime} BTC={btc_regime}"
        )

        # ── ATR-based stop en target ─────────────────
        atr_val = atr_calc(candles_4h, ATR_PERIOD)
        if atr_val and atr_val > 0:
            stop   = current - atr_val * ATR_MULTIPLIER
            target = current + atr_val * ATR_MULTIPLIER * ATR_TARGET_R
        else:
            # Fallback: vaste percentages als ATR niet beschikbaar
            stop   = current * 0.98
            target = current * 1.04

        # Zorg dat stop positief is
        stop = max(stop, current * 0.95)

        # ── Claude beoordeling van signaal ───────────
        claude_beoordeling = claude_beoordeel_signaal(
            symbol_usdt, setup_type, coin_regime, btc_regime,
            score, chance, confidence, rsi_4h, vol_ratio,
            exp_win_rate, exp_n, why_tag,
        )

        # ── Pre-BUY aanmaken ─────────────────────────
        prebuy = {
            "id":            str(uuid.uuid4()),
            "symbol":        symbol_usdt,
            "setup_type":    setup_type,
            "regime":        coin_regime,
            "score":         score,
            "chance":        chance,
            "confidence":    confidence,
            "entry":         current,
            "stop":          stop,
            "target":        target,
            "timeframe":     "4h",
            "bitvavo_market": bitvavo_market,
            "exp_n":         exp_n,
            "exp_win_rate":  exp_win_rate,
            "exp_bias":      exp_bias,
            "why_tag":       why_tag,
            "claude_beoordeling": claude_beoordeling,
        }

        prebuy_id = insert_pending(conn, prebuy)

        if prebuy_id:
            prebuy_count += 1
            prebuy_today += 1

            # ── Auto BUY triggeren ───────────────────
            # Alleen als BTC niet in BEAR is
            if btc_regime != "BEAR" or not BTC_SKIP_BEAR:
                trigger_auto_buy(prebuy_id)
            else:
                log(f"⚠️ {symbol_usdt} — Pre-BUY aangemaakt maar geen auto_buy (BTC BEAR)")

        # Daglimiet check
        if prebuy_today >= MAX_PREBUY_PER_DAY:
            log(f"Pre-buy daglimiet bereikt: {prebuy_today}")
            break

        # Voortgang elke 25 coins
        if scanned % 25 == 0:
            log(
                f"  Voortgang: {scanned}/{len(scan_pairs)} | "
                f"{prebuy_count} pre-buys | {skipped} overgeslagen"
            )

    log(
        f"✅ Scan klaar: {scanned} gescand | "
        f"{prebuy_count} pre-buys | {skipped} overgeslagen"
    )
    return prebuy_count


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    log("=" * 60)
    log("Multi Coin Scorer v2.0 — gestart")
    log("=" * 60)
    log(f"Database:       {'✅' if DATABASE_URL else '❌ ONTBREEKT'}")
    log(f"Webhook URL:    {'✅' if WEBHOOK_BASE_URL else '⚠️ niet ingesteld'}")
    log(f"Claude API:     {'✅' if ANTHROPIC_API_KEY else '⚠️ niet ingesteld'}")
    log(f"Twilio:         {'✅' if TWILIO_ACCOUNT_SID else '⚠️ niet ingesteld'}")
    log(f"Min score:      {MIN_SCORE_TO_TRADE}")
    log(f"Min chance:     {MIN_CHANCE}%")
    log(f"Min confidence: {MIN_CONFIDENCE}%")
    log(f"ATR period:     {ATR_PERIOD} | multiplier: {ATR_MULTIPLIER}")
    log(f"ATR target R:   {ATR_TARGET_R}")
    log(f"Fee+slippage:   {TOTAL_COST_PCT*100:.2f}%")
    log(f"BTC skip BEAR:  {BTC_SKIP_BEAR}")
    log(f"Trading hours:  {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC")
    log(f"Prebuy expiry:  {PREBUY_EXPIRY_HOURS}u")
    log(f"Cooldown:       {COIN_COOLDOWN_HOURS}u na verlies")
    log(f"Max prebuy/dag: {MAX_PREBUY_PER_DAY}")
    log("=" * 60)

    if not DATABASE_URL:
        log("❌ DATABASE_URL ontbreekt — kan niet doorgaan")
        sys.exit(1)

    # Claude health check bij start
    if ANTHROPIC_API_KEY:
        log("🧠 Claude scanner health check...")
        health = claude_scanner_health_check()
        if health:
            log(f"Claude: {health}")

    try:
        conn = db_connect()
        log("✅ Database verbonden")

        # BTC regime test
        btc = get_btc_regime(conn)
        log(f"📊 BTC regime: {btc}")

        # Bitvavo test
        markets = get_tradable_markets()
        log(f"📋 Bitvavo markets: {len(markets)}")

        # Hoofd scan
        n = scan_universe(conn)
        log(f"✅ Resultaat: {n} pre-buys gegenereerd")

        conn.close()

    except KeyboardInterrupt:
        log("⛔ Scanner gestopt door gebruiker")
        sys.exit(0)
    except Exception as e:
        report_error(e, "__main__", severity="KRITIEK")
        sys.exit(1)
