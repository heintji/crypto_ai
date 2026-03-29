# analysis/multi_coin_score.py
# ============================================================
# Crypto AI Bot — Multi Coin Scorer v3.0  (VOLLEDIG)
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
#   - Coach geheugen integratie (adaptieve drempels)
#   - Scan sessie statistieken (naar DB + WhatsApp)
#   - Regime-afhankelijke score drempel  [v3.0]
#   - Funding rate filter                [v3.0]
#   - VWAP positie scoring               [v3.0]
#   - Stochastic RSI bevestiging         [v3.0]
#   - RSI divergentie detectie           [v3.0]
#   - VWAP_BOUNCE + BULLISH_DIV setups   [v3.0]
#   - Weekend live filter                [v3.0]
#   - Dagelijks WhatsApp rapport 08:00   [v3.0]
#   - Scanner sessies naar DB tabel      [v3.0]
#   - Uitgebreide health monitoring      [v3.0]
#   - BTC trend richting detectie        [v3.0]
#   - Verlopen pending cleanup           [v3.0]
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
#   -> Claude AI analyseert elk signaal + fouten + sessie
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
import traceback
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

# ── v3.0: Regime-afhankelijke score drempels ──────────────
# BTC BULL  = markt werkt mee  = lagere drempel
# BTC RANGE = neutraal         = standaard drempel
# BTC BEAR  = gevaarlijk       = hogere drempel (shadow only)
SCORE_DREMPEL_BULL  = int(os.getenv("SCORE_DREMPEL_BULL")  or "88")
SCORE_DREMPEL_RANGE = int(os.getenv("SCORE_DREMPEL_RANGE") or "92")
SCORE_DREMPEL_BEAR  = int(os.getenv("SCORE_DREMPEL_BEAR")  or "99")

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
BINANCE_FAPI    = "https://fapi.binance.com/fapi/v1"   # v3.0: futures voor funding rate
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
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT") or "0.08")
MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT") or "0.005")

# ── v3.0: Funding rate filter ─────────────────────────────
# Extreme funding = te veel longs/shorts = squeeze risico
MAX_FUNDING_RATE = float(os.getenv("MAX_FUNDING_RATE") or "0.001")    # +0.1% max
MIN_FUNDING_RATE = float(os.getenv("MIN_FUNDING_RATE") or "-0.001")   # -0.1% min

# ── v3.0: Overige filters ─────────────────────────────────
MAX_SPREAD_PCT   = float(os.getenv("MAX_SPREAD_PCT") or "0.5")
SKIP_WEEKEND     = os.getenv("SKIP_WEEKEND_LIVE", "0").strip() == "1"
RAPPORT_HOUR_UTC = int(os.getenv("RAPPORT_HOUR_UTC") or "8")

# Caches
_MARKETS_CACHE: Dict[str, Any] = {"ts": 0.0, "markets": set()}
_MARKETS_TTL   = 30 * 60
_FUNDING_CACHE: Dict[str, Tuple[float, float]] = {}
_FUNDING_TTL   = 60 * 60

# Scan sessie statistieken (in-memory, reset per run)
_SESSIE: Dict[str, Any] = {
    "start":           None,
    "gescand":         0,
    "signalen":        0,
    "gefilterd":       {},
    "beste_score":     0,
    "beste_coin":      "",
    "coins_met_score": [],
    "fouten":          0,
    "live_trades":     0,
    "shadow_trades":   0,
    "btc_regime":      "UNKNOWN",
    "score_drempel":   MIN_SCORE_TO_TRADE,
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

def pct_str(val: float) -> str:
    return f"{val * 100:.1f}%"

def eur_str(val: float) -> str:
    sign = "+" if val >= 0 else ""
    return f"{sign}€{val:.2f}"


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
    """Rapporteert fouten via log + WhatsApp (bij KRITIEK/HOOG)."""
    tb = traceback.format_exc()[-300:]
    log(f"[{severity}] {function} ({symbol}): {type(error).__name__}: {error}")
    log(f"Traceback: {tb}")
    _SESSIE["fouten"] = _SESSIE.get("fouten", 0) + 1
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
        f"SCANNER FOUT — {severity}\n"
        f"Functie: {function}\nCoin: {symbol or 'n/a'}\n"
        f"Claude: {uitleg}\n\nCommands: STATUS | STOP"
    )


def claude_beoordeel_signaal(symbol: str, setup_type: str, regime: str,
                              btc_regime: str, score: int, chance: int,
                              confidence: int, rsi_4h: float, vol_ratio: float,
                              exp_win_rate: float, exp_n: int, why_tag: str,
                              funding_rate: float = 0.0) -> str:
    prompt = (
        f"Crypto trading bot signaal beoordeling in 2 zinnen Nederlands.\n"
        f"Coin:{symbol} Setup:{setup_type} Regime:{regime} BTC:{btc_regime}\n"
        f"Score:{score} Kans:{chance}% Conf:{confidence}% RSI:{rsi_4h:.1f} "
        f"Vol:{vol_ratio:.1f}x WR:{pct_str(exp_win_rate)}({exp_n} trades) "
        f"Funding:{funding_rate*100:.4f}%\n"
        f"Tags: {why_tag}\nIs dit een goed signaal en wat zijn de risicos?"
    )
    return _claude_analyse(prompt, 120)


def claude_scanner_health_check() -> str:
    prompt = (
        f"Check multi_coin_score.py v3.0 configuratie in 3 zinnen Nederlands.\n"
        f"DB:{'OK' if DATABASE_URL else 'ONTBREEKT'} "
        f"Webhook:{'OK' if WEBHOOK_BASE_URL else 'NIET_INGESTELD'} "
        f"Claude:{'OK' if ANTHROPIC_API_KEY else 'ONTBREEKT'} "
        f"MinScore:{MIN_SCORE_TO_TRADE} "
        f"BULL:{SCORE_DREMPEL_BULL}/RANGE:{SCORE_DREMPEL_RANGE}/BEAR:{SCORE_DREMPEL_BEAR} "
        f"ATR:{ATR_MULTIPLIER} Fee:{TOTAL_COST_PCT*100:.2f}% "
        f"Uren:{TRADING_HOURS_START}-{TRADING_HOURS_END}UTC "
        f"FundingMax:{MAX_FUNDING_RATE*100:.3f}%\n"
        f"Zijn er problemen of risicos?"
    )
    return _claude_analyse(prompt, 150)


def claude_analyseer_sessie(sessie: Dict) -> str:
    duur = (now_utc() - sessie["start"]).total_seconds() / 60 if sessie["start"] else 0
    filter_txt = ", ".join(f"{k}:{v}" for k, v in list(sessie["gefilterd"].items())[:8])
    prompt = (
        f"Analyseer deze crypto scanner sessie in 3 zinnen Nederlands.\n"
        f"Gescand:{sessie.get('gescand',0)} Signalen:{sessie.get('signalen',0)} "
        f"Duur:{duur:.1f}min Fouten:{sessie.get('fouten',0)}\n"
        f"Live:{sessie.get('live_trades',0)} Shadow:{sessie.get('shadow_trades',0)}\n"
        f"Beste coin:{sessie.get('beste_coin','')} score={sessie.get('beste_score',0)}\n"
        f"BTC:{sessie.get('btc_regime','?')} Drempel:{sessie.get('score_drempel',0)}\n"
        f"Filters: {filter_txt}\n"
        f"Wat valt op en zijn er aanbevelingen voor de volgende scan?"
    )
    return _claude_analyse(prompt, 200)


def claude_beoordeel_marktomstandigheden(btc_regime: str, n_signalen: int,
                                          n_gescand: int, uur: int,
                                          btc_sterkte: float = 50.0) -> str:
    conversie = round(n_signalen / max(n_gescand, 1) * 100, 1)
    prompt = (
        f"Beoordeel marktomstandigheden voor crypto bot in 2 zinnen Nederlands.\n"
        f"BTC regime:{btc_regime} Sterkte:{btc_sterkte:.1f}% Uur:{uur}:00 UTC\n"
        f"Scan: {n_signalen}/{n_gescand} signalen ({conversie}% conversie)\n"
        f"Is dit een goede tijd om te handelen?"
    )
    return _claude_analyse(prompt, 100)


def claude_dagrapport(n_live: int, n_shadow: int, dagpnl: float,
                       wins: int, losses: int, btc_regime: str,
                       top_coins: List[Dict]) -> str:
    """v3.0: Claude schrijft dagelijks WhatsApp rapport."""
    wr = wins / max(wins + losses, 1)
    top_txt = ", ".join(f"{c.get('symbol','?')}({c.get('score',0)})" for c in top_coins[:3])
    prompt = (
        f"Schrijf dagelijks crypto bot rapport in 4 zinnen Nederlands.\n"
        f"Datum: {utc_day_str()} UTC | BTC:{btc_regime}\n"
        f"Live:{n_live} Win:{wins} Loss:{losses} WR:{pct_str(wr)}\n"
        f"Shadow:{n_shadow} | PnL:{eur_str(dagpnl)}\n"
        f"Beste signalen: {top_txt}\n"
        f"Hoogtepunten en aanbevelingen voor morgen?"
    )
    return _claude_analyse(prompt, 250)


# ============================================================
# DATABASE
# ============================================================
def db_connect(retries: int = 3):
    """
    Verbindt met PostgreSQL. Retries bij fout. autocommit=False.
    FIX: geen retries en geen autocommit=False in vorige versie.
    """
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    for poging in range(1, retries + 1):
        try:
            conn = psycopg2.connect(
                DATABASE_URL,
                connect_timeout=8,
                options="-c statement_timeout=8000",
                sslmode="require",
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
    Zonder rollback blijft de transactie in ABORTED state en
    mislukken ALLE volgende queries.

    FIX: safe_rollback bestond niet in vorige versie.
    """
    try:
        conn.rollback()
    except Exception as e:
        log(f"Rollback fout (niet kritiek): {e}")


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
    except Exception as e: log(f"set_bot_state '{key}': {e}")

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

def get_consecutive_losses(conn) -> int:
    """v3.0: Telt aaneengesloten verliezen (live trades)."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT outcome FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND UPPER(COALESCE(outcome,'OPEN')) IN ('WIN','LOSS')
            ORDER BY COALESCE(exit_time, updated_at) DESC
            LIMIT 10
            """)
            rows = cur.fetchall()
            count = 0
            for row in rows:
                if safe_str(row[0]).upper() == "LOSS":
                    count += 1
                else:
                    break
            return count
    except Exception: return 0

def table_bestaat(conn, naam: str) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT 1 FROM information_schema.tables
            WHERE table_schema='public' AND table_name=%s
            """, (naam,))
            return cur.fetchone() is not None
    except Exception: return False


# ============================================================
# COACH GEHEUGEN INTEGRATIE
# ============================================================
def haal_coach_drempels_op(conn) -> Dict[str, Any]:
    """
    Leest coach aanbevelingen uit bot_state en coach_memory.
    ai_coach.py schrijft optimale drempels op basis van historische data.
    v3.0: ook regime-afhankelijke drempels worden gelezen.
    """
    drempels = {
        "min_score":       MIN_SCORE_TO_TRADE,
        "min_chance":      MIN_CHANCE,
        "min_confidence":  MIN_CONFIDENCE,
        "atr_multiplier":  ATR_MULTIPLIER,
        "atr_target_r":    ATR_TARGET_R,
        "rsi_min":         RSI_MIN,
        "rsi_max":         RSI_MAX,
        "score_bull":      SCORE_DREMPEL_BULL,
        "score_range":     SCORE_DREMPEL_RANGE,
        "score_bear":      SCORE_DREMPEL_BEAR,
        "coach_suggesties": [],
    }
    try:
        for key, dest, conv, default in [
            ("min_score_to_trade",  "min_score",      safe_int,   MIN_SCORE_TO_TRADE),
            ("atr_multiplier",      "atr_multiplier", safe_float, ATR_MULTIPLIER),
            ("atr_target_r",        "atr_target_r",   safe_float, ATR_TARGET_R),
            ("rsi_min",             "rsi_min",        safe_int,   RSI_MIN),
            ("rsi_max",             "rsi_max",        safe_int,   RSI_MAX),
            ("score_drempel_bull",  "score_bull",     safe_int,   SCORE_DREMPEL_BULL),
            ("score_drempel_range", "score_range",    safe_int,   SCORE_DREMPEL_RANGE),
            ("score_drempel_bear",  "score_bear",     safe_int,   SCORE_DREMPEL_BEAR),
        ]:
            val = get_bot_state_value(conn, key, "")
            if val:
                drempels[dest] = conv(val, default)

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


def get_score_drempel_voor_regime(btc_regime: str, drempels: Dict) -> int:
    """
    v3.0: Regime-afhankelijke score drempel.
    BULL  = lagere drempel (markt werkt mee, meer kansen)
    RANGE = standaard drempel
    BEAR  = hogere drempel (risicovoller, alleen beste setups)
    """
    if btc_regime == "BULL":
        return drempels.get("score_bull", SCORE_DREMPEL_BULL)
    elif btc_regime == "BEAR":
        return drempels.get("score_bear", SCORE_DREMPEL_BEAR)
    else:
        return drempels.get("score_range", SCORE_DREMPEL_RANGE)


# ============================================================
# SCANNER SESSIE STATISTIEKEN
# ============================================================
def init_sessie() -> None:
    _SESSIE["start"]           = now_utc()
    _SESSIE["gescand"]         = 0
    _SESSIE["signalen"]        = 0
    _SESSIE["gefilterd"]       = {}
    _SESSIE["beste_score"]     = 0
    _SESSIE["beste_coin"]      = ""
    _SESSIE["coins_met_score"] = []
    _SESSIE["fouten"]          = 0
    _SESSIE["live_trades"]     = 0
    _SESSIE["shadow_trades"]   = 0
    _SESSIE["btc_regime"]      = "UNKNOWN"
    _SESSIE["score_drempel"]   = MIN_SCORE_TO_TRADE


def update_sessie(symbol: str, score: int, reden: Optional[str] = None) -> None:
    _SESSIE["gescand"] += 1
    if reden:
        tel_filter(reden)
    if score > 0:
        _SESSIE["coins_met_score"].append({"symbol": symbol, "score": score})
        if score > _SESSIE["beste_score"]:
            _SESSIE["beste_score"] = score
            _SESSIE["beste_coin"]  = symbol
    if score >= _SESSIE.get("score_drempel", MIN_SCORE_TO_TRADE):
        _SESSIE["signalen"] += 1


def sla_sessie_op(conn) -> None:
    """Slaat sessie statistieken op in bot_state voor het dashboard."""
    if not _SESSIE["start"]:
        return
    try:
        duur = (now_utc() - _SESSIE["start"]).total_seconds()
        data = {
            "tijdstip":      now_utc().isoformat(),
            "gescand":       _SESSIE["gescand"],
            "signalen":      _SESSIE["signalen"],
            "duur_sec":      round(duur, 1),
            "beste_coin":    _SESSIE["beste_coin"],
            "beste_score":   _SESSIE["beste_score"],
            "filters":       _SESSIE["gefilterd"],
            "fouten":        _SESSIE["fouten"],
            "live_trades":   _SESSIE["live_trades"],
            "shadow_trades": _SESSIE["shadow_trades"],
            "btc_regime":    _SESSIE["btc_regime"],
            "score_drempel": _SESSIE["score_drempel"],
            "top5": sorted(_SESSIE["coins_met_score"],
                           key=lambda x: x["score"], reverse=True)[:5],
        }
        set_bot_state_value(conn, "laatste_scan_sessie",   json.dumps(data))
        set_bot_state_value(conn, "laatste_scan_tijd",     now_utc().strftime("%Y-%m-%d %H:%M:%S UTC"))
        set_bot_state_value(conn, "laatste_scan_coins",    str(_SESSIE["gescand"]))
        set_bot_state_value(conn, "laatste_scan_signalen", str(_SESSIE["signalen"]))
        log(f"Sessie: {_SESSIE['gescand']} coins | {_SESSIE['signalen']} signalen | {duur:.0f}s")
    except Exception as e:
        log(f"Sessie opslaan fout: {e}")


def sla_sessie_naar_tabel(conn) -> None:
    """v3.0: Slaat sessie op in scanner_sessies tabel voor historische analyse."""
    if not _SESSIE["start"]:
        return
    try:
        duur = (now_utc() - _SESSIE["start"]).total_seconds()
        top5 = sorted(_SESSIE["coins_met_score"],
                      key=lambda x: x["score"], reverse=True)[:5]
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS public.scanner_sessies (
                id            SERIAL PRIMARY KEY,
                sessie_start  TIMESTAMPTZ,
                sessie_eind   TIMESTAMPTZ DEFAULT NOW(),
                gescand       INTEGER DEFAULT 0,
                signalen      INTEGER DEFAULT 0,
                duur_sec      DOUBLE PRECISION DEFAULT 0,
                beste_coin    TEXT,
                beste_score   INTEGER DEFAULT 0,
                btc_regime    TEXT,
                score_drempel INTEGER DEFAULT 0,
                live_trades   INTEGER DEFAULT 0,
                shadow_trades INTEGER DEFAULT 0,
                fouten        INTEGER DEFAULT 0,
                filters_json  JSONB,
                top5_json     JSONB,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            );
            """)
            cur.execute("""
            INSERT INTO public.scanner_sessies (
                sessie_start, sessie_eind, gescand, signalen, duur_sec,
                beste_coin, beste_score, btc_regime, score_drempel,
                live_trades, shadow_trades, fouten, filters_json, top5_json
            ) VALUES (%s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            """, (
                _SESSIE["start"].isoformat(),
                _SESSIE["gescand"], _SESSIE["signalen"], round(duur, 1),
                _SESSIE["beste_coin"], _SESSIE["beste_score"],
                _SESSIE["btc_regime"], _SESSIE["score_drempel"],
                _SESSIE["live_trades"], _SESSIE["shadow_trades"], _SESSIE["fouten"],
                json.dumps(_SESSIE["gefilterd"]), json.dumps(top5),
            ))
        conn.commit()
        log("Sessie opgeslagen in scanner_sessies tabel")
    except Exception as e:
        log(f"Sessie naar tabel fout: {e}")
        try: conn.rollback()
        except Exception: pass


def log_sessie_voortgang(n: int, totaal: int) -> None:
    if n % 20 == 0 and n > 0:
        pct = round(n / max(totaal, 1) * 100)
        log(f"Voortgang: {n}/{totaal} ({pct}%) | "
            f"{_SESSIE['signalen']} signalen | "
            f"beste: {_SESSIE['beste_coin']} score={_SESSIE['beste_score']}")


# ============================================================
# BITVAVO UNIVERSE FILTER
# ============================================================
def get_tradable_markets() -> Set[str]:
    """Haalt actieve Bitvavo EUR markets op. Cache: 30 min TTL."""
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
    s = safe_str(symbol_usdt).upper()
    if not s.endswith("USDT"): return None
    base = s[:-4]
    market = f"{base}-EUR"
    return market if market in get_tradable_markets() else None


# ============================================================
# BINANCE DATA
# ============================================================
def binance_get(endpoint: str, params: dict,
                base: str = BINANCE_BASE,
                retries: int = MAX_RETRIES) -> Optional[Any]:
    """Binance public API met retry en exponential backoff."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(f"{base}{endpoint}", params=params, timeout=BINANCE_TIMEOUT)
            if resp.ok:
                return resp.json()
            # 4xx = client error (ongeldige symbol etc.) → geen retry
            if 400 <= resp.status_code < 500:
                return None
            # 5xx = server error → retry heeft zin
            log(f"Binance {resp.status_code} ({endpoint}) poging {attempt}/{retries}")
        except requests.exceptions.Timeout:
            log(f"Binance timeout poging {attempt}/{retries}")
        except Exception as e:
            log(f"Binance fout poging {attempt}/{retries}: {e}")
        if attempt < retries:
            time.sleep(2 ** attempt)
    return None


def fetch_candles(symbol: str, interval: str = "4h", limit: int = 120) -> List[Dict]:
    """Haalt OHLCV candles op van Binance. Inclusief quote_volume voor VWAP."""
    time.sleep(BINANCE_SLEEP)
    data = binance_get("/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not data: return []
    candles = []
    for c in data:
        try:
            candles.append({
                "open":         safe_float(c[1]),
                "high":         safe_float(c[2]),
                "low":          safe_float(c[3]),
                "close":        safe_float(c[4]),
                "volume":       safe_float(c[5]),
                "ts":           safe_int(c[0]),
                "quote_volume": safe_float(c[7]),
                "trades":       safe_int(c[8]),
            })
        except Exception: continue
    return candles


def fetch_ticker_24h(symbol: str) -> Optional[Dict]:
    time.sleep(BINANCE_SLEEP)
    return binance_get("/ticker/24hr", {"symbol": symbol})


def fetch_order_book_spread(symbol: str) -> float:
    """Bid/ask spread als proxy voor liquiditeit. Geeft spread als % terug."""
    time.sleep(BINANCE_SLEEP)
    data = binance_get("/ticker/bookTicker", {"symbol": symbol})
    if not data: return 0.0
    try:
        bid = safe_float(data.get("bidPrice", 0))
        ask = safe_float(data.get("askPrice", 0))
        mid = (bid + ask) / 2
        if mid > 0:
            return round((ask - bid) / mid * 100, 4)
    except Exception: pass
    return 0.0


def fetch_funding_rate(symbol: str) -> float:
    """
    v3.0: Haalt huidige funding rate op van Binance Futures.
    Hoge positieve funding = te veel longs = short squeeze risico.
    Hoge negatieve funding = te veel shorts = long squeeze risico.
    Coins zonder futures geven 0.0 terug (geen filter van toepassing).
    Cache: 1 uur per coin.
    """
    now_ts = time.time()
    cached = _FUNDING_CACHE.get(symbol)
    if cached and (now_ts - cached[1]) < _FUNDING_TTL:
        return cached[0]
    try:
        time.sleep(BINANCE_SLEEP)
        data = binance_get("/fundingRate", {"symbol": symbol, "limit": 1},
                           base=BINANCE_FAPI)
        if data and isinstance(data, list) and len(data) > 0:
            rate = safe_float(data[0].get("fundingRate", 0.0))
            _FUNDING_CACHE[symbol] = (rate, now_ts)
            return rate
    except Exception as e:
        log(f"Funding rate fout ({symbol}): {e}")
    _FUNDING_CACHE[symbol] = (0.0, now_ts)
    return 0.0


def check_funding_rate_ok(symbol: str) -> Tuple[bool, float]:
    """v3.0: Controleert of funding rate binnen grenzen valt. Geeft (ok, rate) terug."""
    rate = fetch_funding_rate(symbol)
    ok = MIN_FUNDING_RATE <= rate <= MAX_FUNDING_RATE
    return ok, rate


# ============================================================
# TECHNISCHE INDICATOREN
# ============================================================
def rsi_wilder(closes: List[float], period: int = 14) -> Optional[float]:
    """Wilder RSI — identiek aan TradingView. Nauwkeuriger dan simpele RSI."""
    if len(closes) < period + 1: return None
    changes  = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains    = [max(c, 0.0) for c in changes]
    losses   = [max(-c, 0.0) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period: return None
    return sum(values[-period:]) / period


def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period: return None
    mult    = 2.0 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = v * mult + ema_val * (1 - mult)
    return ema_val


def atr_calc(candles: List[Dict], period: int = 14) -> Optional[float]:
    """ATR met Wilder smoothing. TR = max(H-L, |H-PC|, |L-PC|)."""
    if len(candles) < period + 1: return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period: return None
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return round(atr_val, 8)


def bollinger_bands(closes: List[float], period: int = 20,
                    num_std: float = 2.0) -> Tuple[float, float, float]:
    """Bollinger Bands: upper, middle, lower."""
    if len(closes) < period:
        c = closes[-1] if closes else 0.0
        return c, c, c
    recent = closes[-period:]
    mid    = sum(recent) / period
    std    = (sum((x - mid) ** 2 for x in recent) / period) ** 0.5
    return round(mid + num_std * std, 8), round(mid, 8), round(mid - num_std * std, 8)


def macd(closes: List[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> Tuple[float, float, float]:
    """MACD: (macd_line, signal_line, histogram). Extra trend bevestiging."""
    if len(closes) < slow + signal: return 0.0, 0.0, 0.0
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    if ema_fast is None or ema_slow is None: return 0.0, 0.0, 0.0
    macd_line = ema_fast - ema_slow
    macd_vals: List[float] = []
    for i in range(slow - 1, len(closes)):
        ef = ema(closes[:i+1], fast)
        es = ema(closes[:i+1], slow)
        if ef and es:
            macd_vals.append(ef - es)
    if len(macd_vals) < signal:
        return round(macd_line, 8), 0.0, round(macd_line, 8)
    signal_line = ema(macd_vals, signal) or 0.0
    return round(macd_line, 8), round(signal_line, 8), round(macd_line - signal_line, 8)


def stochastic_rsi(closes: List[float], rsi_period: int = 14,
                   stoch_period: int = 14) -> Optional[float]:
    """
    v3.0: Stochastic RSI — RSI van RSI.
    Oversold <20 = score bonus. Overbought >80 = score malus.
    """
    if len(closes) < rsi_period + stoch_period + 1: return None
    rsi_vals: List[float] = []
    for i in range(rsi_period, len(closes) + 1):
        r = rsi_wilder(closes[:i], rsi_period)
        if r is not None:
            rsi_vals.append(r)
    if len(rsi_vals) < stoch_period: return None
    recent_rsi = rsi_vals[-stoch_period:]
    rsi_min, rsi_max = min(recent_rsi), max(recent_rsi)
    if rsi_max == rsi_min: return 50.0
    return round((rsi_vals[-1] - rsi_min) / (rsi_max - rsi_min) * 100, 2)


def vwap(candles: List[Dict], periodes: int = 20) -> Optional[float]:
    """
    v3.0: Volume Weighted Average Price.
    Prijs boven VWAP = bullish. Onder VWAP = bearish.
    """
    recent = candles[-periodes:] if len(candles) >= periodes else candles
    if not recent: return None
    try:
        totaal_vol = 0.0
        totaal_pv  = 0.0
        for c in recent:
            typisch = (c["high"] + c["low"] + c["close"]) / 3
            vol     = c.get("quote_volume") or c["volume"]
            if vol > 0:
                totaal_pv  += typisch * vol
                totaal_vol += vol
        if totaal_vol > 0:
            return round(totaal_pv / totaal_vol, 8)
    except Exception: pass
    return None


def detect_coin_regime(closes: List[float]) -> str:
    """BULL / BEAR / RANGE op basis van SMA20 vs SMA50."""
    if len(closes) < 50: return "UNKNOWN"
    sma20 = sma(closes, 20)
    sma50 = sma(closes, 50)
    if sma20 is None or sma50 is None: return "UNKNOWN"
    diff_pct = abs(sma20 - sma50) / max(sma50, 1e-10)
    if diff_pct < 0.015: return "RANGE"
    return "BULL" if sma20 > sma50 else "BEAR"


def detect_volatiliteit(candles: List[Dict], prijs: float) -> Tuple[str, float]:
    """LAAG / NORMAAL / HOOG / EXTREEM op basis van ATR%."""
    atr = atr_calc(candles, ATR_PERIOD)
    if not atr or prijs <= 0: return "NORMAAL", 0.02
    atr_pct = atr / prijs
    if atr_pct < MIN_ATR_PCT:           return "LAAG",    atr_pct
    elif atr_pct > MAX_ATR_PCT:         return "EXTREEM", atr_pct
    elif atr_pct > MAX_ATR_PCT * 0.7:   return "HOOG",    atr_pct
    return "NORMAAL", atr_pct


def detecteer_support_weerstand(candles: List[Dict],
                                 lookback: int = 20) -> Tuple[float, float]:
    """Swing high/low als weerstand/support. Geeft (support, weerstand) terug."""
    if len(candles) < lookback:
        if candles:
            prijs = candles[-1]["close"]
            return prijs * 0.97, prijs * 1.03
        return 0.0, 0.0
    recente   = candles[-lookback:]
    weerstand = max(c["high"] for c in recente)
    support   = min(c["low"]  for c in recente)
    return round(support, 8), round(weerstand, 8)


def detecteer_momentum(closes: List[float], periode: int = 10) -> float:
    """(huidig - N periodes terug) / N periodes terug * 100."""
    if len(closes) < periode + 1: return 0.0
    oud    = closes[-(periode + 1)]
    huidig = closes[-1]
    if oud <= 0: return 0.0
    return round((huidig - oud) / oud * 100, 3)


def detecteer_squeeze(closes: List[float], candles: List[Dict]) -> bool:
    """Bollinger Band Squeeze = lage volatiliteit voor uitbraak."""
    if len(closes) < 20 or len(candles) < 20: return False
    bb_upper, bb_mid, bb_lower = bollinger_bands(closes, 20, 2.0)
    atr = atr_calc(candles, 14)
    if not atr: return False
    kc_upper = bb_mid + 1.5 * atr
    kc_lower = bb_mid - 1.5 * atr
    return bb_upper < kc_upper and bb_lower > kc_lower


def detecteer_divergentie(closes: List[float], candles: List[Dict],
                           lookback: int = 10) -> str:
    """
    v3.0: RSI divergentie detectie.
    BULLISH: prijs maakt lower low, RSI maakt higher low (koopkracht neemt toe).
    BEARISH: prijs maakt higher high, RSI maakt lower high.
    Geeft 'BULLISH' / 'BEARISH' / 'GEEN' terug.
    """
    if len(closes) < lookback + 15: return "GEEN"
    try:
        prijs_huidig = closes[-1]
        prijs_eerder = closes[-(lookback + 1)]
        rsi_huidig   = rsi_wilder(closes, 14) or 50.0
        rsi_eerder   = rsi_wilder(closes[:-(lookback)], 14)
        if rsi_eerder is None: return "GEEN"
        if prijs_huidig < prijs_eerder and rsi_huidig > rsi_eerder + 3:
            return "BULLISH"
        if prijs_huidig > prijs_eerder and rsi_huidig < rsi_eerder - 3:
            return "BEARISH"
    except Exception: pass
    return "GEEN"


def bereken_vwap_positie(candles: List[Dict], prijs: float) -> str:
    """v3.0: BOVEN / ONDER / OP / ONBEKEND t.o.v. VWAP."""
    vwap_val = vwap(candles, 20)
    if vwap_val is None or prijs <= 0: return "ONBEKEND"
    if prijs > vwap_val * 1.005:  return "BOVEN"
    elif prijs < vwap_val * 0.995: return "ONDER"
    return "OP"


# ============================================================
# BTC REGIME
# ============================================================
def get_btc_regime(conn) -> str:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT regime FROM public.btc_regime_4h ORDER BY open_time DESC LIMIT 1")
            row = cur.fetchone()
            return safe_str(row[0], "UNKNOWN") if row else "UNKNOWN"
    except Exception: return "UNKNOWN"


def get_btc_sterkte(conn) -> float:
    """Sterkte als abs((close-ema200)/ema200*100). Geen strength kolom in DB."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT close, ema200 FROM public.btc_regime_4h ORDER BY open_time DESC LIMIT 1")
            row = cur.fetchone()
            if row:
                _close, _ema200 = safe_float(row[0]), safe_float(row[1])
                return round(abs((_close - _ema200) / _ema200 * 100), 1) if _ema200 else 50.0
            return 50.0
    except Exception: return 50.0


def get_btc_trend_richting(conn) -> str:
    """
    v3.0: Detecteert of BTC regime verbetert of verslechtert.
    VERBETEREND / VERSLECHTEREND / STABIEL
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT regime FROM public.btc_regime_4h ORDER BY open_time DESC LIMIT 5")
            rows = cur.fetchall()
            if len(rows) < 2: return "STABIEL"
            volgorde = {"BEAR": 0, "RANGE": 1, "BULL": 2, "UNKNOWN": 1}
            huidig = volgorde.get(safe_str(rows[0][0]), 1)
            vorig  = volgorde.get(safe_str(rows[1][0]), 1)
            if huidig > vorig:   return "VERBETEREND"
            elif huidig < vorig: return "VERSLECHTEREND"
            return "STABIEL"
    except Exception: return "STABIEL"


# ============================================================
# SETUP TYPE DETECTIE
# ============================================================
def detect_setup_type(candles_4h: List[Dict],
                       candles_1h: List[Dict]) -> Tuple[str, str]:
    """
    Detecteert setup type op basis van 4H en 1H candles.
    Geeft (setup_type, why_tag) terug.

    Setup types (sterk naar zwak):
    1. SQUEEZE_BREAK:     uitbraak na Bollinger Band squeeze
    2. BREAKOUT:          uitbraak boven recente 20-candle swing high
    3. BULLISH_DIVERGENCE: RSI bullish divergentie met upturn   [v3.0]
    4. TREND_PULLBACK:    pullback naar SMA20 in uptrend
    5. VWAP_BOUNCE:       bounce terug boven VWAP               [v3.0]
    6. BOUNCE:            bounce van SMA50 support
    7. OVERSOLD_RECLAIM:  herstel na extreme oversold
    8. MOMENTUM:          sterk momentum met gezonde RSI
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

    # 1. SQUEEZE_BREAK
    if detecteer_squeeze(closes_4h, candles_4h):
        if current > vorige * 1.005:
            return "SQUEEZE_BREAK", f"squeeze|RSI={rsi_4h:.0f}"

    # 2. BREAKOUT — boven recente swing high
    if len(candles_4h) >= 20:
        high_20    = max(c["high"] for c in candles_4h[-20:])
        prev_close = closes_4h[-2] if len(closes_4h) > 1 else current
        if current > high_20 * 0.998 and prev_close < high_20:
            return "BREAKOUT", f"break_high20({high_20:.4f})|RSI={rsi_4h:.0f}"

    # 3. BULLISH_DIVERGENCE (v3.0)
    div = detecteer_divergentie(closes_4h, candles_4h, 10)
    if div == "BULLISH" and current > vorige:
        return "BULLISH_DIVERGENCE", f"bull_div|RSI={rsi_4h:.0f}"

    # 4. TREND_PULLBACK
    if sma20_4h > sma50_4h and current > sma50_4h:
        dist_sma20 = abs(current - sma20_4h) / max(sma20_4h, 1e-10)
        if dist_sma20 < 0.025 and RSI_MIN <= rsi_4h <= 58:
            return "TREND_PULLBACK", f"sma20_pb({dist_sma20*100:.1f}%)|RSI={rsi_4h:.0f}"

    # 5. VWAP_BOUNCE (v3.0)
    vwap_val = vwap(candles_4h, 20)
    if vwap_val and current > vwap_val and vorige < vwap_val and rsi_4h < 55:
        return "VWAP_BOUNCE", f"vwap_cross({vwap_val:.4f})|RSI={rsi_4h:.0f}"

    # 6. BOUNCE — van SMA50
    if sma50_4h > 0:
        dist_sma50 = abs(current - sma50_4h) / max(sma50_4h, 1e-10)
        if dist_sma50 < 0.02 and rsi_4h < 52:
            return "BOUNCE", f"sma50_bounce({dist_sma50*100:.1f}%)|RSI={rsi_4h:.0f}"

    # 7. OVERSOLD_RECLAIM
    if rsi_4h < 30 and current > vorige * 1.002:
        return "OVERSOLD_RECLAIM", f"oversold_bounce|RSI={rsi_4h:.0f}"

    # 8. MOMENTUM
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
                    exp_n: int, drempels: Dict,
                    vwap_positie: str = "ONBEKEND",
                    stoch_rsi_val: Optional[float] = None,
                    divergentie: str = "GEEN",
                    funding_rate: float = 0.0) -> Tuple[int, int, int, str, float, float]:
    """
    Berekent score (0-100), chance, confidence.

    BASIS (max 100):
      RSI ideale zone:        0-20 pt
      Trend alignment SMA:    0-20 pt
      Volume bevestiging:     0-15 pt
      Experience win rate:    0-20 pt
      BTC regime + sterkte:   0-15 pt
      Multi-timeframe 1H RSI: 0-10 pt

    BONUSSEN (v3.0 uitgebreid):
      MACD bullish:           +5 pt
      BB Squeeze:             +3 pt
      Momentum >5%:           +4 pt
      VWAP boven:             +3 pt  [v3.0]
      StochRSI <30:           +3 pt  [v3.0]
      Bullish divergentie:    +4 pt  [v3.0]

    MALUSSEN:
      Fee >0.3%:              -3 pt
      Vol EXTREEM:            -5 pt
      Vol LAAG:               -2 pt
      Funding extreem:        -5 pt  [v3.0]
      VWAP onder:             -3 pt  [v3.0]
      MACD bearish:           -3 pt
      Bearish divergentie:    -4 pt  [v3.0]
      Momentum <-5%:          -2 pt
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

    score: int = 0
    why_tags: List[str] = []

    # ── 1. RSI in ideale zone (0-20) ─────────────────────
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

    # ── 2. Trend alignment (0-20) ─────────────────────────
    if sma20_4h and sma50_4h:
        if sma20_4h > sma50_4h and current > sma20_4h:
            score += 20; why_tags.append("trend=BULL_BOVEN_SMA20")
        elif sma20_4h > sma50_4h and current > sma50_4h:
            score += 12; why_tags.append("trend=BULL_ONDER_SMA20")
        elif sma20_4h < sma50_4h:
            why_tags.append("trend=BEAR")
        else:
            score += 5; why_tags.append("trend=RANGE")
    else:
        why_tags.append("trend=onbekend")

    # ── 3. Volume (0-15) ──────────────────────────────────
    if vol_ratio >= 2.0:
        score += 15; why_tags.append(f"vol={vol_ratio:.1f}xHOOG")
    elif vol_ratio >= 1.3:
        score += 10; why_tags.append(f"vol={vol_ratio:.1f}xOK")
    elif vol_ratio >= 1.0:
        score += 6;  why_tags.append(f"vol={vol_ratio:.1f}xNORMAAL")
    else:
        why_tags.append(f"vol={vol_ratio:.1f}xLAAG")

    # ── 4. Experience win rate (0-20) ─────────────────────
    if exp_n >= 10:
        if exp_win_rate >= 0.65:
            score += 20; why_tags.append(f"exp={pct_str(exp_win_rate)}({exp_n})")
        elif exp_win_rate >= 0.55:
            score += 14; why_tags.append(f"exp={pct_str(exp_win_rate)}({exp_n})")
        elif exp_win_rate >= 0.45:
            score += 7;  why_tags.append(f"exp={pct_str(exp_win_rate)}({exp_n})")
        else:
            why_tags.append(f"exp={pct_str(exp_win_rate)}LAAG({exp_n})")
    elif exp_n >= 3:
        score += 10; why_tags.append(f"exp=weinig({exp_n})")
    else:
        score += 10; why_tags.append("exp=nieuw")

    # ── 5. BTC regime + sterkte (0-15) ────────────────────
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

    # ── 6. Multi-timeframe 1H RSI (0-10) ──────────────────
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

    # ── BONUSSEN ─────────────────────────────────────────
    macd_line, signal_line, _ = macd(closes_4h)
    if macd_line > signal_line and macd_line > 0:
        score = min(score + 5, 105); why_tags.append("MACD=bullish")
    elif macd_line < signal_line and macd_line < 0:
        score = max(score - 3, 0);   why_tags.append("MACD=bearish")

    if detecteer_squeeze(closes_4h, candles_4h):
        score = min(score + 3, 105); why_tags.append("SQUEEZE")

    mom = detecteer_momentum(closes_4h, 10)
    if mom > 5:
        score = min(score + 4, 105); why_tags.append(f"MOM=+{mom:.1f}%")
    elif mom < -5:
        score = max(score - 2, 0);   why_tags.append(f"MOM={mom:.1f}%")

    # v3.0 bonussen
    if vwap_positie == "BOVEN":
        score = min(score + 3, 105); why_tags.append("VWAP=boven")
    elif vwap_positie == "ONDER":
        score = max(score - 3, 0);   why_tags.append("VWAP=onder")

    if stoch_rsi_val is not None:
        if stoch_rsi_val < 30:
            score = min(score + 3, 105); why_tags.append(f"StochRSI={stoch_rsi_val:.0f}oversold")
        elif stoch_rsi_val > 80:
            score = max(score - 2, 0);   why_tags.append(f"StochRSI={stoch_rsi_val:.0f}overbought")

    if divergentie == "BULLISH":
        score = min(score + 4, 105); why_tags.append("DIV=bullish")
    elif divergentie == "BEARISH":
        score = max(score - 4, 0);   why_tags.append("DIV=bearish")

    # ── MALUSSEN ─────────────────────────────────────────
    fee_impact = TOTAL_COST_PCT * 100
    if fee_impact > 0.3:
        score = max(0, score - 3); why_tags.append(f"fee={fee_impact:.2f}%")

    vol_label, atr_pct = detect_volatiliteit(candles_4h, current)
    if vol_label == "EXTREEM":
        score = max(0, score - 5); why_tags.append(f"vol=EXTREEM({atr_pct*100:.1f}%)")
    elif vol_label == "LAAG":
        score = max(0, score - 2); why_tags.append("vol=LAAG")

    # v3.0 malus: extreme funding
    if abs(funding_rate) > MAX_FUNDING_RATE:
        score = max(0, score - 5); why_tags.append(f"funding=EXTREEM({funding_rate*100:.4f}%)")

    score = max(0, min(100, score))

    # ── Chance berekening ─────────────────────────────────
    if exp_n >= 10 and exp_win_rate > 0:
        chance = int(exp_win_rate * 100 * (score / 100) * 1.2)
    else:
        chance = int(score * 0.65)
    chance = max(0, min(100, chance))

    # ── Confidence berekening ─────────────────────────────
    if exp_n >= 100:   confidence = min(95, 70 + int(exp_win_rate * 25))
    elif exp_n >= 50:  confidence = min(85, 55 + int(exp_win_rate * 25))
    elif exp_n >= 20:  confidence = min(75, 45 + int(exp_win_rate * 20))
    elif exp_n >= 5:   confidence = min(65, 35 + int(exp_win_rate * 20))
    else:              confidence = 40

    why_tag = " | ".join(why_tags[:10])
    return score, chance, confidence, why_tag, rsi_4h, vol_ratio


# ============================================================
# EXPERIENCE SCOREBOARD
# ============================================================
def get_experience(conn, symbol: str, setup_type: str,
                   regime: str) -> Tuple[float, int, str]:
    """Haalt experience op. Primair: coin-specifiek. Fallback: setup-niveau."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COALESCE(win_rate,0.5), COALESCE(n,0), COALESCE(bias,'NEUTRAL')
            FROM public.experience_scoreboard
            WHERE symbol=%s AND setup_type=%s AND regime=%s LIMIT 1
            """, (symbol, setup_type, regime))
            row = cur.fetchone()
            if row:
                return safe_float(row[0]), safe_int(row[1]), safe_str(row[2], "NEUTRAL")
    except Exception: pass
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COALESCE(AVG(win_rate),0.5), COALESCE(SUM(n),0)
            FROM public.experience_scoreboard
            WHERE setup_type=%s AND regime=%s
            """, (setup_type, regime))
            row = cur.fetchone()
            if row and safe_int(row[1]) >= 5:
                return safe_float(row[0]), safe_int(row[1]), "NEUTRAL"
    except Exception: pass
    return 0.5, 0, "NEUTRAL"


def get_coin_statistieken(conn, symbol: str) -> Dict[str, Any]:
    """Uitgebreide coin statistieken uit experience_trades voor score_details."""
    stats = {"n_total": 0, "n_live": 0, "n_shadow": 0,
             "win_rate": 0.5, "gem_r": 0.0, "profit_factor": 1.0,
             "laatste_trade": None}
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COUNT(*) AS n,
                COUNT(*) FILTER (WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')) AS n_live,
                COUNT(*) FILTER (WHERE UPPER(COALESCE(source,'')) = 'SHADOW') AS n_shadow,
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
                stats["n_shadow"]      = safe_int(row[2])
                stats["win_rate"]      = safe_float(row[3], 0.5)
                stats["gem_r"]         = safe_float(row[4])
                stats["profit_factor"] = safe_float(row[5], 1.0)
                stats["laatste_trade"] = str(row[6]) if row[6] else None
    except Exception as e:
        log(f"Coin statistieken fout ({symbol}): {e}")
    return stats


# ============================================================
# COIN FILTERS
# ============================================================
def is_coin_on_cooldown(conn, symbol: str) -> bool:
    """48u cooldown na live verlies."""
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
                if hours_since < COIN_COOLDOWN_HOURS:
                    log(f"Cooldown {coin}: {hours_since:.0f}u/{COIN_COOLDOWN_HOURS:.0f}u")
                    return True
    except Exception: pass
    return False


def is_coin_blacklisted(conn, symbol: str) -> bool:
    """Blacklist: win rate < drempel na minimum trades. Check ook coach blacklist."""
    coin = symbol.replace("USDT","").replace("BUSD","")
    try:
        bl_raw = get_bot_state_value(conn, "coin_blacklist", "[]")
        bl = json.loads(bl_raw)
        if coin in bl or symbol in bl: return True
    except Exception: pass
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
                    wr = wins / n
                    if wr < BLACKLIST_MAX_WINRATE:
                        log(f"Blacklist {coin}: WR={pct_str(wr)} ({n} trades)")
                        return True
    except Exception: pass
    return False


def is_coin_whitelisted(conn, symbol: str) -> bool:
    """Whitelist: hoge win rate. Geeft +5 score bonus."""
    coin = symbol.replace("USDT","").replace("BUSD","")
    try:
        wl_raw = get_bot_state_value(conn, "coin_whitelist", "[]")
        wl = json.loads(wl_raw)
        return coin in wl or symbol in wl
    except Exception: return False


def get_prebuy_count_today(conn) -> int:
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COUNT(*) FROM public.pending_approvals
            WHERE DATE(created_at AT TIME ZONE 'UTC') = CURRENT_DATE
            """)
            row = cur.fetchone()
            return safe_int(row[0]) if row else 0
    except Exception: return 0


def get_open_live_count(conn) -> int:
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
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COALESCE(SUM(
                CASE WHEN UPPER(outcome)='WIN'  THEN  ABS(COALESCE(pnl_eur,0))
                     WHEN UPPER(outcome)='LOSS' THEN -ABS(COALESCE(pnl_eur,0))
                     ELSE 0 END), 0)
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND DATE(COALESCE(exit_time, updated_at) AT TIME ZONE 'UTC') = CURRENT_DATE
            """)
            return safe_float((cur.fetchone() or [0])[0])
    except Exception: return 0.0


def get_daily_win_loss(conn) -> Tuple[int, int]:
    """v3.0: Win en loss count van vandaag."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN'),
                COUNT(*) FILTER (WHERE UPPER(outcome)='LOSS')
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND DATE(COALESCE(exit_time, updated_at) AT TIME ZONE 'UTC') = CURRENT_DATE
            """)
            row = cur.fetchone()
            if row: return safe_int(row[0]), safe_int(row[1])
    except Exception: pass
    return 0, 0


def get_daily_shadow_count(conn) -> int:
    """v3.0: Shadow trades van vandaag."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COUNT(*) FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) = 'SHADOW'
              AND DATE(COALESCE(entry_time, created_at) AT TIME ZONE 'UTC') = CURRENT_DATE
            """)
            return safe_int((cur.fetchone() or [0])[0])
    except Exception: return 0


def get_top_signalen_vandaag(conn) -> List[Dict]:
    """v3.0: Beste signalen vandaag voor dagrapport."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT coin, symbol, score, setup_type, regime
            FROM public.pending_approvals
            WHERE DATE(created_at AT TIME ZONE 'UTC') = CURRENT_DATE
            ORDER BY score DESC LIMIT 5
            """)
            rows = cur.fetchall()
            return [{"coin": safe_str(r[0]), "symbol": safe_str(r[1]),
                     "score": safe_int(r[2]), "setup_type": safe_str(r[3]),
                     "regime": safe_str(r[4])} for r in (rows or [])]
    except Exception: return []


def symbol_already_pending(conn, symbol: str) -> bool:
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


def cleanup_verlopen_pending(conn) -> int:
    """v3.0: Markeert verlopen pending signalen als EXPIRED."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            UPDATE public.pending_approvals
            SET status='EXPIRED', updated_at=NOW()
            WHERE UPPER(status)='PENDING'
              AND expires_at IS NOT NULL
              AND expires_at < NOW()
            """)
            n = cur.rowcount
        conn.commit()
        if n > 0: log(f"{n} verlopen pending signalen gemarkeerd als EXPIRED")
        return n
    except Exception as e:
        safe_rollback(conn)
        log(f"Cleanup pending fout: {e}")
        return 0


# ============================================================
# PRE-BUY AANMAKEN EN AUTO BUY
# ============================================================
def zorg_voor_pending_tabel(conn) -> None:
    """Maakt pending_approvals tabel aan als die niet bestaat. v3.0: extra kolommen."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS public.pending_approvals (
                id                 TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
                coin               TEXT,
                symbol             TEXT,
                setup_type         TEXT,
                timeframe          TEXT DEFAULT '4H',
                regime             TEXT,
                btc_regime         TEXT,
                score              DOUBLE PRECISION,
                label              TEXT DEFAULT 'GO',
                entry              DOUBLE PRECISION,
                stop               DOUBLE PRECISION,
                target             DOUBLE PRECISION,
                rr_ratio           DOUBLE PRECISION,
                expires_at         TIMESTAMPTZ,
                raw_score          DOUBLE PRECISION,
                chance             DOUBLE PRECISION,
                confidence         DOUBLE PRECISION,
                bitvavo_market     TEXT,
                exp_n              INTEGER DEFAULT 0,
                exp_win_rate       DOUBLE PRECISION DEFAULT 0.5,
                exp_bias           TEXT DEFAULT 'NEUTRAL',
                why_tag            TEXT,
                claude_beoordeling TEXT,
                created_at         TIMESTAMPTZ DEFAULT NOW(),
                updated_at         TIMESTAMPTZ DEFAULT NOW(),
                status             TEXT DEFAULT 'PENDING',
                gebruikt_op        TIMESTAMPTZ,
                score_details      JSONB,
                vwap_positie       TEXT,
                divergentie        TEXT,
                funding_rate       DOUBLE PRECISION DEFAULT 0.0,
                live_toegestaan    BOOLEAN DEFAULT FALSE
            );
            CREATE INDEX IF NOT EXISTS idx_pending_status
                ON public.pending_approvals(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_pending_coin
                ON public.pending_approvals(coin, created_at);
            CREATE INDEX IF NOT EXISTS idx_pending_expires
                ON public.pending_approvals(expires_at) WHERE status='PENDING';
            """)
        conn.commit()
    except Exception as e:
        # KRITIEK: rollback zodat cleanup_verlopen_pending niet ook crasht
        # met "current transaction is aborted"
        safe_rollback(conn)
        log(f"Tabel check fout: {e}")

    # Migraties — voeg ontbrekende kolommen toe aan bestaande tabel
    # ADD COLUMN IF NOT EXISTS is veilig: doet niets als kolom al bestaat.
    # FIX: zonder migraties crasht insert_pending als tabel al bestond
    # zonder de nieuwe v3.0 kolommen.
    try:
        migraties = [
            ("score_details",   "JSONB"),
            ("vwap_positie",    "TEXT"),
            ("divergentie",     "TEXT"),
            ("funding_rate",    "DOUBLE PRECISION DEFAULT 0.0"),
            ("live_toegestaan", "BOOLEAN DEFAULT FALSE"),
            ("updated_at",      "TIMESTAMPTZ DEFAULT NOW()"),
            ("btc_regime",      "TEXT"),
            ("rr_ratio",        "DOUBLE PRECISION"),
            ("exp_bias",        "TEXT DEFAULT 'NEUTRAL'"),
        ]
        with conn.cursor() as cur:
            for kolom, definitie in migraties:
                cur.execute(f"""
                ALTER TABLE public.pending_approvals
                ADD COLUMN IF NOT EXISTS {kolom} {definitie};
                """)
        conn.commit()
    except Exception as e:
        safe_rollback(conn)
        log(f"Pending migratie fout: {e}")


def insert_pending(conn, prebuy: Dict) -> str:
    """Voegt Pre-BUY signaal in pending_approvals. v3.0: extra velden."""
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
                created_at, status, score_details,
                vwap_positie, divergentie, funding_rate, live_toegestaan
            ) VALUES (
                %s,%s,%s,%s,'4H',%s,%s,
                %s,'GO',%s,%s,%s,%s,%s,
                %s,%s,%s,%s,
                %s,%s,%s,%s,%s,
                NOW(),'PENDING',%s::jsonb,
                %s,%s,%s,%s
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
                prebuy.get("vwap_positie","ONBEKEND"),
                prebuy.get("divergentie","GEEN"),
                prebuy.get("funding_rate", 0.0),
                prebuy.get("live_toegestaan", False),
            ))
        conn.commit()
        log(f"Pre-BUY: {prebuy['symbol']} score={prebuy['score']} "
            f"setup={prebuy['setup_type']} LIVE={'JA' if prebuy.get('live_toegestaan') else 'NEE'} "
            f"id={prebuy_id[:8]}")
        return prebuy_id
    except Exception as e:
        log(f"insert_pending fout ({prebuy['symbol']}): {e}")
        try: conn.rollback()
        except Exception: pass
        return ""


def trigger_auto_buy(prebuy_id: str) -> bool:
    """Triggert /auto_buy op whatsapp_webhook.py. Webhook beslist: shadow altijd, live als actief."""
    if not WEBHOOK_BASE_URL:
        log("WEBHOOK_BASE_URL niet ingesteld — auto_buy niet getriggerd")
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
# DAGELIJKS RAPPORT  (v3.0)
# ============================================================
def is_rapport_tijd() -> bool:
    """Rapport-uur aangebroken (08:00 UTC, eerste 15 minuten)."""
    nu = now_utc()
    return nu.hour == RAPPORT_HOUR_UTC and nu.minute < 15


def is_rapport_al_verstuurd(conn) -> bool:
    return get_bot_state_value(conn, "dagrapport_datum", "") == utc_day_str()


def verstuur_dagrapport(conn) -> None:
    """
    v3.0: Verstuurt dagelijks WhatsApp rapport om 08:00 UTC.
    Bevat: PnL, trades, win rate, shadow count, beste signalen, BTC regime.
    """
    if not is_rapport_tijd() or is_rapport_al_verstuurd(conn):
        return
    log("Dagrapport aanmaken...")
    try:
        btc_regime   = get_btc_regime(conn)
        btc_sterkte  = get_btc_sterkte(conn)
        dagpnl       = get_daily_pnl(conn)
        wins, losses = get_daily_win_loss(conn)
        dag_trades   = get_daily_trade_count(conn)
        shadow_cnt   = get_daily_shadow_count(conn)
        top_coins    = get_top_signalen_vandaag(conn)

        claude_txt = claude_dagrapport(dag_trades, shadow_cnt, dagpnl,
                                        wins, losses, btc_regime, top_coins)

        totaal = wins + losses
        wr = wins / totaal if totaal > 0 else 0.0

        rapport = (
            f"DAGRAPPORT — {utc_day_str()}\n"
            f"{'='*30}\n"
            f"BTC: {btc_regime} ({btc_sterkte:.0f}%)\n"
            f"Live: {dag_trades} trades | W:{wins} L:{losses} WR:{pct_str(wr)}\n"
            f"Shadow: {shadow_cnt} | PnL: {eur_str(dagpnl)}\n"
        )
        if top_coins:
            rapport += "\nBeste signalen:\n"
            for c in top_coins[:3]:
                rapport += f"  {c.get('symbol','')} score={c.get('score',0)} ({c.get('setup_type','')})\n"
        if claude_txt:
            rapport += f"\nClaude: {claude_txt}"
        rapport += "\n\nCommands: STATUS | TRADES | STOP"

        send_whatsapp(rapport)
        set_bot_state_value(conn, "dagrapport_datum",    utc_day_str())
        set_bot_state_value(conn, "dagrapport_verstuurd", now_utc().strftime("%Y-%m-%d %H:%M UTC"))
        log("Dagrapport verstuurd")
    except Exception as e:
        report_error(e, "verstuur_dagrapport", severity="MEDIUM")


# ============================================================
# HEALTH MONITORING  (v3.0)
# ============================================================
def voer_health_check_uit(conn) -> Dict[str, Any]:
    """
    v3.0: Uitgebreide health check van scanner en systeem.
    Controleert: DB, Bitvavo API, Binance API, Claude API, Webhook,
                 BTC data versheid, Candles versheid.
    """
    health: Dict[str, Any] = {
        "database":      False,
        "bitvavo_api":   False,
        "binance_api":   False,
        "claude_api":    False,
        "webhook":       False,
        "btc_data_vers": False,
        "candles_vers":  False,
        "problemen":     [],
        "score":         0,
    }

    # Database
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            health["database"] = True
    except Exception as e:
        health["problemen"].append(f"DB fout: {e}")

    # Bitvavo
    try:
        resp = requests.get(f"{BITVAVO_BASE}/v2/markets", timeout=10)
        health["bitvavo_api"] = resp.ok
        if not resp.ok:
            health["problemen"].append(f"Bitvavo status {resp.status_code}")
    except Exception as e:
        health["problemen"].append(f"Bitvavo onbereikbaar: {e}")

    # Binance
    try:
        resp = requests.get(f"{BINANCE_BASE}/ping", timeout=5)
        health["binance_api"] = resp.ok
        if not resp.ok:
            health["problemen"].append(f"Binance status {resp.status_code}")
    except Exception as e:
        health["problemen"].append(f"Binance onbereikbaar: {e}")

    # Claude
    if ANTHROPIC_API_KEY:
        test = _claude_analyse("Zeg alleen OK.", 10)
        health["claude_api"] = bool(test)
        if not test:
            health["problemen"].append("Claude API reageert niet")
    else:
        health["problemen"].append("ANTHROPIC_API_KEY niet ingesteld")

    # Webhook
    if WEBHOOK_BASE_URL:
        try:
            resp = requests.get(f"{WEBHOOK_BASE_URL}/health", timeout=10)
            health["webhook"] = resp.ok
        except Exception:
            health["problemen"].append("Webhook onbereikbaar")
    else:
        health["problemen"].append("WEBHOOK_BASE_URL niet ingesteld")

    # BTC data versheid
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(open_time) FROM public.btc_regime_4h")
            row = cur.fetchone()
            if row and row[0]:
                laatste = row[0]
                if hasattr(laatste, 'tzinfo') and laatste.tzinfo is None:
                    laatste = laatste.replace(tzinfo=timezone.utc)
                uren_oud = (now_utc() - laatste).total_seconds() / 3600
                health["btc_data_vers"] = uren_oud < 5
                if not health["btc_data_vers"]:
                    health["problemen"].append(f"BTC data {uren_oud:.0f}u oud")
    except Exception as e:
        health["problemen"].append(f"BTC data check fout: {e}")

    # Candles versheid
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(updated_at) FROM public.candles WHERE timeframe='4h'")
            row = cur.fetchone()
            if row and row[0]:
                laatste = row[0]
                if hasattr(laatste, 'tzinfo') and laatste.tzinfo is None:
                    laatste = laatste.replace(tzinfo=timezone.utc)
                uren_oud = (now_utc() - laatste).total_seconds() / 3600
                health["candles_vers"] = uren_oud < 2
                if not health["candles_vers"]:
                    health["problemen"].append(f"Candles {uren_oud:.0f}u oud")
    except Exception:
        pass  # Non-kritiek

    ok_count = sum(1 for k, v in health.items() if isinstance(v, bool) and v)
    total    = sum(1 for v in health.values() if isinstance(v, bool))
    health["score"] = int(ok_count / max(total, 1) * 100)
    return health


def log_health_check(health: Dict) -> None:
    log(f"Health check: {health['score']}/100")
    log(f"  DB:{'+' if health['database'] else '-'} "
        f"Bitvavo:{'+' if health['bitvavo_api'] else '-'} "
        f"Binance:{'+' if health['binance_api'] else '-'} "
        f"Claude:{'+' if health['claude_api'] else '-'} "
        f"Webhook:{'+' if health['webhook'] else '-'} "
        f"BTCdata:{'+' if health['btc_data_vers'] else '-'}")
    if health["problemen"]:
        log(f"  Problemen: {'; '.join(health['problemen'][:5])}")


# ============================================================
# HOOFD SCAN LOOP
# ============================================================
def scan_universe(conn, drempels: Dict) -> int:
    """
    Scant alle Bitvavo-tradable coins via Binance data.

    ARCHITECTUUR — 24/7 ACTIEF:
    ┌─────────────────────────────────────────────────────────────┐
    │  Scanner draait ALTIJD — 24 uur per dag, 7 dagen/week      │
    │  Shadow trades → ALTIJD geopend (leerdata voor ai_coach)   │
    │  Live trades   → alleen als bot AAN + trading hours OK     │
    │                                                             │
    │  START/STOP via WhatsApp stuurt ALLEEN live trades aan!    │
    │  BTC BEAR  → live geblokkeerd, shadow gaat door            │
    │  Dagbudget → live geblokkeerd, shadow gaat door            │
    └─────────────────────────────────────────────────────────────┘

    v3.0 NIEUW:
    - Regime-afhankelijke score drempel (BULL=88, RANGE=92, BEAR=99)
    - Funding rate filter per coin
    - VWAP + Stochastic RSI + Divergentie in score
    - Weekend live filter (optioneel via SKIP_WEEKEND_LIVE=1)
    - BTC trend richting in log
    """
    # ── Bot status ──────────────────────────────────────────
    bot_actief = is_bot_active(conn)
    bot_gepauz = is_bot_paused(conn)
    live_ok    = bot_actief and not bot_gepauz

    if not bot_actief:
        log("Bot GESTOPT — scanner zoekt 24/7 door voor shadow trades")
    elif bot_gepauz:
        reden = get_bot_state_value(conn, "bot_paused_reason", "onbekend")
        log(f"Bot GEPAUZEERD ({reden}) — shadow trades blijven actief")

    # ── Trading hours: alleen voor live ────────────────────
    if live_ok and not is_trading_hours():
        log(f"Buiten trading hours ({TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC)"
            f" — geen LIVE, shadow gaat door")
        live_ok = False

    # ── Weekend filter (v3.0) ──────────────────────────────
    if live_ok and SKIP_WEEKEND and is_weekend():
        log("Weekend — LIVE overgeslagen (SKIP_WEEKEND=1), shadow gaat door")
        live_ok = False

    # ── Daglimieten: alleen voor live ──────────────────────
    if live_ok:
        dagpnl = get_daily_pnl(conn)
        if dagpnl <= -DAILY_STOP_LOSS_EUR:
            log(f"Dagbudget bereikt ({eur_str(dagpnl)}) — geen LIVE meer, shadow gaat door")
            live_ok = False

    if live_ok:
        dag_trades = get_daily_trade_count(conn)
        if dag_trades >= MAX_REAL_TRADES_PER_DAY:
            log(f"Daglimiet {dag_trades}/{MAX_REAL_TRADES_PER_DAY} — geen LIVE meer")
            live_ok = False

    if live_ok:
        open_count = get_open_live_count(conn)
        if open_count >= MAX_OPEN_REAL_TRADES:
            log(f"Max open {open_count}/{MAX_OPEN_REAL_TRADES} — geen LIVE meer")
            live_ok = False

    # ── Pre-buy dagplafond ──────────────────────────────────
    prebuy_today = get_prebuy_count_today(conn)
    if prebuy_today >= MAX_PREBUY_PER_DAY:
        log(f"Pre-buy daglimiet bereikt: {prebuy_today}/{MAX_PREBUY_PER_DAY}")
        return 0

    # ── BTC regime + trend ─────────────────────────────────
    btc_regime   = get_btc_regime(conn)
    btc_sterkte  = get_btc_sterkte(conn)
    btc_trend    = get_btc_trend_richting(conn)
    _SESSIE["btc_regime"] = btc_regime

    if btc_regime == "BEAR" and BTC_SKIP_BEAR and live_ok:
        log("BTC BEAR — geen LIVE trades, shadow gaat door")
        live_ok = False

    # ── v3.0: Regime-afhankelijke score drempel ────────────
    score_drempel = get_score_drempel_voor_regime(btc_regime, drempels)
    _SESSIE["score_drempel"] = score_drempel

    log(f"BTC: {btc_regime} ({btc_sterkte:.0f}%) trend={btc_trend} | "
        f"Drempel: {score_drempel} (BULL={drempels['score_bull']} "
        f"RANGE={drempels['score_range']} BEAR={drempels['score_bear']}) | "
        f"LIVE: {'JA' if live_ok else 'NEE'} | SHADOW: ALTIJD | "
        f"TIJD: {now_utc().strftime('%H:%M')} UTC")

    # ── Bitvavo markets ─────────────────────────────────────
    tradable = get_tradable_markets()
    if not tradable:
        log("Geen tradable markets gevonden — scanner stopt")
        return 0

    scan_pairs: List[Tuple[str, str]] = []
    for market in tradable:
        if market.endswith("-EUR"):
            base = market[:-4]
            scan_pairs.append((f"{base}USDT", market))

    log(f"Scannen: {len(scan_pairs)} pairs | score drempel: {score_drempel}")

    min_chance = drempels.get("min_chance", MIN_CHANCE)
    min_conf   = drempels.get("min_confidence", MIN_CONFIDENCE)
    prebuy_count = 0

    for idx, (symbol_usdt, bitvavo_market) in enumerate(scan_pairs):

        if symbol_usdt in ("BTCUSDT", "BTCBUSD"):
            tel_filter("BTC_skip"); continue

        try:
            # ── Coin filters ───────────────────────────────
            if is_coin_blacklisted(conn, symbol_usdt):
                tel_filter("blacklist"); update_sessie(symbol_usdt, 0, "blacklist"); continue
            if is_coin_on_cooldown(conn, symbol_usdt):
                tel_filter("cooldown"); update_sessie(symbol_usdt, 0, "cooldown"); continue
            if symbol_already_pending(conn, symbol_usdt):
                tel_filter("al_pending"); update_sessie(symbol_usdt, 0, "al_pending"); continue

            # ── Candles ophalen ────────────────────────────
            candles_4h = fetch_candles(symbol_usdt, "4h", 120)
            if len(candles_4h) < 30:
                tel_filter("geen_candles"); update_sessie(symbol_usdt, 0, "geen_candles"); continue

            candles_1h = fetch_candles(symbol_usdt, "1h", 60)
            closes_4h  = [c["close"] for c in candles_4h]
            current    = closes_4h[-1]

            if current <= 0:
                tel_filter("prijs_nul"); continue

            # ── Volatiliteit check ─────────────────────────
            vol_label, atr_pct = detect_volatiliteit(candles_4h, current)
            if vol_label == "LAAG":
                tel_filter("vol_laag"); update_sessie(symbol_usdt, 0, "vol_laag"); continue

            # ── Coin regime ────────────────────────────────
            coin_regime = detect_coin_regime(closes_4h)
            if coin_regime == "BEAR" and btc_regime != "BULL":
                tel_filter("coin_bear"); update_sessie(symbol_usdt, 0, "coin_bear"); continue

            # ── Setup detectie ─────────────────────────────
            setup_type, why_base = detect_setup_type(candles_4h, candles_1h)
            if setup_type == "UNKNOWN":
                tel_filter("geen_setup"); update_sessie(symbol_usdt, 0, "geen_setup"); continue

            # ── Experience ophalen ─────────────────────────
            exp_win_rate, exp_n, exp_bias = get_experience(
                conn, symbol_usdt, setup_type, coin_regime)

            # ── v3.0: Extra indicatoren ────────────────────
            vwap_pos    = bereken_vwap_positie(candles_4h, current)
            stoch_rsi   = stochastic_rsi(closes_4h, RSI_PERIOD, 14)
            divergentie = detecteer_divergentie(closes_4h, candles_4h, 10)

            # ── v3.0: Funding rate filter ──────────────────
            funding_ok, funding_rate = check_funding_rate_ok(symbol_usdt)
            if not funding_ok:
                tel_filter("funding_extreem")
                update_sessie(symbol_usdt, 0, "funding_extreem")
                log(f"Funding te extreem {symbol_usdt}: {funding_rate*100:.4f}%")
                continue

            # ── Ticker voor volume data ────────────────────
            ticker = fetch_ticker_24h(symbol_usdt)

            # ── Score berekening ───────────────────────────
            score, chance, confidence, why_tag, rsi_4h, vol_ratio = calculate_score(
                candles_4h, candles_1h, ticker, coin_regime, btc_regime,
                btc_sterkte, setup_type, exp_win_rate, exp_n, drempels,
                vwap_pos, stoch_rsi, divergentie, funding_rate,
            )

            # Whitelist bonus
            if is_coin_whitelisted(conn, symbol_usdt):
                score = min(score + 5, 100)
                why_tag += " | WHITELIST"

            update_sessie(symbol_usdt, score)

            # ── v3.0: Regime-afhankelijke drempel check ────
            if score < score_drempel:
                tel_filter("score_laag"); continue
            if chance < min_chance:
                tel_filter("chance_laag"); continue
            if confidence < min_conf:
                tel_filter("conf_laag"); continue

            log(f"SIGNAAL {symbol_usdt}: score={score}/{score_drempel} "
                f"chance={chance}% conf={confidence}% setup={setup_type} "
                f"vwap={vwap_pos} div={divergentie} funding={funding_rate*100:.4f}% "
                f"LIVE={'JA' if live_ok else 'NEE'} SHADOW=JA")

            # ── ATR-based stop en target ───────────────────
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

            # Support / weerstand aanpassing
            support, weerstand = detecteer_support_weerstand(candles_4h, 20)
            if support > 0 and stop < support * 0.98:
                stop = support * 0.99
            if weerstand > 0 and target > weerstand * 1.05:
                target = weerstand * 0.99

            # ── Claude beoordeling ─────────────────────────
            claude_txt = claude_beoordeel_signaal(
                symbol_usdt, setup_type, coin_regime, btc_regime,
                score, chance, confidence, rsi_4h, vol_ratio,
                exp_win_rate, exp_n, why_tag, funding_rate,
            )

            coin_stats = get_coin_statistieken(conn, symbol_usdt)

            # ── Pre-BUY aanmaken ───────────────────────────
            prebuy = {
                "id":              str(uuid.uuid4()),
                "symbol":          symbol_usdt,
                "setup_type":      setup_type,
                "regime":          coin_regime,
                "btc_regime":      btc_regime,
                "score":           score,
                "chance":          chance,
                "confidence":      confidence,
                "entry":           current,
                "stop":            stop,
                "target":          target,
                "bitvavo_market":  bitvavo_market,
                "exp_n":           exp_n,
                "exp_win_rate":    exp_win_rate,
                "exp_bias":        exp_bias,
                "why_tag":         why_tag,
                "claude_beoordeling": claude_txt,
                "live_toegestaan": live_ok,
                "vwap_positie":    vwap_pos,
                "divergentie":     divergentie,
                "funding_rate":    funding_rate,
                "score_details": {
                    "rsi_4h":           rsi_4h,
                    "vol_ratio":        vol_ratio,
                    "atr_pct":          round(atr_pct * 100, 2),
                    "vol_label":        vol_label,
                    "vwap_positie":     vwap_pos,
                    "stoch_rsi":        stoch_rsi,
                    "divergentie":      divergentie,
                    "funding_rate":     funding_rate,
                    "coin_stats":       coin_stats,
                    "why_base":         why_base,
                    "btc_sterkte":      btc_sterkte,
                    "btc_trend":        btc_trend,
                    "score_drempel":    score_drempel,
                    "live_toegestaan":  live_ok,
                    "atr_multiplier":   atr_eff,
                    "atr_target_r":     tgt_eff,
                },
            }

            prebuy_id = insert_pending(conn, prebuy)
            if prebuy_id:
                prebuy_count += 1
                prebuy_today += 1
                if live_ok:
                    _SESSIE["live_trades"] += 1
                else:
                    _SESSIE["shadow_trades"] += 1
                # Stuur ALTIJD naar /auto_buy
                # Webhook besluit zelf: shadow altijd, live alleen als bot actief
                trigger_auto_buy(prebuy_id)

            if prebuy_today >= MAX_PREBUY_PER_DAY:
                log(f"Pre-buy daglimiet bereikt: {prebuy_today}")
                break

        except Exception as e:
            report_error(e, "scan_universe_coin", severity="MEDIUM", symbol=symbol_usdt)
            continue

        log_sessie_voortgang(idx + 1, len(scan_pairs))

    log(f"Scan klaar: {_SESSIE['gescand']} gescand | {prebuy_count} pre-buys | "
        f"Drempel gebruikt: {score_drempel} (BTC={btc_regime})")
    return prebuy_count


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    log("=" * 65)
    log(f"Multi Coin Scorer v3.0 — {now_utc().strftime('%Y-%m-%d %H:%M UTC')}")
    log("=" * 65)
    log(f"Database:              {'OK' if DATABASE_URL else 'ONTBREEKT'}")
    log(f"Webhook URL:           {'OK' if WEBHOOK_BASE_URL else 'niet ingesteld'}")
    log(f"Claude API:            {'OK' if ANTHROPIC_API_KEY else 'niet ingesteld'}")
    log(f"Min score:             {MIN_SCORE_TO_TRADE}")
    log(f"Score BULL/RANGE/BEAR: {SCORE_DREMPEL_BULL}/{SCORE_DREMPEL_RANGE}/{SCORE_DREMPEL_BEAR}")
    log(f"Min chance/conf:       {MIN_CHANCE}% / {MIN_CONFIDENCE}%")
    log(f"ATR:                   {ATR_PERIOD}p x{ATR_MULTIPLIER} stop x{ATR_TARGET_R} target")
    log(f"Fee+slippage:          {TOTAL_COST_PCT*100:.2f}%")
    log(f"BTC skip BEAR:         {BTC_SKIP_BEAR}")
    log(f"Trading hours:         {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC")
    log(f"Cooldown:              {COIN_COOLDOWN_HOURS}u na verlies")
    log(f"Funding filter:        {MIN_FUNDING_RATE*100:.3f}% tot {MAX_FUNDING_RATE*100:.3f}%")
    log(f"Weekend live skip:     {SKIP_WEEKEND}")
    log(f"Rapport uur UTC:       {RAPPORT_HOUR_UTC}:00")
    log("=" * 65)

    if not DATABASE_URL:
        log("KRITIEK: DATABASE_URL ontbreekt — scanner kan niet starten")
        sys.exit(1)

    conn = None  # FIX: conn=None voor try/finally zodat finally altijd werkt
    try:
        conn = db_connect()
        log("Database verbonden")

        # ── v3.0: Health check ──────────────────────────────
        log("Health check uitvoeren...")
        health = voer_health_check_uit(conn)
        log_health_check(health)
        if health["score"] < 50:
            send_whatsapp(
                f"SCANNER WAARSCHUWING\n"
                f"Health: {health['score']}/100\n"
                f"Problemen: {' | '.join(health['problemen'][:3])}"
            )

        # Claude health check
        if ANTHROPIC_API_KEY:
            log("Claude scanner health check...")
            hc_txt = claude_scanner_health_check()
            if hc_txt:
                log(f"Claude health: {hc_txt}")

        # Pending tabel aanmaken als nodig
        zorg_voor_pending_tabel(conn)

        # v3.0: Verlopen pending opruimen
        cleanup_verlopen_pending(conn)

        # Coach drempels ophalen (adaptieve parameters)
        drempels = haal_coach_drempels_op(conn)
        log(f"Coach drempels: score>={drempels['min_score']} "
            f"ATR={drempels['atr_multiplier']} "
            f"BULL={drempels['score_bull']}/RANGE={drempels['score_range']}/BEAR={drempels['score_bear']}")

        # Sessie initialiseren
        init_sessie()

        # BTC en Bitvavo status
        btc         = get_btc_regime(conn)
        btc_sterkte = get_btc_sterkte(conn)
        btc_trend   = get_btc_trend_richting(conn)
        markets     = get_tradable_markets()
        log(f"BTC regime: {btc} ({btc_sterkte:.0f}%) trend={btc_trend}")
        log(f"Bitvavo markets: {len(markets)} tradable")

        # v3.0: Dagelijks rapport check
        verstuur_dagrapport(conn)

        # Hoofd scan
        n = scan_universe(conn, drempels)
        log(f"Resultaat: {n} pre-buys gegenereerd")

        # Sessie opslaan in bot_state + scanner_sessies tabel
        sla_sessie_op(conn)
        sla_sessie_naar_tabel(conn)

        # Claude sessie analyse
        if ANTHROPIC_API_KEY and _SESSIE["gescand"] > 10:
            analyse = claude_analyseer_sessie(_SESSIE)
            if analyse:
                log(f"Claude sessie: {analyse}")
                set_bot_state_value(conn, "laatste_scan_claude", analyse)

        # Claude marktbeoordeling
        if ANTHROPIC_API_KEY:
            markt = claude_beoordeel_marktomstandigheden(
                btc, n, _SESSIE["gescand"], now_utc().hour, btc_sterkte
            )
            if markt:
                log(f"Claude markt: {markt}")
                set_bot_state_value(conn, "markt_beoordeling", markt)

        # WhatsApp bij meerdere signalen
        if n >= 3:
            top5 = sorted(_SESSIE["coins_met_score"],
                          key=lambda x: x["score"], reverse=True)[:5]
            bericht = (
                f"Scanner: {n} signalen (drempel={_SESSIE['score_drempel']} BTC={btc})\n"
                + "\n".join(f"{c.get('symbol','')} score={c.get('score',0)}" for c in top5)
            )
            send_whatsapp(bericht)

        # Status wegschrijven
        set_bot_state_value(conn, "scanner_actief",  "true")
        set_bot_state_value(conn, "scanner_versie",  "3.0")
        set_bot_state_value(conn, "scanner_health",  str(health["score"]))

        conn.close()
        log("Scanner klaar")
        sys.exit(0)

    except KeyboardInterrupt:
        log("Scanner gestopt door gebruiker")
        sys.exit(0)
    except Exception as e:
        report_error(e, "__main__", severity="KRITIEK")
        sys.exit(1)

    finally:
        # ALTIJD sluiten — FIX: vorige versie had geen finally block
        if conn:
            try:
                conn.close()
                log("DB verbinding gesloten")
            except Exception:
                pass
