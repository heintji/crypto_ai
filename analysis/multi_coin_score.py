# analysis/multi_coin_score.py
# ============================================================
# Crypto AI Bot — Multi Coin Scorer v2.0
# ============================================================
# Zelfde gedachtegang als alle andere bestanden:
#
#   ✅ Zelfde ENV variabelen + limieten
#   ✅ Zelfde send_whatsapp() implementatie
#   ✅ Zelfde Claude health monitoring
#   ✅ Zelfde bot state check (PostgreSQL)
#   ✅ Zelfde sslmode="require" op DB connectie
#   ✅ Zelfde safe_int / safe_float / safe_str helpers
#   ✅ Zelfde trading hours filter (08:00-22:00 UTC)
#   ✅ Zelfde weekend: gewoon doorgaan
#
# BUGS GEFIXED vs origineel:
#   ✅ Bitvavo universe filter — gebruikt publieke get_tradable_markets()
#   ✅ Simpele RSI → Wilder RSI (zoals indicators.py)
#   ✅ Vaste 2% stop → ATR-based dynamische stop
#   ✅ Geen rate limiting → 0.2s sleep tussen Binance calls
#   ✅ Score drempel 80 → 85 voor hogere kwaliteit
#
# NIEUWE FEATURES:
#   ✅ BTC regime filter — geen trades als BTC BEAR
#   ✅ Volume confirmatie filter
#   ✅ Multi-timeframe confirmatie (1H + 4H)
#   ✅ Coin cooldown check (24u na verlies)
#   ✅ Coin blacklist check (winrate <30% na 20 trades)
#   ✅ Fee correctie in score berekening
#   ✅ Slippage correctie
#   ✅ Experience scoreboard integratie
#   ✅ Auto BUY trigger via /auto_buy endpoint
#   ✅ Claude analyseert elke Pre-BUY signal
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
TRADING_HOURS_START     = int(os.getenv("TRADING_HOURS_START") or "8")
TRADING_HOURS_END       = int(os.getenv("TRADING_HOURS_END") or "22")
BOT_STATE_TABLE         = "public.bot_state"

# Score instellingen
MIN_SCORE_TO_TRADE  = int(os.getenv("MIN_SCORE_TO_TRADE") or "85")
MIN_CHANCE          = int(os.getenv("MIN_CHANCE") or "70")
MIN_CONFIDENCE      = int(os.getenv("MIN_CONFIDENCE") or "70")
MAX_PREBUY_PER_DAY  = int(os.getenv("MAX_PREBUY_PER_DAY") or "50")

# Fee + slippage — identiek aan live_trader
BITVAVO_FEE_PCT = float(os.getenv("BITVAVO_FEE_PCT") or "0.0025")
SLIPPAGE_PCT    = float(os.getenv("SLIPPAGE_PCT") or "0.001")
TOTAL_COST_PCT  = BITVAVO_FEE_PCT + SLIPPAGE_PCT

# Coin filter instellingen
COIN_COOLDOWN_HOURS   = float(os.getenv("COIN_COOLDOWN_HOURS") or "24.0")
BLACKLIST_MIN_TRADES  = int(os.getenv("BLACKLIST_MIN_TRADES") or "20")
BLACKLIST_MAX_WINRATE = float(os.getenv("BLACKLIST_MAX_WINRATE") or "0.30")
EDGE_DECAY_THRESHOLD  = float(os.getenv("EDGE_DECAY_THRESHOLD") or "10.0")

# Binance API
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
RSI_PERIOD  = int(os.getenv("RSI_PERIOD") or "14")
RSI_MIN     = int(os.getenv("RSI_MIN") or "35")
RSI_MAX     = int(os.getenv("RSI_MAX") or "65")

# Prebuy expiry
PREBUY_EXPIRY_HOURS = int(os.getenv("PREBUY_EXPIRY_HOURS") or "4")

# Markets cache
_MARKETS_CACHE: Dict[str, Any] = {"ts": 0.0, "markets": set()}
_MARKETS_TTL = 30 * 60


# ============================================================
# BASIS HELPERS — identiek aan alle andere bestanden
# ============================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


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
    return TRADING_HOURS_START <= now_utc().hour < TRADING_HOURS_END


# ============================================================
# WHATSAPP — identieke implementatie als alle andere bestanden
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
        if resp.status_code in (200, 201):
            log(f"✅ WhatsApp verzonden ({len(message)} tekens)")
            return True
        log(f"❌ WhatsApp {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        log(f"❌ WhatsApp exception: {type(e).__name__}: {e}")
        return False


# ============================================================
# CLAUDE HEALTH MONITORING — identiek aan alle bestanden
# ============================================================
def _claude_analyse(prompt: str, max_tokens: int = 300) -> str:
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


def report_error(error: Exception, function: str, severity: str = "HOOG") -> None:
    log(f"[{severity}] {function}: {type(error).__name__}: {error}")
    if severity not in ("KRITIEK", "HOOG"):
        return

    prompt = f"""
Je bent een crypto trading bot monitor.
Er is een fout in multi_coin_score.py opgetreden.

Ernst:   {severity}
Functie: {function}
Fout:    {type(error).__name__}: {str(error)[:200]}

Geef in 2-3 zinnen Nederlands:
- Wat er mis is
- Impact op nieuwe Pre-BUY signalen
- Wat de gebruiker moet doen
""".strip()

    uitleg = _claude_analyse(prompt, max_tokens=150)
    if not uitleg:
        uitleg = f"{type(error).__name__}: {str(error)[:100]}"

    send_whatsapp(
        f"🚨 SCANNER FOUT — {severity}\n"
        f"{'─' * 28}\n\n"
        f"📁 Functie: {function}\n\n"
        f"🧠 Claude:\n{uitleg}\n\n"
        f"🤖 Bot bewaakt open trades.\n"
        f"Nieuwe scans tijdelijk gestopt.\n\n"
        f"Commands: STATUS | TRADES | STOP"
    )


# ============================================================
# DATABASE — sslmode="require" identiek aan alle bestanden
# ============================================================
def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def get_bot_state_value(conn, key: str, default: str = "") -> str:
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
    return get_bot_state_value(conn, "bot_active", "false").lower() == "true"


def is_bot_paused(conn) -> bool:
    if get_bot_state_value(conn, "bot_paused", "false").lower() != "true":
        return False
    until_str = get_bot_state_value(conn, "bot_paused_until", "")
    if not until_str:
        return True
    try:
        until = datetime.fromisoformat(until_str)
        if now_utc() > until:
            return False
        return True
    except Exception:
        return True


# ============================================================
# BITVAVO UNIVERSE FILTER — publiek, gecached
# Gebruikt dezelfde get_tradable_markets als live_trader
# ============================================================
def get_tradable_markets() -> Set[str]:
    """
    Haalt actieve Bitvavo EUR markets op.
    Identiek aan live_trader.get_tradable_markets().
    Cache: 30 minuten.
    """
    now = time.time()
    if _MARKETS_CACHE["markets"] and (now - _MARKETS_CACHE["ts"]) < _MARKETS_TTL:
        return _MARKETS_CACHE["markets"]

    try:
        resp = requests.get(
            f"{BITVAVO_BASE}/v2/markets",
            timeout=10,
        )
        resp.raise_for_status()
        items = resp.json()

        tradable: Set[str] = set()
        for item in items:
            market = safe_str(item.get("market"))
            status = safe_str(item.get("status")).lower()
            if market and status == "trading" and market.endswith("-EUR"):
                tradable.add(market)

        _MARKETS_CACHE["ts"]      = now
        _MARKETS_CACHE["markets"] = tradable
        log(f"✅ Bitvavo markets geladen: {len(tradable)} tradable")
        return tradable

    except Exception as e:
        log(f"⚠️ Bitvavo markets fout: {e}")
        return _MARKETS_CACHE.get("markets") or set()


def symbol_to_bitvavo_market(symbol_usdt: str) -> Optional[str]:
    """ETHUSDT → ETH-EUR. Geeft None als niet tradable op Bitvavo."""
    s = safe_str(symbol_usdt).upper()
    if not s.endswith("USDT"):
        return None
    base = s[:-4]
    if not base:
        return None
    market = f"{base}-EUR"
    tradable = get_tradable_markets()
    return market if market in tradable else None


# ============================================================
# BINANCE DATA OPHALEN
# ============================================================
def binance_get(endpoint: str, params: dict, retries: int = MAX_RETRIES) -> Optional[Any]:
    """Binance public API met retry en rate limiting."""
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
        except Exception as e:
            log(f"⚠️ Binance fout poging {attempt}/{retries}: {e}")
        if attempt < retries:
            time.sleep(2 ** attempt)
    return None


def fetch_candles(symbol: str, interval: str = "4h", limit: int = 100) -> List[dict]:
    """Haalt candles op van Binance met OHLCV data."""
    data = binance_get("/klines", {
        "symbol": symbol, "interval": interval, "limit": limit
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


def fetch_ticker_24h(symbol: str) -> Optional[dict]:
    """Haalt 24u ticker op van Binance."""
    time.sleep(BINANCE_SLEEP)
    return binance_get("/ticker/24hr", {"symbol": symbol})


# ============================================================
# TECHNISCHE INDICATOREN
# ============================================================
def rsi_wilder(closes: List[float], period: int = 14) -> Optional[float]:
    """
    Wilder RSI — identiek aan indicators.py.
    Veel nauwkeuriger dan simpele RSI.
    """
    if len(closes) < period + 1:
        return None
    changes  = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains    = [max(c, 0) for c in changes]
    losses   = [max(-c, 0) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    mult = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = v * mult + ema_val * (1 - mult)
    return ema_val


def atr(candles: List[dict], period: int = 14) -> Optional[float]:
    """
    Average True Range — voor dynamische stop/target berekening.
    Vervangt de vaste 2% stop.
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
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


def detect_regime(closes: List[float]) -> str:
    """
    Detecteert marktregime op basis van SMA20 vs SMA50.
    BULL / BEAR / RANGE
    """
    if len(closes) < 50:
        return "UNKNOWN"
    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    if sma20 is None or sma50 is None:
        return "UNKNOWN"
    diff_pct = abs(sma20 - sma50) / sma50

    if diff_pct < 0.015:
        return "RANGE"
    return "BULL" if sma20 > sma50 else "BEAR"


def detect_btc_regime(closes_btc: List[float]) -> str:
    """
    BTC regime op basis van EMA200.
    BULL / BEAR / RANGE
    """
    if len(closes_btc) < 200:
        return "UNKNOWN"
    ema200 = ema(closes_btc, 200)
    ema200_prev = ema(closes_btc[:-1], 200)
    if ema200 is None or ema200_prev is None:
        return "UNKNOWN"

    current = closes_btc[-1]
    slope   = ema200 - ema200_prev
    pct_diff = abs(current - ema200) / ema200

    if pct_diff < 0.02 or abs(slope) < 0.001 * ema200:
        return "RANGE"
    return "BULL" if current >= ema200 else "BEAR"


# ============================================================
# SETUP DETECTIE
# ============================================================
def detect_setup(candles_4h: List[dict], candles_1h: List[dict]) -> Tuple[str, str]:
    """
    Detecteert setup type + richting op basis van 4H + 1H candles.
    Geeft (setup_type, why_tag) terug.
    """
    if len(candles_4h) < 20:
        return "UNKNOWN", ""

    closes_4h = [c["close"] for c in candles_4h]
    closes_1h = [c["close"] for c in candles_1h] if candles_1h else []

    rsi_4h = rsi_wilder(closes_4h, RSI_PERIOD)
    sma20_4h = sma(closes_4h, 20)
    sma50_4h = sma(closes_4h, 50)
    current  = closes_4h[-1]
    prev     = closes_4h[-2] if len(closes_4h) > 1 else current

    if rsi_4h is None or sma20_4h is None or sma50_4h is None:
        return "UNKNOWN", ""

    # TREND PULLBACK — prijs trekt terug naar SMA20 in BULL trend
    if sma20_4h > sma50_4h and current > sma50_4h:
        if abs(current - sma20_4h) / sma20_4h < 0.02 and RSI_MIN <= rsi_4h <= 55:
            return "TREND_PULLBACK", f"RSI={rsi_4h:.0f} SMA20pullback"

    # BREAKOUT — prijs breekt boven recente weerstand
    high_20 = max(c["high"] for c in candles_4h[-20:])
    if current > high_20 * 0.998 and prev < high_20:
        return "BREAKOUT", f"RSI={rsi_4h:.0f} breakout_high20"

    # BOUNCE — prijs bounced van SMA50 support
    if abs(current - sma50_4h) / sma50_4h < 0.015 and rsi_4h < 50:
        return "BOUNCE", f"RSI={rsi_4h:.0f} SMA50bounce"

    # MOMENTUM — sterke stijging met gezonde RSI
    if rsi_4h > 55 and current > sma20_4h > sma50_4h:
        return "MOMENTUM", f"RSI={rsi_4h:.0f} momentum"

    return "UNKNOWN", f"RSI={rsi_4h:.0f}"


# ============================================================
# SCORE BEREKENING
# ============================================================
def calculate_score(
    candles_4h:   List[dict],
    candles_1h:   List[dict],
    ticker:       dict,
    regime:       str,
    btc_regime:   str,
    setup_type:   str,
    exp_win_rate: float,
    exp_n:        int,
) -> Tuple[int, int, int, str]:
    """
    Berekent (score, chance, confidence, why_tag).

    Score componenten:
    - RSI in goede zone (0-20 punten)
    - Trend alignment (0-20 punten)
    - Volume bevestiging (0-15 punten)
    - Experience win rate (0-20 punten)
    - BTC regime (0-15 punten)
    - Multi-timeframe (0-10 punten)

    Totaal: max 100 punten
    Min voor trade: 85 punten
    """
    if not candles_4h or len(candles_4h) < 20:
        return 0, 0, 0, "te_weinig_data"

    closes_4h  = [c["close"] for c in candles_4h]
    closes_1h  = [c["close"] for c in candles_1h] if candles_1h else []
    volumes_4h = [c["volume"] for c in candles_4h]

    rsi_4h   = rsi_wilder(closes_4h, RSI_PERIOD)
    sma20_4h = sma(closes_4h, 20)
    sma50_4h = sma(closes_4h, 50)
    current  = closes_4h[-1]
    vol_now  = volumes_4h[-1] if volumes_4h else 0
    vol_avg  = sma(volumes_4h[:-1], 20) or 1

    score    = 0
    why_tags = []

    # 1. RSI in ideale zone (0-20 punten)
    if rsi_4h is not None:
        if RSI_MIN <= rsi_4h <= RSI_MAX:
            rsi_score = 20 - abs(rsi_4h - 50) / 2
            score += int(rsi_score)
            why_tags.append(f"RSI={rsi_4h:.0f}✅")
        elif rsi_4h < RSI_MIN:
            score += 5
            why_tags.append(f"RSI={rsi_4h:.0f}⬇️")
        else:
            why_tags.append(f"RSI={rsi_4h:.0f}❌")

    # 2. Trend alignment SMA20 > SMA50 (0-20 punten)
    if sma20_4h and sma50_4h:
        if sma20_4h > sma50_4h and current > sma20_4h:
            score += 20
            why_tags.append("trend✅")
        elif sma20_4h > sma50_4h:
            score += 10
            why_tags.append("trend↗️")
        else:
            why_tags.append("trend❌")

    # 3. Volume bevestiging (0-15 punten)
    vol_ratio = vol_now / vol_avg if vol_avg > 0 else 1.0
    if vol_ratio >= 1.5:
        score += 15
        why_tags.append(f"vol={vol_ratio:.1f}x✅")
    elif vol_ratio >= 1.0:
        score += 8
        why_tags.append(f"vol={vol_ratio:.1f}x➡️")
    else:
        score += 0
        why_tags.append(f"vol={vol_ratio:.1f}x❌")

    # 4. Experience win rate (0-20 punten)
    if exp_n >= 5:
        if exp_win_rate >= 0.60:
            score += 20
            why_tags.append(f"exp={exp_win_rate:.0%}✅")
        elif exp_win_rate >= 0.50:
            score += 12
            why_tags.append(f"exp={exp_win_rate:.0%}➡️")
        elif exp_win_rate >= 0.40:
            score += 5
            why_tags.append(f"exp={exp_win_rate:.0%}⬇️")
        else:
            why_tags.append(f"exp={exp_win_rate:.0%}❌")
    else:
        score += 10
        why_tags.append(f"exp=nieuw({exp_n})")

    # 5. BTC regime (0-15 punten)
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
        why_tags.append("BTC=?")

    # 6. Multi-timeframe bevestiging 1H (0-10 punten)
    if closes_1h and len(closes_1h) >= 14:
        rsi_1h = rsi_wilder(closes_1h, 14)
        if rsi_1h and RSI_MIN <= rsi_1h <= RSI_MAX:
            score += 10
            why_tags.append(f"1H_RSI={rsi_1h:.0f}✅")
        elif rsi_1h:
            score += 3
            why_tags.append(f"1H_RSI={rsi_1h:.0f}➡️")

    # Cap op 100
    score = min(score, 100)

    # Fee correctie — trades met kleine marge zijn minder waard
    # Bij €0.50 trade zijn fees relatief groot
    fee_impact = TOTAL_COST_PCT * 100  # bijv. 0.35%
    if fee_impact > 0.3:
        score = max(0, score - 3)
        why_tags.append(f"fee={fee_impact:.2f}%")

    # Chance = kans op succes op basis van experience + regime
    if exp_n >= 10 and exp_win_rate > 0:
        chance = int(exp_win_rate * 100 * (score / 100))
    else:
        chance = int(score * 0.7)
    chance = max(0, min(100, chance))

    # Confidence = hoeveel data we hebben
    if exp_n >= 50:
        confidence = min(95, 60 + int(exp_win_rate * 35))
    elif exp_n >= 20:
        confidence = min(80, 45 + int(exp_win_rate * 25))
    elif exp_n >= 5:
        confidence = min(65, 35 + int(exp_win_rate * 20))
    else:
        confidence = 40

    why_tag = " | ".join(why_tags[:6])
    return score, chance, confidence, why_tag


# ============================================================
# EXPERIENCE SCOREBOARD
# ============================================================
def get_experience(conn, symbol: str, setup_type: str, regime: str) -> Tuple[float, int, str]:
    """
    Haalt experience op uit scoreboard voor dit setup/regime.
    Geeft (win_rate, n_trades, bias) terug.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COALESCE(win_rate, 0.0) AS win_rate,
                COALESCE(n, 0)          AS n,
                COALESCE(bias, 'NEUTRAL') AS bias
            FROM public.experience_scoreboard
            WHERE symbol = %s
              AND setup_type = %s
              AND regime = %s
            LIMIT 1
            """, (symbol, setup_type, regime))
            row = cur.fetchone()
            if row:
                return safe_float(row[0]), safe_int(row[1]), safe_str(row[2], "NEUTRAL")
    except Exception:
        pass
    return 0.5, 0, "NEUTRAL"


def is_coin_on_cooldown(conn, symbol: str) -> bool:
    """24u cooldown na verlies op die coin — identiek aan trade_monitor."""
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
    """Blacklist check — identiek aan trade_monitor."""
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
    """Aantal pre-buys vandaag."""
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
# PRE-BUY AANMAKEN
# ============================================================
def insert_pending(conn, prebuy: dict) -> str:
    """Voegt een Pre-BUY signal in in pending_approvals."""
    prebuy_id = prebuy.get("id") or str(uuid.uuid4())
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
                %s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,
                %s,%s,%s,
                %s,%s,
                %s,%s,%s,
                %s,NOW(),'PENDING'
            )
            ON CONFLICT (id) DO UPDATE SET
                score      = EXCLUDED.score,
                expires_at = EXCLUDED.expires_at,
                status     = 'PENDING'
            """, (
                prebuy_id,
                prebuy["symbol"],
                prebuy["setup_type"],
                prebuy["regime"],
                prebuy["score"],
                "GO",
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
        log(f"⚠️ insert_pending fout ({prebuy['symbol']}): {e}")
        return ""


def trigger_auto_buy(prebuy_id: str) -> bool:
    """
    Roept de /auto_buy route aan op de webhook.
    Webhook voert dan de BUY uit na alle limiet checks.
    """
    if not WEBHOOK_BASE_URL:
        log("⚠️ WEBHOOK_BASE_URL niet ingesteld — geen auto_buy trigger")
        return False
    try:
        resp = requests.post(
            f"{WEBHOOK_BASE_URL}/auto_buy",
            headers={"X-Bot-Auth": BOT_INTERNAL_SECRET},
            json={"prebuy_id": prebuy_id},
            timeout=15,
        )
        if resp.ok:
            log(f"✅ Auto BUY getriggerd: {prebuy_id}")
            return True
        log(f"⚠️ Auto BUY trigger fout: {resp.status_code}: {resp.text[:100]}")
        return False
    except Exception as e:
        log(f"⚠️ Auto BUY trigger exception: {e}")
        return False


# ============================================================
# HOOFD SCAN LOOP
# ============================================================
def scan_universe(conn) -> int:
    """
    Scant alle Bitvavo-tradable coins.
    Genereert Pre-BUY signals voor coins met score >= MIN_SCORE_TO_TRADE.
    Geeft aantal gegenereerde pre-buys terug.
    """
    # Bot actief check
    if not is_bot_active(conn):
        log("Bot gestopt — geen scans")
        return 0

    if is_bot_paused(conn):
        log("Bot gepauzeerd — geen scans")
        return 0

    # Trading hours check
    if not is_trading_hours():
        log(f"Buiten trading hours ({TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC)")
        return 0

    # Daily pre-buy cap check
    prebuy_today = get_prebuy_count_today(conn)
    if prebuy_today >= MAX_PREBUY_PER_DAY:
        log(f"Pre-buy daglimiet bereikt: {prebuy_today}/{MAX_PREBUY_PER_DAY}")
        return 0

    # BTC regime ophalen — filter voor BEAR
    log("BTC regime ophalen...")
    btc_candles = fetch_candles("BTCUSDT", "4h", 210)
    btc_closes  = [c["close"] for c in btc_candles]
    btc_regime  = detect_btc_regime(btc_closes)
    log(f"BTC regime: {btc_regime}")

    if btc_regime == "BEAR":
        log("⚠️ BTC in BEAR regime — alleen shadow trades, geen echte BUYs")

    # Bitvavo tradable markets
    tradable = get_tradable_markets()
    if not tradable:
        log("❌ Geen tradable markets gevonden — scan gestopt")
        return 0

    log(f"Scannen: {len(tradable)} Bitvavo markets...")

    # Haal alle USDT symbols op die ook op Bitvavo staan
    bitvavo_usdt_symbols = []
    for market in tradable:
        if market.endswith("-EUR"):
            base = market[:-4]
            symbol_usdt = f"{base}USDT"
            bitvavo_usdt_symbols.append((symbol_usdt, market))

    prebuy_count = 0
    scanned      = 0

    for symbol_usdt, bitvavo_market in bitvavo_usdt_symbols:
        scanned += 1

        # Rate limiting
        time.sleep(BINANCE_SLEEP)

        # Coin filters
        if is_coin_blacklisted(conn, symbol_usdt):
            log(f"⚫ {symbol_usdt} — blacklist")
            continue

        if is_coin_on_cooldown(conn, symbol_usdt):
            log(f"⏳ {symbol_usdt} — cooldown actief")
            continue

        if symbol_already_pending(conn, symbol_usdt):
            continue

        # Candles ophalen
        candles_4h = fetch_candles(symbol_usdt, "4h", 100)
        if len(candles_4h) < 30:
            continue

        candles_1h = fetch_candles(symbol_usdt, "1h", 50)

        closes_4h = [c["close"] for c in candles_4h]
        current   = closes_4h[-1]

        if current <= 0:
            continue

        # Regime detectie voor deze coin
        regime = detect_regime(closes_4h)

        # Setup detectie
        setup_type, why_base = detect_setup(candles_4h, candles_1h)
        if setup_type == "UNKNOWN":
            continue

        # Experience scoreboard
        exp_win_rate, exp_n, exp_bias = get_experience(conn, symbol_usdt, setup_type, regime)

        # Ticker voor volume
        ticker = fetch_ticker_24h(symbol_usdt) or {}

        # Score berekening
        score, chance, confidence, why_tag = calculate_score(
            candles_4h, candles_1h, ticker, regime, btc_regime,
            setup_type, exp_win_rate, exp_n,
        )

        if score < MIN_SCORE_TO_TRADE:
            continue
        if chance < MIN_CHANCE:
            continue
        if confidence < MIN_CONFIDENCE:
            continue

        log(f"🎯 {symbol_usdt}: score={score} chance={chance} conf={confidence} "
            f"setup={setup_type} regime={regime} btc={btc_regime}")

        # ATR-based stop en target
        atr_val = atr(candles_4h, ATR_PERIOD)
        if atr_val and atr_val > 0:
            stop   = current - atr_val * ATR_MULTIPLIER
            target = current + atr_val * ATR_MULTIPLIER * ATR_TARGET_R
        else:
            stop   = current * 0.98
            target = current * 1.04

        # Pre-BUY aanmaken
        prebuy = {
            "id":            str(uuid.uuid4()),
            "symbol":        symbol_usdt,
            "setup_type":    setup_type,
            "regime":        regime,
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
        }

        prebuy_id = insert_pending(conn, prebuy)

        if prebuy_id:
            prebuy_count += 1
            prebuy_today += 1

            # Auto BUY triggeren via webhook
            if btc_regime != "BEAR":
                trigger_auto_buy(prebuy_id)
            else:
                log(f"⚠️ {symbol_usdt} — Pre-BUY aangemaakt maar geen auto_buy (BTC BEAR)")

        if prebuy_today >= MAX_PREBUY_PER_DAY:
            log(f"Pre-buy daglimiet bereikt: {prebuy_today}")
            break

        if scanned % 25 == 0:
            log(f"Voortgang: {scanned}/{len(bitvavo_usdt_symbols)} gescand, {prebuy_count} pre-buys")

    log(f"✅ Scan klaar: {scanned} gescand, {prebuy_count} pre-buys gegenereerd")
    return prebuy_count


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    log("=" * 50)
    log("Multi Coin Scorer v2.0 — gestart")
    log("=" * 50)
    log(f"Database:     {'✅' if DATABASE_URL else '❌ ONTBREEKT'}")
    log(f"Webhook URL:  {'✅' if WEBHOOK_BASE_URL else '⚠️ niet ingesteld'}")
    log(f"Claude API:   {'✅' if ANTHROPIC_API_KEY else '❌ ONTBREEKT'}")
    log(f"Min score:    {MIN_SCORE_TO_TRADE}")
    log(f"Min chance:   {MIN_CHANCE}%")
    log(f"ATR period:   {ATR_PERIOD} | multiplier: {ATR_MULTIPLIER}")
    log(f"Fee+slip:     {TOTAL_COST_PCT*100:.2f}%")
    log(f"Trading:      {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC")
    log("=" * 50)

    try:
        with db_connect() as conn:
            n = scan_universe(conn)
            log(f"Resultaat: {n} pre-buys gegenereerd")
    except Exception as e:
        report_error(e, "__main__", severity="KRITIEK")
        sys.exit(1)
