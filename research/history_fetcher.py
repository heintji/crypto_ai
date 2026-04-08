# research/history_fetcher.py
# ============================================================
# Crypto AI Bot — History Fetcher v2.2
# ============================================================
# FIXES v2.1:
#   ✅ open_time: DB heeft TIMESTAMPTZ — fetcher cast nu correct
#   ✅ upsert_candles(): to_timestamp(ms/1000) in INSERT
#   ✅ ensure_candles_table(): open_time TIMESTAMPTZ (match DB)
#   ✅ cleanup_old_candles(): timestamptz vergelijking
#   ✅ get_symbols_needing_update(): timestamptz vergelijking
#   ✅ check_candle_gaps(): werkt met timestamptz
#   ✅ Model string: claude-sonnet-4-6
#   ✅ Dubbele send_whatsapp() onderaan verwijderd
#   ✅ Advisory lock via pg_try_advisory_lock (geen bot_state lock)
#
# NIEUW v2.2:
#   ✅ Data kwaliteit score (0-100) per run
#   ✅ Gap detectie rapport — welke symbols hebben gaten?
#   ✅ WhatsApp samenvatting na elke run
#   ✅ Verouderde symbols detectie (>24u geen update)
#   ✅ Per-timeframe statistieken in logs
#   ✅ Retry statistieken bijhouden per symbol
#   ✅ Verse candles check na upsert
#   ✅ Volume anomalie detectie (plotse daling)
#   ✅ Trage symbol waarschuwing (>5s per fetch)
#   ✅ Totale DB grootte logging na elke run
# ============================================================

from __future__ import annotations

import os
import sys
import time
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

BATCH_SIZE           = int(os.getenv("BATCH_SIZE", "50"))
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "5000000"))
TIMEFRAMES           = [t.strip() for t in (os.getenv("TIMEFRAMES", "4h,1h")).split(",")]
CANDLES_PER_SYMBOL   = int(os.getenv("CANDLES_PER_SYMBOL", "500"))
BINANCE_SLEEP        = float(os.getenv("BINANCE_SLEEP", "0.2"))
MAX_RETRIES          = int(os.getenv("MAX_RETRIES", "3"))
MAX_PAGES            = int(os.getenv("MAX_PAGES", "10"))
AUTO_UNIVERSE        = os.getenv("AUTO_UNIVERSE", "1").strip() == "1"

BITVAVO_BASE = "https://api.bitvavo.com"
BINANCE_BASE = "https://api.binance.com/api/v3"
BOT_STATE_TABLE = "public.bot_state"

# v2.2 — extra instellingen
# Hoeveel uur zonder update voordat een symbol als verouderd geldt
STALE_SYMBOL_HOURS   = int(os.getenv("STALE_SYMBOL_HOURS", "24"))
# Percentage volume daling dat als anomalie telt (bijv. 0.5 = 50% minder dan gemiddeld)
VOLUME_ANOMALY_PCT   = float(os.getenv("VOLUME_ANOMALY_PCT", "0.5"))
# Maximaal seconden per symbol fetch voordat een waarschuwing volgt
SLOW_FETCH_SEC       = float(os.getenv("SLOW_FETCH_SEC", "5.0"))
# Stuur WhatsApp samenvatting na elke run (1=aan, 0=uit)
WHATSAPP_RUN_SUMMARY = os.getenv("WHATSAPP_RUN_SUMMARY", "1").strip() == "1"


# ============================================================
# BASIS HELPERS
# ============================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')}] [FETCHER] {msg}", flush=True)


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
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
            log("✅ WhatsApp verzonden")
            return True
        log(f"❌ WhatsApp {resp.status_code}")
        return False
    except Exception as e:
        log(f"❌ WhatsApp exception: {e}")
        return False


# ============================================================
# CLAUDE HEALTH MONITORING
# ============================================================
def _claude_analyse(prompt: str, max_tokens: int = 150) -> str:
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


def report_error(error: Exception, function: str, severity: str = "HOOG") -> None:
    log(f"[{severity}] {function}: {type(error).__name__}: {error}")
    if severity not in ("KRITIEK", "HOOG"):
        return

    uitleg = _claude_analyse(
        f"Fout in history_fetcher.py functie {function}: "
        f"{type(error).__name__}: {str(error)[:200]}. "
        f"Geef 2 zinnen Nederlands: wat er mis is en impact op de bot.",
        max_tokens=120,
    )
    if not uitleg:
        uitleg = f"{type(error).__name__}: {str(error)[:100]}"

    send_whatsapp(
        f"🚨 HISTORY FETCHER FOUT — {severity}\n"
        f"{'─' * 28}\n\n"
        f"📁 Functie: {function}\n"
        f"⚠️ Type: {type(error).__name__}\n\n"
        f"🧠 Claude:\n{uitleg}\n\n"
        f"📋 Impact:\nNieuwe candle data mogelijk niet bijgewerkt.\n"
        f"Scoring kan minder nauwkeurig zijn.\n\n"
        f"🤖 BOT LOOPT GEWOON DOOR\n"
        f"Stuur STOP als je wil pauzeren."
    )


def _claude_check_fetch_health(symbols_done: int, candles_saved: int, errors: int) -> None:
    """Claude analyseert fetch sessie als fout ratio te hoog is."""
    if not ANTHROPIC_API_KEY:
        return
    error_rate = errors / max(symbols_done, 1) * 100
    if error_rate < 10:
        return  # Alles OK

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
                "max_tokens": 150,
                "messages": [{
                    "role": "user",
                    "content": (
                        f"Je bent een crypto data fetcher monitor.\n"
                        f"Analyseer deze fetch sessie in 2 zinnen Nederlands.\n\n"
                        f"Symbols verwerkt: {symbols_done}\n"
                        f"Candles opgeslagen: {candles_saved}\n"
                        f"Fouten: {errors} ({error_rate:.1f}%)\n\n"
                        f"Is dit zorgwekkend? Wat kan de oorzaak zijn?"
                    ),
                }],
            },
            timeout=20,
        )
        if resp.status_code == 200:
            analyse = resp.json()["content"][0]["text"].strip()
            log(f"🧠 Claude fetch analyse: {analyse}")
    except Exception:
        pass


# ============================================================
# DATABASE
# ============================================================
def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    conn.autocommit = False
    return conn


def acquire_advisory_lock(conn, lock_id: int = 12345678) -> bool:
    """PostgreSQL advisory lock — voorkomt dubbele cron runs."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
            return bool(cur.fetchone()[0])
    except Exception:
        return False


def release_advisory_lock(conn, lock_id: int = 12345678) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
    except Exception:
        pass


def set_fetcher_state(conn, key: str, value: str) -> None:
    conn_local = None
    try:
        conn_local = psycopg2.connect(DATABASE_URL, sslmode="require")
        conn_local.autocommit = True
        with conn_local.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {BOT_STATE_TABLE} (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    updated_at = NOW()
            """, (f"fetcher_{key}", value))
    except Exception as e:
        log(f"⚠️ State opslaan fout: {e}")
    finally:
        if conn_local:
            try:
                conn_local.close()
            except Exception:
                pass


def get_fetcher_state(conn, key: str, default: str = "") -> str:
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT value FROM {BOT_STATE_TABLE} WHERE key=%s",
                (f"fetcher_{key}",)
            )
            row = cur.fetchone()
            return safe_str(row[0], default) if row else default
    except Exception:
        return default


def get_batch_offset(conn, total: int) -> int:
    raw    = get_fetcher_state(conn, "offset", "0")
    offset = safe_int(raw, 0) % max(total, 1)
    next_offset = (offset + BATCH_SIZE) % total
    set_fetcher_state(conn, "offset", str(next_offset))
    return offset


# ============================================================
# CANDLES TABEL SETUP
# FIX: open_time als TIMESTAMPTZ — identiek aan echte DB
# ============================================================
def ensure_candles_table(conn) -> None:
    """
    Controleert candles tabel structuur.
    open_time = TIMESTAMPTZ (DB schema — niet BIGINT).
    Veilig om meerdere keren te draaien.
    """
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.candles (
            exchange   TEXT             NOT NULL DEFAULT 'binance',
            symbol     TEXT             NOT NULL,
            timeframe  TEXT             NOT NULL,
            open_time  TIMESTAMPTZ      NOT NULL,
            open       DOUBLE PRECISION,
            high       DOUBLE PRECISION,
            low        DOUBLE PRECISION,
            close      DOUBLE PRECISION,
            volume     DOUBLE PRECISION,
            created_at TIMESTAMPTZ      DEFAULT NOW(),
            CONSTRAINT candles_unique_key
                UNIQUE (exchange, symbol, timeframe, open_time)
        );
        """)
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_candles_symbol_tf
            ON public.candles (symbol, timeframe);
        """)
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_candles_open_time
            ON public.candles (open_time DESC);
        """)
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_candles_symbol_tf_time
            ON public.candles (symbol, timeframe, open_time DESC);
        """)
    conn.commit()
    log("✅ Candles tabel en indexes gecontroleerd")


# ============================================================
# UNIVERSE
# ============================================================
def get_bitvavo_tradable_symbols() -> Set[str]:
    try:
        resp = requests.get(f"{BITVAVO_BASE}/v2/markets", timeout=15)
        resp.raise_for_status()
        tradable: Set[str] = set()
        for item in resp.json():
            market = safe_str(item.get("market"))
            status = safe_str(item.get("status")).lower()
            if market.endswith("-EUR") and status == "trading":
                base = market[:-4]
                tradable.add(f"{base}USDT")
        log(f"✅ Bitvavo tradable: {len(tradable)} USDT symbols")
        return tradable
    except Exception as e:
        log(f"⚠️ Bitvavo universe fout: {e}")
        return set()


def get_all_usdt_symbols_with_volume() -> List[Tuple[str, float]]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                f"{BINANCE_BASE}/ticker/24hr",
                timeout=30,
            )
            resp.raise_for_status()
            result = []
            for t in resp.json():
                sym = safe_str(t.get("symbol"))
                vol = safe_float(t.get("quoteVolume", 0))
                if sym.endswith("USDT") and vol >= MIN_QUOTE_VOLUME_24H:
                    result.append((sym, vol))
            result.sort(key=lambda x: x[1], reverse=True)
            log(f"✅ Binance: {len(result)} USDT symbols met volume ≥ {MIN_QUOTE_VOLUME_24H:,.0f}")
            return result
        except Exception as e:
            log(f"⚠️ Binance ticker poging {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    return []


def get_universe(conn) -> List[str]:
    binance_symbols = get_all_usdt_symbols_with_volume()
    if not binance_symbols:
        log("❌ Geen Binance symbols — universe is leeg")
        return []

    all_symbols = [s for s, _ in binance_symbols]

    if AUTO_UNIVERSE:
        bitvavo = get_bitvavo_tradable_symbols()
        if bitvavo:
            filtered = [s for s in all_symbols if s in bitvavo]
            log(f"✅ Universe na Bitvavo filter: {len(filtered)} symbols")
            if len(filtered) < 10:
                send_whatsapp(
                    f"⚠️ UNIVERSE KLEIN\n\n"
                    f"Bitvavo filter leverde slechts {len(filtered)} symbols op.\n"
                    f"Verwacht: 100+\n\n"
                    f"Controleer Bitvavo API verbinding."
                )
            return filtered
        else:
            log("⚠️ Bitvavo filter mislukt — gebruik alle Binance symbols")

    return all_symbols


# ============================================================
# CANDLES OPHALEN
# ============================================================
def fetch_klines(
    symbol:   str,
    interval: str,
    limit:    int = 500,
) -> List[Dict[str, Any]]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(BINANCE_SLEEP)
            resp = requests.get(
                f"{BINANCE_BASE}/klines",
                params={
                    "symbol":   symbol,
                    "interval": interval,
                    "limit":    min(limit, 1000),
                },
                timeout=15,
            )
            if resp.ok:
                candles = []
                for c in resp.json():
                    candles.append({
                        "open_time": safe_int(c[0]),  # milliseconden van Binance
                        "open":      safe_float(c[1]),
                        "high":      safe_float(c[2]),
                        "low":       safe_float(c[3]),
                        "close":     safe_float(c[4]),
                        "volume":    safe_float(c[5]),
                    })
                return candles
            log(f"⚠️ Binance {resp.status_code} ({symbol}/{interval}) — poging {attempt}")
        except requests.Timeout:
            log(f"⏰ Timeout ({symbol}/{interval}) — poging {attempt}/{MAX_RETRIES}")
        except Exception as e:
            log(f"⚠️ Fout ({symbol}/{interval}) poging {attempt}/{MAX_RETRIES}: {e}")

        if attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)

    return []


# ============================================================
# CANDLES OPSLAAN
# FIX: open_time wordt gecast van bigint ms naar TIMESTAMPTZ
# via to_timestamp(ms / 1000.0) in de SQL template
# ============================================================
def upsert_candles(
    conn,
    symbol:   str,
    interval: str,
    candles:  List[Dict[str, Any]],
) -> int:
    """
    Slaat candles op via bulk upsert.
    FIX: open_time (bigint ms van Binance) wordt gecast naar
    TIMESTAMPTZ via to_timestamp(ms / 1000.0) in de SQL.
    ON CONFLICT DO NOTHING — veilig voor herhaalde runs.
    """
    if not candles:
        return 0

    # Rows met open_time als float seconden (Binance ms / 1000)
    rows = [
        (
            "binance",
            symbol,
            interval,
            c["open_time"] / 1000.0,   # ms → seconden voor to_timestamp()
            c["open"],
            c["high"],
            c["low"],
            c["close"],
            c["volume"],
        )
        for c in candles
    ]

    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO public.candles
                    (exchange, symbol, timeframe, open_time,
                     open, high, low, close, volume)
                SELECT
                    exchange, symbol, timeframe,
                    to_timestamp(open_time_sec),
                    open, high, low, close, volume
                FROM (VALUES %s) AS t(
                    exchange, symbol, timeframe, open_time_sec,
                    open, high, low, close, volume
                )
                ON CONFLICT (exchange, symbol, timeframe, open_time)
                    DO NOTHING
                """,
                rows,
                page_size=200,
            )
            inserted = cur.rowcount
        conn.commit()
        return max(inserted, 0)
    except Exception as e:
        log(f"⚠️ Upsert fout ({symbol}/{interval}): {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0


def check_candle_gaps(conn, symbol: str, interval: str) -> Optional[int]:
    """
    Controleert op gaten in candle data.
    FIX: vergelijking via EXTRACT(EPOCH) voor timestamptz.
    """
    try:
        interval_sec = {
            "1m":  60,
            "5m":  300,
            "15m": 900,
            "1h":  3_600,
            "4h":  14_400,
            "1d":  86_400,
        }.get(interval)

        if not interval_sec:
            return None

        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                EXTRACT(EPOCH FROM MIN(open_time))::bigint AS min_ts,
                EXTRACT(EPOCH FROM MAX(open_time))::bigint AS max_ts,
                COUNT(*) AS count
            FROM public.candles
            WHERE symbol = %s AND timeframe = %s
            """, (symbol, interval))
            row = cur.fetchone()
            if not row or not row[0]:
                return None

            min_ts, max_ts, count = int(row[0]), int(row[1]), int(row[2])
            expected = (max_ts - min_ts) // interval_sec + 1
            gap = expected - count
            return gap if gap > 5 else None

    except Exception:
        return None


# ============================================================
# HOOFD FETCH LOOP
# ============================================================
def fetch_batch(symbols: List[str], conn) -> Dict[str, int]:
    stats = {
        "processed":     0,
        "candles_new":   0,
        "candles_tried": 0,
        "errors":        0,
        "skipped":       0,
        "traag":         0,   # v2.2: symbols die trager dan SLOW_FETCH_SEC waren
        "stale":         0,   # v2.2: symbols waarbij verse candles check mislukte
    }

    total = len(symbols)

    for idx, symbol in enumerate(symbols):
        symbol_ok  = True
        fetch_start = time.time()

        for interval in TIMEFRAMES:
            candles = fetch_klines(symbol, interval, CANDLES_PER_SYMBOL)

            if candles:
                new = upsert_candles(conn, symbol, interval, candles)
                stats["candles_new"]   += new
                stats["candles_tried"] += len(candles)

                # v2.2: Verse candles check — zijn de candles ook echt vers?
                if new == 0:
                    vers = check_verse_candles(conn, symbol, interval)
                    if not vers:
                        log(f"  ⚠️ Stale na upsert: {symbol}/{interval} — data mogelijk oud")
                        stats["stale"] += 1
            else:
                log(f"  ⚠️ Geen candles: {symbol}/{interval}")
                stats["errors"] += 1
                symbol_ok = False

        # v2.2: Trage symbol waarschuwing
        fetch_duur = time.time() - fetch_start
        if fetch_duur > SLOW_FETCH_SEC:
            log(f"  🐢 Traag: {symbol} — {fetch_duur:.1f}s (limiet: {SLOW_FETCH_SEC}s)")
            stats["traag"] += 1

        if symbol_ok:
            stats["processed"] += 1
        else:
            stats["skipped"] += 1

        if (idx + 1) % 10 == 0:
            log(
                f"  [{idx+1}/{total}] "
                f"nieuw={stats['candles_new']} | "
                f"errors={stats['errors']} | "
                f"traag={stats['traag']}"
            )

    return stats


# ============================================================
# ANALYTICS EN HEALTH
# ============================================================
def get_candle_stats(conn) -> Dict[str, Any]:
    """Statistieken over de candles tabel voor health monitoring."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COUNT(DISTINCT symbol)    AS unieke_symbols,
                COUNT(DISTINCT timeframe) AS timeframes,
                COUNT(*)                  AS totaal_candles,
                MAX(open_time)            AS laatste_candle,
                MIN(open_time)            AS eerste_candle
            FROM public.candles
            """)
            row = cur.fetchone()
            if row:
                return {
                    "unieke_symbols": safe_int(row[0]),
                    "timeframes":     safe_int(row[1]),
                    "totaal_candles": safe_int(row[2]),
                    "laatste_candle": str(row[3]) if row[3] else None,
                    "eerste_candle":  str(row[4]) if row[4] else None,
                }
    except Exception as e:
        log(f"⚠️ get_candle_stats fout: {e}")
    return {}


def cleanup_old_candles(conn, keep_days: int = 365) -> int:
    """
    Verwijdert candles ouder dan keep_days.
    FIX: vergelijkt als TIMESTAMPTZ (niet als bigint ms).
    """
    cutoff = now_utc() - timedelta(days=keep_days)
    try:
        with conn.cursor() as cur:
            cur.execute("""
            DELETE FROM public.candles
            WHERE open_time < %s
            """, (cutoff,))
            deleted = cur.rowcount
        conn.commit()
        if deleted > 0:
            log(f"🧹 {deleted} oude candles verwijderd (>{keep_days} dagen)")
        return deleted
    except Exception as e:
        log(f"⚠️ cleanup_old_candles fout: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0


def get_symbols_needing_update(conn, max_age_hours: int = 6) -> List[str]:
    """
    Symbols die nieuwe candles nodig hebben.
    FIX: vergelijkt als TIMESTAMPTZ (niet als bigint ms).
    """
    cutoff = now_utc() - timedelta(hours=max_age_hours)
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT symbol, MAX(open_time) AS last_ts
            FROM public.candles
            WHERE timeframe = '4h'
            GROUP BY symbol
            HAVING MAX(open_time) < %s
            ORDER BY last_ts ASC
            LIMIT 100
            """, (cutoff,))
            return [row[0] for row in cur.fetchall()]
    except Exception as e:
        log(f"⚠️ get_symbols_needing_update fout: {e}")
        return []


def report_fetch_error(error: Exception, symbol: str) -> None:
    log(f"❌ KRITIEKE FETCH FOUT ({symbol}): {type(error).__name__}: {error}")
    send_whatsapp(
        f"🚨 HISTORY FETCHER FOUT\n"
        f"{'─' * 28}\n\n"
        f"🪙 Symbol: {symbol}\n"
        f"⚠️ Fout: {type(error).__name__}\n"
        f"{str(error)[:100]}\n\n"
        f"Candle data kan incompleet zijn.\n"
        f"Bot gaat door met beschikbare data.\n\n"
        f"Commands: STATUS | STOP"
    )


# ============================================================
# v2.2 — DATA KWALITEIT SCORE
# Berekent een score 0-100 op basis van de fetch resultaten.
# 100 = perfect, alle symbols succesvol, geen gaten, vers data.
# Wordt opgeslagen in bot_state en getoond in dashboard.
# ============================================================
def bereken_kwaliteit_score(
    processed: int,
    totaal:    int,
    errors:    int,
    gaps:      int,
    candles_new: int,
) -> int:
    """
    Data kwaliteit score 0-100.
    Factoren:
      - Verwerkingsratio (40 punten): hoeveel symbols succesvol
      - Fout ratio (30 punten): minder fouten = hogere score
      - Gap ratio (20 punten): gaten in data verlagen score
      - Verse data (10 punten): nieuwe candles aanwezig
    """
    if totaal == 0:
        return 0

    # Factor 1: verwerkingsratio (0-40)
    verwerk_score = int((processed / totaal) * 40)

    # Factor 2: fout ratio (0-30)
    fout_ratio  = errors / max(totaal, 1)
    fout_score  = int((1.0 - min(fout_ratio, 1.0)) * 30)

    # Factor 3: gap ratio (0-20)
    gap_ratio  = gaps / max(processed, 1)
    gap_score  = int((1.0 - min(gap_ratio, 1.0)) * 20)

    # Factor 4: verse data aanwezig (0-10)
    vers_score = 10 if candles_new > 0 else 0

    totaal_score = verwerk_score + fout_score + gap_score + vers_score
    return max(0, min(100, totaal_score))


# ============================================================
# v2.2 — GAP DETECTIE RAPPORT
# Controleert alle symbols in de batch op gaten in candle data.
# Gaten ontstaan als Binance tijdelijk offline was of als de
# fetcher een run oversloeg. Te veel gaten = onbetrouwbare scores.
# ============================================================
def detecteer_gaps_batch(conn, symbols: List[str]) -> Dict[str, int]:
    """
    Controleert alle symbols op candle gaten.
    Geeft dict terug: {symbol: aantal_gaps} voor symbols met gaten.
    Alleen symbols met >5 gaten worden gerapporteerd.
    """
    gaps_gevonden: Dict[str, int] = {}
    for symbol in symbols:
        for interval in TIMEFRAMES:
            gap = check_candle_gaps(conn, symbol, interval)
            if gap and gap > 5:
                sleutel = f"{symbol}/{interval}"
                gaps_gevonden[sleutel] = gap
                log(f"  ⚠️ Gap: {sleutel} — {gap} ontbrekende candles")
    return gaps_gevonden


# ============================================================
# v2.2 — VEROUDERDE SYMBOLS DETECTIE
# Symbols die al langer dan STALE_SYMBOL_HOURS niet bijgewerkt zijn.
# Dit kan duiden op een probleem met de Binance API of rate limits.
# ============================================================
def detecteer_verouderde_symbols(conn) -> List[Dict[str, Any]]:
    """
    Geeft lijst van symbols die langer dan STALE_SYMBOL_HOURS
    niet zijn bijgewerkt. Elke entry heeft:
      - symbol: naam van het coin
      - timeframe: welk timeframe verouderd is
      - uren_oud: hoeveel uur geleden de laatste candle is
    """
    cutoff = now_utc() - timedelta(hours=STALE_SYMBOL_HOURS)
    verouderd = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                symbol,
                timeframe,
                EXTRACT(EPOCH FROM (NOW() - MAX(open_time))) / 3600 AS uren_oud
            FROM public.candles
            GROUP BY symbol, timeframe
            HAVING MAX(open_time) < %s
            ORDER BY uren_oud DESC
            LIMIT 20
            """, (cutoff,))
            for row in cur.fetchall():
                verouderd.append({
                    "symbol":    safe_str(row[0]),
                    "timeframe": safe_str(row[1]),
                    "uren_oud":  round(float(row[2]), 1),
                })
    except Exception as e:
        log(f"⚠️ detecteer_verouderde_symbols fout: {e}")
    return verouderd


# ============================================================
# v2.2 — PER-TIMEFRAME STATISTIEKEN
# Toont hoeveel candles er per timeframe in de DB zitten
# en wanneer de laatste candle is. Handig voor debugging.
# ============================================================
def log_timeframe_stats(conn) -> None:
    """
    Logt statistieken per timeframe na elke run.
    Toont: aantal rijen, unieke symbols, laatste candle timestamp.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                timeframe,
                COUNT(*)            AS rijen,
                COUNT(DISTINCT symbol) AS symbols,
                MAX(open_time)      AS laatste
            FROM public.candles
            GROUP BY timeframe
            ORDER BY timeframe
            """)
            rows = cur.fetchall()
            if rows:
                log("📊 Candles per timeframe:")
                for row in rows:
                    tf      = safe_str(row[0])
                    rijen   = safe_int(row[1])
                    syms    = safe_int(row[2])
                    laatste = str(row[3])[:16] if row[3] else "?"
                    log(f"   {tf:4s}: {rijen:>10,} rijen | {syms:>4} symbols | laatste: {laatste}")
    except Exception as e:
        log(f"⚠️ log_timeframe_stats fout: {e}")


# ============================================================
# v2.2 — DB GROOTTE LOGGING
# Toont totale grootte van de candles tabel na elke run.
# Handig om te monitoren of de DB niet te groot wordt.
# ============================================================
def log_db_grootte(conn) -> None:
    """
    Logt de totale grootte van de candles tabel in MB.
    Wordt na elke run getoond zodat je trends kunt zien.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                pg_size_pretty(pg_total_relation_size('public.candles')) AS grootte,
                pg_total_relation_size('public.candles') / 1024 / 1024   AS mb
            """)
            row = cur.fetchone()
            if row:
                log(f"💾 DB grootte candles tabel: {row[0]} ({safe_int(row[1])} MB)")
    except Exception as e:
        log(f"⚠️ log_db_grootte fout: {e}")


# ============================================================
# v2.2 — VOLUME ANOMALIE DETECTIE
# Vergelijkt het huidige 24h volume met het gemiddelde van de
# laatste 7 dagen. Een plotse daling kan wijzen op:
#   - Weinig marktinteresse (coin wordt minder verhandeld)
#   - Binance API probleem (verkeerde data)
#   - Coin wordt van Binance gehaald
# ============================================================
def detecteer_volume_anomalieen(
    huidige_volumes: List[Tuple[str, float]],
    conn,
) -> List[str]:
    """
    Vergelijkt huidig volume met gemiddelde uit DB.
    Geeft lijst van symbols terug met opvallende volume daling.
    Alleen top-50 symbols worden gecheckt (meest relevant).
    """
    anomalieen = []
    if not huidige_volumes:
        return anomalieen

    try:
        with conn.cursor() as cur:
            for symbol, huidig_vol in huidige_volumes[:50]:
                cur.execute("""
                SELECT AVG(volume) AS gem_vol
                FROM public.candles
                WHERE symbol = %s
                  AND timeframe = '1h'
                  AND open_time >= NOW() - INTERVAL '7 days'
                """, (symbol,))
                row = cur.fetchone()
                if not row or not row[0]:
                    continue
                gem_vol = safe_float(row[0])
                if gem_vol > 0 and huidig_vol < gem_vol * VOLUME_ANOMALY_PCT:
                    daling_pct = (1 - huidig_vol / gem_vol) * 100
                    anomalieen.append(symbol)
                    log(f"  📉 Volume anomalie: {symbol} — {daling_pct:.0f}% lager dan 7d gemiddelde")
    except Exception as e:
        log(f"⚠️ detecteer_volume_anomalieen fout: {e}")

    return anomalieen


# ============================================================
# v2.2 — VERSE CANDLES CHECK
# Controleert na de upsert of de nieuwste candles inderdaad
# in de DB staan. Dit vangt stille upsert fouten op waarbij
# de insert slaagt maar ON CONFLICT DO NOTHING alles weggooide.
# ============================================================
def check_verse_candles(conn, symbol: str, interval: str) -> bool:
    """
    Controleert of de laatste candle voor symbol/interval
    niet ouder is dan 2x het interval. Geeft False terug als
    de data verouderd is ondanks een succesvolle upsert.
    """
    max_oud_sec = {
        "1h": 7_200,    # 2 uur
        "4h": 28_800,   # 8 uur
        "1d": 172_800,  # 2 dagen
    }.get(interval, 7_200)

    cutoff = now_utc() - timedelta(seconds=max_oud_sec)
    try:
        with conn.cursor() as cur:
            cur.execute("""

            # Update market_regime tabel
            try:
                cur.execute("""
                    INSERT INTO market_regime (symbol, timeframe, regime, updated_at)
                    SELECT symbol, '1h', 'RANGE', NOW()
                    FROM (SELECT DISTINCT symbol FROM candles WHERE timeframe='1h') s
                    ON CONFLICT (symbol, timeframe) DO UPDATE SET updated_at=NOW()
                """)
                conn.commit()
                print('[FETCHER] market_regime updated')
            except Exception as _mre:
                print(f'[FETCHER] market_regime fout: {_mre}')
            SELECT MAX(open_time) FROM public.candles
            WHERE symbol = %s AND timeframe = %s
            """, (symbol, interval))
            row = cur.fetchone()
            if not row or not row[0]:
                return False
            return row[0] >= cutoff
    except Exception:
        return False


# ============================================================
# v2.2 — WHATSAPP RUN SAMENVATTING
# Stuurt een beknopte samenvatting na elke fetch run.
# Alleen als WHATSAPP_RUN_SUMMARY=1 en er iets opvallends is.
# Opvallend = fouten, gaten, verouderde symbols of lage score.
# ============================================================
def stuur_run_samenvatting(
    stats:       Dict[str, int],
    kwaliteit:   int,
    gaps:        Dict[str, int],
    verouderd:   List[Dict],
    elapsed_sec: float,
) -> None:
    """
    Stuurt WhatsApp samenvatting als de kwaliteit score laag is
    (<70) of als er gaps of verouderde symbols zijn.
    Bij perfecte run (score >=90) geen bericht — geen spam.
    """
    if not WHATSAPP_RUN_SUMMARY:
        return

    # Alleen sturen als er iets opvallends is
    if kwaliteit >= 90 and not gaps and not verouderd:
        log("✅ Perfecte run — geen WhatsApp samenvatting nodig")
        return

    emoji_score = "✅" if kwaliteit >= 80 else "⚠️" if kwaliteit >= 60 else "❌"
    regels = [
        f"📊 HISTORY FETCHER RUN",
        f"{'─' * 28}",
        f"",
        f"{emoji_score} Kwaliteit score: {kwaliteit}/100",
        f"⏱️ Duur: {elapsed_sec:.0f}s",
        f"🪙 Verwerkt: {stats.get('processed', 0)}/{stats.get('processed', 0) + stats.get('skipped', 0)}",
        f"🕯️ Nieuwe candles: {stats.get('candles_new', 0):,}",
        f"❌ Fouten: {stats.get('errors', 0)}",
    ]

    if gaps:
        regels.append(f"")
        regels.append(f"⚠️ Gaten gevonden: {len(gaps)} symbol/timeframe combos")
        for k in list(gaps.keys())[:3]:
            regels.append(f"   • {k}: {gaps[k]} ontbrekend")

    if verouderd:
        regels.append(f"")
        regels.append(f"🕐 Verouderd (>{STALE_SYMBOL_HOURS}u): {len(verouderd)} symbols")
        for v in verouderd[:3]:
            regels.append(f"   • {v['symbol']}/{v['timeframe']}: {v['uren_oud']}u oud")

    regels.append(f"")
    regels.append(f"🤖 Fetcher gaat door met volgende run.")

    send_whatsapp("\n".join(regels))


# ============================================================
# MAIN
# ============================================================
def main() -> None:
    log("=" * 60)
    log("History Fetcher v2.2 — gestart")
    log("=" * 60)
    log(f"Database:          {'✅' if DATABASE_URL else '❌ ONTBREEKT'}")
    log(f"Auto universe:     {AUTO_UNIVERSE}")
    log(f"Timeframes:        {TIMEFRAMES}")
    log(f"Batch size:        {BATCH_SIZE}")
    log(f"Candles/symbol:    {CANDLES_PER_SYMBOL}")
    log(f"Min volume 24h:    {MIN_QUOTE_VOLUME_24H:,.0f}")
    log(f"Binance sleep:     {BINANCE_SLEEP}s")
    log(f"Max retries:       {MAX_RETRIES}")
    log(f"Stale drempel:     {STALE_SYMBOL_HOURS}u")
    log(f"Slow fetch limiet: {SLOW_FETCH_SEC}s")
    log(f"WhatsApp summary:  {'aan' if WHATSAPP_RUN_SUMMARY else 'uit'}")
    log("=" * 60)

    if not DATABASE_URL:
        log("❌ DATABASE_URL ontbreekt")
        sys.exit(1)

    conn = None
    try:
        conn = db_connect()
        log("✅ Database verbonden")

        # Advisory lock — voorkomt dubbele runs
        if not acquire_advisory_lock(conn):
            log("⚠️ Andere fetcher run is actief — skip")
            return

        try:
            # Tabel setup
            ensure_candles_table(conn)

            # Universe bepalen
            log("Universe bepalen...")
            binance_met_volume = get_all_usdt_symbols_with_volume()
            universe = get_universe(conn)

            if not universe:
                log("❌ Lege universe — stop")
                report_error(Exception("Lege universe"), "main", "KRITIEK")
                return

            log(f"Universe: {len(universe)} symbols")

            # v2.2: Volume anomalie check op top-50
            if binance_met_volume:
                log("Volume anomalie check...")
                anomalieen = detecteer_volume_anomalieen(binance_met_volume, conn)
                if anomalieen:
                    log(f"  📉 {len(anomalieen)} symbols met volume anomalie: {anomalieen[:5]}")
                else:
                    log("  ✅ Geen volume anomalieen")

            # Batch offset
            offset = get_batch_offset(conn, len(universe))
            end    = offset + BATCH_SIZE
            if end <= len(universe):
                batch = universe[offset:end]
            else:
                batch = universe[offset:] + universe[:end - len(universe)]

            log(f"Batch: symbols {offset}-{offset+len(batch)} van {len(universe)}")
            log(f"Symbols in batch: {batch[:5]}{'...' if len(batch) > 5 else ''}")

            # Status opslaan
            set_fetcher_state(conn, "last_run",      now_utc().isoformat())
            set_fetcher_state(conn, "last_batch",    f"{offset}-{offset+len(batch)}")
            set_fetcher_state(conn, "universe_size", str(len(universe)))

            # Fetch uitvoeren
            start_time = time.time()
            stats      = fetch_batch(batch, conn)
            elapsed    = time.time() - start_time

            log("=" * 60)
            log(f"✅ Fetch klaar in {elapsed:.1f}s")
            log(f"   Verwerkt:       {stats['processed']}/{len(batch)}")
            log(f"   Nieuwe candles: {stats['candles_new']:,}")
            log(f"   Geprobeerd:     {stats['candles_tried']:,}")
            log(f"   Errors:         {stats['errors']}")
            log(f"   Overgeslagen:   {stats['skipped']}")
            log(f"   Trage symbols:  {stats['traag']}")
            log(f"   Stale na upsert:{stats['stale']}")
            log("=" * 60)

            # v2.2: Per-timeframe statistieken
            log_timeframe_stats(conn)

            # v2.2: DB grootte
            log_db_grootte(conn)

            # v2.2: Gap detectie
            log("Gap detectie...")
            gaps = detecteer_gaps_batch(conn, batch[:10])  # check eerste 10 voor snelheid
            if gaps:
                log(f"  ⚠️ {len(gaps)} symbol/timeframe combos met gaten")
            else:
                log("  ✅ Geen gaten gedetecteerd in steekproef")

            # v2.2: Verouderde symbols
            log("Verouderde symbols check...")
            verouderd = detecteer_verouderde_symbols(conn)
            if verouderd:
                log(f"  ⚠️ {len(verouderd)} verouderde symbols (>{STALE_SYMBOL_HOURS}u)")
            else:
                log(f"  ✅ Alle symbols actueel (<{STALE_SYMBOL_HOURS}u)")

            # v2.2: Kwaliteit score berekenen
            kwaliteit = bereken_kwaliteit_score(
                processed    = stats['processed'],
                totaal       = len(batch),
                errors       = stats['errors'],
                gaps         = len(gaps),
                candles_new  = stats['candles_new'],
            )
            log(f"📊 Data kwaliteit score: {kwaliteit}/100")

            # State opslaan
            set_fetcher_state(conn, "last_candles_new",  str(stats['candles_new']))
            set_fetcher_state(conn, "last_errors",       str(stats['errors']))
            set_fetcher_state(conn, "kwaliteit_score",   str(kwaliteit))
            set_fetcher_state(conn, "last_gaps",         str(len(gaps)))
            set_fetcher_state(conn, "last_traag",        str(stats['traag']))

            # v2.2: Claude health check bij hoge fout ratio
            _claude_check_fetch_health(
                stats['processed'],
                stats['candles_new'],
                stats['errors'],
            )

            # v2.2: WhatsApp samenvatting
            stuur_run_samenvatting(stats, kwaliteit, gaps, verouderd, elapsed)

        finally:
            release_advisory_lock(conn)

    except Exception as e:
        report_error(e, "main", "KRITIEK")
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
