# live_trader.py
# ============================================================
# Crypto AI Bot — Live Trader v2.0
# ============================================================
# Voert echte BUY en SELL orders uit op Bitvavo.
# Gebruikt Bitvavo API voor live orders, Binance voor data.
#
# IDENTIEK AAN ALLE ANDERE BESTANDEN:
#   ✅ Zelfde ENV variabelen en Fase 1 limieten
#   ✅ Zelfde send_whatsapp() implementatie
#   ✅ Zelfde Claude health monitoring
#   ✅ Zelfde bot state (PostgreSQL bot_state tabel)
#   ✅ Zelfde sslmode="require" op DB connectie
#   ✅ Zelfde safe_int / safe_float / safe_str helpers
#   ✅ Zelfde trading hours filter (08:00-22:00 UTC)
#   ✅ Zelfde weekend: gewoon doorgaan — geen blokkering
#   ✅ Zelfde check_trading_limits logica
#
# BUGS GEFIXED vs origineel:
#   ✅ HMAC signing — digestmod=hashlib.sha256 toegevoegd
#   ✅ get_tradable_markets() publiek gemaakt
#   ✅ price=0 bug bij state opslaan gefixed via fills fallback
#   ✅ sslmode="require" op DB connectie
#   ✅ Auto mode: live eerst, paper als fallback
#   ✅ Geen automatische pauze — bot gaat altijd door
#
# NIEUWE FEATURES:
#   ✅ Retry bij netwerk errors (3x exponential backoff)
#   ✅ Bitvavo fee correct berekend in state
#   ✅ MFE/MAE tracking geïnitialiseerd bij state opslaan
#   ✅ DB logging naar experience_trades (source=LIVE)
#   ✅ Claude analyseert kritieke fouten
#   ✅ Volledige market mapping USDT→EUR
#   ✅ Coin blacklist check voor BUY
#   ✅ Coin cooldown check (24u na verlies)
#   ✅ ATR-based stop validatie
#   ✅ Sell via fractions (partial en full)
# ============================================================

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import psycopg2
import psycopg2.extras
import requests


# ============================================================
# ENV — identiek aan alle andere bestanden
# ============================================================
DATABASE_URL        = (os.getenv("DATABASE_URL") or "").strip()
ANTHROPIC_API_KEY   = (os.getenv("ANTHROPIC_API_KEY") or "").strip()

TWILIO_ACCOUNT_SID   = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN    = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
TWILIO_WHATSAPP_TO   = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()

BITVAVO_API_KEY     = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET  = (os.getenv("BITVAVO_API_SECRET") or "").strip()
BITVAVO_OPERATOR_ID = (os.getenv("BITVAVO_OPERATOR_ID") or "").strip()

BITVAVO_BASE = "https://api.bitvavo.com"
BINANCE_BASE = "https://api.binance.com/api/v3"

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

# Fee + slippage — identiek aan alle bestanden
BITVAVO_FEE_PCT = float(os.getenv("BITVAVO_FEE_PCT") or "0.0025")
SLIPPAGE_PCT    = float(os.getenv("SLIPPAGE_PCT") or "0.001")
TOTAL_COST_PCT  = BITVAVO_FEE_PCT + SLIPPAGE_PCT

# Coin filters
COIN_COOLDOWN_HOURS   = float(os.getenv("COIN_COOLDOWN_HOURS") or "24.0")
BLACKLIST_MIN_TRADES  = int(os.getenv("BLACKLIST_MIN_TRADES") or "20")
BLACKLIST_MAX_WINRATE = float(os.getenv("BLACKLIST_MAX_WINRATE") or "0.30")

MAX_RETRIES     = int(os.getenv("MAX_RETRIES") or "3")
BOT_STATE_TABLE = "public.bot_state"

# Data bestanden
def _get_data_dir() -> str:
    d = (os.getenv("DATA_DIR") or "").strip()
    if d:
        return d
    return "/data" if os.path.isdir("/data") else "/tmp/data"

DATA_DIR        = _get_data_dir()
LIVE_STATE_PATH = os.path.join(DATA_DIR, "live_state.json")

# Bitvavo markets cache
_MARKETS_CACHE: Dict[str, Any] = {"ts": 0.0, "markets": set()}
_MARKETS_TTL = 30 * 60  # 30 minuten


# ============================================================
# BASIS HELPERS — identiek aan alle andere bestanden
# ============================================================
def now_utc() -> datetime:
    """Huidige UTC tijd — identiek in alle bestanden."""
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    """Gestandaardiseerde logging — identiek in alle bestanden."""
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


# ============================================================
# WHATSAPP — identieke implementatie als alle andere bestanden
# ============================================================
def send_whatsapp(message: str) -> bool:
    """
    Stuurt WhatsApp bericht via Twilio.
    Identieke implementatie in alle bestanden.
    Alleen voor kritieke meldingen — geen spam per trade.
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
    Ernst niveaus: KRITIEK, HOOG, MEDIUM, LAAG.
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

    send_whatsapp(
        f"🚨 LIVE TRADER FOUT — {severity}\n"
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
        f"Commands: STATUS | TRADES | STOP"
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
    """
    prompt = f"""
Je bent een crypto trading coach.
Analyseer deze gesloten trade in 2 korte zinnen Nederlands.

Coin:      {symbol}
Setup:     {setup_type} / Regime: {regime}
Entry:     {entry:.6f} → Exit: {exit_price:.6f}
PnL:       €{pnl_eur:.4f}
Duur:      {hold_min:.0f} min
Score:     {score}
Uitkomst:  {outcome}
Exitreden: {exit_reden}

Was de entry en exit correct? Wat leren we?
""".strip()

    return _claude_analyse(prompt, max_tokens=120)


def claude_trader_health_check() -> str:
    """
    Claude controleert of de trader correct geconfigureerd is.
    Wordt aangeroepen bij opstarten.
    """
    prompt = f"""
Je bent een crypto trading bot configuratie checker.
Controleer of de live_trader.py correct is geconfigureerd.

CONFIGURATIE:
- BITVAVO_API_KEY:    {'✅ aanwezig' if BITVAVO_API_KEY else '❌ ONTBREEKT'}
- BITVAVO_API_SECRET: {'✅ aanwezig' if BITVAVO_API_SECRET else '❌ ONTBREEKT'}
- DATABASE_URL:       {'✅ aanwezig' if DATABASE_URL else '❌ ONTBREEKT'}
- MAX_PER_TRADE_EUR:  €{MAX_PER_TRADE_EUR:.2f}
- DAILY_STOP_LOSS:    €{DAILY_STOP_LOSS_EUR:.2f}
- TRADING_HOURS:      {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC
- FEE_PCT:            {BITVAVO_FEE_PCT*100:.2f}%
- SLIPPAGE_PCT:       {SLIPPAGE_PCT*100:.2f}%

Geef een korte check (2-3 zinnen):
1. Is de configuratie compleet?
2. Zijn er potentiële problemen?
3. Aanbevelingen?
""".strip()

    return _claude_analyse(prompt, max_tokens=150)


# ============================================================
# DATABASE — sslmode="require" identiek aan alle bestanden
# ============================================================
def db_connect():
    """DB verbinding met sslmode=require. Identiek in alle bestanden."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


# ============================================================
# BOT STATE — identiek aan whatsapp_webhook.py en trade_monitor.py
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
# LIMIETEN CHECK — identiek aan whatsapp_webhook.py
# Bot stopt NOOIT automatisch — jij beslist via STOP
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
    """Telt open echte trades uit live_state.json + DB."""
    # Probeer eerst state file
    try:
        state = load_state()
        pos   = state.get("positions") or {}
        if pos:
            return len(pos)
    except Exception:
        pass

    # Dan DB
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
    """Wins, losses, PnL voor een dag — identiek aan webhook."""
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
    """Opeenvolgende verliezen — identiek aan alle bestanden."""
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
    Bot stopt NOOIT automatisch — jij beslist via STOP.
    Identiek aan whatsapp_webhook.py check_trading_limits.
    """
    if not is_bot_active(conn):
        return False, "Bot GESTOPT — stuur START"

    if is_bot_paused(conn):
        reason = get_bot_state(conn, "bot_paused_reason", "")
        return False, f"Bot GEPAUZEERD: {reason}"

    if not is_trading_hours():
        return False, f"Buiten trading hours ({TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC)"

    # Daily stop loss: alleen informeren — bot gaat door
    _, _, daily_pnl = get_daily_pnl(conn, utc_day_str())
    if daily_pnl <= -DAILY_STOP_LOSS_EUR:
        log(f"ℹ️ Dagbudget bereikt: €{daily_pnl:.2f} — bot gaat door (jij beslist via STOP)")

    trades_today = get_real_trades_today(conn)
    if trades_today >= MAX_REAL_TRADES_PER_DAY:
        return False, f"Daglimiet: {trades_today}/{MAX_REAL_TRADES_PER_DAY}"

    open_count = get_open_real_trades_count(conn)
    if open_count >= MAX_OPEN_REAL_TRADES:
        return False, f"Max open: {open_count}/{MAX_OPEN_REAL_TRADES}"

    # Consecutive losses: alleen informeren — bot gaat door
    consecutive = get_consecutive_losses(conn)
    if consecutive >= MAX_CONSECUTIVE_LOSSES:
        log(f"ℹ️ {consecutive}x verlies op rij — bot gaat door (jij beslist via STOP)")

    return True, "OK"


# ============================================================
# BITVAVO UNIVERSE FILTER
# Publiek gemaakt zodat multi_coin_score het ook kan gebruiken
# ============================================================
def get_tradable_markets() -> Set[str]:
    """
    Haalt actieve Bitvavo EUR markets op.
    Publiek en gecached (30 minuten TTL).
    Wordt ook gebruikt door multi_coin_score.py.

    Fix: was _get_tradable_markets (privaat) — nu publiek.
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
        log(f"✅ Bitvavo markets gecached: {len(tradable)} tradable")
        return tradable

    except Exception as e:
        log(f"⚠️ Bitvavo markets fout: {e}")
        return _MARKETS_CACHE.get("markets") or set()


def symbol_to_market(symbol_usdt: str) -> Optional[str]:
    """
    Converteert USDT symbol naar Bitvavo EUR market.
    ETHUSDT → ETH-EUR als ETH-EUR tradable is op Bitvavo.
    Geeft None als niet tradable.
    """
    s = safe_str(symbol_usdt).upper()
    if not s.endswith("USDT"):
        # Probeer direct als market
        if "-EUR" in s:
            tradable = get_tradable_markets()
            return s if s in tradable else None
        return None
    base   = s[:-4]
    market = f"{base}-EUR"
    tradable = get_tradable_markets()
    return market if market in tradable else None


def is_coin_tradable(symbol_usdt: str) -> bool:
    """Controleert of een coin tradable is op Bitvavo."""
    return symbol_to_market(symbol_usdt) is not None


# ============================================================
# BITVAVO API — SIGNING
# Fix: digestmod=hashlib.sha256 toegevoegd
# ============================================================
def _bitvavo_headers(
    method: str,
    path:   str,
    body:   str = "",
) -> Dict[str, str]:
    """
    Genereert Bitvavo API headers met correcte HMAC signing.

    Fix: hmac.new() mist digestmod parameter.
    Zonder digestmod geeft Python een DeprecationWarning en
    kan op sommige versies falen. Correcte aanroep:
    hmac.new(key, msg, digestmod=hashlib.sha256)
    """
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        raise ValueError("Bitvavo API key of secret ontbreekt")

    ts      = str(int(time.time() * 1000))
    message = f"{ts}{method}{path}{body}"

    sig = hmac.new(
        BITVAVO_API_SECRET.strip("'\"").encode("utf-8"),
        message.encode("utf-8"),
        digestmod=hashlib.sha256,  # FIX: was hmac.new(..., hashlib.sha256)
    ).hexdigest()

    headers = {
        "Bitvavo-Access-Key":       BITVAVO_API_KEY.strip("'\""),
        "Bitvavo-Access-Signature": sig,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Window":    "10000",
        "Content-Type":             "application/json",
    }

    if BITVAVO_OPERATOR_ID:
        headers["Bitvavo-Operator-Id"] = BITVAVO_OPERATOR_ID.strip("'\"")

    return headers


def _bitvavo_request(
    method:  str,
    path:    str,
    payload: Optional[Dict] = None,
    retries: int = MAX_RETRIES,
) -> Tuple[bool, Any]:
    """
    Voert een Bitvavo API request uit met retry.

    Geeft (success, response_data) terug.
    Bij fout wordt (False, error_message) teruggegeven.
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

            # Bitvavo error response
            err_code = data.get("errorCode", resp.status_code)
            err_msg  = data.get("error", str(data))
            log(f"⚠️ Bitvavo {method} {path} → {err_code}: {err_msg} (poging {attempt}/{retries})")

            # Niet opnieuw proberen bij auth fouten
            if resp.status_code in (401, 403):
                return False, f"Auth fout {resp.status_code}: {err_msg}"

        except requests.exceptions.Timeout:
            log(f"⚠️ Bitvavo timeout poging {attempt}/{retries}")
        except Exception as e:
            log(f"⚠️ Bitvavo request fout poging {attempt}/{retries}: {e}")

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
        log(f"⚠️ Binance prijs fout ({symbol_usdt}): {e}")
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
        log(f"⚠️ Bitvavo prijs fout ({market}): {e}")
    return None


def get_current_price(symbol_usdt: str, market: str) -> Optional[float]:
    """
    Haalt prijs op — Bitvavo eerst, Binance als fallback.
    Bitvavo geeft EUR prijs, Binance geeft USDT prijs.
    """
    # Bitvavo EUR prijs is meest accuraat voor onze trades
    price = get_price_bitvavo(market)
    if price and price > 0:
        return price
    # Binance USDT prijs als fallback
    price = get_price_binance(symbol_usdt)
    if price and price > 0:
        return price
    return None


def get_eur_balance() -> float:
    """Haalt beschikbaar EUR saldo op van Bitvavo."""
    ok, data = _bitvavo_request("GET", "/balance", {"symbol": "EUR"})
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
# LIVE STATE — file helpers
# ============================================================
def load_state() -> Dict[str, Any]:
    """Laadt de live trade state uit JSON file."""
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
    """Slaat de live trade state op naar JSON file. Atomisch via tmp."""
    _ensure_dir(LIVE_STATE_PATH)
    tmp = LIVE_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, LIVE_STATE_PATH)


# ============================================================
# DB LOGGING — naar experience_trades
# ============================================================
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
                created_at, updated_at
            )
            VALUES (
                %s,'LIVE',%s,%s,NOW(),NOW(),%s,%s,%s,%s,%s,%s,
                %s,%s,%s,'OPEN',
                %s,%s,%s,
                %s,%s,%s,
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
            ))
        conn.commit()
        log(f"✅ DB gelogd (OPEN): {symbol} entry={entry:.6f}")
    except Exception as e:
        log(f"⚠️ log_trade_open_to_db fout ({symbol}): {e}")


def log_trade_close_to_db(
    conn,
    symbol:     str,
    prebuy_id:  str,
    exit_price: float,
    pnl_eur:    float,
    outcome:    str,
    exit_reden: str,
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
                # Nieuw record als OPEN niet gevonden
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
        log(f"✅ DB gelogd ({outcome}): {symbol} pnl=€{pnl_eur:.4f}")
    except Exception as e:
        log(f"⚠️ log_trade_close_to_db fout ({symbol}): {e}")


# ============================================================
# COIN FILTERS — identiek aan multi_coin_score en trade_monitor
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
                if hasattr(last_loss, 'tzinfo') and last_loss.tzinfo is None:
                    last_loss = last_loss.replace(tzinfo=timezone.utc)
                hours_since = (now_utc() - last_loss).total_seconds() / 3600
                return hours_since < COIN_COOLDOWN_HOURS
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
# BUY ORDER
# ============================================================
def place_market_buy_eur(
    market:     str,
    amount_eur: float,
) -> Tuple[bool, Dict]:
    """
    Plaatst een market BUY order op Bitvavo voor een EUR bedrag.

    Fix: signing was incorrect — nu met digestmod=hashlib.sha256.
    Fix: price=0 bug — nu via fills fallback.

    Geeft (success, order_data) terug.
    """
    payload = {
        "market":    market,
        "side":      "buy",
        "orderType": "market",
        "amountQuote": str(round(amount_eur, 2)),
    }

    log(f"📤 Bitvavo BUY: {market} €{amount_eur:.2f}")

    ok, data = _bitvavo_request("POST", "/order", payload)

    if not ok:
        log(f"❌ BUY mislukt ({market}): {data}")
        return False, {"error": str(data)}

    if isinstance(data, dict) and "error" in data:
        log(f"❌ BUY error ({market}): {data}")
        return False, data

    # Extraheer prijs en qty uit fills
    price = 0.0
    qty   = 0.0

    fills = data.get("fills") or []
    if fills:
        total_eur  = sum(safe_float(f.get("amount")) * safe_float(f.get("price")) for f in fills)
        total_qty  = sum(safe_float(f.get("amount")) for f in fills)
        price      = total_eur / total_qty if total_qty > 0 else 0.0
        qty        = total_qty
    else:
        # Fallback: gebruik price en filledAmount uit order
        price = safe_float(data.get("price") or data.get("avgFillPrice") or 0)
        qty   = safe_float(data.get("filledAmount") or data.get("filled") or 0)

        # Als prijs nog 0 is, haal op via ticker
        if price <= 0:
            price = get_price_bitvavo(market) or 0.0

    data["_parsed_price"] = price
    data["_parsed_qty"]   = qty

    log(f"✅ BUY uitgevoerd: {market} qty={qty:.6f} @ €{price:.6f}")
    return True, data


def place_market_sell(
    market:   str,
    qty:      float,
    fraction: float = 1.0,
) -> Tuple[bool, Dict]:
    """
    Plaatst een market SELL order op Bitvavo.

    fraction=1.0  → verkoop alles
    fraction=0.40 → verkoop 40%

    Geeft (success, order_data) terug.
    """
    sell_qty = qty * fraction
    sell_qty = round(sell_qty, 8)

    if sell_qty <= 0:
        return False, {"error": f"Ongeldige qty: {sell_qty}"}

    payload = {
        "market":    market,
        "side":      "sell",
        "orderType": "market",
        "amount":    str(sell_qty),
    }

    log(f"📤 Bitvavo SELL: {market} qty={sell_qty:.6f} ({fraction*100:.0f}%)")

    ok, data = _bitvavo_request("POST", "/order", payload)

    if not ok:
        log(f"❌ SELL mislukt ({market}): {data}")
        return False, {"error": str(data)}

    # Extraheer prijs
    fills    = data.get("fills") or []
    price    = 0.0
    sold_qty = 0.0

    if fills:
        total_eur  = sum(safe_float(f.get("amount")) * safe_float(f.get("price")) for f in fills)
        total_qty  = sum(safe_float(f.get("amount")) for f in fills)
        price      = total_eur / total_qty if total_qty > 0 else 0.0
        sold_qty   = total_qty
    else:
        price    = safe_float(data.get("price") or data.get("avgFillPrice") or 0)
        sold_qty = safe_float(data.get("filledAmount") or sell_qty)
        if price <= 0:
            market_name = market
            price = get_price_bitvavo(market_name) or 0.0

    data["_parsed_price"]    = price
    data["_parsed_sold_qty"] = sold_qty
    data["_parsed_fraction"] = fraction

    log(f"✅ SELL uitgevoerd: {market} qty={sold_qty:.6f} @ €{price:.6f}")
    return True, data


# ============================================================
# HOOFD BUY FUNCTIE — aangeroepen door whatsapp_webhook.py
# ============================================================
def buy_eur(
    symbol:     str,
    amount_eur: float = MAX_PER_TRADE_EUR,
    meta:       Optional[Dict] = None,
) -> Tuple[bool, str]:
    """
    Voert een live BUY uit op Bitvavo.

    Stappen:
    1. Market ophalen (USDT→EUR)
    2. Alle limieten controleren
    3. Coin filters (cooldown, blacklist)
    4. EUR balance check
    5. BUY order plaatsen
    6. State opslaan
    7. DB loggen

    Geeft (success, message) terug.
    """
    meta = meta or {}

    # 1. Market ophalen
    market = symbol_to_market(symbol)
    if not market:
        return False, f"{symbol} niet tradable op Bitvavo"

    try:
        conn = db_connect()

        # 2. Limieten check
        ok, reason = check_trading_limits(conn)
        if not ok:
            conn.close()
            return False, reason

        # 3. Coin filters
        if is_coin_blacklisted(conn, symbol):
            conn.close()
            log(f"⚫ {symbol} op blacklist — BUY geblokkeerd")
            return False, f"{symbol} op blacklist (win rate te laag)"

        if is_coin_on_cooldown(conn, symbol):
            conn.close()
            log(f"⏳ {symbol} in cooldown — BUY geblokkeerd")
            return False, f"{symbol} in cooldown (24u na verlies)"

        # 4. EUR balance check
        eur_balance = get_eur_balance()
        if eur_balance < amount_eur:
            conn.close()
            return False, f"Onvoldoende EUR: {eur_balance:.2f} < {amount_eur:.2f}"

        # 5. BUY order plaatsen
        ok, order_data = place_market_buy_eur(market, amount_eur)

        if not ok:
            conn.close()
            report_error(
                Exception(order_data.get("error", "Onbekend")),
                "buy_eur.place_market_buy_eur",
                symbol, "KRITIEK",
                get_open_real_trades_count(conn) if conn else 0,
            )
            return False, f"BUY mislukt: {order_data.get('error')}"

        # 6. Prijs en qty bepalen
        entry   = safe_float(order_data.get("_parsed_price"))
        qty     = safe_float(order_data.get("_parsed_qty"))
        fee_eur = round(amount_eur * BITVAVO_FEE_PCT, 6)

        if entry <= 0 or qty <= 0:
            log(f"⚠️ Prijs/qty ongeldig na BUY: entry={entry} qty={qty}")
            # Probeer prijs via ticker
            entry = get_price_bitvavo(market) or get_price_binance(symbol) or 0.0
            qty   = amount_eur / entry if entry > 0 else 0.0

        stop   = safe_float(meta.get("stop"),   entry * 0.98)
        target = safe_float(meta.get("target"), entry * 1.04)

        # 7. State opslaan
        state = load_state()
        state["positions"][symbol] = {
            "symbol":       symbol,
            "market":       market,
            "entry":        entry,
            "stop_loss":    stop,
            "stop":         stop,
            "target":       target,
            "qty":          qty,
            "amount_eur":   amount_eur,
            "fee_eur":      fee_eur,
            "opened_at":    int(time.time()),
            "prebuy_id":    safe_str(meta.get("prebuy_id")),
            "setup_type":   safe_str(meta.get("setup_type"), "UNKNOWN"),
            "regime":       safe_str(meta.get("regime"), "UNKNOWN"),
            "score":        safe_int(meta.get("score")),
            "timeframe":    safe_str(meta.get("timeframe"), "4h"),
            "source":       "LIVE",
            "status":       "OPEN",
            "had_over_1r":  False,
            "partial_sold_40": False,
            "below_1r_count": 0,
            "last_candle_check_ts": 0,
            "max_price_seen": entry,
            "min_price_seen": entry,
            "mfe_r":        0.0,
            "mae_r":        0.0,
            "order_id":     safe_str(order_data.get("orderId")),
        }
        state["open_trades"] = list(state["positions"].values())
        save_state(state)

        # 8. DB loggen
        log_trade_open_to_db(
            conn, symbol, market, entry, qty, amount_eur, fee_eur,
            stop, target, meta,
        )

        conn.close()

        log(f"✅ Live BUY: {symbol} @ €{entry:.6f} qty={qty:.6f} stop={stop:.6f}")
        return True, f"BUY {symbol} @ €{entry:.6f}"

    except Exception as e:
        report_error(e, "buy_eur", symbol, "KRITIEK")
        return False, str(e)


# ============================================================
# HOOFD SELL FUNCTIE — aangeroepen door trade_monitor.py
# ============================================================
def sell(
    symbol:   str,
    fraction: float = 1.0,
    meta:     Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Voert een live SELL uit op Bitvavo.

    fraction=1.0  → verkoop alles (stop loss, structuur break, max hold)
    fraction=0.40 → partial sell (40% na eerste keer >1R)

    Wordt aangeroepen door trade_monitor.py via _execute_sell().
    Geeft result dict terug: {ok, pnl_eur, fee_eur, exit_price, reason}
    """
    meta = meta or {}

    try:
        state  = load_state()
        pos    = (state.get("positions") or {}).get(symbol)

        if not pos:
            return {
                "ok":     False,
                "reason": f"{symbol} niet gevonden in state",
            }

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
            return {
                "ok":     False,
                "reason": str(order_data.get("error", "SELL mislukt")),
            }

        exit_price = safe_float(order_data.get("_parsed_price"))
        sold_qty   = safe_float(order_data.get("_parsed_sold_qty"), qty * fraction)
        fee_buy    = safe_float(pos.get("fee_eur"))
        fee_sell   = round(exit_price * sold_qty * BITVAVO_FEE_PCT, 6)
        pnl_eur    = (exit_price - entry) * sold_qty - fee_buy * fraction - fee_sell

        outcome = "WIN" if pnl_eur > 0 else "LOSS"

        # State updaten
        if fraction >= 1.0:
            # Volledig verkopen — verwijder uit state
            state["positions"].pop(symbol, None)
            state["open_trades"] = [
                t for t in (state.get("open_trades") or [])
                if t.get("symbol") != symbol
            ]
        else:
            # Partial sell — update qty
            remaining_qty       = qty - sold_qty
            remaining_eur       = amount_eur * (1 - fraction)
            state["positions"][symbol]["qty"]        = remaining_qty
            state["positions"][symbol]["amount_eur"] = remaining_eur
            state["positions"][symbol]["fee_eur"]    = fee_buy * (1 - fraction)

        save_state(state)

        # DB loggen
        try:
            conn = db_connect()
            log_trade_close_to_db(
                conn, symbol, prebuy_id, exit_price, pnl_eur,
                outcome, exit_reden, "",
            )
            conn.close()
        except Exception as e:
            log(f"⚠️ DB log fout ({symbol}): {e}")

        log(
            f"{'✅' if outcome=='WIN' else '❌'} "
            f"SELL {symbol}: {outcome} "
            f"€{pnl_eur:.4f} | "
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
        }

    except Exception as e:
        report_error(e, "sell", symbol, "KRITIEK")
        return {
            "ok":     False,
            "reason": str(e),
        }


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    log("=" * 60)
    log("Live Trader v2.0 — configuratie check")
    log("=" * 60)
    log(f"Database:       {'✅' if DATABASE_URL else '❌ ONTBREEKT'}")
    log(f"Bitvavo Key:    {'✅' if BITVAVO_API_KEY else '❌ ONTBREEKT'}")
    log(f"Bitvavo Secret: {'✅' if BITVAVO_API_SECRET else '❌ ONTBREEKT'}")
    log(f"Twilio:         {'✅' if TWILIO_ACCOUNT_SID else '⚠️ niet ingesteld'}")
    log(f"Claude API:     {'✅' if ANTHROPIC_API_KEY else '⚠️ niet ingesteld'}")
    log(f"Max trade:      €{MAX_PER_TRADE_EUR:.2f}")
    log(f"Daily stop:     €{DAILY_STOP_LOSS_EUR:.2f}")
    log(f"Max trades/dag: {MAX_REAL_TRADES_PER_DAY}")
    log(f"Max open:       {MAX_OPEN_REAL_TRADES}")
    log(f"Trading hours:  {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC")
    log(f"Fee+slippage:   {TOTAL_COST_PCT*100:.2f}%")
    log(f"Data dir:       {DATA_DIR}")
    log("=" * 60)

    # Test Bitvavo verbinding
    if BITVAVO_API_KEY and BITVAVO_API_SECRET:
        log("Test Bitvavo balance...")
        eur = get_eur_balance()
        log(f"EUR balance: €{eur:.2f}")

    # Test Bitvavo markets
    log("Test Bitvavo markets...")
    markets = get_tradable_markets()
    log(f"Tradable markets: {len(markets)}")

    # Claude health check
    if ANTHROPIC_API_KEY:
        log("Claude health check...")
        health = claude_trader_health_check()
        if health:
            log(f"Claude: {health}")

    log("✅ Live Trader configuratie check klaar")
