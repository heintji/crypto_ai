# live_trader.py
# ============================================================
# Crypto AI Bot Ã¢ÂÂ Live Trader v3.0
# ============================================================
# Voert echte BUY en SELL orders uit op Bitvavo.
# Gebruikt Bitvavo API voor live orders, Binance voor data.
#
# V3.0 PATTERN Ã¢ÂÂ identiek aan alle andere bestanden:
#   Ã¢ÂÂ safe_rollback() overal
#   Ã¢ÂÂ db_connect() retries=3 + autocommit=False
#   Ã¢ÂÂ conn=None voor try/finally + finally conn.close()
#   Ã¢ÂÂ _bitvavo_request() geen retry op 4xx client errors
#   Ã¢ÂÂ isinstance(s, dict) check voor JSON state files
#   Ã¢ÂÂ WhatsApp rate limiting per fouttype
#   Ã¢ÂÂ Model: claude-sonnet-4-6
#   Ã¢ÂÂ log_naar_bot_state() schrijft naar bot_state tabel
#
# NIEUWE FEATURES V3.0:
#   Ã¢ÂÂ shadow_buy() Ã¢ÂÂ shadow trade parallel aan elke live BUY
#   Ã¢ÂÂ bereken_positiegrootte_kelly() Ã¢ÂÂ Half Kelly Criterion sizing
#   Ã¢ÂÂ validate_atr_stop() Ã¢ÂÂ ATR-based stop validatie
#   Ã¢ÂÂ log_naar_bot_state() Ã¢ÂÂ live_trader_busy/last_action/last_ts/error
#   Ã¢ÂÂ get_account_snapshot() Ã¢ÂÂ volledige Bitvavo portfolio snapshot
#   Ã¢ÂÂ check_min_order_size() Ã¢ÂÂ waarschuwt bij order onder Bitvavo minimum
#   Ã¢ÂÂ get_coin_stats() Ã¢ÂÂ win rate / R / profit factor per coin
#   Ã¢ÂÂ bereken_pnl_nauwkeurig() Ã¢ÂÂ PnL incl. fee_buy + fee_sell
#   Ã¢ÂÂ log_trade_event() Ã¢ÂÂ elke actie naar coach_events voor ai_coach
#   Ã¢ÂÂ load_shadow_state() / save_shadow_state() Ã¢ÂÂ shadow state helpers
#
# BUGS GEFIXED vs v2.0:
#   Ã¢ÂÂ HMAC signing Ã¢ÂÂ digestmod=hashlib.sha256
#   Ã¢ÂÂ get_tradable_markets() publiek
#   Ã¢ÂÂ price=0 bug via fills fallback
#   Ã¢ÂÂ sslmode="require" op DB connectie
#   Ã¢ÂÂ Auto mode: live eerst, paper als fallback
#   Ã¢ÂÂ Geen automatische pauze Ã¢ÂÂ bot gaat altijd door
#   Ã¢ÂÂ buy_eur/sell: conn=None + finally conn.close()
#   Ã¢ÂÂ _bitvavo_request: geen retry op alle 4xx (was alleen 401/403)
# ============================================================

from __future__ import annotations

import hashlib
import hmac
from python_bitvavo_api.bitvavo import Bitvavo as BitvavoSDK
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import psycopg2
import psycopg2.extras
import requests
try:
    from bot_health_helper import health_update, health_fout
except ImportError:
    def health_update(*a, **k): pass
    def health_fout(*a, **k): pass


# ============================================================
# ENV Ã¢ÂÂ identiek aan alle andere bestanden
# ============================================================
DATABASE_URL         = (os.getenv("DATABASE_URL")         or "").strip()
ANTHROPIC_API_KEY    = (os.getenv("ANTHROPIC_API_KEY")    or "").strip()

TWILIO_ACCOUNT_SID   = (os.getenv("TWILIO_ACCOUNT_SID")   or "").strip()
TWILIO_AUTH_TOKEN    = (os.getenv("TWILIO_AUTH_TOKEN")    or "").strip()
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
TWILIO_WHATSAPP_TO   = (os.getenv("TWILIO_WHATSAPP_TO")   or "").strip()

BITVAVO_API_KEY      = (os.getenv("BITVAVO_API_KEY")      or "").strip()
BITVAVO_API_SECRET   = (os.getenv("BITVAVO_API_SECRET")   or "").strip()
BITVAVO_OPERATOR_ID  = int(os.getenv("BITVAVO_OPERATOR_ID") or "10001")

BITVAVO_BASE = "https://api.bitvavo.com"
BINANCE_BASE = "https://api.binance.com/api/v3"

# ============================================================
# FASE 1 LIMIETEN Ã¢ÂÂ identiek aan alle andere bestanden
# ============================================================
MAX_PER_TRADE_EUR            = float(os.getenv("MAX_PER_TRADE_EUR")            or "1.00")
MAX_REAL_TRADES_PER_DAY      = int(os.getenv("MAX_REAL_TRADES_PER_DAY")        or "10")
MAX_OPEN_REAL_TRADES         = int(os.getenv("MAX_OPEN_REAL_TRADES")           or "10")
TRAILING_STOP_PCT      = float(os.getenv("TRAILING_STOP_PCT")        or "0.03")  # 3% trailing stop
TRAILING_STOP_ACTIEF   = os.getenv("TRAILING_STOP_ACTIEF", "true").lower() == "true"
DAILY_STOP_LOSS_EUR          = float(os.getenv("DAILY_STOP_LOSS_EUR")          or "5.00")
MAX_CONSECUTIVE_LOSSES       = int(os.getenv("MAX_CONSECUTIVE_LOSSES")         or "3")
CONSECUTIVE_LOSS_PAUSE_HOURS = int(os.getenv("CONSECUTIVE_LOSS_PAUSE_HOURS")   or "2")
TRADING_HOURS_START          = int(os.getenv("TRADING_HOURS_START")            or "0")
TRADING_HOURS_END            = int(os.getenv("TRADING_HOURS_END")              or "24")

# Fee + slippage Ã¢ÂÂ identiek aan alle bestanden
BITVAVO_FEE_PCT = float(os.getenv("BITVAVO_FEE_PCT") or "0.0025")
SLIPPAGE_PCT    = float(os.getenv("SLIPPAGE_PCT")    or "0.001")
TOTAL_COST_PCT  = BITVAVO_FEE_PCT + SLIPPAGE_PCT

# Coin filters
COIN_COOLDOWN_HOURS   = float(os.getenv("COIN_COOLDOWN_HOURS")   or "24.0")
BLACKLIST_MIN_TRADES  = int(os.getenv("BLACKLIST_MIN_TRADES")    or "20")
BLACKLIST_MAX_WINRATE = float(os.getenv("BLACKLIST_MAX_WINRATE") or "0.30")

# ATR parameters Ã¢ÂÂ identiek aan trade_monitor
ATR_MULTIPLIER        = float(os.getenv("ATR_MULTIPLIER")        or "1.6")
ATR_PERIOD            = int(os.getenv("ATR_PERIOD")              or "14")

# Bitvavo minimum order grootte (waarschuwing, geen blokkade in Fase 1)
BITVAVO_MIN_ORDER_EUR = float(os.getenv("BITVAVO_MIN_ORDER_EUR") or "5.0")

MAX_RETRIES     = int(os.getenv("MAX_RETRIES") or "3")
BOT_STATE_TABLE = "public.bot_state"

# ============================================================
# DATA BESTANDEN
# ============================================================
def _get_data_dir() -> str:
    d = (os.getenv("DATA_DIR") or "").strip()
    if d:
        return d
    return "/data" if os.path.isdir("/data") else "/tmp/data"


DATA_DIR          = _get_data_dir()
LIVE_STATE_PATH   = os.path.join(DATA_DIR, "live_state.json")
SHADOW_STATE_PATH = os.path.join(DATA_DIR, "shadow_trades.json")
SNAPSHOT_PATH     = os.path.join(DATA_DIR, "account_snapshot.json")

# Bitvavo markets cache
_MARKETS_CACHE: Dict[str, Any] = {"ts": 0.0, "markets": set()}
_MARKETS_TTL = 30 * 60  # 30 minuten

# WhatsApp rate limiting per fouttype Ã¢ÂÂ voorkomt spam bij herhaalde fouten
_WA_LAST_SENT: Dict[str, float] = {}
_WA_COOLDOWN_SECS = 300  # 5 minuten per fouttype


# ============================================================
# BASIS HELPERS Ã¢ÂÂ identiek aan alle andere bestanden
# ============================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')}] [TRADER] {msg}", flush=True)


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


def utc_day_str(offset_days: int = 0) -> str:
    return (now_utc() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def is_trading_hours() -> bool:
    return TRADING_HOURS_START <= now_utc().hour < TRADING_HOURS_END


def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def safe_rollback(conn) -> None:
    """
    Rollback zonder exception Ã¢ÂÂ v3.0 pattern.
    Altijd aanroepen in except blokken met een open connectie.
    """
    try:
        if conn:
            conn.rollback()
    except Exception:
        pass


# ============================================================
# WHATSAPP Ã¢ÂÂ identieke implementatie + rate limiting v3.0
# ============================================================
def send_whatsapp(message: str) -> bool:
    """
    Stuurt WhatsApp bericht via Twilio.
    Identieke implementatie in alle bestanden.
    Alleen voor kritieke meldingen Ã¢ÂÂ geen spam per trade.
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
            log(f"Ã¢ÂÂ WhatsApp verzonden ({len(message)} tekens)")
            return True
        log(f"Ã¢ÂÂ WhatsApp {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        log(f"Ã¢ÂÂ WhatsApp exception: {type(e).__name__}: {e}")
        return False


def send_whatsapp_rate_limited(message: str, key: str = "default") -> bool:
    """
    WhatsApp met rate limiting per fouttype Ã¢ÂÂ v3.0 pattern.
    Voorkomt WhatsApp spam bij herhaalde fouten.
    key = fouttype identifier (bv. "buy_error", "sell_error").
    Max 1 bericht per 5 minuten per key.
    """
    now  = time.time()
    last = _WA_LAST_SENT.get(key, 0.0)
    if now - last < _WA_COOLDOWN_SECS:
        remaining = int(_WA_COOLDOWN_SECS - (now - last))
        log(f"Ã°ÂÂÂµ WhatsApp rate limited ({key}) Ã¢ÂÂ nog {remaining}s wachten")
        return False
    _WA_LAST_SENT[key] = now
    return send_whatsapp(message)


# ============================================================
# CLAUDE HEALTH MONITORING Ã¢ÂÂ model: claude-sonnet-4-6
# ============================================================
def _claude_analyse(prompt: str, max_tokens: int = 300) -> str:
    """Claude API aanroep Ã¢ÂÂ identiek aan alle bestanden."""
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
                "model":      "claude-sonnet-4-6",
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


def report_error(
    error:       Exception,
    function:    str,
    symbol:      str = "",
    severity:    str = "HOOG",
    open_trades: int = 0,
) -> None:
    """
    Rapporteert fout via Claude analyse + WhatsApp.
    Ernst niveaus: KRITIEK, HOOG, MEDIUM, LAAG.
    Rate limited per fouttype Ã¢ÂÂ geen spam.
    Identiek aan alle andere bestanden.
    """
    log(f"[{severity}] {function} ({symbol}): {type(error).__name__}: {error}")

    if severity not in ("KRITIEK", "HOOG"):
        return

    prompt = f"""
Je bent een crypto trading bot monitor voor live_trader.py.
Er is een fout opgetreden.

Ernst:        {severity}
Functie:      {function}
Coin:         {symbol or 'onbekend'}
Open trades:  {open_trades}
Fout:         {type(error).__name__}: {str(error)[:200]}

Geef in 3 zinnen Nederlands:
1. Wat er mis is gegaan
2. Impact op trades en kapitaal
3. Wat de gebruiker moet doen
""".strip()

    uitleg = _claude_analyse(prompt, max_tokens=200)
    if not uitleg:
        uitleg = f"{type(error).__name__}: {str(error)[:100]}"

    # Rate limited per severity+function combinatie Ã¢ÂÂ geen spam
    wa_key = f"error_{severity}_{function.replace('.', '_')}"
    send_whatsapp_rate_limited(
        f"Ã°ÂÂÂ¨ LIVE TRADER FOUT Ã¢ÂÂ {severity}\n"
        f"{'Ã¢ÂÂ' * 30}\n\n"
        f"Ã°ÂÂÂ Functie:     {function}\n"
        f"Ã°ÂÂªÂ Coin:        {symbol or 'Ã¢ÂÂ'}\n"
        f"Ã°ÂÂÂ Open trades: {open_trades}\n"
        f"Ã¢ÂÂ Ã¯Â¸Â Fout:       {type(error).__name__}\n\n"
        f"Ã°ÂÂ§Â  Claude:\n{uitleg}\n\n"
        f"Ã°ÂÂÂ WAT TE DOEN:\n"
        f"1. Check Render logs voor details\n"
        f"2. Stuur TRADES voor open posities\n"
        f"3. Check Bitvavo account direct\n"
        f"4. Stuur STOP als je wil pauzeren\n\n"
        f"Ã°ÂÂ¤Â BOT PROBEERT DOOR TE GAAN\n"
        f"Commands: STATUS | TRADES | STOP",
        key=wa_key,
    )


def claude_analyseer_trade(
    symbol:     str,
    setup_type: str,
    regime:     str,
    entry:      float,
    exit_price: float,
    pnl_eur:    float,
    hold_min:   float,
    outcome:    str,
    score:      int,
    exit_reden: str,
) -> str:
    """
    Claude analyseert een gesloten trade.
    Wordt opgeslagen in DB voor weekrapport.
    Wordt NIET via WhatsApp gestuurd (geen spam).
    Alleen bij WIN of forse verlies (R < -0.5).
    """
    prompt = f"""
Je bent een crypto trading coach.
Analyseer deze gesloten trade in 2 korte zinnen Nederlands.

Coin:      {symbol}
Setup:     {setup_type} / Regime: {regime}
Entry:     {entry:.6f} Ã¢ÂÂ Exit: {exit_price:.6f}
PnL:       Ã¢ÂÂ¬{pnl_eur:.4f}
Duur:      {hold_min:.0f} min
Score:     {score}
Uitkomst:  {outcome}
Exitreden: {exit_reden}

Was de entry en exit correct? Wat leren we?
""".strip()

    return _claude_analyse(prompt, max_tokens=120)


def claude_trader_health_check() -> str:
    """Claude controleert configuratie bij opstarten."""
    prompt = f"""
Je bent een crypto trading bot configuratie checker.
Controleer of de live_trader.py correct is geconfigureerd.

CONFIGURATIE:
- BITVAVO_API_KEY:    {'Ã¢ÂÂ aanwezig' if BITVAVO_API_KEY else 'Ã¢ÂÂ ONTBREEKT'}
- BITVAVO_API_SECRET: {'Ã¢ÂÂ aanwezig' if BITVAVO_API_SECRET else 'Ã¢ÂÂ ONTBREEKT'}
- DATABASE_URL:       {'Ã¢ÂÂ aanwezig' if DATABASE_URL else 'Ã¢ÂÂ ONTBREEKT'}
- MAX_PER_TRADE_EUR:  Ã¢ÂÂ¬{MAX_PER_TRADE_EUR:.2f}
- DAILY_STOP_LOSS:    Ã¢ÂÂ¬{DAILY_STOP_LOSS_EUR:.2f}
- TRADING_HOURS:      {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC
- FEE_PCT:            {BITVAVO_FEE_PCT*100:.2f}%
- SLIPPAGE_PCT:       {SLIPPAGE_PCT*100:.2f}%
- ATR_MULTIPLIER:     {ATR_MULTIPLIER}

Geef een korte check (2-3 zinnen):
1. Is de configuratie compleet?
2. Zijn er potentiÃÂ«le problemen?
3. Aanbevelingen?
""".strip()

    return _claude_analyse(prompt, max_tokens=150)


# ============================================================
# DATABASE Ã¢ÂÂ v3.0: retries=3 + autocommit=False
# ============================================================
def db_connect(retries: int = 3):
    """
    DB verbinding met sslmode=require Ã¢ÂÂ v3.0 pattern.
    retries=3: probeert 3x met exponential backoff.
    autocommit=False: expliciete commit vereist (veiliger).
    """
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            conn = psycopg2.connect(DATABASE_URL, sslmode="require")
            conn.autocommit = False
            return conn
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = 2 ** attempt
                log(f"Ã¢ÂÂ Ã¯Â¸Â DB connect poging {attempt}/{retries} mislukt, wacht {wait}s: {e}")
                time.sleep(wait)
    raise RuntimeError(f"DB connect mislukt na {retries} pogingen: {last_err}")


# ============================================================
# BOT STATE Ã¢ÂÂ identiek aan alle bestanden
# ============================================================
def get_bot_state(conn, key: str, default: str = "") -> str:
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT value FROM {BOT_STATE_TABLE} WHERE key=%s", (key,)
            )
            row = cur.fetchone()
            return safe_str(row[0], default) if row else default
    except Exception:
        return default


def set_bot_state(conn, key: str, value: str) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
            INSERT INTO {BOT_STATE_TABLE}(key, value, updated_at)
            VALUES(%s, %s, NOW())
            ON CONFLICT(key) DO UPDATE
                SET value=EXCLUDED.value, updated_at=NOW()
            """, (key, value))
        conn.commit()
    except Exception as e:
        log(f"Ã¢ÂÂ Ã¯Â¸Â set_bot_state fout: {e}")


def _set_bot_state_multi(conn, kvs: Dict[str, str]) -> None:
    """Meerdere bot state waarden in ÃÂ©ÃÂ©n transactie Ã¢ÂÂ efficiÃÂ«nter."""
    try:
        with conn.cursor() as cur:
            for key, value in kvs.items():
                cur.execute(f"""
                INSERT INTO {BOT_STATE_TABLE}(key, value, updated_at)
                VALUES(%s, %s, NOW())
                ON CONFLICT(key) DO UPDATE
                    SET value=EXCLUDED.value, updated_at=NOW()
                """, (key, value))
        conn.commit()
    except Exception as e:
        log(f"Ã¢ÂÂ Ã¯Â¸Â _set_bot_state_multi fout: {e}")
        safe_rollback(conn)


def is_bot_active(conn) -> bool:
    return get_bot_state(conn, "bot_active", "false").lower() == "true"


def is_bot_paused(conn) -> bool:
    if get_bot_state(conn, "bot_paused", "false").lower() != "true":
        return False
    until_str = get_bot_state(conn, "bot_paused_until", "")
    if not until_str:
        return True
    try:
        until = datetime.fromisoformat(until_str)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if now_utc() > until:
            set_bot_state(conn, "bot_paused", "false")
            set_bot_state(conn, "bot_paused_until", "")
            return False
        return True
    except Exception:
        return True


def log_naar_bot_state(
    action:  str,
    busy:    bool            = False,
    pnl_eur: Optional[float] = None,
    error:   str             = "",
) -> None:
    """
    Schrijft live trader status naar bot_state tabel Ã¢ÂÂ v3.0.
    Wordt door app.py gelezen voor dashboard display.

    Keys die worden geschreven:
      live_trader_last_action  Ã¢ÂÂ laatste actie (bv. "BUY ETHUSDT @ Ã¢ÂÂ¬2500")
      live_trader_last_ts      Ã¢ÂÂ timestamp ISO (voor Render Services Monitor)
      live_trader_busy         Ã¢ÂÂ "true" als trade bezig is
      live_trader_last_pnl     Ã¢ÂÂ PnL van laatste gesloten trade
      live_trader_error        Ã¢ÂÂ laatste foutmelding (leeg = OK)
    """
    conn = None
    try:
        conn = db_connect()
        kvs: Dict[str, str] = {
            "live_trader_last_action": action[:200],
            "live_trader_last_ts":     now_utc().isoformat(),
            "live_trader_busy":        "true" if busy else "false",
        }
        if pnl_eur is not None:
            kvs["live_trader_last_pnl"] = str(round(pnl_eur, 4))
        kvs["live_trader_error"] = error[:500] if error else ""
        _set_bot_state_multi(conn, kvs)
    except Exception as e:
        log(f"Ã¢ÂÂ Ã¯Â¸Â log_naar_bot_state fout: {e}")
    finally:
        if conn:
            conn.close()


# ============================================================
# LIMIETEN CHECK Ã¢ÂÂ identiek aan whatsapp_webhook.py
# Bot stopt NOOIT automatisch Ã¢ÂÂ jij beslist via STOP
# ============================================================
def get_real_trades_today(conn) -> int:
    """Telt echte trades vandaag."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COUNT(*) FROM public.pending_approvals
            WHERE status IN ('CONSUMED','EXECUTED')
              AND DATE(COALESCE(consumed_at, created_at) AT TIME ZONE 'UTC') = %s
            """, (utc_day_str(),))
            row = cur.fetchone()
            return safe_int(row[0]) if row else 0
    except Exception:
        return 0


def get_open_real_trades_count(conn) -> int:
    """Telt open echte trades Ã¢ÂÂ state file eerst, dan DB als fallback."""
    try:
        state = load_state()
        pos   = state.get("positions") or {}
        if pos:
            return len(pos)
    except Exception:
        pass
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COUNT(*) FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND TRIM(UPPER(COALESCE(outcome,''))) IN ('OPEN','','UNKNOWN')
            """)
            row = cur.fetchone()
            return safe_int(row[0]) if row else 0
    except Exception:
        return 0


def get_daily_pnl(conn, day: str) -> Tuple[int, int, float]:
    """Wins, losses, PnL voor een dag."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')  AS wins,
                COUNT(*) FILTER (WHERE UPPER(outcome)='LOSS') AS losses,
                COALESCE(SUM(
                    CASE
                        WHEN UPPER(outcome)='WIN'  THEN  ABS(COALESCE(pnl_eur,0))
                        WHEN UPPER(outcome)='LOSS' THEN -ABS(COALESCE(pnl_eur,0))
                        ELSE 0
                    END
                ), 0) AS pnl
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND DATE(COALESCE(exit_time, updated_at) AT TIME ZONE 'UTC') = %s
            """, (day,))
            row = cur.fetchone()
            if row:
                return safe_int(row[0]), safe_int(row[1]), safe_float(row[2])
    except Exception:
        pass
    return 0, 0, 0.0


def get_consecutive_losses(conn) -> int:
    """Opeenvolgende verliezen Ã¢ÂÂ identiek aan alle bestanden."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT outcome FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
            ORDER BY COALESCE(exit_time, updated_at) DESC
            LIMIT 10
            """)
            rows  = cur.fetchall()
            count = 0
            for row in rows:
                if safe_str(row[0]).upper() == "LOSS":
                    count += 1
                else:
                    break
            return count
    except Exception:
        return 0


def check_trading_limits(conn) -> Tuple[bool, str]:
    """
    Controleert alle trading limieten voor een BUY.
    Bot stopt NOOIT automatisch Ã¢ÂÂ jij beslist via STOP.
    Identiek aan whatsapp_webhook.py check_trading_limits.
    """
    if not is_bot_active(conn):
        return False, "Bot GESTOPT Ã¢ÂÂ stuur START"

    if is_bot_paused(conn):
        reason = get_bot_state(conn, "bot_paused_reason", "")
        return False, f"Bot GEPAUZEERD: {reason}"

    if not is_trading_hours():
        return False, (
            f"Buiten trading hours "
            f"({TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC)"
        )

    # Daily stop loss: alleen informeren Ã¢ÂÂ bot gaat door
    _, _, daily_pnl = get_daily_pnl(conn, utc_day_str())
    daily_stop = float(get_bot_state(conn, "daily_stop_loss_eur", str(DAILY_STOP_LOSS_EUR)) or DAILY_STOP_LOSS_EUR)
    if daily_pnl <= -daily_stop:
        log(
            f"Ã¢ÂÂ¹Ã¯Â¸Â Dagbudget bereikt: Ã¢ÂÂ¬{daily_pnl:.2f} Ã¢ÂÂ "
            f"bot gaat door (jij beslist via STOP)"
        )

    trades_today = get_real_trades_today(conn)
    if trades_today >= MAX_REAL_TRADES_PER_DAY:
        return False, f"Daglimiet: {trades_today}/{MAX_REAL_TRADES_PER_DAY}"

    open_count = get_open_real_trades_count(conn)
    max_open = int(get_bot_state(conn, "max_open_trades", str(MAX_OPEN_REAL_TRADES)) or MAX_OPEN_REAL_TRADES)
    if open_count >= max_open:
        return False, f"Max open: {open_count}/{max_open}"

    # Consecutive losses: alleen informeren Ã¢ÂÂ bot gaat door
    consecutive = get_consecutive_losses(conn)
    if consecutive >= MAX_CONSECUTIVE_LOSSES:
        log(
            f"Ã¢ÂÂ¹Ã¯Â¸Â {consecutive}x verlies op rij Ã¢ÂÂ "
            f"bot gaat door (jij beslist via STOP)"
        )

    return True, "OK"


# ============================================================
# BITVAVO UNIVERSE FILTER Ã¢ÂÂ publiek + gecached
# ============================================================
def get_tradable_markets() -> Set[str]:
    """
    Haalt actieve Bitvavo EUR markets op.
    Publiek en gecached (30 min TTL).
    Wordt ook gebruikt door multi_coin_score.py.
    """
    now = time.time()
    if _MARKETS_CACHE["markets"] and (now - _MARKETS_CACHE["ts"]) < _MARKETS_TTL:
        return _MARKETS_CACHE["markets"]

    try:
        resp = requests.get(f"{BITVAVO_BASE}/v2/markets", timeout=10)
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
        log(f"Ã¢ÂÂ Bitvavo markets gecached: {len(tradable)} tradable")
        return tradable

    except Exception as e:
        log(f"Ã¢ÂÂ Ã¯Â¸Â Bitvavo markets fout: {e}")
        return _MARKETS_CACHE.get("markets") or set()


def symbol_to_market(symbol_usdt: str) -> Optional[str]:
    """
    ETHUSDT Ã¢ÂÂ ETH-EUR als tradable op Bitvavo.
    Geeft None als niet tradable.
    """
    s = safe_str(symbol_usdt).upper()
    if not s.endswith("USDT"):
        if "-EUR" in s:
            tradable = get_tradable_markets()
            return s if s in tradable else None
        return None
    base     = s[:-4]
    market   = f"{base}-EUR"
    tradable = get_tradable_markets()
    return market if market in tradable else None


def is_coin_tradable(symbol_usdt: str) -> bool:
    return symbol_to_market(symbol_usdt) is not None


# ============================================================
# BITVAVO API Ã¢ÂÂ SIGNING + REQUEST
# v3.0 fix: geen retry op ALLE 4xx (was alleen 401/403)
# ============================================================
def _bitvavo_headers(method: str, path: str, body: str = "") -> Dict[str, str]:
    """
    Genereert Bitvavo API headers met correcte HMAC signing.
    Fix v2.0: digestmod=hashlib.sha256 (was implicit).
    """
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        raise ValueError("Bitvavo API key of secret ontbreekt")

    ts      = str(int(time.time() * 1000))
    message = f"{ts}{method}{path}{body}"

    sig = hmac.new(
        BITVAVO_API_SECRET.strip("'\"").encode("utf-8"),
        message.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    headers = {
        "Bitvavo-Access-Key":       BITVAVO_API_KEY.strip("'\""),
        "Bitvavo-Access-Signature": sig,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Window":    "10000",
        "Content-Type":             "application/json",
    }


    return headers


def _bitvavo_request(
    method:  str,
    path:    str,
    payload: Optional[Dict] = None,
    retries: int             = MAX_RETRIES,
) -> Tuple[bool, Any]:
    """
    Voert een Bitvavo API request uit met retry.

    v3.0 fix: geen retry op ALLE 4xx client errors.
    Client errors zijn programmeerfouten, niet tijdelijk.
    (Was alleen 401/403 Ã¢ÂÂ nu ook 400, 404, 422 etc.)

    Geeft (success, response_data) terug.
    """
    signing_path = f"/v2{path}"
    url          = f"{BITVAVO_BASE}{signing_path}"
    body_str     = json.dumps(payload) if payload else ""

    for attempt in range(1, retries + 1):
        try:
            headers = _bitvavo_headers(method, signing_path, body_str)

            if method == "GET":
                resp = requests.get(url, headers=headers, timeout=15)
            elif method == "POST":
                resp = requests.post(
                    url, headers=headers, data=body_str, timeout=15
                )
            elif method == "DELETE":
                resp = requests.delete(url, headers=headers, timeout=15)
            else:
                return False, f"Onbekende methode: {method}"

            data = resp.json()

            if resp.ok:
                return True, data

            err_code = data.get("errorCode", resp.status_code)
            err_msg  = data.get("error", str(data))
            log(
                f"Ã¢ÂÂ Ã¯Â¸Â Bitvavo {method} {path} Ã¢ÂÂ {err_code}: {err_msg} "
                f"(poging {attempt}/{retries})"
            )

            # v3.0: geen retry op ALLE 4xx client errors
            if 400 <= resp.status_code < 500:
                log(f"  Client error {resp.status_code} Ã¢ÂÂ geen retry")
                return False, f"Client error {resp.status_code}: {err_msg}"

        except requests.exceptions.Timeout:
            log(f"Ã¢ÂÂ Ã¯Â¸Â Bitvavo timeout poging {attempt}/{retries}")
        except Exception as e:
            log(f"Ã¢ÂÂ Ã¯Â¸Â Bitvavo request fout poging {attempt}/{retries}: {e}")

        if attempt < retries:
            wait = 2 ** attempt
            log(f"  Wachten {wait}s voor retry...")
            time.sleep(wait)

    return False, f"Bitvavo request mislukt na {retries} pogingen"


# ============================================================
# PRIJS OPHALEN
# ============================================================
def get_price_binance(symbol_usdt: str) -> Optional[float]:
    """Haalt prijs op via Binance public API."""
    try:
        resp = requests.get(
            f"{BINANCE_BASE}/ticker/price",
            params={"symbol": symbol_usdt.upper()},
            timeout=10,
        )
        if resp.ok:
            return safe_float(resp.json().get("price"))
    except Exception as e:
        log(f"Ã¢ÂÂ Ã¯Â¸Â Binance prijs fout ({symbol_usdt}): {e}")
    return None


def get_price_bitvavo(market: str) -> Optional[float]:
    """Haalt prijs op via Bitvavo public API (geen auth nodig)."""
    try:
        resp = requests.get(
            f"{BITVAVO_BASE}/v2/ticker/price",
            params={"market": market},
            timeout=10,
        )
        if resp.ok:
            return safe_float(resp.json().get("price"))
    except Exception as e:
        log(f"Ã¢ÂÂ Ã¯Â¸Â Bitvavo prijs fout ({market}): {e}")
    return None


def get_current_price(symbol_usdt: str, market: str) -> Optional[float]:
    """Bitvavo EUR prijs eerst, Binance USDT als fallback."""
    price = get_price_bitvavo(market)
    if price and price > 0:
        return price
    price = get_price_binance(symbol_usdt)
    if price and price > 0:
        return price
    return None


def get_eur_balance() -> float:
    """Haalt beschikbaar EUR saldo op van Bitvavo."""
    ok, data = _bitvavo_request("GET", "/balance")
    if not ok or not data:
        return 0.0
    if isinstance(data, list):
        for item in data:
            if safe_str(item.get("symbol")) == "EUR":
                return safe_float(item.get("available", 0))
    elif isinstance(data, dict):
        return safe_float(data.get("available", 0))
    return 0.0


# ============================================================
# LIVE STATE Ã¢ÂÂ file helpers
# v3.0: isinstance(s, dict) check Ã¢ÂÂ corrupt JSON auto-reset
# ============================================================
def load_state() -> Dict[str, Any]:
    """
    Laadt de live trade state uit JSON file.
    v3.0: corrupt JSON (bv. lijst ipv dict) wordt automatisch gereset.
    """
    _ensure_dir(LIVE_STATE_PATH)
    if not os.path.exists(LIVE_STATE_PATH):
        return {"positions": {}, "open_trades": []}
    try:
        with open(LIVE_STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
        # v3.0: isinstance check Ã¢ÂÂ was ontbreekt in v2.0
        if not isinstance(s, dict):
            log(f"Ã¢ÂÂ Ã¯Â¸Â live_state.json corrupt (type={type(s).__name__}) Ã¢ÂÂ reset")
            s = {}
    except Exception as e:
        log(f"Ã¢ÂÂ Ã¯Â¸Â live_state.json laad fout: {e} Ã¢ÂÂ reset")
        s = {}
    s.setdefault("positions", {})
    s.setdefault("open_trades", [])
    return s


def save_state(state: Dict[str, Any]) -> None:
    """Slaat de live trade state atomisch op via tmp file."""
    _ensure_dir(LIVE_STATE_PATH)
    tmp = LIVE_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, LIVE_STATE_PATH)


# ============================================================
# SHADOW STATE Ã¢ÂÂ v3.0 nieuw
# Shadow trades lopen parallel aan live trades voor ai_coach vergelijking.
# ============================================================
def load_shadow_state() -> Dict[str, Any]:
    """Laadt shadow trade state uit JSON file."""
    _ensure_dir(SHADOW_STATE_PATH)
    if not os.path.exists(SHADOW_STATE_PATH):
        return {"positions": {}, "closed": []}
    try:
        with open(SHADOW_STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
        if not isinstance(s, dict):
            log(f"Ã¢ÂÂ Ã¯Â¸Â shadow_trades.json corrupt Ã¢ÂÂ reset")
            s = {}
    except Exception as e:
        log(f"Ã¢ÂÂ Ã¯Â¸Â shadow_trades.json laad fout: {e} Ã¢ÂÂ reset")
        s = {}
    s.setdefault("positions", {})
    s.setdefault("closed", [])
    return s


def save_shadow_state(state: Dict[str, Any]) -> None:
    """Slaat shadow state atomisch op."""
    _ensure_dir(SHADOW_STATE_PATH)
    tmp = SHADOW_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, SHADOW_STATE_PATH)


def shadow_buy(
    symbol:     str,
    entry:      float,
    qty:        float,
    amount_eur: float,
    meta:       Optional[Dict] = None,
) -> None:
    """
    Logt een shadow trade parallel aan elke live BUY Ã¢ÂÂ v3.0 nieuw.

    Doel: ai_coach vergelijkt shadow vs live resultaten om
    edge decay te detecteren (als shadow veel beter is dan live,
    is er waarschijnlijk een executie- of slippage-probleem).

    Opgeslagen in shadow_trades.json.
    """
    meta = meta or {}
    try:
        state = load_shadow_state()
        state["positions"][symbol] = {
            "symbol":          symbol,
            "entry":           entry,
            "qty":             qty,
            "amount_eur":      amount_eur,
            "opened_at":       int(time.time()),
            "opened_at_iso":   now_utc().isoformat(),
            "stop":            safe_float(meta.get("stop"),   entry * 0.98),
            "target":          safe_float(meta.get("target"), entry * 1.04),
            "setup_type":      safe_str(meta.get("setup_type")),
            "regime":          safe_str(meta.get("regime")),
            "score":           safe_int(meta.get("score")),
            "source":          "SHADOW",
            "status":          "OPEN",
            "had_over_1r":     False,
            "partial_sold_40": False,
        }
        save_shadow_state(state)
        log(f"Ã°ÂÂÂ¤ Shadow BUY gelogd: {symbol} @ {entry:.6f}")
    except Exception as e:
        log(f"Ã¢ÂÂ Ã¯Â¸Â shadow_buy fout ({symbol}): {e}")


def shadow_sell(
    symbol:     str,
    exit_price: float,
    fraction:   float = 1.0,
) -> None:
    """
    Sluit een shadow trade Ã¢ÂÂ aangeroepen vanuit sell().
    Archiveert resultaat voor ai_coach edge decay analyse.
    """
    try:
        state = load_shadow_state()
        positions = state.get("positions") or {}
        pos = positions.get(symbol)
        if not pos:
            for k, v in positions.items():
                if isinstance(v, dict) and v.get("symbol") == symbol:
                    pos = v
                    break
        if not pos:
            return

        entry    = safe_float(pos.get("entry"))
        qty      = safe_float(pos.get("qty"))
        sold_qty = qty * fraction
        pnl_eur  = (exit_price - entry) * sold_qty
        outcome  = "WIN" if pnl_eur > 0 else "LOSS"

        closed = pos.copy()
        closed.update({
            "exit_price": exit_price,
            "exit_pnl":   round(pnl_eur, 4),
            "outcome":    outcome,
            "closed_at":  now_utc().isoformat(),
            "fraction":   fraction,
        })
        state["closed"].append(closed)

        if fraction >= 1.0:
            state["positions"].pop(symbol, None)
        else:
            state["positions"][symbol]["qty"]        = qty - sold_qty
            state["positions"][symbol]["amount_eur"] *= (1 - fraction)

        save_shadow_state(state)
        log(f"Ã°ÂÂÂ¤ Shadow SELL: {symbol} {outcome} Ã¢ÂÂ¬{pnl_eur:.4f}")
    except Exception as e:
        log(f"Ã¢ÂÂ Ã¯Â¸Â shadow_sell fout ({symbol}): {e}")


# ============================================================
# ACCOUNT SNAPSHOT Ã¢ÂÂ v3.0 nieuw
# ============================================================
def get_account_snapshot() -> Dict[str, Any]:
    """
    Haalt volledige Bitvavo portfolio op en slaat op als JSON.
    Wordt gebruikt door dashboard (app.py) en ai_coach.
    Opgeslagen in account_snapshot.json.
    """
    ok, data = _bitvavo_request("GET", "/balance")
    if not ok:
        log(f"Ã¢ÂÂ Ã¯Â¸Â Account snapshot mislukt: {data}")
        return {}

    snapshot: Dict[str, Any] = {
        "ts":            now_utc().isoformat(),
        "balances":      {},
        "eur_available": 0.0,
    }

    if isinstance(data, list):
        for item in data:
            sym       = safe_str(item.get("symbol"))
            available = safe_float(item.get("available"))
            in_order  = safe_float(item.get("inOrder"))
            if available > 0 or in_order > 0:
                snapshot["balances"][sym] = {
                    "available": available,
                    "inOrder":   in_order,
                }
                if sym == "EUR":
                    snapshot["eur_available"] = available

    try:
        _ensure_dir(SNAPSHOT_PATH)
        tmp = SNAPSHOT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
        os.replace(tmp, SNAPSHOT_PATH)
        log(f"Ã¢ÂÂ Account snapshot: {len(snapshot['balances'])} coins, "
            f"EUR={snapshot['eur_available']:.2f}")
    except Exception as e:
        log(f"Ã¢ÂÂ Ã¯Â¸Â Snapshot opslaan fout: {e}")

    return snapshot


# ============================================================
# COIN STATS Ã¢ÂÂ v3.0 nieuw
# ============================================================
def get_coin_stats(conn, symbol: str) -> Dict[str, Any]:
    """
    Haalt historische statistieken op per coin uit experience_trades.
    Gebruikt door Kelly Criterion sizing en coin clustering in ai_coach.

    Returns: {n, wins, win_rate, avg_win_eur, avg_loss_eur,
               avg_win_r, avg_loss_r, profit_factor, total_pnl}
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COUNT(*)                                                            AS n,
                COUNT(*) FILTER (WHERE UPPER(outcome) = 'WIN')                      AS wins,
                AVG(pnl_eur)      FILTER (WHERE UPPER(outcome) = 'WIN')             AS avg_win_eur,
                AVG(ABS(pnl_eur)) FILTER (WHERE UPPER(outcome) = 'LOSS')            AS avg_loss_eur,
                COALESCE(SUM(pnl_eur)      FILTER (WHERE UPPER(outcome) = 'WIN'),  0) AS total_win,
                COALESCE(SUM(ABS(pnl_eur)) FILTER (WHERE UPPER(outcome) = 'LOSS'), 0) AS total_loss,
                COALESCE(SUM(pnl_eur), 0)                                           AS total_pnl
            FROM public.experience_trades
            WHERE coin = %s
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND pnl_eur IS NOT NULL
            """, (symbol,))
            row = cur.fetchone()
            if row:
                n            = safe_int(row[0])
                wins         = safe_int(row[1])
                avg_win_eur  = safe_float(row[2])
                avg_loss_eur = safe_float(row[3])
                total_win    = safe_float(row[4])
                total_loss   = safe_float(row[5])
                total_pnl    = safe_float(row[6])

                win_rate      = wins / n if n > 0 else 0.5
                profit_factor = total_win / total_loss if total_loss > 0 else 1.0
                base          = MAX_PER_TRADE_EUR or 0.50
                avg_win_r     = avg_win_eur  / base if base > 0 else 1.0
                avg_loss_r    = avg_loss_eur / base if base > 0 else 1.0

                return {
                    "n":             n,
                    "wins":          wins,
                    "win_rate":      round(win_rate, 4),
                    "avg_win_eur":   round(avg_win_eur, 4),
                    "avg_loss_eur":  round(avg_loss_eur, 4),
                    "avg_win_r":     round(avg_win_r, 4),
                    "avg_loss_r":    round(avg_loss_r, 4),
                    "profit_factor": round(profit_factor, 4),
                    "total_pnl":     round(total_pnl, 4),
                }
    except Exception as e:
        log(f"Ã¢ÂÂ Ã¯Â¸Â get_coin_stats fout ({symbol}): {e}")

    return {
        "n": 0, "wins": 0, "win_rate": 0.5,
        "avg_win_eur": 0.0, "avg_loss_eur": 0.0,
        "avg_win_r": 1.0, "avg_loss_r": 1.0,
        "profit_factor": 1.0, "total_pnl": 0.0,
    }


# ============================================================
# KELLY CRITERION Ã¢ÂÂ v3.0 nieuw
# ============================================================
def bereken_positiegrootte_kelly(
    conn,
    symbol:   str,
    base_eur: float = MAX_PER_TRADE_EUR,
) -> float:
    """
    Berekent optimale positiegrootte via Half Kelly Criterion Ã¢ÂÂ v3.0 nieuw.

    Kelly formule: f* = (b*p - q) / b
      b = gem. win / gem. loss (reward/risk ratio)
      p = win rate
      q = 1 - p (loss rate)

    Half Kelly = f* / 2 (minder volatiliteit, veiliger in de praktijk)

    Constraints:
      - Minimaal 10 trades aan data nodig
      - Resultaat: min Ã¢ÂÂ¬0.50, max Ã¢ÂÂ¬5.00
      - Multiplier: clamped op [0.10, 2.00] van base_eur

    Als onvoldoende data: geeft base_eur terug (conservatief).
    """
    try:
        stats = get_coin_stats(conn, symbol)
        n     = stats.get("n", 0)

        if n < 10:
            log(f"Kelly {symbol}: onvoldoende data (n={n} < 10) Ã¢ÂÂ Ã¢ÂÂ¬{base_eur:.2f}")
            return base_eur

        win_rate   = stats.get("win_rate", 0.5)
        avg_win_r  = stats.get("avg_win_r", 1.0)
        avg_loss_r = stats.get("avg_loss_r", 1.0)

        if avg_loss_r <= 0:
            return base_eur

        b          = avg_win_r / avg_loss_r   # reward/risk ratio
        q          = 1.0 - win_rate
        kelly      = (b * win_rate - q) / b
        half_kelly = kelly / 2.0

        # Clamp multiplier tussen 10% en 200% van base
        half_kelly = max(0.10, min(half_kelly, 2.0))
        result     = round(base_eur * half_kelly, 2)
        # Absolute grenzen
        result     = max(0.50, min(result, 5.0))

        log(
            f"Kelly {symbol}: n={n} wr={win_rate:.0%} "
            f"b={b:.2f} f*={kelly:.2f} half={half_kelly:.2f} "
            f"Ã¢ÂÂ Ã¢ÂÂ¬{result:.2f}"
        )
        return result

    except Exception as e:
        log(f"Ã¢ÂÂ Ã¯Â¸Â Kelly fout ({symbol}): {e}")
        return base_eur


# ============================================================
# ATR STOP VALIDATIE Ã¢ÂÂ v3.0 nieuw
# ============================================================
def validate_atr_stop(
    symbol: str,
    entry:  float,
    stop:   float,
    atr:    Optional[float] = None,
) -> float:
    """
    Valideert en corrigeert ATR-based stop loss Ã¢ÂÂ v3.0 nieuw.

    Regels:
    - Stop mag NOOIT boven entry liggen
    - Met ATR: stop tussen 0.5x en ATR_MULTIPLIER (1.6x) ATR onder entry
    - Zonder ATR: minimaal 2% onder entry

    Geeft gecorrigeerde stop terug.
    """
    if entry <= 0:
        return stop

    # Stop boven of gelijk aan entry = altijd fout
    if stop >= entry:
        corrected = entry * 0.98
        log(f"Ã¢ÂÂ Ã¯Â¸Â Stop boven entry voor {symbol}: {stop:.6f} Ã¢ÂÂ {corrected:.6f}")
        return corrected

    if atr and atr > 0:
        min_stop = entry - (ATR_MULTIPLIER * atr)   # max afstand (ATR_MULTIPLIER x ATR)
        max_stop = entry - (0.5 * atr)              # min afstand (0.5x ATR)

        if stop > max_stop:
            log(f"Ã¢ÂÂ Ã¯Â¸Â Stop te krap voor {symbol}: {stop:.6f} Ã¢ÂÂ {max_stop:.6f} (0.5x ATR)")
            return max_stop

        if stop < min_stop:
            log(f"Ã¢ÂÂ Ã¯Â¸Â Stop te wijd voor {symbol}: {stop:.6f} Ã¢ÂÂ {min_stop:.6f} ({ATR_MULTIPLIER}x ATR)")
            return min_stop

        return stop  # stop is OK

    # Geen ATR: minimaal 2% onder entry
    min_stop_pct = entry * 0.98
    if stop > min_stop_pct:
        log(f"Ã¢ÂÂ Ã¯Â¸Â Stop te krap (geen ATR) voor {symbol}: {stop:.6f} Ã¢ÂÂ {min_stop_pct:.6f}")
        return min_stop_pct

    return stop


# ============================================================
# MIN ORDER SIZE CHECK Ã¢ÂÂ v3.0 nieuw
# ============================================================
def check_min_order_size(amount_eur: float) -> Tuple[bool, str]:
    """
    Controleert of order boven Bitvavo minimum zit Ã¢ÂÂ v3.0 nieuw.

    Bitvavo minimum is Ã¢ÂÂ¬5.00. Fase 1 limit is Ã¢ÂÂ¬0.50.
    Dit is een WAARSCHUWING, geen blokkade Ã¢ÂÂ
    Bitvavo kan kleine orders accepteren of afwijzen afhankelijk van market.

    Als order wordt afgewezen krijg je een client error 400 terug
    van _bitvavo_request() Ã¢ÂÂ die wordt correct afgehandeld.
    """
    if amount_eur < BITVAVO_MIN_ORDER_EUR:
        msg = (
            f"Ã¢ÂÂ Ã¯Â¸Â Order Ã¢ÂÂ¬{amount_eur:.2f} < Bitvavo minimum "
            f"Ã¢ÂÂ¬{BITVAVO_MIN_ORDER_EUR:.2f} Ã¢ÂÂ kan worden afgewezen"
        )
        log(msg)
        return False, msg
    return True, "OK"


# ============================================================
# PNL BEREKENING Ã¢ÂÂ v3.0 nieuw
# ============================================================
def bereken_pnl_nauwkeurig(
    entry:      float,
    exit_price: float,
    qty:        float,
    amount_eur: float,
    fraction:   float = 1.0,
) -> Dict[str, float]:
    """
    Berekent PnL nauwkeurig inclusief alle kosten Ã¢ÂÂ v3.0 nieuw.

    v2.0 had alleen fee_buy meegeteld Ã¢ÂÂ fee_sell ontbrak.
    v3.0 telt beide mee voor nauwkeurig netto PnL.

    Kosten:
      fee_buy:  betaald bij aankoop = amount_eur * fraction * BITVAVO_FEE_PCT
      fee_sell: betaald bij verkoop = exit_price * qty * fraction * BITVAVO_FEE_PCT
      slippage: schatting = amount_eur * fraction * SLIPPAGE_PCT

    Returns: {gross_pnl, net_pnl, fee_buy, fee_sell,
               total_fees, slippage_estimate, r_multiple}
    """
    sold_qty      = qty * fraction
    invested_eur  = amount_eur * fraction

    fee_buy       = round(invested_eur * BITVAVO_FEE_PCT, 6)
    fee_sell      = round(exit_price * sold_qty * BITVAVO_FEE_PCT, 6)
    slippage_cost = round(invested_eur * SLIPPAGE_PCT, 6)

    gross_pnl  = (exit_price - entry) * sold_qty
    net_pnl    = gross_pnl - fee_buy - fee_sell
    r_multiple = net_pnl / invested_eur if invested_eur > 0 else 0.0

    return {
        "gross_pnl":         round(gross_pnl, 6),
        "net_pnl":           round(net_pnl, 6),
        "fee_buy":           fee_buy,
        "fee_sell":          fee_sell,
        "total_fees":        round(fee_buy + fee_sell, 6),
        "slippage_estimate": slippage_cost,
        "r_multiple":        round(r_multiple, 4),
    }


# ============================================================
# DB LOGGING Ã¢ÂÂ experience_trades + coach_events
# ============================================================
def log_trade_event(
    conn,
    symbol:     str,
    event_type: str,
    details:    Dict,
) -> None:
    """
    Logt een trade event naar coach_events voor ai_coach Ã¢ÂÂ v3.0 nieuw.

    event_type: "BUY", "SELL", "PARTIAL_SELL", "STOP_HIT",
                "TARGET_HIT", "SHADOW_BUY", "ERROR"

    ai_coach leest coach_events voor weekrapport en analyse.
    Fout bij INSERT wordt gelogd maar veroorzaakt geen crash.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO public.coach_events
                (symbol, event_type, details, created_at)
            VALUES (%s, %s, %s::jsonb, NOW())
            ON CONFLICT DO NOTHING
            """, (symbol, event_type, json.dumps(details)))
        conn.commit()
    except Exception as e:
        # coach_events tabel bestaat mogelijk nog niet Ã¢ÂÂ geen crash
        log(f"Ã¢ÂÂ Ã¯Â¸Â log_trade_event fout ({symbol}/{event_type}): {e}")
        safe_rollback(conn)


def log_trade_open_to_db(
    conn,
    symbol:     str,
    market:     str,
    entry:      float,
    qty:        float,
    amount_eur: float,
    fee_eur:    float,
    stop:       float,
    target:     float,
    meta:       Optional[Dict] = None,
) -> None:
    """Logt een nieuw geopende live trade naar experience_trades."""
    meta      = meta or {}
    prebuy_id = safe_str(meta.get("prebuy_id"))
    trade_key = f"LIVE|{symbol}|{prebuy_id or int(time.time())}"

    try:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO public.experience_trades (
                trade_key, source, coin, symbol, timestamp, entry_time,
                setup_type, market_regime, entry, stop, stop_loss, target,
                qty, amount_eur, fee_eur, outcome,
                bot_confidence, score, timeframe,
                exp_n, exp_win_rate, why_tag,
                bitvavo_market,
                created_at, updated_at
            )
            VALUES (
                %s,'LIVE',%s,%s,NOW(),NOW(),%s,%s,%s,%s,%s,%s,
                %s,%s,%s,'OPEN',
                %s,%s,%s,
                %s,%s,%s,
                %s,
                NOW(),NOW()
            )
            ON CONFLICT (trade_key) DO UPDATE SET
                updated_at = NOW()
            """, (
                trade_key, symbol, symbol,
                safe_str(meta.get("setup_type")),
                safe_str(meta.get("regime")),
                entry, stop, stop, target,
                qty, amount_eur, fee_eur,
                safe_int(meta.get("confidence")),
                safe_int(meta.get("score")),
                safe_str(meta.get("timeframe"), "4h"),
                safe_int(meta.get("exp_n")),
                safe_float(meta.get("exp_win_rate")),
                safe_str(meta.get("why_tag")),
                market,
            ))
        conn.commit()
        log(f"Ã¢ÂÂ DB gelogd (OPEN): {symbol} entry={entry:.6f}")

        # Coach event voor ai_coach
        log_trade_event(conn, symbol, "BUY", {
            "trade_key":  trade_key,
            "entry":      entry,
            "stop":       stop,
            "target":     target,
            "amount_eur": amount_eur,
            "score":      safe_int(meta.get("score")),
            "setup_type": safe_str(meta.get("setup_type")),
            "regime":     safe_str(meta.get("regime")),
        })

    except Exception as e:
        log(f"Ã¢ÂÂ Ã¯Â¸Â log_trade_open_to_db fout ({symbol}): {e}")
        safe_rollback(conn)


def log_trade_close_to_db(
    conn,
    symbol:         str,
    prebuy_id:      str,
    exit_price:     float,
    pnl_eur:        float,
    outcome:        str,
    exit_reden:     str,
    claude_analyse: str = "",
) -> None:
    """Updatet een gesloten live trade in experience_trades."""
    trade_key = f"LIVE|{symbol}|{prebuy_id or int(time.time())}"

    try:
        with conn.cursor() as cur:
            cur.execute("""
            UPDATE public.experience_trades SET
                outcome    = %s,
                pnl_eur    = %s,
                exit_time  = NOW(),
                updated_at = NOW()
            WHERE trade_key = %s
            """, (outcome, pnl_eur, trade_key))

            if cur.rowcount == 0:
                # Nieuw record als OPEN niet gevonden (edge case)
                cur.execute("""
                INSERT INTO public.experience_trades (
                    trade_key, source, coin, timestamp, entry_time,
                    outcome, pnl_eur, exit_time, created_at, updated_at
                )
                VALUES (%s,'LIVE',%s,NOW(),NOW(),%s,%s,NOW(),NOW(),NOW())
                ON CONFLICT (trade_key) DO UPDATE SET
                    outcome=EXCLUDED.outcome, pnl_eur=EXCLUDED.pnl_eur,
                    exit_time=NOW(), updated_at=NOW()
                """, (trade_key, symbol, outcome, pnl_eur))

        conn.commit()
        log(f"Ã¢ÂÂ DB gelogd ({outcome}): {symbol} pnl=Ã¢ÂÂ¬{pnl_eur:.4f}")

        # Coach event voor ai_coach
        log_trade_event(conn, symbol, "SELL", {
            "trade_key":  trade_key,
            "exit_price": exit_price,
            "pnl_eur":    pnl_eur,
            "outcome":    outcome,
            "exit_reden": exit_reden,
        })

    except Exception as e:
        log(f"Ã¢ÂÂ Ã¯Â¸Â log_trade_close_to_db fout ({symbol}): {e}")
        safe_rollback(conn)


# ============================================================
# COIN FILTERS Ã¢ÂÂ identiek aan multi_coin_score en trade_monitor
# ============================================================
def is_coin_on_cooldown(conn, symbol: str) -> bool:
    """24u cooldown na verlies op die coin."""
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
                if hasattr(last_loss, "tzinfo") and last_loss.tzinfo is None:
                    last_loss = last_loss.replace(tzinfo=timezone.utc)
                hours_since = (now_utc() - last_loss).total_seconds() / 3600
                cooldown = float(get_bot_state(conn, "coin_cooldown_hours", str(COIN_COOLDOWN_HOURS)) or COIN_COOLDOWN_HOURS)
                return hours_since < cooldown
    except Exception:
        pass
    return False


def is_coin_blacklisted(conn, symbol: str) -> bool:
    """Blacklist: win rate <30% na 20+ trades."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COUNT(*) AS n,
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


# ============================================================
# BUY ORDER Ã¢ÂÂ Bitvavo market BUY helpers
# ============================================================
def place_market_buy_eur(
    market:     str,
    amount_eur: float,
) -> Tuple[bool, Dict]:
    """
    Plaatst een market BUY order op Bitvavo voor een EUR bedrag.
    Geeft (success, order_data) terug.
    """
    payload = {
        "market":      market,
        "side":        "buy",
        "orderType":   "market",
        "amountQuote": f"{amount_eur:.2f}",
        "operatorId":  BITVAVO_OPERATOR_ID,
    }

    log(f"Ã°ÂÂÂ¤ Bitvavo BUY: {market} Ã¢ÂÂ¬{amount_eur:.2f}")

    try:
        sdk = BitvavoSDK({"APIKEY": BITVAVO_API_KEY.strip(), "APISECRET": BITVAVO_API_SECRET.strip()})
        data = sdk.placeOrder(market, "buy", "market", {"amountQuote": f"{amount_eur:.2f}", "operatorId": BITVAVO_OPERATOR_ID})
        ok = "errorCode" not in data and "error" not in data
    except Exception as e:
        log(f"Ã¢ÂÂ BUY exception ({market}): {e}")
        return False, {"error": str(e)}
    if not ok:
        log(f"Ã¢ÂÂ BUY mislukt ({market}): {data}")
        return False, {"error": str(data)}
    if isinstance(data, dict) and "error" in data:
        log(f"Ã¢ÂÂ BUY error ({market}): {data}")
        return False, data

    # Prijs en qty bepalen uit fills (meest nauwkeurig)
    price = 0.0
    qty   = 0.0

    fills = data.get("fills") or []
    if fills:
        total_eur = sum(
            safe_float(f.get("amount")) * safe_float(f.get("price"))
            for f in fills
        )
        total_qty = sum(safe_float(f.get("amount")) for f in fills)
        price     = total_eur / total_qty if total_qty > 0 else 0.0
        qty       = total_qty
    else:
        # Fallback: order velden
        price = safe_float(data.get("price") or data.get("avgFillPrice") or 0)
        qty   = safe_float(data.get("filledAmount") or data.get("filled") or 0)
        if price <= 0:
            price = get_price_bitvavo(market) or 0.0

    data["_parsed_price"] = price
    data["_parsed_qty"]   = qty

    log(f"Ã¢ÂÂ BUY uitgevoerd: {market} qty={qty:.6f} @ Ã¢ÂÂ¬{price:.6f}")
    return True, data


def place_market_sell(
    market:   str,
    qty:      float,
    fraction: float = 1.0,
) -> Tuple[bool, Dict]:
    """
    Plaatst een market SELL order op Bitvavo.
    fraction=1.0 Ã¢ÂÂ alles, fraction=0.40 Ã¢ÂÂ 40%
    """
    sell_qty = round(qty * fraction, 8)

    if sell_qty <= 0:
        return False, {"error": f"Ongeldige qty: {sell_qty}"}

    payload = {
        "market":    market,
        "side":      "sell",
        "orderType": "market",
        "amount":    str(sell_qty),
        "operatorId": BITVAVO_OPERATOR_ID,
    }

    log(f"Ã°ÂÂÂ¤ Bitvavo SELL: {market} qty={sell_qty:.6f} ({fraction*100:.0f}%)")

    try:
        sdk = BitvavoSDK({"APIKEY": BITVAVO_API_KEY.strip(), "APISECRET": BITVAVO_API_SECRET.strip()})
        data = sdk.placeOrder(market, "sell", "market", {"amount": str(sell_qty), "operatorId": BITVAVO_OPERATOR_ID})
        _err_code = data.get("errorCode", 0) if isinstance(data, dict) else 0
        if _err_code in (212, 216):
            log("Ghost close error " + str(_err_code) + " (" + symbol + "): sell gefaald")
            return {"ok": False, "ghost": True, "errorCode": _err_code, "reason": "error_" + str(_err_code)}
        ok = "errorCode" not in data and "error" not in data
    except Exception as e:
        log(f"Ã¢ÂÂ SELL exception ({market}): {e}")
        return False, {"error": str(e)}
    if not ok:
        log(f"Ã¢ÂÂ SELL mislukt ({market}): {data}")
        return False, {"error": str(data)}

    # Prijs bepalen uit fills
    fills    = data.get("fills") or []
    price    = 0.0
    sold_qty = 0.0

    if fills:
        total_eur = sum(
            safe_float(f.get("amount")) * safe_float(f.get("price"))
            for f in fills
        )
        total_qty = sum(safe_float(f.get("amount")) for f in fills)
        price     = total_eur / total_qty if total_qty > 0 else 0.0
        sold_qty  = total_qty
    else:
        price    = safe_float(data.get("price") or data.get("avgFillPrice") or 0)
        sold_qty = safe_float(data.get("filledAmount") or sell_qty)
        if price <= 0:
            price = get_price_bitvavo(market) or 0.0

    data["_parsed_price"]    = price
    data["_parsed_sold_qty"] = sold_qty
    data["_parsed_fraction"] = fraction

    log(f"Ã¢ÂÂ SELL uitgevoerd: {market} qty={sold_qty:.6f} @ Ã¢ÂÂ¬{price:.6f}")
    return True, data


# ============================================================
# HOOFD BUY FUNCTIE Ã¢ÂÂ v3.0
# conn=None + finally + shadow_buy + Kelly + ATR stop validatie
# ============================================================
def buy_eur(
    symbol:     str,
    amount_eur: float          = 0.0,
    meta:       Optional[Dict] = None,
) -> Tuple[bool, str]:
    """
    Voert een live BUY uit op Bitvavo Ã¢ÂÂ v3.0.

    Stappen:
    1.  Market ophalen (USDTÃ¢ÂÂEUR)
    2.  Min order size check (waarschuwing)
    3.  DB limieten controleren
    4.  Coin filters (cooldown, blacklist)
    5.  EUR balance check
    6.  BUY order plaatsen op Bitvavo
    7.  Stop validatie (ATR)
    8.  State opslaan (live_state.json)
    9.  Shadow buy parallel loggen
    10. DB loggen (experience_trades)
    11. bot_state updaten (dashboard)

    v3.0: conn=None + finally conn.close() + safe_rollback.
    """
    meta = meta or {}
    # Positiegrootte uit bot_state lezen als niet opgegeven
    if not amount_eur:
        try:
            conn_tmp = db_connect()
            val = get_bot_state(conn_tmp, "position_size_eur", str(MAX_PER_TRADE_EUR))
            conn_tmp.close()
            amount_eur = float(val or MAX_PER_TRADE_EUR)
        except Exception:
            amount_eur = MAX_PER_TRADE_EUR
    # 1b. Regime-adaptive sizing
    try:
        regime_raw = str(meta.get("regime", "")).upper()
        score_raw  = int(meta.get("score", 0))
        if regime_raw == "BULL" and score_raw >= 85:
            amount_eur = min(amount_eur * 1.5, MAX_PER_TRADE_EUR * 1.5)
            log(f" BULL+score85: positie verhoogd naar {amount_eur:.2f}")
        elif regime_raw in ("BEAR", "CRASH"):
            amount_eur = amount_eur * 0.5
            log(f" {regime_raw}: positie verlaagd naar {amount_eur:.2f}")
        amount_eur = max(amount_eur, BITVAVO_MIN_ORDER_EUR)
    except Exception:
        pass

    # 1. Market ophalen
    market = symbol_to_market(symbol)
    if not market:
        return False, f"{symbol} niet tradable op Bitvavo"

    # 2. Min order check (waarschuwing, geen blokkade)
    check_min_order_size(amount_eur)

    log_naar_bot_state(f"BUY start: {symbol}", busy=True)

    conn = None
    try:
        conn = db_connect()

        # 3. Limieten check
        ok, reason = check_trading_limits(conn)
        if not ok:
            log_naar_bot_state(f"BUY geblokkeerd: {reason}", busy=False)
            return False, reason

        # 4. Coin filters
        if is_coin_blacklisted(conn, symbol):
            msg = f"{symbol} op blacklist (win rate te laag)"
            log(f"Ã¢ÂÂ« {msg}")
            log_naar_bot_state(f"BUY geblokkeerd: {msg}", busy=False)
            return False, msg

        if is_coin_on_cooldown(conn, symbol):
            msg = f"{symbol} in cooldown (24u na verlies)"
            log(f"Ã¢ÂÂ³ {msg}")
            log_naar_bot_state(f"BUY geblokkeerd: {msg}", busy=False)
            return False, msg

        # 5. EUR balance check
        eur_balance = get_eur_balance()
        if eur_balance < amount_eur:
            msg = f"Onvoldoende EUR: Ã¢ÂÂ¬{eur_balance:.2f} < Ã¢ÂÂ¬{amount_eur:.2f}"
            log_naar_bot_state(f"BUY geblokkeerd: {msg}", busy=False)
            return False, msg

        # 5b. Risk Gate  Claude poortwachter (nog niet geimplementeerd)
        RISK_GATE_OK = False  # fix: risk_gate.py not yet implemented
        if RISK_GATE_OK:
            try:
                rg = _riskgate_beoordeel(
                    symbol=symbol,
                    score=int(meta.get("score", 0)),
                    regime=str(meta.get("regime", "ONBEKEND")),
                    entry=float(meta.get("entry", 0)),
                    stop=float(meta.get("stop", 0)),
                    target=float(meta.get("target", 0)),
                    setup_type=str(meta.get("setup_type", "")),
                )
                if not rg.get("go", True):
                    msg = f" Risk Gate NO-GO: {rg.get('reden','')[:80]}"
                    log(msg)
                    log_naar_bot_state(f"BUY geblokkeerd: Risk Gate NO-GO", busy=False)
                    return False, msg
                log(f" Risk Gate GO: {rg.get('reden','')[:60]}")
            except Exception as _rge:
                log(f"Risk Gate fout (skip): {_rge}")

        # 6. BUY order plaatsen
        ok, order_data = place_market_buy_eur(market, amount_eur)

        if not ok:
            err_msg = str(order_data.get("error", "Onbekend"))
            report_error(
                Exception(err_msg),
                "buy_eur.place_market_buy_eur",
                symbol, "KRITIEK",
                get_open_real_trades_count(conn),
            )
            log_naar_bot_state(
                f"BUY fout: {err_msg[:80]}",
                busy=False,
                error=err_msg,
            )
            send_whatsapp_rate_limited(f"Ã¢ÂÂ BUY FOUT: {symbol}\nReden: {err_msg[:100]}\nSaldo: Ã¢ÂÂ¬{eur_balance:.2f}", key=f"buy_fout_{symbol}")
            return False, f"BUY mislukt: {err_msg}"

        # 7. Prijs en qty bepalen
        entry   = safe_float(order_data.get("_parsed_price"))
        qty     = safe_float(order_data.get("_parsed_qty"))
        fee_eur = round(amount_eur * BITVAVO_FEE_PCT, 6)

        if entry <= 0 or qty <= 0:
            log(f"Ã¢ÂÂ Ã¯Â¸Â Prijs/qty ongeldig na BUY Ã¢ÂÂ fallback ticker")
            entry = get_price_bitvavo(market) or get_price_binance(symbol) or 0.0
            qty   = amount_eur / entry if entry > 0 else 0.0

        # ATR stop validatie (v3.0)
        raw_stop = safe_float(meta.get("stop"),   entry * 0.98)
        atr_val  = safe_float(meta.get("atr")) or None
        stop     = validate_atr_stop(symbol, entry, raw_stop, atr_val)
        target   = safe_float(meta.get("target"), entry * 1.04)

        # 8. State opslaan
        state = load_state()
        state["positions"][symbol] = {
            "symbol":               symbol,
            "market":               market,
            "entry":                entry,
            "stop_loss":            stop,
            "stop":                 stop,
            "target":               target,
            "qty":                  qty,
            "amount_eur":           amount_eur,
            "fee_eur":              fee_eur,
            "opened_at":            int(time.time()),
            "opened_at_iso":        now_utc().isoformat(),
            "prebuy_id":            safe_str(meta.get("prebuy_id")),
            "setup_type":           safe_str(meta.get("setup_type"), "UNKNOWN"),
            "regime":               safe_str(meta.get("regime"), "UNKNOWN"),
            "score":                safe_int(meta.get("score")),
            "timeframe":            safe_str(meta.get("timeframe"), "4h"),
            "source":               "LIVE",
            "status":               "OPEN",
            "had_over_1r":          False,
            "partial_sold_40":      False,
            "below_1r_count":       0,
            "last_candle_check_ts": 0,
            "max_price_seen":       entry,
            "min_price_seen":       entry,
            "mfe_r":                0.0,
            "mae_r":                0.0,
            "order_id":             safe_str(order_data.get("orderId")),
        }
        state["open_trades"] = list(state["positions"].values())
        save_state(state)

        # 9. Shadow buy parallel (geen blokkade bij fout)
        shadow_buy(symbol, entry, qty, amount_eur, meta)

        # 10. DB loggen
        log_trade_open_to_db(
            conn, symbol, market, entry, qty, amount_eur, fee_eur,
            stop, target, meta,
        )

        conn.close()
        conn = None

        # 11. bot_state updaten voor dashboard
        log_naar_bot_state(
            f"BUY {symbol} @ Ã¢ÂÂ¬{entry:.6f}",
            busy=False,
        )

        log(f"Ã¢ÂÂ Live BUY: {symbol} @ Ã¢ÂÂ¬{entry:.6f} qty={qty:.6f} stop={stop:.6f}")
        send_whatsapp_rate_limited(
            f"Ã°ÂÂÂ¢ TRADE GEOPEND: {symbol}\n"
            f"Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ\n\n"
            f"Ã°ÂÂÂ° Bedrag:  Ã¢ÂÂ¬{amount_eur:.2f}\n"
            f"Ã°ÂÂÂ Entry:   Ã¢ÂÂ¬{entry:.6f}\n"
            f"Ã°ÂÂÂ Stop:    Ã¢ÂÂ¬{stop:.6f}\n"
            f"Ã°ÂÂÂ¯ Target:  Ã¢ÂÂ¬{target:.6f}\n",
            key=f"buy_{symbol}"
        )
        return True, f"BUY {symbol} @ Ã¢ÂÂ¬{entry:.6f}"

    except Exception as e:
        safe_rollback(conn)
        report_error(e, "buy_eur", symbol, "KRITIEK")
        log_naar_bot_state(
            f"BUY crash: {symbol}",
            busy=False,
            error=f"{type(e).__name__}: {str(e)[:100]}",
        )
        return False, str(e)
    finally:
        if conn:
            conn.close()


# ============================================================
# HOOFD SELL FUNCTIE Ã¢ÂÂ v3.0
# conn=None + finally + shadow_sell + bereken_pnl_nauwkeurig
# ============================================================
def sell(
    symbol:   str,
    fraction: float          = 1.0,
    meta:     Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Voert een live SELL uit op Bitvavo Ã¢ÂÂ v3.0.

    fraction=1.0  Ã¢ÂÂ verkoop alles (stop loss, structuur break, max hold)
    fraction=0.40 Ã¢ÂÂ partial sell (40% na eerste keer >1R)

    Wordt aangeroepen door trade_monitor.py via _execute_sell().

    v3.0 verbeteringen:
    - bereken_pnl_nauwkeurig() Ã¢ÂÂ fee_sell was ontbreekt in v2.0
    - shadow_sell() parallel
    - conn=None + finally conn.close()
    - log_naar_bot_state() voor dashboard

    Geeft result dict terug:
      {ok, pnl_eur, fee_eur, exit_price, sold_qty,
       outcome, exit_reden, r_multiple}
    """
    meta = meta or {}
    # Check of coin echt op Bitvavo staat
    if fraction >= 1.0:
        try:
            coin = symbol.replace("USDT","").replace("EUR","")
            bal = get_eur_balance(coin) if coin != "EUR" else get_eur_balance()
            if bal <= 0:
                send_whatsapp_rate_limited(f"Ã¢ÂÂ Ã¯Â¸Â SELL GEBLOKKEERD: {symbol}\nGeen {coin} op Bitvavo", key=f"sell_check_{symbol}")
                return {"ok": False, "reason": f"Geen {coin} op Bitvavo"}
        except Exception:
            pass

    log_naar_bot_state(f"SELL start: {symbol} ({fraction*100:.0f}%)", busy=True)

    conn = None
    try:
        state = load_state()
        positions = state.get("positions") or {}
        pos = positions.get(symbol)
        if not pos:
            for k, v in positions.items():
                if isinstance(v, dict) and v.get("symbol") == symbol:
                    pos = v
                    break

        if not pos:
            log_naar_bot_state(f"SELL fout: {symbol} niet in state", busy=False)
            return {"ok": False, "reason": f"{symbol} niet gevonden in state"}

        market     = safe_str(pos.get("market"))
        entry      = safe_float(pos.get("entry"))
        qty        = safe_float(pos.get("qty"))
        amount_eur = safe_float(pos.get("amount_eur"))
        prebuy_id  = safe_str(pos.get("prebuy_id"))
        exit_reden = safe_str(meta.get("exit_reden"), "UNKNOWN")

        if not market:
            market = symbol_to_market(symbol) or f"{symbol[:-4]}-EUR"

        # SELL order plaatsen
        ok, order_data = place_market_sell(market, qty, fraction)

        if not ok:
            err = str(order_data.get("error", "SELL mislukt"))
            report_error(
                Exception(err), "sell.place_market_sell",
                symbol, "KRITIEK",
            )
            log_naar_bot_state(f"SELL fout: {err[:80]}", busy=False, error=err)
            return {"ok": False, "reason": err}

        exit_price = safe_float(order_data.get("_parsed_price"))
        sold_qty   = safe_float(order_data.get("_parsed_sold_qty"), qty * fraction)

        # v3.0: nauwkeurige PnL berekening (fee_buy + fee_sell)
        pnl_data = bereken_pnl_nauwkeurig(
            entry, exit_price, qty, amount_eur, fraction
        )
        pnl_eur  = pnl_data["net_pnl"]
        fee_sell = pnl_data["fee_sell"]
        outcome  = "WIN" if pnl_eur > 0 else "LOSS"

        # State updaten
        if fraction >= 1.0:
            # Volledig sluiten
            state["positions"].pop(symbol, None)
            state["open_trades"] = [
                t for t in (state.get("open_trades") or [])
                if t.get("symbol") != symbol
            ]
        else:
            # Partial sell Ã¢ÂÂ update resterende positie
            remaining_qty = qty - sold_qty
            remaining_eur = amount_eur * (1 - fraction)
            remaining_fee = safe_float(pos.get("fee_eur")) * (1 - fraction)
            state["positions"][symbol]["qty"]            = remaining_qty
            state["positions"][symbol]["amount_eur"]     = remaining_eur
            state["positions"][symbol]["fee_eur"]        = remaining_fee
            if fraction >= 0.39:  # ~40% partial sell vlag
                state["positions"][symbol]["partial_sold_40"] = True

        save_state(state)

        # Shadow sell parallel (geen blokkade bij fout)
        shadow_sell(symbol, exit_price, fraction)

        # Claude trade analyse Ã¢ÂÂ alleen bij WIN of forse verlies
        claude_txt = ""
        hold_min   = 0.0
        try:
            opened_at = safe_int(pos.get("opened_at"))
            if opened_at:
                hold_min = (time.time() - opened_at) / 60
        except Exception:
            pass

        if outcome == "WIN" or pnl_data.get("r_multiple", 0.0) < -0.5:
            claude_txt = claude_analyseer_trade(
                symbol     = symbol,
                setup_type = safe_str(pos.get("setup_type")),
                regime     = safe_str(pos.get("regime")),
                entry      = entry,
                exit_price = exit_price,
                pnl_eur    = pnl_eur,
                hold_min   = hold_min,
                outcome    = outcome,
                score      = safe_int(pos.get("score")),
                exit_reden = exit_reden,
            )

        # DB loggen
        try:
            conn = db_connect()
            log_trade_close_to_db(
                conn, symbol, prebuy_id, exit_price, pnl_eur,
                outcome, exit_reden, claude_txt,
            )
            conn.close()
            conn = None
        except Exception as e:
            log(f"Ã¢ÂÂ Ã¯Â¸Â DB log fout bij SELL ({symbol}): {e}")

        # bot_state updaten voor dashboard
        log_naar_bot_state(
            f"SELL {symbol} {outcome} Ã¢ÂÂ¬{pnl_eur:.4f}",
            busy=False,
            pnl_eur=pnl_eur,
        )

        icon = "Ã¢ÂÂ" if outcome == "WIN" else "Ã¢ÂÂ"
        log(
            f"{icon} SELL {symbol}: {outcome} "
            f"Ã¢ÂÂ¬{pnl_eur:.4f} | "
            f"exit={exit_price:.6f} | "
            f"{exit_reden} | "
            f"fractie={fraction*100:.0f}%"
        )

        return {
            "ok":         True,
            "pnl_eur":    round(pnl_eur, 4),
            "fee_eur":    round(fee_sell, 6),
            "exit_price": exit_price,
            "sold_qty":   sold_qty,
            "outcome":    outcome,
            "exit_reden": exit_reden,
            "r_multiple": pnl_data.get("r_multiple", 0.0),
        }

    except Exception as e:
        safe_rollback(conn)
        report_error(e, "sell", symbol, "KRITIEK")
        log_naar_bot_state(
            f"SELL crash: {symbol}",
            busy=False,
            error=f"{type(e).__name__}: {str(e)[:100]}",
        )
        return {"ok": False, "reason": str(e)}
    finally:
        if conn:
            conn.close()


# ============================================================
# MAIN Ã¢ÂÂ configuratie check + Bitvavo test
# ============================================================
if __name__ == "__main__":
    log("=" * 60)
    log("Live Trader v3.0 Ã¢ÂÂ configuratie check")
    log("=" * 60)
    log(f"Database:        {'Ã¢ÂÂ' if DATABASE_URL       else 'Ã¢ÂÂ ONTBREEKT'}")
    log(f"Bitvavo Key:     {'Ã¢ÂÂ' if BITVAVO_API_KEY    else 'Ã¢ÂÂ ONTBREEKT'}")
    log(f"Bitvavo Secret:  {'Ã¢ÂÂ' if BITVAVO_API_SECRET else 'Ã¢ÂÂ ONTBREEKT'}")
    log(f"Twilio:          {'Ã¢ÂÂ' if TWILIO_ACCOUNT_SID else 'Ã¢ÂÂ Ã¯Â¸Â niet ingesteld'}")
    log(f"Claude API:      {'Ã¢ÂÂ' if ANTHROPIC_API_KEY  else 'Ã¢ÂÂ Ã¯Â¸Â niet ingesteld'}")
    log(f"Max trade:       Ã¢ÂÂ¬{MAX_PER_TRADE_EUR:.2f}")
    log(f"Daily stop:      Ã¢ÂÂ¬{DAILY_STOP_LOSS_EUR:.2f}")
    log(f"Max trades/dag:  {MAX_REAL_TRADES_PER_DAY}")
    log(f"Max open:        {MAX_OPEN_REAL_TRADES}")
    log(f"Trading hours:   {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC")
    log(f"Fee+slippage:    {TOTAL_COST_PCT*100:.2f}%")
    log(f"ATR multiplier:  {ATR_MULTIPLIER}")
    log(f"Data dir:        {DATA_DIR}")
    log("=" * 60)

    # Test Bitvavo balance + snapshot
    if BITVAVO_API_KEY and BITVAVO_API_SECRET:
        log("Test Bitvavo balance...")
        eur = get_eur_balance()
        log(f"EUR balance: Ã¢ÂÂ¬{eur:.2f}")

        log("Test account snapshot...")
        snap = get_account_snapshot()
        log(f"Snapshot: {len(snap.get('balances', {}))} coins")

    # Test Bitvavo markets
    log("Test Bitvavo markets...")
    markets = get_tradable_markets()
    log(f"Tradable markets: {len(markets)}")

    # Test state files
    log("Test state files...")
    state  = load_state()
    shadow = load_shadow_state()
    log(f"Live state:   {len(state.get('positions', {}))} open posities")
    log(f"Shadow state: {len(shadow.get('positions', {}))} shadow posities")

    # Claude health check
    if ANTHROPIC_API_KEY:
        log("Claude health check...")
        health = claude_trader_health_check()
        if health:
            log(f"Claude: {health}")

    log("Ã¢ÂÂ Live Trader v3.0 configuratie check klaar")


def main_loop():
    """Hoofd trading loop Ã¢ÂÂ draait continu."""
    log("Ã°ÂÂÂ Live Trader main loop gestart")
    conn = None
    try:
        conn = db_connect()
    except Exception as e:
        log(f"Ã¢ÂÂ DB verbinding mislukt: {e}")
        return

    while True:
        try:
            # Haal pending approvals op
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, symbol, bitvavo_market, score, entry, stop, target,
                           kelly_grootte_eur, live_toegestaan
                    FROM pending_approvals
                    WHERE status = 'PENDING'
                    AND live_toegestaan = TRUE
                    AND expires_at > NOW()
                    ORDER BY score DESC
                    LIMIT 5
                """)
                rows = cur.fetchall()

                # ── Shadow-only: score 80-89, geen live geld ──
                cur.execute("""
                    SELECT id, symbol, bitvavo_market, score, entry, stop, target, kelly_grootte_eur
                    FROM pending_approvals
                    WHERE status = 'PENDING'
                    AND score >= 80 AND score < 90
                    AND expires_at > NOW()
                    ORDER BY score DESC
                    LIMIT 10
                """)
                shadow_only_query = True  # marker
                shadow_rows = cur.fetchall()
                for srow in shadow_rows:
                    try:
                        sid, ssymbol, smarket, sscore, sentry, sstop, starget, skelly = srow
                        smeta = {"score": sscore, "setup_type": "TREND_PULLBACK",
                                 "regime": "SHADOW_ONLY", "shadow_only": True}
                        sqty  = round(float(skelly or 7) / max(float(sentry or 1), 0.0001), 6)
                        shadow_buy(ssymbol, float(sentry or 0), sqty, float(skelly or 7), smeta)
                        cur.execute("UPDATE pending_approvals SET status='SHADOW' WHERE id=%s", (sid,))
                        log(f"Shadow-only: {ssymbol} score={sscore}")
                    except Exception as _se:
                        log(f"Shadow-only fout: {_se}")


        except Exception as e:
            # DB reconnect bij SSL/connection fout
            _em = str(e).lower(); _en = type(e).__name__
            if any(x in _em or x in _en for x in ("ssl","connection","operational","closed")):
                log("DB reconnect na " + _en)
                try:
                    conn = db_connect()
                    log("DB reconnect OK")
                except Exception as _re:
                    log("DB reconnect mislukt: " + str(_re))
            log(f"Ã¢ÂÂ Loop fout: {e}")
            try:
                conn.rollback()
            except Exception:
                pass

        # Heartbeat  dashboard kan zien dat de bot leeft
        try: health_update("live_trader", "OK", "bot draait")
        except Exception: pass
        try:
            set_bot_state(conn, "live_trader_last_ts", now_utc().isoformat())
            set_bot_state(conn, "live_trader_busy", "false")
        except Exception:
            pass
        time.sleep(30)


# Start main loop
if __name__ == "__main__":
    main_loop()
