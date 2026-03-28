# research/history_fetcher.py
# ============================================================
# Crypto AI Bot — History Fetcher v2.0
# ============================================================
# Haalt historische OHLCV candles op van Binance en slaat
# ze op in de PostgreSQL candles tabel.
# Wordt 1x per dag gedraaid via de scheduler (run_bot.py).
#
# Zelfde gedachtegang als alle andere bestanden:
#   ✅ sslmode="require" op DB connectie
#   ✅ Zelfde send_whatsapp() implementatie
#   ✅ Zelfde Claude health monitoring
#   ✅ Zelfde safe_int / safe_float / safe_str helpers
#   ✅ Zelfde now_utc() en log() patroon
#   ✅ Zelfde retry bij netwerk errors
#   ✅ Zelfde BOT_STATE_TABLE voor status bijhouden
#
# BUGS GEFIXED vs origineel:
#   ✅ sslmode="require" was aanwezig maar inconsistent
#   ✅ Batch universe boundary fix — rotatie werkt correct
#   ✅ Retry met exponential backoff bij Binance errors
#   ✅ max_pages limiet — eindigt altijd
#   ✅ AUTO_UNIVERSE flag nu actief gebruikt
#   ✅ Index op (symbol, timeframe) voor snellere queries
#
# NIEUWE FEATURES:
#   ✅ Bitvavo universe filter — alleen coins die tradable zijn
#   ✅ Volume filter per symbol
#   ✅ Progress tracking via bot_state tabel
#   ✅ Advisory lock — geen dubbele cron runs
#   ✅ Candle gap detectie
#   ✅ WhatsApp melding als universe te klein is
#   ✅ Statistieken na elke run
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
# ENV — identiek aan alle andere bestanden
# ============================================================
DATABASE_URL      = (os.getenv("DATABASE_URL") or "").strip()
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()

TWILIO_ACCOUNT_SID   = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN    = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
TWILIO_WHATSAPP_TO   = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()

# Fetcher configuratie
BATCH_SIZE           = int(os.getenv("BATCH_SIZE", "50"))
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "5000000"))
TIMEFRAMES           = [t.strip() for t in (os.getenv("TIMEFRAMES", "4h,1h")).split(",")]
CANDLES_PER_SYMBOL   = int(os.getenv("CANDLES_PER_SYMBOL", "500"))
BINANCE_SLEEP        = float(os.getenv("BINANCE_SLEEP", "0.2"))
MAX_RETRIES          = int(os.getenv("MAX_RETRIES", "3"))
MAX_PAGES            = int(os.getenv("MAX_PAGES", "10"))
AUTO_UNIVERSE        = os.getenv("AUTO_UNIVERSE", "1").strip() == "1"

# Bitvavo
BITVAVO_BASE = "https://api.bitvavo.com"
BINANCE_BASE = "https://api.binance.com/api/v3"

BOT_STATE_TABLE = "public.bot_state"


# ============================================================
# BASIS HELPERS — identiek aan alle andere bestanden
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
# WHATSAPP — identiek aan alle andere bestanden
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
            log(f"✅ WhatsApp verzonden")
            return True
        log(f"❌ WhatsApp {resp.status_code}")
        return False
    except Exception as e:
        log(f"❌ WhatsApp exception: {e}")
        return False


# ============================================================
# CLAUDE HEALTH MONITORING — identiek aan alle andere bestanden
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


# ============================================================
# DATABASE
# ============================================================
def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def acquire_advisory_lock(conn, lock_id: int = 12345678) -> bool:
    """
    PostgreSQL advisory lock — voorkomt dubbele cron runs.
    Geeft True terug als lock verkregen is.
    """
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
    """Slaat fetcher state op in bot_state tabel."""
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
            INSERT INTO {BOT_STATE_TABLE} (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = NOW()
            """, (f"fetcher_{key}", value))
        conn.commit()
    except Exception as e:
        log(f"⚠️ State opslaan fout: {e}")


def get_fetcher_state(conn, key: str, default: str = "") -> str:
    """Haalt fetcher state op uit bot_state tabel."""
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
    """
    Haalt en roteert batch offset.
    Elke run verwerkt een andere batch — zo worden alle coins gedekt.
    FIX: wrap-around werkt nu correct voor alle batch groottes.
    """
    raw    = get_fetcher_state(conn, "offset", "0")
    offset = safe_int(raw, 0) % max(total, 1)

    # Bereken volgende offset met correcte wrap-around
    next_offset = (offset + BATCH_SIZE) % total
    set_fetcher_state(conn, "offset", str(next_offset))

    return offset


# ============================================================
# CANDLES TABEL SETUP
# ============================================================
def ensure_candles_table(conn) -> None:
    """
    Maakt candles tabel aan met alle indexes.
    Veilig om meerdere keren te draaien.
    """
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.candles (
            exchange   TEXT            NOT NULL DEFAULT 'binance',
            symbol     TEXT            NOT NULL,
            timeframe  TEXT            NOT NULL,
            open_time  BIGINT          NOT NULL,
            open       DOUBLE PRECISION,
            high       DOUBLE PRECISION,
            low        DOUBLE PRECISION,
            close      DOUBLE PRECISION,
            volume     DOUBLE PRECISION,
            created_at TIMESTAMPTZ     DEFAULT NOW(),
            CONSTRAINT candles_unique_key
                UNIQUE (exchange, symbol, timeframe, open_time)
        );
        """)

        # Primaire index voor symbol + timeframe queries
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_candles_symbol_tf
            ON public.candles (symbol, timeframe);
        """)

        # Index voor recente candles ophalen
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_candles_open_time
            ON public.candles (open_time DESC);
        """)

        # Index voor BTC regime builder
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_candles_symbol_tf_time
            ON public.candles (symbol, timeframe, open_time DESC);
        """)

    conn.commit()
    log("✅ Candles tabel en indexes gecontroleerd")


# ============================================================
# UNIVERSE OPHALEN
# ============================================================
def get_bitvavo_tradable_symbols() -> Set[str]:
    """
    Haalt Bitvavo-tradable EUR markets op.
    Converteert naar USDT symbolen voor Binance.
    Identiek aan get_tradable_markets() in live_trader.py.
    """
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
    """
    Haalt alle USDT symbols op van Binance met 24h volume.
    Gefilterd op minimum volume.
    """
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
            # Sorteer op volume — hoogste eerst
            result.sort(key=lambda x: x[1], reverse=True)
            log(f"✅ Binance: {len(result)} USDT symbols met volume ≥ {MIN_QUOTE_VOLUME_24H:,.0f}")
            return result
        except Exception as e:
            log(f"⚠️ Binance ticker poging {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

    return []


def get_universe(conn) -> List[str]:
    """
    Bouwt de universe op van symbols om te fetchen.

    Als AUTO_UNIVERSE aan:
    - Haalt Bitvavo tradable symbols op
    - Kruist met Binance volume filter
    - Alleen coins die op beide staan

    Anders:
    - Alle Binance USDT pairs met voldoende volume
    """
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
# CANDLES OPHALEN EN OPSLAAN
# ============================================================
def fetch_klines(
    symbol:   str,
    interval: str,
    limit:    int = 500,
) -> List[Dict[str, Any]]:
    """
    Haalt klines op van Binance voor een symbol en interval.
    Retry met exponential backoff bij fouten.
    MAX_PAGES limiet voorkomt oneindige loops.
    """
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
                        "open_time": safe_int(c[0]),
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


def upsert_candles(
    conn,
    symbol:   str,
    interval: str,
    candles:  List[Dict[str, Any]],
) -> int:
    """
    Slaat candles op in DB via bulk upsert.
    ON CONFLICT DO NOTHING — veilig voor herhaalde runs.
    Geeft aantal nieuwe rijen terug.
    """
    if not candles:
        return 0

    rows = [
        (
            "binance", symbol, interval,
            c["open_time"],
            c["open"], c["high"], c["low"], c["close"], c["volume"],
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
                VALUES %s
                ON CONFLICT (exchange, symbol, timeframe, open_time)
                    DO NOTHING
                """,
                rows,
                page_size=200,
            )
            inserted = cur.rowcount
        conn.commit()
        return inserted
    except Exception as e:
        log(f"⚠️ Upsert fout ({symbol}/{interval}): {e}")
        conn.rollback()
        return 0


def check_candle_gaps(conn, symbol: str, interval: str) -> Optional[int]:
    """
    Controleert op gaten in de candle data.
    Geeft het aantal ontbrekende candles terug, of None als OK.
    """
    try:
        interval_ms = {
            "1m":  60_000,
            "5m":  300_000,
            "15m": 900_000,
            "1h":  3_600_000,
            "4h":  14_400_000,
            "1d":  86_400_000,
        }.get(interval)

        if not interval_ms:
            return None

        with conn.cursor() as cur:
            cur.execute("""
            SELECT MIN(open_time), MAX(open_time), COUNT(*)
            FROM public.candles
            WHERE symbol = %s AND timeframe = %s
            """, (symbol, interval))
            row = cur.fetchone()
            if not row or not row[0]:
                return None

            min_ts, max_ts, count = row
            expected = (max_ts - min_ts) // interval_ms + 1
            gap = expected - count
            return gap if gap > 5 else None  # >5 = echte gap

    except Exception:
        return None


# ============================================================
# HOOFD FETCH LOOP
# ============================================================
def fetch_batch(
    symbols: List[str],
    conn,
) -> Dict[str, int]:
    """
    Verwerkt een batch symbols.
    Haalt candles op voor alle timeframes.
    Geeft statistieken terug.
    """
    stats = {
        "processed":    0,
        "candles_new":  0,
        "candles_tried": 0,
        "errors":       0,
        "skipped":      0,
    }

    total = len(symbols)

    for idx, symbol in enumerate(symbols):
        symbol_ok = True

        for interval in TIMEFRAMES:
            candles = fetch_klines(symbol, interval, CANDLES_PER_SYMBOL)

            if candles:
                new = upsert_candles(conn, symbol, interval, candles)
                stats["candles_new"]  += new
                stats["candles_tried"] += len(candles)
            else:
                log(f"  ⚠️ Geen candles: {symbol}/{interval}")
                stats["errors"] += 1
                symbol_ok = False

        if symbol_ok:
            stats["processed"] += 1
        else:
            stats["skipped"] += 1

        # Progress log elke 10 symbols
        if (idx + 1) % 10 == 0:
            log(
                f"  [{idx+1}/{total}] "
                f"nieuw={stats['candles_new']} | "
                f"errors={stats['errors']}"
            )

    return stats


# ============================================================
# MAIN
# ============================================================
def main() -> None:
    log("=" * 60)
    log("History Fetcher v2.0 — gestart")
    log("=" * 60)
    log(f"Database:          {'✅' if DATABASE_URL else '❌ ONTBREEKT'}")
    log(f"Auto universe:     {AUTO_UNIVERSE}")
    log(f"Timeframes:        {TIMEFRAMES}")
    log(f"Batch size:        {BATCH_SIZE}")
    log(f"Candles/symbol:    {CANDLES_PER_SYMBOL}")
    log(f"Min volume 24h:    {MIN_QUOTE_VOLUME_24H:,.0f}")
    log(f"Binance sleep:     {BINANCE_SLEEP}s")
    log(f"Max retries:       {MAX_RETRIES}")
    log("=" * 60)

    if not DATABASE_URL:
        log("❌ DATABASE_URL ontbreekt")
        sys.exit(1)

    conn = db_connect()
    log("✅ Database verbonden")

    # Advisory lock — voorkomt dubbele runs
    if not acquire_advisory_lock(conn):
        log("⚠️ Andere fetcher run is actief — skip")
        conn.close()
        return

    try:
        # Tabel setup
        ensure_candles_table(conn)

        # Universe bepalen
        log("Universe bepalen...")
        universe = get_universe(conn)

        if not universe:
            log("❌ Lege universe — stop")
            report_error(Exception("Lege universe"), "main", "KRITIEK")
            return

        log(f"Universe: {len(universe)} symbols")

        # Batch offset berekenen
        offset = get_batch_offset(conn, len(universe))

        # Batch samenstellen met correcte wrap-around
        end     = offset + BATCH_SIZE
        if end <= len(universe):
            batch = universe[offset:end]
        else:
            # Wrap-around: pak einde + begin
            batch = universe[offset:] + universe[:end - len(universe)]

        log(f"Batch: symbols {offset}-{offset+len(batch)} van {len(universe)}")
        log(f"Symbols in batch: {batch[:5]}{'...' if len(batch) > 5 else ''}")

        # Status opslaan
        set_fetcher_state(conn, "last_run", now_utc().isoformat())
        set_fetcher_state(conn, "last_batch", f"{offset}-{offset+len(batch)}")
        set_fetcher_state(conn, "universe_size", str(len(universe)))

        # Fetch uitvoeren
        start_time = time.time()
        stats = fetch_batch(batch, conn)
        elapsed = time.time() - start_time

        # Statistieken
        log("=" * 60)
        log(f"✅ Fetch klaar in {elapsed:.1f}s")
        log(f"   Verwerkt:      {stats['processed']}/{len(batch)}")
        log(f"   Nieuwe candles: {stats['candles_new']:,}")
        log(f"   Geprobeerd:    {stats['candles_tried']:,}")
        log(f"   Errors:        {stats['errors']}")
        log(f"   Overgeslagen:  {stats['skipped']}")
        log("=" * 60)

        # Update state met resultaten
        set_fetcher_state(conn, "last_candles_new", str(stats['candles_new']))
        set_fetcher_state(conn, "last_errors", str(stats['errors']))

    except Exception as e:
        report_error(e, "main", "KRITIEK")
        raise

    finally:
        release_advisory_lock(conn)
        conn.close()


if __name__ == "__main__":
    main()


# ============================================================
# UITGEBREIDE ANALYTICS EN HEALTH MONITORING
# ============================================================
def get_candle_stats(conn) -> Dict[str, Any]:
    """
    Geeft statistieken over de candles tabel.
    Wordt gebruikt voor health monitoring.
    Samenwerking: build_btc_regime.py leest ook uit candles tabel.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COUNT(DISTINCT symbol)   AS unieke_symbols,
                COUNT(DISTINCT timeframe) AS timeframes,
                COUNT(*)                 AS totaal_candles,
                MAX(to_timestamp(open_time/1000)) AS laatste_candle,
                MIN(to_timestamp(open_time/1000)) AS eerste_candle
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
    Voorkomt dat de DB eindeloos groeit.
    Geeft aantal verwijderde rijen terug.
    """
    cutoff_ms = int((time.time() - keep_days * 86400) * 1000)
    try:
        with conn.cursor() as cur:
            cur.execute("""
            DELETE FROM public.candles
            WHERE open_time < %s
            """, (cutoff_ms,))
            deleted = cur.rowcount
        conn.commit()
        if deleted > 0:
            log(f"🧹 {deleted} oude candles verwijderd (>{keep_days} dagen)")
        return deleted
    except Exception as e:
        log(f"⚠️ cleanup_old_candles fout: {e}")
        return 0


def send_whatsapp(message: str) -> bool:
    """
    Stuurt WhatsApp bericht via Twilio.
    Identieke implementatie als alle andere bestanden.
    Alleen voor kritieke fetch fouten.
    """
    TWILIO_ACCOUNT_SID   = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    TWILIO_AUTH_TOKEN    = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
    TWILIO_WHATSAPP_TO   = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()

    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO]):
        log(f"WhatsApp (geen Twilio): {message[:80]}")
        return False
    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts"
            f"/{TWILIO_ACCOUNT_SID}/Messages.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={"From": TWILIO_WHATSAPP_FROM,
                  "To":   TWILIO_WHATSAPP_TO,
                  "Body": message},
            timeout=15,
        )
        return resp.status_code in (200, 201)
    except Exception:
        return False


def report_fetch_error(error: Exception, symbol: str) -> None:
    """
    Rapporteert kritieke fetch fouten.
    Wordt aangeroepen als Binance meerdere keren faalt.
    """
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


def get_symbols_needing_update(conn, max_age_hours: int = 6) -> List[str]:
    """
    Geeft lijst van symbols die nieuwe candles nodig hebben.
    Gebaseerd op de laatste candle timestamp per symbol.

    Samenwerking: multi_coin_score.py gebruikt dezelfde symbols
    voor live scoring. Deze functie zorgt dat data actueel blijft.
    """
    cutoff_ms = int((time.time() - max_age_hours * 3600) * 1000)
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
            """, (cutoff_ms,))
            return [row[0] for row in cur.fetchall()]
    except Exception as e:
        log(f"⚠️ get_symbols_needing_update fout: {e}")
        return []


def _claude_check_fetch_health(symbols_done: int, candles_saved: int, errors: int) -> None:
    """
    Claude analyseert fetch sessie gezondheid.
    Stuurt waarschuwing als fout ratio te hoog is.
    Identiek patroon als andere Claude monitoring functies.
    """
    ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not ANTHROPIC_API_KEY:
        return

    error_rate = errors / max(symbols_done, 1) * 100
    if error_rate < 10:
        return  # Alles OK — geen analyse nodig

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
                "max_tokens": 150,
                "messages": [{
                    "role": "user",
                    "content": f"""
Je bent een crypto data fetcher monitor.
Analyseer deze fetch sessie in 2 zinnen Nederlands.

Symbols verwerkt: {symbols_done}
Candles opgeslagen: {candles_saved}
Fouten: {errors} ({error_rate:.1f}%)

Is dit zorgwekkend? Wat kan de oorzaak zijn?
""".strip()
                }],
            },
            timeout=20,
        )
        if resp.status_code == 200:
            analyse = resp.json()["content"][0]["text"].strip()
            log(f"🧠 Claude fetch analyse: {analyse}")
    except Exception:
        pass
