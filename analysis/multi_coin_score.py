# analysis/multi_coin_score.py
# ============================================================
# Crypto AI Bot — Multi Coin Scorer v2.0  (VOLLEDIG)
# ============================================================
# Scant alle Bitvavo-tradable coins via Binance data.
# Berekent een score (0-100) per coin op basis van:
#   - Wilder RSI (nauwkeuriger dan simpele RSI)
#   - ATR-based dynamische stops (ipv vaste 2%)
#   - BTC regime filter (geen trades in BEAR)
#   - Volume bevestiging
#   - Multi-timeframe (1H + 4H)
#   - Experience scoreboard (historische win rate)
#   - Coin cooldown (48u na verlies)
#   - Coin blacklist (win rate <35% na 15 trades)
#   - Volatiliteit filter
#   - Momentum detectie
#   - Support/weerstand detectie
#   - Coin regime (per coin: BULL/BEAR/RANGE)
#   - Coach geheugen integratie
#   - Scan sessie statistieken
#   - Adaptieve score drempel
#
# SAMENWERKING MET ANDERE BESTANDEN:
#   -> Schrijft naar public.pending_approvals
#   -> Leest van public.bot_state (is bot actief?)
#   -> Leest van public.experience_trades (cooldown/blacklist)
#   -> Leest van public.experience_scoreboard (win rates)
#   -> Leest van public.btc_regime_4h (BTC regime filter)
#   -> Leest van public.coach_memory (adaptieve params)
#   -> Schrijft naar public.scanner_sessies (statistieken)
#   -> Triggert /auto_buy op whatsapp_webhook.py
#   -> Claude AI analyseert elk signaal + fouten
#
# IDENTIEK AAN ALLE ANDERE BESTANDEN:
#   v Zelfde ENV variabelen en Fase 1 limieten
#   v Zelfde send_whatsapp() implementatie
#   v Zelfde Claude health monitoring
#   v Zelfde bot state (PostgreSQL bot_state tabel)
#   v Zelfde is_bot_active / is_bot_paused check
#   v Zelfde sslmode="require" op DB connectie
#   v Zelfde safe_int / safe_float / safe_str helpers
#   v Zelfde trading hours filter
# ============================================================

from __future__ import annotations

import json
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
# ENV
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
# FASE 1 LIMIETEN
# ============================================================
MAX_PER_TRADE_EUR       = float(os.getenv("MAX_PER_TRADE_EUR") or "0.50")
MAX_REAL_TRADES_PER_DAY = int(os.getenv("MAX_REAL_TRADES_PER_DAY") or "10")
MAX_OPEN_REAL_TRADES    = int(os.getenv("MAX_OPEN_REAL_TRADES") or "5")
DAILY_STOP_LOSS_EUR     = float(os.getenv("DAILY_STOP_LOSS_EUR") or "5.00")
TRADING_HOURS_START     = int(os.getenv("TRADING_HOURS_START") or "9")
TRADING_HOURS_END       = int(os.getenv("TRADING_HOURS_END") or "17")
BOT_STATE_TABLE         = "public.bot_state"

# Score & filter instellingen
MIN_SCORE_TO_TRADE  = int(os.getenv("MIN_SCORE_TO_TRADE") or "92")
MIN_CHANCE          = int(os.getenv("MIN_CHANCE") or "75")
MIN_CONFIDENCE      = int(os.getenv("MIN_CONFIDENCE") or "75")
MAX_PREBUY_PER_DAY  = int(os.getenv("MAX_PREBUY_PER_DAY") or "20")
PREBUY_EXPIRY_HOURS = int(os.getenv("PREBUY_EXPIRY_HOURS") or "2")

# Fee + slippage
BITVAVO_FEE_PCT = float(os.getenv("BITVAVO_FEE_PCT") or "0.0025")
SLIPPAGE_PCT    = float(os.getenv("SLIPPAGE_PCT") or "0.001")
TOTAL_COST_PCT  = BITVAVO_FEE_PCT + SLIPPAGE_PCT

# Coin filter instellingen
COIN_COOLDOWN_HOURS   = float(os.getenv("COIN_COOLDOWN_HOURS") or "48.0")
BLACKLIST_MIN_TRADES  = int(os.getenv("BLACKLIST_MIN_TRADES") or "15")
BLACKLIST_MAX_WINRATE = float(os.getenv("BLACKLIST_MAX_WINRATE") or "0.35")

# Binance API
BINANCE_BASE    = "https://api.binance.com/api/v3"
BINANCE_SLEEP   = float(os.getenv("BINANCE_SLEEP") or "0.2")
BINANCE_TIMEOUT = int(os.getenv("BINANCE_TIMEOUT") or "10")
MAX_RETRIES     = int(os.getenv("MAX_RETRIES") or "3")
BITVAVO_BASE    = "https://api.bitvavo.com"

# ATR instellingen
ATR_PERIOD     = int(os.getenv("ATR_PERIOD") or "14")
ATR_MULTIPLIER = float(os.getenv("ATR_MULTIPLIER") or "1.6")
ATR_TARGET_R   = float(os.getenv("ATR_TARGET_R") or "2.5")

# RSI instellingen
RSI_PERIOD = int(os.getenv("RSI_PERIOD") or "14")
RSI_MIN    = int(os.getenv("RSI_MIN") or "40")
RSI_MAX    = int(os.getenv("RSI_MAX") or "60")

# BTC regime filter
BTC_SKIP_BEAR = os.getenv("BTC_SKIP_BEAR", "1").strip() == "1"

# Volatiliteit filter
MAX_ATR_PCT   = float(os.getenv("MAX_ATR_PCT") or "0.08")   # max 8% ATR van prijs
MIN_ATR_PCT   = float(os.getenv("MIN_ATR_PCT") or "0.005")  # min 0.5% ATR

# Markets cache
_MARKETS_CACHE: Dict[str, Any] = {"ts": 0.0, "markets": set()}
_MARKETS_TTL = 30 * 60

# Scan sessie statistieken (in-memory)
_SESSIE: Dict[str, Any] = {
    "start":        None,
    "gescand":      0,
    "signalen":     0,
    "gefilterd":    {},   # reden -> count
    "beste_score":  0,
    "beste_coin":   "",
    "coins_met_score": [],
}


# ============================================================
# BASIS HELPERS
# ============================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def log(msg: str) -> None:
    ts = now_utc().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [SCANNER] {msg}", flush=True)

def safe_int(x: Any, default: int = 0) -> int:
    try: return int(x)
    except Exception: return default

def safe_float(x: Any, default: float = 0.0) -> float:
    try: return float(x)
    except Exception: return default

def safe_str(x: Any, default: str = "") -> str:
    if x is None: return default
    try:
        s = str(x).strip()
        return s if s else default
    except Exception: return default

def utc_day_str() -> str:
    return now_utc().strftime("%Y-%m-%d")

def is_trading_hours() -> bool:
    return TRADING_HOURS_START <= now_utc().hour < TRADING_HOURS_END

def is_weekend() -> bool:
    return now_utc().weekday() >= 5

def tel_filter(reden: str) -> None:
    _SESSIE["gefilterd"][reden] = _SESSIE["gefilterd"].get(reden, 0) + 1


# ============================================================
# WHATSAPP
# ============================================================
def send_whatsapp(message: str) -> bool:
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO]):
        log(f"WhatsApp (geen Twilio): {message[:80]}")
        return False
    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={"From": TWILIO_WHATSAPP_FROM, "To": TWILIO_WHATSAPP_TO, "Body": message},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            log(f"WhatsApp verzonden ({len(message)} tekens)")
            return True
        log(f"WhatsApp {resp.status_code}: {resp.text[:100]}")
        return False
    except Exception as e:
        log(f"WhatsApp fout: {e}")
        return False


# ============================================================
# CLAUDE AI
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
            content = resp.json().get("content", [])
            if content:
                return content[0]["text"].strip()
        log(f"Claude API status {resp.status_code}")
        return ""
    except Exception as e:
        log(f"Claude API fout: {e}")
        return ""


def report_error(error: Exception, function: str,
                 severity: str = "HOOG", symbol: str = "") -> None:
    log(f"[{severity}] {function} ({symbol}): {type(error).__name__}: {error}")
    if severity not in ("KRITIEK", "HOOG"):
        return
    prompt = (
        f"Je bent een crypto trading bot monitor.\n"
        f"Ernst: {severity} | Functie: {function} | Coin: {symbol or 'X'}\n"
        f"Fout: {type(error).__name__}: {str(error)[:200]}\n\n"
        f"Geef in 2 zinnen Nederlands: wat ging mis en wat te doen."
    )
    uitleg = _claude_analyse(prompt, 150) or f"{type(error).__name__}: {str(error)[:100]}"
    send_whatsapp(
        f"SCANNER FOUT -- {severity}\n"
        f"Functie: {function}\nCoin: {symbol}\n"
        f"Claude: {uitleg}\n\nCommands: STATUS | STOP"
    )


def claude_beoordeel_signaal(symbol: str, setup_type: str, regime: str,
                              btc_regime: str, score: int, chance: int,
                              confidence: int, rsi_4h: float, vol_ratio: float,
                              exp_win_rate: float, exp_n: int, why_tag: str) -> str:
    prompt = (
        f"Crypto trading bot signaal beoordeling in 2 zinnen Nederlands.\n"
        f"Coin:{symbol} Setup:{setup_type} Regime:{regime} BTC:{btc_regime}\n"
        f"Score:{score} Kans:{chance}% Conf:{confidence}% RSI:{rsi_4h:.1f} "
        f"Vol:{vol_ratio:.1f}x WR:{exp_win_rate:.1%}({exp_n} trades)\n"
        f"Tags: {why_tag}\nIs dit een goed signaal en wat zijn de risicos?"
    )
    return _claude_analyse(prompt, 120)


def claude_scanner_health_check() -> str:
    prompt = (
        f"Check multi_coin_score.py configuratie in 3 zinnen Nederlands.\n"
        f"DB:{'OK' if DATABASE_URL else 'ONTBREEKT'} "
        f"Webhook:{'OK' if WEBHOOK_BASE_URL else 'NIET INGESTELD'} "
        f"Claude:{'OK' if ANTHROPIC_API_KEY else 'ONTBREEKT'} "
        f"MinScore:{MIN_SCORE_TO_TRADE} ATR:{ATR_MULTIPLIER} "
        f"Fee:{TOTAL_COST_PCT*100:.2f}% Uren:{TRADING_HOURS_START}-{TRADING_HOURS_END}UTC\n"
        f"Zijn er problemen of risicos?"
    )
    return _claude_analyse(prompt, 150)


def claude_analyseer_sessie(sessie: Dict) -> str:
    """Claude analyseert de volledige scan sessie en geeft aanbevelingen."""
    duur = (now_utc() - sessie["start"]).total_seconds() / 60 if sessie["start"] else 0
    filter_txt = ", ".join(f"{k}:{v}" for k, v in list(sessie["gefilterd"].items())[:8])
    prompt = (
        f"Analyseer deze crypto scanner sessie in 3 zinnen Nederlands.\n"
        f"Gescand:{sessie.get('gescand',0)} Signalen:{sessie.get('signalen',0)} "
        f"Duur:{duur:.1f}min\n"
        f"Beste coin: {sessie.get('beste_coin','')} score={sessie.get('beste_score',0)}\n"
        f"Filters: {filter_txt}\n"
        f"Wat valt op en zijn er aanbevelingen voor de volgende scan?"
    )
    return _claude_analyse(prompt, 180)


def claude_beoordeel_marktomstandigheden(btc_regime: str, n_signalen: int,
                                          n_gescand: int, uur: int) -> str:
    """Claude geeft een korte marktbeoordeling op basis van scan resultaten."""
    conversie = round(n_signalen / max(n_gescand, 1) * 100, 1)
    prompt = (
        f"Beoordeel marktomstandigheden voor crypto bot in 2 zinnen Nederlands.\n"
        f"BTC regime:{btc_regime} Uur:{uur}:00 UTC\n"
        f"Scan: {n_signalen}/{n_gescand} signalen ({conversie}% conversie)\n"
        f"Is dit een goede tijd om te handelen?"
    )
    return _claude_analyse(prompt, 100)


# ============================================================
# DATABASE
# ============================================================
def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, connect_timeout=8,
                            options="-c statement_timeout=8000",
                            sslmode="require")

def get_bot_state_value(conn, key: str, default: str = "") -> str:
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT value FROM {BOT_STATE_TABLE} WHERE key=%s", (key,))
            row = cur.fetchone()
            return safe_str(row[0], default) if row else default
    except Exception: return default

def set_bot_state_value(conn, key: str, value: str) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.bot_state(key,value,updated_at) VALUES(%s,%s,NOW()) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=NOW()",
                (key, value))
        conn.commit()
    except Exception as e: log(f"set_bot_state: {e}")

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
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return now_utc() <= until
    except Exception: return True


# ============================================================
# COACH GEHEUGEN INTEGRATIE
# Leest coach aanbevelingen voor adaptieve drempels
# ============================================================
def haal_coach_drempels_op(conn) -> Dict[str, Any]:
    """
    Leest coach aanbevelingen uit bot_state en coach_memory.
    De ai_coach.py schrijft optimale drempels op basis van historische data.
    Scanner past zich automatisch aan.
    """
    drempels = {
        "min_score":     MIN_SCORE_TO_TRADE,
        "min_chance":    MIN_CHANCE,
        "min_confidence": MIN_CONFIDENCE,
        "atr_multiplier": ATR_MULTIPLIER,
        "atr_target_r":  ATR_TARGET_R,
        "rsi_min":       RSI_MIN,
        "rsi_max":       RSI_MAX,
    }
    try:
        # Lees coach suggesties uit bot_state
        coach_score  = get_bot_state_value(conn, "min_score_to_trade", "")
        coach_atr    = get_bot_state_value(conn, "atr_multiplier", "")
        coach_target = get_bot_state_value(conn, "atr_target_r", "")
        coach_rsi_min = get_bot_state_value(conn, "rsi_min", "")
        coach_rsi_max = get_bot_state_value(conn, "rsi_max", "")

        if coach_score:
            drempels["min_score"] = safe_int(coach_score, MIN_SCORE_TO_TRADE)
        if coach_atr:
            drempels["atr_multiplier"] = safe_float(coach_atr, ATR_MULTIPLIER)
        if coach_target:
            drempels["atr_target_r"] = safe_float(coach_target, ATR_TARGET_R)
        if coach_rsi_min:
            drempels["rsi_min"] = safe_int(coach_rsi_min, RSI_MIN)
        if coach_rsi_max:
            drempels["rsi_max"] = safe_int(coach_rsi_max, RSI_MAX)

        # Lees ook coach_memory voor scanner suggesties
        if table_bestaat(conn, "coach_memory"):
            with conn.cursor() as cur:
                cur.execute("""
                SELECT waarde FROM public.coach_memory
                WHERE type='scanner' AND sleutel='suggesties'
                ORDER BY bijgewerkt DESC LIMIT 1
                """)
                row = cur.fetchone()
                if row and row[0]:
                    try:
                        suggesties = json.loads(row[0])
                        drempels["coach_suggesties"] = suggesties[:3]
                        if suggesties:
                            log(f"Coach suggesties geladen: {len(suggesties)}")
                    except Exception: pass

    except Exception as e:
        log(f"Coach drempels fout: {e}")

    return drempels


def table_bestaat(conn, naam: str) -> bool:
    """Controleert of een tabel bestaat in de DB."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT 1 FROM information_schema.tables
            WHERE table_schema='public' AND table_name=%s
            """, (naam,))
            return cur.fetchone() is not None
    except Exception: return False


# ============================================================
# SCANNER SESSIE STATISTIEKEN
# ============================================================
def init_sessie() -> None:
    """Initialiseert de scan sessie statistieken."""
    _SESSIE["start"]          = now_utc()
    _SESSIE["gescand"]        = 0
    _SESSIE["signalen"]       = 0
    _SESSIE["gefilterd"]      = {}
    _SESSIE["beste_score"]    = 0
    _SESSIE["beste_coin"]     = ""
    _SESSIE["coins_met_score"] = []


def update_sessie(symbol: str, score: int, reden: Optional[str] = None) -> None:
    """Update sessie statistieken na elke coin scan."""
    _SESSIE["gescand"] += 1
    if reden:
        tel_filter(reden)
    if score > 0:
        _SESSIE["coins_met_score"].append({"symbol": symbol, "score": score})
        if score > _SESSIE["beste_score"]:
            _SESSIE["beste_score"] = score
            _SESSIE["beste_coin"]  = symbol
    if score >= MIN_SCORE_TO_TRADE:
        _SESSIE["signalen"] += 1


def sla_sessie_op(conn) -> None:
    """Slaat sessie statistieken op in bot_state voor het dashboard."""
    if not _SESSIE["start"]:
        return
    try:
        duur = (now_utc() - _SESSIE["start"]).total_seconds()
        data = {
            "tijdstip":     now_utc().isoformat(),
            "gescand":      _SESSIE["gescand"],
            "signalen":     _SESSIE["signalen"],
            "duur_sec":     round(duur, 1),
            "beste_coin":   _SESSIE["beste_coin"],
            "beste_score":  _SESSIE["beste_score"],
            "filters":      _SESSIE["gefilterd"],
            "top5":         sorted(
                _SESSIE["coins_met_score"],
                key=lambda x: x["score"], reverse=True
            )[:5],
        }
        set_bot_state_value(conn, "laatste_scan_sessie", json.dumps(data))
        set_bot_state_value(conn, "laatste_scan_tijd", now_utc().strftime("%Y-%m-%d %H:%M:%S UTC"))
        set_bot_state_value(conn, "laatste_scan_coins", str(_SESSIE["gescand"]))
        set_bot_state_value(conn, "laatste_scan_signalen", str(_SESSIE["signalen"]))
        log(f"Sessie opgeslagen: {_SESSIE['gescand']} coins | {_SESSIE['signalen']} signalen | {duur:.0f}s")
    except Exception as e:
        log(f"Sessie opslaan fout: {e}")


def log_sessie_voortgang(n: int, totaal: int) -> None:
    """Logt voortgang elke 20 coins."""
    if n % 20 == 0 and n > 0:
        pct = round(n / max(totaal, 1) * 100)
        log(
            f"Voortgang: {n}/{totaal} ({pct}%) | "
            f"{_SESSIE['signalen']} signalen | "
            f"beste: {_SESSIE['beste_coin']} score={_SESSIE['beste_score']}"
        )


# ============================================================
# BITVAVO UNIVERSE FILTER
# ============================================================
def get_tradable_markets() -> Set[str]:
    """
    Haalt actieve Bitvavo EUR markets op via publieke API.
    Cache: 30 minuten TTL.
    """
    now_ts = time.time()
    if _MARKETS_CACHE["markets"] and (now_ts - _MARKETS_CACHE["ts"]) < _MARKETS_TTL:
        return _MARKETS_CACHE["markets"]  # type: ignore

    try:
        resp = requests.get(f"{BITVAVO_BASE}/v2/markets", timeout=15)
        resp.raise_for_status()
        tradable: Set[str] = set()
        for item in resp.json():
            market = safe_str(item.get("market"))
            status = safe_str(item.get("status")).lower()
            if market and status == "trading" and market.endswith("-EUR"):
                tradable.add(market)
        _MARKETS_CACHE["ts"]      = now_ts
        _MARKETS_CACHE["markets"] = tradable
        log(f"Bitvavo markets: {len(tradable)} tradable EUR markets")
        return tradable
    except Exception as e:
        log(f"Bitvavo markets fout: {e}")
        return _MARKETS_CACHE.get("markets") or set()


def symbol_to_bitvavo_market(symbol_usdt: str) -> Optional[str]:
    """ETHUSDT -> ETH-EUR als tradable op Bitvavo."""
    s = safe_str(symbol_usdt).upper()
    if not s.endswith("USDT"):
        return None
    base   = s[:-4]
    market = f"{base}-EUR"
    return market if market in get_tradable_markets() else None


# ============================================================
# BINANCE DATA
# ============================================================
def binance_get(endpoint: str, params: dict, retries: int = MAX_RETRIES) -> Optional[Any]:
    """Binance public API met retry en backoff."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                f"{BINANCE_BASE}{endpoint}", params=params, timeout=BINANCE_TIMEOUT
            )
            if resp.ok:
                return resp.json()
            log(f"Binance {resp.status_code} ({endpoint}) poging {attempt}/{retries}")
        except requests.exceptions.Timeout:
            log(f"Binance timeout poging {attempt}/{retries}")
        except Exception as e:
            log(f"Binance fout poging {attempt}/{retries}: {e}")
        if attempt < retries:
            time.sleep(2 ** attempt)
    return None


def fetch_candles(symbol: str, interval: str = "4h", limit: int = 120) -> List[Dict]:
    """Haalt OHLCV candles op van Binance."""
    time.sleep(BINANCE_SLEEP)
    data = binance_get("/klines", {"symbol": symbol, "interval": interval, "limit": limit})
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
        except Exception: continue
    return candles


def fetch_ticker_24h(symbol: str) -> Optional[Dict]:
    """24u ticker data van Binance."""
    time.sleep(BINANCE_SLEEP)
    return binance_get("/ticker/24hr", {"symbol": symbol})


def fetch_order_book_spread(symbol: str) -> float:
    """
    Haalt bid/ask spread op als proxy voor liquiditeit.
    Hoge spread = lage liquiditeit = meer slippage risico.
    """
    time.sleep(BINANCE_SLEEP)
    data = binance_get("/ticker/bookTicker", {"symbol": symbol})
    if not data:
        return 0.0
    try:
        bid = safe_float(data.get("bidPrice", 0))
        ask = safe_float(data.get("askPrice", 0))
        mid = (bid + ask) / 2
        if mid > 0:
            return round((ask - bid) / mid * 100, 4)  # spread als %
    except Exception: pass
    return 0.0


# ============================================================
# TECHNISCHE INDICATOREN
# ============================================================
def rsi_wilder(closes: List[float], period: int = 14) -> Optional[float]:
    """
    Wilder RSI — identiek aan TradingView berekening.
    Nauwkeuriger dan simpele RSI.
    """
    if len(closes) < period + 1:
        return None
    changes  = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains    = [max(c, 0.0) for c in changes]
    losses   = [max(-c, 0.0) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


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
    ATR met Wilder smoothing.
    TR = max(high-low, |high-prev_close|, |low-prev_close|)
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
    return round(atr_val, 8)


def bollinger_bands(closes: List[float], period: int = 20,
                    num_std: float = 2.0) -> Tuple[float, float, float]:
    """
    Bollinger Bands — upper, middle, lower.
    Gebruikt voor volatiliteit en squeeze detectie.
    """
    if len(closes) < period:
        c = closes[-1] if closes else 0
        return c, c, c
    recent = closes[-period:]
    mid    = sum(recent) / period
    std    = (sum((x - mid) ** 2 for x in recent) / period) ** 0.5
    return round(mid + num_std * std, 8), round(mid, 8), round(mid - num_std * std, 8)


def macd(closes: List[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> Tuple[float, float, float]:
    """
    MACD indicator: macd_line, signal_line, histogram.
    Gebruikt als extra trend bevestiging.
    """
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    if ema_fast is None or ema_slow is None:
        return 0.0, 0.0, 0.0
    macd_line = ema_fast - ema_slow
    # Bereken signal lijn (EMA van MACD)
    macd_vals = []
    for i in range(slow - 1, len(closes)):
        ef = ema(closes[:i+1], fast)
        es = ema(closes[:i+1], slow)
        if ef and es:
            macd_vals.append(ef - es)
    if len(macd_vals) < signal:
        return round(macd_line, 8), 0.0, round(macd_line, 8)
    signal_line = ema(macd_vals, signal)
    signal_line = signal_line or 0.0
    histogram   = macd_line - signal_line
    return round(macd_line, 8), round(signal_line, 8), round(histogram, 8)


def stochastic_rsi(closes: List[float], rsi_period: int = 14,
                   stoch_period: int = 14) -> Optional[float]:
    """
    Stochastic RSI — RSI van RSI.
    Waarden 0-100. Oversold <20, Overbought >80.
    Geeft extra signaalbevestiging.
    """
    if len(closes) < rsi_period + stoch_period + 1:
        return None
    rsi_vals = []
    for i in range(rsi_period, len(closes) + 1):
        r = rsi_wilder(closes[:i], rsi_period)
        if r is not None:
            rsi_vals.append(r)
    if len(rsi_vals) < stoch_period:
        return None
    recent_rsi = rsi_vals[-stoch_period:]
    rsi_min    = min(recent_rsi)
    rsi_max    = max(recent_rsi)
    if rsi_max == rsi_min:
        return 50.0
    stoch_rsi = (rsi_vals[-1] - rsi_min) / (rsi_max - rsi_min) * 100
    return round(stoch_rsi, 2)


def detect_coin_regime(closes: List[float]) -> str:
    """
    Detecteert regime voor individuele coin.
    BULL / BEAR / RANGE op basis van SMA20 vs SMA50.
    """
    if len(closes) < 50:
        return "UNKNOWN"
    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    if sma20 is None or sma50 is None:
        return "UNKNOWN"
    diff_pct = abs(sma20 - sma50) / max(sma50, 1e-10)
    if diff_pct < 0.015:
        return "RANGE"
    return "BULL" if sma20 > sma50 else "BEAR"


def detect_volatiliteit(candles: List[Dict], prijs: float) -> Tuple[str, float]:
    """
    Detecteert volatiliteit op basis van ATR als % van prijs.
    Geeft (label, atr_pct) terug.
    Label: LAAG / NORMAAL / HOOG / EXTREEM
    """
    atr = atr_calc(candles, ATR_PERIOD)
    if not atr or prijs <= 0:
        return "NORMAAL", 0.02
    atr_pct = atr / prijs
    if atr_pct < MIN_ATR_PCT:
        return "LAAG", atr_pct
    elif atr_pct > MAX_ATR_PCT:
        return "EXTREEM", atr_pct
    elif atr_pct > MAX_ATR_PCT * 0.7:
        return "HOOG", atr_pct
    return "NORMAAL", atr_pct


def detecteer_support_weerstand(candles: List[Dict],
                                 lookback: int = 20) -> Tuple[float, float]:
    """
    Detecteert recente support en weerstand niveaus.
    Gebruikt swing highs en lows.
    Geeft (support, weerstand) terug.
    """
    if len(candles) < lookback:
        if candles:
            prijs = candles[-1]["close"]
            return prijs * 0.97, prijs * 1.03
        return 0.0, 0.0

    recente = candles[-lookback:]
    highs   = [c["high"] for c in recente]
    lows    = [c["low"] for c in recente]

    weerstand = max(highs)
    support   = min(lows)

    return round(support, 8), round(weerstand, 8)


def detecteer_momentum(closes: List[float], periode: int = 10) -> float:
    """
    Berekent momentum: (huidig - N periodes terug) / N periodes terug * 100.
    Positief = stijgend momentum, negatief = dalend.
    """
    if len(closes) < periode + 1:
        return 0.0
    oud   = closes[-(periode + 1)]
    huidig = closes[-1]
    if oud <= 0:
        return 0.0
    return round((huidig - oud) / oud * 100, 3)


def detecteer_squeeze(closes: List[float], candles: List[Dict]) -> bool:
    """
    Bollinger Band Squeeze: bands smaller dan Keltner Channel.
    Squeeze = lage volatiliteit voor potentiele uitbraak.
    """
    if len(closes) < 20 or len(candles) < 20:
        return False
    bb_upper, bb_mid, bb_lower = bollinger_bands(closes, 20, 2.0)
    atr = atr_calc(candles, 14)
    if not atr:
        return False
    kc_upper = bb_mid + 1.5 * atr
    kc_lower = bb_mid - 1.5 * atr
    return bb_upper < kc_upper and bb_lower > kc_lower


# ============================================================
# BTC REGIME
# ============================================================
def get_btc_regime(conn) -> str:
    """Haalt BTC regime op uit DB."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT regime FROM public.btc_regime_4h
            ORDER BY open_time DESC LIMIT 1
            """)
            row = cur.fetchone()
            return safe_str(row[0], "UNKNOWN") if row else "UNKNOWN"
    except Exception: return "UNKNOWN"


def get_btc_sterkte(conn) -> float:
    """
    Haalt BTC regime sterkte op (0-100%).
    Hogere sterkte = sterker regime = meer vertrouwen.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COALESCE(strength, 50) FROM public.btc_regime_4h
            ORDER BY open_time DESC LIMIT 1
            """)
            row = cur.fetchone()
            return safe_float(row[0], 50.0) if row else 50.0
    except Exception: return 50.0


# ============================================================
# SETUP TYPE DETECTIE
# ============================================================
def detect_setup_type(candles_4h: List[Dict],
                       candles_1h: List[Dict]) -> Tuple[str, str]:
    """
    Detecteert setup type op basis van 4H en 1H candles.
    Geeft (setup_type, why_tag) terug.

    Setup types:
    - TREND_PULLBACK: pullback naar SMA20 in uptrend
    - BREAKOUT:       uitbraak boven recente swing high
    - BOUNCE:         bounce van SMA50 support
    - MOMENTUM:       sterk stijgend met gezonde RSI
    - SQUEEZE_BREAK:  uitbraak na Bollinger Band squeeze
    - OVERSOLD_RECLAIM: sterk oversold maar herstelt
    """
    if len(candles_4h) < 20:
        return "UNKNOWN", "te_weinig_data"

    closes_4h = [c["close"] for c in candles_4h]
    closes_1h = [c["close"] for c in candles_1h] if candles_1h else []

    rsi_4h   = rsi_wilder(closes_4h, RSI_PERIOD)
    sma20_4h = sma(closes_4h, 20)
    sma50_4h = sma(closes_4h, 50)
    current  = closes_4h[-1]
    vorige   = closes_4h[-2] if len(closes_4h) >= 2 else current

    if rsi_4h is None or sma20_4h is None or sma50_4h is None:
        return "UNKNOWN", "indicator_fout"

    # SQUEEZE_BREAK — uitbraak na squeeze
    if detecteer_squeeze(closes_4h, candles_4h):
        if current > vorige * 1.005:  # Stijgend
            return "SQUEEZE_BREAK", f"squeeze|RSI={rsi_4h:.0f}"

    # BREAKOUT — boven recente high
    if len(candles_4h) >= 20:
        high_20   = max(c["high"] for c in candles_4h[-20:])
        prev_close = closes_4h[-2] if len(closes_4h) > 1 else current
        if current > high_20 * 0.998 and prev_close < high_20:
            return "BREAKOUT", f"break_high20({high_20:.4f})|RSI={rsi_4h:.0f}"

    # TREND_PULLBACK — pullback naar SMA20 in uptrend
    if sma20_4h > sma50_4h and current > sma50_4h:
        dist_sma20 = abs(current - sma20_4h) / sma20_4h
        if dist_sma20 < 0.025 and RSI_MIN <= rsi_4h <= 58:
            return "TREND_PULLBACK", f"sma20_pb({dist_sma20*100:.1f}%)|RSI={rsi_4h:.0f}"

    # BOUNCE — van SMA50 support
    if sma50_4h > 0:
        dist_sma50 = abs(current - sma50_4h) / sma50_4h
        if dist_sma50 < 0.02 and rsi_4h < 52:
            return "BOUNCE", f"sma50_bounce({dist_sma50*100:.1f}%)|RSI={rsi_4h:.0f}"

    # OVERSOLD_RECLAIM — sterk oversold maar herstelt
    if rsi_4h < 30 and current > vorige * 1.002:
        return "OVERSOLD_RECLAIM", f"oversold_bounce|RSI={rsi_4h:.0f}"

    # MOMENTUM — sterke stijging met gezonde RSI
    mom = detecteer_momentum(closes_4h, 10)
    if 52 <= rsi_4h <= RSI_MAX and current > sma20_4h > sma50_4h and mom > 3:
        return "MOMENTUM", f"momentum({mom:.1f}%)|RSI={rsi_4h:.0f}"

    return "UNKNOWN", f"geen_setup|RSI={rsi_4h:.0f}"


# ============================================================
# SCORE BEREKENING
# ============================================================
def calculate_score(candles_4h: List[Dict], candles_1h: List[Dict],
                    ticker: Optional[Dict], regime: str, btc_regime: str,
                    btc_sterkte: float, setup_type: str, exp_win_rate: float,
                    exp_n: int, drempels: Dict) -> Tuple[int, int, int, str, float, float]:
    """
    Berekent score (0-100), chance, confidence.

    Score componenten (totaal 100 punten):
    RSI in ideale zone:        0-20 punten
    Trend alignment SMA:       0-20 punten
    Volume bevestiging:        0-15 punten
    Experience win rate:       0-20 punten
    BTC regime + sterkte:      0-15 punten
    Multi-timeframe 1H:        0-10 punten
    ────────────────────────────────────────
    Totaal max:                100 punten
    Fee/spread malus:          max -5 punten
    Volatiliteit malus:        max -5 punten
    MACD bonus:                max +5 punten
    Squeeze bonus:             max +3 punten
    Momentum bonus:            max +4 punten
    """
    if not candles_4h or len(candles_4h) < 20:
        return 0, 0, 0, "te_weinig_data", 0.0, 0.0

    rsi_min_eff = drempels.get("rsi_min", RSI_MIN)
    rsi_max_eff = drempels.get("rsi_max", RSI_MAX)

    closes_4h  = [c["close"] for c in candles_4h]
    closes_1h  = [c["close"] for c in candles_1h] if candles_1h else []
    volumes_4h = [c["volume"] for c in candles_4h]

    rsi_4h   = rsi_wilder(closes_4h, RSI_PERIOD)
    sma20_4h = sma(closes_4h, 20)
    sma50_4h = sma(closes_4h, 50)
    current  = closes_4h[-1]

    vol_now   = volumes_4h[-1] if volumes_4h else 0
    vol_avg   = sma(volumes_4h[:-1], 20) or 1.0
    vol_ratio = vol_now / vol_avg if vol_avg > 0 else 1.0

    score    = 0
    why_tags = []

    # ── 1. RSI in ideale zone (0-20 punten) ──────────────
    if rsi_4h is not None:
        if rsi_min_eff <= rsi_4h <= rsi_max_eff:
            rsi_score = 20 - abs(rsi_4h - 50) / 1.5
            score += int(min(rsi_score, 20))
            why_tags.append(f"RSI={rsi_4h:.0f}OK")
        elif rsi_4h < rsi_min_eff:
            score += 5
            why_tags.append(f"RSI={rsi_4h:.0f}oversold")
        else:
            why_tags.append(f"RSI={rsi_4h:.0f}overbought")
    else:
        rsi_4h = 50.0

    # ── 2. Trend alignment (0-20 punten) ─────────────────
    if sma20_4h and sma50_4h:
        if sma20_4h > sma50_4h and current > sma20_4h:
            score += 20
            why_tags.append("trend=BULL_BOVEN_SMA20")
        elif sma20_4h > sma50_4h and current > sma50_4h:
            score += 12
            why_tags.append("trend=BULL_ONDER_SMA20")
        elif sma20_4h < sma50_4h:
            why_tags.append("trend=BEAR")
        else:
            score += 5
            why_tags.append("trend=RANGE")
    else:
        why_tags.append("trend=onbekend")

    # ── 3. Volume bevestiging (0-15 punten) ───────────────
    if vol_ratio >= 2.0:
        score += 15; why_tags.append(f"vol={vol_ratio:.1f}xHOOG")
    elif vol_ratio >= 1.3:
        score += 10; why_tags.append(f"vol={vol_ratio:.1f}xOK")
    elif vol_ratio >= 1.0:
        score += 6;  why_tags.append(f"vol={vol_ratio:.1f}xNORMAAL")
    else:
        why_tags.append(f"vol={vol_ratio:.1f}xLAAG")

    # ── 4. Experience win rate (0-20 punten) ──────────────
    if exp_n >= 10:
        if exp_win_rate >= 0.65:
            score += 20; why_tags.append(f"exp={exp_win_rate:.0%}({exp_n})")
        elif exp_win_rate >= 0.55:
            score += 14; why_tags.append(f"exp={exp_win_rate:.0%}({exp_n})")
        elif exp_win_rate >= 0.45:
            score += 7;  why_tags.append(f"exp={exp_win_rate:.0%}({exp_n})")
        else:
            why_tags.append(f"exp={exp_win_rate:.0%}LAAG({exp_n})")
    elif exp_n >= 3:
        score += 10; why_tags.append(f"exp=weinig({exp_n})")
    else:
        score += 10; why_tags.append("exp=nieuw")

    # ── 5. BTC regime + sterkte (0-15 punten) ─────────────
    if btc_regime == "BULL":
        btc_score = int(10 + (btc_sterkte / 100) * 5)
        score += btc_score
        why_tags.append(f"BTC=BULL({btc_sterkte:.0f}%)")
    elif btc_regime == "RANGE":
        score += 7; why_tags.append("BTC=RANGE")
    elif btc_regime == "BEAR":
        why_tags.append("BTC=BEAR")
    else:
        score += 5; why_tags.append(f"BTC={btc_regime}")

    # ── 6. Multi-timeframe 1H (0-10 punten) ───────────────
    if closes_1h and len(closes_1h) >= RSI_PERIOD + 1:
        rsi_1h = rsi_wilder(closes_1h, RSI_PERIOD)
        if rsi_1h is not None:
            if rsi_min_eff <= rsi_1h <= rsi_max_eff:
                score += 10; why_tags.append(f"1H_RSI={rsi_1h:.0f}OK")
            elif rsi_1h < rsi_min_eff:
                score += 4;  why_tags.append(f"1H_RSI={rsi_1h:.0f}oversold")
            else:
                why_tags.append(f"1H_RSI={rsi_1h:.0f}overbought")
        else:
            score += 5; why_tags.append("1H_RSI=?")
    else:
        score += 5; why_tags.append("1H=geen")

    score = min(score, 100)

    # ── BONUSSEN ──────────────────────────────────────────
    # MACD bonus
    macd_line, signal_line, histogram = macd(closes_4h)
    if macd_line > signal_line and macd_line > 0:
        score = min(score + 5, 105); why_tags.append("MACD=bullish")
    elif macd_line < signal_line and macd_line < 0:
        score = max(score - 3, 0); why_tags.append("MACD=bearish")

    # Squeeze bonus
    if detecteer_squeeze(closes_4h, candles_4h):
        score = min(score + 3, 105); why_tags.append("SQUEEZE")

    # Momentum bonus
    mom = detecteer_momentum(closes_4h, 10)
    if mom > 5:
        score = min(score + 4, 105); why_tags.append(f"MOM=+{mom:.1f}%")
    elif mom < -5:
        score = max(score - 2, 0); why_tags.append(f"MOM={mom:.1f}%")

    # ── MALUSSEN ─────────────────────────────────────────
    # Fee correctie
    fee_impact = TOTAL_COST_PCT * 100
    if fee_impact > 0.3:
        score = max(0, score - 3); why_tags.append(f"fee={fee_impact:.2f}%")

    # Volatiliteit malus
    vol_label, atr_pct = detect_volatiliteit(candles_4h, current)
    if vol_label == "EXTREEM":
        score = max(0, score - 5); why_tags.append(f"vol=EXTREEM({atr_pct*100:.1f}%)")
    elif vol_label == "LAAG":
        score = max(0, score - 2); why_tags.append(f"vol=LAAG")

    score = max(0, min(100, score))

    # ── Chance berekening ─────────────────────────────────
    if exp_n >= 10 and exp_win_rate > 0:
        chance = int(exp_win_rate * 100 * (score / 100) * 1.2)
    else:
        chance = int(score * 0.65)
    chance = max(0, min(100, chance))

    # ── Confidence berekening ─────────────────────────────
    if exp_n >= 100:
        confidence = min(95, 70 + int(exp_win_rate * 25))
    elif exp_n >= 50:
        confidence = min(85, 55 + int(exp_win_rate * 25))
    elif exp_n >= 20:
        confidence = min(75, 45 + int(exp_win_rate * 20))
    elif exp_n >= 5:
        confidence = min(65, 35 + int(exp_win_rate * 20))
    else:
        confidence = 40

    why_tag = " | ".join(why_tags[:8])
    return score, chance, confidence, why_tag, rsi_4h, vol_ratio


# ============================================================
# EXPERIENCE SCOREBOARD
# ============================================================
def get_experience(conn, symbol: str, setup_type: str,
                   regime: str) -> Tuple[float, int, str]:
    """
    Haalt experience op uit scoreboard voor dit setup/regime.
    Geschreven door history_simulator.py.
    Geeft (win_rate, n_trades, bias) terug.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COALESCE(win_rate, 0.5) AS win_rate,
                COALESCE(n, 0)          AS n,
                COALESCE(bias, 'NEUTRAL') AS bias
            FROM public.experience_scoreboard
            WHERE symbol=%s AND setup_type=%s AND regime=%s
            LIMIT 1
            """, (symbol, setup_type, regime))
            row = cur.fetchone()
            if row:
                return safe_float(row[0]), safe_int(row[1]), safe_str(row[2], "NEUTRAL")
    except Exception: pass

    # Fallback: setup-niveau statistieken (niet coin-specifiek)
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COALESCE(AVG(win_rate), 0.5),
                COALESCE(SUM(n), 0)
            FROM public.experience_scoreboard
            WHERE setup_type=%s AND regime=%s
            """, (setup_type, regime))
            row = cur.fetchone()
            if row and safe_int(row[1]) >= 5:
                return safe_float(row[0]), safe_int(row[1]), "NEUTRAL"
    except Exception: pass

    return 0.5, 0, "NEUTRAL"


def get_coin_statistieken(conn, symbol: str) -> Dict[str, Any]:
    """
    Haalt uitgebreide coin statistieken op uit experience_trades.
    Wordt gebruikt voor extra filtering en scoring.
    """
    stats = {
        "n_total": 0, "n_live": 0, "win_rate": 0.5,
        "gem_r": 0.0, "gem_houdtijd_u": 0.0,
        "profit_factor": 1.0, "laatste_trade": None,
    }
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COUNT(*) AS n,
                COUNT(*) FILTER (WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')) AS n_live,
                ROUND(COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')::numeric/NULLIF(COUNT(*),0),3) AS wr,
                ROUND(AVG(COALESCE(pnl_r,result_r,0))::numeric,3) AS gem_r,
                ROUND(COALESCE(
                    SUM(CASE WHEN UPPER(outcome)='WIN' THEN ABS(COALESCE(pnl_eur,0)) ELSE 0 END) /
                    NULLIF(SUM(CASE WHEN UPPER(outcome)='LOSS' THEN ABS(COALESCE(pnl_eur,0)) ELSE 0 END),0.001),
                1),2) AS pf,
                MAX(COALESCE(exit_time, updated_at)) AS laatste
            FROM public.experience_trades
            WHERE UPPER(COALESCE(coin,'')) = UPPER(%s)
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
            """, (symbol.replace("USDT","").replace("BUSD",""),))
            row = cur.fetchone()
            if row:
                stats["n_total"]       = safe_int(row[0])
                stats["n_live"]        = safe_int(row[1])
                stats["win_rate"]      = safe_float(row[2], 0.5)
                stats["gem_r"]         = safe_float(row[3])
                stats["profit_factor"] = safe_float(row[4], 1.0)
                stats["laatste_trade"] = row[5]
    except Exception: pass
    return stats


def is_coin_on_cooldown(conn, symbol: str) -> bool:
    """
    48u cooldown na verlies — identiek aan alle andere bestanden.
    """
    coin = symbol.replace("USDT","").replace("BUSD","")
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT exit_time FROM public.experience_trades
            WHERE UPPER(COALESCE(coin,'')) = UPPER(%s)
              AND UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND UPPER(outcome) = 'LOSS'
              AND exit_time IS NOT NULL
            ORDER BY exit_time DESC LIMIT 1
            """, (coin,))
            row = cur.fetchone()
            if row and row[0]:
                last_loss = row[0]
                if hasattr(last_loss, 'tzinfo') and last_loss.tzinfo is None:
                    last_loss = last_loss.replace(tzinfo=timezone.utc)
                hours_since = (now_utc() - last_loss).total_seconds() / 3600
                return hours_since < COIN_COOLDOWN_HOURS
    except Exception: pass
    return False


def is_coin_blacklisted(conn, symbol: str) -> bool:
    """
    Blacklist: win rate < drempel na minimum trades.
    Check ook coach blacklist in bot_state.
    """
    coin = symbol.replace("USDT","").replace("BUSD","")

    # Check coach blacklist eerst
    try:
        bl_raw = get_bot_state_value(conn, "coin_blacklist", "[]")
        bl = json.loads(bl_raw)
        if coin in bl or symbol in bl:
            return True
    except Exception: pass

    # Check experience data
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COUNT(*), COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')
            FROM public.experience_trades
            WHERE UPPER(COALESCE(coin,'')) = UPPER(%s)
              AND UPPER(COALESCE(source,'')) IN ('REAL','LIVE','SIM','SHADOW')
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
            """, (coin,))
            row = cur.fetchone()
            if row:
                n, wins = safe_int(row[0]), safe_int(row[1])
                if n >= BLACKLIST_MIN_TRADES:
                    return (wins / n) < BLACKLIST_MAX_WINRATE
    except Exception: pass
    return False


def is_coin_whitelisted(conn, symbol: str) -> bool:
    """
    Whitelist: coin heeft historisch hoge win rate.
    Whitelisted coins krijgen een score bonus.
    """
    coin = symbol.replace("USDT","").replace("BUSD","")
    try:
        wl_raw = get_bot_state_value(conn, "coin_whitelist", "[]")
        wl = json.loads(wl_raw)
        return coin in wl or symbol in wl
    except Exception: return False


def get_prebuy_count_today(conn) -> int:
    """Telt pre-buy signals van vandaag."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COUNT(*) FROM public.pending_approvals
            WHERE DATE(aangemaakt AT TIME ZONE 'UTC') = CURRENT_DATE
            """)
            row = cur.fetchone()
            return safe_int(row[0]) if row else 0
    except Exception: return 0


def get_open_live_count(conn) -> int:
    """Aantal open live trades."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COUNT(*) FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND UPPER(COALESCE(outcome,'OPEN')) NOT IN ('WIN','LOSS','CANCELLED')
            """)
            return safe_int((cur.fetchone() or [0])[0])
    except Exception: return 0


def get_daily_trade_count(conn) -> int:
    """Live trades van vandaag."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COUNT(*) FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND DATE(COALESCE(entry_time, created_at) AT TIME ZONE 'UTC') = CURRENT_DATE
            """)
            return safe_int((cur.fetchone() or [0])[0])
    except Exception: return 0


def get_daily_pnl(conn) -> float:
    """Dagelijkse PnL in EUR."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COALESCE(SUM(
                CASE WHEN UPPER(outcome)='WIN' THEN ABS(COALESCE(pnl_eur,0))
                     WHEN UPPER(outcome)='LOSS' THEN -ABS(COALESCE(pnl_eur,0))
                     ELSE 0 END), 0)
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND DATE(COALESCE(exit_time, updated_at) AT TIME ZONE 'UTC') = CURRENT_DATE
            """)
            return safe_float((cur.fetchone() or [0])[0])
    except Exception: return 0.0


def symbol_already_pending(conn, symbol: str) -> bool:
    """Controleert of coin al een actief pending signaal heeft."""
    coin = symbol.replace("USDT","").replace("BUSD","")
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT 1 FROM public.pending_approvals
            WHERE (UPPER(COALESCE(coin,''))=UPPER(%s) OR UPPER(COALESCE(symbol,''))=UPPER(%s))
              AND UPPER(COALESCE(status,'PENDING')) IN ('PENDING','APPROVED')
              AND (expires_at IS NULL OR expires_at > NOW())
            LIMIT 1
            """, (coin, symbol))
            return cur.fetchone() is not None
    except Exception: return False


# ============================================================
# PRE-BUY AANMAKEN EN AUTO BUY
# ============================================================
def zorg_voor_pending_tabel(conn) -> None:
    """Maakt pending_approvals tabel aan als die niet bestaat."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS public.pending_approvals (
                id             TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
                coin           TEXT,
                symbol         TEXT,
                setup_type     TEXT,
                timeframe      TEXT DEFAULT '4H',
                regime         TEXT,
                btc_regime     TEXT,
                score          DOUBLE PRECISION,
                label          TEXT DEFAULT 'GO',
                entry          DOUBLE PRECISION,
                stop           DOUBLE PRECISION,
                target         DOUBLE PRECISION,
                rr_ratio       DOUBLE PRECISION,
                expires_at     TIMESTAMPTZ,
                raw_score      DOUBLE PRECISION,
                chance         DOUBLE PRECISION,
                confidence     DOUBLE PRECISION,
                bitvavo_market TEXT,
                exp_n          INTEGER DEFAULT 0,
                exp_win_rate   DOUBLE PRECISION DEFAULT 0.5,
                exp_bias       TEXT DEFAULT 'NEUTRAL',
                why_tag        TEXT,
                claude_beoordeling TEXT,
                aangemaakt     TIMESTAMPTZ DEFAULT NOW(),
                updated_at     TIMESTAMPTZ DEFAULT NOW(),
                status         TEXT DEFAULT 'PENDING',
                gebruikt_op    TIMESTAMPTZ,
                score_details  JSONB
            );
            CREATE INDEX IF NOT EXISTS idx_pending_status
                ON public.pending_approvals(status, aangemaakt);
            CREATE INDEX IF NOT EXISTS idx_pending_coin
                ON public.pending_approvals(coin, aangemaakt);
            """)
        conn.commit()
    except Exception as e: log(f"Tabel check: {e}")


def insert_pending(conn, prebuy: Dict) -> str:
    """Voegt Pre-BUY signaal in pending_approvals."""
    prebuy_id  = prebuy.get("id") or str(uuid.uuid4())
    coin       = prebuy["symbol"].replace("USDT","").replace("BUSD","")
    expires_at = now_utc() + timedelta(hours=PREBUY_EXPIRY_HOURS)
    rr = 0.0
    if prebuy.get("entry") and prebuy.get("stop") and prebuy.get("target"):
        risico = prebuy["entry"] - prebuy["stop"]
        winst  = prebuy["target"] - prebuy["entry"]
        rr     = round(winst / max(risico, 1e-10), 2)

    try:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO public.pending_approvals (
                id, coin, symbol, setup_type, timeframe, regime, btc_regime,
                score, label, entry, stop, target, rr_ratio, expires_at,
                raw_score, chance, confidence, bitvavo_market,
                exp_n, exp_win_rate, exp_bias, why_tag, claude_beoordeling,
                aangemaakt, status, score_details
            )
            VALUES (
                %s,%s,%s,%s,'4H',%s,%s,
                %s,'GO',%s,%s,%s,%s,%s,
                %s,%s,%s,%s,
                %s,%s,%s,%s,%s,
                NOW(),'PENDING',%s::jsonb
            )
            ON CONFLICT (id) DO UPDATE SET
                score=EXCLUDED.score, expires_at=EXCLUDED.expires_at,
                status='PENDING', updated_at=NOW()
            """, (
                prebuy_id, coin, prebuy["symbol"],
                prebuy["setup_type"], prebuy["regime"],
                prebuy.get("btc_regime","UNKNOWN"),
                prebuy["score"], prebuy["entry"],
                prebuy["stop"], prebuy["target"], rr, expires_at,
                prebuy["score"], prebuy["chance"], prebuy["confidence"],
                prebuy.get("bitvavo_market",""),
                prebuy.get("exp_n", 0), prebuy.get("exp_win_rate", 0.5),
                prebuy.get("exp_bias","NEUTRAL"), prebuy.get("why_tag",""),
                prebuy.get("claude_beoordeling",""),
                json.dumps(prebuy.get("score_details",{})),
            ))
        conn.commit()
        log(f"Pre-BUY: {prebuy['symbol']} score={prebuy['score']} id={prebuy_id[:8]}")
        return prebuy_id
    except Exception as e:
        log(f"insert_pending fout ({prebuy['symbol']}): {e}")
        conn.rollback()
        return ""


def trigger_auto_buy(prebuy_id: str) -> bool:
    """Triggert /auto_buy op whatsapp_webhook.py."""
    if not WEBHOOK_BASE_URL:
        log("WEBHOOK_BASE_URL niet ingesteld")
        return False
    try:
        resp = requests.post(
            f"{WEBHOOK_BASE_URL}/auto_buy",
            headers={"X-Bot-Auth": BOT_INTERNAL_SECRET},
            json={"prebuy_id": prebuy_id},
            timeout=20,
        )
        if resp.ok:
            log(f"Auto BUY getriggerd: {prebuy_id[:8]}")
            return True
        log(f"Auto BUY fout: {resp.status_code}: {resp.text[:100]}")
        return False
    except Exception as e:
        log(f"Auto BUY exception: {e}")
        return False


# ============================================================
# HOOFD SCAN LOOP
# ============================================================
def scan_universe(conn, drempels: Dict) -> int:
    """
    Scant alle Bitvavo-tradable coins via Binance data.
    Geeft aantal gegenereerde pre-buys terug.
    """
    # ── Checks voor we beginnen ──────────────────────────
    if not is_bot_active(conn):
        log("Bot gestopt -- geen scans")
        return 0

    if is_bot_paused(conn):
        log("Bot gepauzeerd -- geen scans")
        return 0

    if not is_trading_hours():
        log(f"Buiten trading hours ({TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC)")
        return 0

    # Dagbudget check
    dagpnl = get_daily_pnl(conn)
    if dagpnl <= -DAILY_STOP_LOSS_EUR:
        log(f"Dagbudget bereikt: {dagpnl:.4f} / -{DAILY_STOP_LOSS_EUR}")
        return 0

    # Limieten check
    dag_trades  = get_daily_trade_count(conn)
    open_count  = get_open_live_count(conn)
    prebuy_today = get_prebuy_count_today(conn)

    if dag_trades >= MAX_REAL_TRADES_PER_DAY:
        log(f"Daglimiet trades: {dag_trades}/{MAX_REAL_TRADES_PER_DAY}")
        return 0
    if open_count >= MAX_OPEN_REAL_TRADES:
        log(f"Open trades limiet: {open_count}/{MAX_OPEN_REAL_TRADES}")
        return 0
    if prebuy_today >= MAX_PREBUY_PER_DAY:
        log(f"Pre-buy daglimiet: {prebuy_today}/{MAX_PREBUY_PER_DAY}")
        return 0

    # ── BTC regime ───────────────────────────────────────
    btc_regime  = get_btc_regime(conn)
    btc_sterkte = get_btc_sterkte(conn)
    log(f"BTC regime: {btc_regime} ({btc_sterkte:.0f}%)")

    if btc_regime == "BEAR" and BTC_SKIP_BEAR:
        log("BTC BEAR -- scans overgeslagen")
        set_bot_state_value(conn, "scanner_actief", "false")
        set_bot_state_value(conn, "laatste_scan_reden", "BTC BEAR")
        return 0

    # ── Bitvavo markets ───────────────────────────────────
    tradable = get_tradable_markets()
    if not tradable:
        log("Geen tradable markets")
        return 0

    scan_pairs: List[Tuple[str, str]] = []
    for market in tradable:
        if market.endswith("-EUR"):
            base = market[:-4]
            scan_pairs.append((f"{base}USDT", market))

    log(f"Scannen: {len(scan_pairs)} pairs | score drempel: {drempels['min_score']}")

    # ── Scan loop ─────────────────────────────────────────
    min_score = drempels.get("min_score", MIN_SCORE_TO_TRADE)
    min_chance = drempels.get("min_chance", MIN_CHANCE)
    min_conf   = drempels.get("min_confidence", MIN_CONFIDENCE)

    prebuy_count = 0

    for idx, (symbol_usdt, bitvavo_market) in enumerate(scan_pairs):
        # Skip BTC zelf (te duur voor kleine inzet)
        if symbol_usdt in ("BTCUSDT", "BTCBUSD"):
            tel_filter("BTC_skip")
            continue

        # ── Coin filters ──────────────────────────────
        if is_coin_blacklisted(conn, symbol_usdt):
            tel_filter("blacklist"); update_sessie(symbol_usdt, 0, "blacklist"); continue
        if is_coin_on_cooldown(conn, symbol_usdt):
            tel_filter("cooldown"); update_sessie(symbol_usdt, 0, "cooldown"); continue
        if symbol_already_pending(conn, symbol_usdt):
            tel_filter("al_pending"); update_sessie(symbol_usdt, 0, "al_pending"); continue

        # ── Candles ophalen ───────────────────────────
        candles_4h = fetch_candles(symbol_usdt, "4h", 120)
        if len(candles_4h) < 30:
            tel_filter("geen_candles"); update_sessie(symbol_usdt, 0, "geen_candles"); continue

        candles_1h = fetch_candles(symbol_usdt, "1h", 60)
        closes_4h  = [c["close"] for c in candles_4h]
        current    = closes_4h[-1]

        if current <= 0:
            continue

        # ── Volatiliteit check ────────────────────────
        vol_label, atr_pct = detect_volatiliteit(candles_4h, current)
        if vol_label == "LAAG":
            tel_filter("vol_laag"); update_sessie(symbol_usdt, 0, "vol_laag"); continue

        # ── Coin regime ───────────────────────────────
        coin_regime = detect_coin_regime(closes_4h)
        if coin_regime == "BEAR" and btc_regime != "BULL":
            tel_filter("coin_bear"); update_sessie(symbol_usdt, 0, "coin_bear"); continue

        # ── Setup detectie ────────────────────────────
        setup_type, why_base = detect_setup_type(candles_4h, candles_1h)
        if setup_type == "UNKNOWN":
            tel_filter("geen_setup"); update_sessie(symbol_usdt, 0, "geen_setup"); continue

        # ── Experience ophalen ────────────────────────
        exp_win_rate, exp_n, exp_bias = get_experience(
            conn, symbol_usdt, setup_type, coin_regime
        )

        # ── Ticker voor volume data ───────────────────
        ticker = fetch_ticker_24h(symbol_usdt)

        # ── Score berekening ──────────────────────────
        score, chance, confidence, why_tag, rsi_4h, vol_ratio = calculate_score(
            candles_4h, candles_1h, ticker, coin_regime, btc_regime,
            btc_sterkte, setup_type, exp_win_rate, exp_n, drempels
        )

        # Whitelist bonus
        if is_coin_whitelisted(conn, symbol_usdt):
            score = min(score + 5, 100)
            why_tag += " | WHITELIST"

        update_sessie(symbol_usdt, score)

        # ── Score drempel check ───────────────────────
        if score < min_score:
            tel_filter("score_laag"); continue
        if chance < min_chance:
            tel_filter("chance_laag"); continue
        if confidence < min_conf:
            tel_filter("conf_laag"); continue

        log(f"SIGNAAL {symbol_usdt}: score={score} chance={chance}% "
            f"conf={confidence}% setup={setup_type} regime={coin_regime}")

        # ── ATR-based stop en target ──────────────────
        atr_eff = drempels.get("atr_multiplier", ATR_MULTIPLIER)
        tgt_eff = drempels.get("atr_target_r", ATR_TARGET_R)
        atr_val = atr_calc(candles_4h, ATR_PERIOD)

        if atr_val and atr_val > 0:
            stop   = current - atr_val * atr_eff
            target = current + atr_val * atr_eff * tgt_eff
        else:
            stop   = current * 0.98
            target = current * 1.04
        stop = max(stop, current * 0.94)

        # Support / weerstand check
        support, weerstand = detecteer_support_weerstand(candles_4h, 20)
        if support > 0 and stop < support * 0.98:
            stop = support * 0.99  # Stop net onder support
        if weerstand > 0 and target > weerstand * 1.05:
            target = weerstand * 0.99  # Target net onder weerstand

        # ── Claude beoordeling ────────────────────────
        claude_txt = claude_beoordeel_signaal(
            symbol_usdt, setup_type, coin_regime, btc_regime,
            score, chance, confidence, rsi_4h, vol_ratio,
            exp_win_rate, exp_n, why_tag,
        )

        # Coin statistieken ophalen voor score_details
        coin_stats = get_coin_statistieken(conn, symbol_usdt)

        # ── Pre-BUY aanmaken ──────────────────────────
        prebuy = {
            "id":             str(uuid.uuid4()),
            "symbol":         symbol_usdt,
            "setup_type":     setup_type,
            "regime":         coin_regime,
            "btc_regime":     btc_regime,
            "score":          score,
            "chance":         chance,
            "confidence":     confidence,
            "entry":          current,
            "stop":           stop,
            "target":         target,
            "bitvavo_market": bitvavo_market,
            "exp_n":          exp_n,
            "exp_win_rate":   exp_win_rate,
            "exp_bias":       exp_bias,
            "why_tag":        why_tag,
            "claude_beoordeling": claude_txt,
            "score_details": {
                "rsi_4h":       rsi_4h,
                "vol_ratio":    vol_ratio,
                "atr_pct":      round(atr_pct * 100, 2),
                "vol_label":    vol_label,
                "coin_stats":   coin_stats,
                "why_base":     why_base,
                "btc_sterkte":  btc_sterkte,
            },
        }

        prebuy_id = insert_pending(conn, prebuy)
        if prebuy_id:
            prebuy_count += 1
            prebuy_today += 1

            # Auto BUY triggeren
            if btc_regime != "BEAR" or not BTC_SKIP_BEAR:
                trigger_auto_buy(prebuy_id)
            else:
                log(f"{symbol_usdt} -- Pre-BUY aangemaakt, geen auto_buy (BTC BEAR)")

        if prebuy_today >= MAX_PREBUY_PER_DAY:
            log(f"Pre-buy daglimiet bereikt: {prebuy_today}")
            break

        log_sessie_voortgang(idx + 1, len(scan_pairs))

    log(f"Scan klaar: {_SESSIE['gescand']} gescand | {prebuy_count} pre-buys")
    return prebuy_count


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    log("=" * 60)
    log(f"Multi Coin Scorer v2.0 -- {now_utc().strftime('%Y-%m-%d %H:%M UTC')}")
    log("=" * 60)
    log(f"Database:       {'OK' if DATABASE_URL else 'ONTBREEKT'}")
    log(f"Webhook URL:    {'OK' if WEBHOOK_BASE_URL else 'niet ingesteld'}")
    log(f"Claude API:     {'OK' if ANTHROPIC_API_KEY else 'niet ingesteld'}")
    log(f"Min score:      {MIN_SCORE_TO_TRADE}")
    log(f"Min chance:     {MIN_CHANCE}%  | Min conf: {MIN_CONFIDENCE}%")
    log(f"ATR:            {ATR_PERIOD} periodes | x{ATR_MULTIPLIER} stop | x{ATR_TARGET_R} target")
    log(f"Fee+slippage:   {TOTAL_COST_PCT*100:.2f}%")
    log(f"BTC skip BEAR:  {BTC_SKIP_BEAR}")
    log(f"Trading hours:  {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC")
    log(f"Cooldown:       {COIN_COOLDOWN_HOURS}u na verlies")
    log("=" * 60)

    if not DATABASE_URL:
        log("DATABASE_URL ontbreekt")
        sys.exit(1)

    # Claude health check
    if ANTHROPIC_API_KEY:
        log("Claude scanner health check...")
        health = claude_scanner_health_check()
        if health:
            log(f"Claude: {health}")

    try:
        conn = db_connect()
        log("Database verbonden")

        # Pending tabel aanmaken als nodig
        zorg_voor_pending_tabel(conn)

        # Coach drempels ophalen (adaptieve parameters)
        drempels = haal_coach_drempels_op(conn)
        log(f"Coach drempels: score>={drempels['min_score']} ATR={drempels['atr_multiplier']}")

        # Sessie initialiseren
        init_sessie()

        # BTC check
        btc = get_btc_regime(conn)
        log(f"BTC regime: {btc}")

        # Bitvavo check
        markets = get_tradable_markets()
        log(f"Bitvavo markets: {len(markets)}")

        # Hoofd scan
        n = scan_universe(conn, drempels)
        log(f"Resultaat: {n} pre-buys gegenereerd")

        # Sessie opslaan in DB
        sla_sessie_op(conn)

        # Claude sessie analyse
        if ANTHROPIC_API_KEY and _SESSIE["gescand"] > 10:
            analyse = claude_analyseer_sessie(_SESSIE)
            if analyse:
                log(f"Claude sessie: {analyse}")
                set_bot_state_value(conn, "laatste_scan_claude", analyse)

        # Marktbeoordeling
        if ANTHROPIC_API_KEY:
            markt = claude_beoordeel_marktomstandigheden(
                btc, n, _SESSIE["gescand"], now_utc().hour
            )
            if markt:
                log(f"Claude markt: {markt}")

        # WhatsApp bij meerdere signalen
        if n >= 3:
            top5 = sorted(_SESSIE["coins_met_score"],
                          key=lambda x: x["score"], reverse=True)[:5]
            bericht = (
                f"Scanner: {n} signalen\n"
                f"{chr(10).join(c.get('symbol','') + ' score=' + str(c.get('score',0)) for c in top5)}"
            )
            send_whatsapp(bericht)

        set_bot_state_value(conn, "scanner_actief", "true")
        conn.close()
        log("Scanner klaar")

    except KeyboardInterrupt:
        log("Scanner gestopt door gebruiker")
        sys.exit(0)
    except Exception as e:
        report_error(e, "__main__", severity="KRITIEK")
        sys.exit(1)
