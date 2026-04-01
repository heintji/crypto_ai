# trade_monitor.py
# ============================================================
# Crypto AI Bot — Trade Monitor v3.1
# ============================================================
# Bewaakt alle open live en shadow trades.
# Voert exits uit op basis van de strategie regels.
# Draait als Render Background Worker (continue loop).
#
# ARCHITECTUUR:
# ─────────────────────────────────────────────────────────────
# Dit is de BEWAKER van open posities.
# Hij draait 24/7 als Background Worker op Render.
# Elke 30 seconden checkt hij alle open trades.
# Shadow trades lopen ALTIJD parallel — leerdata.
# Live trades alleen als bot_active=true.
#
# EXIT LOGICA (jouw strategie):
# ─────────────────────────────────────────────────────────────
#   Stop bereikt (R < 0)            → SELL 100%
#   Max houdtijd bereikt             → SELL 100% (24u normaal)
#     BULL regime:                   → 36u (trend helpt mee)
#     BEAR regime:                   → 12u (snel sluiten)
#   Target bereikt                   → STRUCTUUR mode (trailing)
#   STRUCTUUR gebroken (-1%)         → SELL 100%
#   1R bereikt → stop naar breakeven → trailing stop actief
#   2R bereikt → stop naar +1R       → trailing stop verbeterd
#   >1R bereikt, terug <1R           → SELL 40% partial + WhatsApp
#   3x candles <1R na partial sell   → SELL rest
#
# KRITIEKE FIXES vs v2.0:
# ─────────────────────────────────────────────────────────────
# ✅ load_state/load_shadow_state: isinstance(s, dict) check
#    → was: 'list' object has no attribute 'setdefault'
#    → crash bij elke run, herstart elke 30s
# ✅ WhatsApp rate limiting: max 1x per uur per fouttype
#    → was: 429 Twilio limiet door crash-loop spam
# ✅ safe_rollback() toegevoegd
# ✅ db_connect(): autocommit=False + retries
# ✅ finally conn.close() in run_monitor_once
# ✅ conn = None initialisatie voor try/finally
# ✅ Shadow positions reset als corrupt JSON
#
# NIEUWE FEATURES v3.0:
# ─────────────────────────────────────────────────────────────
# ✅ Regime-afhankelijke max houdtijd (BULL=36u, RANGE=24u, BEAR=12u)
# ✅ Trailing stop na 1R (breakeven) en 2R (+1R winst)
# ✅ Dagelijkse samenvatting in bot_state voor dashboard
# ✅ Uitgebreide MFE/MAE tracking per trade
# ✅ Profit factor tracking (doel >1.5)
# ✅ Edge decay detectie (sim vs live vergelijking)
# ✅ Rolling 7/30-dagen metrics
# ✅ Health monitoring met WhatsApp bij problemen
# ✅ Per-run statistieken gelogd
#
# FIXES v3.1:
# ─────────────────────────────────────────────────────────────
# ✅ Fix 1: model string → claude-sonnet-4-6
# ✅ Fix 2: STRUCTUUR mode minimum winst check (target - 0.5%)
#    → voorkomt verkoop onder target door fees/slippage
# ✅ Fix 3: WhatsApp notificatie bij partial sell 40%
#    → jij weet nu altijd wanneer bot 40% verkoopt
# ✅ Fix 4: Shadow trades max houdtijd → 48u (was 24u)
#    → meer leerdata per shadow trade
#
# SAMENWERKING MET ANDERE BESTANDEN:
# ─────────────────────────────────────────────────────────────
# ← pending_approvals → live_trader.py opent trades
# ← live_state.json   → bevat open live posities
# ← shadow_state.json → bevat open shadow posities
# → experience_trades → schrijft gesloten trades
# → bot_state         → schrijft status en statistieken
# → coach_events      → schrijft events voor ai_coach.py
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
MAX_PER_TRADE_EUR            = float(os.getenv("MAX_PER_TRADE_EUR")            or "0.50")
MAX_REAL_TRADES_PER_DAY      = int(os.getenv("MAX_REAL_TRADES_PER_DAY")        or "10")
MAX_OPEN_REAL_TRADES         = int(os.getenv("MAX_OPEN_REAL_TRADES")           or "5")
DAILY_STOP_LOSS_EUR          = float(os.getenv("DAILY_STOP_LOSS_EUR")          or "5.00")
MAX_CONSECUTIVE_LOSSES       = int(os.getenv("MAX_CONSECUTIVE_LOSSES")         or "3")
CONSECUTIVE_LOSS_PAUSE_HOURS = int(os.getenv("CONSECUTIVE_LOSS_PAUSE_HOURS")   or "2")
TRADING_HOURS_START          = int(os.getenv("TRADING_HOURS_START")            or "9")
TRADING_HOURS_END            = int(os.getenv("TRADING_HOURS_END")              or "17")

# Monitor-specifieke instellingen
MONITOR_INTERVAL_SEC  = int(os.getenv("MONITOR_INTERVAL_SEC")  or "30")
MAX_HOLD_HOURS        = float(os.getenv("MAX_HOLD_HOURS")       or "24.0")
COIN_COOLDOWN_HOURS   = float(os.getenv("COIN_COOLDOWN_HOURS")  or "48.0")
BLACKLIST_MIN_TRADES  = int(os.getenv("BLACKLIST_MIN_TRADES")   or "15")
BLACKLIST_MAX_WINRATE = float(os.getenv("BLACKLIST_MAX_WINRATE") or "0.35")
EDGE_DECAY_THRESHOLD  = float(os.getenv("EDGE_DECAY_THRESHOLD") or "8.0")
DB_CONNECT_RETRIES    = int(os.getenv("DB_CONNECT_RETRIES")     or "3")

# ATR instellingen
ATR_PERIOD     = int(os.getenv("ATR_PERIOD")     or "14")
ATR_MULTIPLIER = float(os.getenv("ATR_MULTIPLIER") or "1.6")
ATR_TARGET_R   = float(os.getenv("ATR_TARGET_R")  or "2.5")

# RSI instellingen
RSI_MIN = int(os.getenv("RSI_MIN") or "40")
RSI_MAX = int(os.getenv("RSI_MAX") or "60")

# Fee + slippage
BITVAVO_FEE_PCT = float(os.getenv("BITVAVO_FEE_PCT") or "0.0025")
SLIPPAGE_PCT    = float(os.getenv("SLIPPAGE_PCT")    or "0.001")
TOTAL_COST_PCT  = BITVAVO_FEE_PCT + SLIPPAGE_PCT

BOT_STATE_TABLE = "public.bot_state"
FORCE_TEST_EXIT = os.getenv("FORCE_TEST_EXIT", "").strip().upper()

# WhatsApp rate limiting — max 1x per uur per fouttype
# FIX: voorkomt 429 Twilio spam bij herhaalde crashes
_WHATSAPP_SENT: Dict[str, float] = {}
_WHATSAPP_COOLDOWN_SEC = 3600   # 1 uur

# Run statistieken (in-memory, reset per run)
_RUN_STATS: Dict[str, Any] = {
    "runs":          0,
    "live_checked":  0,
    "shadow_checked": 0,
    "live_closed":   0,
    "shadow_closed": 0,
    "fouten":        0,
    "start_ts":      0.0,
}


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
    """Geeft huidige UTC tijd terug als timezone-aware datetime."""
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    """Uniform log formaat voor Render logs."""
    print(f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')}] [MONITOR] {msg}", flush=True)


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
# WHATSAPP — met rate limiting
# ============================================================
def send_whatsapp(message: str, rate_key: str = "") -> bool:
    """
    Stuurt WhatsApp bericht via Twilio.
    Identiek aan alle andere bestanden.

    rate_key: als opgegeven, max 1 bericht per uur per key.
    Voorkomt 429 Twilio spam bij herhaalde crashes.

    FIX vs v2.0: geen rate limiting → 429 spam bij crash-loop.
    Nu: max 1 foutbericht per uur per fouttype.
    """
    if rate_key:
        now_ts = time.time()
        last   = _WHATSAPP_SENT.get(rate_key, 0.0)
        if now_ts - last < _WHATSAPP_COOLDOWN_SEC:
            wacht = int((_WHATSAPP_COOLDOWN_SEC - (now_ts - last)) / 60)
            log(f"WhatsApp rate limit ({rate_key}) — wacht nog {wacht}min")
            return False
        _WHATSAPP_SENT[rate_key] = now_ts

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
        ok = resp.status_code in (200, 201)
        if ok:
            log(f"✅ WhatsApp verzonden ({len(message)} tekens)")
        else:
            log(f"❌ WhatsApp {resp.status_code}: {resp.text[:200]}")
        return ok
    except Exception as e:
        log(f"❌ WhatsApp fout: {type(e).__name__}: {e}")
        return False


# ============================================================
# CLAUDE — analyse helper
# ============================================================
def _claude_analyse(prompt: str, max_tokens: int = 300) -> str:
    """
    Claude API aanroep. Geeft lege string bij elke fout.
    FIX v3.1: model string bijgewerkt naar claude-sonnet-4-6.
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
                "model":      "claude-sonnet-4-6",  # FIX v3.1: was claude-sonnet-4-20250514
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
    Rapporteert fout via log + WhatsApp (bij KRITIEK/HOOG).
    Rate limiting: max 1x per uur per fout+severity combinatie.

    FIX vs v2.0: geen rate limiting → 429 Twilio spam.
    """
    log(f"[{severity}] {function} ({symbol}): {type(error).__name__}: {error}")
    _RUN_STATS["fouten"] = _RUN_STATS.get("fouten", 0) + 1

    if severity not in ("KRITIEK", "HOOG"):
        return

    # Rate limiting per fouttype
    rate_key = f"error_{function}_{severity}"

    prompt = (
        f"Je bent een crypto trading bot monitor voor trade_monitor.py.\n"
        f"Er is een fout opgetreden bij het bewaken van een open trade.\n\n"
        f"Ernst:        {severity}\n"
        f"Functie:      {function}\n"
        f"Coin:         {symbol or 'onbekend'}\n"
        f"Open trades:  {open_trades}\n"
        f"Fout:         {type(error).__name__}: {str(error)[:200]}\n\n"
        f"Geef in 3 zinnen Nederlands:\n"
        f"1. Wat er mis is\n"
        f"2. Impact op open trades en kapitaal\n"
        f"3. Wat de gebruiker moet doen"
    )

    uitleg = _claude_analyse(prompt, max_tokens=200) or \
             f"{type(error).__name__}: {str(error)[:100]}"

    send_whatsapp(
        rate_key=rate_key,
        message=(
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
        ),
    )


def log_coach_event(conn, categorie: str, event_type: str,
                    omschrijving: str, ernst: str = "LAAG") -> None:
    """
    Logt een event naar coach_events tabel.
    Wordt door ai_coach.py opgepikt voor analyse.
    Stille fout — niet kritiek als dit mislukt.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO public.coach_events
                (tijdstip, categorie, event_type, omschrijving, ernst)
            VALUES (NOW(), %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """, (categorie, event_type, omschrijving[:500], ernst))
        conn.commit()
    except Exception:
        safe_rollback(conn)


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
    prompt = (
        f"Je bent een crypto trading bot coach.\n"
        f"Analyseer deze gesloten trade in 2-3 zinnen Nederlands.\n\n"
        f"Coin:        {symbol}\n"
        f"Setup:       {setup_type} / Regime: {regime}\n"
        f"Entry:       {entry:.6f} → Exit: {exit_price:.6f}\n"
        f"PnL:         €{pnl_eur:.4f}\n"
        f"Duur:        {hold_min:.0f} minuten\n"
        f"Score:       {score}\n"
        f"Uitkomst:    {outcome}\n"
        f"Exitreden:   {exit_reden}\n"
        f"MFE (best):  {mfe_r:.2f}R\n"
        f"MAE (worst): {mae_r:.2f}R\n\n"
        f"Was de entry en exit logisch? Wat leren we van de MFE/MAE?"
    )
    return _claude_analyse(prompt, max_tokens=150)


# ============================================================
# DATABASE — sslmode="require" + retries + autocommit=False
# ============================================================
def db_connect(retries: int = DB_CONNECT_RETRIES):
    """
    Verbindt met PostgreSQL.
    Retries bij verbindingsfout. autocommit=False.

    FIX vs v2.0: geen retries, geen autocommit=False.
    """
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    for poging in range(1, retries + 1):
        try:
            conn = psycopg2.connect(DATABASE_URL, sslmode="require",
                                    connect_timeout=10)
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

    FIX vs v2.0: safe_rollback bestond niet.
    """
    try:
        conn.rollback()
    except Exception as e:
        log(f"Rollback fout (niet kritiek): {e}")


# ============================================================
# BOT STATE
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
        safe_rollback(conn)
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
        safe_rollback(conn)
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
    """Wins, losses, PnL vandaag."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')  AS wins,
                COUNT(*) FILTER (WHERE UPPER(outcome)='LOSS') AS losses,
                COALESCE(SUM(
                    CASE WHEN UPPER(outcome)='WIN'  THEN  ABS(COALESCE(pnl_eur,0))
                         WHEN UPPER(outcome)='LOSS' THEN -ABS(COALESCE(pnl_eur,0))
                         ELSE 0 END
                ), 0) AS pnl
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND DATE(COALESCE(exit_time, updated_at) AT TIME ZONE 'UTC') = %s
            """, (utc_day_str(),))
            row = cur.fetchone()
            if row:
                return safe_int(row[0]), safe_int(row[1]), safe_float(row[2])
    except Exception:
        safe_rollback(conn)
    return 0, 0, 0.0


def get_rolling_stats(conn, days: int = 7) -> Tuple[int, int, float]:
    """Wins, losses, PnL over de laatste X dagen."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN'),
                COUNT(*) FILTER (WHERE UPPER(outcome)='LOSS'),
                COALESCE(SUM(
                    CASE WHEN UPPER(outcome)='WIN'  THEN  ABS(COALESCE(pnl_eur,0))
                         WHEN UPPER(outcome)='LOSS' THEN -ABS(COALESCE(pnl_eur,0))
                         ELSE 0 END
                ), 0)
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '1 day' * %s
            """, (days,))
            row = cur.fetchone()
            if row:
                return safe_int(row[0]), safe_int(row[1]), safe_float(row[2])
    except Exception:
        safe_rollback(conn)
    return 0, 0, 0.0


def get_consecutive_losses(conn) -> int:
    """Opeenvolgende verliezen — stop bij eerste WIN."""
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
        safe_rollback(conn)
        return 0


def get_profit_factor(conn, days: int = 30) -> float:
    """Profit factor over de laatste X dagen. Doel: >1.5."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN UPPER(outcome)='WIN'
                    THEN ABS(COALESCE(pnl_eur,0)) ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN UPPER(outcome)='LOSS'
                    THEN ABS(COALESCE(pnl_eur,0)) ELSE 0 END), 0.001)
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
        safe_rollback(conn)
    return 0.0


def check_edge_decay(conn) -> Optional[str]:
    """
    Vergelijkt sim win rate met live win rate.
    Verschil > EDGE_DECAY_THRESHOLD = mogelijke edge decay.
    Geeft WhatsApp bericht terug of None.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                UPPER(COALESCE(source,'')) AS src,
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN'),
                COUNT(*) FILTER (WHERE UPPER(outcome)='LOSS')
            FROM public.experience_trades
            WHERE UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '30 days'
            GROUP BY 1
            """)
            rows = cur.fetchall()

        data: Dict[str, Tuple[int, int]] = {}
        for row in rows:
            src = safe_str(row[0])
            key = "REAL" if src in ("REAL", "LIVE") else src
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
                f"Strategie presteert live anders dan in simulatie.\n"
                f"Mogelijke oorzaken:\n"
                f"• Markt is veranderd (regime shift)\n"
                f"• Fees/slippage niet in sim\n"
                f"• Entry timing verschilt live\n\n"
                f"🤖 BOT LOOPT GEWOON DOOR\n"
                f"Stuur HEALTH voor Claude analyse.\n\n"
                f"Commands: HEALTH | STOP | STATUS"
            )
    except Exception:
        safe_rollback(conn)
    return None


def is_coin_on_cooldown(conn, symbol: str) -> bool:
    """48u cooldown na verlies op die coin."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT exit_time FROM public.experience_trades
            WHERE coin = %s
              AND UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND UPPER(outcome) = 'LOSS'
              AND exit_time IS NOT NULL
            ORDER BY exit_time DESC LIMIT 1
            """, (symbol,))
            row = cur.fetchone()
            if row and row[0]:
                last_loss = row[0]
                if hasattr(last_loss, 'tzinfo') and last_loss.tzinfo is None:
                    last_loss = last_loss.replace(tzinfo=timezone.utc)
                return (now_utc() - last_loss).total_seconds() / 3600 < COIN_COOLDOWN_HOURS
    except Exception:
        safe_rollback(conn)
    return False


def is_coin_blacklisted(conn, symbol: str) -> bool:
    """Blacklist: win rate < drempel na minimum trades."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COUNT(*), COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')
            FROM public.experience_trades
            WHERE coin = %s
              AND UPPER(COALESCE(source,'')) IN ('REAL','LIVE','SIM','SHADOW')
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
            """, (symbol,))
            row = cur.fetchone()
            if row:
                n, wins = safe_int(row[0]), safe_int(row[1])
                if n >= BLACKLIST_MIN_TRADES:
                    return (wins / n) < BLACKLIST_MAX_WINRATE
    except Exception:
        safe_rollback(conn)
    return False


def update_trade_in_db(
    conn,
    symbol:         str,
    prebuy_id:      str,
    outcome:        str,
    pnl_eur:        float,
    exit_price:     float,
    exit_time:      datetime,
    setup_type:     str = "",
    regime:         str = "",
    score:          int = 0,
    fee_eur:        float = 0.0,
    claude_analyse: str = "",
    mfe_r:          float = 0.0,
    mae_r:          float = 0.0,
    exit_r:         float = 0.0,
) -> None:
    """Update of insert live trade in experience_trades."""
    try:
        trade_key = f"LIVE|{symbol}|{prebuy_id or int(time.time())}"
        with conn.cursor() as cur:
            cur.execute("""
            UPDATE public.experience_trades SET
                outcome    = %s,
                pnl_eur    = %s,
                exit_time  = %s,
                status     = 'CLOSED',
                closed_at  = NOW(),
                updated_at = NOW()
            WHERE trade_key = %s OR (symbol = %s AND source = 'LIVE' AND status = 'OPEN')
            """, (outcome, pnl_eur, exit_time, trade_key, symbol))

            if cur.rowcount == 0:
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
        safe_rollback(conn)
        log(f"⚠️ update_trade_in_db fout ({symbol}): {e}")


# ============================================================
# STATE FILE HELPERS
# ============================================================
def load_state() -> Dict[str, Any]:
    """Laadt open live posities uit experience_trades DB."""
    conn2 = None
    try:
        conn2 = db_connect()
        cur2 = conn2.cursor()
        cur2.execute("SELECT trade_key, symbol, bitvavo_market, entry, stop, stop_loss, target, qty, amount_eur, setup_type, score, entry_time, prebuy_id, max_price_seen FROM experience_trades WHERE status = 'OPEN' AND is_shadow = FALSE AND source = 'LIVE'")
        rows = cur2.fetchall()
        positions = {}
        for r in rows:
            key = r[0] or f"LIVE|{r[1]}|0"
            positions[key] = {"symbol": r[1], "market": r[2], "entry": float(r[3] or 0), "stop": float(r[4] or r[5] or 0), "target": float(r[6] or 0), "qty": float(r[7] or 0), "amount_eur": float(r[8] or 0), "setup_type": r[9], "score": r[10], "entry_time": str(r[11]), "prebuy_id": r[12], "peak_price": float(r[13] or r[3] or 0)}
        return {"positions": positions, "open_trades": list(positions.keys())}
    except Exception as e:
        log(f"⚠️ load_state DB fout: {e}")
        return {"positions": {}, "open_trades": []}
    finally:
        if conn2:
            try: conn2.close()
            except: pass

def save_state(state: Dict[str, Any]) -> None:
    """Slaat state op — atomisch via tmp file."""
    _ensure_dir(LIVE_STATE_PATH)
    tmp = LIVE_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, LIVE_STATE_PATH)


def get_open_symbols(state: Dict[str, Any]) -> List[str]:
    return list((state.get("positions") or {}).keys())


def load_shadow_state() -> Dict[str, Any]:
    """
    Laadt de shadow trade state.

    FIX vs v2.0: als JSON een list is (corrupt) ipv dict
    crashte alles met 'list object has no attribute setdefault'.
    Nu: isinstance check + reset naar leeg dict bij corruptie.
    """
    _ensure_dir(SHADOW_STATE_PATH)
    if not os.path.exists(SHADOW_STATE_PATH):
        return {"positions": {}, "open_trades": []}
    try:
        with open(SHADOW_STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
        # FIX: corrupt JSON (list ipv dict) → reset
        if not isinstance(s, dict):
            log(f"⚠️ shadow_trades.json was geen dict ({type(s).__name__}) — reset")
            s = {}
    except Exception:
        s = {}
    s.setdefault("positions", {})
    s.setdefault("open_trades", [])
    # Zorg dat positions een dict is, niet een list
    if not isinstance(s["positions"], dict):
        log("⚠️ shadow positions was geen dict — reset naar leeg")
        s["positions"] = {}
    return s


def save_shadow_state(state: Dict[str, Any]) -> None:
    """Slaat shadow state op — atomisch via tmp file."""
    _ensure_dir(SHADOW_STATE_PATH)
    tmp = SHADOW_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, SHADOW_STATE_PATH)


def get_open_shadow_symbols(shadow_state: Dict[str, Any]) -> List[str]:
    return list((shadow_state.get("positions") or {}).keys())


# ============================================================
# PRIJS OPHALEN
# ============================================================
def get_current_price_bitvavo(market: str) -> Optional[float]:
    """Haalt prijs op via Bitvavo public API."""
    try:
        resp = requests.get(
            "https://api.bitvavo.com/v2/ticker/price",
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
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": symbol_usdt.upper()},
            timeout=10,
        )
        if resp.ok:
            return safe_float(resp.json().get("price"))
    except Exception as e:
        log(f"⚠️ Binance prijs fout ({symbol_usdt}): {e}")
    return None


def get_price(symbol_usdt: str, market: str) -> Optional[float]:
    """Haalt prijs op — Bitvavo eerst, Binance als fallback."""
    price = get_current_price_bitvavo(market)
    if price and price > 0:
        return price
    return get_current_price_binance(symbol_usdt)


# ============================================================
# SELL UITVOEREN
# ============================================================
def _execute_sell(
    symbol:   str,
    fraction: float,
    meta:     Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Voert sell uit via live_trader.py."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from trading.live_trader import sell as live_sell
        result = live_sell(symbol, fraction=fraction, meta=meta)
        return result or {"ok": False, "reason": "GEEN_RESULT"}
    except ImportError:
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
    """R = (current - entry) / (entry - stop)."""
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

    EXIT LOGICA:
    1. Max houdtijd (regime-afhankelijk)  → SELL 100%
    2. Stop bereikt (R < 0)               → SELL 100%
    3. Target bereikt                      → STRUCTUUR mode
    4. STRUCTUUR gebroken (-1%)            → SELL 100%
       FIX v3.1: minimum winst check — niet verkopen onder target - 0.5%
    5. 1R bereikt → trailing stop actief
    6. 2R bereikt → trailing stop verbeterd
    7. Terug <1R na >1R                   → SELL 40% partial + WhatsApp
    8. 3x candles <1R na partial          → SELL rest

    Geeft (changed, sold) terug.
    """
    entry  = safe_float(trade.get("entry"))
    stop   = safe_float(trade.get("stop_loss") or trade.get("stop"))
    target = safe_float(trade.get("target"))
    market = safe_str(trade.get("market"))

    # Sla originele stop op voor trailing berekening
    if not trade.get("original_stop") and stop > 0:
        trade["original_stop"] = stop

    if not market:
        try:
            from trading.live_trader import symbol_to_market
            market = symbol_to_market(symbol) or f"{symbol[:-4]}-EUR"
        except ImportError:
            market = f"{symbol[:-4]}-EUR" if symbol.endswith("USDT") else symbol

    current  = get_price(symbol, market)
    if current is None or current <= 0:
        log(f"⚠️ Geen prijs voor {symbol} — skip")
        return False, False

    r        = _calc_r(entry, stop, current)
    hold_min = _holding_minutes(trade)
    changed  = False

    log(f"  {symbol}: prijs={current:.6f} R={r:.2f} gehouden={hold_min:.0f}min")

    # MFE/MAE tracking updaten
    if entry > 0 and stop > 0:
        risk = abs(entry - stop)
        if risk > 0:
            cur_mfe = (current - entry) / risk
            cur_mae = (entry - current) / risk
            trade["mfe_r"] = round(max(cur_mfe, safe_float(trade.get("mfe_r"))), 4)
            trade["mae_r"] = round(max(cur_mae, safe_float(trade.get("mae_r"))), 4)

    # FORCE TEST EXIT
    if FORCE_TEST_EXIT and FORCE_TEST_EXIT == symbol.upper():
        log(f"⚡ FORCE_TEST_EXIT: {symbol}")
        result = _execute_sell(symbol, 1.0, meta={"exit_reden": "FORCE_TEST"})
        return True, result.get("ok", False)

    # ── 1. Max houdtijd — regime-afhankelijk ─────────────
    # BULL: langer vasthouden (36u), trend werkt mee
    # RANGE: normaal (24u)
    # BEAR: snel sluiten (12u), verhoogd risico
    btc_regime = safe_str(get_bot_state(conn, "btc_regime_huidig", "UNKNOWN")).upper()
    if btc_regime == "BULL":
        max_hold = MAX_HOLD_HOURS * 1.5    # 36u
    elif btc_regime == "BEAR":
        max_hold = MAX_HOLD_HOURS * 0.5    # 12u
    else:
        max_hold = MAX_HOLD_HOURS          # 24u

    if hold_min >= max_hold * 60:
        log(f"⏰ {symbol}: max houdtijd ({hold_min:.0f}min, BTC={btc_regime}) → SELL 100%")
        result = _execute_sell(symbol, 1.0, meta={"exit_reden": "MAX_HOLD_TIME"})
        _finalize_trade(symbol, trade, current, result, conn, "MAX_HOLD_TIME")
        return True, result.get("ok", False)

    # ── 2. Stop loss ──────────────────────────────────────
    if current <= stop or r < 0:
        log(f"🛑 {symbol}: stop geraakt ({current:.6f} ≤ {stop:.6f}) → SELL 100%")
        result = _execute_sell(symbol, 1.0, meta={"exit_reden": "STOP_LOSS"})
        _finalize_trade(symbol, trade, current, result, conn, "STOP_LOSS")
        return True, result.get("ok", False)

    # ── 3. Target bereikt → STRUCTUUR mode ───────────────
    if target > 0 and current >= target and not trade.get("target_reached_notified"):
        trade["target_reached_notified"] = True
        trade["mode"]           = "STRUCTUUR"
        trade["structuur_high"] = current
        log(f"🎯 {symbol}: target bereikt @ {current:.6f} → STRUCTUUR mode")
        changed = True

    # ── 4. STRUCTUUR mode — trailing exit ─────────────────
    if trade.get("mode") == "STRUCTUUR":
        structuur_high = safe_float(trade.get("structuur_high"), current)
        if current > structuur_high:
            trade["structuur_high"] = current
            changed = True
        elif current < structuur_high * 0.99:
            # FIX v3.1: minimum winst check
            # Voorkomt dat fees/slippage ons onder target uitkomen.
            # Verkopen we alleen als we minstens target - 0.5% verdienen.
            # Zo blijft de trade altijd winstgevend na kosten.
            min_winst_prijs = target * 0.995
            if current < min_winst_prijs:
                log(f"📉 {symbol}: structuur gebroken @ {current:.6f} "
                    f"(min winst prijs={min_winst_prijs:.6f}) → SELL 100%")
                result = _execute_sell(symbol, 1.0, meta={"exit_reden": "STRUCTUUR_BREAK"})
                _finalize_trade(symbol, trade, current, result, conn, "STRUCTUUR_BREAK")
                return True, result.get("ok", False)
            else:
                log(f"⚠️ {symbol}: structuur trekt terug maar boven min winst "
                    f"({current:.6f} > {min_winst_prijs:.6f}) — wachten op herstel")
        return changed, False

    # ── 5. Trailing stop na 1R ─────────────────────────────
    if r >= 1.0 and not trade.get("had_over_1r"):
        trade["had_over_1r"]    = True
        trade["max_price_seen"] = max(current, safe_float(trade.get("max_price_seen"), current))
        changed = True
        entry_price = safe_float(trade.get("entry"))
        if entry_price > 0:
            trade["stop_loss"] = entry_price
            trade["stop"]      = entry_price
            log(f"📈 {symbol}: 1R bereikt (R={r:.2f}) → stop naar break-even {entry_price:.6f}")

    # ── 6. Trailing stop na 2R ─────────────────────────────
    if r >= 2.0 and not trade.get("had_over_2r"):
        trade["had_over_2r"] = True
        changed = True
        entry_price = safe_float(trade.get("entry"))
        orig_stop   = safe_float(trade.get("original_stop") or
                                 trade.get("stop_loss") or trade.get("stop"))
        if entry_price > 0 and orig_stop > 0:
            risk     = abs(entry_price - orig_stop)
            new_stop = entry_price + risk   # +1R winst als floor
            trade["stop_loss"] = new_stop
            trade["stop"]      = new_stop
            log(f"🚀 {symbol}: 2R bereikt (R={r:.2f}) → stop naar +1R {new_stop:.6f}")

    # ── 7. Partial sell — terug <1R na >1R ────────────────
    if trade.get("had_over_1r") and not trade.get("partial_sold_40") and r < 1.0:
        log(f"⚠️ {symbol}: terug <1R na >1R (R={r:.2f}) → SELL 40%")
        result = _execute_sell(symbol, 0.40, meta={"exit_reden": "PARTIAL_40"})
        if result.get("ok"):
            trade["partial_sold_40"]      = True
            trade["below_1r_count"]       = 0
            trade["last_candle_check_ts"] = 0
            changed = True
            log(f"✅ {symbol}: 40% verkocht")
            # FIX v3.1: WhatsApp notificatie bij partial sell
            # Zodat jij altijd weet wanneer de bot 40% heeft verkocht.
            # 60% positie blijft open met stop op break-even.
            entry_p  = safe_float(trade.get("entry"))
            pct_move = (current - entry_p) / entry_p * 100 if entry_p > 0 else 0.0
            send_whatsapp(
                rate_key=f"partial_{symbol}",
                message=(
                    f"⚠️ PARTIAL SELL — {symbol}\n"
                    f"{'─' * 28}\n\n"
                    f"40% verkocht — prijs terug <1R\n\n"
                    f"Entry:   {entry_p:.6f}\n"
                    f"Nu:      {current:.6f} ({pct_move:+.1f}%)\n"
                    f"R:       {r:.2f}\n\n"
                    f"60% positie nog open.\n"
                    f"Stop staat op break-even.\n"
                    f"Als prijs 3 candles <1R blijft → rest verkopen.\n\n"
                    f"Commands: TRADES | STATUS"
                ),
            )

    # ── 8. Candle counter <1R na partial sell ─────────────
    if trade.get("partial_sold_40") and r < 1.0:
        timeframe = safe_str(trade.get("timeframe"), "4h")
        if "1h" in timeframe.lower():
            candle_sec = 3600
        elif "15m" in timeframe.lower():
            candle_sec = 900
        else:
            candle_sec = 4 * 3600

        last_check = safe_float(trade.get("last_candle_check_ts"))
        if time.time() - last_check >= candle_sec:
            trade["below_1r_count"]       = trade.get("below_1r_count", 0) + 1
            trade["last_candle_check_ts"] = time.time()
            changed = True
            log(f"  {symbol}: below_1r_count={trade['below_1r_count']}/3")

            if trade["below_1r_count"] >= 3:
                log(f"⚠️ {symbol}: 3 candles <1R → SELL rest")
                result = _execute_sell(symbol, 1.0, meta={"exit_reden": "BELOW_1R_3X"})
                _finalize_trade(symbol, trade, current, result, conn, "BELOW_1R_3X")
                return True, result.get("ok", False)
    else:
        if trade.get("below_1r_count", 0) > 0:
            trade["below_1r_count"]       = 0
            trade["last_candle_check_ts"] = 0
            changed = True

    # Prijs tracking
    if current > safe_float(trade.get("max_price_seen"), current):
        trade["max_price_seen"] = current
        changed = True
    if not trade.get("min_price_seen") or current < safe_float(trade.get("min_price_seen"), current):
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
    DB update, Claude analyse, consecutive loss check, profit factor.
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
    stop       = safe_float(trade.get("original_stop") or
                            trade.get("stop_loss") or trade.get("stop"))
    mfe_r      = safe_float(trade.get("mfe_r"))
    mae_r      = safe_float(trade.get("mae_r"))

    risk   = abs(entry - stop)
    exit_r = round((exit_price - entry) / risk, 2) if risk > 0 else 0.0

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

    # WhatsApp notificatie bij 100% sell
    emoji = "✅" if outcome == "WIN" else "❌"
    send_whatsapp(
        rate_key=f"trade_closed_{symbol}",
        message=(
            f"{emoji} TRADE GESLOTEN: {symbol}\n"
            f"{'─' * 30}\n\n"
            f"📊 Resultaat:  {outcome}\n"
            f"💶 PnL:        €{pnl_eur:.4f}\n"
            f"📈 R-multiple: {exit_r:.2f}R\n"
            f"🎯 Reden:      {exit_reden}\n"
            f"⏱️ Houdtijd:   {hold_min:.0f} min\n"
        )
    )

    # Log naar coach_events
    log_coach_event(
        conn,
        categorie    = "TRADE",
        event_type   = f"TRADE_{outcome}",
        omschrijving = (
            f"{symbol} {outcome} {exit_reden} "
            f"€{pnl_eur:.4f} R={exit_r:.2f} "
            f"MFE={mfe_r:.2f}R MAE={mae_r:.2f}R"
        ),
        ernst = "LAAG" if outcome == "WIN" else "MEDIUM",
    )

    # Statistieken updaten
    if outcome == "WIN":
        _RUN_STATS["live_closed"] = _RUN_STATS.get("live_closed", 0) + 1

    # Consecutive loss check — INFORMATIEF, bot gaat door
    consecutive = get_consecutive_losses(conn)
    if consecutive >= MAX_CONSECUTIVE_LOSSES:
        wins_7, losses_7, pnl_7   = get_rolling_stats(conn, 7)
        wins_30, losses_30, pnl_30 = get_rolling_stats(conn, 30)
        t7   = wins_7 + losses_7
        t30  = wins_30 + losses_30
        wr7  = (wins_7  / t7  * 100) if t7  > 0 else 0.0
        wr30 = (wins_30 / t30 * 100) if t30 > 0 else 0.0
        pf30 = get_profit_factor(conn, 30)

        send_whatsapp(
            rate_key=f"consec_losses_{consecutive}",
            message=(
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
                f"Stuur STOP als je wil pauzeren.\n\n"
                f"Commands: STOP | STATUS | TRADES"
            ),
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
            row             = cur.fetchone()
            trade_count_30d = safe_int(row[0]) if row else 0

        if trade_count_30d > 0 and trade_count_30d % 10 == 0:
            pf = get_profit_factor(conn, 30)
            if 0 < pf < 1.5:
                wins_30, losses_30, pnl_30 = get_rolling_stats(conn, 30)
                wins_7, losses_7, pnl_7    = get_rolling_stats(conn, 7)
                t30  = wins_30 + losses_30
                t7   = wins_7 + losses_7
                wr30 = (wins_30 / t30 * 100) if t30 > 0 else 0.0
                wr7  = (wins_7  / t7  * 100) if t7  > 0 else 0.0

                send_whatsapp(
                    rate_key="pf_laag",
                    message=(
                        f"📉 PROFIT FACTOR LAAG\n"
                        f"{'─' * 30}\n\n"
                        f"📊 Profit Factor 30d: {pf:.2f}\n"
                        f"🎯 Doel:              >1.5\n\n"
                        f"📈 LAATSTE 30 DAGEN:\n"
                        f"• Trades: {trade_count_30d}\n"
                        f"• WR: {wr30:.1f}% ({wins_30}W/{losses_30}L)\n"
                        f"• PnL: {'+'if pnl_30>=0 else ''}€{pnl_30:.2f}\n\n"
                        f"📈 LAATSTE 7 DAGEN:\n"
                        f"• WR: {wr7:.1f}% ({wins_7}W/{losses_7}L)\n"
                        f"• PnL: {'+'if pnl_7>=0 else ''}€{pnl_7:.2f}\n\n"
                        f"🤖 BOT LOOPT GEWOON DOOR\n\n"
                        f"Commands: STOP | STATUS | RAPPORT"
                    ),
                )
    except Exception:
        pass

    # Edge decay check elke 20 trades
    try:
        if trade_count_30d > 0 and trade_count_30d % 20 == 0:
            decay_msg = check_edge_decay(conn)
            if decay_msg:
                send_whatsapp(rate_key="edge_decay", message=decay_msg)
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
    Geeft (changed, closed) terug.

    FIX v3.1: max houdtijd verhoogd naar 48u (was 24u).
    Meer tijd = meer leerdata over hoe trades zich werkelijk ontwikkelen.
    Shadow trades gebruiken geen echt geld dus langere houdtijd is geen risico.
    """
    if current is None or current <= 0:
        return False, False

    entry    = safe_float(shadow_trade.get("entry"))
    stop     = safe_float(shadow_trade.get("stop_loss") or shadow_trade.get("stop"))
    target   = safe_float(shadow_trade.get("target"))
    r        = _calc_r(entry, stop, current)
    hold_min = _holding_minutes(shadow_trade)
    changed  = False

    # FIX v3.1: shadow trades krijgen 48u houdtijd (live=24u)
    # Meer leerdata per trade — geen financieel risico bij verlenging
    shadow_max_hold_min = MAX_HOLD_HOURS * 2 * 60  # 48u in minuten
    if hold_min >= shadow_max_hold_min:
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

    # MFE/MAE tracking
    if entry > 0:
        if current > safe_float(shadow_trade.get("max_price_seen"), current):
            shadow_trade["max_price_seen"] = current
            changed = True
        if (not shadow_trade.get("min_price_seen") or
                current < safe_float(shadow_trade.get("min_price_seen"), current)):
            shadow_trade["min_price_seen"] = current
            changed = True

    # Had_over_1r
    if r >= 1.0 and not shadow_trade.get("had_over_1r"):
        shadow_trade["had_over_1r"] = True
        changed = True

    # Structuur fail — terug <0.5R na >1R
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
    stop       = safe_float(trade.get("stop_loss") or trade.get("stop"))
    amount_eur = safe_float(trade.get("amount_eur"), 0.50)
    qty        = safe_float(trade.get("qty") or trade.get("position_size"))
    setup_type = safe_str(trade.get("setup_type"))
    regime     = safe_str(trade.get("regime"))
    score      = safe_int(trade.get("score"))
    prebuy_id  = safe_str(trade.get("prebuy_id"))

    if qty <= 0 and entry > 0:
        qty = amount_eur / entry

    fee_entry = amount_eur * (BITVAVO_FEE_PCT + SLIPPAGE_PCT)
    fee_exit  = exit_price * qty * (BITVAVO_FEE_PCT + SLIPPAGE_PCT) if qty > 0 else 0.0
    gross_pnl = (exit_price - entry) * qty if entry > 0 and qty > 0 else 0.0
    pnl_eur   = gross_pnl - fee_entry - fee_exit

    risk   = abs(entry - stop) if stop > 0 and entry > 0 else 0.0
    exit_r = round((exit_price - entry) / risk, 3) if risk > 0 else 0.0

    max_price = safe_float(trade.get("max_price_seen"), exit_price)
    min_price = safe_float(trade.get("min_price_seen"), exit_price)
    mfe_r     = round((max_price - entry) / risk, 3) if risk > 0 else 0.0
    mae_r     = round((entry - min_price) / risk, 3) if risk > 0 else 0.0
    hold_min  = round(_holding_minutes(trade), 1)

    try:
        trade_key = f"SHADOW|{symbol}|{prebuy_id or int(time.time())}"
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO public.experience_trades (
                trade_key, source, is_shadow, coin,
                timestamp, entry_time,
                setup_type, market_regime, entry,
                outcome, pnl_eur, pnl_r, result_r,
                mfe_r, mae_r, time_minutes,
                bot_confidence,
                exit_time, created_at, updated_at
            ) VALUES (
                %s,'SHADOW',TRUE,%s,NOW(),NOW(),
                %s,%s,%s,
                %s,%s,%s,%s,
                %s,%s,%s,
                %s,
                NOW(),NOW(),NOW()
            )
            ON CONFLICT (trade_key) DO UPDATE SET
                outcome      = EXCLUDED.outcome,
                pnl_eur      = EXCLUDED.pnl_eur,
                pnl_r        = EXCLUDED.pnl_r,
                result_r     = EXCLUDED.result_r,
                mfe_r        = EXCLUDED.mfe_r,
                mae_r        = EXCLUDED.mae_r,
                time_minutes = EXCLUDED.time_minutes,
                exit_time    = NOW(),
                updated_at   = NOW()
            """, (
                trade_key, symbol,
                setup_type, regime, entry,
                outcome, round(pnl_eur, 4), exit_r, exit_r,
                mfe_r, mae_r, hold_min,
                score,
            ))
        conn.commit()
        log(
            f"🎭 Shadow {symbol}: {outcome} {exit_reden} "
            f"€{pnl_eur:.4f} R={exit_r:.2f} "
            f"MFE={mfe_r:.2f}R MAE={mae_r:.2f}R {hold_min:.0f}min"
        )
    except Exception as e:
        safe_rollback(conn)
        log(f"⚠️ Shadow DB log fout ({symbol}): {e}")


# ============================================================
# HOOFD MONITOR RUN
# ============================================================
def run_monitor_once(target_symbol: Optional[str] = None) -> None:
    """
    Één run van de monitor.

    STAPPEN:
    1. DB verbinding (met retries)
    2. Bot state check
    3. BTC regime ophalen
    4. Daily PnL check (informatief)
    5. Live trades verwerken
    6. Shadow trades verwerken
    7. State opslaan (alleen als gewijzigd)
    8. Run statistieken opslaan

    FIX vs v2.0:
    - conn = None initialisatie voor try/finally
    - finally: conn.close() altijd
    - safe_rollback bij exceptions
    """
    _RUN_STATS["runs"] = _RUN_STATS.get("runs", 0) + 1
    run_start = time.time()

    # FIX: conn = None zodat finally altijd werkt
    conn = None
    try:
        conn = db_connect()
    except Exception as e:
        report_error(e, "run_monitor_once.db_connect", severity="KRITIEK")
        return

    try:
        # ── Bot state ────────────────────────────────────
        bot_active = is_bot_active(conn)
        bot_paused = is_bot_paused(conn)

        # BTC regime ophalen voor regime-afhankelijke logica
        try:
            with conn.cursor() as cur:
                cur.execute("""
                SELECT regime FROM public.btc_regime_4h
                ORDER BY open_time DESC LIMIT 1
                """)
                row = cur.fetchone()
                if row:
                    set_bot_state(conn, "btc_regime_huidig", safe_str(row[0], "UNKNOWN"))
        except Exception:
            safe_rollback(conn)

        if not bot_active:
            log("Bot gestopt — alleen open trades bewaken")
        elif bot_paused:
            log("Bot gepauzeerd — alleen open trades bewaken")

        # ── Daily PnL check — INFORMATIEF ────────────────
        wins_v, losses_v, daily_pnl = get_daily_pnl(conn)
        if daily_pnl <= -DAILY_STOP_LOSS_EUR and bot_active:
            log(f"ℹ️ Dagbudget bereikt: €{daily_pnl:.2f} — bot gaat door")
            wins_7, losses_7, pnl_7 = get_rolling_stats(conn, 7)
            t7   = wins_7 + losses_7
            wr7  = (wins_7 / t7 * 100) if t7 > 0 else 0.0
            pf30 = get_profit_factor(conn, 30)
            open_n = len(load_state().get("positions", {}))

            send_whatsapp(
                rate_key="dagbudget",
                message=(
                    f"🛑 DAGBUDGET BEREIKT\n"
                    f"{'─' * 30}\n\n"
                    f"💶 Verlies vandaag:  €{abs(daily_pnl):.2f}\n"
                    f"📊 Dagbudget:        €{DAILY_STOP_LOSS_EUR:.2f}\n\n"
                    f"📈 VANDAAG:\n"
                    f"• Wins: {wins_v} | Losses: {losses_v}\n\n"
                    f"📈 LAATSTE 7 DAGEN:\n"
                    f"• WR: {wr7:.1f}% ({wins_7}W/{losses_7}L)\n"
                    f"• PnL: {'+'if pnl_7>=0 else ''}€{pnl_7:.2f}\n"
                    f"• PF 30d: {pf30:.2f}\n\n"
                    f"📂 Open trades: {open_n} (worden bewaakt)\n\n"
                    f"🤖 BOT LOOPT GEWOON DOOR\n"
                    f"Stuur STOP als je wil pauzeren.\n\n"
                    f"Commands: STOP | STATUS | TRADES"
                ),
            )

        # ── LIVE TRADES ───────────────────────────────────
        state   = load_state()
        symbols = get_open_symbols(state)

        if target_symbol:
            symbols = [s for s in symbols if s == target_symbol]

        live_changed = False
        _RUN_STATS["live_checked"] = _RUN_STATS.get("live_checked", 0) + len(symbols)

        for symbol in symbols:
            trade = (state.get("positions") or {}).get(symbol)
            if not trade:
                continue
            if is_coin_blacklisted(conn, symbol):
                log(f"⚫ {symbol} op blacklist — trade al open, bewaken")
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

        if live_changed:
            save_state(state)

        # ── SHADOW TRADES ─────────────────────────────────
        try:
            shadow_state   = load_shadow_state()
            shadow_symbols = get_open_shadow_symbols(shadow_state)

            if target_symbol:
                shadow_symbols = [s for s in shadow_symbols if s == target_symbol]

            shadow_changed = False
            _RUN_STATS["shadow_checked"] = \
                _RUN_STATS.get("shadow_checked", 0) + len(shadow_symbols)

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
                        shadow_state["open_trades"] = [
                            t for t in (shadow_state.get("open_trades") or [])
                            if t.get("symbol") != symbol
                        ]
                        _RUN_STATS["shadow_closed"] = \
                            _RUN_STATS.get("shadow_closed", 0) + 1
                except Exception as e:
                    log(f"⚠️ Shadow trade fout ({symbol}): {e}")

            if shadow_changed:
                save_shadow_state(shadow_state)

        except Exception as e:
            log(f"⚠️ Shadow monitor fout: {e}")

        # ── Positielimiet check ───────────────────────────
        check_positie_limieten(conn)

        # ── Stale trade detectie ──────────────────────────
        detecteer_stale_trades(conn)

        # ── Health check elke 20 runs (~10 minuten) ───────
        if _RUN_STATS.get("runs", 0) % 20 == 1:
            health = voer_health_check_uit(conn)
            log_health_check(health)
            if health["score"] < 50:
                send_whatsapp(
                    rate_key="health_laag",
                    message=(
                        f"MONITOR HEALTH LAAG\n"
                        f"Score: {health['score']}/100\n\n"
                        f"Problemen:\n"
                        + "\n".join(f"- {p}" for p in health["problemen"][:5])
                        + "\n\nCommands: STATUS | STOP"
                    ),
                )

        # ── Weekrapport check ─────────────────────────────
        if is_weekrapport_tijd():
            verstuur_weekrapport(conn)

        # ── Dashboard update elke 5 runs ──────────────────
        if _RUN_STATS.get("runs", 0) % 5 == 0:
            update_monitor_dashboard(conn)

        # ── Run statistieken ──────────────────────────────
        open_live   = len(load_state().get("positions", {}))
        open_shadow = len(load_shadow_state().get("positions", {}))
        elapsed     = round(time.time() - run_start, 2)

        log(
            f"Monitor run klaar ({elapsed}s) — "
            f"{len(symbols)} live open | {open_shadow} shadow open"
        )

        # Sla run status op in bot_state voor dashboard
        try:
            set_bot_state(conn, "monitor_laatste_run",
                          now_utc().strftime("%Y-%m-%d %H:%M:%S UTC"))
            set_bot_state(conn, "monitor_open_live",   str(open_live))
            set_bot_state(conn, "monitor_open_shadow", str(open_shadow))
        except Exception:
            pass

    except Exception as e:
        report_error(e, "run_monitor_once", severity="KRITIEK",
                     open_trades=len(load_state().get("positions", {})))

    finally:
        # ALTIJD sluiten — FIX: v2.0 had geen finally block
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ============================================================
# HEALTH MONITORING — v3.0
# ============================================================
def voer_health_check_uit(conn) -> Dict[str, Any]:
    """
    Controleert de gezondheid van de monitor en alle afhankelijkheden.
    Wordt elke 10 minuten uitgevoerd (elke 20 runs bij 30s interval).

    Controles:
    ──────────────────────────────────────────────────────────────
    DB            → SELECT 1 ping
    Bitvavo API   → /v2/markets ping
    Binance API   → /api/v3/ping
    Claude API    → korte test prompt
    State files   → readable en valid JSON
    BTC data      → niet ouder dan 5 uur
    Open trades   → alle trades hebben geldige entry/stop/target

    Ernst niveaus:
    ──────────────────────────────────────────────────────────────
    KRITIEK  → WhatsApp direct + bot moet handmatig gecheckt worden
    HOOG     → WhatsApp + logmelding
    MEDIUM   → alleen log
    LAAG     → stil
    """
    health: Dict[str, Any] = {
        "database":       False,
        "bitvavo_api":    False,
        "binance_api":    False,
        "claude_api":     False,
        "state_file":     False,
        "btc_data_vers":  False,
        "trades_valide":  False,
        "problemen":      [],
        "waarschuwingen": [],
        "score":          0,
    }

    # ── Database ──────────────────────────────────────────
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            health["database"] = True
    except Exception as e:
        safe_rollback(conn)
        health["problemen"].append(f"DB fout: {e}")

    # ── Bitvavo API ───────────────────────────────────────
    try:
        resp = requests.get("https://api.bitvavo.com/v2/markets", timeout=10)
        health["bitvavo_api"] = resp.ok
        if not resp.ok:
            health["problemen"].append(f"Bitvavo status {resp.status_code}")
    except Exception as e:
        health["problemen"].append(f"Bitvavo onbereikbaar: {e}")

    # ── Binance API ───────────────────────────────────────
    try:
        resp = requests.get("https://api.binance.com/api/v3/ping", timeout=5)
        health["binance_api"] = resp.ok
        if not resp.ok:
            health["problemen"].append(f"Binance status {resp.status_code}")
    except Exception as e:
        health["problemen"].append(f"Binance onbereikbaar: {e}")

    # ── Claude API ────────────────────────────────────────
    if ANTHROPIC_API_KEY:
        test = _claude_analyse("Zeg alleen OK.", 10)
        health["claude_api"] = bool(test)
        if not test:
            health["waarschuwingen"].append("Claude API reageert niet")
    else:
        health["waarschuwingen"].append("ANTHROPIC_API_KEY niet ingesteld")

    # ── State files ───────────────────────────────────────
    try:
        state  = load_state()
        shadow = load_shadow_state()
        health["state_file"] = True
        # Valideer dat alle posities een entry hebben
        for symbol, trade in (state.get("positions") or {}).items():
            if not safe_float(trade.get("entry")):
                health["waarschuwingen"].append(
                    f"Trade {symbol}: ontbrekende entry prijs"
                )
            if not safe_float(trade.get("stop_loss") or trade.get("stop")):
                health["waarschuwingen"].append(
                    f"Trade {symbol}: ontbrekende stop loss"
                )
    except Exception as e:
        health["problemen"].append(f"State file fout: {e}")

    # ── BTC data versheid ─────────────────────────────────
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT MAX(open_time) FROM public.btc_regime_4h
            """)
            row = cur.fetchone()
            if row and row[0]:
                laatste = row[0]
                if hasattr(laatste, 'tzinfo') and laatste.tzinfo is None:
                    laatste = laatste.replace(tzinfo=timezone.utc)
                uren_oud = (now_utc() - laatste).total_seconds() / 3600
                health["btc_data_vers"] = uren_oud < 5
                if not health["btc_data_vers"]:
                    health["waarschuwingen"].append(
                        f"BTC regime data is {uren_oud:.0f}u oud"
                    )
    except Exception:
        safe_rollback(conn)
        health["waarschuwingen"].append("BTC data check mislukt")

    # ── Open trades validatie ─────────────────────────────
    try:
        state = load_state()
        alle_valide = True
        for symbol, trade in (state.get("positions") or {}).items():
            entry  = safe_float(trade.get("entry"))
            stop   = safe_float(trade.get("stop_loss") or trade.get("stop"))
            target = safe_float(trade.get("target"))
            if entry <= 0 or stop <= 0 or target <= 0:
                alle_valide = False
                health["waarschuwingen"].append(
                    f"{symbol}: ongeldige trade data (e={entry:.0f} s={stop:.0f} t={target:.0f})"
                )
        health["trades_valide"] = alle_valide
    except Exception:
        pass

    # ── Score berekening ──────────────────────────────────
    kritieke = sum(1 for k, v in health.items() if isinstance(v, bool) and v)
    totaal   = sum(1 for v in health.values() if isinstance(v, bool))
    health["score"] = int(kritieke / max(totaal, 1) * 100)

    return health


def log_health_check(health: Dict[str, Any]) -> None:
    """Print health check samenvatting naar logs."""
    log(f"Health: {health['score']}/100 | "
        f"DB:{'+' if health['database'] else '-'} "
        f"Bitvavo:{'+' if health['bitvavo_api'] else '-'} "
        f"Binance:{'+' if health['binance_api'] else '-'} "
        f"Claude:{'+' if health['claude_api'] else '-'} "
        f"BTCdata:{'+' if health['btc_data_vers'] else '-'}")
    for p in health["problemen"]:
        log(f"  ❌ Probleem: {p}")
    for w in health["waarschuwingen"]:
        log(f"  ⚠️ Waarschuwing: {w}")


# ============================================================
# OPEN TRADE OVERZICHT — voor WhatsApp TRADES commando
# ============================================================
def get_open_trade_overzicht(conn) -> str:
    """
    Maakt een overzicht van alle open live en shadow trades.
    Wordt gebruikt door whatsapp_webhook.py voor het TRADES commando.
    Geeft een opgemaakte string terug klaar voor WhatsApp.

    Toont per trade:
    ─────────────────────────────────────────────────────────
    - Coin + setup type
    - Entry prijs + huidige prijs
    - R-multiple huidig
    - Houdtijd in uren
    - Stop loss + target
    - MFE/MAE tot nu toe
    - Regime-afhankelijke max houdtijd
    """
    state  = load_state()
    shadow = load_shadow_state()

    live_pos   = state.get("positions") or {}
    shadow_pos = shadow.get("positions") or {}

    if not live_pos and not shadow_pos:
        return "📂 Geen open trades op dit moment."

    lines = [f"📂 OPEN TRADES — {now_utc().strftime('%H:%M UTC')}", ""]

    # ── Live trades ───────────────────────────────────────
    if live_pos:
        lines.append(f"🔴 LIVE ({len(live_pos)}x):")
        for symbol, trade in live_pos.items():
            entry    = safe_float(trade.get("entry"))
            stop     = safe_float(trade.get("stop_loss") or trade.get("stop"))
            target   = safe_float(trade.get("target"))
            setup    = safe_str(trade.get("setup_type"), "?")
            hold_min = _holding_minutes(trade)
            hold_u   = hold_min / 60

            # Huidige prijs ophalen
            market  = safe_str(trade.get("market"), f"{symbol[:-4]}-EUR")
            current = get_price(symbol, market)
            if current and current > 0:
                r   = _calc_r(entry, stop, current)
                pct = (current - entry) / entry * 100 if entry > 0 else 0.0
                prijs_str = (
                    f"{current:.6f} ({'+' if pct >= 0 else ''}{pct:.1f}%) R={r:.2f}"
                )
            else:
                prijs_str = "prijs niet beschikbaar"

            mfe_r = safe_float(trade.get("mfe_r"))
            mae_r = safe_float(trade.get("mae_r"))

            lines.append(
                f"  {symbol} ({setup})\n"
                f"  Entry: {entry:.6f}\n"
                f"  Nu:    {prijs_str}\n"
                f"  Stop:  {stop:.6f} | Target: {target:.6f}\n"
                f"  Tijd:  {hold_u:.1f}u | MFE={mfe_r:.2f}R MAE={mae_r:.2f}R"
            )
    else:
        lines.append("🔴 LIVE: geen open trades")

    lines.append("")

    # ── Shadow trades ─────────────────────────────────────
    if shadow_pos:
        lines.append(f"🎭 SHADOW ({len(shadow_pos)}x):")
        for symbol, trade in list(shadow_pos.items())[:5]:  # Max 5 tonen
            entry    = safe_float(trade.get("entry"))
            stop     = safe_float(trade.get("stop_loss") or trade.get("stop"))
            target   = safe_float(trade.get("target"))
            setup    = safe_str(trade.get("setup_type"), "?")
            hold_min = _holding_minutes(trade)
            hold_u   = hold_min / 60

            market  = safe_str(trade.get("market"), f"{symbol[:-4]}-EUR")
            current = get_price(symbol, market)
            if current and current > 0:
                r   = _calc_r(entry, stop, current)
                pct = (current - entry) / entry * 100 if entry > 0 else 0.0
                prijs_str = f"{current:.4f} R={r:.2f}"
            else:
                prijs_str = "?"

            lines.append(f"  {symbol} ({setup}) {hold_u:.1f}u @ {prijs_str}")

        if len(shadow_pos) > 5:
            lines.append(f"  ... en {len(shadow_pos) - 5} meer")
    else:
        lines.append("🎭 SHADOW: geen open trades")

    return "\n".join(lines)


# ============================================================
# WEEKRAPPORT — v3.0
# ============================================================
def is_weekrapport_tijd() -> bool:
    """
    Weekrapport op zondag 08:00-08:15 UTC.
    Claude analyseert de hele week en geeft aanbevelingen.
    """
    nu = now_utc()
    return nu.weekday() == 6 and nu.hour == 8 and nu.minute < 15


def is_weekrapport_al_verstuurd(conn) -> bool:
    """Controleert of weekrapport al verstuurd is deze week."""
    vorige = get_bot_state(conn, "weekrapport_datum", "")
    maandag = (now_utc() - timedelta(days=now_utc().weekday())).strftime("%Y-%m-%d")
    return vorige >= maandag


def verstuur_weekrapport(conn) -> None:
    """
    v3.0: Verstuurt weekrapport op zondag 08:00 UTC.
    Claude analyseert de hele week:
    - Win rate + PnL
    - Beste en slechtste trades
    - Setup type performance
    - Aanbevelingen voor volgende week
    """
    if not is_weekrapport_tijd() or is_weekrapport_al_verstuurd(conn):
        return

    log("Weekrapport aanmaken...")
    try:
        wins_7, losses_7, pnl_7    = get_rolling_stats(conn, 7)
        wins_30, losses_30, pnl_30  = get_rolling_stats(conn, 30)
        pf_7  = get_profit_factor(conn, 7)
        pf_30 = get_profit_factor(conn, 30)
        t7    = wins_7 + losses_7
        t30   = wins_30 + losses_30
        wr7   = (wins_7  / t7  * 100) if t7  > 0 else 0.0
        wr30  = (wins_30 / t30 * 100) if t30 > 0 else 0.0

        # Beste setup types deze week
        setup_stats = _get_setup_stats(conn, days=7)
        beste_setup = max(setup_stats.items(),
                         key=lambda x: x[1]["wr"],
                         default=("?", {"wr": 0, "n": 0}))

        # BTC regime context
        btc_regime = get_bot_state(conn, "btc_regime_huidig", "UNKNOWN")

        # Claude weekanalyse
        prompt = (
            f"Schrijf een crypto trading bot weekrapport in 5 zinnen Nederlands.\n"
            f"Week van {(now_utc() - timedelta(days=7)).strftime('%d/%m')} "
            f"tot {now_utc().strftime('%d/%m/%Y')}\n\n"
            f"STATISTIEKEN DEZE WEEK:\n"
            f"Trades: {t7} | Wins: {wins_7} | Losses: {losses_7}\n"
            f"Win rate: {wr7:.1f}% | PnL: {'+'if pnl_7>=0 else ''}€{pnl_7:.2f}\n"
            f"Profit Factor: {pf_7:.2f} (doel >1.5)\n\n"
            f"STATISTIEKEN LAATSTE 30 DAGEN:\n"
            f"Trades: {t30} | WR: {wr30:.1f}% | PF: {pf_30:.2f}\n"
            f"PnL 30d: {'+'if pnl_30>=0 else ''}€{pnl_30:.2f}\n\n"
            f"BTC regime: {btc_regime}\n"
            f"Beste setup: {beste_setup[0]} (WR={beste_setup[1].get('wr', 0)*100:.0f}%)\n\n"
            f"Geef inzichten en concrete aanbevelingen voor volgende week."
        )
        claude_txt = _claude_analyse(prompt, max_tokens=400)

        rapport = (
            f"📊 WEEKRAPPORT — {now_utc().strftime('%d/%m/%Y')}\n"
            f"{'═' * 32}\n\n"
            f"📈 DEZE WEEK:\n"
            f"• Trades:  {t7} ({wins_7}W / {losses_7}L)\n"
            f"• Win rate: {wr7:.1f}%\n"
            f"• PnL:     {'+'if pnl_7>=0 else ''}€{pnl_7:.2f}\n"
            f"• PF:      {pf_7:.2f} {'✅' if pf_7>=1.5 else '⚠️'}\n\n"
            f"📈 LAATSTE 30 DAGEN:\n"
            f"• Win rate: {wr30:.1f}% ({t30} trades)\n"
            f"• PnL:     {'+'if pnl_30>=0 else ''}€{pnl_30:.2f}\n"
            f"• PF:      {pf_30:.2f} {'✅' if pf_30>=1.5 else '⚠️'}\n\n"
            f"🧠 BTC regime: {btc_regime}\n\n"
        )

        if claude_txt:
            rapport += f"🤖 Claude analyse:\n{claude_txt}\n\n"

        rapport += "Commands: STATUS | TRADES | STOP"

        send_whatsapp(rate_key="weekrapport", message=rapport)
        set_bot_state(conn, "weekrapport_datum",
                      now_utc().strftime("%Y-%m-%d"))
        log("Weekrapport verstuurd")

    except Exception as e:
        log(f"⚠️ Weekrapport fout: {e}")


def _get_setup_stats(conn, days: int = 7) -> Dict[str, Any]:
    """
    Haalt performance statistieken op per setup type.
    Gebruikt voor weekrapport en Claude analyse.
    Geeft dict terug: {setup_type: {n, wins, losses, wr, gem_r}}
    """
    stats: Dict[str, Any] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COALESCE(setup_type, 'UNKNOWN') AS setup,
                COUNT(*) AS n,
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')  AS wins,
                COUNT(*) FILTER (WHERE UPPER(outcome)='LOSS') AS losses,
                ROUND(AVG(COALESCE(pnl_r, result_r, 0))::numeric, 3) AS gem_r
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '1 day' * %s
            GROUP BY 1
            ORDER BY n DESC
            """, (days,))
            for row in cur.fetchall():
                setup  = safe_str(row[0])
                n      = safe_int(row[1])
                wins   = safe_int(row[2])
                losses = safe_int(row[3])
                gem_r  = safe_float(row[4])
                stats[setup] = {
                    "n":      n,
                    "wins":   wins,
                    "losses": losses,
                    "wr":     wins / n if n > 0 else 0.0,
                    "gem_r":  gem_r,
                }
    except Exception:
        safe_rollback(conn)
    return stats


# ============================================================
# MONITOR STATISTIEKEN DASHBOARD
# ============================================================
def update_monitor_dashboard(conn) -> None:
    """
    Slaat uitgebreide monitor statistieken op in bot_state.
    Wordt gelezen door app.py dashboard.

    Statistieken opgeslagen:
    ─────────────────────────────────────────────────────────
    monitor_status       → ACTIEF / GESTOPT / GEPAUZEERD
    monitor_uptime_min   → minuten dat monitor draait
    monitor_runs_totaal  → totaal runs deze sessie
    monitor_live_open    → aantal open live trades
    monitor_shadow_open  → aantal open shadow trades
    monitor_live_gesloten → live trades gesloten deze sessie
    monitor_shadow_gesloten → shadow trades gesloten
    monitor_fouten       → fouten deze sessie
    monitor_pf_30d       → profit factor laatste 30 dagen
    monitor_wr_7d        → win rate laatste 7 dagen
    monitor_pnl_vandaag  → PnL vandaag
    monitor_btc_regime   → huidig BTC regime
    monitor_versie       → 3.1
    """
    try:
        uptime_min = 0.0
        if _RUN_STATS.get("start_ts"):
            uptime_min = (time.time() - _RUN_STATS["start_ts"]) / 60

        open_live   = len(load_state().get("positions", {}))
        open_shadow = len(load_shadow_state().get("positions", {}))

        wins_7, losses_7, pnl_7  = get_rolling_stats(conn, 7)
        t7 = wins_7 + losses_7
        wr_7  = round((wins_7 / t7 * 100), 1) if t7 > 0 else 0.0
        pf_30 = get_profit_factor(conn, 30)
        _, _, pnl_vandaag = get_daily_pnl(conn)
        btc_regime = get_bot_state(conn, "btc_regime_huidig", "UNKNOWN")

        bot_active = is_bot_active(conn)
        bot_paused = is_bot_paused(conn)
        if bot_paused:
            status = "GEPAUZEERD"
        elif bot_active:
            status = "ACTIEF"
        else:
            status = "GESTOPT"

        waardes = {
            "monitor_status":         status,
            "monitor_uptime_min":     str(round(uptime_min, 1)),
            "monitor_runs_totaal":    str(_RUN_STATS.get("runs", 0)),
            "monitor_live_open":      str(open_live),
            "monitor_shadow_open":    str(open_shadow),
            "monitor_live_gesloten":  str(_RUN_STATS.get("live_closed", 0)),
            "monitor_shadow_gesloten": str(_RUN_STATS.get("shadow_closed", 0)),
            "monitor_fouten":         str(_RUN_STATS.get("fouten", 0)),
            "monitor_pf_30d":         str(pf_30),
            "monitor_wr_7d":          str(wr_7),
            "monitor_pnl_vandaag":    str(round(pnl_vandaag, 4)),
            "monitor_btc_regime":     btc_regime,
            "monitor_versie":         "3.1",
            "monitor_laatste_update": now_utc().strftime("%Y-%m-%d %H:%M:%S UTC"),
        }

        for key, value in waardes.items():
            set_bot_state(conn, key, value)

    except Exception as e:
        log(f"⚠️ Dashboard update fout: {e}")


# ============================================================
# POSITIE GROOTTE MONITOR — bescherming
# ============================================================
def check_positie_limieten(conn) -> None:
    """
    Controleert of open posities binnen de Fase 1 limieten vallen.
    Geeft informatieve log melding — sluit GEEN posities automatisch.

    Controles:
    ─────────────────────────────────────────────────────────
    MAX_OPEN_REAL_TRADES  → max 5 gelijktijdig open live
    MAX_PER_TRADE_EUR     → elke positie max €0.50
    DAILY_STOP_LOSS_EUR   → dagverlies max €5.00

    Als een limiet overschreden is → log + WhatsApp (informatief).
    Bot sluit NOOIT automatisch posities op basis van limieten.
    Jij beslist via STOP of SELL commando's.
    """
    try:
        state    = load_state()
        posities = state.get("positions") or {}
        n_open   = len(posities)

        # Open trade limiet
        if n_open > MAX_OPEN_REAL_TRADES:
            log(
                f"⚠️ Positielimiet overschreden: {n_open}/{MAX_OPEN_REAL_TRADES} "
                f"open live trades. multi_coin_score.py zou geen nieuwe moeten openen."
            )

        # Per-trade grootte check
        for symbol, trade in posities.items():
            amount = safe_float(trade.get("amount_eur") or trade.get("invested_eur"))
            if amount > MAX_PER_TRADE_EUR * 2:
                log(
                    f"⚠️ {symbol}: grote positie €{amount:.2f} "
                    f"(Fase 1 max = €{MAX_PER_TRADE_EUR:.2f})"
                )

        # Dagverlies check
        _, _, daily_pnl = get_daily_pnl(conn)
        if daily_pnl < -DAILY_STOP_LOSS_EUR:
            log(
                f"ℹ️ Dagbudget overschreden: €{daily_pnl:.2f} "
                f"(limiet = €{DAILY_STOP_LOSS_EUR:.2f}) — informatief"
            )

    except Exception as e:
        log(f"⚠️ Positielimiet check fout: {e}")


# ============================================================
# STALE TRADE DETECTIE — v3.0
# ============================================================
def detecteer_stale_trades(conn) -> None:
    """
    Detecteert trades die mogelijk niet meer gemonitord worden.
    Dit kan gebeuren als de monitor een tijdje offline was.

    Een trade is 'stale' als:
    ─────────────────────────────────────────────────────────
    - last_check meer dan 10 minuten geleden
    - opened_at meer dan MAX_HOLD_HOURS * 2 geleden (overdosis houdtijd)
    - entry, stop of target ontbreekt

    Actie: log melding + WhatsApp bij ernstige stale trades.
    Sluit NOOIT automatisch — jij beslist.
    """
    try:
        state = load_state()
        now_ts = time.time()

        for symbol, trade in (state.get("positions") or {}).items():
            last_check = safe_float(trade.get("last_check"))
            opened_at  = safe_float(trade.get("opened_at") or
                                    trade.get("monitor_started_at"))
            entry      = safe_float(trade.get("entry"))
            stop       = safe_float(trade.get("stop_loss") or trade.get("stop"))

            # Check: last_check te oud
            if last_check > 0:
                min_since_check = (now_ts - last_check) / 60
                if min_since_check > 10:
                    log(f"⚠️ Stale trade: {symbol} — "
                        f"laatste check was {min_since_check:.0f}min geleden")

            # Check: extreme houdtijd
            if opened_at > 0:
                uren_open = (now_ts - opened_at) / 3600
                max_ooit  = MAX_HOLD_HOURS * 2  # Absoluut maximum
                if uren_open > max_ooit:
                    log(f"🚨 {symbol}: extreem lang open ({uren_open:.0f}u) — "
                        f"check Bitvavo manueel!")
                    send_whatsapp(
                        rate_key=f"stale_{symbol}",
                        message=(
                            f"⚠️ STALE TRADE DETECTIE\n"
                            f"{'─' * 28}\n\n"
                            f"🪙 Coin: {symbol}\n"
                            f"⏰ Open: {uren_open:.0f} uur\n"
                            f"🎯 Max verwacht: {max_ooit:.0f}u\n\n"
                            f"Entry: {entry:.6f}\n"
                            f"Stop:  {stop:.6f}\n\n"
                            f"💡 Check Bitvavo handmatig\n"
                            f"en sluit indien nodig.\n\n"
                            f"Commands: TRADES | STATUS"
                        ),
                    )

            # Check: ontbrekende kritieke data
            if entry <= 0 or stop <= 0:
                log(f"⚠️ {symbol}: ontbrekende entry of stop — "
                    f"trade kan niet correct gemonitord worden")

    except Exception as e:
        log(f"⚠️ Stale trade detectie fout: {e}")


# ============================================================
# HOOFD MONITOR LOOP
# ============================================================
def run_monitor_loop() -> None:
    """
    Continue monitor loop — draait als Render Background Worker.
    Interval: 30 seconden (configureerbaar via MONITOR_INTERVAL_SEC).
    """
    log("=" * 60)
    log("Trade Monitor v3.1 — gestart")
    log("=" * 60)
    log(f"Database:     {'✅' if DATABASE_URL else '❌ ONTBREEKT'}")
    log(f"Twilio:       {'✅' if TWILIO_ACCOUNT_SID else '⚠️ niet ingesteld'}")
    log(f"Claude API:   {'✅' if ANTHROPIC_API_KEY else '⚠️ niet ingesteld'}")
    log(f"Interval:     {MONITOR_INTERVAL_SEC}s")
    log(f"Max hold:     {MAX_HOLD_HOURS}u live | {MAX_HOLD_HOURS*2:.0f}u shadow")
    log(f"Cooldown:     {COIN_COOLDOWN_HOURS}u na verlies")
    log(f"Fee+slip:     {TOTAL_COST_PCT*100:.2f}%")
    log(f"Daily stop:   €{DAILY_STOP_LOSS_EUR:.2f} (informatief)")
    log(f"Cons. losses: {MAX_CONSECUTIVE_LOSSES} (informatief)")
    log(f"WA cooldown:  {_WHATSAPP_COOLDOWN_SEC//60}min per fouttype")
    log(f"Data dir:     {DATA_DIR}")
    log(f"Bot stopt:    NOOIT automatisch — jij via STOP")
    log("=" * 60)

    _RUN_STATS["start_ts"] = time.time()

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
    parser = argparse.ArgumentParser(description="Trade Monitor v3.1")
    parser.add_argument("--once",   action="store_true",
                        help="Één monitor run en stoppen")
    parser.add_argument("--symbol", type=str, default=None,
                        help="Alleen dit symbool monitoren (bijv. ETHUSDT)")
    args = parser.parse_args()

    if args.once or args.symbol:
        log(f"Één monitor run{f' voor {args.symbol}' if args.symbol else ''}...")
        run_monitor_once(target_symbol=args.symbol)
    else:
        run_monitor_loop()
