# whatsapp_webhook.py
# ============================================================
# Crypto AI Bot — WhatsApp Webhook v2.1
# ============================================================
# Wat dit doet:
#   - Ontvangt WhatsApp commands via Twilio
#   - START/STOP/STATUS/TRADES/RAPPORT/HELP
#   - Bot handelt 100% automatisch
#   - Stuurt 1x per dag dagrapport (Render Cron 08:00 UTC)
#   - Bot state opgeslagen in PostgreSQL
#
# WhatsApp berichten die VERSTUURD worden:
#   1. START bevestiging
#   2. STOP bevestiging
#   3. STATUS overzicht
#   4. TRADES overzicht
#   5. RAPPORT (handmatig of automatisch 08:00 UTC)
#   6. HELP overzicht
# ============================================================

from __future__ import annotations

import inspect
import json
import os
import traceback
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, request

try:
    from twilio.twiml.messaging_response import MessagingResponse
    from twilio.request_validator import RequestValidator
    TWILIO_AVAILABLE = True
except Exception:
    MessagingResponse = None
    RequestValidator = None
    TWILIO_AVAILABLE = False

app = Flask(__name__)


# ============================================================
# ENV
# ============================================================
DATABASE_URL      = (os.getenv("DATABASE_URL") or "").strip()
TRADER_MODE       = (os.getenv("TRADER_MODE") or "auto").strip().lower()
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()

TWILIO_ACCOUNT_SID   = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN    = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
TWILIO_WHATSAPP_TO   = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()

BOT_INTERNAL_SECRET = (os.getenv("BOT_INTERNAL_SECRET") or "crypto_ai_bot").strip()

# ============================================================
# FASE 1 LIMIETEN
# ============================================================
MAX_PER_TRADE_EUR            = float(os.getenv("MAX_PER_TRADE_EUR") or "0.50")
MAX_REAL_TRADES_PER_DAY      = int(os.getenv("MAX_REAL_TRADES_PER_DAY") or "10")
MAX_OPEN_REAL_TRADES         = int(os.getenv("MAX_OPEN_REAL_TRADES") or "5")
DAILY_STOP_LOSS_EUR          = float(os.getenv("DAILY_STOP_LOSS_EUR") or "5.00")
MAX_CONSECUTIVE_LOSSES       = int(os.getenv("MAX_CONSECUTIVE_LOSSES") or "3")
CONSECUTIVE_LOSS_PAUSE_HOURS = int(os.getenv("CONSECUTIVE_LOSS_PAUSE_HOURS") or "2")
TRADING_HOURS_START          = int(os.getenv("TRADING_HOURS_START") or "8")
TRADING_HOURS_END            = int(os.getenv("TRADING_HOURS_END") or "22")

BOT_STATE_TABLE = "public.bot_state"


# ============================================================
# BASIS HELPERS
# ============================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def twiml_response(text: str):
    if MessagingResponse is None:
        return (text, 200, {"Content-Type": "text/plain; charset=utf-8"})
    resp = MessagingResponse()
    resp.message(text)
    return (str(resp), 200, {"Content-Type": "application/xml"})


def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


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


def is_weekend() -> bool:
    return now_utc().weekday() >= 5


# ============================================================
# WHATSAPP VERSTUREN
# ============================================================
def send_whatsapp(message: str) -> bool:
    """
    Stuurt WhatsApp bericht via Twilio.
    Wordt ook gebruikt door trade_monitor.py
    """
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO]):
        log(f"WhatsApp (geen Twilio): {message[:80]}")
        return False
    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json",
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
# CLAUDE AI ANALYSE
# ============================================================
def claude_analyse(prompt: str, max_tokens: int = 400) -> str:
    """
    Stuurt data naar Claude API voor analyse.
    Claude heeft toegang tot alle bot data via de prompt.
    Geeft analyse terug in gewone Nederlandse taal.
    Als ANTHROPIC_API_KEY ontbreekt → lege string.
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
            return resp.json()["content"][0]["text"].strip()
        log(f"Claude API fout {resp.status_code}: {resp.text[:200]}")
        return ""
    except Exception as e:
        log(f"Claude analyse fout: {type(e).__name__}: {e}")
        return ""


def claude_analyseer_dagrapport(
    wins: int, losses: int, winrate: float, pnl: float,
    shadow_wr: float, sim_wr: float,
) -> str:
    """
    Claude analyseert het dagrapport en geeft een kort oordeel.
    Wordt toegevoegd onderaan het dagrapport.
    """
    prompt = f"""
Je bent een crypto trading bot coach. Analyseer deze dagresultaten kort.

Echte trades: {wins} wins / {losses} losses / {winrate:.1f}% winrate / €{pnl:.2f} PnL
Shadow win rate: {shadow_wr:.1f}%
Simulatie win rate: {sim_wr:.1f}%

Geef een analyse van MAX 3 zinnen in het Nederlands:
- Is dit een goede dag?
- Wat valt op?
- 1 concrete tip voor morgen

Wees direct en eerlijk. Geen wollige taal.
""".strip()

    analyse = claude_analyse(prompt, max_tokens=200)
    if analyse:
        return f"\n🧠 Claude:\n{analyse}"
    return ""


def claude_analyseer_weekrapport(data: dict) -> str:
    """
    Claude analyseert het weekrapport.
    Data = {REAL: {wins, losses, pnl}, SHADOW: {...}, SIM: {...}}
    """
    real   = data.get("REAL",   {"wins": 0, "losses": 0, "pnl": 0.0})
    shadow = data.get("SHADOW", {"wins": 0, "losses": 0, "pnl": 0.0})
    sim    = data.get("SIM",    {"wins": 0, "losses": 0, "pnl": 0.0})

    def wr(d: dict) -> float:
        t = d["wins"] + d["losses"]
        return (d["wins"] / t * 100) if t > 0 else 0.0

    prompt = f"""
Je bent een crypto trading bot coach. Analyseer deze weekresultaten.

ECHTE TRADES: {real['wins']}W / {real['losses']}L / {wr(real):.1f}% winrate / €{real['pnl']:.2f}
SHADOW:       {shadow['wins']}W / {shadow['losses']}L / {wr(shadow):.1f}% winrate
SIMULATIE:    {sim['wins']}W / {sim['losses']}L / {wr(sim):.1f}% winrate

Geef een analyse van MAX 4 zinnen in het Nederlands:
- Hoe was de week overall?
- Klopt sim/shadow met echte resultaten? (edge decay?)
- Welke trend zie je?
- 1 concrete aanbeveling voor volgende week

Wees direct en concreet.
""".strip()

    analyse = claude_analyse(prompt, max_tokens=250)
    if analyse:
        return f"\n🧠 Claude analyse:\n{analyse}"
    return ""


def claude_analyseer_maandrapport(
    real_wr: float, real_pnl: float,
    shadow_wr: float, sim_wr: float,
    trend_diff: float,
    wr_eerder: float, wr_recent: float,
) -> str:
    """Claude analyseert het maandrapport met trend data."""
    prompt = f"""
Je bent een crypto trading bot coach. Analyseer deze maandresultaten.

ECHTE TRADES: {real_wr:.1f}% winrate / €{real_pnl:.2f} PnL
SHADOW:       {shadow_wr:.1f}% winrate
SIMULATIE:    {sim_wr:.1f}% winrate

TREND (echte trades):
- Eerste 15 dagen: {wr_eerder:.1f}% winrate
- Laatste 15 dagen: {wr_recent:.1f}% winrate
- Verschil: {trend_diff:+.1f}%

Geef een analyse van MAX 5 zinnen in het Nederlands:
- Was dit een goede maand?
- Wat zegt de trend?
- Klopt sim/shadow met live? (edge decay check)
- Is de strategie nog gezond?
- 1 concrete aanbeveling voor volgende maand

Wees eerlijk en direct. Dit gaat om echt geld.
""".strip()

    analyse = claude_analyse(prompt, max_tokens=300)
    if analyse:
        return f"\n🧠 Claude maandanalyse:\n{analyse}"
    return ""


def claude_health_check(conn) -> str:
    """
    Claude controleert de gezondheid van de hele bot setup.
    Wordt 1x per week automatisch uitgevoerd (Render Cron maandag).
    Kijkt naar: config, limieten, DB tabellen, win rate trend, edge decay.
    """
    # Verzamel alle health data
    try:
        db_ok = True
        tabellen = []
        with conn.cursor() as cur:
            for tabel in ["experience_trades", "experience_scoreboard",
                          "pending_approvals", "bot_state"]:
                cur.execute(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name=%s)", (tabel,)
                )
                bestaat = bool(cur.fetchone()[0])
                tabellen.append(f"{tabel}: {'✅' if bestaat else '❌ ONTBREEKT'}")
    except Exception as e:
        db_ok = False
        tabellen = [f"DB fout: {e}"]

    # Win rate laatste 7 dagen vs 30 dagen
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COUNT(*) FILTER (
                    WHERE UPPER(outcome)='WIN'
                    AND COALESCE(exit_time,updated_at) >= NOW()-INTERVAL '7 days'
                ) AS w7,
                COUNT(*) FILTER (
                    WHERE UPPER(outcome)='LOSS'
                    AND COALESCE(exit_time,updated_at) >= NOW()-INTERVAL '7 days'
                ) AS l7,
                COUNT(*) FILTER (
                    WHERE UPPER(outcome)='WIN'
                    AND COALESCE(exit_time,updated_at) >= NOW()-INTERVAL '30 days'
                ) AS w30,
                COUNT(*) FILTER (
                    WHERE UPPER(outcome)='LOSS'
                    AND COALESCE(exit_time,updated_at) >= NOW()-INTERVAL '30 days'
                ) AS l30
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
            """)
            row = cur.fetchone()
            w7  = safe_int(row[0]) if row else 0
            l7  = safe_int(row[1]) if row else 0
            w30 = safe_int(row[2]) if row else 0
            l30 = safe_int(row[3]) if row else 0
            wr7  = (w7  / (w7 + l7)   * 100) if (w7 + l7)   > 0 else 0.0
            wr30 = (w30 / (w30 + l30) * 100) if (w30 + l30) > 0 else 0.0
    except Exception:
        wr7 = wr30 = 0.0
        w7 = l7 = w30 = l30 = 0

    # Edge decay: sim vs live
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                UPPER(COALESCE(source,'')) AS src,
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')  AS wins,
                COUNT(*) FILTER (WHERE UPPER(outcome)='LOSS') AS losses
            FROM public.experience_trades
            WHERE COALESCE(exit_time,updated_at) >= NOW()-INTERVAL '30 days'
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
            GROUP BY 1
            """)
            edge_rows = cur.fetchall()
        edge_data = {}
        for row in edge_rows:
            src  = safe_str(row[0])
            key  = "REAL" if src in ("REAL","LIVE") else src
            w    = safe_int(row[1])
            l    = safe_int(row[2])
            t    = w + l
            edge_data[key] = (w / t * 100) if t > 0 else 0.0
    except Exception:
        edge_data = {}

    sim_wr_30  = edge_data.get("SIM", 0.0)
    real_wr_30 = edge_data.get("REAL", 0.0)
    edge_diff  = sim_wr_30 - real_wr_30

    # Config check
    config_ok = []
    config_ok.append(f"DATABASE_URL:      {'✅' if DATABASE_URL else '❌'}")
    config_ok.append(f"TWILIO:            {'✅' if TWILIO_ACCOUNT_SID else '❌'}")
    config_ok.append(f"ANTHROPIC_API_KEY: {'✅' if ANTHROPIC_API_KEY else '❌'}")
    config_ok.append(f"BOT_SECRET sterk:  {'✅' if len(BOT_INTERNAL_SECRET) > 12 else '⚠️ te kort'}")
    config_ok.append(f"MAX_PER_TRADE:     €{MAX_PER_TRADE_EUR:.2f}")
    config_ok.append(f"DAILY_STOP_LOSS:   €{DAILY_STOP_LOSS_EUR:.2f}")

    prompt = f"""
Je bent een crypto trading bot health monitor.
Controleer of alles correct is ingesteld en geef een duidelijk rapport.

DATABASE TABELLEN:
{chr(10).join(tabellen)}

CONFIGURATIE:
{chr(10).join(config_ok)}

WIN RATE TREND (echte trades):
- Laatste 7 dagen:  {wr7:.1f}% ({w7}W/{l7}L)
- Laatste 30 dagen: {wr30:.1f}% ({w30}W/{l30}L)

EDGE DECAY CHECK (30 dagen):
- Simulatie win rate: {sim_wr_30:.1f}%
- Live win rate:      {real_wr_30:.1f}%
- Verschil:           {edge_diff:+.1f}% (>10% = probleem)

Geef een gezondheidsrapport in het Nederlands:
1. OVERALL STATUS: GEZOND / AANDACHT / KRITIEK
2. Wat is goed?
3. Wat heeft aandacht nodig?
4. Concrete acties (max 3 stappen)

Wees direct. Dit is een wekelijkse health check.
""".strip()

    analyse = claude_analyse(prompt, max_tokens=400)
    if not analyse:
        return "🏥 Health check: Claude API niet beschikbaar."

    return (
        f"🏥 WEKELIJKSE HEALTH CHECK\n"
        f"{'─' * 28}\n\n"
        f"📋 CONFIG:\n"
        f"{chr(10).join(config_ok)}\n\n"
        f"📊 WIN RATE TREND:\n"
        f"• 7 dagen:  {wr7:.1f}%\n"
        f"• 30 dagen: {wr30:.1f}%\n\n"
        f"⚡ EDGE DECAY:\n"
        f"• Sim: {sim_wr_30:.1f}% vs Live: {real_wr_30:.1f}%\n\n"
        f"{'─' * 28}\n"
        f"🧠 Claude oordeel:\n{analyse}"
    )


def claude_trade_leeranalyse(conn) -> str:
    """
    Claude analyseert ALLE trades en leert eruit.
    Vindt patronen, beste setups, slechtste setups.
    Geeft concrete aanbevelingen hoe de bot beter kan presteren.
    Wordt wekelijks aangeroepen.
    """
    try:
        # Top 5 beste setups
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                setup_type,
                market_regime,
                COUNT(*) AS n,
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN') AS wins,
                ROUND(AVG(COALESCE(mfe,0))::numeric, 3) AS avg_mfe,
                ROUND(AVG(COALESCE(mae,0))::numeric, 3) AS avg_mae
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE','SIM','SHADOW')
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND setup_type IS NOT NULL
            GROUP BY setup_type, market_regime
            HAVING COUNT(*) >= 5
            ORDER BY
                (COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')::float / COUNT(*)) DESC,
                COUNT(*) DESC
            LIMIT 8
            """)
            setup_rows = cur.fetchall()

        # Slechtste setups
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                setup_type,
                market_regime,
                COUNT(*) AS n,
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN') AS wins
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE','SIM','SHADOW')
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND setup_type IS NOT NULL
            GROUP BY setup_type, market_regime
            HAVING COUNT(*) >= 5
            ORDER BY
                (COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')::float / COUNT(*)) ASC
            LIMIT 5
            """)
            slechte_rows = cur.fetchall()

        # Totaal statistieken
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COUNT(*) AS totaal,
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN') AS wins,
                COUNT(*) FILTER (WHERE UPPER(outcome)='LOSS') AS losses,
                ROUND(AVG(COALESCE(time_minutes,0))::numeric, 0) AS avg_tijd_min
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE','SIM','SHADOW')
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
            """)
            totaal_row = cur.fetchone()

        totaal    = safe_int(totaal_row[0]) if totaal_row else 0
        tot_wins  = safe_int(totaal_row[1]) if totaal_row else 0
        tot_loss  = safe_int(totaal_row[2]) if totaal_row else 0
        avg_tijd  = safe_float(totaal_row[3]) if totaal_row else 0.0
        total_wr  = (tot_wins / totaal * 100) if totaal > 0 else 0.0

        # Formatteer setup data
        beste_text = ""
        for row in setup_rows:
            setup   = safe_str(row[0], "-")
            regime  = safe_str(row[1], "-")
            n       = safe_int(row[2])
            wins    = safe_int(row[3])
            wr      = (wins / n * 100) if n > 0 else 0.0
            mfe     = safe_float(row[4])
            mae     = safe_float(row[5])
            beste_text += f"  {setup}/{regime}: {wr:.0f}% ({n} trades) MFE={mfe} MAE={mae}\n"

        slechte_text = ""
        for row in slechte_rows:
            setup  = safe_str(row[0], "-")
            regime = safe_str(row[1], "-")
            n      = safe_int(row[2])
            wins   = safe_int(row[3])
            wr     = (wins / n * 100) if n > 0 else 0.0
            slechte_text += f"  {setup}/{regime}: {wr:.0f}% ({n} trades)\n"

    except Exception as e:
        return f"Leeranalyse fout: {type(e).__name__}: {e}"

    prompt = f"""
Je bent een crypto trading bot optimalisatie coach.
Analyseer deze trade data en geef concrete verbeteringen.

TOTAAL: {totaal} trades | {total_wr:.1f}% winrate | gem. {avg_tijd:.0f} min per trade

BESTE SETUPS (hoogste winrate):
{beste_text or "  Geen data"}

SLECHTSTE SETUPS (laagste winrate):
{slechte_text or "  Geen data"}

Geef een leerrapport in het Nederlands met:
1. WAT WERKT: Welke setup/regime combinaties zijn de echte edge?
2. WAT NIET WERKT: Wat moet de bot vermijden of minder doen?
3. SCORE DREMPEL: Op basis van MFE/MAE, moet de score drempel omhoog of omlaag?
4. CONCRETE PARAMETERS: 3 specifieke aanpassingen die de winrate verhogen
5. PRIORITEIT: Wat is de #1 aanpassing met de grootste impact?

Wees zeer concreet. Geef percentages en specifieke setup namen.
""".strip()

    analyse = claude_analyse(prompt, max_tokens=500)
    if not analyse:
        return "Leeranalyse: Claude API niet beschikbaar."

    return (
        f"🎓 TRADE LEERANALYSE\n"
        f"{'─' * 28}\n\n"
        f"📊 Totaal: {totaal} trades | {total_wr:.1f}% WR\n"
        f"⏱️ Gem. duur: {avg_tijd:.0f} min\n\n"
        f"{'─' * 28}\n"
        f"🧠 Claude leert:\n{analyse}"
    )


# ============================================================
# BOT STATE — POSTGRESQL
# ============================================================
def ensure_bot_state_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {BOT_STATE_TABLE} (
            key        TEXT PRIMARY KEY,
            value      TEXT,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)
    conn.commit()


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
    with conn.cursor() as cur:
        cur.execute(f"""
        INSERT INTO {BOT_STATE_TABLE}(key, value, updated_at)
        VALUES(%s, %s, NOW())
        ON CONFLICT(key) DO UPDATE
            SET value=EXCLUDED.value, updated_at=NOW()
        """, (key, value))
    conn.commit()


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
        if now_utc() > until:
            set_bot_state(conn, "bot_paused", "false")
            set_bot_state(conn, "bot_paused_until", "")
            return False
        return True
    except Exception:
        return True


def pause_bot(conn, hours: float, reason: str) -> None:
    until = now_utc() + timedelta(hours=hours)
    set_bot_state(conn, "bot_paused", "true")
    set_bot_state(conn, "bot_paused_until", until.isoformat())
    set_bot_state(conn, "bot_paused_reason", reason)
    log(f"Bot gepauzeerd tot {until.strftime('%H:%M UTC')} — {reason}")


# ============================================================
# DATA OPHALEN
# ============================================================
def get_real_trades_today(conn) -> int:
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COUNT(*) FROM public.pending_approvals
            WHERE status = 'CONSUMED'
              AND DATE(consumed_at AT TIME ZONE 'UTC') = %s
            """, (utc_day_str(),))
            row = cur.fetchone()
            return safe_int(row[0]) if row else 0
    except Exception:
        return 0


def get_open_real_trades_count(conn) -> int:
    """Telt open echte trades. Fallback naar live_state.json."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COUNT(*) FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND TRIM(UPPER(COALESCE(outcome,''))) IN ('OPEN','UNKNOWN','')
            """)
            row = cur.fetchone()
            count = safe_int(row[0]) if row else 0
            if count > 0:
                return count
    except Exception:
        pass

    # Fallback: live_state.json
    try:
        data_dir   = (os.getenv("DATA_DIR") or "").strip() or (
            "/data" if os.path.isdir("/data") else "/tmp/data"
        )
        state_path = os.path.join(data_dir, "live_state.json")
        if os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            return len(state.get("open_trades") or [])
    except Exception:
        pass
    return 0


def get_daily_pnl(conn, day: str) -> Tuple[int, int, float]:
    """Geeft (wins, losses, pnl_eur) terug voor een dag."""
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


def get_shadow_daily(conn, day: str) -> Tuple[int, int]:
    """Shadow trades wins + losses voor een dag."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')  AS wins,
                COUNT(*) FILTER (WHERE UPPER(outcome)='LOSS') AS losses
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) = 'SHADOW'
              AND DATE(COALESCE(exit_time, updated_at) AT TIME ZONE 'UTC') = %s
            """, (day,))
            row = cur.fetchone()
            if row:
                return safe_int(row[0]), safe_int(row[1])
    except Exception:
        pass
    return 0, 0


def get_sim_daily(conn, day: str) -> Tuple[int, int]:
    """Simulatie trades wins + losses voor een dag."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')  AS wins,
                COUNT(*) FILTER (WHERE UPPER(outcome)='LOSS') AS losses
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) = 'SIM'
              AND DATE(COALESCE(exit_time, updated_at) AT TIME ZONE 'UTC') = %s
            """, (day,))
            row = cur.fetchone()
            if row:
                return safe_int(row[0]), safe_int(row[1])
    except Exception:
        pass
    return 0, 0


def get_bot_status_line(conn) -> str:
    """Geeft bot status regel terug voor in elk bericht."""
    if not is_bot_active(conn):
        return "🔴 Bot: GESTOPT"
    elif is_bot_paused(conn):
        return "⏸️ Bot: GEPAUZEERD"
    return "🟢 Bot: ACTIEF"


def get_consecutive_losses(conn) -> int:
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


# ============================================================
# LIMIETEN CHECK
# ============================================================
def check_trading_limits(conn) -> Tuple[bool, str]:
    if not is_bot_active(conn):
        return False, "Bot GESTOPT"

    if is_bot_paused(conn):
        reason = get_bot_state(conn, "bot_paused_reason", "onbekend")
        until  = get_bot_state(conn, "bot_paused_until", "")
        return False, f"Bot GEPAUZEERD: {reason} (tot {until})"

    if not is_trading_hours():
        return False, f"Buiten hours ({TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC)"
    # Weekend: gewoon doorgaan — geen blokkering

    _, _, daily_pnl = get_daily_pnl(conn, utc_day_str())
    if daily_pnl <= -DAILY_STOP_LOSS_EUR:
        # Bot loopt gewoon door — jij beslist via STOP
        log(f"ℹ️ Dagbudget bereikt: €{daily_pnl:.2f} — bot gaat door")

    trades_today = get_real_trades_today(conn)
    if trades_today >= MAX_REAL_TRADES_PER_DAY:
        return False, f"Daglimiet: {trades_today}/{MAX_REAL_TRADES_PER_DAY}"

    open_trades = get_open_real_trades_count(conn)
    if open_trades >= MAX_OPEN_REAL_TRADES:
        return False, f"Max open: {open_trades}/{MAX_OPEN_REAL_TRADES}"

    consecutive = get_consecutive_losses(conn)
    if consecutive >= MAX_CONSECUTIVE_LOSSES:
        # Bot loopt gewoon door — alleen informatief loggen
        # Jij beslist zelf via STOP command
        log(f"ℹ️ {consecutive}x verlies op rij — bot gaat door (jij beslist via STOP)")

    return True, "OK"


# ============================================================
# TWILIO AUTHENTICATIE
# ============================================================
def verify_twilio_signature() -> bool:
    if not TWILIO_AVAILABLE or not TWILIO_AUTH_TOKEN:
        log("⚠️ Twilio auth overgeslagen (dev mode)")
        return True
    try:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        return validator.validate(
            request.url,
            request.form.to_dict(),
            request.headers.get("X-Twilio-Signature", ""),
        )
    except Exception as e:
        log(f"Twilio verificatie fout: {e}")
        return False


# ============================================================
# AUTO BUY
# ============================================================
def _get_buy_fn(mode: str):
    module_path = "trading.live_trader" if mode == "live" else "trading.paper_trader"
    mod = __import__(module_path, fromlist=["buy_eur"])
    fn  = getattr(mod, "buy_eur", None)
    if not callable(fn):
        raise AttributeError(f"buy_eur niet in {module_path}")
    return fn


def _call_buy(buy_fn, symbol: str, amount_eur: float, meta: Dict[str, Any]) -> Any:
    try:
        sig    = inspect.signature(buy_fn)
        kwargs = {"meta": meta} if "meta" in sig.parameters else {}
        return buy_fn(symbol, amount_eur, **kwargs)
    except Exception:
        return buy_fn(symbol, amount_eur)


def execute_auto_buy(prebuy: Dict[str, Any]) -> Tuple[bool, str, str]:
    symbol = safe_str(prebuy.get("symbol"))
    if not symbol:
        return False, "Symbol ontbreekt", ""

    meta = {
        "prebuy_id":     safe_str(prebuy.get("id")),
        "entry":         safe_float(prebuy.get("entry")),
        "stop":          safe_float(prebuy.get("stop")),
        "stop_loss":     safe_float(prebuy.get("stop")),
        "target":        safe_float(prebuy.get("target")),
        "setup_type":    safe_str(prebuy.get("setup_type"), "UNKNOWN"),
        "regime":        safe_str(prebuy.get("regime"), "UNKNOWN"),
        "timeframe":     safe_str(prebuy.get("timeframe"), "4h"),
        "score":         safe_int(prebuy.get("score")),
        "raw_score":     safe_int(prebuy.get("score")),
        "chance":        safe_int(prebuy.get("chance")),
        "confidence":    safe_int(prebuy.get("confidence")),
        "label":         "GO",
        "amount_eur":    MAX_PER_TRADE_EUR,
        "user_decision": "AUTO",
        "bot_decision":  "AUTO_BUY",
    }

    modes = (
        ["live"]         if TRADER_MODE == "live"  else
        ["paper"]        if TRADER_MODE == "paper" else
        ["live", "paper"]
    )

    for mode in modes:
        try:
            buy_fn = _get_buy_fn(mode)
            result = _call_buy(buy_fn, symbol, MAX_PER_TRADE_EUR, meta)
            if isinstance(result, dict):
                if result.get("ok") is True:
                    price = safe_float(result.get("price"), meta["entry"])
                    return True, f"{symbol} €{MAX_PER_TRADE_EUR:.2f} @ {price:.6f}", mode
                log(f"BUY {mode} niet ok: {result}")
            else:
                return True, f"{symbol} €{MAX_PER_TRADE_EUR:.2f}", mode
        except Exception as e:
            log(f"BUY {mode} fout: {type(e).__name__}: {e}")

    return False, f"BUY mislukt voor {symbol}", ""


def get_pending_by_id(conn, prebuy_id: str) -> Optional[Dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
        SELECT id, symbol, setup_type, regime, score, chance, confidence,
               entry, stop, target, status, created_at, expires_at,
               timeframe, bitvavo_market
        FROM public.pending_approvals WHERE id=%s LIMIT 1
        """, (prebuy_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def mark_consumed(conn, prebuy_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""
        UPDATE public.pending_approvals
        SET status='CONSUMED', consumed_at=NOW()
        WHERE id=%s
        """, (prebuy_id,))
    conn.commit()


def process_auto_buy(conn, prebuy: Dict[str, Any]) -> None:
    symbol    = safe_str(prebuy.get("symbol"))
    prebuy_id = safe_str(prebuy.get("id"))

    mag, reden = check_trading_limits(conn)
    if not mag:
        log(f"Auto BUY geblokkeerd ({symbol}): {reden}")
        return

    ok, bericht, mode = execute_auto_buy(prebuy)
    if ok:
        mark_consumed(conn, prebuy_id)
        log(f"✅ Auto BUY: {bericht} via {mode}")
    else:
        log(f"❌ Auto BUY mislukt: {bericht}")


# ============================================================
# RAPPORT
# ============================================================
def generate_daily_rapport(conn) -> str:
    """
    Dagrapport:
    - Echte trades: wins / losses / win% / PnL
    - Shadow trades: wins / losses (leerdata)

    Als vandaag nog geen trades = toon gisteren automatisch.
    """
    today    = utc_day_str()
    gisteren = utc_day_str(-1)

    wins_v, losses_v, _ = get_daily_pnl(conn, today)
    if wins_v + losses_v == 0:
        rapport_dag = gisteren
        dag_label   = f"Gisteren ({gisteren})"
    else:
        rapport_dag = today
        dag_label   = f"Vandaag ({today})"

    wins, losses, pnl          = get_daily_pnl(conn, rapport_dag)
    shadow_wins, shadow_losses = get_shadow_daily(conn, rapport_dag)
    sim_wins, sim_losses       = get_sim_daily(conn, rapport_dag)
    bot_lijn                   = get_bot_status_line(conn)

    total        = wins + losses
    winrate      = (wins / total * 100)          if total > 0        else 0.0
    shadow_total = shadow_wins + shadow_losses
    shadow_wr    = (shadow_wins / shadow_total * 100) if shadow_total > 0 else 0.0
    sim_total    = sim_wins + sim_losses
    sim_wr       = (sim_wins / sim_total * 100)  if sim_total > 0    else 0.0
    pnl_teken    = "+" if pnl >= 0 else ""

    claude = claude_analyseer_dagrapport(
        wins, losses, winrate, pnl, shadow_wr, sim_wr
    )

    return (
        f"📊 DAGRAPPORT — {dag_label}\n"
        f"{bot_lijn}\n"
        f"{'─' * 28}\n\n"
        f"💶 ECHTE TRADES:\n"
        f"✅ Goed:     {wins} trades\n"
        f"❌ Fout:     {losses} trades\n"
        f"📈 Win rate: {winrate:.1f}%\n"
        f"💰 Resultaat: {pnl_teken}€{pnl:.2f}\n\n"
        f"🎭 SHADOW (leerdata):\n"
        f"✅ Goed:     {shadow_wins} trades\n"
        f"❌ Fout:     {shadow_losses} trades\n"
        f"📈 Win rate: {shadow_wr:.1f}%\n\n"
        f"🔮 SIMULATIE:\n"
        f"✅ Goed:     {sim_wins} trades\n"
        f"❌ Fout:     {sim_losses} trades\n"
        f"📈 Win rate: {sim_wr:.1f}%"
        f"{claude}"
    )


# ============================================================
# STATUS FORMATERING
# ============================================================
def fmt_status(conn) -> str:
    bot_active   = is_bot_active(conn)
    bot_paused   = is_bot_paused(conn)
    pause_reason = get_bot_state(conn, "bot_paused_reason", "")
    pause_until  = get_bot_state(conn, "bot_paused_until", "")

    if not bot_active:
        bot_status = "🔴 GESTOPT"
    elif bot_paused:
        bot_status = f"⏸️ GEPAUZEERD\n  Reden: {pause_reason}\n  Tot:   {pause_until}"
    else:
        bot_status = "🟢 ACTIEF"

    trades_today = get_real_trades_today(conn)
    open_trades  = get_open_real_trades_count(conn)
    consecutive  = get_consecutive_losses(conn)
    _, _, pnl    = get_daily_pnl(conn, utc_day_str())

    return (
        f"🤖 BOT STATUS\n"
        f"{'─' * 25}\n\n"
        f"Status: {bot_status}\n\n"
        f"📊 VANDAAG:\n"
        f"• Trades:  {trades_today}/{MAX_REAL_TRADES_PER_DAY}\n"
        f"• Open:    {open_trades}/{MAX_OPEN_REAL_TRADES}\n"
        f"• PnL:     €{pnl:.2f}\n"
        f"• Budget:  €{max(0.0, DAILY_STOP_LOSS_EUR - abs(pnl)):.2f} over\n\n"
        f"⏰ CONDITIES:\n"
        f"• Trading hours: {'✅' if is_trading_hours() else '❌'} ({TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00)\n"
        f"• Verlies rij:   {consecutive}/{MAX_CONSECUTIVE_LOSSES}\n\n"
        f"Commands: START | STOP | TRADES | RAPPORT | HELP"
    )


HELP_TEXT = (
    f"🤖 CRYPTO AI BOT\n"
    f"{'─' * 25}\n\n"
    f"AAN/UIT:\n"
    f"• START       → bot begint traden\n"
    f"• STOP        → bot stopt\n\n"
    f"INFO:\n"
    f"• STATUS      → bot status\n"
    f"• TRADES      → open trades\n"
    f"• RAPPORT     → dagrapport\n"
    f"• WEEKRAPPORT → weekoverzicht\n"
    f"• MAANDRAPPORT → maandoverzicht\n"
    f"• HELP        → dit overzicht\n\n"
    f"AUTOMATISCH:\n"
    f"• Dagelijks   08:00 UTC\n"
    f"• Wekelijks   maandag 08:00 UTC\n"
    f"• Maandelijks 1e v/d maand 08:00 UTC\n\n"
    f"LIMIETEN:\n"
    f"• €{MAX_PER_TRADE_EUR:.2f} per trade\n"
    f"• Max {MAX_REAL_TRADES_PER_DAY} trades/dag\n"
    f"• Max {MAX_OPEN_REAL_TRADES} open tegelijk\n"
    f"• Stop bij €{DAILY_STOP_LOSS_EUR:.2f} verlies/dag\n"
    f"• Pauze na {MAX_CONSECUTIVE_LOSSES}x verlies\n"
    f"• {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC\n\n"
    f"🧠 Claude AI analyseert elk rapport.\n"
    f"Bot handelt volledig automatisch.\n"
    f"7 dagen per week — ook weekend."
)


# ============================================================
# FLASK ROUTES
# ============================================================
@app.get("/")
def root():
    return "Crypto AI Bot — Webhook actief", 200


@app.get("/healthz")
def healthz():
    return "OK", 200


@app.get("/health")
def health_check():
    status = {
        "ok":            True,
        "timestamp":     now_utc().isoformat(),
        "database":      False,
        "twilio":        bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN),
        "claude_api":    bool(ANTHROPIC_API_KEY),
        "trading_hours": is_trading_hours(),
        "weekend":       is_weekend(),
        "trader_mode":   TRADER_MODE,
    }
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            status["database"]     = True
            status["bot_active"]   = is_bot_active(conn)
            status["bot_paused"]   = is_bot_paused(conn)
            status["trades_today"] = get_real_trades_today(conn)
            status["open_trades"]  = get_open_real_trades_count(conn)
            _, _, pnl = get_daily_pnl(conn, utc_day_str())
            status["daily_pnl"]    = pnl
    except Exception as e:
        status["ok"]       = False
        status["db_error"] = str(e)
    return status, 200


@app.post("/whatsapp")
def whatsapp():
    """Hoofdroute — ontvangt WhatsApp commands."""
    try:
        if not verify_twilio_signature():
            log("⚠️ Twilio signature mislukt")
            return ("Unauthorized", 403)

        body = (request.values.get("Body") or "").strip()
        if not body:
            return twiml_response("Stuur een command. Typ HELP.")

        parts = body.split()
        cmd   = parts[0].upper() if parts else "HELP"

        if not DATABASE_URL:
            return twiml_response("❌ DATABASE_URL ontbreekt.")

        with db_connect() as conn:
            ensure_bot_state_table(conn)

            # ── START ──────────────────────────────────────
            if cmd == "START":
                set_bot_state(conn, "bot_active",       "true")
                set_bot_state(conn, "bot_paused",       "false")
                set_bot_state(conn, "bot_paused_until", "")
                set_bot_state(conn, "bot_paused_reason","")

                trades_today = get_real_trades_today(conn)
                open_trades  = get_open_real_trades_count(conn)

                return twiml_response(
                    f"{get_bot_status_line(conn)}\n"
                    f"🟢 BOT GESTART\n"
                    f"{'─' * 25}\n\n"
                    f"Bot handelt automatisch.\n"
                    f"Bedrag: €{MAX_PER_TRADE_EUR:.2f} per trade\n"
                    f"Hours:  {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC\n\n"
                    f"Vandaag: {trades_today}/{MAX_REAL_TRADES_PER_DAY} trades\n"
                    f"Open:    {open_trades}/{MAX_OPEN_REAL_TRADES}\n\n"
                    f"Stuur STOP om te stoppen."
                )

            # ── STOP ───────────────────────────────────────
            if cmd == "STOP":
                set_bot_state(conn, "bot_active", "false")
                open_trades = get_open_real_trades_count(conn)

                return twiml_response(
                    f"{get_bot_status_line(conn)}\n"
                    f"🔴 BOT GESTOPT\n"
                    f"{'─' * 25}\n\n"
                    f"Geen nieuwe trades.\n"
                    f"Open trades bewaakt: {open_trades}\n\n"
                    f"Stuur START om te hervatten."
                )

            # ── STATUS ─────────────────────────────────────
            if cmd == "STATUS":
                return twiml_response(fmt_status(conn))

            # ── TRADES ─────────────────────────────────────
            if cmd == "TRADES":
                try:
                    with conn.cursor(
                        cursor_factory=psycopg2.extras.RealDictCursor
                    ) as cur:
                        cur.execute("""
                        SELECT coin, entry, stop, target, entry_time
                        FROM public.experience_trades
                        WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
                          AND TRIM(UPPER(COALESCE(outcome,'')))
                              IN ('OPEN','UNKNOWN','')
                        ORDER BY entry_time DESC
                        LIMIT 5
                        """)
                        rows = cur.fetchall()

                    if not rows:
                        return twiml_response(
                            f"{get_bot_status_line(conn)}\n"
                            f"📂 OPEN TRADES\n"
                            f"{'─' * 25}\n\n"
                            f"Geen open trades.\n"
                            f"Bot scant de markt."
                        )

                    lines = [f"{get_bot_status_line(conn)}\n📂 OPEN TRADES ({len(rows)})\n{'─' * 25}"]
                    for r in rows:
                        coin   = safe_str(r.get("coin"), "-")
                        entry  = safe_float(r.get("entry"))
                        stop   = safe_float(r.get("stop"))
                        target = safe_float(r.get("target"))
                        lines.append(
                            f"\n• {coin}\n"
                            f"  Entry:  {entry:.6f}\n"
                            f"  Stop:   {stop:.6f}\n"
                            f"  Target: {target:.6f}"
                        )
                    return twiml_response("\n".join(lines))

                except Exception as e:
                    return twiml_response(
                        f"Trades ophalen mislukt:\n{type(e).__name__}"
                    )

            # ── RAPPORT ────────────────────────────────────
            if cmd == "RAPPORT":
                return twiml_response(generate_daily_rapport(conn))

            # ── WEEKRAPPORT ────────────────────────────────
            if cmd == "WEEKRAPPORT":
                return twiml_response(generate_weekly_rapport(conn))

            # ── MAANDRAPPORT ───────────────────────────────
            if cmd == "MAANDRAPPORT":
                return twiml_response(generate_monthly_rapport(conn))

            # ── ADVIES ─────────────────────────────────────
            if cmd == "ADVIES":
                # Handmatig Claude leeranalyse opvragen
                return twiml_response(claude_trade_leeranalyse(conn))

            # ── HEALTH ─────────────────────────────────────
            if cmd == "HEALTH":
                # Handmatig health check opvragen
                return twiml_response(claude_health_check(conn))

            # ── HELP ───────────────────────────────────────
            if cmd in ("HELP", "?"):
                return twiml_response(HELP_TEXT)

            # ── Onbekend ───────────────────────────────────
            return twiml_response(f"Onbekend: {cmd}\n\nStuur HELP.")

    except Exception as e:
        log(f"❌ ERROR /whatsapp: {type(e).__name__}: {e}")
        log(traceback.format_exc())
        return twiml_response(
            "⚠️ Interne fout.\n"
            "Open trades worden bewaakt."
        )


# ============================================================
# AUTO BUY ROUTE — multi_coin_score.py roept dit aan
# ============================================================
@app.post("/auto_buy")
def auto_buy_route():
    try:
        if request.headers.get("X-Bot-Auth", "") != BOT_INTERNAL_SECRET:
            return {"ok": False, "error": "Unauthorized"}, 403

        data      = request.get_json(force=True) or {}
        prebuy_id = safe_str(data.get("prebuy_id"))
        if not prebuy_id:
            return {"ok": False, "error": "prebuy_id ontbreekt"}, 400

        with db_connect() as conn:
            ensure_bot_state_table(conn)
            prebuy = get_pending_by_id(conn, prebuy_id)
            if not prebuy:
                return {"ok": False, "error": "prebuy niet gevonden"}, 404
            process_auto_buy(conn, prebuy)
            return {"ok": True, "prebuy_id": prebuy_id}

    except Exception as e:
        log(f"Auto buy route fout: {e}")
        return {"ok": False, "error": str(e)}, 500


# ============================================================
# DAGRAPPORT ROUTE — Render Cron 08:00 UTC elke dag
# ============================================================
@app.post("/send_daily_rapport")
def send_daily_rapport_route():
    """
    Render Cron instelling:
    Schedule: 0 8 * * *
    Command:  curl -X POST -H "X-Bot-Auth: <secret>" https://jouw-app.onrender.com/send_daily_rapport
    """
    try:
        if request.headers.get("X-Bot-Auth", "") != BOT_INTERNAL_SECRET:
            return {"ok": False, "error": "Unauthorized"}, 403

        with db_connect() as conn:
            rapport = generate_daily_rapport(conn)

        ok = send_whatsapp(rapport)
        log(f"Dagrapport verstuurd: {'✅' if ok else '❌'}")
        return {"ok": ok, "rapport": rapport}

    except Exception as e:
        log(f"Dagrapport fout: {e}")
        return {"ok": False, "error": str(e)}, 500


# ============================================================
# WEEKRAPPORT ROUTE — Render Cron maandag 08:00 UTC
# ============================================================
def generate_weekly_rapport(conn) -> str:
    """
    Weekrapport — afgelopen 7 dagen.
    Echte trades + shadow + sim.
    """
    today     = now_utc()
    week_start = today - timedelta(days=7)
    week_label = f"{week_start.strftime('%d %b')} – {today.strftime('%d %b %Y')}"

    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                UPPER(COALESCE(source,'')) AS src,
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
            WHERE COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '7 days'
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
            GROUP BY 1
            """)
            rows = cur.fetchall()

        data = {}
        for row in rows:
            src  = safe_str(row[0])
            wins = safe_int(row[1])
            loss = safe_int(row[2])
            pnl  = safe_float(row[3])
            # Groepeer REAL + LIVE samen
            key = "REAL" if src in ("REAL", "LIVE") else src
            if key not in data:
                data[key] = {"wins": 0, "losses": 0, "pnl": 0.0}
            data[key]["wins"]   += wins
            data[key]["losses"] += loss
            data[key]["pnl"]    += pnl

        def blok(label: str, emoji: str, key: str) -> str:
            d     = data.get(key, {"wins": 0, "losses": 0, "pnl": 0.0})
            w     = d["wins"]
            l     = d["losses"]
            p     = d["pnl"]
            total = w + l
            wr    = (w / total * 100) if total > 0 else 0.0
            sign  = "+" if p >= 0 else ""
            return (
                f"{emoji} {label}:\n"
                f"✅ Goed:     {w} | ❌ Fout: {l}\n"
                f"📈 Win rate: {wr:.1f}%\n"
                f"💰 Resultaat: {sign}€{p:.2f}"
            )

        claude = claude_analyseer_weekrapport(data)
        bot_lijn = get_bot_status_line(conn)

        return (
            f"📅 WEEKRAPPORT\n"
            f"{bot_lijn}\n"
            f"{week_label}\n"
            f"{'─' * 28}\n\n"
            f"{blok('ECHTE TRADES', '💶', 'REAL')}\n\n"
            f"{blok('SHADOW', '🎭', 'SHADOW')}\n\n"
            f"{blok('SIMULATIE', '🔮', 'SIM')}"
            f"{claude}"
        )

    except Exception as e:
        return f"Weekrapport fout: {type(e).__name__}: {e}"


@app.post("/send_weekly_rapport")
def send_weekly_rapport_route():
    """
    Render Cron instelling:
    Schedule: 0 8 * * 1   (elke maandag 08:00 UTC)
    Command:  curl -X POST -H "X-Bot-Auth: <secret>" https://jouw-app.onrender.com/send_weekly_rapport
    """
    try:
        if request.headers.get("X-Bot-Auth", "") != BOT_INTERNAL_SECRET:
            return {"ok": False, "error": "Unauthorized"}, 403
        with db_connect() as conn:
            rapport = generate_weekly_rapport(conn)
        ok = send_whatsapp(rapport)
        log(f"Weekrapport verstuurd: {'✅' if ok else '❌'}")
        return {"ok": ok, "rapport": rapport}
    except Exception as e:
        log(f"Weekrapport fout: {e}")
        return {"ok": False, "error": str(e)}, 500


@app.post("/send_monthly_rapport")
def send_monthly_rapport_route():
    """
    Render Cron instelling:
    Schedule: 0 8 1 * *   (1e van elke maand 08:00 UTC)
    Command:  curl -X POST -H "X-Bot-Auth: <secret>" https://jouw-app.onrender.com/send_monthly_rapport
    """
    try:
        if request.headers.get("X-Bot-Auth", "") != BOT_INTERNAL_SECRET:
            return {"ok": False, "error": "Unauthorized"}, 403
        with db_connect() as conn:
            rapport = generate_monthly_rapport(conn)
        ok = send_whatsapp(rapport)
        log(f"Maandrapport verstuurd: {'✅' if ok else '❌'}")
        return {"ok": ok, "rapport": rapport}
    except Exception as e:
        log(f"Maandrapport fout: {e}")
        return {"ok": False, "error": str(e)}, 500


# ============================================================
# HEALTH CHECK ROUTE — Render Cron maandag 09:00 UTC
# ============================================================
@app.post("/send_health_check")
def send_health_check_route():
    """
    Claude controleert wekelijks de volledige bot setup.
    Kijkt naar: config, DB tabellen, win rate trend, edge decay.

    Render Cron instelling:
    Schedule: 0 9 * * 1   (elke maandag 09:00 UTC — na weekrapport)
    Command:  curl -X POST -H "X-Bot-Auth: <secret>" https://jouw-app.onrender.com/send_health_check
    """
    try:
        if request.headers.get("X-Bot-Auth", "") != BOT_INTERNAL_SECRET:
            return {"ok": False, "error": "Unauthorized"}, 403

        with db_connect() as conn:
            health = claude_health_check(conn)

        ok = send_whatsapp(health)
        log(f"Health check verstuurd: {'✅' if ok else '❌'}")
        return {"ok": ok, "health": health}

    except Exception as e:
        log(f"Health check fout: {e}")
        return {"ok": False, "error": str(e)}, 500


# ============================================================
# LEERANALYSE ROUTE — Render Cron elke 2 weken zondag 08:00 UTC
# ============================================================
@app.post("/send_leeranalyse")
def send_leeranalyse_route():
    """
    Claude analyseert alle trades en geeft verbeteradvies.
    Vindt patronen, beste/slechtste setups, concrete aanpassingen.

    Render Cron instelling:
    Schedule: 0 8 1,15 * *   (1e en 15e van de maand 08:00 UTC)
    Command:  curl -X POST -H "X-Bot-Auth: <secret>" https://jouw-app.onrender.com/send_leeranalyse
    """
    try:
        if request.headers.get("X-Bot-Auth", "") != BOT_INTERNAL_SECRET:
            return {"ok": False, "error": "Unauthorized"}, 403

        with db_connect() as conn:
            analyse = claude_trade_leeranalyse(conn)

        ok = send_whatsapp(analyse)
        log(f"Leeranalyse verstuurd: {'✅' if ok else '❌'}")
        return {"ok": ok, "analyse": analyse}

    except Exception as e:
        log(f"Leeranalyse fout: {e}")
        return {"ok": False, "error": str(e)}, 500
def generate_monthly_rapport(conn) -> str:
    """
    Maandrapport — afgelopen 30 dagen.
    Echte trades + shadow + sim + trend.
    """
    today      = now_utc()
    maand_naam = today.strftime("%B %Y")

    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                UPPER(COALESCE(source,'')) AS src,
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
            WHERE COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '30 days'
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
            GROUP BY 1
            """)
            rows = cur.fetchall()

        data = {}
        for row in rows:
            src  = safe_str(row[0])
            wins = safe_int(row[1])
            loss = safe_int(row[2])
            pnl  = safe_float(row[3])
            key  = "REAL" if src in ("REAL", "LIVE") else src
            if key not in data:
                data[key] = {"wins": 0, "losses": 0, "pnl": 0.0}
            data[key]["wins"]   += wins
            data[key]["losses"] += loss
            data[key]["pnl"]    += pnl

        # Win rate trend: vergelijk eerste 15 vs laatste 15 dagen
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                CASE
                    WHEN COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '15 days'
                    THEN 'recent'
                    ELSE 'eerder'
                END AS periode,
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')  AS wins,
                COUNT(*) FILTER (WHERE UPPER(outcome)='LOSS') AS losses
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '30 days'
            GROUP BY 1
            """)
            trend_rows = cur.fetchall()

        trend_data = {}
        for row in trend_rows:
            periode = safe_str(row[0])
            w = safe_int(row[1])
            l = safe_int(row[2])
            trend_data[periode] = {"wins": w, "losses": l}

        def wr(d: dict) -> float:
            t = d.get("wins", 0) + d.get("losses", 0)
            return (d.get("wins", 0) / t * 100) if t > 0 else 0.0

        wr_eerder  = wr(trend_data.get("eerder", {}))
        wr_recent  = wr(trend_data.get("recent", {}))
        trend_diff = wr_recent - wr_eerder

        if trend_diff > 5:
            trend_lijn = f"📈 Trend: STIJGEND (+{trend_diff:.1f}%)"
        elif trend_diff < -5:
            trend_lijn = f"📉 Trend: DALEND ({trend_diff:.1f}%)"
        else:
            trend_lijn = f"➡️ Trend: STABIEL ({trend_diff:+.1f}%)"

        def blok(label: str, emoji: str, key: str) -> str:
            d     = data.get(key, {"wins": 0, "losses": 0, "pnl": 0.0})
            w     = d["wins"]
            l     = d["losses"]
            p     = d["pnl"]
            total = w + l
            rate  = (w / total * 100) if total > 0 else 0.0
            sign  = "+" if p >= 0 else ""
            return (
                f"{emoji} {label}:\n"
                f"✅ Goed:     {w} | ❌ Fout: {l}\n"
                f"📈 Win rate: {rate:.1f}%\n"
                f"💰 Resultaat: {sign}€{p:.2f}"
            )

        real_d   = data.get("REAL",   {"wins": 0, "losses": 0, "pnl": 0.0})
        shadow_d = data.get("SHADOW", {"wins": 0, "losses": 0, "pnl": 0.0})
        sim_d    = data.get("SIM",    {"wins": 0, "losses": 0, "pnl": 0.0})

        def _wr(d: dict) -> float:
            t = d["wins"] + d["losses"]
            return (d["wins"] / t * 100) if t > 0 else 0.0

        claude = claude_analyseer_maandrapport(
            real_wr   = _wr(real_d),
            real_pnl  = real_d["pnl"],
            shadow_wr = _wr(shadow_d),
            sim_wr    = _wr(sim_d),
            trend_diff = trend_diff,
            wr_eerder  = wr_eerder,
            wr_recent  = wr_recent,
        )

        bot_lijn = get_bot_status_line(conn)

        return (
            f"📆 MAANDRAPPORT — {maand_naam}\n"
            f"{bot_lijn}\n"
            f"Afgelopen 30 dagen\n"
            f"{'─' * 28}\n\n"
            f"{blok('ECHTE TRADES', '💶', 'REAL')}\n\n"
            f"{blok('SHADOW', '🎭', 'SHADOW')}\n\n"
            f"{blok('SIMULATIE', '🔮', 'SIM')}\n\n"
            f"{'─' * 28}\n"
            f"{trend_lijn}\n"
            f"Eerste 15d: {wr_eerder:.1f}%\n"
            f"Laatste 15d: {wr_recent:.1f}%"
            f"{claude}"
        )

    except Exception as e:
        return f"Maandrapport fout: {type(e).__name__}: {e}"


@app.post("/send_monthly_rapport")
def send_monthly_rapport_route():
    """
    Render Cron instelling:
    Schedule: 0 8 1 * *   (1e van elke maand 08:00 UTC)
    Command:  curl -X POST -H "X-Bot-Auth: <secret>" https://jouw-app.onrender.com/send_monthly_rapport
    """
    try:
        if request.headers.get("X-Bot-Auth", "") != BOT_INTERNAL_SECRET:
            return {"ok": False, "error": "Unauthorized"}, 403

        with db_connect() as conn:
            rapport = generate_monthly_rapport(conn)

        ok = send_whatsapp(rapport)
        log(f"Maandrapport verstuurd: {'✅' if ok else '❌'}")
        return {"ok": ok, "rapport": rapport}

    except Exception as e:
        log(f"Maandrapport fout: {e}")
        return {"ok": False, "error": str(e)}, 500


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT") or "10000")
    log("=" * 50)
    log("Crypto AI Bot — WhatsApp Webhook v2.1")
    log("=" * 50)
    log(f"Port:        {port}")
    log(f"Twilio:      {'✅' if TWILIO_ACCOUNT_SID else '❌ ONTBREEKT'}")
    log(f"Database:    {'✅' if DATABASE_URL else '❌ ONTBREEKT'}")
    log(f"Claude API:  {'✅' if ANTHROPIC_API_KEY else '❌ ONTBREEKT'}")
    log(f"Mode:        {TRADER_MODE}")
    log(f"Max/trade:   €{MAX_PER_TRADE_EUR:.2f}")
    log(f"Max/dag:     {MAX_REAL_TRADES_PER_DAY}")
    log(f"Max open:    {MAX_OPEN_REAL_TRADES}")
    log(f"Daily stop:  €{DAILY_STOP_LOSS_EUR:.2f}")
    log(f"Hours:       {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC")
    log("=" * 50)
    app.run(host="0.0.0.0", port=port)
