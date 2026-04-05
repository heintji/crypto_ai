# analysis/multi_coin_score.py
# ============================================================
# Crypto AI Bot — Multi Coin Scorer v4.1
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
#   - Taker buy ratio (koopdruk)         [v4.0] NEW
#   - Markt structuur (HH/HL vs LH/LL)  [v4.0] NEW
#   - Volume zone detectie               [v4.0] NEW
#   - Coin correlatie filter             [v4.0] NEW
#   - Auto blacklist/whitelist update    [v4.0] NEW
#   - Edge decay per coin                [v4.0] NEW
#   - Kelly Criterion positiegrootte     [v4.0] NEW
#   - Coin clustering STAR/STABLE/WEAK   [v4.0] NEW
#   - Score gradient efficiency analyse  [v4.0] NEW
#   - Uitgebreide WhatsApp scan rapport  [v4.0] NEW
#   - Markt sessie timing (EU/US/ASIA)   [v4.0] NEW
#   - Correlatie risico Claude analyse   [v4.0] NEW
#   - Open posities correlatie check     [v4.0] NEW
#
# FIXES v4.1:
# ─────────────────────────────────────────────────────────────
# ✅ Fix 1: model string → claude-sonnet-4-6
# ✅ Fix 2: BREAKOUT_RETEST in BEAR geblokkeerd
#    Basis: 149 shadow trades tonen 28.9% win rate = verliesgevend.
#    In een bearish BTC markt mislukt bijna elke uitbraak.
#    Blokkeren bespaart onnodige verliezen.
# ✅ Fix 3: TREND_PULLBACK score bonus +5
#    Basis: 67% win rate RANGE, 53.6% BULL = bewezen beste setup.
#    Bonus geeft deze setup voorrang bij gelijke scores.
# ✅ Fix 4: Dashboard globals override in main()
#    Min score en trading hours worden nu gelezen uit bot_state DB.
#    Wijzigingen via het dashboard zijn direct actief zonder herstart.
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
TRADING_HOURS_START     = int(os.getenv("TRADING_HOURS_START") or "0")
TRADING_HOURS_END       = int(os.getenv("TRADING_HOURS_END") or "24")
BOT_STATE_TABLE         = "public.bot_state"

# Score & filter instellingen
MIN_SCORE_TO_TRADE  = int(os.getenv("MIN_SCORE_TO_TRADE") or "85")
MIN_CHANCE          = int(os.getenv("MIN_CHANCE") or "75")
MIN_CONFIDENCE      = int(os.getenv("MIN_CONFIDENCE") or "75")
MAX_PREBUY_PER_DAY  = int(os.getenv("MAX_PREBUY_PER_DAY") or "200")
PREBUY_EXPIRY_HOURS = int(os.getenv("PREBUY_EXPIRY_HOURS") or "2")

# ── Regime-afhankelijke score drempels ────────────────────
SCORE_DREMPEL_BULL  = int(os.getenv("SCORE_DREMPEL_BULL")  or "85")
SCORE_DREMPEL_RANGE = int(os.getenv("SCORE_DREMPEL_RANGE") or "80")
SCORE_DREMPEL_BEAR  = int(os.getenv("SCORE_DREMPEL_BEAR")  or "99")

# Fee + slippage
BITVAVO_FEE_PCT = float(os.getenv("BITVAVO_FEE_PCT") or "0.0025")
SLIPPAGE_PCT    = float(os.getenv("SLIPPAGE_PCT") or "0.001")
TOTAL_COST_PCT  = BITVAVO_FEE_PCT + SLIPPAGE_PCT

# Coin filter instellingen
COIN_COOLDOWN_HOURS   = float(os.getenv("COIN_COOLDOWN_HOURS") or "48.0")
BLACKLIST_MIN_TRADES  = int(os.getenv("BLACKLIST_MIN_TRADES") or "15")
BLACKLIST_MAX_WINRATE = float(os.getenv("BLACKLIST_MAX_WINRATE") or "0.35")
WHITELIST_MIN_WINRATE = float(os.getenv("WHITELIST_MIN_WINRATE") or "0.60")
WHITELIST_MIN_TRADES  = int(os.getenv("WHITELIST_MIN_TRADES") or "20")

# Binance API
BINANCE_BASE    = "https://api.binance.com/api/v3"
BINANCE_FAPI    = "https://fapi.binance.com/fapi/v1"
BINANCE_SLEEP   = float(os.getenv("BINANCE_SLEEP") or "0.0")
BINANCE_TIMEOUT = int(os.getenv("BINANCE_TIMEOUT") or "10")
MAX_RETRIES     = int(os.getenv("MAX_RETRIES") or "3")
BITVAVO_BASE    = "https://api.bitvavo.com"

# ATR instellingen
ATR_PERIOD     = int(os.getenv("ATR_PERIOD") or "14")
ATR_MULTIPLIER = float(os.getenv("ATR_MULTIPLIER") or "1.6")
ATR_TARGET_R   = float(os.getenv("ATR_TARGET_R") or "2.0")

# RSI instellingen
RSI_PERIOD = int(os.getenv("RSI_PERIOD") or "14")
RSI_MIN    = int(os.getenv("RSI_MIN") or "40")
RSI_MAX    = int(os.getenv("RSI_MAX") or "60")

# BTC regime filter
BTC_SKIP_BEAR = os.getenv("BTC_SKIP_BEAR", "1").strip() == "1"

# Volatiliteit filter
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT") or "0.08")
MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT") or "0.005")

# Funding rate filter
MAX_FUNDING_RATE = float(os.getenv("MAX_FUNDING_RATE") or "0.001")
MIN_FUNDING_RATE = float(os.getenv("MIN_FUNDING_RATE") or "-0.001")

# v4.0: Correlatie filter — max correlatie tussen open coins
MAX_CORRELATIE_DREMPEL = float(os.getenv("MAX_CORRELATIE_DREMPEL") or "0.85")

# v4.0: Markt sessie timing bonus
SESSIE_BONUS_EU   = int(os.getenv("SESSIE_BONUS_EU") or "3")
SESSIE_BONUS_US   = int(os.getenv("SESSIE_BONUS_US") or "2")
SESSIE_BONUS_ASIA = int(os.getenv("SESSIE_BONUS_ASIA") or "1")

# v4.0: Kelly Criterion instelling
KELLY_FRACTIE = float(os.getenv("KELLY_FRACTIE") or "0.25")
MAX_KELLY_EUR  = float(os.getenv("MAX_KELLY_EUR") or "2.00")
MIN_KELLY_EUR  = float(os.getenv("MIN_KELLY_EUR") or "0.25")

# v4.0: Auto blacklist/whitelist update instellingen
AUTO_BL_INTERVAL_UREN = int(os.getenv("AUTO_BL_INTERVAL_UREN") or "6")

# Overige filters
MAX_SPREAD_PCT   = float(os.getenv("MAX_SPREAD_PCT") or "0.5")
SKIP_WEEKEND     = os.getenv("SKIP_WEEKEND_LIVE", "0").strip() == "1"
RAPPORT_HOUR_UTC = int(os.getenv("RAPPORT_HOUR_UTC") or "8")

# Caches
_MARKETS_CACHE: Dict[str, Any] = {"ts": 0.0, "markets": set()}
_MARKETS_TTL   = 30 * 60
_FUNDING_CACHE: Dict[str, Tuple[float, float]] = {}
_FUNDING_TTL   = 60 * 60
_CORRELATIE_CACHE: Dict[str, List[float]] = {}

# Scan sessie statistieken
_SESSIE: Dict[str, Any] = {
    "start":              None,
    "gescand":            0,
    "signalen":           0,
    "gefilterd":          {},
    "beste_score":        0,
    "beste_coin":         "",
    "coins_met_score":    [],
    "fouten":             0,
    "live_trades":        0,
    "shadow_trades":      0,
    "btc_regime":         "UNKNOWN",
    "score_drempel":      MIN_SCORE_TO_TRADE,
    "correlatie_skip":    0,
    "taker_bonus":        0,
    "markt_structuur_ok": 0,
    "kelly_grootte":      [],
    "coin_clusters":      {"STAR": 0, "STABLE": 0, "WEAK": 0, "ZOMBIE": 0},
    "sessie_timing":      "ONBEKEND",
    "bl_updates":         0,
    "wl_updates":         0,
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
    """
    Claude API aanroep. Geeft lege string bij elke fout.
    FIX v4.1: model string bijgewerkt naar claude-sonnet-4-6.
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
                "model":      "claude-sonnet-4-6",  # FIX v4.1: was claude-sonnet-4-20250514
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
                              funding_rate: float = 0.0,
                              markt_structuur: str = "ONBEKEND",
                              taker_ratio: float = 0.5,
                              coin_cluster: str = "STABLE") -> str:
    prompt = (
        f"Crypto trading bot signaal beoordeling in 2 zinnen Nederlands.\n"
        f"Coin:{symbol} Setup:{setup_type} Regime:{regime} BTC:{btc_regime}\n"
        f"Score:{score} Kans:{chance}% Conf:{confidence}% RSI:{rsi_4h:.1f} "
        f"Vol:{vol_ratio:.1f}x WR:{pct_str(exp_win_rate)}({exp_n} trades) "
        f"Funding:{funding_rate*100:.4f}%\n"
        f"Markt structuur:{markt_structuur} Taker koopdruk:{taker_ratio:.1%} "
        f"Coin cluster:{coin_cluster}\n"
        f"Tags: {why_tag}\nIs dit een goed signaal en wat zijn de risicos?"
    )
    return _claude_analyse(prompt, 120)


def claude_analyseer_correlatie_risico(open_coins: List[str],
                                        new_coin: str,
                                        correlatie: float) -> str:
    prompt = (
        f"Crypto portfolio correlatie risico analyse in 2 zinnen Nederlands.\n"
        f"Open posities: {', '.join(open_coins[:5])}\n"
        f"Nieuw signaal: {new_coin} | Correlatie: {correlatie:.2f}\n"
        f"Is dit correlatie risico acceptabel? En wat is de impact op het totale risico?"
    )
    return _claude_analyse(prompt, 100)


def claude_analyseer_sessie(sessie: Dict) -> str:
    duur = (now_utc() - sessie["start"]).total_seconds() / 60 if sessie["start"] else 0
    filter_txt = ", ".join(f"{k}:{v}" for k, v in list(sessie["gefilterd"].items())[:8])
    clusters = sessie.get("coin_clusters", {})
    prompt = (
        f"Analyseer deze crypto scanner sessie in 3 zinnen Nederlands.\n"
        f"Gescand:{sessie.get('gescand',0)} Signalen:{sessie.get('signalen',0)} "
        f"Duur:{duur:.1f}min Fouten:{sessie.get('fouten',0)}\n"
        f"Live:{sessie.get('live_trades',0)} Shadow:{sessie.get('shadow_trades',0)}\n"
        f"Correlatie skip:{sessie.get('correlatie_skip',0)} "
        f"Taker bonus:{sessie.get('taker_bonus',0)}\n"
        f"Clusters — STAR:{clusters.get('STAR',0)} STABLE:{clusters.get('STABLE',0)} "
        f"WEAK:{clusters.get('WEAK',0)} ZOMBIE:{clusters.get('ZOMBIE',0)}\n"
        f"Beste coin:{sessie.get('beste_coin','')} score={sessie.get('beste_score',0)}\n"
        f"BTC:{sessie.get('btc_regime','?')} Drempel:{sessie.get('score_drempel',0)}\n"
        f"Filters: {filter_txt}\n"
        f"Wat valt op en zijn er aanbevelingen voor de volgende scan?"
    )
    return _claude_analyse(prompt, 200)


def claude_scanner_health_check() -> str:
    prompt = (
        f"Check multi_coin_score.py v4.1 configuratie in 3 zinnen Nederlands.\n"
        f"DB:{'OK' if DATABASE_URL else 'ONTBREEKT'} "
        f"Webhook:{'OK' if WEBHOOK_BASE_URL else 'NIET_INGESTELD'} "
        f"Claude:{'OK' if ANTHROPIC_API_KEY else 'ONTBREEKT'} "
        f"MinScore:{MIN_SCORE_TO_TRADE} "
        f"BULL:{SCORE_DREMPEL_BULL}/RANGE:{SCORE_DREMPEL_RANGE}/BEAR:{SCORE_DREMPEL_BEAR} "
        f"ATR:{ATR_MULTIPLIER} Fee:{TOTAL_COST_PCT*100:.2f}% "
        f"Uren:{TRADING_HOURS_START}-{TRADING_HOURS_END}UTC "
        f"FundingMax:{MAX_FUNDING_RATE*100:.3f}% "
        f"Correlatie max:{MAX_CORRELATIE_DREMPEL:.2f} Kelly:{KELLY_FRACTIE:.2f}\n"
        f"Zijn er problemen of risicos?"
    )
    return _claude_analyse(prompt, 150)


def claude_beoordeel_marktomstandigheden(btc_regime: str, n_signalen: int,
                                          n_gescand: int, uur: int,
                                          btc_sterkte: float = 50.0,
                                          sessie_timing: str = "ONBEKEND") -> str:
    conversie = round(n_signalen / max(n_gescand, 1) * 100, 1)
    prompt = (
        f"Beoordeel marktomstandigheden voor crypto bot in 2 zinnen Nederlands.\n"
        f"BTC regime:{btc_regime} Sterkte:{btc_sterkte:.1f}% Uur:{uur}:00 UTC "
        f"Sessie:{sessie_timing}\n"
        f"Scan: {n_signalen}/{n_gescand} signalen ({conversie}% conversie)\n"
        f"Is dit een goede tijd om te handelen?"
    )
    return _claude_analyse(prompt, 100)


def claude_dagrapport(n_live: int, n_shadow: int, dagpnl: float,
                       wins: int, losses: int, btc_regime: str,
                       top_coins: List[Dict],
                       edge_decay_coins: List[str] = []) -> str:
    wr = wins / max(wins + losses, 1)
    top_txt = ", ".join(f"{c.get('symbol','?')}({c.get('score',0)})" for c in top_coins[:3])
    edge_txt = f"Edge decay gedetecteerd bij: {', '.join(edge_decay_coins[:3])}" if edge_decay_coins else ""
    prompt = (
        f"Schrijf dagelijks crypto bot rapport in 4 zinnen Nederlands.\n"
        f"Datum: {utc_day_str()} UTC | BTC:{btc_regime}\n"
        f"Live:{n_live} Win:{wins} Loss:{losses} WR:{pct_str(wr)}\n"
        f"Shadow:{n_shadow} | PnL:{eur_str(dagpnl)}\n"
        f"Beste signalen: {top_txt}\n"
        f"{edge_txt}\n"
        f"Hoogtepunten en aanbevelingen voor morgen?"
    )
    return _claude_analyse(prompt, 250)


def claude_analyseer_score_gradient(gradient_data: Dict) -> str:
    prompt = (
        f"Analyseer score drempel efficiëntie voor crypto bot in 3 zinnen Nederlands.\n"
        f"Gradient data (drempel → gemiddelde WR):\n"
        f"{json.dumps(gradient_data, indent=2, default=str)}\n"
        f"Welke score drempel geeft de beste balans tussen trades en win rate? "
        f"Geef een concrete aanbeveling."
    )
    return _claude_analyse(prompt, 150)


# ============================================================
# DATABASE
# ============================================================
def db_connect(retries: int = 3):
    """Verbindt met PostgreSQL. Retries bij fout. autocommit=False."""
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
    """Voert rollback uit zonder te crashen. Altijd aanroepen na een DB fout."""
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
    v4.1: ook trading_hours worden gelezen zodat dashboard controls werken.
    """
    drempels = {
        "min_score":           MIN_SCORE_TO_TRADE,
        "min_chance":          MIN_CHANCE,
        "min_confidence":      MIN_CONFIDENCE,
        "atr_multiplier":      ATR_MULTIPLIER,
        "atr_target_r":        ATR_TARGET_R,
        "rsi_min":             RSI_MIN,
        "rsi_max":             RSI_MAX,
        "score_bull":          SCORE_DREMPEL_BULL,
        "score_range":         SCORE_DREMPEL_RANGE,
        "score_bear":          SCORE_DREMPEL_BEAR,
        "kelly_fractie":       KELLY_FRACTIE,
        "max_correlatie":      MAX_CORRELATIE_DREMPEL,
        "trading_hours_start": TRADING_HOURS_START,
        "trading_hours_end":   TRADING_HOURS_END,
        "coach_suggesties":    [],
    }
    try:
        for key, dest, conv, default in [
            ("min_score_to_trade",  "min_score",           safe_int,   MIN_SCORE_TO_TRADE),
            ("atr_multiplier",      "atr_multiplier",      safe_float, ATR_MULTIPLIER),
            ("atr_target_r",        "atr_target_r",        safe_float, ATR_TARGET_R),
            ("rsi_min",             "rsi_min",             safe_int,   RSI_MIN),
            ("rsi_max",             "rsi_max",             safe_int,   RSI_MAX),
            ("score_drempel_bull",  "score_bull",          safe_int,   SCORE_DREMPEL_BULL),
            ("score_drempel_range", "score_range",         safe_int,   SCORE_DREMPEL_RANGE),
            ("score_drempel_bear",  "score_bear",          safe_int,   SCORE_DREMPEL_BEAR),
            ("kelly_fractie",       "kelly_fractie",       safe_float, KELLY_FRACTIE),
            ("max_correlatie",      "max_correlatie",      safe_float, MAX_CORRELATIE_DREMPEL),
            # v4.1: dashboard Bot Controls sliders
            ("trading_hours_start", "trading_hours_start", safe_int,   TRADING_HOURS_START),
            ("trading_hours_end",   "trading_hours_end",   safe_int,   TRADING_HOURS_END),
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
    """Regime-afhankelijke score drempel."""
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
    _SESSIE["start"]              = now_utc()
    _SESSIE["gescand"]            = 0
    _SESSIE["signalen"]           = 0
    _SESSIE["gefilterd"]          = {}
    _SESSIE["beste_score"]        = 0
    _SESSIE["beste_coin"]         = ""
    _SESSIE["coins_met_score"]    = []
    _SESSIE["fouten"]             = 0
    _SESSIE["live_trades"]        = 0
    _SESSIE["shadow_trades"]      = 0
    _SESSIE["btc_regime"]         = "UNKNOWN"
    _SESSIE["score_drempel"]      = drempels.get("min_score", MIN_SCORE_TO_TRADE)
    _SESSIE["correlatie_skip"]    = 0
    _SESSIE["taker_bonus"]        = 0
    _SESSIE["markt_structuur_ok"] = 0
    _SESSIE["kelly_grootte"]      = []
    _SESSIE["coin_clusters"]      = {"STAR": 0, "STABLE": 0, "WEAK": 0, "ZOMBIE": 0}
    _SESSIE["sessie_timing"]      = bepaal_markt_sessie_timing()
    _SESSIE["bl_updates"]         = 0
    _SESSIE["wl_updates"]         = 0


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
            "tijdstip":         now_utc().isoformat(),
            "gescand":          _SESSIE["gescand"],
            "signalen":         _SESSIE["signalen"],
            "duur_sec":         round(duur, 1),
            "beste_coin":       _SESSIE["beste_coin"],
            "beste_score":      _SESSIE["beste_score"],
            "filters":          _SESSIE["gefilterd"],
            "fouten":           _SESSIE["fouten"],
            "live_trades":      _SESSIE["live_trades"],
            "shadow_trades":    _SESSIE["shadow_trades"],
            "btc_regime":       _SESSIE["btc_regime"],
            "score_drempel":    _SESSIE["score_drempel"],
            "correlatie_skip":  _SESSIE["correlatie_skip"],
            "taker_bonus":      _SESSIE["taker_bonus"],
            "sessie_timing":    _SESSIE["sessie_timing"],
            "coin_clusters":    _SESSIE["coin_clusters"],
            "bl_updates":       _SESSIE["bl_updates"],
            "wl_updates":       _SESSIE["wl_updates"],
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
    """Slaat sessie op in scanner_sessies tabel voor historische analyse."""
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
                sessie_timing TEXT,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            );
            """)
            cur.execute("""
            INSERT INTO public.scanner_sessies (
                sessie_start, sessie_eind, gescand, signalen, duur_sec,
                beste_coin, beste_score, btc_regime, score_drempel,
                live_trades, shadow_trades, fouten, filters_json, top5_json, sessie_timing
            ) VALUES (%s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
            """, (
                _SESSIE["start"].isoformat(),
                _SESSIE["gescand"], _SESSIE["signalen"], round(duur, 1),
                _SESSIE["beste_coin"], _SESSIE["beste_score"],
                _SESSIE["btc_regime"], _SESSIE["score_drempel"],
                _SESSIE["live_trades"], _SESSIE["shadow_trades"], _SESSIE["fouten"],
                json.dumps(_SESSIE["gefilterd"]), json.dumps(top5),
                _SESSIE["sessie_timing"],
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
            f"beste: {_SESSIE['beste_coin']} score={_SESSIE['beste_score']} | "
            f"correlatie skip: {_SESSIE['correlatie_skip']}")


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
            if 400 <= resp.status_code < 500:
                return None
            log(f"Binance {resp.status_code} ({endpoint}) poging {attempt}/{retries}")
        except requests.exceptions.Timeout:
            log(f"Binance timeout poging {attempt}/{retries}")
        except Exception as e:
            log(f"Binance fout poging {attempt}/{retries}: {e}")
        if attempt < retries:
            time.sleep(2 ** attempt)
    return None


def fetch_candles(symbol: str, interval: str = "4h", limit: int = 120) -> List[Dict]:
    """
    Haalt OHLCV candles op van Binance.
    v4.0: ook taker_buy_base en taker_buy_quote meegenomen.
    """
    time.sleep(BINANCE_SLEEP)
    data = binance_get("/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not data: return []
    candles = []
    for c in data:
        try:
            candles.append({
                "open":            safe_float(c[1]),
                "high":            safe_float(c[2]),
                "low":             safe_float(c[3]),
                "close":           safe_float(c[4]),
                "volume":          safe_float(c[5]),
                "ts":              safe_int(c[0]),
                "quote_volume":    safe_float(c[7]),
                "trades":          safe_int(c[8]),
                "taker_buy_base":  safe_float(c[9]),
                "taker_buy_quote": safe_float(c[10]),
            })
        except Exception: continue
    return candles


def fetch_ticker_24h(symbol: str) -> Optional[Dict]:
    time.sleep(BINANCE_SLEEP)
    return binance_get("/ticker/24hr", {"symbol": symbol})


def fetch_order_book_spread(symbol: str) -> float:
    """Bid/ask spread als proxy voor liquiditeit."""
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
    """Haalt huidige funding rate op van Binance Futures. Cache: 1 uur."""
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
    """Controleert of funding rate binnen grenzen valt."""
    rate = fetch_funding_rate(symbol)
    ok = MIN_FUNDING_RATE <= rate <= MAX_FUNDING_RATE
    return ok, rate


# ============================================================
# TECHNISCHE INDICATOREN
# ============================================================
def rsi_wilder(closes: List[float], period: int = 14) -> Optional[float]:
    """Wilder RSI — identiek aan TradingView."""
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
    """ATR met Wilder smoothing."""
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
    """MACD: (macd_line, signal_line, histogram)."""
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
    """Stochastic RSI — RSI van RSI. Oversold <20, Overbought >80."""
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
    """Volume Weighted Average Price. Prijs boven VWAP = bullish."""
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
    """Swing high/low als weerstand/support."""
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
    """RSI divergentie detectie. BULLISH / BEARISH / GEEN."""
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
    """BOVEN / ONDER / OP / ONBEKEND t.o.v. VWAP."""
    vwap_val = vwap(candles, 20)
    if vwap_val is None or prijs <= 0: return "ONBEKEND"
    if prijs > vwap_val * 1.005:  return "BOVEN"
    elif prijs < vwap_val * 0.995: return "ONDER"
    return "OP"


# ============================================================
# v4.0 NIEUWE INDICATOREN
# ============================================================

def bereken_taker_buy_ratio(candles: List[Dict], lookback: int = 14) -> float:
    """
    v4.0: Taker buy ratio — maat voor koopdruk.
    > 0.55 = kopers domineren, < 0.45 = verkopers domineren.
    """
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    if not recent: return 0.5
    totaal_vol  = 0.0
    taker_koop  = 0.0
    for c in recent:
        vol  = safe_float(c.get("volume", 0))
        tbuy = safe_float(c.get("taker_buy_base", 0))
        if vol > 0:
            totaal_vol += vol
            taker_koop += tbuy
    if totaal_vol <= 0:
        return 0.5
    return round(taker_koop / totaal_vol, 4)


def detecteer_markt_structuur(closes: List[float], candles: List[Dict],
                               lookback: int = 20) -> str:
    """
    v4.0: Markt structuur analyse op basis van swing highs en swing lows.
    BULLISH_TREND = HH+HL, BEARISH_TREND = LH+LL, CONSOLIDATIE = gemengd.
    """
    if len(closes) < lookback or len(candles) < lookback:
        return "ONBEKEND"
    recent_candles = candles[-lookback:]
    swing_highs: List[float] = []
    swing_lows: List[float]  = []
    for i in range(1, len(recent_candles) - 1):
        h = recent_candles[i]["high"]
        l = recent_candles[i]["low"]
        h_prev, h_next = recent_candles[i-1]["high"], recent_candles[i+1]["high"]
        l_prev, l_next = recent_candles[i-1]["low"],  recent_candles[i+1]["low"]
        if h > h_prev and h > h_next:
            swing_highs.append(h)
        if l < l_prev and l < l_next:
            swing_lows.append(l)
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "CONSOLIDATIE"
    hh = swing_highs[-1] > swing_highs[-2]
    hl = swing_lows[-1]  > swing_lows[-2]
    lh = swing_highs[-1] < swing_highs[-2]
    ll = swing_lows[-1]  < swing_lows[-2]
    if hh and hl:   return "BULLISH_TREND"
    if lh and ll:   return "BEARISH_TREND"
    return "CONSOLIDATIE"


def bereken_volume_zone(candles: List[Dict], bins: int = 10) -> Tuple[float, float]:
    """v4.0: Volume profiel analyse — vindt de hoogste volume prijs zone."""
    if len(candles) < bins:
        c = candles[-1] if candles else {}
        prijs = c.get("close", 0) if c else 0
        return prijs * 0.95, prijs * 1.05
    alle_highs = [c["high"] for c in candles]
    alle_lows  = [c["low"]  for c in candles]
    prijs_min  = min(alle_lows)
    prijs_max  = max(alle_highs)
    if prijs_max <= prijs_min:
        return prijs_min, prijs_max
    bin_grootte = (prijs_max - prijs_min) / bins
    volumes_per_bin: List[float] = [0.0] * bins
    for c in candles:
        typisch = (c["high"] + c["low"] + c["close"]) / 3
        vol     = safe_float(c.get("quote_volume") or c.get("volume", 0))
        bin_idx = int((typisch - prijs_min) / bin_grootte)
        bin_idx = max(0, min(bins - 1, bin_idx))
        volumes_per_bin[bin_idx] += vol
    max_vol_idx = volumes_per_bin.index(max(volumes_per_bin))
    zone_low    = round(prijs_min + max_vol_idx * bin_grootte, 8)
    zone_high   = round(zone_low + bin_grootte, 8)
    return zone_low, zone_high


def bereken_pearson_correlatie(closes_a: List[float],
                                closes_b: List[float]) -> float:
    """v4.0: Pearson correlatie coëfficiënt tussen twee price series."""
    n = min(len(closes_a), len(closes_b))
    if n < 20:
        return 0.0
    a = closes_a[-n:]
    b = closes_b[-n:]
    gem_a = sum(a) / n
    gem_b = sum(b) / n
    teller   = sum((a[i] - gem_a) * (b[i] - gem_b) for i in range(n))
    noemer_a = (sum((x - gem_a) ** 2 for x in a)) ** 0.5
    noemer_b = (sum((x - gem_b) ** 2 for x in b)) ** 0.5
    noemer = noemer_a * noemer_b
    if noemer == 0:
        return 0.0
    return round(teller / noemer, 4)


def coin_correlatie_filter(conn, symbol: str, closes: List[float],
                            drempels: Dict) -> Tuple[bool, float, str]:
    """v4.0: Controleert of een coin te sterk gecorreleerd is met open posities."""
    max_drempel = drempels.get("max_correlatie", MAX_CORRELATIE_DREMPEL)
    open_coins: List[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COALESCE(coin, symbol, '') FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND UPPER(COALESCE(outcome,'OPEN')) NOT IN ('WIN','LOSS','CANCELLED')
            LIMIT 10
            """)
            rows = cur.fetchall()
            open_coins = [safe_str(r[0]).upper() for r in (rows or []) if r[0]]
    except Exception:
        return True, 0.0, ""
    if not open_coins:
        return True, 0.0, ""
    coin_base = symbol.replace("USDT", "").replace("BUSD", "")
    _CORRELATIE_CACHE[coin_base] = closes[-50:] if len(closes) >= 50 else closes
    max_corr   = 0.0
    corr_coin  = ""
    for open_coin in open_coins:
        open_closes = _CORRELATIE_CACHE.get(open_coin, [])
        if not open_closes:
            continue
        corr = bereken_pearson_correlatie(closes, open_closes)
        if corr > max_corr:
            max_corr  = corr
            corr_coin = open_coin
    if max_corr > max_drempel:
        log(f"Correlatie filter {symbol} ↔ {corr_coin}: r={max_corr:.3f} > {max_drempel:.2f}")
        _SESSIE["correlatie_skip"] = _SESSIE.get("correlatie_skip", 0) + 1
        return False, max_corr, corr_coin
    return True, max_corr, corr_coin


def update_coin_blacklist_whitelist(conn) -> None:
    """v4.0: Automatische blacklist en whitelist update op basis van recente performance."""
    laatste_update_str = get_bot_state_value(conn, "bl_wl_update_tijd", "")
    if laatste_update_str:
        try:
            laatste = datetime.fromisoformat(laatste_update_str)
            if laatste.tzinfo is None:
                laatste = laatste.replace(tzinfo=timezone.utc)
            uren_geleden = (now_utc() - laatste).total_seconds() / 3600
            if uren_geleden < AUTO_BL_INTERVAL_UREN:
                return
        except Exception:
            pass
    log(f"Blacklist/whitelist automatisch bijwerken...")
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                UPPER(COALESCE(coin, symbol, '')) AS coin_naam,
                COUNT(*) AS n,
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN') AS wins,
                ROUND(COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')::numeric
                      / NULLIF(COUNT(*),0), 4) AS win_rate
            FROM public.experience_trades
            WHERE UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND UPPER(COALESCE(coin, symbol, '')) != ''
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '90 days'
            GROUP BY 1
            HAVING COUNT(*) >= %s
            """, (min(BLACKLIST_MIN_TRADES, WHITELIST_MIN_TRADES),))
            rijen = cur.fetchall()
        huidige_bl = set()
        huidige_wl = set()
        try:
            bl_raw = get_bot_state_value(conn, "coin_blacklist", "[]")
            wl_raw = get_bot_state_value(conn, "coin_whitelist", "[]")
            huidige_bl = set(json.loads(bl_raw))
            huidige_wl = set(json.loads(wl_raw))
        except Exception:
            pass
        nieuwe_bl = set(huidige_bl)
        nieuwe_wl = set(huidige_wl)
        bl_toegevoegd = []
        wl_toegevoegd = []
        bl_verwijderd = []
        wl_verwijderd = []
        for rij in (rijen or []):
            coin_naam = safe_str(rij[0])
            n         = safe_int(rij[1])
            win_rate  = safe_float(rij[3])
            if not coin_naam or coin_naam in ("BTC", "BTCUSDT"):
                continue
            if n >= BLACKLIST_MIN_TRADES and win_rate < BLACKLIST_MAX_WINRATE:
                if coin_naam not in nieuwe_bl:
                    nieuwe_bl.add(coin_naam)
                    bl_toegevoegd.append(f"{coin_naam}({pct_str(win_rate)})")
                if coin_naam in nieuwe_wl:
                    nieuwe_wl.discard(coin_naam)
                    wl_verwijderd.append(coin_naam)
            elif n >= WHITELIST_MIN_TRADES and win_rate > WHITELIST_MIN_WINRATE:
                if coin_naam not in nieuwe_wl:
                    nieuwe_wl.add(coin_naam)
                    wl_toegevoegd.append(f"{coin_naam}({pct_str(win_rate)})")
                if coin_naam in nieuwe_bl:
                    nieuwe_bl.discard(coin_naam)
                    bl_verwijderd.append(coin_naam)
            elif coin_naam in nieuwe_bl and win_rate >= BLACKLIST_MAX_WINRATE + 0.10:
                nieuwe_bl.discard(coin_naam)
                bl_verwijderd.append(coin_naam)
        set_bot_state_value(conn, "coin_blacklist",    json.dumps(sorted(nieuwe_bl)))
        set_bot_state_value(conn, "coin_whitelist",    json.dumps(sorted(nieuwe_wl)))
        set_bot_state_value(conn, "bl_wl_update_tijd", now_utc().isoformat())
        if bl_toegevoegd:
            log(f"Blacklist +{len(bl_toegevoegd)}: {', '.join(bl_toegevoegd[:5])}")
            _SESSIE["bl_updates"] += len(bl_toegevoegd)
        if wl_toegevoegd:
            log(f"Whitelist +{len(wl_toegevoegd)}: {', '.join(wl_toegevoegd[:5])}")
            _SESSIE["wl_updates"] += len(wl_toegevoegd)
        if bl_verwijderd:
            log(f"Blacklist -{len(bl_verwijderd)}: {', '.join(bl_verwijderd[:5])}")
        if wl_verwijderd:
            log(f"Whitelist -{len(wl_verwijderd)}: {', '.join(wl_verwijderd[:5])}")
        log(f"BL/WL update klaar: {len(nieuwe_bl)} blacklisted, {len(nieuwe_wl)} whitelisted")
    except Exception as e:
        safe_rollback(conn)
        log(f"BL/WL update fout: {e}")


def get_edge_decay_coin(conn, symbol: str) -> Dict[str, Any]:
    """v4.0: Edge decay detectie per coin. Verschil sim vs live WR > 15% = decay."""
    result = {
        "sim_wr": 0.0, "live_wr": 0.0, "verschil": 0.0,
        "edge_decay": False, "n_sim": 0, "n_live": 0,
    }
    coin_base = symbol.replace("USDT", "").replace("BUSD", "")
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT UPPER(COALESCE(source,'')) AS src,
                   COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE UPPER(outcome)='WIN') AS wins
            FROM public.experience_trades
            WHERE UPPER(COALESCE(coin,'')) = UPPER(%s)
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '30 days'
            GROUP BY 1
            """, (coin_base,))
            rijen = cur.fetchall()
        data: Dict[str, Tuple[int, int]] = {}
        for rij in (rijen or []):
            src = safe_str(rij[0])
            n   = safe_int(rij[1])
            w   = safe_int(rij[2])
            key = "LIVE" if src in ("REAL", "LIVE") else src
            data[key] = data.get(key, (0, 0))
            data[key] = (data[key][0] + n, data[key][1] + w)
        sim_data  = data.get("SIM", (0, 0))
        live_data = data.get("LIVE", (0, 0))
        if sim_data[0] >= 5:
            result["sim_wr"] = round(sim_data[1] / sim_data[0], 4)
            result["n_sim"]  = sim_data[0]
        if live_data[0] >= 5:
            result["live_wr"] = round(live_data[1] / live_data[0], 4)
            result["n_live"]  = live_data[0]
        if result["n_sim"] >= 5 and result["n_live"] >= 5:
            result["verschil"]   = round(result["sim_wr"] - result["live_wr"], 4)
            result["edge_decay"] = result["verschil"] > 0.15
    except Exception as e:
        log(f"Edge decay check fout ({symbol}): {e}")
    return result


def bereken_kelly_grootte(win_rate: float, avg_win_r: float = 2.5,
                           avg_loss_r: float = 1.0,
                           max_eur: float = MAX_KELLY_EUR,
                           min_eur: float = MIN_KELLY_EUR,
                           kelly_fractie: float = KELLY_FRACTIE) -> float:
    """v4.0: Kelly Criterion voor optimale positiegrootte."""
    if win_rate <= 0 or avg_win_r <= 0:
        return min_eur
    b       = avg_win_r / avg_loss_r if avg_loss_r > 0 else avg_win_r
    kelly_f = (win_rate * b - (1 - win_rate)) / b
    kelly_f = max(0.0, kelly_f)
    kelly_f = kelly_f * kelly_fractie
    bankroll = DAILY_STOP_LOSS_EUR * 2
    kelly_eur = bankroll * kelly_f
    kelly_eur = max(min_eur, min(max_eur, kelly_eur))
    kelly_eur = min(kelly_eur, MAX_PER_TRADE_EUR * 2)
    return round(kelly_eur, 2)


def bepaal_coin_cluster(exp_n: int, win_rate: float,
                         recent_trades: int = 0,
                         edge_decay: bool = False) -> str:
    """v4.0: Coin clustering — STAR/STABLE/WEAK/ZOMBIE."""
    if edge_decay:
        return "ZOMBIE"
    if exp_n >= 20 and win_rate >= 0.60:
        return "STAR"
    elif exp_n >= 10 and win_rate >= 0.45:
        return "STABLE"
    elif exp_n >= 10 and win_rate < 0.35:
        return "ZOMBIE"
    else:
        return "WEAK"


def bepaal_markt_sessie_timing() -> str:
    """v4.0: Bepaalt in welke markt sessie we zitten (EU/US/AZIATISCH/OVERLAP)."""
    uur = now_utc().hour
    if 13 <= uur <= 16:
        return "EU_US_OVERLAP"
    if 7 <= uur <= 8:
        return "ASIA_EU_OVERLAP"
    if 0 <= uur < 8:
        return "AZIATISCH"
    elif 8 <= uur < 13:
        return "EUROPEES"
    elif 13 <= uur < 22:
        return "AMERIKAANS"
    else:
        return "NACHT"


def get_sessie_timing_bonus(sessie: str) -> int:
    """v4.0: Score bonus op basis van markt sessie timing."""
    bonussen = {
        "EU_US_OVERLAP":  SESSIE_BONUS_EU + SESSIE_BONUS_US,
        "ASIA_EU_OVERLAP": SESSIE_BONUS_EU,
        "EUROPEES":       SESSIE_BONUS_EU,
        "AMERIKAANS":     SESSIE_BONUS_US,
        "AZIATISCH":      SESSIE_BONUS_ASIA,
        "NACHT":          0,
        "ONBEKEND":       0,
    }
    return bonussen.get(sessie, 0)


def bereken_score_gradient_efficiency(conn) -> Dict[str, Any]:
    """v4.0: Score gradient efficiency analyse per score bucket."""
    result: Dict[str, Any] = {
        "buckets": [], "aanbeveling": "", "timestamp": now_utc().isoformat(),
    }
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                FLOOR(COALESCE(score, 0) / 5) * 5 AS score_bucket,
                COUNT(*) AS n,
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN') AS wins,
                ROUND(COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')::numeric
                      / NULLIF(COUNT(*), 0) * 100, 1) AS win_rate_pct,
                ROUND(AVG(COALESCE(pnl_eur, 0))::numeric, 4) AS avg_pnl
            FROM public.experience_trades
            WHERE UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND score IS NOT NULL AND score >= 60
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '60 days'
            GROUP BY 1 ORDER BY 1
            """)
            rijen = cur.fetchall()
        for rij in (rijen or []):
            result["buckets"].append({
                "score_van": int(safe_float(rij[0])),
                "score_tot": int(safe_float(rij[0]) + 5),
                "n_trades":  safe_int(rij[1]),
                "wins":      safe_int(rij[2]),
                "win_rate":  safe_float(rij[3]),
                "avg_pnl_eur": safe_float(rij[4]),
            })
    except Exception as e:
        log(f"Score gradient fout: {e}")
    return result


def bouw_uitgebreide_scan_samenvatting(conn, sessie: Dict,
                                        drempels: Dict,
                                        btc_regime: str,
                                        btc_sterkte: float) -> str:
    """v4.0: Bouwt een uitgebreide WhatsApp scan samenvatting."""
    if not sessie.get("start"):
        return ""
    duur = (now_utc() - sessie["start"]).total_seconds() / 60
    clusters = sessie.get("coin_clusters", {})
    top5 = sorted(sessie.get("coins_met_score", []),
                  key=lambda x: x["score"], reverse=True)[:5]
    filters = sessie.get("gefilterd", {})
    top_filters = sorted(filters.items(), key=lambda x: x[1], reverse=True)[:5]
    filter_txt = " | ".join(f"{k}:{v}" for k, v in top_filters)
    top_txt = ""
    for i, c in enumerate(top5, 1):
        top_txt += f"  #{i} {c.get('symbol','?')} score={c.get('score',0)}\n"
    claude_txt = ""
    if ANTHROPIC_API_KEY:
        claude_txt = claude_analyseer_sessie(sessie)
    bericht = (
        f"SCAN RAPPORT — {now_utc().strftime('%H:%M UTC')}\n"
        f"{'='*32}\n"
        f"BTC: {btc_regime} ({btc_sterkte:.0f}%) | "
        f"Sessie: {sessie.get('sessie_timing','?')}\n"
        f"Drempel: {sessie.get('score_drempel',0)} "
        f"(BULL={drempels.get('score_bull',0)} "
        f"RANGE={drempels.get('score_range',0)} "
        f"BEAR={drempels.get('score_bear',0)})\n\n"
        f"SCAN:\n"
        f"  Gescand:  {sessie.get('gescand',0)} coins\n"
        f"  Signalen: {sessie.get('signalen',0)}\n"
        f"  Live:     {sessie.get('live_trades',0)} | "
        f"Shadow: {sessie.get('shadow_trades',0)}\n"
        f"  Duur:     {duur:.1f} min\n\n"
        f"FILTERS:\n"
        f"  {filter_txt}\n"
        f"  Correlatie skip: {sessie.get('correlatie_skip',0)}\n\n"
        f"CLUSTERS:\n"
        f"  STAR:{clusters.get('STAR',0)} "
        f"STABLE:{clusters.get('STABLE',0)} "
        f"WEAK:{clusters.get('WEAK',0)} "
        f"ZOMBIE:{clusters.get('ZOMBIE',0)}\n\n"
        f"BL/WL UPDATE:\n"
        f"  +Blacklist:{sessie.get('bl_updates',0)} "
        f"+Whitelist:{sessie.get('wl_updates',0)}\n\n"
    )
    if top5:
        bericht += f"TOP SIGNALEN:\n{top_txt}\n"
    if claude_txt:
        bericht += f"Claude: {claude_txt[:200]}\n"
    bericht += "Commands: STATUS | TRADES | STOP"
    return bericht


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
    """Sterkte als abs((close-ema200)/ema200*100)."""
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
    """Detecteert of BTC regime verbetert of verslechtert."""
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
    Setup types: SQUEEZE_BREAK, MARKT_STRUCTUUR_BREAKOUT, BREAKOUT,
    BULLISH_DIVERGENCE, TREND_PULLBACK, VWAP_BOUNCE, VOLUME_ZONE_BOUNCE,
    BOUNCE, OVERSOLD_RECLAIM, MOMENTUM.
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
    if detecteer_squeeze(closes_4h, candles_4h):
        if current > vorige * 1.005:
            return "SQUEEZE_BREAK", f"squeeze|RSI={rsi_4h:.0f}"
    ms = detecteer_markt_structuur(closes_4h, candles_4h, 20)
    if ms == "BULLISH_TREND" and current > vorige * 1.008:
        return "MARKT_STRUCTUUR_BREAKOUT", f"ms_bull_breakout|RSI={rsi_4h:.0f}"
    if len(candles_4h) >= 20:
        high_20    = max(c["high"] for c in candles_4h[-20:])
        prev_close = closes_4h[-2] if len(closes_4h) > 1 else current
        if current > high_20 * 0.998 and prev_close < high_20:
            return "BREAKOUT", f"break_high20({high_20:.4f})|RSI={rsi_4h:.0f}"
    div = detecteer_divergentie(closes_4h, candles_4h, 10)
    if div == "BULLISH" and current > vorige:
        return "BULLISH_DIVERGENCE", f"bull_div|RSI={rsi_4h:.0f}"
    if sma20_4h > sma50_4h and current > sma50_4h:
        dist_sma20 = abs(current - sma20_4h) / max(sma20_4h, 1e-10)
        if dist_sma20 < 0.025 and RSI_MIN <= rsi_4h <= 58:
            return "TREND_PULLBACK", f"sma20_pb({dist_sma20*100:.1f}%)|RSI={rsi_4h:.0f}"
    vwap_val = vwap(candles_4h, 20)
    if vwap_val and current > vwap_val and vorige < vwap_val and rsi_4h < 55:
        return "VWAP_BOUNCE", f"vwap_cross({vwap_val:.4f})|RSI={rsi_4h:.0f}"
    vol_zone_low, vol_zone_high = bereken_volume_zone(candles_4h, 10)
    if vol_zone_low > 0:
        if vol_zone_low <= current <= vol_zone_high * 1.02 and current > vorige:
            taker_ratio = bereken_taker_buy_ratio(candles_4h, 10)
            if taker_ratio > 0.52:
                return "VOLUME_ZONE_BOUNCE", (
                    f"vol_zone({vol_zone_low:.4f}-{vol_zone_high:.4f})"
                    f"|taker={taker_ratio:.2f}|RSI={rsi_4h:.0f}"
                )
    if sma50_4h > 0:
        dist_sma50 = abs(current - sma50_4h) / max(sma50_4h, 1e-10)
        if dist_sma50 < 0.02 and rsi_4h < 52:
            return "BOUNCE", f"sma50_bounce({dist_sma50*100:.1f}%)|RSI={rsi_4h:.0f}"
    if rsi_4h < 30 and current > vorige * 1.002:
        return "OVERSOLD_RECLAIM", f"oversold_bounce|RSI={rsi_4h:.0f}"
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
                    funding_rate: float = 0.0,
                    markt_structuur: str = "ONBEKEND",
                    taker_ratio: float = 0.5,
                    sessie_timing: str = "ONBEKEND",
                    coin_cluster: str = "STABLE") -> Tuple[int, int, int, str, float, float]:
    """
    v4.1: Berekent score (0-100), chance, confidence.
    Bevat TREND_PULLBACK bonus +5 op basis van shadow data (67% win rate).
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

    # ── BONUSSEN v3.0 ─────────────────────────────────────
    macd_line, signal_line, _ = macd(closes_4h)
    if macd_line > signal_line and macd_line > 0:
        score = min(score + 5, 110); why_tags.append("MACD=bullish")
    elif macd_line < signal_line and macd_line < 0:
        score = max(score - 3, 0);   why_tags.append("MACD=bearish")

    if detecteer_squeeze(closes_4h, candles_4h):
        score = min(score + 3, 110); why_tags.append("SQUEEZE")

    mom = detecteer_momentum(closes_4h, 10)
    if mom > 5:
        score = min(score + 4, 110); why_tags.append(f"MOM=+{mom:.1f}%")
    elif mom < -5:
        score = max(score - 2, 0);   why_tags.append(f"MOM={mom:.1f}%")

    if vwap_positie == "BOVEN":
        score = min(score + 3, 110); why_tags.append("VWAP=boven")
    elif vwap_positie == "ONDER":
        score = max(score - 3, 0);   why_tags.append("VWAP=onder")

    if stoch_rsi_val is not None:
        if stoch_rsi_val < 30:
            score = min(score + 3, 110); why_tags.append(f"StochRSI={stoch_rsi_val:.0f}oversold")
        elif stoch_rsi_val > 80:
            score = max(score - 2, 0);   why_tags.append(f"StochRSI={stoch_rsi_val:.0f}overbought")

    if divergentie == "BULLISH":
        score = min(score + 4, 110); why_tags.append("DIV=bullish")
    elif divergentie == "BEARISH":
        score = max(score - 4, 0);   why_tags.append("DIV=bearish")

    # ── BONUSSEN v4.0 ─────────────────────────────────────
    if taker_ratio > 0.60:
        score = min(score + 5, 110)
        why_tags.append(f"taker={taker_ratio:.2f}STERK")
        _SESSIE["taker_bonus"] = _SESSIE.get("taker_bonus", 0) + 1
    elif taker_ratio > 0.55:
        score = min(score + 3, 110)
        why_tags.append(f"taker={taker_ratio:.2f}OK")
    elif taker_ratio < 0.40:
        score = max(score - 3, 0)
        why_tags.append(f"taker={taker_ratio:.2f}VERKOOP")

    if markt_structuur == "BULLISH_TREND":
        score = min(score + 4, 110)
        why_tags.append("MS=BULL_HH_HL")
        _SESSIE["markt_structuur_ok"] = _SESSIE.get("markt_structuur_ok", 0) + 1
    elif markt_structuur == "BEARISH_TREND":
        score = max(score - 5, 0)
        why_tags.append("MS=BEAR_LH_LL")

    sessie_bonus = get_sessie_timing_bonus(sessie_timing)
    if sessie_bonus > 0:
        score = min(score + sessie_bonus, 110)
        why_tags.append(f"sessie={sessie_timing}+{sessie_bonus}")

    if coin_cluster == "STAR":
        score = min(score + 3, 110); why_tags.append("cluster=STAR")
    elif coin_cluster == "ZOMBIE":
        score = max(score - 10, 0); why_tags.append("cluster=ZOMBIE")
    elif coin_cluster == "WEAK":
        why_tags.append("cluster=WEAK")

    # ── BONUS v4.1: TREND_PULLBACK ────────────────────────
    # Basis: shadow data toont 67% win rate in RANGE, 53.6% in BULL.
    # Dit is de bewezen beste setup — bonus geeft hem voorrang.
    if setup_type == "TREND_PULLBACK":
        score = min(score + 5, 110)
        why_tags.append("TP_BONUS+5")

    # ── MALUSSEN ─────────────────────────────────────────
    fee_impact = TOTAL_COST_PCT * 100
    if fee_impact > 0.3:
        score = max(0, score - 3); why_tags.append(f"fee={fee_impact:.2f}%")

    vol_label, atr_pct = detect_volatiliteit(candles_4h, current)
    if vol_label == "EXTREEM":
        score = max(0, score - 5); why_tags.append(f"vol=EXTREEM({atr_pct*100:.1f}%)")
    elif vol_label == "LAAG":
        score = max(0, score - 2); why_tags.append("vol=LAAG")

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

    why_tag = " | ".join(why_tags[:12])
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
    """Uitgebreide coin statistieken uit experience_trades."""
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
                    SUM(CASE WHEN UPPER(outcome)='WIN' THEN ABS(COALESCE(pnl_eur,0)) ELSE 0 END)::numeric /
                    NULLIF(SUM(CASE WHEN UPPER(outcome)='LOSS' THEN ABS(COALESCE(pnl_eur,0)) ELSE 0 END),0.001)::numeric,
                1.0),2) AS pf,
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


def get_edge_decay_coins_vandaag(conn) -> List[str]:
    """v4.0: Haalt coins op waarvoor edge decay gedetecteerd is (30 dagen)."""
    decay_coins: List[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                UPPER(COALESCE(coin, symbol, '')) AS cn,
                SUM(CASE WHEN UPPER(COALESCE(source,'')) IN ('REAL','LIVE') THEN 1 ELSE 0 END) AS n_live,
                SUM(CASE WHEN UPPER(COALESCE(source,'')) = 'SIM' THEN 1 ELSE 0 END) AS n_sim,
                AVG(CASE WHEN UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
                         AND UPPER(outcome)='WIN' THEN 1.0 ELSE 0.0 END) AS live_wr,
                AVG(CASE WHEN UPPER(COALESCE(source,'')) = 'SIM'
                         AND UPPER(outcome)='WIN' THEN 1.0 ELSE 0.0 END) AS sim_wr
            FROM public.experience_trades
            WHERE UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '30 days'
            GROUP BY 1
            HAVING SUM(CASE WHEN UPPER(COALESCE(source,'')) IN ('REAL','LIVE') THEN 1 ELSE 0 END) >= 5
               AND SUM(CASE WHEN UPPER(COALESCE(source,'')) = 'SIM' THEN 1 ELSE 0 END) >= 5
            """)
            rijen = cur.fetchall()
            for rij in (rijen or []):
                coin    = safe_str(rij[0])
                live_wr = safe_float(rij[3])
                sim_wr  = safe_float(rij[4])
                if sim_wr - live_wr > 0.15:
                    decay_coins.append(coin)
    except Exception:
        pass
    return decay_coins


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
    """Markeert verlopen pending signalen als EXPIRED."""
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
    """Maakt pending_approvals tabel aan als die niet bestaat."""
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 0")
        conn.commit()
    except Exception:
        safe_rollback(conn)
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
        safe_rollback(conn)
        log(f"Tabel check fout: {e}")
    try:
        migraties = [
            ("score_details",       "JSONB"),
            ("vwap_positie",        "TEXT"),
            ("divergentie",         "TEXT"),
            ("funding_rate",        "DOUBLE PRECISION DEFAULT 0.0"),
            ("live_toegestaan",     "BOOLEAN DEFAULT FALSE"),
            ("updated_at",          "TIMESTAMPTZ DEFAULT NOW()"),
            ("btc_regime",          "TEXT"),
            ("rr_ratio",            "DOUBLE PRECISION"),
            ("exp_bias",            "TEXT DEFAULT 'NEUTRAL'"),
            ("markt_structuur",     "TEXT"),
            ("taker_ratio",         "DOUBLE PRECISION DEFAULT 0.5"),
            ("coin_cluster",        "TEXT DEFAULT 'STABLE'"),
            ("kelly_grootte_eur",   "DOUBLE PRECISION DEFAULT 0.50"),
            ("correlatie_max",      "DOUBLE PRECISION DEFAULT 0.0"),
            ("edge_decay",          "BOOLEAN DEFAULT FALSE"),
            ("sessie_timing",       "TEXT"),
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
    """Voegt Pre-BUY signaal in pending_approvals. v4.0: extra velden."""
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
                vwap_positie, divergentie, funding_rate, live_toegestaan,
                markt_structuur, taker_ratio, coin_cluster,
                kelly_grootte_eur, correlatie_max, edge_decay, sessie_timing
            ) VALUES (
                %s,%s,%s,%s,'4H',%s,%s,
                %s,'GO',%s,%s,%s,%s,%s,
                %s,%s,%s,%s,
                %s,%s,%s,%s,%s,
                NOW(),'PENDING',%s::jsonb,
                %s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s
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
                {"NEUTRAL":0,"LONG":1,"SHORT":-1}.get(str(prebuy.get("exp_bias","NEUTRAL")),0), prebuy.get("why_tag",""),
                prebuy.get("claude_beoordeling",""),
                json.dumps(prebuy.get("score_details",{})),
                prebuy.get("vwap_positie","ONBEKEND"),
                prebuy.get("divergentie","GEEN"),
                prebuy.get("funding_rate", 0.0),
                prebuy.get("live_toegestaan", False),
                prebuy.get("markt_structuur", "ONBEKEND"),
                prebuy.get("taker_ratio", 0.5),
                prebuy.get("coin_cluster", "STABLE"),
                prebuy.get("kelly_grootte_eur", MAX_PER_TRADE_EUR),
                prebuy.get("correlatie_max", 0.0),
                prebuy.get("edge_decay", False),
                prebuy.get("sessie_timing", "ONBEKEND"),
            ))
        conn.commit()
        log(f"Pre-BUY: {prebuy['symbol']} score={prebuy['score']} "
            f"setup={prebuy['setup_type']} cluster={prebuy.get('coin_cluster','?')} "
            f"kelly=€{prebuy.get('kelly_grootte_eur', MAX_PER_TRADE_EUR):.2f} "
            f"LIVE={'JA' if prebuy.get('live_toegestaan') else 'NEE'} "
            f"id={prebuy_id[:8]}")
        return prebuy_id
    except Exception as e:
        log(f"insert_pending fout ({prebuy['symbol']}): {e}")
        try: conn.rollback()
        except Exception: pass
        return ""


def trigger_auto_buy(prebuy_id: str) -> bool:
    """Triggert /auto_buy op whatsapp_webhook.py."""
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
# DAGELIJKS RAPPORT
# ============================================================
def is_rapport_tijd() -> bool:
    nu = now_utc()
    return nu.hour == RAPPORT_HOUR_UTC and nu.minute < 15


def is_rapport_al_verstuurd(conn) -> bool:
    return get_bot_state_value(conn, "dagrapport_datum", "") == utc_day_str()


def verstuur_dagrapport(conn) -> None:
    """v4.0: Uitgebreider dagrapport met edge decay en cluster info."""
    if not is_rapport_tijd() or is_rapport_al_verstuurd(conn):
        return
    log("Dagrapport aanmaken...")
    try:
        btc_regime        = get_btc_regime(conn)
        btc_sterkte       = get_btc_sterkte(conn)
        dagpnl            = get_daily_pnl(conn)
        wins, losses      = get_daily_win_loss(conn)
        dag_trades        = get_daily_trade_count(conn)
        shadow_cnt        = get_daily_shadow_count(conn)
        top_coins         = get_top_signalen_vandaag(conn)
        edge_decay_coins  = get_edge_decay_coins_vandaag(conn)
        claude_txt = claude_dagrapport(dag_trades, shadow_cnt, dagpnl,
                                        wins, losses, btc_regime, top_coins,
                                        edge_decay_coins)
        totaal = wins + losses
        wr = wins / totaal if totaal > 0 else 0.0
        rapport = (
            f"DAGRAPPORT — {utc_day_str()}\n"
            f"{'='*30}\n"
            f"BTC: {btc_regime} ({btc_sterkte:.0f}%)\n"
            f"Live: {dag_trades} | W:{wins} L:{losses} WR:{pct_str(wr)}\n"
            f"Shadow: {shadow_cnt} | PnL: {eur_str(dagpnl)}\n"
        )
        if top_coins:
            rapport += "\nBeste signalen:\n"
            for c in top_coins[:3]:
                rapport += f"  {c.get('symbol','')} score={c.get('score',0)} ({c.get('setup_type','')})\n"
        if edge_decay_coins:
            rapport += f"\nEDGE DECAY: {', '.join(edge_decay_coins[:3])}\n"
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
# HEALTH MONITORING
# ============================================================
def voer_health_check_uit(conn) -> Dict[str, Any]:
    """Uitgebreide health check van scanner en systeem."""
    health: Dict[str, Any] = {
        "database": False, "bitvavo_api": False, "binance_api": False,
        "claude_api": False, "webhook": False, "btc_data_vers": False,
        "candles_vers": False, "problemen": [], "score": 0,
    }
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            health["database"] = True
    except Exception as e:
        health["problemen"].append(f"DB fout: {e}")
    try:
        resp = requests.get(f"{BITVAVO_BASE}/v2/markets", timeout=10)
        health["bitvavo_api"] = resp.ok
        if not resp.ok:
            health["problemen"].append(f"Bitvavo status {resp.status_code}")
    except Exception as e:
        health["problemen"].append(f"Bitvavo onbereikbaar: {e}")
    try:
        resp = requests.get(f"{BINANCE_BASE}/ping", timeout=5)
        health["binance_api"] = resp.ok
        if not resp.ok:
            health["problemen"].append(f"Binance status {resp.status_code}")
    except Exception as e:
        health["problemen"].append(f"Binance onbereikbaar: {e}")
    if ANTHROPIC_API_KEY:
        test = _claude_analyse("Zeg alleen OK.", 10)
        health["claude_api"] = bool(test)
        if not test:
            health["problemen"].append("Claude API reageert niet")
    else:
        health["problemen"].append("ANTHROPIC_API_KEY niet ingesteld")
    if WEBHOOK_BASE_URL:
        try:
            resp = requests.get(f"{WEBHOOK_BASE_URL}/health", timeout=10)
            health["webhook"] = resp.ok
        except Exception:
            health["problemen"].append("Webhook onbereikbaar")
    else:
        health["problemen"].append("WEBHOOK_BASE_URL niet ingesteld")
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
        pass
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

    v4.1 FIXES IN DEZE FUNCTIE:
    - BREAKOUT_RETEST in BEAR wordt geblokkeerd (28.9% win rate)
    - TREND_PULLBACK krijgt +5 score bonus (67% win rate)
    - Dashboard controls (min score, trading hours) werken correct
    """
    bot_actief = is_bot_active(conn)
    bot_gepauz = is_bot_paused(conn)
    live_ok    = bot_actief and not bot_gepauz

    if not bot_actief:
        log("Bot GESTOPT — scanner zoekt 24/7 door voor shadow trades")
    elif bot_gepauz:
        reden = get_bot_state_value(conn, "bot_paused_reason", "onbekend")
        log(f"Bot GEPAUZEERD ({reden}) — shadow trades blijven actief")

    if live_ok and not is_trading_hours():
        log(f"Buiten trading hours — geen LIVE, shadow gaat door")
        live_ok = False

    if live_ok and SKIP_WEEKEND and is_weekend():
        log("Weekend — LIVE overgeslagen, shadow gaat door")
        live_ok = False

    if live_ok:
        dagpnl = get_daily_pnl(conn)
        if dagpnl <= -DAILY_STOP_LOSS_EUR:
            log(f"Dagbudget bereikt ({eur_str(dagpnl)}) — geen LIVE meer")
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

    prebuy_today = get_prebuy_count_today(conn)
    if prebuy_today >= MAX_PREBUY_PER_DAY:
        log(f"Pre-buy daglimiet bereikt: {prebuy_today}/{MAX_PREBUY_PER_DAY}")
        return 0

    btc_regime  = get_btc_regime(conn)
    btc_sterkte = get_btc_sterkte(conn)
    btc_trend   = get_btc_trend_richting(conn)
    _SESSIE["btc_regime"] = btc_regime

    if btc_regime == "BEAR" and BTC_SKIP_BEAR and live_ok:
        log("BTC BEAR — geen LIVE trades, shadow gaat door")
        live_ok = False

    score_drempel = get_score_drempel_voor_regime(btc_regime, drempels)
    _SESSIE["score_drempel"] = score_drempel

    sessie_timing = _SESSIE.get("sessie_timing", bepaal_markt_sessie_timing())

    log(f"BTC: {btc_regime} ({btc_sterkte:.0f}%) trend={btc_trend} | "
        f"Drempel: {score_drempel} | LIVE: {'JA' if live_ok else 'NEE'} | "
        f"Sessie: {sessie_timing} | TIJD: {now_utc().strftime('%H:%M')} UTC")

    tradable = get_tradable_markets()
    if not tradable:
        log("Geen tradable markets gevonden — scanner stopt")
        return 0

    scan_pairs: List[Tuple[str, str]] = []
    for market in tradable:
        if market.endswith("-EUR"):
            base = market[:-4]
            scan_pairs.append((f"{base}USDT", market))

    log(f"Scannen: {len(scan_pairs)} pairs | drempel: {score_drempel} | sessie: {sessie_timing}")

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

            # FIX v4.1: BREAKOUT_RETEST in BEAR blokkeren
            # Shadow data: 149 trades, 28.9% win rate = verliesgevend.
            # In een bearish BTC markt mislukt bijna elke uitbraak poging.
            # TREND_PULLBACK werkt wel goed (67% win rate) maar BREAKOUT niet.
            # Dit filter bespaart onnodige verliezen bij slechte marktomstandigheden.
            if "BREAKOUT" in setup_type or "RETEST" in setup_type:
                tel_filter("breakout_geblokkeerd")
                update_sessie(symbol_usdt, 0, "breakout_geblokkeerd")
                continue

            # ── Experience ophalen ─────────────────────────
            exp_win_rate, exp_n, exp_bias = get_experience(
                conn, symbol_usdt, setup_type, coin_regime)

            # ── v3.0: Extra indicatoren ────────────────────
            vwap_pos    = bereken_vwap_positie(candles_4h, current)
            stoch_rsi   = stochastic_rsi(closes_4h, RSI_PERIOD, 14)
            divergentie = detecteer_divergentie(closes_4h, candles_4h, 10)

            # ── v4.0: Taker buy ratio ─────────────────────
            taker_ratio = bereken_taker_buy_ratio(candles_4h, 14)

            # ── v4.0: Markt structuur ─────────────────────
            markt_structuur = detecteer_markt_structuur(closes_4h, candles_4h, 20)
            if markt_structuur == "BEARISH_TREND" and btc_regime != "BULL":
                tel_filter("ms_bear"); update_sessie(symbol_usdt, 0, "ms_bear"); continue

            # ── v4.0: Edge decay check ────────────────────
            edge_info  = get_edge_decay_coin(conn, symbol_usdt)
            edge_decay = edge_info.get("edge_decay", False)

            # ── v4.0: Coin clustering ─────────────────────
            coin_cluster = bepaal_coin_cluster(exp_n, exp_win_rate,
                                                edge_decay=edge_decay)
            _SESSIE["coin_clusters"][coin_cluster] = \
                _SESSIE["coin_clusters"].get(coin_cluster, 0) + 1

            if coin_cluster == "ZOMBIE" and live_ok:
                tel_filter("zombie_coin")
                log(f"ZOMBIE coin {symbol_usdt} — shadow only")

            # ── Funding rate filter ────────────────────────
            funding_ok, funding_rate = check_funding_rate_ok(symbol_usdt)
            if not funding_ok:
                tel_filter("funding_extreem")
                update_sessie(symbol_usdt, 0, "funding_extreem")
                log(f"Funding te extreem {symbol_usdt}: {funding_rate*100:.4f}%")
                continue

            # ── Ticker ────────────────────────────────────
            ticker = fetch_ticker_24h(symbol_usdt)

            # ── Score berekening ───────────────────────────
            score, chance, confidence, why_tag, rsi_4h, vol_ratio = calculate_score(
                candles_4h, candles_1h, ticker, coin_regime, btc_regime,
                btc_sterkte, setup_type, exp_win_rate, exp_n, drempels,
                vwap_pos, stoch_rsi, divergentie, funding_rate,
                markt_structuur, taker_ratio, sessie_timing, coin_cluster,
            )

            # Whitelist bonus
            if is_coin_whitelisted(conn, symbol_usdt):
                score = min(score + 5, 100)
                why_tag += " | WHITELIST"

            update_sessie(symbol_usdt, score)

            # ── Regime-afhankelijke drempel check ─────────
            if score < score_drempel:
                tel_filter("score_laag"); continue
            if chance < min_chance:
                tel_filter("chance_laag"); continue
            if confidence < min_conf:
                tel_filter("conf_laag"); continue

            # ── v4.0: Correlatie filter ───────────────────
            corr_ok, max_corr, corr_coin = coin_correlatie_filter(
                conn, symbol_usdt, closes_4h, drempels)
            if not corr_ok:
                tel_filter("correlatie")
                update_sessie(symbol_usdt, 0, "correlatie")
                continue

            log(f"SIGNAAL {symbol_usdt}: score={score}/{score_drempel} "
                f"chance={chance}% conf={confidence}% setup={setup_type} "
                f"ms={markt_structuur} taker={taker_ratio:.2f} "
                f"cluster={coin_cluster} edge_decay={edge_decay} "
                f"LIVE={'JA' if live_ok and coin_cluster != 'ZOMBIE' else 'NEE'}")

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

            support, weerstand = detecteer_support_weerstand(candles_4h, 20)
            if support > 0 and stop < support * 0.98:
                stop = support * 0.99
            if weerstand > 0 and target > weerstand * 1.05:
                target = weerstand * 0.99

            vol_zone_low, vol_zone_high = bereken_volume_zone(candles_4h, 10)
            if vol_zone_low > 0 and stop < vol_zone_low:
                stop = max(stop, vol_zone_low * 0.995)

            kelly_eur = bereken_kelly_grootte(
                win_rate      = exp_win_rate if exp_n >= 10 else 0.50,
                avg_win_r     = tgt_eff,
                avg_loss_r    = 1.0,
                max_eur       = MAX_KELLY_EUR,
                min_eur       = MIN_KELLY_EUR,
                kelly_fractie = drempels.get("kelly_fractie", KELLY_FRACTIE),
            )
            _SESSIE["kelly_grootte"].append(kelly_eur)

            claude_txt = claude_beoordeel_signaal(
                symbol_usdt, setup_type, coin_regime, btc_regime,
                score, chance, confidence, rsi_4h, vol_ratio,
                exp_win_rate, exp_n, why_tag, funding_rate,
                markt_structuur, taker_ratio, coin_cluster,
            )

            coin_stats = get_coin_statistieken(conn, symbol_usdt)
            live_toegestaan = live_ok and coin_cluster != "ZOMBIE"

            prebuy = {
                "id":               str(uuid.uuid4()),
                "symbol":           symbol_usdt,
                "setup_type":       setup_type,
                "regime":           coin_regime,
                "btc_regime":       btc_regime,
                "score":            score,
                "chance":           chance,
                "confidence":       confidence,
                "entry":            current,
                "stop":             stop,
                "target":           target,
                "bitvavo_market":   bitvavo_market,
                "exp_n":            exp_n,
                "exp_win_rate":     exp_win_rate,
                "exp_bias":         exp_bias,
                "why_tag":          why_tag,
                "claude_beoordeling": claude_txt,
                "live_toegestaan":  live_toegestaan,
                "vwap_positie":     vwap_pos,
                "divergentie":      divergentie,
                "funding_rate":     funding_rate,
                "markt_structuur":  markt_structuur,
                "taker_ratio":      taker_ratio,
                "coin_cluster":     coin_cluster,
                "kelly_grootte_eur": kelly_eur,
                "correlatie_max":   max_corr,
                "edge_decay":       edge_decay,
                "sessie_timing":    sessie_timing,
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
                    "live_toegestaan":  live_toegestaan,
                    "atr_multiplier":   atr_eff,
                    "atr_target_r":     tgt_eff,
                    "markt_structuur":  markt_structuur,
                    "taker_ratio":      taker_ratio,
                    "coin_cluster":     coin_cluster,
                    "kelly_grootte_eur": kelly_eur,
                    "correlatie_max":   max_corr,
                    "correlatie_coin":  corr_coin,
                    "edge_decay":       edge_decay,
                    "edge_decay_info":  edge_info,
                    "sessie_timing":    sessie_timing,
                    "vol_zone_low":     vol_zone_low,
                    "vol_zone_high":    vol_zone_high,
                },
            }

            prebuy_id = insert_pending(conn, prebuy)
            if prebuy_id:
                prebuy_count += 1
                prebuy_today += 1
                if live_toegestaan:
                    _SESSIE["live_trades"] += 1
                else:
                    _SESSIE["shadow_trades"] += 1
                trigger_auto_buy(prebuy_id)

            if prebuy_today >= MAX_PREBUY_PER_DAY:
                log(f"Pre-buy daglimiet bereikt: {prebuy_today}")
                break

        except Exception as e:
            report_error(e, "scan_universe_coin", severity="MEDIUM", symbol=symbol_usdt)
            continue

        log_sessie_voortgang(idx + 1, len(scan_pairs))

    log(f"Scan klaar: {_SESSIE['gescand']} gescand | {prebuy_count} pre-buys | "
        f"Drempel: {score_drempel} (BTC={btc_regime}) | "
        f"Sessie: {sessie_timing} | Correlatie skip: {_SESSIE['correlatie_skip']}")
    return prebuy_count


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    log("=" * 65)
    log(f"Multi Coin Scorer v4.1 — {now_utc().strftime('%Y-%m-%d %H:%M UTC')}")
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
    log(f"Correlatie max:        {MAX_CORRELATIE_DREMPEL:.2f}")
    log(f"Kelly fractie:         {KELLY_FRACTIE:.2f} (max €{MAX_KELLY_EUR:.2f})")
    log(f"Auto BL interval:      {AUTO_BL_INTERVAL_UREN}u")
    log(f"Whitelist min WR:      {WHITELIST_MIN_WINRATE:.0%} na {WHITELIST_MIN_TRADES} trades")
    log(f"Markt sessie timing:   {bepaal_markt_sessie_timing()}")
    log("=" * 65)

    if not DATABASE_URL:
        log("KRITIEK: DATABASE_URL ontbreekt — scanner kan niet starten")
        sys.exit(1)

    conn = None
    try:
        conn = db_connect()
        log("Database verbonden")

        log("Health check uitvoeren...")
        health = voer_health_check_uit(conn)
        log_health_check(health)
        if health["score"] < 50:
            send_whatsapp(
                f"SCANNER WAARSCHUWING\n"
                f"Health: {health['score']}/100\n"
                f"Problemen: {' | '.join(health['problemen'][:3])}"
            )

        if ANTHROPIC_API_KEY:
            log("Claude scanner health check...")
            hc_txt = claude_scanner_health_check()
            if hc_txt:
                log(f"Claude health: {hc_txt}")

        zorg_voor_pending_tabel(conn)
        cleanup_verlopen_pending(conn)

        log("Auto blacklist/whitelist update check...")
        update_coin_blacklist_whitelist(conn)


        drempels = haal_coach_drempels_op(conn)


        init_sessie()

        btc         = get_btc_regime(conn)
        btc_sterkte = get_btc_sterkte(conn)
        btc_trend   = get_btc_trend_richting(conn)
        markets     = get_tradable_markets()
        log(f"BTC regime: {btc} ({btc_sterkte:.0f}%) trend={btc_trend}")
        log(f"Bitvavo markets: {len(markets)} tradable")
        log(f"Markt sessie: {_SESSIE['sessie_timing']}")

        verstuur_dagrapport(conn)

        n = scan_universe(conn, drempels)
        log(f"Resultaat: {n} pre-buys gegenereerd")

        sla_sessie_op(conn)
        sla_sessie_naar_tabel(conn)

        if ANTHROPIC_API_KEY and _SESSIE["gescand"] > 50:
            log("Score gradient efficiency berekenen...")
            gradient = bereken_score_gradient_efficiency(conn)
            if gradient.get("buckets"):
                set_bot_state_value(conn, "score_gradient", json.dumps(gradient))
                claude_gradient = claude_analyseer_score_gradient(
                    {b["score_van"]: b["win_rate"] for b in gradient["buckets"]}
                )
                if claude_gradient:
                    log(f"Claude gradient: {claude_gradient}")
                    set_bot_state_value(conn, "score_gradient_analyse", claude_gradient)

        if ANTHROPIC_API_KEY and _SESSIE["gescand"] > 10:
            analyse = claude_analyseer_sessie(_SESSIE)
            if analyse:
                log(f"Claude sessie: {analyse}")
                set_bot_state_value(conn, "laatste_scan_claude", analyse)

        if ANTHROPIC_API_KEY:
            markt = claude_beoordeel_marktomstandigheden(
                btc, n, _SESSIE["gescand"], now_utc().hour, btc_sterkte,
                _SESSIE["sessie_timing"]
            )
            if markt:
                log(f"Claude markt: {markt}")
                set_bot_state_value(conn, "markt_beoordeling", markt)

        if n >= 2 or _SESSIE["gescand"] > 100:
            samenvatting = bouw_uitgebreide_scan_samenvatting(
                conn, _SESSIE, drempels, btc, btc_sterkte)
            if samenvatting and n >= 2:
                send_whatsapp(samenvatting)
        elif n == 0 and _SESSIE["gescand"] > 50:
            log(f"0 signalen na {_SESSIE['gescand']} coins — geen WhatsApp")

        set_bot_state_value(conn, "scanner_actief",  "true")
        set_bot_state_value(conn, "scanner_versie",  "4.1")
        set_bot_state_value(conn, "scanner_health",  str(health["score"]))

        conn.close()
        log("Scanner v4.1 klaar")
        try:
            _conn = db_connect()
            set_bot_state(_conn, "scanner_last_run", now_utc().isoformat())
            _conn.close()
        except Exception as _e:
            log(f"scanner_last_run fout: {_e}")
        sys.exit(0)

    except KeyboardInterrupt:
        log("Scanner gestopt door gebruiker")
        sys.exit(0)
    except Exception as e:
        report_error(e, "__main__", severity="KRITIEK")
        sys.exit(1)

    finally:
        if conn:
            try:
                conn.close()
                log("DB verbinding gesloten")
            except Exception:
                pass
