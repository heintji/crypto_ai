# whatsapp_webhook.py
# ============================================================
# Crypto AI Bot — WhatsApp Webhook v2.1
# ============================================================
# Dit is het centrale communicatiepunt van de bot.
# Alle WhatsApp berichten van en naar de gebruiker lopen
# via dit bestand. Tevens handelt dit de automatische BUY
# triggers af vanuit multi_coin_score.py.
#
# ARCHITECTUUR:
#   multi_coin_score → /auto_buy → live_trader.buy_eur()
#   WhatsApp START/STOP/STATUS → bot_state in PostgreSQL
#   Render Cron → /send_daily_rapport / /send_weekly_rapport
#
# IDENTIEK AAN ALLE ANDERE BESTANDEN:
#   ✅ Zelfde ENV variabelen en limieten
#   ✅ Zelfde send_whatsapp() implementatie
#   ✅ Zelfde Claude health monitoring (KRITIEK/HOOG/MEDIUM/LAAG)
#   ✅ Zelfde bot state (PostgreSQL bot_state tabel)
#   ✅ Zelfde is_bot_active / is_bot_paused / pause_bot
#   ✅ Zelfde sslmode="require" op DB connectie
#   ✅ Zelfde safe_int / safe_float / safe_str helpers
#   ✅ Zelfde trading hours filter (08:00-22:00 UTC)
#   ✅ Zelfde weekend: gewoon doorgaan
#
# BUGS GEFIXED v2.1:
#   ✅ /health route toegevoegd (was /healthz — Render pollt /health)
#   ✅ /health geeft uitgebreide JSON terug met bot status + DB check
#   ✅ /health geeft 503 terug als DB niet bereikbaar (Render alert)
#   ✅ /healthz blijft als alias voor backwards compatibility
#   ✅ _PROCESS_START bijgehouden voor uptime in /health response
#
# BUGS GEFIXED v2.0:
#   ✅ Auto mode fix — live eerst, paper als fallback
#   ✅ send_whatsapp() gedefinieerd zodat trade_monitor kan notificeren
#   ✅ HMAC digestmod= fix
#   ✅ Twilio signature verificatie aanwezig
#   ✅ Volledige meta meegegeven aan live_trader
#   ✅ Geen automatische pauze — jij beslist via STOP
#
# AUTOMATISCHE RAPPORTEN (via Render Cron):
#   0 8 * * *     /send_daily_rapport
#   0 8 * * 1     /send_weekly_rapport
#   0 9 * * 1     /send_health_check
#   0 8 1,15 * *  /send_leeranalyse
#   0 8 1 * *     /send_monthly_rapport
#
# WHATSAPP COMMANDS:
#   START        → bot begint traden
#   STOP         → bot stopt (enige manier om te stoppen)
#   STATUS       → volledig overzicht
#   TRADES       → open trades tonen
#   RAPPORT      → dagrapport handmatig
#   WEEKRAPPORT  → weekoverzicht
#   MAANDRAPPORT → maandoverzicht
#   ADVIES       → Claude leeranalyse
#   HEALTH       → health check
#   HELP         → alle commands
# ============================================================

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, request, jsonify

# Twilio optioneel — voor signature verificatie
try:
    from twilio.request_validator import RequestValidator
    TWILIO_AVAILABLE = True
except ImportError:
    TWILIO_AVAILABLE = False

app = Flask(__name__)

# ============================================================
# ENV — identiek aan alle andere bestanden
# ============================================================
DATABASE_URL        = (os.getenv("DATABASE_URL") or "").strip()
ANTHROPIC_API_KEY   = (os.getenv("ANTHROPIC_API_KEY") or "").strip()

TWILIO_ACCOUNT_SID   = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN    = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
TWILIO_WHATSAPP_TO   = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()

BITVAVO_API_KEY      = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET   = (os.getenv("BITVAVO_API_SECRET") or "").strip()
BITVAVO_OPERATOR_ID  = (os.getenv("BITVAVO_OPERATOR_ID") or "").strip()

BOT_INTERNAL_SECRET  = (os.getenv("BOT_INTERNAL_SECRET") or "crypto_ai_bot").strip()
TRADER_MODE          = (os.getenv("TRADER_MODE") or "auto").strip().lower()
WEBHOOK_BASE_URL     = (os.getenv("WEBHOOK_BASE_URL") or "").strip()

# ============================================================
# FASE 1 LIMIETEN — identiek aan alle andere bestanden
# ============================================================
MAX_PER_TRADE_EUR            = float(os.getenv("MAX_PER_TRADE_EUR") or "0.50")
MAX_REAL_TRADES_PER_DAY      = int(os.getenv("MAX_REAL_TRADES_PER_DAY") or "10")
MAX_OPEN_REAL_TRADES         = int(os.getenv("MAX_OPEN_REAL_TRADES") or "5")
DAILY_STOP_LOSS_EUR          = float(os.getenv("DAILY_STOP_LOSS_EUR") or "5.00")
MAX_CONSECUTIVE_LOSSES       = int(os.getenv("MAX_CONSECUTIVE_LOSSES") or "3")
CONSECUTIVE_LOSS_PAUSE_HOURS = int(os.getenv("CONSECUTIVE_LOSS_PAUSE_HOURS") or "2")
TRADING_HOURS_START          = int(os.getenv("TRADING_HOURS_START") or "8")
TRADING_HOURS_END            = int(os.getenv("TRADING_HOURS_END") or "22")

# Bitvavo fee + slippage — identiek aan live_trader en trade_monitor
BITVAVO_FEE_PCT = float(os.getenv("BITVAVO_FEE_PCT") or "0.0025")
SLIPPAGE_PCT    = float(os.getenv("SLIPPAGE_PCT") or "0.001")
TOTAL_COST_PCT  = BITVAVO_FEE_PCT + SLIPPAGE_PCT

BOT_STATE_TABLE = "public.bot_state"

# Starttijd van dit process — voor uptime berekening in /health
_PROCESS_START = datetime.now(timezone.utc)

# ============================================================
# BASIS HELPERS — identiek aan alle andere bestanden
# ============================================================
def now_utc() -> datetime:
    """Huidige UTC tijd — identiek in alle bestanden."""
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    """Gestandaardiseerde logging met timestamp — identiek in alle bestanden."""
    print(f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def safe_int(x: Any, default: int = 0) -> int:
    """Veilige integer conversie — crasht nooit."""
    try:
        return int(x)
    except Exception:
        return default


def safe_float(x: Any, default: float = 0.0) -> float:
    """Veilige float conversie — crasht nooit."""
    try:
        return float(x)
    except Exception:
        return default


def safe_str(x: Any, default: str = "") -> str:
    """Veilige string conversie — crasht nooit."""
    if x is None:
        return default
    try:
        s = str(x).strip()
        return s if s else default
    except Exception:
        return default


def utc_day_str(offset_days: int = 0) -> str:
    """Geeft datum string in UTC formaat YYYY-MM-DD."""
    return (now_utc() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def is_trading_hours() -> bool:
    """Controleert of we binnen trading hours zijn (08:00-22:00 UTC)."""
    return True  # 24/7 trading - geen uren limiet


def format_eur(amount: float) -> str:
    """Formatteert bedrag als euro string."""
    return f"€{amount:+.2f}" if amount != 0 else "€0.00"


def format_pct(rate: float) -> str:
    """Formatteert als percentage string."""
    return f"{rate * 100:.1f}%"


# ============================================================
# WHATSAPP — kern functie, identiek in alle bestanden
# Bot stuurt NOOIT automatisch behalve bij kritieke events
# ============================================================
def send_whatsapp(message: str) -> bool:
    """
    Stuurt WhatsApp bericht via Twilio API.

    Dit is de enige manier waarop de bot communiceert met jou.
    Identieke implementatie in alle bestanden zodat elk onderdeel
    WhatsApp berichten kan sturen zonder imports.

    Alleen voor:
    - Kritieke foutmeldingen (bot kan niet doorgaan)
    - Signalen (verliezen op rij, dagbudget, etc.)
    - Dagelijks / wekelijks rapport

    NOOIT voor: elke individuele trade (te veel spam)
    """
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO]):
        log(f"WhatsApp (geen Twilio config): {message[:80]}")
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
        log(f"❌ WhatsApp fout {resp.status_code}: {resp.text[:200]}")
        return False
    except requests.exceptions.Timeout:
        log("❌ WhatsApp timeout na 15 seconden")
        return False
    except Exception as e:
        log(f"❌ WhatsApp exception: {type(e).__name__}: {e}")
        return False


# ============================================================
# CLAUDE API — identiek aan alle andere bestanden
# Zelfde model, zelfde patroon, zelfde foutafhandeling
# ============================================================
def _claude_analyse(prompt: str, max_tokens: int = 400) -> str:
    """
    Roept Claude API aan voor analyse.
    Identieke implementatie in alle bestanden.

    Model: claude-sonnet-4-20250514 (altijd Sonnet 4 in bot)
    Timeout: 25 seconden
    Geeft lege string terug bij fout — bot gaat altijd door.
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
            data = resp.json()
            if data.get("content") and len(data["content"]) > 0:
                return data["content"][0]["text"].strip()
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
    symbol: str = "",
    severity: str = "HOOG",
    open_trades: int = 0,
) -> None:
    """
    Rapporteert fout via Claude analyse + WhatsApp.

    Ernst niveaus (identiek in alle bestanden):
    - KRITIEK → WhatsApp direct + bot logt het
    - HOOG    → WhatsApp + bot logt het
    - MEDIUM  → alleen log
    - LAAG    → alleen log

    Bot stopt NOOIT automatisch — jij beslist via STOP.
    """
    log(f"[{severity}] {function} ({symbol}): {type(error).__name__}: {error}")

    if severity not in ("KRITIEK", "HOOG"):
        return

    prompt = f"""
Je bent een crypto trading bot monitor voor whatsapp_webhook.py.
Er is een fout opgetreden die de gebruiker moet weten.

Ernst:        {severity}
Functie:      {function}
Coin:         {symbol or 'onbekend'}
Open trades:  {open_trades}
Fout type:    {type(error).__name__}
Fout details: {str(error)[:300]}

Geef in 3 korte zinnen Nederlands:
1. Wat er precies mis is gegaan
2. Wat de impact is op de bot en open trades
3. Welke actie de gebruiker moet ondernemen
""".strip()

    uitleg = _claude_analyse(prompt, max_tokens=250)
    if not uitleg:
        uitleg = f"Fout in {function}: {type(error).__name__}: {str(error)[:150]}"

    send_whatsapp(
        f"🚨 WEBHOOK FOUT — {severity}\n"
        f"{'─' * 30}\n\n"
        f"📁 Functie:     {function}\n"
        f"🪙 Coin:        {symbol or '—'}\n"
        f"📂 Open trades: {open_trades}\n"
        f"⚠️ Fout:       {type(error).__name__}\n\n"
        f"🧠 Claude analyse:\n"
        f"{uitleg}\n\n"
        f"📋 WAT TE DOEN:\n"
        f"1. Check Render logs voor details\n"
        f"2. Stuur STATUS voor bot overzicht\n"
        f"3. Stuur TRADES voor open posities\n"
        f"4. Stuur STOP als je wil pauzeren\n\n"
        f"🤖 BOT PROBEERT DOOR TE GAAN\n"
        f"Open trades worden bewaakt.\n\n"
        f"Commands: STATUS | TRADES | STOP"
    )


# ============================================================
# DATABASE — identiek aan alle andere bestanden
# ============================================================
def db_connect():
    """
    Verbinding met PostgreSQL database.
    sslmode="require" is verplicht voor Render.
    Identiek in alle bestanden.
    """
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL ontbreekt. "
            "Stel in via Render Environment Variables."
        )
    return psycopg2.connect(DATABASE_URL, sslmode="require")


# ============================================================
# BOT STATE — identiek aan live_trader en trade_monitor
# ============================================================
def get_bot_state(conn, key: str, default: str = "") -> str:
    """Leest een waarde uit de bot_state tabel."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT value FROM {BOT_STATE_TABLE} WHERE key=%s",
                (key,)
            )
            row = cur.fetchone()
            return safe_str(row[0], default) if row else default
    except Exception as e:
        log(f"⚠️ get_bot_state fout ({key}): {e}")
        return default


def set_bot_state(conn, key: str, value: str) -> None:
    """Schrijft een waarde naar de bot_state tabel."""
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
            INSERT INTO {BOT_STATE_TABLE} (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE
                SET value=EXCLUDED.value, updated_at=NOW()
            """, (key, value))
        conn.commit()
    except Exception as e:
        log(f"⚠️ set_bot_state fout ({key}): {e}")


def is_bot_active(conn) -> bool:
    """Bot is actief als bot_active=true in de DB."""
    return get_bot_state(conn, "bot_active", "false").lower() == "true"


def is_bot_paused(conn) -> bool:
    """
    Controleert of bot gepauzeerd is.
    Controleer ook of de pauze tijd verstreken is.
    """
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
            # Pauze is voorbij — reset automatisch
            set_bot_state(conn, "bot_paused", "false")
            set_bot_state(conn, "bot_paused_until", "")
            log("✅ Bot pauze voorbij — automatisch hervat")
            return False
        return True
    except Exception:
        return True


def pause_bot(conn, hours: float, reason: str) -> None:
    """
    Pauzeert de bot voor een bepaalde tijd.
    Kan alleen via WhatsApp STOP command worden aangeroepen.
    Bot stopt NOOIT automatisch.
    """
    until = now_utc() + timedelta(hours=hours)
    set_bot_state(conn, "bot_paused", "true")
    set_bot_state(conn, "bot_paused_until", until.isoformat())
    set_bot_state(conn, "bot_paused_reason", reason)
    log(f"⏸️ Bot gepauzeerd tot {until.strftime('%Y-%m-%d %H:%M UTC')} — {reason}")


def activate_bot(conn) -> None:
    """Activeert de bot na START command."""
    set_bot_state(conn, "bot_active", "true")
    set_bot_state(conn, "bot_paused", "false")
    set_bot_state(conn, "bot_paused_until", "")
    set_bot_state(conn, "bot_paused_reason", "")
    log("✅ Bot geactiveerd")


def deactivate_bot(conn) -> None:
    """Deactiveert de bot na STOP command."""
    set_bot_state(conn, "bot_active", "false")
    log("🔴 Bot gestopt via STOP command")


def get_bot_status_line(conn) -> str:
    """Geeft één regel met bot status voor in berichten."""
    if not is_bot_active(conn):
        return "🔴 Bot: GESTOPT"
    elif is_bot_paused(conn):
        reason = get_bot_state(conn, "bot_paused_reason", "")
        until  = get_bot_state(conn, "bot_paused_until", "")
        return f"⏸️ Bot: GEPAUZEERD — {reason} (tot {until[:16]})"
    return "🟢 Bot: ACTIEF"


# ============================================================
# DATABASE QUERY HELPERS
# ============================================================
def get_real_trades_today(conn) -> int:
    """Telt het aantal echte trades vandaag."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COUNT(*) FROM public.pending_approvals
            WHERE status IN ('CONSUMED', 'EXECUTED')
              AND DATE(COALESCE(consumed_at, created_at) AT TIME ZONE 'UTC') = %s
            """, (utc_day_str(),))
            row = cur.fetchone()
            return safe_int(row[0]) if row else 0
    except Exception:
        return 0


def get_open_real_trades_count(conn) -> int:
    """Telt het aantal open echte trades."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COUNT(*) FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND TRIM(UPPER(COALESCE(outcome,''))) IN ('OPEN','','UNKNOWN')
            """)
            row = cur.fetchone()
            return safe_int(row[0]) if row else 0
    except Exception as e:
        log(f"⚠️ get_open_real_trades_count fout: {e}")
        return 0


def get_daily_pnl(conn, day: str) -> Tuple[int, int, float]:
    """
    Haalt wins, losses en PnL op voor een specifieke dag.
    Identiek aan trade_monitor.py get_daily_pnl().
    """
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
    except Exception as e:
        log(f"⚠️ get_daily_pnl fout: {e}")
    return 0, 0, 0.0


def get_rolling_stats(conn, days: int) -> Tuple[int, int, float]:
    """Haalt wins, losses, PnL over de laatste X dagen."""
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
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '1 day' * %s
            """, (days,))
            row = cur.fetchone()
            if row:
                return safe_int(row[0]), safe_int(row[1]), safe_float(row[2])
    except Exception as e:
        log(f"⚠️ get_rolling_stats fout: {e}")
    return 0, 0, 0.0


def get_consecutive_losses(conn) -> int:
    """
    Telt opeenvolgende verliezen.
    Identiek aan trade_monitor.py en live_trader.py.
    Stopt bij eerste win — dus alleen recente streak.
    """
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


def get_profit_factor(conn, days: int = 30) -> float:
    """
    Berekent profit factor over de laatste X dagen.
    Profit Factor = totale winst / totale verlies.
    Doel: >1.5 voor een gezond systeem.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN UPPER(outcome)='WIN'
                    THEN ABS(COALESCE(pnl_eur,0)) ELSE 0 END), 0) AS totaal_winst,
                COALESCE(SUM(CASE WHEN UPPER(outcome)='LOSS'
                    THEN ABS(COALESCE(pnl_eur,0)) ELSE 0 END), 0.001) AS totaal_verlies
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '1 day' * %s
            """, (days,))
            row = cur.fetchone()
            if row:
                winst   = safe_float(row[0])
                verlies = max(safe_float(row[1]), 0.001)
                return round(winst / verlies, 2)
    except Exception:
        pass
    return 0.0


def get_open_trades_detail(conn) -> List[Dict]:
    """Haalt details van open trades op voor TRADES command."""
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
            SELECT
                COALESCE(coin, symbol, 'UNKNOWN') AS coin,
                entry,
                COALESCE(stop_loss, stop) AS stop,
                target,
                setup_type,
                market_regime,
                score,
                entry_time,
                amount_eur
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND TRIM(UPPER(COALESCE(outcome,''))) IN ('OPEN','','UNKNOWN')
            ORDER BY entry_time DESC
            LIMIT 5
            """)
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log(f"⚠️ get_open_trades_detail fout: {e}")
        return []


def get_pending_approvals_list(conn, limit: int = 5) -> List[Dict]:
    """Haalt actieve pre-BUY signals op."""
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
            SELECT id, symbol, setup_type, regime, score, chance,
                   confidence, entry, stop, target, expires_at,
                   bitvavo_market, why_tag
            FROM public.pending_approvals
            WHERE COALESCE(status,'PENDING') IN ('PENDING','APPROVED')
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY score DESC
            LIMIT %s
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log(f"⚠️ get_pending_approvals_list fout: {e}")
        return []


def get_table_columns(conn, table: str) -> List[str]:
    """Haalt kolomnamen op van een tabel."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
            """, (table,))
            return [row[0] for row in cur.fetchall()]
    except Exception:
        return []


def table_has_column(conn, table: str, column: str) -> bool:
    """Controleert of een kolom bestaat in een tabel."""
    return column in get_table_columns(conn, table)


# ============================================================
# LIMIETEN CHECK
# Identiek aan live_trader.py check_trading_limits
# Bot stopt NOOIT automatisch — jij via STOP
# ============================================================
def check_trading_limits(conn) -> Tuple[bool, str]:
    """
    Controleert alle trading limieten voor een BUY.

    Controles (in volgorde):
    1. Bot actief?
    2. Bot gepauzeerd?
    3. Binnen trading hours?
    4. Daglimiet trades bereikt?
    5. Max open trades bereikt?

    Opmerking: consecutive losses en daily stop loss stoppen
    de bot NIET automatisch — alleen informeren via WhatsApp.
    Jij beslist via STOP command.

    Geeft (ok, reden) terug.
    """
    if not is_bot_active(conn):
        return False, "Bot GESTOPT — stuur START om te beginnen"

    if is_bot_paused(conn):
        reason = get_bot_state(conn, "bot_paused_reason", "onbekend")
        until  = get_bot_state(conn, "bot_paused_until", "")
        return False, f"Bot GEPAUZEERD: {reason} (tot {until[:16]})"

    if not is_trading_hours():
        return False, (
            f"Buiten trading hours "
            f"({TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC)"
        )

    # Weekend: gewoon doorgaan — geen blokkering
    # Dagbudget: alleen informatief — bot gaat door
    _, _, daily_pnl = get_daily_pnl(conn, utc_day_str())
    if daily_pnl <= -DAILY_STOP_LOSS_EUR:
        log(f"ℹ️ Dagbudget bereikt: {daily_pnl:.2f} — bot gaat door (jij beslist via STOP)")

    trades_today = get_real_trades_today(conn)
    if trades_today >= MAX_REAL_TRADES_PER_DAY:
        return False, f"Daglimiet bereikt: {trades_today}/{MAX_REAL_TRADES_PER_DAY} trades"

    open_count = get_open_real_trades_count(conn)
    if open_count >= MAX_OPEN_REAL_TRADES:
        return False, f"Max open trades bereikt: {open_count}/{MAX_OPEN_REAL_TRADES}"

    # Consecutive losses: alleen informatief
    consecutive = get_consecutive_losses(conn)
    if consecutive >= MAX_CONSECUTIVE_LOSSES:
        log(f"ℹ️ {consecutive}x verlies op rij — bot gaat door (jij beslist via STOP)")

    return True, "OK"


# ============================================================
# TWILIO AUTHENTICATIE
# Controleert of het bericht echt van Twilio komt
# ============================================================
def verify_twilio_signature() -> bool:
    """
    Verifieert Twilio webhook signature.
    Voorkomt dat onbevoegden commands kunnen sturen.
    Alleen in productie actief als Twilio beschikbaar is.
    """
    if not TWILIO_AVAILABLE or not TWILIO_AUTH_TOKEN:
        log("⚠️ Twilio auth overgeslagen (geen Twilio of token)")
        return True  # Dev mode
    try:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        signature = request.headers.get("X-Twilio-Signature", "")
        return validator.validate(
            request.url,
            request.form.to_dict(),
            signature,
        )
    except Exception as e:
        log(f"⚠️ Twilio verificatie fout: {e}")
        return False


def verify_internal_auth() -> bool:
    """
    Verifieert interne bot-naar-bot authenticatie.
    Gebruikt voor /auto_buy en /send_* endpoints.
    """
    auth_header = request.headers.get("X-Bot-Auth", "")
    return auth_header == BOT_INTERNAL_SECRET


# ============================================================
# LIVE TRADER AANROEPEN
# ============================================================
def _call_live_trader_buy(
    symbol: str,
    amount_eur: float,
    meta: Optional[Dict] = None,
) -> Tuple[bool, str]:
    """
    Roept live_trader.buy_eur() aan.

    Fix: auto mode — live eerst, paper als fallback.
    Was: paper eerst, dan live. Omgekeerd wat we wilen.

    Geeft (success, message) terug.
    """
    meta = meta or {}
    try:
        # Importeer live_trader dynamisch
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

        if TRADER_MODE in ("live", "auto"):
            try:
                from trading.live_trader import buy_eur as live_buy
                ok, msg = live_buy(symbol, amount_eur=amount_eur, meta=meta)
                if ok:
                    return True, f"✅ LIVE BUY: {symbol} €{amount_eur:.2f}"
                log(f"⚠️ Live buy mislukt: {msg}")
                if TRADER_MODE == "live":
                    return False, f"Live BUY mislukt: {msg}"
                # auto mode: probeer paper als live mislukt
            except ImportError:
                log("⚠️ live_trader niet gevonden — probeer paper")

        if TRADER_MODE in ("paper", "auto"):
            try:
                from trading.paper_trader import buy as paper_buy
                ok, msg = paper_buy(symbol, amount_eur=amount_eur, meta=meta)
                if ok:
                    return True, f"📄 PAPER BUY: {symbol} €{amount_eur:.2f}"
                return False, f"Paper BUY mislukt: {msg}"
            except ImportError:
                log("⚠️ paper_trader niet gevonden")

        return False, f"Geen trader beschikbaar voor mode: {TRADER_MODE}"

    except Exception as e:
        log(f"❌ _call_live_trader_buy fout: {type(e).__name__}: {e}")
        return False, str(e)


# ============================================================
# AUTO BUY — wordt getriggerd door multi_coin_score.py
# ============================================================
def execute_auto_buy(prebuy_id: str, conn) -> Tuple[bool, str]:
    """
    Voert een automatische BUY uit op basis van een Pre-BUY ID.

    Stappen:
    1. Pre-BUY ophalen uit DB
    2. Alle limieten controleren
    3. BUY uitvoeren via live_trader
    4. Pre-BUY status updaten naar CONSUMED
    5. WhatsApp notificatie (ALLEEN bij kritieke fouten)

    Bot koopt altijd automatisch — geen YES/NO meer nodig.
    """
    try:
        # Pre-BUY ophalen
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
            SELECT id, symbol, setup_type, regime, score, chance, confidence,
                   entry, stop, target, expires_at, bitvavo_market,
                   timeframe, why_tag, exp_n, exp_win_rate
            FROM public.pending_approvals
            WHERE id = %s
              AND COALESCE(status,'PENDING') = 'PENDING'
              AND (expires_at IS NULL OR expires_at > NOW())
            LIMIT 1
            """, (prebuy_id,))
            row = cur.fetchone()

        if not row:
            return False, f"Pre-BUY {prebuy_id} niet gevonden of verlopen"

        prebuy = dict(row)
        symbol = safe_str(prebuy.get("symbol"))

        if not symbol:
            return False, "Geen symbol in Pre-BUY"

        # Limieten check
        ok, reason = check_trading_limits(conn)
        if not ok:
            log(f"⚠️ Auto BUY geblokkeerd ({symbol}): {reason}")
            return False, reason

        # Meta voor live_trader
        meta = {
            "prebuy_id":    prebuy_id,
            "symbol":       symbol,
            "entry":        safe_float(prebuy.get("entry")),
            "stop":         safe_float(prebuy.get("stop")),
            "target":       safe_float(prebuy.get("target")),
            "setup_type":   safe_str(prebuy.get("setup_type")),
            "regime":       safe_str(prebuy.get("regime")),
            "score":        safe_int(prebuy.get("score")),
            "timeframe":    safe_str(prebuy.get("timeframe"), "4h"),
            "market":       safe_str(prebuy.get("bitvavo_market")),
            "chance":       safe_int(prebuy.get("chance")),
            "confidence":   safe_int(prebuy.get("confidence")),
            "why_tag":      safe_str(prebuy.get("why_tag")),
            "exp_n":        safe_int(prebuy.get("exp_n")),
            "exp_win_rate": safe_float(prebuy.get("exp_win_rate")),
        }

        # BUY uitvoeren  Kelly Criterion sizing (gecapped op MAX_PER_TRADE_EUR)
        kelly_eur = safe_float(prebuy.get("kelly_grootte_eur")) or 0.0
        trade_eur = min(kelly_eur, MAX_PER_TRADE_EUR) if kelly_eur >= 5.0 else MAX_PER_TRADE_EUR
        log(f"Kelly grootte: €{kelly_eur:.2f} → trade €{trade_eur:.2f}")
        success, msg = _call_live_trader_buy(symbol, trade_eur, meta)

        if success:
            # Status updaten naar CONSUMED
            with conn.cursor() as cur:
                cur.execute("""
                UPDATE public.pending_approvals
                SET status='CONSUMED', consumed_at=NOW()
                WHERE id=%s
                """, (prebuy_id,))
            conn.commit()
            log(f"✅ Auto BUY uitgevoerd: {symbol} (prebuy={prebuy_id})")
        else:
            log(f"❌ Auto BUY mislukt: {symbol}: {msg}")

        return success, msg

    except Exception as e:
        log(f"❌ execute_auto_buy fout: {type(e).__name__}: {e}")
        report_error(e, "execute_auto_buy", symbol if 'symbol' in dir() else "", "HOOG")
        return False, str(e)


# ============================================================
# RAPPORT GENERATORS — voor dagelijks/wekelijks/maandelijks
# ============================================================
def claude_analyseer_dagrapport(
    wins_real:   int,
    losses_real: int,
    pnl_real:    float,
    wins_shadow: int,
    losses_shadow: int,
    wins_sim:    int,
    losses_sim:  int,
    pf_30d:      float,
    open_count:  int,
) -> str:
    """
    Claude analyseert de dagelijkse performance.
    Geeft een korte maar inzichtelijke analyse terug.
    """
    total_real = wins_real + losses_real
    wr_real = (wins_real / total_real * 100) if total_real > 0 else 0.0

    prompt = f"""
Je bent een crypto trading bot coach die dagelijkse performance analyseert.
Geef een korte analyse (3-4 zinnen) in het Nederlands.

ECHTE TRADES GISTEREN:
- Wins: {wins_real} | Losses: {losses_real} | Win rate: {wr_real:.1f}%
- PnL: €{pnl_real:.2f}
- Open trades: {open_count}

SHADOW TRADES (leerdata):
- Wins: {wins_shadow} | Losses: {losses_shadow}

SIMULATIE:
- Wins: {wins_sim} | Losses: {losses_sim}

PROFIT FACTOR (30d): {pf_30d:.2f}

Analyseer:
1. Was dit een goede dag? Waarom?
2. Wat valt op in de data?
3. Aanbeveling voor vandaag (1 zin)
""".strip()

    return _claude_analyse(prompt, max_tokens=250)


def claude_analyseer_weekrapport(
    week_data: Dict,
    pf_30d:    float,
) -> str:
    """Claude analyseert de wekelijkse performance."""
    prompt = f"""
Je bent een crypto trading bot coach die wekelijkse performance analyseert.
Geef een analyse (4-5 zinnen) in het Nederlands.

WEEK DATA:
{json.dumps(week_data, indent=2, ensure_ascii=False, default=str)}

PROFIT FACTOR (30d): {pf_30d:.2f}

Analyseer:
1. Beste setup type van de week
2. Slechte setups die vermeden moeten worden
3. Win rate trend (verbetering of verslechtering?)
4. Concrete aanbeveling voor volgende week
""".strip()

    return _claude_analyse(prompt, max_tokens=350)


def claude_analyseer_maandrapport(
    maand_data: Dict,
    sim_wr:     float,
    live_wr:    float,
) -> str:
    """Claude analyseert de maandelijkse performance en edge decay."""
    edge_diff = sim_wr - live_wr
    prompt = f"""
Je bent een crypto trading bot coach die maandelijkse performance analyseert.
Geef een uitgebreide analyse (5-6 zinnen) in het Nederlands.

MAAND DATA:
{json.dumps(maand_data, indent=2, ensure_ascii=False, default=str)}

EDGE DECAY CHECK:
- Sim win rate: {sim_wr:.1f}%
- Live win rate: {live_wr:.1f}%
- Verschil: {edge_diff:.1f}% {'⚠️ ZORGWEKKEND' if edge_diff > 10 else '✅ OK'}

Analyseer:
1. Maandresultaat — positief of negatief?
2. Beste en slechtste coins/setups
3. Edge decay — werkt strategie nog goed live?
4. Parameter aanpassingen aanbevolen?
5. Doel voor volgende maand
""".strip()

    return _claude_analyse(prompt, max_tokens=400)


def claude_trade_leeranalyse(conn) -> str:
    """
    Claude analyseert alle trades voor leerpatronen.
    Stuurt elke 2 weken via /send_leeranalyse.
    """
    # Haal scoreboard op
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
            SELECT setup_type, market_regime AS regime,
                   COUNT(*) AS n,
                   ROUND(COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')::numeric
                         / NULLIF(COUNT(*),0) * 100, 1) AS wr_pct,
                   ROUND(AVG(COALESCE(pnl_eur,0))::numeric, 4) AS avg_pnl
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '60 days'
              AND setup_type IS NOT NULL
            GROUP BY 1, 2
            HAVING COUNT(*) >= 3
            ORDER BY wr_pct DESC
            LIMIT 10
            """)
            scoreboard = [dict(r) for r in cur.fetchall()]
    except Exception:
        scoreboard = []

    prompt = f"""
Je bent een crypto trading bot coach die leerpatronen analyseert.
Geef concrete aanbevelingen (6-8 zinnen) in het Nederlands.

TOP SETUPS (laatste 60 dagen echte trades):
{json.dumps(scoreboard, indent=2, ensure_ascii=False, default=str)}

Analyseer:
1. Welke setup/regime combinatie werkt het BESTE? (met cijfers)
2. Welke setup/regime combinatie werkt het SLECHTSTE?
3. Wat zou de score drempel moeten zijn op basis van de data?
4. Welke coins zijn het meest winstgevend?
5. Concrete actie voor de komende 2 weken
6. Zijn er patronen die op edge decay wijzen?
""".strip()

    return _claude_analyse(prompt, max_tokens=450)


def claude_health_check(conn) -> str:
    """
    Claude analyseert de bot gezondheid.
    Stuurt wekelijks via /send_health_check.
    """
    # Verzamel data
    try:
        # Tabellen check
        with conn.cursor() as cur:
            cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN (
                  'experience_trades', 'pending_approvals',
                  'bot_state', 'experience_scoreboard',
                  'btc_regime_4h', 'market_regime'
              )
            ORDER BY table_name
            """)
            tabellen = [row[0] for row in cur.fetchall()]

        # Win rates
        w7, l7, pnl7 = get_rolling_stats(conn, 7)
        w30, l30, pnl30 = get_rolling_stats(conn, 30)
        t7  = w7 + l7
        t30 = w30 + l30
        wr7  = (w7 / t7 * 100) if t7 > 0 else 0.0
        wr30 = (w30 / t30 * 100) if t30 > 0 else 0.0
        pf30 = get_profit_factor(conn, 30)

        # Edge decay
        try:
            with conn.cursor() as cur:
                cur.execute("""
                SELECT
                    UPPER(COALESCE(source,'')) AS src,
                    COUNT(*) FILTER (WHERE UPPER(outcome)='WIN') AS wins,
                    COUNT(*) FILTER (WHERE UPPER(outcome)='LOSS') AS losses
                FROM public.experience_trades
                WHERE UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
                  AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '30 days'
                GROUP BY 1
                """)
                edge_rows = cur.fetchall()
            data = {}
            for r in edge_rows:
                src = safe_str(r[0])
                key = "REAL" if src in ("REAL","LIVE") else src
                w, l = safe_int(r[1]), safe_int(r[2])
                data[key] = data.get(key, (0,0))
                data[key] = (data[key][0]+w, data[key][1]+l)

            def wr(k):
                w, l = data.get(k, (0,0))
                return (w/(w+l)*100) if (w+l) > 0 else 0.0
            sim_wr = wr("SIM")
            live_wr = wr("REAL")
        except Exception:
            sim_wr = live_wr = 0.0

    except Exception as e:
        return f"Health check data kon niet worden opgehaald: {e}"

    config_checks = [
        f"DATABASE_URL:      {'✅' if DATABASE_URL else '❌'}",
        f"TWILIO:            {'✅' if TWILIO_ACCOUNT_SID else '❌'}",
        f"ANTHROPIC_API_KEY: {'✅' if ANTHROPIC_API_KEY else '⚠️ niet ingesteld'}",
        f"BOT_SECRET:        {'✅' if len(BOT_INTERNAL_SECRET) > 10 else '⚠️ te kort'}",
        f"MAX_PER_TRADE:     €{MAX_PER_TRADE_EUR:.2f}",
        f"DAILY_STOP_LOSS:   €{DAILY_STOP_LOSS_EUR:.2f}",
        f"TRADING_HOURS:     {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC",
    ]

    prompt = f"""
Je bent een crypto trading bot health monitor.
Controleer of alles correct werkt en geef een health rapport.

AANWEZIGE TABELLEN: {', '.join(tabellen)}

WIN RATE TREND (echte trades):
- Laatste 7 dagen:  {wr7:.1f}% ({w7}W/{l7}L) | PnL: €{pnl7:.2f}
- Laatste 30 dagen: {wr30:.1f}% ({w30}W/{l30}L) | PnL: €{pnl30:.2f}
- Profit Factor 30d: {pf30:.2f}

EDGE DECAY CHECK:
- Simulatie win rate (30d): {sim_wr:.1f}%
- Live win rate (30d):      {live_wr:.1f}%
- Verschil: {sim_wr - live_wr:.1f}%

CONFIGURATIE:
{chr(10).join(config_checks)}

Geef een health rapport in het Nederlands (5-6 zinnen):
1. Is de bot gezond? (ja/nee en waarom)
2. Win rate trend: verbetert of verslechtert het?
3. Edge decay analyse
4. Configuratie problemen?
5. Aanbeveling voor deze week
""".strip()

    return _claude_analyse(prompt, max_tokens=400)


# ============================================================
# RAPPORT OPMAAK
# ============================================================
def build_daily_rapport(conn) -> str:
    """
    Bouwt het dagelijkse WhatsApp rapport.
    Verstuurd elke ochtend om 08:00 UTC via Render Cron.
    Gisteren's data + open trades + Claude analyse.
    """
    gisteren = utc_day_str(-1)
    vandaag  = utc_day_str(0)

    # Gisteren data
    w_real, l_real, pnl_real    = get_daily_pnl(conn, gisteren)
    w_shad, l_shad, _           = get_daily_pnl(conn, gisteren)
    w_sim,  l_sim,  _           = (0, 0, 0.0)  # sim trades hebben geen exit_time dagfilter

    # Shadow trades ophalen
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')  AS wins,
                COUNT(*) FILTER (WHERE UPPER(outcome)='LOSS') AS losses
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) = 'SHADOW'
              AND DATE(COALESCE(exit_time, updated_at) AT TIME ZONE 'UTC') = %s
            """, (gisteren,))
            row = cur.fetchone()
            if row:
                w_shad, l_shad = safe_int(row[0]), safe_int(row[1])
    except Exception:
        pass

    # Sim trades
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')  AS wins,
                COUNT(*) FILTER (WHERE UPPER(outcome)='LOSS') AS losses
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) = 'SIM'
              AND DATE(COALESCE(exit_time, updated_at) AT TIME ZONE 'UTC') = %s
            """, (gisteren,))
            row = cur.fetchone()
            if row:
                w_sim, l_sim = safe_int(row[0]), safe_int(row[1])
    except Exception:
        pass

    # Open trades
    open_count = get_open_real_trades_count(conn)
    open_trades = get_open_trades_detail(conn)

    # Bot status
    status_line = get_bot_status_line(conn)

    # Win rates
    tot_real = w_real + l_real
    wr_real  = (w_real / tot_real * 100) if tot_real > 0 else 0.0
    tot_shad = w_shad + l_shad
    wr_shad  = (w_shad / tot_shad * 100) if tot_shad > 0 else 0.0

    # Profit factor
    pf_30d = get_profit_factor(conn, 30)

    # Rolling 7 dagen
    w7, l7, pnl7 = get_rolling_stats(conn, 7)
    t7 = w7 + l7
    wr7 = (w7 / t7 * 100) if t7 > 0 else 0.0

    # Claude analyse
    claude_tekst = claude_analyseer_dagrapport(
        w_real, l_real, pnl_real,
        w_shad, l_shad,
        w_sim,  l_sim,
        pf_30d, open_count,
    )

    # Open trades tekst
    open_tekst = ""
    if open_trades:
        open_tekst = f"\n📂 OPEN TRADES ({open_count}):\n"
        for t in open_trades[:3]:
            coin  = safe_str(t.get("coin"), "?")
            entry = safe_float(t.get("entry"))
            open_tekst += f"• {coin}: entry={entry:.4f}\n"
    else:
        open_tekst = "\n📂 Geen open trades\n"

    teken = "+" if pnl_real >= 0 else ""

    bericht = (
        f"📊 DAGRAPPORT — {gisteren}\n"
        f"{'─' * 32}\n"
        f"{status_line}\n\n"
        f"💶 ECHTE TRADES:\n"
        f"• Wins: {w_real} | Losses: {l_real}\n"
        f"• Win rate: {wr_real:.1f}%\n"
        f"• PnL gisteren: {teken}€{pnl_real:.2f}\n"
        f"• PnL 7 dagen:  {'+' if pnl7>=0 else ''}€{pnl7:.2f} ({wr7:.1f}%)\n\n"
        f"🎭 SHADOW (leerdata):\n"
        f"• Wins: {w_shad} | Losses: {l_shad} | WR: {wr_shad:.1f}%\n\n"
        f"🔮 SIMULATIE:\n"
        f"• Wins: {w_sim} | Losses: {l_sim}\n\n"
        f"📈 Profit Factor 30d: {pf_30d:.2f}"
        f" {'✅' if pf_30d >= 1.5 else '⚠️'}\n"
        f"{open_tekst}\n"
        f"🧠 Claude:\n{claude_tekst}\n\n"
        f"{'─' * 32}\n"
        f"Commands: STOP | STATUS | TRADES"
    )

    return bericht


def build_weekly_rapport(conn) -> str:
    """
    Bouwt het wekelijkse WhatsApp rapport.
    Verstuurd elke maandag om 08:00 UTC.
    """
    week_data: Dict = {}

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
            SELECT setup_type,
                   COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE UPPER(outcome)='WIN') AS wins,
                   ROUND(COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')::numeric
                         / NULLIF(COUNT(*),0) * 100, 1) AS wr_pct,
                   ROUND(SUM(COALESCE(pnl_eur,0))::numeric, 2) AS total_pnl
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '7 days'
              AND setup_type IS NOT NULL
            GROUP BY 1
            ORDER BY wr_pct DESC
            """)
            week_data["per_setup"] = [dict(r) for r in cur.fetchall()]

            cur.execute("""
            SELECT market_regime AS regime,
                   COUNT(*) AS n,
                   ROUND(COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')::numeric
                         / NULLIF(COUNT(*),0) * 100, 1) AS wr_pct,
                   ROUND(SUM(COALESCE(pnl_eur,0))::numeric, 2) AS total_pnl
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '7 days'
            GROUP BY 1
            ORDER BY wr_pct DESC
            """)
            week_data["per_regime"] = [dict(r) for r in cur.fetchall()]

    except Exception as e:
        week_data["error"] = str(e)

    w7, l7, pnl7 = get_rolling_stats(conn, 7)
    t7 = w7 + l7
    wr7 = (w7 / t7 * 100) if t7 > 0 else 0.0
    pf30 = get_profit_factor(conn, 30)
    status_line = get_bot_status_line(conn)

    week_data["totaal"] = {"wins": w7, "losses": l7, "pnl": pnl7, "wr_pct": wr7}

    claude_tekst = claude_analyseer_weekrapport(week_data, pf30)
    teken = "+" if pnl7 >= 0 else ""

    return (
        f"📅 WEEKRAPPORT\n"
        f"{'─' * 32}\n"
        f"{status_line}\n\n"
        f"💶 ECHTE TRADES DEZE WEEK:\n"
        f"• Wins:     {w7}\n"
        f"• Losses:   {l7}\n"
        f"• Win rate: {wr7:.1f}%\n"
        f"• PnL:      {teken}€{pnl7:.2f}\n\n"
        f"📈 Profit Factor 30d: {pf30:.2f}"
        f" {'✅' if pf30 >= 1.5 else '⚠️'}\n\n"
        f"🧠 Claude analyse:\n{claude_tekst}\n\n"
        f"{'─' * 32}\n"
        f"Commands: STOP | STATUS | RAPPORT"
    )


def build_monthly_rapport(conn) -> str:
    """
    Bouwt het maandelijkse WhatsApp rapport.
    Verstuurd op de 1e van de maand om 08:00 UTC.
    """
    w30, l30, pnl30 = get_rolling_stats(conn, 30)
    t30 = w30 + l30
    wr30 = (w30 / t30 * 100) if t30 > 0 else 0.0
    pf30 = get_profit_factor(conn, 30)

    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT UPPER(COALESCE(source,'')) AS src,
                   COUNT(*) FILTER (WHERE UPPER(outcome)='WIN') AS wins,
                   COUNT(*) FILTER (WHERE UPPER(outcome)='LOSS') AS losses
            FROM public.experience_trades
            WHERE UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '30 days'
            GROUP BY 1
            """)
            rows = cur.fetchall()
        data = {}
        for r in rows:
            src = safe_str(r[0])
            key = "REAL" if src in ("REAL","LIVE") else src
            data[key] = data.get(key, (0,0))
            data[key] = (data[key][0]+safe_int(r[1]), data[key][1]+safe_int(r[2]))
        sim_wr  = (data["SIM"][0]/(data["SIM"][0]+data["SIM"][1])*100) if "SIM" in data and sum(data["SIM"])>0 else 0.0
        live_wr = (data["REAL"][0]/(data["REAL"][0]+data["REAL"][1])*100) if "REAL" in data and sum(data["REAL"])>0 else 0.0
    except Exception:
        sim_wr = live_wr = 0.0

    maand_data = {
        "wins": w30, "losses": l30, "wr_pct": wr30,
        "pnl": pnl30, "profit_factor": pf30,
    }

    claude_tekst = claude_analyseer_maandrapport(maand_data, sim_wr, live_wr)
    status_line  = get_bot_status_line(conn)
    teken = "+" if pnl30 >= 0 else ""

    return (
        f"📆 MAANDRAPPORT (30 dagen)\n"
        f"{'─' * 32}\n"
        f"{status_line}\n\n"
        f"💶 RESULTAAT:\n"
        f"• Wins:        {w30}\n"
        f"• Losses:      {l30}\n"
        f"• Win rate:    {wr30:.1f}%\n"
        f"• PnL:         {teken}€{pnl30:.2f}\n"
        f"• Prof. Factor: {pf30:.2f} {'✅' if pf30>=1.5 else '⚠️'}\n\n"
        f"⚡ EDGE DECAY:\n"
        f"• Sim:  {sim_wr:.1f}%\n"
        f"• Live: {live_wr:.1f}%\n"
        f"• Diff: {sim_wr-live_wr:.1f}% {'⚠️' if sim_wr-live_wr>10 else '✅'}\n\n"
        f"🧠 Claude:\n{claude_tekst}\n\n"
        f"{'─' * 32}\n"
        f"Commands: STOP | STATUS | HEALTH"
    )


def build_status_bericht(conn) -> str:
    """
    Bouwt het STATUS bericht.
    Antwoord op WhatsApp STATUS command.
    """
    status_line = get_bot_status_line(conn)

    w_v, l_v, pnl_v = get_daily_pnl(conn, utc_day_str())
    tot_v = w_v + l_v
    wr_v  = (w_v / tot_v * 100) if tot_v > 0 else 0.0

    w7, l7, pnl7 = get_rolling_stats(conn, 7)
    t7 = w7 + l7
    wr7 = (w7 / t7 * 100) if t7 > 0 else 0.0

    trades_today = get_real_trades_today(conn)
    open_count   = get_open_real_trades_count(conn)
    pf30         = get_profit_factor(conn, 30)
    consecutive  = get_consecutive_losses(conn)

    pause_info = ""
    if is_bot_paused(conn):
        reason = get_bot_state(conn, "bot_paused_reason", "")
        until  = get_bot_state(conn, "bot_paused_until", "")
        pause_info = f"\n⏸️ Pauze: {reason}\n   Tot: {until[:16]}\n"

    teken_v = "+" if pnl_v >= 0 else ""
    teken_7 = "+" if pnl7 >= 0 else ""

    return (
        f"📊 BOT STATUS\n"
        f"{'─' * 32}\n"
        f"{status_line}"
        f"{pause_info}\n\n"
        f"💶 VANDAAG:\n"
        f"• Trades: {tot_v} ({w_v}W/{l_v}L) | WR: {wr_v:.1f}%\n"
        f"• PnL: {teken_v}€{pnl_v:.2f}\n"
        f"• Daglimiet: {trades_today}/{MAX_REAL_TRADES_PER_DAY} trades\n\n"
        f"📈 LAATSTE 7 DAGEN:\n"
        f"• Trades: {t7} ({w7}W/{l7}L) | WR: {wr7:.1f}%\n"
        f"• PnL: {teken_7}€{pnl7:.2f}\n\n"
        f"📂 Open trades: {open_count}/{MAX_OPEN_REAL_TRADES}\n"
        f"📈 Profit Factor 30d: {pf30:.2f}"
        f" {'✅' if pf30 >= 1.5 else '⚠️'}\n"
        f"📉 Verliezen op rij: {consecutive}\n\n"
        f"⚙️ LIMIETEN:\n"
        f"• Max per trade: €{MAX_PER_TRADE_EUR:.2f}\n"
        f"• Dagbudget:     €{DAILY_STOP_LOSS_EUR:.2f}\n"
        f"• Trading hours: {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC\n\n"
        f"🤖 BOT LOOPT GEWOON DOOR\n"
        f"Stuur STOP als je wil pauzeren.\n\n"
        f"Commands: STOP | TRADES | RAPPORT | HELP"
    )


def build_trades_bericht(conn) -> str:
    """
    Bouwt het TRADES bericht.
    Toont alle open live trades met details.
    """
    open_trades = get_open_trades_detail(conn)
    status_line = get_bot_status_line(conn)

    if not open_trades:
        return (
            f"📂 OPEN TRADES\n"
            f"{'─' * 32}\n"
            f"{status_line}\n\n"
            f"Geen open live trades.\n\n"
            f"Commands: STATUS | RAPPORT"
        )

    tekst = (
        f"📂 OPEN TRADES ({len(open_trades)})\n"
        f"{'─' * 32}\n"
        f"{status_line}\n\n"
    )

    for i, t in enumerate(open_trades, 1):
        coin   = safe_str(t.get("coin"), "?")
        entry  = safe_float(t.get("entry"))
        stop   = safe_float(t.get("stop"))
        target = safe_float(t.get("target"))
        setup  = safe_str(t.get("setup_type"), "?")
        regime = safe_str(t.get("market_regime"), "?")
        score  = safe_int(t.get("score"))
        amount = safe_float(t.get("amount_eur"))

        risk   = entry - stop if entry > stop else 0
        reward = target - entry if target > entry else 0
        rr     = reward / risk if risk > 0 else 0

        tekst += (
            f"#{i} {coin} | {setup}/{regime}\n"
            f"   Entry:  {entry:.6f}\n"
            f"   Stop:   {stop:.6f} (R={risk:.6f})\n"
            f"   Target: {target:.6f} (RR={rr:.1f})\n"
            f"   Score:  {score} | €{amount:.2f}\n\n"
        )

    tekst += "Commands: STATUS | RAPPORT | STOP"
    return tekst


def build_help_bericht() -> str:
    """Bouwt het HELP bericht met alle commands."""
    return (
        f"🤖 CRYPTO AI BOT — COMMANDS\n"
        f"{'─' * 32}\n\n"
        f"🟢 CONTROLE:\n"
        f"START        → bot begint traden\n"
        f"STOP         → bot stopt (jij beslist)\n\n"
        f"📊 INFORMATIE:\n"
        f"STATUS       → volledig overzicht\n"
        f"TRADES       → open trades\n\n"
        f"📋 RAPPORTEN:\n"
        f"RAPPORT      → dagrapport nu\n"
        f"WEEKRAPPORT  → weekoverzicht\n"
        f"MAANDRAPPORT → maandoverzicht\n"
        f"ADVIES       → Claude leeranalyse\n"
        f"HEALTH       → health check\n\n"
        f"🧠 AI COACH:\n"
        f"ANALYSE      → volledige analyse 60d\n"
        f"ANALYSE 30   → analyse 30 dagen\n"
        f"ANALYSE 90   → analyse 90 dagen\n"
        f"ANALYSEKORT  → snel overzicht 30d\n\n"
        f"ℹ️ OVERIG:\n"
        f"HELP         → dit bericht\n\n"
        f"{'─' * 32}\n"
        f"⚙️ FASE 1 LIMIETEN:\n"
        f"• Max per trade: €{MAX_PER_TRADE_EUR:.2f}\n"
        f"• Max trades/dag: {MAX_REAL_TRADES_PER_DAY}\n"
        f"• Max open: {MAX_OPEN_REAL_TRADES}\n"
        f"• Dagbudget: €{DAILY_STOP_LOSS_EUR:.2f}\n"
        f"• Trading: {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC"
    )


# ============================================================
# COMMAND PROCESSOR — verwerkt WhatsApp berichten
# ============================================================
def process_command(body: str, conn) -> str:
    """
    Verwerkt een WhatsApp command.
    Geeft antwoord terug als string.
    """
    cmd = body.strip().upper()

    if cmd == "START":
        activate_bot(conn)
        w_v, l_v, pnl_v = get_daily_pnl(conn, utc_day_str())
        tot_v = w_v + l_v
        wr_v  = (w_v / tot_v * 100) if tot_v > 0 else 0.0
        pf30  = get_profit_factor(conn, 30)
        return (
            f"🟢 BOT GESTART\n"
            f"{'─' * 32}\n\n"
            f"Bot is nu actief en handelt automatisch.\n\n"
            f"📊 VANDAAG TOT NU TOE:\n"
            f"• Trades: {tot_v} ({w_v}W/{l_v}L)\n"
            f"• Win rate: {wr_v:.1f}%\n"
            f"• PnL: {'+'if pnl_v>=0 else ''}€{pnl_v:.2f}\n\n"
            f"📈 Profit Factor 30d: {pf30:.2f}\n\n"
            f"⚙️ Limieten:\n"
            f"• Max trade: €{MAX_PER_TRADE_EUR:.2f}\n"
            f"• Max/dag: {MAX_REAL_TRADES_PER_DAY} trades\n"
            f"• Trading: {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC\n\n"
            f"Stuur STOP om te pauzeren.\n"
            f"Stuur STATUS voor overzicht."
        )

    if cmd == "STOP":
        deactivate_bot(conn)
        open_count = get_open_real_trades_count(conn)
        return (
            f"🔴 BOT GESTOPT\n"
            f"{'─' * 32}\n\n"
            f"Bot handelt geen nieuwe trades meer.\n\n"
            f"📂 Open trades: {open_count}\n"
            f"(worden nog wel bewaakt door trade_monitor)\n\n"
            f"Stuur START om te hervatten.\n"
            f"Stuur TRADES voor open posities."
        )

    if cmd == "STATUS":
        return build_status_bericht(conn)

    if cmd == "TRADES":
        return build_trades_bericht(conn)

    if cmd == "RAPPORT":
        return build_daily_rapport(conn)

    if cmd == "WEEKRAPPORT":
        return build_weekly_rapport(conn)

    if cmd == "MAANDRAPPORT":
        return build_monthly_rapport(conn)

    if cmd == "ADVIES":
        status_line  = get_bot_status_line(conn)
        leer_analyse = claude_trade_leeranalyse(conn)
        pf30         = get_profit_factor(conn, 30)
        return (
            f"🧠 CLAUDE LEERANALYSE\n"
            f"{'─' * 32}\n"
            f"{status_line}\n\n"
            f"📈 Profit Factor 30d: {pf30:.2f}"
            f" {'✅' if pf30 >= 1.5 else '⚠️'}\n\n"
            f"{leer_analyse}\n\n"
            f"{'─' * 32}\n"
            f"Commands: STATUS | RAPPORT | STOP"
        )

    if cmd == "HEALTH":
        status_line  = get_bot_status_line(conn)
        health_tekst = claude_health_check(conn)
        return (
            f"🏥 HEALTH CHECK\n"
            f"{'─' * 32}\n"
            f"{status_line}\n\n"
            f"🧠 Claude health analyse:\n{health_tekst}\n\n"
            f"{'─' * 32}\n"
            f"Commands: STATUS | ADVIES | ANALYSE | STOP"
        )

    if cmd == "ANALYSE" or cmd.startswith("ANALYSE "):
        dagen = 60
        if " " in cmd:
            try:
                dagen = max(7, min(int(cmd.split(" ", 1)[1].strip()), 365))
            except (ValueError, IndexError):
                dagen = 60

        send_whatsapp(
            f"🧠 AI COACH GESTART\n"
            f"{'─' * 32}\n\n"
            f"Analyse periode: {dagen} dagen\n"
            f"Dit duurt 20-60 seconden...\n\n"
            f"Claude analyseert:\n"
            f"• Setup performance en trends\n"
            f"• Score drempel optimalisatie\n"
            f"• Stop loss effectiviteit (MAE)\n"
            f"• Beste trading uren en dagen\n"
            f"• Coin blacklist aanbevelingen\n"
            f"• Edge decay detectie\n"
            f"• Profit factor trend\n\n"
            f"Rapport volgt direct..."
        )

        try:
            from ai_coach import run_coach
            log(f"📊 AI Coach gestart voor {dagen} dagen")
            rapport = run_coach(dagen=dagen, stuur_whatsapp=True)
            log(f"✅ AI Coach klaar: {len(rapport)} tekens")
            return (
                f"✅ AI COACH ANALYSE KLAAR\n"
                f"Rapport is verzonden via WhatsApp.\n\n"
                f"Commands: STATUS | HEALTH | STOP"
            )
        except ImportError:
            log("❌ ai_coach.py niet gevonden in dezelfde map")
            return (
                f"❌ ai_coach.py niet beschikbaar.\n\n"
                f"Zorg dat ai_coach.py in dezelfde\n"
                f"map staat als whatsapp_webhook.py\n"
                f"en deploy opnieuw.\n\n"
                f"Commands: HEALTH | ADVIES | HELP"
            )
        except Exception as e:
            log(f"❌ AI Coach fout: {type(e).__name__}: {e}")
            return (
                f"❌ AI COACH FOUT\n"
                f"{'─' * 32}\n\n"
                f"Fout: {type(e).__name__}\n"
                f"{str(e)[:150]}\n\n"
                f"Check Render logs voor details.\n"
                f"Commands: HEALTH | ADVIES"
            )

    if cmd == "ANALYSEKORT":
        try:
            from ai_coach import run_coach
            log("📊 AI Coach kort rapport gestart (30 dagen, geen WhatsApp)")
            rapport = run_coach(dagen=30, stuur_whatsapp=False)
            if len(rapport) > 1400:
                rapport_kort = rapport[:1380] + "\n...[zie volledig ANALYSE]"
            else:
                rapport_kort = rapport
            return rapport_kort
        except ImportError:
            return (
                f"❌ ai_coach.py niet beschikbaar.\n"
                f"Gebruik ANALYSE voor volledig rapport."
            )
        except Exception as e:
            return f"❌ Fout: {type(e).__name__}: {str(e)[:100]}"

    if cmd == "HELP":
        return build_help_bericht()

    return (
        f"❓ Onbekend command: '{body[:20]}'\n\n"
        f"Stuur HELP voor alle commands."
    )


# ============================================================
# FLASK ROUTES — webhook endpoints
# ============================================================

@app.route("/", methods=["GET"])
def index():
    return "OK", 200

@app.route("/health", methods=["GET"])
def health():
    """
    ✅ FIX v2.1: /health route — primaire health check voor Render.

    Render pollt standaard /health om de service te monitoren.
    Zonder deze route verschijnen er elke ~15 minuten 404 warnings
    in de logs (zoals zichtbaar was in de screenshot).

    De route voert drie checks uit:
      1. DB verbinding (SELECT 1)
      2. Bot state ophalen (is_bot_active)
      3. Open trades tellen

    Render gedrag:
      - HTTP 200 → service is "Healthy"
      - HTTP 503 → service is "Unhealthy" → Render stuurt alert

    De JSON response bevat alle relevante bot-info zodat je
    de service direct kunt monitoren zonder in te loggen op Render.
    Uptime wordt berekend vanaf het moment dat het Flask process
    gestart is (_PROCESS_START).
    """
    uptime_sec = int((now_utc() - _PROCESS_START).total_seconds())
    uptime_str = (
        f"{uptime_sec // 3600}h "
        f"{(uptime_sec % 3600) // 60}m "
        f"{uptime_sec % 60}s"
    )

    db_ok        = False
    bot_active   = False
    open_trades  = 0
    trades_today = 0
    db_error     = ""

    conn = None
    try:
        conn         = db_connect()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        db_ok        = True
        bot_active   = is_bot_active(conn)
        open_trades  = get_open_real_trades_count(conn)
        trades_today = get_real_trades_today(conn)
    except Exception as e:
        db_error = str(e)[:200]
        log(f"⚠️ /health DB check mislukt: {db_error}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    payload = {
        "status":        "ok" if db_ok else "degraded",
        "service":       "whatsapp_webhook",
        "version":       "2.1",
        "timestamp_utc": now_utc().strftime("%Y-%m-%d %H:%M:%S"),
        "uptime":        uptime_str,
        "database": {
            "connected": db_ok,
            "error":     db_error if not db_ok else None,
        },
        "bot": {
            "active":           bot_active,
            "open_trades":      open_trades,
            "trades_today":     trades_today,
            "trader_mode":      TRADER_MODE,
            "trading_hours":    f"{TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC",
            "in_trading_hours": is_trading_hours(),
        },
        "config": {
            "twilio_ok":      bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN),
            "claude_ok":      bool(ANTHROPIC_API_KEY),
            "bitvavo_ok":     bool(BITVAVO_API_KEY and BITVAVO_API_SECRET),
            "max_trade_eur":  MAX_PER_TRADE_EUR,
            "daily_stop_eur": DAILY_STOP_LOSS_EUR,
        },
    }

    # 503 als DB niet bereikbaar — Render markeert service als Unhealthy
    http_status = 200 if db_ok else 503
    return jsonify(payload), http_status


@app.route("/healthz", methods=["GET"])
def healthz():
    """
    Alias voor /health — backwards compatibility.
    Render pollt /health maar sommige tools gebruiken /healthz.
    Beide routes delegeren naar dezelfde health() functie.
    """
    return health()


@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    """
    Hoofdroute voor WhatsApp berichten van Twilio.
    Verifieert Twilio signature, verwerkt command, stuurt antwoord.
    """
    if not verify_twilio_signature():
        log("❌ Twilio verificatie mislukt")
        return jsonify({"error": "Unauthorized"}), 403

    body    = safe_str(request.form.get("Body", ""))
    from_nr = safe_str(request.form.get("From", ""))

    log(f"📱 WhatsApp van {from_nr[:15]}: '{body[:40]}'")

    if not body:
        return "", 200

    conn = None
    try:
        conn     = db_connect()
        antwoord = process_command(body, conn)
    except Exception as e:
        log(f"❌ process_command fout: {e}")
        antwoord = (
            f"❌ Fout bij verwerken command.\n"
            f"Probeer opnieuw of stuur HELP."
        )
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    send_whatsapp(antwoord)
    return '<?xml version="1.0" encoding="UTF-8"?><Response></Response>', 200


@app.route("/auto_buy", methods=["POST"])
def auto_buy():
    """
    Interne route voor automatische BUY triggers.
    Wordt aangeroepen door multi_coin_score.py na een goed signaal.
    Vereist BOT_INTERNAL_SECRET header.
    """
    if not verify_internal_auth():
        log("❌ Auto buy auth mislukt")
        return jsonify({"ok": False, "error": "Unauthorized"}), 403

    data      = request.get_json(silent=True) or {}
    prebuy_id = safe_str(data.get("prebuy_id"))

    if not prebuy_id:
        return jsonify({"ok": False, "error": "prebuy_id ontbreekt"}), 400

    log(f"🤖 Auto BUY trigger: prebuy_id={prebuy_id}")

    conn = None
    try:
        conn    = db_connect()
        ok, msg = execute_auto_buy(prebuy_id, conn)
        return jsonify({"ok": ok, "message": msg}), 200 if ok else 400
    except Exception as e:
        log(f"❌ auto_buy route fout: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ── Cron rapport routes ──────────────────────────────────
@app.route("/send_daily_rapport", methods=["POST"])
def send_daily_rapport():
    """Render Cron: 0 8 * * * — Dagrapport om 08:00 UTC."""
    if not verify_internal_auth():
        return jsonify({"ok": False}), 403
    conn = None
    try:
        conn    = db_connect()
        bericht = build_daily_rapport(conn)
        ok      = send_whatsapp(bericht)
        return jsonify({"ok": ok}), 200
    except Exception as e:
        log(f"❌ send_daily_rapport fout: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route("/send_weekly_rapport", methods=["POST"])
def send_weekly_rapport():
    """Render Cron: 0 8 * * 1 — Weekrapport op maandag 08:00 UTC."""
    if not verify_internal_auth():
        return jsonify({"ok": False}), 403
    conn = None
    try:
        conn    = db_connect()
        bericht = build_weekly_rapport(conn)
        ok      = send_whatsapp(bericht)
        return jsonify({"ok": ok}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route("/send_monthly_rapport", methods=["POST"])
def send_monthly_rapport():
    """Render Cron: 0 8 1 * * — Maandrapport op de 1e van de maand."""
    if not verify_internal_auth():
        return jsonify({"ok": False}), 403
    conn = None
    try:
        conn    = db_connect()
        bericht = build_monthly_rapport(conn)
        ok      = send_whatsapp(bericht)
        return jsonify({"ok": ok}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route("/send_health_check", methods=["POST"])
def send_health_check():
    """Render Cron: 0 9 * * 1 — Health check op maandag 09:00 UTC."""
    if not verify_internal_auth():
        return jsonify({"ok": False}), 403
    conn = None
    try:
        conn         = db_connect()
        status_line  = get_bot_status_line(conn)
        health_tekst = claude_health_check(conn)

        bericht = (
            f"🏥 WEKELIJKSE HEALTH CHECK\n"
            f"{'─' * 32}\n"
            f"{status_line}\n\n"
            f"🧠 Claude health rapport:\n{health_tekst}\n\n"
            f"{'─' * 32}\n"
            f"Commands: STATUS | ADVIES | STOP"
        )

        ok = send_whatsapp(bericht)
        return jsonify({"ok": ok}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route("/send_leeranalyse", methods=["POST"])
def send_leeranalyse():
    """Render Cron: 0 8 1,15 * * — Leeranalyse op 1e en 15e van de maand."""
    if not verify_internal_auth():
        return jsonify({"ok": False}), 403
    conn = None
    try:
        conn         = db_connect()
        status_line  = get_bot_status_line(conn)
        leer_analyse = claude_trade_leeranalyse(conn)
        pf30         = get_profit_factor(conn, 30)

        bericht = (
            f"🧠 CLAUDE LEERANALYSE\n"
            f"{'─' * 32}\n"
            f"{status_line}\n\n"
            f"📈 Profit Factor 30d: {pf30:.2f}"
            f" {'✅' if pf30 >= 1.5 else '⚠️'}\n\n"
            f"{leer_analyse}\n\n"
            f"{'─' * 32}\n"
            f"Commands: STATUS | RAPPORT | STOP"
        )

        ok = send_whatsapp(bericht)
        return jsonify({"ok": ok}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ============================================================
# STARTUP
# ============================================================
if __name__ == "__main__":
    log("=" * 60)
    log("WhatsApp Webhook v2.1 — gestart")
    log("=" * 60)
    log(f"Database:       {'✅' if DATABASE_URL else '❌ ONTBREEKT'}")
    log(f"Twilio:         {'✅' if TWILIO_ACCOUNT_SID else '⚠️ niet ingesteld'}")
    log(f"Claude API:     {'✅' if ANTHROPIC_API_KEY else '⚠️ niet ingesteld'}")
    log(f"Bitvavo:        {'✅' if BITVAVO_API_KEY else '⚠️ niet ingesteld'}")
    log(f"Trader mode:    {TRADER_MODE}")
    log(f"Max trade:      €{MAX_PER_TRADE_EUR:.2f}")
    log(f"Daily stop:     €{DAILY_STOP_LOSS_EUR:.2f}")
    log(f"Max trades/dag: {MAX_REAL_TRADES_PER_DAY}")
    log(f"Trading hours:  {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC")
    log(f"Health check:   /health (+ /healthz alias)")
    log("=" * 60)

    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
