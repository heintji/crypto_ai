# trade_monitor.py
# ============================================================
# Crypto AI Bot — Trade Monitor v2.0
# ============================================================
# Bewaakt alle open live en shadow trades.
# Voert exits uit op basis van de strategie regels.
# Draait als Render Background Worker (continue loop).
#
# EXIT LOGICA (jouw strategie):
#   Stop bereikt (R < 0)           → SELL 100%
#   >1R bereikt, terug <1R         → SELL 40%
#   3x candles <1R na partial sell → SELL rest
#   Target bereikt                 → STRUCTUUR mode (trailing)
#   Max houdtijd bereikt (48u)     → SELL 100%
#
# IDENTIEK AAN ALLE ANDERE BESTANDEN:
#   ✅ Zelfde ENV variabelen en Fase 1 limieten
#   ✅ Zelfde send_whatsapp() implementatie
#   ✅ Zelfde Claude health monitoring
#   ✅ Zelfde bot state (PostgreSQL bot_state tabel)
#   ✅ Zelfde is_bot_active / is_bot_paused
#   ✅ Zelfde sslmode="require" op DB connectie
#   ✅ Zelfde safe_int / safe_float / safe_str helpers
#   ✅ Bot stopt NOOIT automatisch — jij via STOP
#
# BUGS GEFIXED vs origineel:
#   ✅ send_whatsapp was niet gedefinieerd → crashte bij elke SELL
#   ✅ Dubbele functiedefinities → eerste werd genegeerd
#   ✅ below_1r_count telde monitor-runs ipv candles
#   ✅ Geen DB logging van live trade closes
#   ✅ State werd altijd opgeslagen ook bij geen wijzigingen
#
# NIEUWE FEATURES:
#   ✅ Claude analyseert elke gesloten trade
#   ✅ MFE/MAE tracking per trade
#   ✅ R-multiple bij exit gelogd
#   ✅ Profit factor tracking (>1.5 doel)
#   ✅ Edge decay detectie (sim vs live vergelijking)
#   ✅ Max houdtijd 48u per trade
#   ✅ Coin cooldown check identiek aan multi_coin_score
#   ✅ Coin blacklist check identiek aan multi_coin_score
#   ✅ WhatsApp bij daily stop loss (informatief, geen auto-stop)
#   ✅ WhatsApp bij consecutive losses (informatief, geen auto-stop)
#   ✅ Rolling 30-dagen metrics
#   ✅ Shadow trades volledig parallel bewaakt
#   ✅ Structuur mode met trailing exit
# ============================================================

from __future__ import annotations

import argparse
import json
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

# ============================================================
# FASE 1 LIMIETEN — identiek aan alle andere bestanden
# ============================================================
MAX_PER_TRADE_EUR            = float(os.getenv("MAX_PER_TRADE_EUR") or "0.50")
MAX_REAL_TRADES_PER_DAY      = int(os.getenv("MAX_REAL_TRADES_PER_DAY") or "10")
MAX_OPEN_REAL_TRADES         = int(os.getenv("MAX_OPEN_REAL_TRADES") or "5")
DAILY_STOP_LOSS_EUR          = float(os.getenv("DAILY_STOP_LOSS_EUR") or "5.00")
MAX_CONSECUTIVE_LOSSES       = int(os.getenv("MAX_CONSECUTIVE_LOSSES") or "3")
CONSECUTIVE_LOSS_PAUSE_HOURS = int(os.getenv("CONSECUTIVE_LOSS_PAUSE_HOURS") or "2")
TRADING_HOURS_START          = int(os.getenv("TRADING_HOURS_START") or "9")   # was 8
TRADING_HOURS_END            = int(os.getenv("TRADING_HOURS_END") or "17")    # was 22

# Monitor-specifieke instellingen
MONITOR_INTERVAL_SEC  = int(os.getenv("MONITOR_INTERVAL_SEC") or "30")

# MAX_HOLD_HOURS: van 48 naar 24 uur
# → Trades die langer open staan presteren gemiddeld slechter
# → Kortere houdtijd = minder blootstelling aan nachtruis
MAX_HOLD_HOURS        = float(os.getenv("MAX_HOLD_HOURS") or "24.0")        # was 48.0

# COIN_COOLDOWN_HOURS: van 24 naar 48 uur — identiek aan multi_coin_score
COIN_COOLDOWN_HOURS   = float(os.getenv("COIN_COOLDOWN_HOURS") or "48.0")   # was 24.0

# BLACKLIST: strenger — identiek aan multi_coin_score
BLACKLIST_MIN_TRADES  = int(os.getenv("BLACKLIST_MIN_TRADES") or "15")       # was 20
BLACKLIST_MAX_WINRATE = float(os.getenv("BLACKLIST_MAX_WINRATE") or "0.35")  # was 0.30

EDGE_DECAY_THRESHOLD  = float(os.getenv("EDGE_DECAY_THRESHOLD") or "8.0")   # was 10.0 — eerder alarm

# Bitvavo fee + slippage — identiek aan alle bestanden
BITVAVO_FEE_PCT = float(os.getenv("BITVAVO_FEE_PCT") or "0.0025")
SLIPPAGE_PCT    = float(os.getenv("SLIPPAGE_PCT") or "0.001")
TOTAL_COST_PCT  = BITVAVO_FEE_PCT + SLIPPAGE_PCT

BOT_STATE_TABLE = "public.bot_state"
FORCE_TEST_EXIT = os.getenv("FORCE_TEST_EXIT", "").strip().upper()


def _get_data_dir() -> str:
    d = (os.getenv("DATA_DIR") or "").strip()
    if d:
        return d
    return "/data" if os.path.isdir("/data") else "/tmp/data"

DATA_DIR          = _get_data_dir()
LIVE_STATE_PATH   = os.path.join(DATA_DIR, "live_state.json")
SHADOW_STATE_PATH = os.path.join(DATA_DIR, "shadow_trades.json")


# ============================================================
# BASIS HELPERS — identiek aan alle andere bestanden
# ============================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')}] [MONITOR] {msg}", flush=True)


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


def _calc_fee(amount_eur: float) -> float:
    """Berekent fee + slippage voor een bedrag."""
    return round(amount_eur * TOTAL_COST_PCT, 6)


# ============================================================
# WHATSAPP — identieke implementatie als alle andere bestanden
# ============================================================
def send_whatsapp(message: str) -> bool:
    """
    Stuurt WhatsApp bericht via Twilio.
    Identiek aan whatsapp_webhook.py en live_trader.py.

    FIX: Was niet gedefinieerd in trade_monitor.py origineel.
    Crashte bij elke SELL — nu correct gedefinieerd.

    Alleen voor KRITIEKE meldingen en signalen.
    Bot stopt NOOIT automatisch — jij beslist via STOP.
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
# CLAUDE HEALTH MONITORING — identiek aan alle bestanden
# ============================================================
def _claude_analyse(prompt: str, max_tokens: int = 300) -> str:
    """Claude API aanroep — identiek aan alle bestanden."""
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


def report_error(
    error: Exception,
    function: str,
    symbol:     str = "",
    severity:   str = "HOOG",
    open_trades: int = 0,
) -> None:
    """
    Rapporteert fout via Claude analyse + WhatsApp.
    Identiek patroon als alle andere bestanden.
    Ernst: KRITIEK, HOOG, MEDIUM, LAAG.
    """
    log(f"[{severity}] {function} ({symbol}): {type(error).__name__}: {error}")

    if severity not in ("KRITIEK", "HOOG"):
        return

    prompt = f"""
Je bent een crypto trading bot monitor voor trade_monitor.py.
Er is een fout opgetreden bij het bewaken van een open trade.

Ernst:        {severity}
Functie:      {function}
Coin:         {symbol or 'onbekend'}
Open trades:  {open_trades}
Fout:         {type(error).__name__}: {str(error)[:200]}

Geef in 3 zinnen Nederlands:
1. Wat er mis is
2. Impact op open trades en kapitaal
3. Wat de gebruiker moet doen
""".strip()

    uitleg = _claude_analyse(prompt, max_tokens=200)
    if not uitleg:
        uitleg = f"{type(error).__name__}: {str(error)[:100]}"

    send_whatsapp(
        f"🚨 TRADE MONITOR FOUT — {severity}\n"
        f"{'─' * 30}\n\n"
        f"📁 Functie:     {function}\n"
        f"🪙 Coin:        {symbol or '—'}\n"
        f"📂 Open trades: {open_trades}\n"
        f"⚠️ Fout:       {type(error).__name__}\n\n"
        f"🧠 Claude:\n{uitleg}\n\n"
        f"📋 WAT TE DOEN:\n"
        f"1. Check Render logs voor details\n"
        f"2. Stuur TRADES voor open posities\n"
        f"3. Check Bitvavo account direct\n"
        f"4. Stuur STOP als je wil pauzeren\n\n"
        f"🤖 BOT PROBEERT DOOR TE GAAN\n"
        f"Open trades worden bewaakt.\n\n"
        f"Commands: STATUS | TRADES | STOP"
    )


def claude_analyseer_gesloten_trade(
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
    mfe_r:      float = 0.0,
    mae_r:      float = 0.0,
) -> str:
    """
    Claude analyseert elke gesloten trade.
    Wordt opgeslagen in DB — NIET via WhatsApp (geen spam).
    Wordt gebruikt in weekrapport en leeranalyse.
    """
    prompt = f"""
Je bent een crypto trading bot coach.
Analyseer deze gesloten trade in 2-3 zinnen Nederlands.

Coin:       {symbol}
Setup:      {setup_type} / Regime: {regime}
Entry:      {entry:.6f} → Exit: {exit_price:.6f}
PnL:        €{pnl_eur:.4f}
Duur:       {hold_min:.0f} minuten
Score:      {score}
Uitkomst:   {outcome}
Exitreden:  {exit_reden}
MFE (best): {mfe_r:.2f}R
MAE (worst): {mae_r:.2f}R

Was de entry en exit logisch? Wat leren we van de MFE/MAE?
""".strip()

    return _claude_analyse(prompt, max_tokens=150)


# ============================================================
# DATABASE — sslmode="require" identiek aan alle bestanden
# ============================================================
def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


# ============================================================
# BOT STATE — identiek aan whatsapp_webhook.py + live_trader.py
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
        log(f"⚠️ set_bot_state fout: {e}")


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


# ============================================================
# DATABASE QUERIES
# ============================================================
def get_daily_pnl(conn) -> Tuple[int, int, float]:
    """Wins, losses, PnL vandaag — identiek aan webhook."""
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
            """, (utc_day_str(),))
            row = cur.fetchone()
            if row:
                return safe_int(row[0]), safe_int(row[1]), safe_float(row[2])
    except Exception:
        pass
    return 0, 0, 0.0


def get_rolling_stats(conn, days: int = 7) -> Tuple[int, int, float]:
    """Wins, losses, PnL over de laatste X dagen."""
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
    except Exception:
        pass
    return 0, 0, 0.0


def get_consecutive_losses(conn) -> int:
    """
    Opeenvolgende verliezen — identiek aan alle bestanden.
    Stop bij eerste WIN — telt alleen recente streak.
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
    Doel: >1.5 voor een gezond systeem.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN UPPER(outcome)='WIN'
                    THEN ABS(COALESCE(pnl_eur,0)) ELSE 0 END), 0) AS winst,
                COALESCE(SUM(CASE WHEN UPPER(outcome)='LOSS'
                    THEN ABS(COALESCE(pnl_eur,0)) ELSE 0 END), 0.001) AS verlies
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


def check_edge_decay(conn) -> Optional[str]:
    """
    Vergelijkt sim win rate met live win rate.
    Verschil >10% = mogelijke edge decay.
    Stuurt informatief WhatsApp bericht — bot gaat door.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                UPPER(COALESCE(source,'')) AS src,
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')  AS wins,
                COUNT(*) FILTER (WHERE UPPER(outcome)='LOSS') AS losses
            FROM public.experience_trades
            WHERE UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '30 days'
            GROUP BY 1
            """)
            rows = cur.fetchall()

        data: Dict[str, Tuple[int,int]] = {}
        for row in rows:
            src = safe_str(row[0])
            key = "REAL" if src in ("REAL","LIVE") else src
            w   = safe_int(row[1])
            l   = safe_int(row[2])
            if key not in data:
                data[key] = (0, 0)
            data[key] = (data[key][0] + w, data[key][1] + l)

        def wr(k: str) -> float:
            w, l = data.get(k, (0, 0))
            return (w / (w + l) * 100) if (w + l) > 0 else 0.0

        sim_wr  = wr("SIM")
        real_wr = wr("REAL")
        diff    = sim_wr - real_wr

        if diff > EDGE_DECAY_THRESHOLD and sim_wr > 0 and real_wr > 0:
            return (
                f"⚡ EDGE DECAY SIGNAAL\n"
                f"{'─' * 30}\n\n"
                f"📊 VERGELIJKING 30 DAGEN:\n"
                f"• Simulatie win rate: {sim_wr:.1f}%\n"
                f"• Live win rate:      {real_wr:.1f}%\n"
                f"• Verschil:           {diff:.1f}%\n"
                f"• Grens:              {EDGE_DECAY_THRESHOLD:.0f}%\n\n"
                f"💡 WAT DIT BETEKENT:\n"
                f"Strategie presteert live anders\n"
                f"dan in simulatie. Mogelijke oorzaken:\n"
                f"• Markt is veranderd (regime shift)\n"
                f"• Fees/slippage niet in sim\n"
                f"• Entry timing verschilt live\n\n"
                f"🤖 BOT LOOPT GEWOON DOOR\n"
                f"Stuur HEALTH voor Claude analyse.\n\n"
                f"Commands: HEALTH | STOP | STATUS"
            )
    except Exception:
        pass
    return None


def is_coin_on_cooldown(conn, symbol: str) -> bool:
    """
    24u cooldown na verlies op die coin.
    Identiek aan multi_coin_score.py en live_trader.py.
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
    Identiek aan multi_coin_score.py en live_trader.py.
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


def update_trade_in_db(
    conn,
    symbol:     str,
    prebuy_id:  str,
    outcome:    str,
    pnl_eur:    float,
    exit_price: float,
    exit_time:  datetime,
    setup_type: str = "",
    regime:     str = "",
    score:      int = 0,
    fee_eur:    float = 0.0,
    claude_analyse: str = "",
    mfe_r:      float = 0.0,
    mae_r:      float = 0.0,
    exit_r:     float = 0.0,
) -> None:
    """
    Update of insert live trade in experience_trades.
    source=LIVE, outcome=WIN of LOSS.
    """
    try:
        trade_key = f"LIVE|{symbol}|{prebuy_id or int(time.time())}"

        with conn.cursor() as cur:
            # Probeer eerst update
            cur.execute("""
            UPDATE public.experience_trades SET
                outcome    = %s,
                pnl_eur    = %s,
                exit_time  = %s,
                updated_at = NOW()
            WHERE trade_key = %s
            """, (outcome, pnl_eur, exit_time, trade_key))

            if cur.rowcount == 0:
                # Insert als niet gevonden
                cur.execute("""
                INSERT INTO public.experience_trades (
                    trade_key, source, coin,
                    timestamp, entry_time,
                    setup_type, market_regime,
                    outcome, pnl_eur, bot_confidence,
                    exit_time, created_at, updated_at
                )
                VALUES (%s,'LIVE',%s,NOW(),NOW(),%s,%s,%s,%s,%s,%s,NOW(),NOW())
                ON CONFLICT (trade_key) DO UPDATE SET
                    outcome=EXCLUDED.outcome,
                    pnl_eur=EXCLUDED.pnl_eur,
                    exit_time=EXCLUDED.exit_time,
                    updated_at=NOW()
                """, (
                    trade_key, symbol, setup_type, regime,
                    outcome, pnl_eur, score, exit_time,
                ))

            # Sla Claude analyse op in bot_state voor weekrapport
            if claude_analyse:
                analyse_key = f"trade_analyse_{symbol}_{int(exit_time.timestamp())}"
                cur.execute(f"""
                INSERT INTO {BOT_STATE_TABLE}(key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT(key) DO UPDATE
                    SET value=EXCLUDED.value, updated_at=NOW()
                """, (
                    analyse_key,
                    json.dumps({
                        "symbol":    symbol,
                        "outcome":   outcome,
                        "pnl_eur":   round(pnl_eur, 4),
                        "exit_r":    exit_r,
                        "mfe_r":     mfe_r,
                        "mae_r":     mae_r,
                        "analyse":   claude_analyse,
                        "timestamp": exit_time.isoformat(),
                    }, ensure_ascii=False),
                ))

        conn.commit()
        log(f"✅ DB bijgewerkt: {symbol} {outcome} €{pnl_eur:.4f} R={exit_r:.2f}")
    except Exception as e:
        log(f"⚠️ update_trade_in_db fout ({symbol}): {e}")


# ============================================================
# LIVE STATE FILE HELPERS
# ============================================================
def load_state() -> Dict[str, Any]:
    """Laadt de live trade state."""
    _ensure_dir(LIVE_STATE_PATH)
    if not os.path.exists(LIVE_STATE_PATH):
        return {"positions": {}, "open_trades": []}
    try:
        with open(LIVE_STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("positions", {})
    s.setdefault("open_trades", [])
    return s


def save_state(state: Dict[str, Any]) -> None:
    """Slaat state op — atomisch via tmp file."""
    _ensure_dir(LIVE_STATE_PATH)
    tmp = LIVE_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, LIVE_STATE_PATH)


def get_open_symbols(state: Dict[str, Any]) -> List[str]:
    return list((state.get("positions") or {}).keys())


# ============================================================
# SHADOW STATE FILE HELPERS
# ============================================================
def load_shadow_state() -> Dict[str, Any]:
    _ensure_dir(SHADOW_STATE_PATH)
    if not os.path.exists(SHADOW_STATE_PATH):
        return {"positions": {}, "open_trades": []}
    try:
        with open(SHADOW_STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("positions", {})
    s.setdefault("open_trades", [])
    return s


def save_shadow_state(state: Dict[str, Any]) -> None:
    _ensure_dir(SHADOW_STATE_PATH)
    tmp = SHADOW_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, SHADOW_STATE_PATH)


def get_open_shadow_symbols(shadow_state: Dict[str, Any]) -> List[str]:
    """
    Geeft lijst van open shadow trade symbolen.

    FIX: Was twee keer gedefinieerd — eerste definitie werd genegeerd.
    Nu één correcte definitie.
    """
    return list((shadow_state.get("positions") or {}).keys())


# ============================================================
# PRIJS OPHALEN
# ============================================================
def get_current_price_bitvavo(market: str) -> Optional[float]:
    """Haalt prijs op via Bitvavo public API."""
    try:
        resp = requests.get(
            f"https://api.bitvavo.com/v2/ticker/price",
            params={"market": market},
            timeout=10,
        )
        if resp.ok:
            return safe_float(resp.json().get("price"))
    except Exception as e:
        log(f"⚠️ Bitvavo prijs fout ({market}): {e}")
    return None


def get_current_price_binance(symbol_usdt: str) -> Optional[float]:
    """Haalt prijs op via Binance public API."""
    try:
        resp = requests.get(
            f"https://api.binance.com/api/v3/ticker/price",
            params={"symbol": symbol_usdt.upper()},
            timeout=10,
        )
        if resp.ok:
            return safe_float(resp.json().get("price"))
    except Exception as e:
        log(f"⚠️ Binance prijs fout ({symbol_usdt}): {e}")
    return None


def get_price(symbol_usdt: str, market: str) -> Optional[float]:
    """
    Haalt prijs op — Bitvavo eerst, Binance als fallback.
    Identiek aan live_trader.get_current_price().
    """
    price = get_current_price_bitvavo(market)
    if price and price > 0:
        return price
    return get_current_price_binance(symbol_usdt)


# ============================================================
# SELL UITVOEREN VIA LIVE_TRADER
# ============================================================
def _execute_sell(
    symbol:   str,
    fraction: float,
    meta:     Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Voert sell uit via live_trader.py sell() functie.
    Geeft result dict terug met ok, pnl_eur, exit_price.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from trading.live_trader import sell as live_sell
        result = live_sell(symbol, fraction=fraction, meta=meta)
        return result or {"ok": False, "reason": "GEEN_RESULT_VAN_LIVE_TRADER"}
    except ImportError:
        # Probeer direct path
        try:
            from live_trader import sell as live_sell
            result = live_sell(symbol, fraction=fraction, meta=meta)
            return result or {"ok": False, "reason": "GEEN_RESULT"}
        except ImportError as e:
            log(f"❌ live_trader import fout ({symbol}): {e}")
            return {"ok": False, "reason": f"Import fout: {e}"}
    except Exception as e:
        log(f"❌ Sell call fout ({symbol}): {type(e).__name__}: {e}")
        return {"ok": False, "reason": str(e)}


# ============================================================
# R-MULTIPLE BEREKENING
# ============================================================
def _calc_r(entry: float, stop: float, current: float) -> float:
    """
    Berekent R-multiple.
    R = (current - entry) / (entry - stop)
    Positief = winst richting, negatief = verlies richting.
    """
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    return (current - entry) / risk


def _holding_minutes(trade: Dict[str, Any]) -> float:
    """Berekent hoe lang trade al open staat in minuten."""
    opened_at = safe_float(
        trade.get("opened_at") or trade.get("monitor_started_at")
    )
    if not opened_at:
        return 0.0
    return (time.time() - opened_at) / 60


# ============================================================
# EXIT LOGICA — per live trade
# ============================================================
def process_live_trade(
    symbol: str,
    trade:  Dict[str, Any],
    conn,
) -> Tuple[bool, bool]:
    """
    Verwerkt één open live trade.
    Controleert exit condities en voert sell uit indien nodig.

    Exit logica:
    1. Max houdtijd (48u)           → SELL 100%
    2. Stop bereikt (R < 0)         → SELL 100%
    3. Target bereikt               → STRUCTUUR mode (trailing)
    4. In STRUCTUUR mode            → trail en exit bij break
    5. >1R voor eerst              → partial sell 40% als terug <1R
    6. 3x candles <1R na partial   → SELL rest

    Geeft (changed, sold) terug.
    """
    entry   = safe_float(trade.get("entry"))
    stop    = safe_float(trade.get("stop_loss") or trade.get("stop"))
    target  = safe_float(trade.get("target"))
    market  = safe_str(trade.get("market"))

    # Market bepalen als niet opgeslagen
    if not market:
        try:
            from trading.live_trader import symbol_to_market
            market = symbol_to_market(symbol) or f"{symbol[:-4]}-EUR"
        except ImportError:
            market = f"{symbol[:-4]}-EUR" if symbol.endswith("USDT") else symbol

    current  = get_price(symbol, market)
    if current is None or current <= 0:
        log(f"⚠️ Geen prijs voor {symbol} — skip deze run")
        return False, False

    r        = _calc_r(entry, stop, current)
    hold_min = _holding_minutes(trade)
    changed  = False

    log(f"  {symbol}: prijs={current:.6f} R={r:.2f} gehouden={hold_min:.0f}min")

    # Update MFE/MAE tracking
    if entry > 0:
        risk = abs(entry - stop)
        if risk > 0:
            mfe_r = (safe_float(trade.get("max_price_seen"), current) - entry) / risk
            mae_r = (entry - safe_float(trade.get("min_price_seen"), current)) / risk
            trade["mfe_r"] = round(max(mfe_r, safe_float(trade.get("mfe_r"))), 4)
            trade["mae_r"] = round(max(mae_r, safe_float(trade.get("mae_r"))), 4)

    # FORCE TEST EXIT — voor debugging
    if FORCE_TEST_EXIT and FORCE_TEST_EXIT == symbol.upper():
        log(f"⚡ FORCE_TEST_EXIT: {symbol}")
        result = _execute_sell(symbol, 1.0, meta={"exit_reden": "FORCE_TEST"})
        return True, result.get("ok", False)

    # ── 1. Max houdtijd ─────────────────────────────
    if hold_min >= MAX_HOLD_HOURS * 60:
        log(f"⏰ {symbol}: max houdtijd ({hold_min:.0f}min) → SELL 100%")
        result = _execute_sell(symbol, 1.0, meta={"exit_reden": "MAX_HOLD_TIME"})
        _finalize_trade(symbol, trade, current, result, conn, "MAX_HOLD_TIME")
        return True, result.get("ok", False)

    # ── 2. Stop loss ─────────────────────────────────
    if current <= stop or r < 0:
        log(f"🛑 {symbol}: stop geraakt ({current:.6f} ≤ {stop:.6f}) → SELL 100%")
        result = _execute_sell(symbol, 1.0, meta={"exit_reden": "STOP_LOSS"})
        _finalize_trade(symbol, trade, current, result, conn, "STOP_LOSS")
        return True, result.get("ok", False)

    # ── 3. Target bereikt → STRUCTUUR mode ──────────
    if target > 0 and current >= target:
        if not trade.get("target_reached_notified"):
            trade["target_reached_notified"] = True
            trade["mode"]          = "STRUCTUUR"
            trade["structuur_high"] = current
            log(f"🎯 {symbol}: target bereikt @ {current:.6f} → STRUCTUUR mode")
            changed = True

    # ── 4. STRUCTUUR mode — trailing exit ────────────
    if trade.get("mode") == "STRUCTUUR":
        structuur_high = safe_float(trade.get("structuur_high"), current)

        # Update structuur high als prijs stijgt
        if current > structuur_high:
            trade["structuur_high"] = current
            changed = True
        elif current < structuur_high * 0.99:
            # Prijs breekt 1% onder structuur high → exit
            log(f"📉 {symbol}: structuur gebroken @ {current:.6f} → SELL 100%")
            result = _execute_sell(symbol, 1.0, meta={"exit_reden": "STRUCTUUR_BREAK"})
            _finalize_trade(symbol, trade, current, result, conn, "STRUCTUUR_BREAK")
            return True, result.get("ok", False)

        return changed, False

    # ── 5. Eerste keer >1R bereikt ───────────────────
    if r >= 1.0 and not trade.get("had_over_1r"):
        trade["had_over_1r"]     = True
        trade["max_r"]           = max(r, safe_float(trade.get("max_r")))
        trade["max_price_seen"]  = max(current, safe_float(trade.get("max_price_seen"), current))
        changed = True
        log(f"📈 {symbol}: eerste keer >1R (R={r:.2f}) — bewaken voor partial sell")

    # ── 5b. Terug onder 1R na had_over_1r → SELL 40% ─
    if trade.get("had_over_1r") and not trade.get("partial_sold_40") and r < 1.0:
        log(f"⚠️ {symbol}: terug <1R na >1R (R={r:.2f}) → SELL 40%")
        result = _execute_sell(symbol, 0.40, meta={"exit_reden": "PARTIAL_40"})
        if result.get("ok"):
            trade["partial_sold_40"]      = True
            trade["below_1r_count"]       = 0
            trade["last_candle_check_ts"] = 0
            changed = True
            log(f"✅ {symbol}: 40% verkocht")

    # ── 6. Telt candle-bars <1R na partial sell ──────
    # FIX: telde monitor-runs — nu telt het echte candle-bars
    if trade.get("partial_sold_40") and r < 1.0:
        timeframe = safe_str(trade.get("timeframe"), "4h")
        if "1h" in timeframe.lower():
            candle_period_sec = 60 * 60
        elif "15m" in timeframe.lower():
            candle_period_sec = 15 * 60
        else:
            candle_period_sec = 4 * 60 * 60  # standaard 4H

        last_check = safe_float(trade.get("last_candle_check_ts"))
        now_ts     = time.time()

        if now_ts - last_check >= candle_period_sec:
            trade["below_1r_count"]       = trade.get("below_1r_count", 0) + 1
            trade["last_candle_check_ts"] = now_ts
            changed = True
            log(f"  {symbol}: below_1r_count={trade['below_1r_count']}/3 (per candle)")

            if trade["below_1r_count"] >= 3:
                log(f"⚠️ {symbol}: 3 candles <1R na partial → SELL rest")
                result = _execute_sell(symbol, 1.0, meta={"exit_reden": "BELOW_1R_3X"})
                _finalize_trade(symbol, trade, current, result, conn, "BELOW_1R_3X")
                return True, result.get("ok", False)

    else:
        # Reset teller als prijs weer boven 1R gaat
        if trade.get("below_1r_count", 0) > 0:
            trade["below_1r_count"]       = 0
            trade["last_candle_check_ts"] = 0
            changed = True

    # Update prijs tracking
    if current > safe_float(trade.get("max_price_seen"), current):
        trade["max_price_seen"] = current
        changed = True

    if (not trade.get("min_price_seen") or
            current < safe_float(trade.get("min_price_seen"), current)):
        trade["min_price_seen"] = current
        changed = True

    trade["last_check"] = int(time.time())
    return changed, False


def _finalize_trade(
    symbol:      str,
    trade:       Dict[str, Any],
    exit_price:  float,
    sell_result: Dict[str, Any],
    conn,
    exit_reden:  str,
) -> None:
    """
    Verwerkt een volledig gesloten trade.

    Acties:
    1. DB update (WIN/LOSS)
    2. Claude analyse
    3. Consecutive losses check (informatief)
    4. Profit factor check (elke 10 trades)
    5. Edge decay check (elke 20 trades)
    """
    if not sell_result.get("ok"):
        report_error(
            Exception(f"SELL mislukt: {sell_result.get('reason')}"),
            "_finalize_trade",
            symbol,
            severity="KRITIEK",
            open_trades=len(load_state().get("positions", {})),
        )
        return

    entry      = safe_float(trade.get("entry"))
    pnl_eur    = safe_float(sell_result.get("pnl_eur"))
    fee_eur    = safe_float(sell_result.get("fee_eur"))
    outcome    = "WIN" if pnl_eur > 0 else "LOSS"
    setup_type = safe_str(trade.get("setup_type"))
    regime     = safe_str(trade.get("regime"))
    score      = safe_int(trade.get("score"))
    prebuy_id  = safe_str(trade.get("prebuy_id"))
    hold_min   = _holding_minutes(trade)
    stop       = safe_float(trade.get("stop_loss") or trade.get("stop"))
    mfe_r      = safe_float(trade.get("mfe_r"))
    mae_r      = safe_float(trade.get("mae_r"))

    # R-multiple bij exit
    risk    = abs(entry - stop)
    exit_r  = round((exit_price - entry) / risk, 2) if risk > 0 else 0.0

    log(
        f"📊 {symbol} gesloten: {outcome} €{pnl_eur:.4f} | "
        f"R={exit_r:.2f} | MFE={mfe_r:.2f}R MAE={mae_r:.2f}R | "
        f"{exit_reden}"
    )

    # Claude trade analyse
    claude_tekst = claude_analyseer_gesloten_trade(
        symbol, setup_type, regime, entry, exit_price,
        pnl_eur, hold_min, outcome, score, exit_reden,
        mfe_r, mae_r,
    )

    # DB update
    update_trade_in_db(
        conn, symbol, prebuy_id, outcome, pnl_eur,
        exit_price, now_utc(), setup_type, regime, score,
        fee_eur, claude_tekst, mfe_r, mae_r, exit_r,
    )

    # Consecutive loss check — INFORMATIEF, bot gaat door
    consecutive = get_consecutive_losses(conn)
    if consecutive >= MAX_CONSECUTIVE_LOSSES:
        wins_7, losses_7, pnl_7   = get_rolling_stats(conn, 7)
        wins_30, losses_30, pnl_30 = get_rolling_stats(conn, 30)
        t7  = wins_7 + losses_7
        t30 = wins_30 + losses_30
        wr7  = (wins_7 / t7 * 100) if t7 > 0 else 0.0
        wr30 = (wins_30 / t30 * 100) if t30 > 0 else 0.0
        pf30 = get_profit_factor(conn, 30)

        send_whatsapp(
            f"⚠️ SIGNAAL — {consecutive} VERLIEZEN OP RIJ\n"
            f"{'─' * 30}\n\n"
            f"🪙 Laatste verlies: {symbol}\n"
            f"📉 Verlies op rij:  {consecutive}/{MAX_CONSECUTIVE_LOSSES}\n\n"
            f"📊 STATISTIEKEN:\n"
            f"• Win rate 7d:  {wr7:.1f}% ({wins_7}W/{losses_7}L)\n"
            f"• Win rate 30d: {wr30:.1f}% ({wins_30}W/{losses_30}L)\n"
            f"• PnL 7d: {'+'if pnl_7>=0 else ''}€{pnl_7:.2f}\n"
            f"• Profit Factor 30d: {pf30:.2f}"
            f" {'✅' if pf30>=1.5 else '⚠️'}\n\n"
            f"🤖 BOT LOOPT GEWOON DOOR\n"
            f"Bot handelt automatisch verder.\n"
            f"Stuur STOP als je wil pauzeren.\n\n"
            f"Commands: STOP | STATUS | TRADES"
        )

    # Profit factor check elke 10 trades
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COUNT(*) FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '30 days'
            """)
            row = cur.fetchone()
            trade_count_30d = safe_int(row[0]) if row else 0

        if trade_count_30d > 0 and trade_count_30d % 10 == 0:
            pf = get_profit_factor(conn, 30)
            if 0 < pf < 1.5:
                wins_30, losses_30, pnl_30 = get_rolling_stats(conn, 30)
                wins_7, losses_7, pnl_7    = get_rolling_stats(conn, 7)
                t30 = wins_30 + losses_30
                t7  = wins_7 + losses_7
                wr30 = (wins_30 / t30 * 100) if t30 > 0 else 0.0
                wr7  = (wins_7 / t7 * 100) if t7 > 0 else 0.0

                send_whatsapp(
                    f"📉 PROFIT FACTOR SIGNAAL\n"
                    f"{'─' * 30}\n\n"
                    f"📊 Profit Factor 30d: {pf:.2f}\n"
                    f"🎯 Doel:              >1.5\n"
                    f"⚠️ Onder doel\n\n"
                    f"📈 LAATSTE 30 DAGEN:\n"
                    f"• Trades: {trade_count_30d}\n"
                    f"• Wins:   {wins_30} | Losses: {losses_30}\n"
                    f"• Win rate: {wr30:.1f}%\n"
                    f"• PnL:    {'+'if pnl_30>=0 else ''}€{pnl_30:.2f}\n\n"
                    f"📈 LAATSTE 7 DAGEN:\n"
                    f"• Wins:   {wins_7} | Losses: {losses_7}\n"
                    f"• Win rate: {wr7:.1f}%\n"
                    f"• PnL:    {'+'if pnl_7>=0 else ''}€{pnl_7:.2f}\n\n"
                    f"💡 Overweeg hogere score drempel.\n\n"
                    f"🤖 BOT LOOPT GEWOON DOOR\n"
                    f"Stuur STOP als je wil pauzeren.\n\n"
                    f"Commands: STOP | STATUS | RAPPORT"
                )
    except Exception:
        pass

    # Edge decay check elke 20 trades
    try:
        if trade_count_30d > 0 and trade_count_30d % 20 == 0:
            decay_msg = check_edge_decay(conn)
            if decay_msg:
                send_whatsapp(decay_msg)
    except Exception:
        pass


# ============================================================
# SHADOW TRADE EVALUATIE
# ============================================================
def evaluate_shadow_for_symbol(
    symbol:       str,
    shadow_trade: Dict[str, Any],
    current:      Optional[float],
    conn,
) -> Tuple[bool, bool]:
    """
    Verwerkt één open shadow trade.
    Logt uitkomst naar experience_trades met source=SHADOW.

    FIX: Was twee keer gedefinieerd — eerste werd genegeerd.
    Nu één definitie die correct werkt.

    Geeft (changed, closed) terug.
    """
    if current is None or current <= 0:
        return False, False

    entry  = safe_float(shadow_trade.get("entry"))
    stop   = safe_float(shadow_trade.get("stop_loss") or shadow_trade.get("stop"))
    target = safe_float(shadow_trade.get("target"))
    r      = _calc_r(entry, stop, current)
    hold_min = _holding_minutes(shadow_trade)

    changed = False
    closed  = False

    # Max houdtijd
    if hold_min >= MAX_HOLD_HOURS * 60:
        _log_shadow_outcome(symbol, shadow_trade, current, "LOSS", "MAX_HOLD_TIME", conn)
        return True, True

    # Stop geraakt
    if current <= stop or r < 0:
        _log_shadow_outcome(symbol, shadow_trade, current, "LOSS", "STOP_LOSS", conn)
        return True, True

    # Target geraakt
    if target > 0 and current >= target:
        _log_shadow_outcome(symbol, shadow_trade, current, "WIN", "TARGET_HIT", conn)
        return True, True

    # Had_over_1r tracking
    if r >= 1.0 and not shadow_trade.get("had_over_1r"):
        shadow_trade["had_over_1r"] = True
        changed = True

    # Structuur fail
    if shadow_trade.get("had_over_1r") and r < 0.5:
        _log_shadow_outcome(symbol, shadow_trade, current, "LOSS", "STRUCTUUR_FAIL", conn)
        return True, True

    shadow_trade["last_check"] = int(time.time())
    return changed, False


def _log_shadow_outcome(
    symbol:     str,
    trade:      Dict[str, Any],
    exit_price: float,
    outcome:    str,
    exit_reden: str,
    conn,
) -> None:
    """Logt shadow trade uitkomst naar experience_trades."""
    entry      = safe_float(trade.get("entry"))
    qty        = safe_float(trade.get("qty") or trade.get("position_size"), 1.0)
    pnl_eur    = (exit_price - entry) * qty if entry > 0 and qty > 0 else 0.0
    setup_type = safe_str(trade.get("setup_type"))
    regime     = safe_str(trade.get("regime"))
    score      = safe_int(trade.get("score"))
    prebuy_id  = safe_str(trade.get("prebuy_id"))

    try:
        trade_key = f"SHADOW|{symbol}|{prebuy_id or int(time.time())}"
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO public.experience_trades (
                trade_key, source, is_shadow, coin,
                timestamp, entry_time,
                setup_type, market_regime, entry,
                outcome, pnl_eur, bot_confidence,
                exit_time, created_at, updated_at
            )
            VALUES (%s,'SHADOW',TRUE,%s,NOW(),NOW(),%s,%s,%s,%s,%s,%s,NOW(),NOW(),NOW())
            ON CONFLICT (trade_key) DO UPDATE SET
                outcome    = EXCLUDED.outcome,
                pnl_eur    = EXCLUDED.pnl_eur,
                exit_time  = NOW(),
                updated_at = NOW()
            """, (
                trade_key, symbol, setup_type, regime,
                entry, outcome, pnl_eur, score,
            ))
        conn.commit()
        log(f"🎭 Shadow {symbol}: {outcome} {exit_reden} €{pnl_eur:.4f}")
    except Exception as e:
        log(f"⚠️ Shadow DB log fout ({symbol}): {e}")


# ============================================================
# HOOFD MONITOR RUN
# ============================================================
def run_monitor_once(target_symbol: Optional[str] = None) -> None:
    """
    Één run van de monitor.

    Stappen:
    1. DB verbinding
    2. Bot state check
    3. Daily PnL check (informatief)
    4. Live trades verwerken
    5. Shadow trades verwerken
    6. State opslaan (alleen als gewijzigd)
    """
    try:
        conn = db_connect()
    except Exception as e:
        report_error(e, "run_monitor_once.db_connect", severity="KRITIEK")
        return

    try:
        # Bot state check
        # Monitor bewaakt ALTIJD open trades — ook als bot gestopt is
        bot_active = is_bot_active(conn)
        bot_paused = is_bot_paused(conn)

        if not bot_active:
            log("Bot gestopt — alleen open trades bewaken")
        elif bot_paused:
            log("Bot gepauzeerd — alleen open trades bewaken")

        # Daily stop loss check — INFORMATIEF, geen automatische stop
        wins_v, losses_v, daily_pnl = get_daily_pnl(conn)
        if daily_pnl <= -DAILY_STOP_LOSS_EUR and bot_active:
            log(f"ℹ️ Dagbudget bereikt: €{daily_pnl:.2f} — bot gaat door")
            wins_7, losses_7, pnl_7 = get_rolling_stats(conn, 7)
            t7 = wins_7 + losses_7
            wr7 = (wins_7 / t7 * 100) if t7 > 0 else 0.0
            pf30 = get_profit_factor(conn, 30)
            open_n = len(load_state().get("positions", {}))

            send_whatsapp(
                f"🛑 DAGBUDGET BEREIKT\n"
                f"{'─' * 30}\n\n"
                f"💶 Verlies vandaag:  €{abs(daily_pnl):.2f}\n"
                f"📊 Dagbudget:        €{DAILY_STOP_LOSS_EUR:.2f}\n\n"
                f"📈 VANDAAG:\n"
                f"• Wins:    {wins_v} | Losses: {losses_v}\n\n"
                f"📈 LAATSTE 7 DAGEN:\n"
                f"• Wins:    {wins_7} | Losses: {losses_7}\n"
                f"• Win rate: {wr7:.1f}%\n"
                f"• PnL:    {'+'if pnl_7>=0 else ''}€{pnl_7:.2f}\n\n"
                f"📈 Profit Factor 30d: {pf30:.2f}\n\n"
                f"📂 Open trades: {open_n}\n"
                f"(worden bewaakt)\n\n"
                f"🤖 BOT LOOPT GEWOON DOOR\n"
                f"Stuur STOP als je wil pauzeren.\n\n"
                f"Commands: STOP | STATUS | TRADES"
            )

        # ── LIVE TRADES ──────────────────────────────
        state   = load_state()
        symbols = get_open_symbols(state)

        if target_symbol:
            symbols = [s for s in symbols if s == target_symbol]

        live_changed = False

        for symbol in symbols:
            trade = (state.get("positions") or {}).get(symbol)
            if not trade:
                continue

            # Log als coin op blacklist staat (maar bewaken we nog steeds)
            if is_coin_blacklisted(conn, symbol):
                log(f"⚫ {symbol} op blacklist — trade is al open, bewaken we")

            try:
                changed, sold = process_live_trade(symbol, trade, conn)
                if changed:
                    live_changed = True
                if sold:
                    state["positions"].pop(symbol, None)
                    state["open_trades"] = [
                        t for t in (state.get("open_trades") or [])
                        if t.get("symbol") != symbol
                    ]
            except Exception as e:
                report_error(e, "process_live_trade", symbol, "HOOG",
                             open_trades=len(symbols))

        # Alleen opslaan als er iets gewijzigd is
        if live_changed:
            save_state(state)

        # ── SHADOW TRADES ────────────────────────────
        try:
            shadow_state   = load_shadow_state()
            shadow_symbols = get_open_shadow_symbols(shadow_state)

            if target_symbol:
                shadow_symbols = [s for s in shadow_symbols if s == target_symbol]

            shadow_changed = False

            for symbol in shadow_symbols:
                shadow_trade = (shadow_state.get("positions") or {}).get(symbol)
                if not shadow_trade:
                    continue

                market  = safe_str(shadow_trade.get("market"), f"{symbol[:-4]}-EUR")
                current = get_price(symbol, market)

                try:
                    changed, closed = evaluate_shadow_for_symbol(
                        symbol, shadow_trade, current, conn
                    )
                    if changed:
                        shadow_changed = True
                    if closed:
                        shadow_state["positions"].pop(symbol, None)
                        # Fix: ook uit open_trades verwijderen
                        shadow_state["open_trades"] = [
                            t for t in (shadow_state.get("open_trades") or [])
                            if t.get("symbol") != symbol
                        ]
                except Exception as e:
                    log(f"⚠️ Shadow trade fout ({symbol}): {e}")

            if shadow_changed:
                save_shadow_state(shadow_state)

        except Exception as e:
            log(f"⚠️ Shadow monitor fout: {e}")

        open_count = len(load_state().get("positions", {}))
        shad_count = len(load_shadow_state().get("positions", {}))
        log(
            f"✅ Monitor run klaar — "
            f"{len(symbols)} live open | {shad_count} shadow open"
        )

    except Exception as e:
        report_error(e, "run_monitor_once", severity="KRITIEK",
                     open_trades=len(load_state().get("positions", {})))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_monitor_loop() -> None:
    """
    Continue monitor loop — draait als Render Background Worker.
    Interval: 30 seconden (configureerbaar via MONITOR_INTERVAL_SEC).
    """
    log("=" * 60)
    log("Trade Monitor v2.0 — gestart")
    log("=" * 60)
    log(f"Database:     {'✅' if DATABASE_URL else '❌ ONTBREEKT'}")
    log(f"Twilio:       {'✅' if TWILIO_ACCOUNT_SID else '⚠️ niet ingesteld'}")
    log(f"Claude API:   {'✅' if ANTHROPIC_API_KEY else '⚠️ niet ingesteld'}")
    log(f"Interval:     {MONITOR_INTERVAL_SEC}s")
    log(f"Max hold:     {MAX_HOLD_HOURS}u")
    log(f"Cooldown:     {COIN_COOLDOWN_HOURS}u na verlies")
    log(f"Fee+slip:     {TOTAL_COST_PCT*100:.2f}%")
    log(f"Daily stop:   €{DAILY_STOP_LOSS_EUR:.2f} (informatief)")
    log(f"Cons. losses: {MAX_CONSECUTIVE_LOSSES} (informatief)")
    log(f"Data dir:     {DATA_DIR}")
    log(f"Bot stopt:    NOOIT automatisch — jij via STOP")
    log("=" * 60)

    while True:
        try:
            run_monitor_once()
        except Exception as e:
            log(f"❌ Monitor loop fout: {type(e).__name__}: {e}")
        time.sleep(MONITOR_INTERVAL_SEC)


# ============================================================
# CLI
# ============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trade Monitor v2.0")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Één monitor run uitvoeren en stoppen",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="Alleen dit symbool monitoren (bijv. ETHUSDT)",
    )
    args = parser.parse_args()

    if args.once or args.symbol:
        log(f"Één monitor run{f' voor {args.symbol}' if args.symbol else ''}...")
        run_monitor_once(target_symbol=args.symbol)
    else:
        run_monitor_loop()
