# trading/live_trader.py
# ============================================================
# Crypto AI Bot — Live Trader v2.0
# ============================================================
# Zelfde gedachtegang als whatsapp_webhook.py:
#
#   ✅ Zelfde ENV variabelen + limieten
#   ✅ Zelfde send_whatsapp() implementatie
#   ✅ Zelfde Claude health monitoring patroon
#   ✅ Zelfde bot state (PostgreSQL bot_state tabel)
#   ✅ Zelfde is_bot_active / is_bot_paused / pause_bot
#   ✅ Zelfde check_trading_limits (alle limieten)
#   ✅ Zelfde trading hours filter (08:00-22:00 UTC)
#   ✅ Zelfde DB logging naar experience_trades
#   ✅ Zelfde safe_int / safe_float / safe_str helpers
#   ✅ Zelfde sslmode="require" op DB connectie
#   ✅ hmac.new(..., digestmod=hashlib.sha256) — fix
#   ✅ get_tradable_markets() publiek (niet _privaat)
#   ✅ Fee (0.25%) + slippage (0.1%) = 0.35% totaal
#   ✅ price=0 bug gefixed via fills fallback
#   ✅ Retry bij netwerk errors (exponential backoff)
#   ✅ DB logging van elke BUY en SELL
#   ✅ Weekend: gewoon traden — geen blokkering
# ============================================================

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Set, Tuple

import psycopg2
import requests


# ============================================================
# ENV — identiek aan whatsapp_webhook.py
# ============================================================
DATABASE_URL      = (os.getenv("DATABASE_URL") or "").strip()
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()

# Twilio — zelfde als webhook
TWILIO_ACCOUNT_SID   = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN    = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
TWILIO_WHATSAPP_TO   = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()

# Bitvavo
BITVAVO_BASE_URL      = (os.getenv("BITVAVO_BASE_URL") or "https://api.bitvavo.com").rstrip("/")
BITVAVO_API_KEY       = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET    = (os.getenv("BITVAVO_API_SECRET") or "").strip()
BITVAVO_OPERATOR_ID   = (os.getenv("BITVAVO_OPERATOR_ID") or "crypto_ai_bot").strip()
BITVAVO_ACCESS_WINDOW = (os.getenv("BITVAVO_ACCESS_WINDOW") or "10000").strip()

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT") or "20")
MAX_RETRIES  = int(os.getenv("MAX_RETRIES") or "3")

# ============================================================
# FASE 1 LIMIETEN — identiek aan whatsapp_webhook.py
# ============================================================
MAX_PER_TRADE_EUR            = float(os.getenv("MAX_PER_TRADE_EUR") or "0.50")
MAX_REAL_TRADES_PER_DAY      = int(os.getenv("MAX_REAL_TRADES_PER_DAY") or "10")
MAX_OPEN_REAL_TRADES         = int(os.getenv("MAX_OPEN_REAL_TRADES") or "5")
DAILY_STOP_LOSS_EUR          = float(os.getenv("DAILY_STOP_LOSS_EUR") or "5.00")
MAX_CONSECUTIVE_LOSSES       = int(os.getenv("MAX_CONSECUTIVE_LOSSES") or "3")
CONSECUTIVE_LOSS_PAUSE_HOURS = int(os.getenv("CONSECUTIVE_LOSS_PAUSE_HOURS") or "2")
TRADING_HOURS_START          = int(os.getenv("TRADING_HOURS_START") or "8")
TRADING_HOURS_END            = int(os.getenv("TRADING_HOURS_END") or "22")

# Bot state — zelfde tabel als webhook
BOT_STATE_TABLE = "public.bot_state"

# Fee + slippage
BITVAVO_FEE_PCT = float(os.getenv("BITVAVO_FEE_PCT") or "0.0025")  # 0.25%
SLIPPAGE_PCT    = float(os.getenv("SLIPPAGE_PCT") or "0.001")       # 0.10%
TOTAL_COST_PCT  = BITVAVO_FEE_PCT + SLIPPAGE_PCT                    # 0.35%

# Data bestanden
def _get_data_dir() -> str:
    d = (os.getenv("DATA_DIR") or "").strip()
    if d:
        return d
    return "/data" if os.path.isdir("/data") else "/tmp/data"

DATA_DIR        = _get_data_dir()
LIVE_STATE_PATH = os.path.join(DATA_DIR, "live_state.json")
LIVE_TRADES_CSV = os.path.join(DATA_DIR, "live_trades.csv")

# Markets cache (30 min)
_MARKETS_CACHE: Dict[str, Any] = {"ts": 0.0, "markets": set()}
_MARKETS_TTL = 30 * 60


# ============================================================
# BASIS HELPERS — identiek aan whatsapp_webhook.py
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


def utc_day_str(offset_days: int = 0) -> str:
    return (now_utc() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def is_trading_hours() -> bool:
    """Trading hours 08:00-22:00 UTC — zelfde als webhook."""
    return TRADING_HOURS_START <= now_utc().hour < TRADING_HOURS_END


def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


# ============================================================
# WHATSAPP — identieke implementatie als whatsapp_webhook.py
# Geen import om circulaire dependencies te voorkomen.
# Alleen gebruikt voor KRITIEKE meldingen.
# ============================================================
def send_whatsapp(message: str) -> bool:
    """
    Stuurt WhatsApp bericht via Twilio.
    Identiek aan whatsapp_webhook.py send_whatsapp().
    Alleen voor KRITIEKE fouten — geen spam per trade.
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
# CLAUDE HEALTH MONITORING
# Zelfde patroon als whatsapp_webhook.py
# Ernst: KRITIEK → WhatsApp | HOOG → WhatsApp | MEDIUM/LAAG → alleen log
# ============================================================
def _claude_analyse(prompt: str, max_tokens: int = 300) -> str:
    """Claude API aanroep — identiek aan webhook."""
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
    symbol: str = "",
    severity: str = "HOOG",
) -> None:
    """
    Rapporteert fout via Claude analyse + WhatsApp.
    Zelfde ernst niveaus als whatsapp_webhook.py:
      KRITIEK → WhatsApp direct
      HOOG    → WhatsApp
      MEDIUM  → alleen log
      LAAG    → alleen log
    """
    log(f"[{severity}] {function} ({symbol}): {type(error).__name__}: {error}")

    if severity not in ("KRITIEK", "HOOG"):
        return

    prompt = f"""
Je bent een crypto trading bot monitor.
Er is een fout in live_trader.py opgetreden.

Ernst:    {severity}
Functie:  {function}
Coin:     {symbol or 'onbekend'}
Fout:     {type(error).__name__}: {str(error)[:200]}

Geef in 2-3 zinnen Nederlands:
- Wat er mis is
- Impact op open trades
- Wat de gebruiker moet doen
""".strip()

    uitleg = _claude_analyse(prompt, max_tokens=200)
    if not uitleg:
        uitleg = f"{type(error).__name__}: {str(error)[:100]}"

    send_whatsapp(
        f"🚨 LIVE TRADER — {severity}\n\n"
        f"📁 Functie: {function}\n"
        f"🪙 Coin: {symbol or '-'}\n\n"
        f"🧠 Claude:\n{uitleg}"
    )


# ============================================================
# DATABASE — sslmode="require" identiek aan webhook
# ============================================================
def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


# ============================================================
# BOT STATE — identiek aan whatsapp_webhook.py
# Zelfde tabel, zelfde functies, zelfde logica
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
# LIMIETEN CHECK — identiek aan whatsapp_webhook.py
# Zelfde volgorde, zelfde logica, zelfde pauzeer gedrag
# Weekend: gewoon traden — geen blokkering
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
    """
    Telt open echte trades.
    Zelfde fallback logica als webhook: DB eerst, dan live_state.json.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT COUNT(*) FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
              AND TRIM(UPPER(COALESCE(outcome,''))) IN ('OPEN','UNKNOWN','')
            """)
            row   = cur.fetchone()
            count = safe_int(row[0]) if row else 0
            if count > 0:
                return count
    except Exception:
        pass

    # Fallback: live_state.json
    try:
        state_path = LIVE_STATE_PATH
        if os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            return len(state.get("open_trades") or [])
    except Exception:
        pass
    return 0


def get_daily_pnl(conn, day: str) -> Tuple[int, int, float]:
    """Geeft (wins, losses, pnl_eur) — identiek aan webhook."""
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
    """Opeenvolgende verliezen — identiek aan webhook."""
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
    Controleert alle limieten voor een BUY.
    Identieke volgorde en logica als whatsapp_webhook.py.
    Weekend: gewoon doorgaan — geen blokkering.
    """
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
        # Alleen loggen — jij beslist via STOP
        log(f"ℹ️ Dagbudget bereikt: €{daily_pnl:.2f} — bot gaat door")

    trades_today = get_real_trades_today(conn)
    if trades_today >= MAX_REAL_TRADES_PER_DAY:
        return False, f"Daglimiet: {trades_today}/{MAX_REAL_TRADES_PER_DAY}"

    open_trades = get_open_real_trades_count(conn)
    if open_trades >= MAX_OPEN_REAL_TRADES:
        return False, f"Max open: {open_trades}/{MAX_OPEN_REAL_TRADES}"

    consecutive = get_consecutive_losses(conn)
    if consecutive >= MAX_CONSECUTIVE_LOSSES:
        # Alleen loggen — jij beslist via STOP
        log(f"ℹ️ {consecutive}x verlies op rij — bot gaat door (jij beslist via STOP)")

    return True, "OK"


# ============================================================
# DB LOGGING — naar experience_trades (zelfde tabel als shadow/sim)
# Zodat dagrapport in webhook alles ziet
# ============================================================
def log_trade_to_db(
    conn,
    symbol:     str,
    side:       str,
    price:      float,
    qty:        float,
    amount_eur: float,
    pnl_eur:    float    = 0.0,
    prebuy_id:  str      = "",
    setup_type: str      = "",
    regime:     str      = "",
    score:      int      = 0,
    outcome:    str      = "OPEN",
    fee_eur:    float    = 0.0,
    exit_time:  Optional[datetime] = None,
) -> None:
    """
    Logt elke live trade naar experience_trades.
    source='LIVE' — zodat webhook rapporten alles zien.
    """
    try:
        trade_key = f"LIVE|{symbol}|{prebuy_id or int(time.time())}"
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO public.experience_trades (
                trade_key, source, coin, timestamp, entry_time,
                setup_type, market_regime, entry,
                outcome, pnl_eur, bot_confidence,
                exit_time, created_at, updated_at
            )
            VALUES (%s,'LIVE',%s,NOW(),NOW(),%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())
            ON CONFLICT (trade_key) DO UPDATE SET
                outcome    = EXCLUDED.outcome,
                pnl_eur    = EXCLUDED.pnl_eur,
                exit_time  = EXCLUDED.exit_time,
                updated_at = NOW()
            """, (
                trade_key, symbol, setup_type, regime,
                price, outcome, pnl_eur, score,
                exit_time,
            ))
        conn.commit()
        log(f"✅ DB gelogd: {symbol} {side} {outcome} €{pnl_eur:.4f}")
    except Exception as e:
        log(f"⚠️ DB log fout ({symbol} {side}): {type(e).__name__}: {e}")


# ============================================================
# FILE HELPERS — live_state.json
# ============================================================
def _load_state() -> Dict[str, Any]:
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


def _save_state(state: Dict[str, Any]) -> None:
    _ensure_dir(LIVE_STATE_PATH)
    tmp = LIVE_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, LIVE_STATE_PATH)


def _log_csv(
    symbol: str, side: str, price: float, qty: float,
    pnl: float = 0.0, fee: float = 0.0, meta: str = "",
) -> None:
    _ensure_dir(LIVE_TRADES_CSV)
    new_file = not os.path.exists(LIVE_TRADES_CSV)
    with open(LIVE_TRADES_CSV, "a", encoding="utf-8") as f:
        if new_file:
            f.write("datetime,symbol,side,price,qty,pnl,fee,meta\n")
        ts = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        f.write(f"{ts},{symbol},{side},{price},{qty},{pnl},{fee},{meta}\n")


# ============================================================
# BITVAVO SIGNING — digestmod= FIX
# ============================================================
def _sign(timestamp_ms: str, method: str, path: str, body: str = "") -> str:
    """
    HMAC signing met digestmod=hashlib.sha256.
    digestmod= is verplicht — fix voor oudere code.
    """
    msg    = f"{timestamp_ms}{method.upper()}{path}{body}".encode("utf-8")
    secret = BITVAVO_API_SECRET.encode("utf-8")
    return hmac.new(secret, msg, digestmod=hashlib.sha256).hexdigest()


def _headers(timestamp_ms: str, signature: str) -> Dict[str, str]:
    return {
        "Bitvavo-Access-Key":       BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": signature,
        "Bitvavo-Access-Timestamp": timestamp_ms,
        "Bitvavo-Access-Window":    BITVAVO_ACCESS_WINDOW,
        "Content-Type":             "application/json",
    }


def _request_signed(
    method:   str,
    path:     str,
    body_obj: Optional[dict] = None,
    retries:  int = MAX_RETRIES,
) -> Dict[str, Any]:
    """
    Signed Bitvavo request met exponential backoff retry.
    Identiek retry patroon als webhook send_whatsapp.
    """
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY of SECRET ontbreekt.")

    body = json.dumps(body_obj, separators=(",", ":")) if body_obj else ""

    for attempt in range(1, retries + 1):
        try:
            ts  = str(int(time.time() * 1000))
            sig = _sign(ts, method, path, body)
            url = f"{BITVAVO_BASE_URL}{path}"

            resp = requests.request(
                method  = method.upper(),
                url     = url,
                headers = _headers(ts, sig),
                data    = body or None,
                timeout = HTTP_TIMEOUT,
            )

            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text}

            return {"ok": resp.ok, "status": resp.status_code, "data": data}

        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            log(f"⚠️ Bitvavo poging {attempt}/{retries}: {type(e).__name__}")
            if attempt < retries:
                time.sleep(2 ** attempt)  # 2s, 4s, 8s
            else:
                raise RuntimeError(
                    f"Bitvavo niet bereikbaar na {retries} pogingen: {e}"
                )
        except Exception as e:
            raise RuntimeError(f"Bitvavo request fout: {type(e).__name__}: {e}")

    raise RuntimeError(f"Bitvavo request mislukt: {path}")


# ============================================================
# BITVAVO MARKETS — PUBLIEK (was privaat _get_tradable_markets)
# Gebruikt door multi_coin_score.py
# ============================================================
def get_tradable_markets() -> Set[str]:
    """
    Haalt actieve Bitvavo EUR markets op.
    PUBLIEK — ook bruikbaar door multi_coin_score.py.
    Cache: 30 minuten.
    """
    now = time.time()
    if _MARKETS_CACHE["markets"] and (now - _MARKETS_CACHE["ts"]) < _MARKETS_TTL:
        return _MARKETS_CACHE["markets"]

    try:
        url  = f"{BITVAVO_BASE_URL}/v2/markets"
        resp = requests.get(url, timeout=HTTP_TIMEOUT)
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
        log(f"✅ Bitvavo markets: {len(tradable)} tradable")
        return tradable

    except Exception as e:
        log(f"⚠️ Markets ophalen mislukt: {e}")
        return _MARKETS_CACHE.get("markets") or set()


def symbol_to_market(symbol_usdt: str) -> str:
    """ETHUSDT → ETH-EUR"""
    s = safe_str(symbol_usdt).upper()
    if "-" in s:
        return s
    if not s.endswith("USDT"):
        raise ValueError(f"Ongeldig symbol: {symbol_usdt}")
    base = s[:-4]
    if not base:
        raise ValueError(f"Leeg base symbol: {symbol_usdt}")
    return f"{base}-EUR"


def ensure_market_tradable(symbol_usdt: str) -> str:
    """Controleert of market echt tradable is op Bitvavo."""
    market   = symbol_to_market(symbol_usdt)
    tradable = get_tradable_markets()
    if market not in tradable:
        raise ValueError(f"Niet tradable op Bitvavo: {market}")
    return market


# ============================================================
# ORDER HELPERS
# ============================================================
def _extract_price(order: Dict[str, Any]) -> float:
    """
    Haalt prijs uit order.
    Fallback naar fills als hoofdprijs 0 is — price=0 bug fix.
    """
    price = safe_float(order.get("price"))
    if price > 0:
        return price
    # Fallback via fills
    fills = order.get("fills") or []
    if fills and isinstance(fills, list):
        prices = [safe_float(f.get("price")) for f in fills
                  if safe_float(f.get("price")) > 0]
        if prices:
            return sum(prices) / len(prices)
    return 0.0


def _extract_qty(order: Dict[str, Any]) -> float:
    """Haalt gefillde hoeveelheid op."""
    for key in ("filledAmount", "amount", "filled"):
        val = safe_float(order.get(key))
        if val > 0:
            return val
    fills = order.get("fills") or []
    if fills and isinstance(fills, list):
        total = sum(safe_float(f.get("amount")) for f in fills)
        if total > 0:
            return total
    return 0.0


def _calc_fee(amount_eur: float) -> float:
    """Fee + slippage in EUR."""
    return round(amount_eur * TOTAL_COST_PCT, 6)


# ============================================================
# BUY — met limieten check
# ============================================================
def place_market_buy_eur(
    symbol_usdt: str,
    amount_eur:  float,
    meta:        Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Market BUY via Bitvavo met EUR bedrag.

    Volgorde:
    1. Controleer alle limieten (zelfde als webhook check_trading_limits)
    2. Controleer of market tradable is op Bitvavo
    3. Plaats order via Bitvavo API
    4. Sla op in live_state.json
    5. Log naar experience_trades DB
    6. Log naar CSV
    """
    meta = meta or {}

    if amount_eur <= 0:
        return {"ok": False, "error": "amount_eur moet > 0 zijn"}

    # 1. Limieten check — zelfde als webhook
    try:
        with db_connect() as conn:
            ensure_bot_state_table(conn)
            mag, reden = check_trading_limits(conn)
            if not mag:
                log(f"BUY geblokkeerd ({symbol_usdt}): {reden}")
                return {"ok": False, "error": reden, "symbol": symbol_usdt}
    except Exception as e:
        report_error(e, "place_market_buy_eur.db_check", symbol_usdt, "HOOG")
        return {"ok": False, "error": f"DB check fout: {e}"}

    # 2. Market check
    try:
        market = ensure_market_tradable(symbol_usdt)
    except ValueError as e:
        report_error(e, "place_market_buy_eur.market_check", symbol_usdt, "HOOG")
        return {"ok": False, "error": str(e), "symbol": symbol_usdt}

    fee_eur = _calc_fee(amount_eur)

    # 3. Bitvavo order
    body = {
        "market":      market,
        "side":        "buy",
        "orderType":   "market",
        "amountQuote": str(float(amount_eur)),
        "operatorId":  BITVAVO_OPERATOR_ID,
    }

    try:
        result = _request_signed("POST", "/v2/order", body)
    except Exception as e:
        report_error(e, "place_market_buy_eur.order", symbol_usdt, "KRITIEK")
        return {"ok": False, "error": str(e), "market": market}

    if not result["ok"]:
        err = result.get("data", {})
        log(f"❌ BUY mislukt {market}: {err}")
        return {"ok": False, "error": err, "market": market}

    order = result.get("data") or {}
    price = _extract_price(order)
    qty   = _extract_qty(order)

    # Meta uitlezen
    prebuy_id  = safe_str(meta.get("prebuy_id"))
    stop_loss  = safe_float(meta.get("stop_loss") or meta.get("stop"))
    target     = safe_float(meta.get("target"))
    timeframe  = safe_str(meta.get("timeframe"), "4h")
    setup_type = safe_str(meta.get("setup_type"), "UNKNOWN")
    regime     = safe_str(meta.get("regime"), "UNKNOWN")
    score      = safe_int(meta.get("score"))
    chance     = safe_int(meta.get("chance"))
    label      = safe_str(meta.get("label"), "GO")

    # 4. live_state.json opslaan
    state    = _load_state()
    position = {
        "market":           market,
        "symbol":           symbol_usdt,
        "qty":              qty,
        "entry":            price,
        "stop_loss":        stop_loss,
        "stop":             stop_loss,
        "target":           target,
        "opened_at":        int(time.time()),
        "prebuy_id":        prebuy_id,
        "amount_eur":       amount_eur,
        "fee_eur":          fee_eur,
        "timeframe":        timeframe,
        "setup_type":       setup_type,
        "regime":           regime,
        "score":            score,
        "chance":           chance,
        "label":            label,
        "source":           "LIVE",
        "status":           "OPEN",
        "max_price_seen":   price,
        "min_price_seen":   price,
        "had_over_1r":      False,
        "partial_sold_40":  False,
        "below_1r_count":   0,
        "remaining_fraction": 1.0,
    }

    state["positions"][symbol_usdt] = position

    # Zorg dat open_trades geen dubbele heeft
    state["open_trades"] = [
        t for t in (state.get("open_trades") or [])
        if t.get("symbol") != symbol_usdt
    ]
    state["open_trades"].append(position)
    _save_state(state)

    # 5. DB logging
    try:
        with db_connect() as conn:
            log_trade_to_db(
                conn, symbol_usdt, "BUY", price, qty,
                amount_eur, pnl_eur=0.0, prebuy_id=prebuy_id,
                setup_type=setup_type, regime=regime,
                score=score, outcome="OPEN", fee_eur=fee_eur,
            )
    except Exception as e:
        log(f"⚠️ DB BUY log fout: {e}")

    # 6. CSV
    _log_csv(
        symbol_usdt, "BUY", price, qty, fee=fee_eur,
        meta=json.dumps({
            "market": market, "amount_eur": amount_eur,
            "setup_type": setup_type, "regime": regime,
            "score": score, "prebuy_id": prebuy_id,
        }, separators=(",", ":")),
    )

    log(
        f"✅ BUY {market} | prijs={price:.6f} | qty={qty:.8f}"
        f" | €{amount_eur:.2f} (fee €{fee_eur:.4f})"
        f" | {setup_type}/{regime} score={score}"
    )

    return {
        "ok":          True,
        "market":      market,
        "price":       price,
        "qty":         qty,
        "amount_eur":  amount_eur,
        "fee_eur":     fee_eur,
        "order":       order,
        "live":        True,
    }


# ============================================================
# SELL — PnL inclusief fees BUY + SELL
# ============================================================
def place_market_sell_base(
    market:      str,
    amount_base: float,
) -> Dict[str, Any]:
    """
    Market SELL via Bitvavo.
    PnL berekening inclusief fees van BUY én SELL.
    DB update naar WIN of LOSS.
    """
    if amount_base <= 0:
        return {"ok": False, "error": "amount_base moet > 0 zijn"}

    body = {
        "market":     market,
        "side":       "sell",
        "orderType":  "market",
        "amount":     str(float(amount_base)),
        "operatorId": BITVAVO_OPERATOR_ID,
    }

    try:
        result = _request_signed("POST", "/v2/order", body)
    except Exception as e:
        report_error(e, "place_market_sell_base", market, "KRITIEK")
        return {"ok": False, "error": str(e), "market": market}

    if not result["ok"]:
        err = result.get("data", {})
        log(f"❌ SELL mislukt {market}: {err}")
        return {"ok": False, "error": err, "market": market}

    order      = result.get("data") or {}
    sell_price = _extract_price(order)
    filled_qty = _extract_qty(order) or amount_base

    # Zoek positie op basis van market
    state      = _load_state()
    symbol_key = None
    for sym, pos in (state.get("positions") or {}).items():
        try:
            if symbol_to_market(sym) == market:
                symbol_key = sym
                break
        except Exception:
            pass

    pnl_eur  = 0.0
    fee_eur  = _calc_fee(sell_price * filled_qty)
    outcome  = "LOSS"

    if symbol_key:
        pos        = state["positions"][symbol_key]
        entry      = safe_float(pos.get("entry"))
        qty_total  = safe_float(pos.get("qty"))
        buy_fee    = safe_float(pos.get("fee_eur"))
        setup_type = safe_str(pos.get("setup_type"))
        regime     = safe_str(pos.get("regime"))
        score      = safe_int(pos.get("score"))
        prebuy_id  = safe_str(pos.get("prebuy_id"))

        # PnL inclusief fees BUY + SELL
        if entry > 0 and filled_qty > 0:
            pnl_eur = (sell_price - entry) * filled_qty - buy_fee - fee_eur
        outcome = "WIN" if pnl_eur > 0 else "LOSS"

        # State bijwerken
        if filled_qty >= qty_total or qty_total <= 0:
            del state["positions"][symbol_key]
        else:
            pos["qty"] = max(0.0, qty_total - filled_qty)
            state["positions"][symbol_key] = pos

        # Open trades bijwerken
        state["open_trades"] = [
            t for t in (state.get("open_trades") or [])
            if t.get("symbol") != symbol_key
        ]
        _save_state(state)

        # DB update — WIN of LOSS
        try:
            with db_connect() as conn:
                log_trade_to_db(
                    conn, symbol_key, "SELL", sell_price, filled_qty,
                    sell_price * filled_qty,
                    pnl_eur=pnl_eur, prebuy_id=prebuy_id,
                    setup_type=setup_type, regime=regime,
                    score=score, outcome=outcome, fee_eur=fee_eur,
                    exit_time=now_utc(),
                )
        except Exception as e:
            log(f"⚠️ DB SELL log fout: {e}")

        # CSV
        _log_csv(
            symbol_key, "SELL", sell_price, filled_qty,
            pnl=pnl_eur, fee=fee_eur,
            meta=json.dumps({
                "market": market, "outcome": outcome,
                "pnl_eur": round(pnl_eur, 4),
            }, separators=(",", ":")),
        )

        # Claude analyseert elke gesloten trade
        try:
            hold_min = (
                (time.time() - safe_float(pos.get("opened_at"))) / 60
                if pos.get("opened_at") else 0.0
            )
            claude_analyseer_trade(
                symbol     = symbol_key,
                setup_type = setup_type,
                regime     = regime,
                entry      = safe_float(pos.get("entry")),
                exit_price = sell_price,
                pnl_eur    = pnl_eur,
                fee_eur    = fee_eur,
                hold_min   = hold_min,
                outcome    = outcome,
                score      = score,
            )
        except Exception as e:
            log(f"⚠️ Claude trade analyse fout: {e}")

    log(
        f"✅ SELL {market} | prijs={sell_price:.6f} | qty={filled_qty:.8f}"
        f" | PnL=€{pnl_eur:.4f} (fee €{fee_eur:.4f}) | {outcome}"
    )

    return {
        "ok":        True,
        "market":    market,
        "price":     sell_price,
        "qty":       filled_qty,
        "pnl_eur":   pnl_eur,
        "fee_eur":   fee_eur,
        "outcome":   outcome,
        "order":     order,
        "live":      True,
    }


# ============================================================
# CLAUDE AI TOEZICHT — op trade niveau
# Zelfde patroon als webhook maar gericht op individuele trades
# ============================================================
def claude_analyseer_trade(
    symbol:     str,
    setup_type: str,
    regime:     str,
    entry:      float,
    exit_price: float,
    pnl_eur:    float,
    fee_eur:    float,
    hold_min:   float,
    outcome:    str,
    score:      int,
) -> None:
    """
    Claude analyseert elke gesloten trade direct na sluiting.
    Logt analyse naar DB zodat weekrapport er gebruik van kan maken.
    Stuurt GEEN WhatsApp — dat zou spam zijn.
    Alleen opgeslagen voor leeranalyse in webhook.
    """
    if not ANTHROPIC_API_KEY:
        return

    prompt = f"""
Je bent een crypto trading bot coach.
Analyseer deze gesloten trade in 2 zinnen Nederlands.

Coin:      {symbol}
Setup:     {setup_type} / Regime: {regime}
Entry:     {entry:.6f} → Exit: {exit_price:.6f}
PnL:       €{pnl_eur:.4f} (na fee €{fee_eur:.4f})
Duur:      {hold_min:.0f} minuten
Score:     {score}
Uitkomst:  {outcome}

Wat ging goed of fout aan deze trade?
Was het regime/setup combinatie logisch?
""".strip()

    analyse = _claude_analyse(prompt, max_tokens=150)
    if not analyse:
        return

    # Sla op in DB als trade notitie
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                INSERT INTO public.bot_state (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value=EXCLUDED.value, updated_at=NOW()
                """, (
                    f"trade_analyse_{symbol}_{int(time.time())}",
                    json.dumps({
                        "symbol":    symbol,
                        "outcome":   outcome,
                        "pnl_eur":   round(pnl_eur, 4),
                        "analyse":   analyse,
                        "timestamp": now_utc().isoformat(),
                    }, ensure_ascii=False),
                ))
            conn.commit()
        log(f"🧠 Claude trade analyse opgeslagen: {symbol} {outcome}")
    except Exception as e:
        log(f"⚠️ Trade analyse DB fout: {e}")


def claude_trader_health_check() -> None:
    """
    Claude controleert de live_trader configuratie bij opstarten.
    Zelfde health check gedachte als webhook claude_health_check().
    Stuurt WhatsApp als er iets kritiek mis is.
    """
    problemen = []

    if not BITVAVO_API_KEY:
        problemen.append("BITVAVO_API_KEY ontbreekt")
    if not BITVAVO_API_SECRET:
        problemen.append("BITVAVO_API_SECRET ontbreekt")
    if not DATABASE_URL:
        problemen.append("DATABASE_URL ontbreekt")
    if len(BITVAVO_API_SECRET) < 10:
        problemen.append("BITVAVO_API_SECRET lijkt te kort")
    if MAX_PER_TRADE_EUR > 10:
        problemen.append(f"MAX_PER_TRADE_EUR={MAX_PER_TRADE_EUR} is hoog voor Fase 1")
    if DAILY_STOP_LOSS_EUR > 20:
        problemen.append(f"DAILY_STOP_LOSS_EUR={DAILY_STOP_LOSS_EUR} is hoog voor Fase 1")

    if not problemen:
        log("✅ Live trader health check: alles OK")
        return

    prompt = f"""
Je bent een crypto trading bot monitor.
De live_trader.py heeft configuratieproblemen bij opstarten.

Problemen:
{chr(10).join(f'- {p}' for p in problemen)}

Limieten:
- MAX_PER_TRADE_EUR: €{MAX_PER_TRADE_EUR:.2f}
- DAILY_STOP_LOSS_EUR: €{DAILY_STOP_LOSS_EUR:.2f}
- MAX_REAL_TRADES_PER_DAY: {MAX_REAL_TRADES_PER_DAY}

Geef in 2-3 zinnen Nederlands:
- Hoe ernstig is dit?
- Wat moet de gebruiker direct doen?
""".strip()

    uitleg = _claude_analyse(prompt, max_tokens=150)
    if not uitleg:
        uitleg = "\n".join(problemen)

    send_whatsapp(
        f"🚨 LIVE TRADER STARTUP PROBLEEM\n\n"
        f"{'chr(10)'.join(problemen)}\n\n"
        f"🧠 Claude:\n{uitleg}"
    )


# ============================================================
# PUBLIEKE ALIASES — voor webhook en trade_monitor
# ============================================================
def buy_eur(
    symbol_usdt: str,
    amount_eur:  float,
    meta:        Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Hoofd BUY functie — aangeroepen door whatsapp_webhook.py."""
    return place_market_buy_eur(symbol_usdt, amount_eur, meta=meta)


def sell(
    symbol:   str,
    fraction: float = 1.0,
    meta:     Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Verkoop fractie van positie.
    fraction=1.0 = alles.
    """
    try:
        market = ensure_market_tradable(symbol)
    except ValueError as e:
        return {"ok": False, "reason": str(e), "market": ""}

    state = _load_state()
    pos   = (state.get("positions") or {}).get(symbol)

    if not pos:
        return {"ok": False, "reason": "NO_POSITION", "market": market}

    qty_total = safe_float(pos.get("qty"))
    if qty_total <= 0:
        return {"ok": False, "reason": "QTY_INVALID", "market": market}

    fraction    = max(0.0, min(1.0, float(fraction)))
    amount_base = qty_total if fraction >= 1.0 else qty_total * fraction

    return place_market_sell_base(market, amount_base)


def sell_base(market: str, amount_base: float) -> Dict[str, Any]:
    """Direct sell op basis van market en hoeveelheid."""
    return place_market_sell_base(market, amount_base)


# ============================================================
# ACCOUNT + STATUS
# ============================================================
def get_balance() -> Dict[str, Any]:
    try:
        result = _request_signed("GET", "/v2/balance")
        if result["ok"]:
            return {"ok": True, "balances": result["data"]}
        return {"ok": False, "error": result.get("data")}
    except Exception as e:
        report_error(e, "get_balance", severity="MEDIUM")
        return {"ok": False, "error": str(e)}


def get_state_summary() -> Dict[str, Any]:
    """Samenvatting van live state — voor STATUS command in webhook."""
    state = _load_state()
    return {
        "open_positions": len(state.get("positions") or {}),
        "open_trades":    len(state.get("open_trades") or []),
        "symbols":        list((state.get("positions") or {}).keys()),
    }


# ============================================================
# MAIN — startup check
# ============================================================
if __name__ == "__main__":
    print("=" * 50)
    print("Live Trader v2.0 — startup check")
    print("=" * 50)
    print(f"Bitvavo API:   {'✅' if BITVAVO_API_KEY else '❌ ONTBREEKT'}")
    print(f"Database:      {'✅' if DATABASE_URL else '❌ ONTBREEKT'}")
    print(f"Twilio:        {'✅' if TWILIO_ACCOUNT_SID else '❌ ONTBREEKT'}")
    print(f"Claude API:    {'✅' if ANTHROPIC_API_KEY else '❌ ONTBREEKT'}")
    print(f"Fee+slippage:  {TOTAL_COST_PCT*100:.2f}%")
    print(f"Max/trade:     €{MAX_PER_TRADE_EUR:.2f}")
    print(f"Max/dag:       {MAX_REAL_TRADES_PER_DAY} trades")
    print(f"Daily stop:    €{DAILY_STOP_LOSS_EUR:.2f}")
    print(f"Trading hours: {TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC")
    print(f"Data dir:      {DATA_DIR}")

    # Claude health check bij opstarten
    claude_trader_health_check()

    print("=" * 50)

    if BITVAVO_API_KEY:
        markets = get_tradable_markets()
        print(f"Tradable markets: {len(markets)}")
