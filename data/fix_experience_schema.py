# fix_experience_schema.py
# ============================================================
# Crypto AI Bot — Schema Sync v3.0
# ============================================================
# Synchroniseert het PostgreSQL database schema.
# Veilig om meerdere keren te draaien (idempotent).
#
# V3.0 PATTERN:
#   ✅ safe_rollback() overal
#   ✅ db_connect() retries=3 + autocommit=False
#   ✅ SET statement_timeout=0 voor DDL operaties
#   ✅ ALTER TABLE IF NOT EXISTS migraties
#   ✅ conn=None + finally conn.close()
#   ✅ Model: claude-sonnet-4-6
#   ✅ WhatsApp rate limiting per fouttype
#
# WAT DIT DOET:
#   1. Alle tabellen aanmaken (CREATE TABLE IF NOT EXISTS)
#   2. Ontbrekende kolommen toevoegen (ALTER TABLE IF NOT EXISTS)
#   3. UNIQUE constraints + indexes
#   4. Data sync (is_shadow, result_r)
#   5. bot_state defaults initialiseren
#   6. Verificatie kritieke kolommen
#
# TABELLEN BEHEERD:
#   bot_state, experience_trades, pending_approvals,
#   experience_scoreboard, btc_regime_4h, btc_regime_changes,
#   market_regime, candles, fetcher_state, coach_events
#
# SAMENWERKING:
#   experience_trades  -> live_trader, trade_monitor, webhook, app
#   pending_approvals  -> multi_coin_score, webhook, app
#   experience_scoreboard -> history_simulator, multi_coin_score
#   bot_state          -> alle bestanden
#   coach_events       -> live_trader (schrijft), ai_coach (leest)
# ============================================================

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
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

# WhatsApp rate limiting
_WA_LAST_SENT: Dict[str, float] = {}
WA_RATE_LIMIT_SEC = 30 * 60


# ============================================================
# BASIS HELPERS — identiek aan alle andere bestanden
# ============================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')}] [SCHEMA] {msg}", flush=True)


def safe_str(x: Any, default: str = "") -> str:
    if x is None:
        return default
    try:
        s = str(x).strip()
        return s if s else default
    except Exception:
        return default


def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


# ============================================================
# WHATSAPP — v3.0: rate limiting per fouttype
# ============================================================
def send_whatsapp(message: str, rate_key: str = "") -> bool:
    """WhatsApp via Twilio. v3.0: rate_key max 1x per 30 min per type."""
    if rate_key:
        last = _WA_LAST_SENT.get(rate_key, 0.0)
        if time.time() - last < WA_RATE_LIMIT_SEC:
            log(f"WhatsApp rate-limited ({rate_key})")
            return False
        _WA_LAST_SENT[rate_key] = time.time()

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
# CLAUDE — model: claude-sonnet-4-6 (v3.0)
# ============================================================
def _claude_analyse(prompt: str, max_tokens: int = 200) -> str:
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


# ============================================================
# DATABASE — v3.0: retries=3 + autocommit=False
# ============================================================
def db_connect(retries: int = 3):
    """DB verbinding. v3.0: retries=3 + autocommit=False."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")

    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            conn = psycopg2.connect(DATABASE_URL, sslmode="require")
            conn.autocommit = False
            return conn
        except Exception as e:
            last_err = e
            log(f"⚠️ DB connect poging {attempt}/{retries}: {e}")
            if attempt < retries:
                time.sleep(2 ** attempt)

    raise RuntimeError(f"DB connect mislukt na {retries} pogingen: {last_err}")


def safe_rollback(conn) -> None:
    """Veilige rollback — gooit nooit een exception. v3.0 pattern."""
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception as e:
        log(f"⚠️ rollback fout (genegeerd): {e}")


def set_statement_timeout_zero(conn) -> None:
    """
    Zet statement_timeout=0 voor DDL operaties.
    v3.0: DDL (CREATE TABLE, ALTER TABLE, CREATE INDEX)
    kan lang duren — geen timeout.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 0")
        conn.commit()
    except Exception as e:
        log(f"⚠️ statement_timeout instellen mislukt: {e}")


# ============================================================
# SCHEMA DEFINITIES
# Alle kolommen die elk bestand verwacht
# ============================================================

# experience_trades — kern van het systeem
EXPERIENCE_TRADES_COLUMNS: List[Tuple[str, str, Optional[str]]] = [
    # Identificatie
    ("trade_key",           "TEXT",             None),
    ("source",              "TEXT",             "'UNKNOWN'"),
    ("is_shadow",           "BOOLEAN",          "FALSE"),

    # Coin
    ("coin",                "TEXT",             None),
    ("symbol",              "TEXT",             None),
    ("timeframe",           "TEXT",             "'4h'"),
    ("bitvavo_market",      "TEXT",             None),

    # Setup
    ("setup_type",          "TEXT",             None),
    ("market_regime",       "TEXT",             None),
    ("regime",              "TEXT",             None),
    ("label",               "TEXT",             None),
    ("why_tag",             "TEXT",             None),
    ("claude_beoordeling",  "TEXT",             None),

    # Timing
    ("timestamp",           "TIMESTAMPTZ",      "NOW()"),
    ("entry_time",          "TIMESTAMPTZ",      None),
    ("exit_time",           "TIMESTAMPTZ",      None),
    ("updated_at",          "TIMESTAMPTZ",      "NOW()"),
    ("created_at",          "TIMESTAMPTZ",      "NOW()"),

    # Prijzen
    ("entry",               "DOUBLE PRECISION", None),
    ("stop",                "DOUBLE PRECISION", None),
    ("stop_loss",           "DOUBLE PRECISION", None),
    ("target",              "DOUBLE PRECISION", None),
    ("exit_price",          "DOUBLE PRECISION", None),

    # Hoeveelheden
    ("qty",                 "DOUBLE PRECISION", None),
    ("position_size",       "DOUBLE PRECISION", None),
    ("amount_eur",          "DOUBLE PRECISION", None),

    # Scores
    ("bot_confidence",      "INTEGER",          "0"),
    ("score",               "INTEGER",          "0"),
    ("raw_score",           "INTEGER",          "0"),
    ("chance",              "INTEGER",          "0"),
    ("confidence",          "INTEGER",          "0"),

    # Experience
    ("exp_n",               "INTEGER",          "0"),
    ("exp_win_rate",        "DOUBLE PRECISION", "0.5"),
    ("exp_bias",            "TEXT",             "'NEUTRAL'"),

    # Uitkomst
    ("outcome",             "TEXT",             None),
    ("pnl_eur",             "DOUBLE PRECISION", "0.0"),
    ("pnl_r",               "DOUBLE PRECISION", "0.0"),
    ("result_r",            "DOUBLE PRECISION", "0.0"),
    ("r_multiple",          "DOUBLE PRECISION", "0.0"),
    ("fee_eur",             "DOUBLE PRECISION", "0.0"),

    # MFE/MAE tracking
    ("mfe",                 "DOUBLE PRECISION", "0.0"),
    ("mae",                 "DOUBLE PRECISION", "0.0"),
    ("mfe_r",               "DOUBLE PRECISION", "0.0"),
    ("mae_r",               "DOUBLE PRECISION", "0.0"),
    ("max_r",               "DOUBLE PRECISION", "0.0"),
    ("max_price_seen",      "DOUBLE PRECISION", None),
    ("min_price_seen",      "DOUBLE PRECISION", None),
    ("time_minutes",        "DOUBLE PRECISION", "0.0"),

    # Metadata
    ("prebuy_id",           "TEXT",             None),
    ("user_decision",       "TEXT",             None),
    ("bot_decision",        "TEXT",             None),
    ("market_condition",    "TEXT",             None),
    ("why",                 "TEXT",             None),
    ("why_full",            "TEXT",             None),
    ("notes",               "TEXT",             None),
    ("order_id",            "TEXT",             None),
]

# pending_approvals — multi_coin_score -> webhook -> live_trader
PENDING_APPROVALS_COLUMNS: List[Tuple[str, str, Optional[str]]] = [
    ("id",              "TEXT",             None),
    ("symbol",          "TEXT",             None),
    ("setup_type",      "TEXT",             None),
    ("regime",          "TEXT",             None),
    ("score",           "INTEGER",          "0"),
    ("raw_score",       "INTEGER",          "0"),
    ("chance",          "INTEGER",          "0"),
    ("confidence",      "INTEGER",          "0"),
    ("label",           "TEXT",             "'GO'"),
    ("entry",           "DOUBLE PRECISION", None),
    ("stop",            "DOUBLE PRECISION", None),
    ("target",          "DOUBLE PRECISION", None),
    ("expires_at",      "TIMESTAMPTZ",      None),
    ("created_at",      "TIMESTAMPTZ",      "NOW()"),
    ("consumed_at",     "TIMESTAMPTZ",      None),
    ("rejected_at",     "TIMESTAMPTZ",      None),
    ("status",          "TEXT",             "'PENDING'"),
    ("timeframe",       "TEXT",             "'4h'"),
    ("bitvavo_market",  "TEXT",             None),
    ("exp_n",           "INTEGER",          "0"),
    ("exp_win_rate",    "DOUBLE PRECISION", "0.5"),
    ("exp_bias",        "TEXT",             "'NEUTRAL'"),
    ("why_tag",         "TEXT",             None),
    ("claude_beoordeling", "TEXT",          None),
    ("updated_at",      "TIMESTAMPTZ",      "NOW()"),
    # v3.0: kolommen toegevoegd via psql shell — nu ook hier
    ("score_details",   "JSONB",            None),
    ("vwap_positie",    "TEXT",             None),
    ("divergentie",     "TEXT",             None),
    ("funding_rate",    "DOUBLE PRECISION", None),
    ("live_toegestaan", "BOOLEAN",          "TRUE"),
    ("atr",             "DOUBLE PRECISION", None),
]

# experience_scoreboard — history_simulator -> multi_coin_score
EXPERIENCE_SCOREBOARD_COLUMNS: List[Tuple[str, str, Optional[str]]] = [
    ("symbol",          "TEXT",             None),
    ("setup_type",      "TEXT",             None),
    ("regime",          "TEXT",             None),
    ("n",               "INTEGER",          "0"),
    ("wins",            "INTEGER",          "0"),
    ("losses",          "INTEGER",          "0"),
    ("timeouts",        "INTEGER",          "0"),
    ("win_rate",        "DOUBLE PRECISION", "0.0"),
    ("avg_pnl_eur",     "DOUBLE PRECISION", "0.0"),
    ("avg_r",           "DOUBLE PRECISION", "0.0"),
    ("avg_hold_min",    "DOUBLE PRECISION", "0.0"),
    ("bias",            "TEXT",             "'NEUTRAL'"),
    ("profit_factor",   "DOUBLE PRECISION", "0.0"),
    ("expectancy",      "DOUBLE PRECISION", "0.0"),
    ("updated_at",      "TIMESTAMPTZ",      "NOW()"),
]


# ============================================================
# HELPER: kolom/tabel bestaan
# ============================================================
def table_exists(cur, table: str) -> bool:
    cur.execute("""
    SELECT EXISTS(
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
    )
    """, (table,))
    return bool(cur.fetchone()[0])


def column_exists(cur, table: str, column: str) -> bool:
    cur.execute("""
    SELECT EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = %s
          AND column_name  = %s
    )
    """, (table, column))
    return bool(cur.fetchone()[0])


# ============================================================
# KOLOMMEN TOEVOEGEN
# v3.0: SET statement_timeout=0 + ALTER TABLE IF NOT EXISTS
# ============================================================
def add_missing_columns(
    conn,
    table:   str,
    columns: List[Tuple[str, str, Optional[str]]],
) -> int:
    """
    Voegt ontbrekende kolommen toe. Idempotent.
    v3.0: statement_timeout=0 zodat ALTER TABLE niet timet.
    """
    added = 0

    with conn.cursor() as cur:
        if not table_exists(cur, table):
            log(f"⚠️ public.{table} bestaat niet — overgeslagen")
            return 0

        # v3.0: geen timeout voor DDL
        cur.execute("SET statement_timeout = 0")

        for col_name, col_type, col_default in columns:
            if column_exists(cur, table, col_name):
                continue
            default_clause = f"DEFAULT {col_default}" if col_default else ""
            sql = (
                f"ALTER TABLE public.{table} "
                f"ADD COLUMN IF NOT EXISTS "
                f"{col_name} {col_type} {default_clause}"
            )
            try:
                cur.execute(sql)
                added += 1
                log(f"  ✅ {table}.{col_name} ({col_type}) toegevoegd")
            except Exception as e:
                log(f"  ⚠️ {table}.{col_name}: {e}")

    conn.commit()
    return added


# ============================================================
# TABELLEN AANMAKEN
# v3.0: SET statement_timeout=0 voor elke DDL
# ============================================================
def _exec_ddl(conn, sql: str, label: str) -> None:
    """Voert DDL uit met statement_timeout=0. v3.0 pattern."""
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 0")
            cur.execute(sql)
        conn.commit()
        log(f"  ✅ {label}")
    except Exception as e:
        safe_rollback(conn)
        log(f"  ⚠️ {label}: {e}")


def create_bot_state_table(conn) -> None:
    _exec_ddl(conn, """
    CREATE TABLE IF NOT EXISTS public.bot_state (
        key        TEXT PRIMARY KEY,
        value      TEXT,
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )
    """, "bot_state tabel")


def create_experience_trades_table(conn) -> None:
    _exec_ddl(conn, """
    CREATE TABLE IF NOT EXISTS public.experience_trades (
        trade_key  TEXT PRIMARY KEY,
        source     TEXT        DEFAULT 'UNKNOWN',
        coin       TEXT,
        timestamp  TIMESTAMPTZ DEFAULT NOW(),
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )
    """, "experience_trades tabel")


def create_pending_approvals_table(conn) -> None:
    """
    pending_approvals: PRIMARY KEY gen_random_uuid() — vereist pgcrypto.
    pgcrypto al geinstalleerd via: CREATE EXTENSION pgcrypto;
    """
    _exec_ddl(conn, """
    CREATE TABLE IF NOT EXISTS public.pending_approvals (
        id         TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
        symbol     TEXT,
        score      INTEGER DEFAULT 0,
        status     TEXT    DEFAULT 'PENDING',
        created_at TIMESTAMPTZ DEFAULT NOW()
    )
    """, "pending_approvals tabel")


def create_experience_scoreboard_table(conn) -> None:
    _exec_ddl(conn, """
    CREATE TABLE IF NOT EXISTS public.experience_scoreboard (
        symbol     TEXT,
        setup_type TEXT,
        regime     TEXT,
        n          INTEGER DEFAULT 0,
        win_rate   DOUBLE PRECISION DEFAULT 0.0,
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (symbol, setup_type, regime)
    )
    """, "experience_scoreboard tabel")


def create_btc_regime_table(conn) -> None:
    """
    btc_regime_4h: geverifieerd schema via psql shell.
    Kolommen: open_time (PK), ts_utc, close, ema200, ema200_slope,
              regime, strength, pct_from_ema, updated_at.
    """
    _exec_ddl(conn, """
    CREATE TABLE IF NOT EXISTS public.btc_regime_4h (
        open_time    BIGINT PRIMARY KEY,
        ts_utc       TIMESTAMPTZ,
        close        DOUBLE PRECISION,
        ema200       DOUBLE PRECISION,
        ema200_slope DOUBLE PRECISION DEFAULT 0.0,
        regime       TEXT,
        strength     DOUBLE PRECISION DEFAULT 0.0,
        pct_from_ema DOUBLE PRECISION DEFAULT 0.0,
        updated_at   TIMESTAMPTZ DEFAULT NOW()
    )
    """, "btc_regime_4h tabel")


def create_btc_regime_changes_table(conn) -> None:
    _exec_ddl(conn, """
    CREATE TABLE IF NOT EXISTS public.btc_regime_changes (
        id         SERIAL PRIMARY KEY,
        old_regime TEXT,
        new_regime TEXT,
        close      DOUBLE PRECISION,
        ema200     DOUBLE PRECISION,
        strength   DOUBLE PRECISION,
        ts         TIMESTAMPTZ DEFAULT NOW()
    )
    """, "btc_regime_changes tabel")


def create_market_regime_table(conn) -> None:
    """
    market_regime: PRIMARY KEY op (symbol, timeframe).
    Gewijzigd via psql shell — hier gegarandeerd.
    """
    _exec_ddl(conn, """
    CREATE TABLE IF NOT EXISTS public.market_regime (
        symbol     TEXT        NOT NULL,
        timeframe  TEXT        NOT NULL,
        asof_ts    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        regime     TEXT,
        strength   DOUBLE PRECISION,
        sma50      DOUBLE PRECISION,
        sma200     DOUBLE PRECISION,
        score      INTEGER,
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (symbol, timeframe)
    )
    """, "market_regime tabel")


def create_candles_table(conn) -> None:
    """
    candles: geverifieerd schema — GEEN created_at in origineel.
    Kolommen: exchange, symbol, timeframe, open_time (BIGINT),
              open/high/low/close/volume, close_time, trades,
              quote_volume, taker_buy_base, taker_buy_quote, updated_at.
    """
    _exec_ddl(conn, """
    CREATE TABLE IF NOT EXISTS public.candles (
        exchange         TEXT        NOT NULL DEFAULT 'bitvavo',
        symbol           TEXT        NOT NULL,
        timeframe        TEXT        NOT NULL,
        open_time        BIGINT      NOT NULL,
        open             DOUBLE PRECISION,
        high             DOUBLE PRECISION,
        low              DOUBLE PRECISION,
        close            DOUBLE PRECISION,
        volume           DOUBLE PRECISION,
        close_time       BIGINT,
        trades           INTEGER,
        quote_volume     DOUBLE PRECISION,
        taker_buy_base   DOUBLE PRECISION,
        taker_buy_quote  DOUBLE PRECISION,
        updated_at       TIMESTAMPTZ DEFAULT NOW(),
        CONSTRAINT candles_unique_key
            UNIQUE (exchange, symbol, timeframe, open_time)
    )
    """, "candles tabel")


def create_fetcher_state_table(conn) -> None:
    _exec_ddl(conn, """
    CREATE TABLE IF NOT EXISTS public.fetcher_state (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """, "fetcher_state tabel")


def create_coach_events_table(conn) -> None:
    """
    coach_events: nieuw in v3.0.
    Geschreven door live_trader (log_trade_event),
    gelezen door ai_coach voor wekelijkse analyse.
    """
    _exec_ddl(conn, """
    CREATE TABLE IF NOT EXISTS public.coach_events (
        id         SERIAL PRIMARY KEY,
        event_type TEXT        NOT NULL,
        symbol     TEXT,
        event_data JSONB,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )
    """, "coach_events tabel (v3.0 nieuw)")


# ============================================================
# CONSTRAINTS
# ============================================================
def ensure_trade_key_unique(conn) -> None:
    """UNIQUE constraint op experience_trades.trade_key."""
    sql = """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'experience_trades_trade_key_unique'
              AND conrelid = 'public.experience_trades'::regclass
        ) THEN
            ALTER TABLE public.experience_trades
            ADD CONSTRAINT experience_trades_trade_key_unique
            UNIQUE (trade_key);
        END IF;
    EXCEPTION WHEN others THEN
        NULL;
    END
    $$;
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 0")
            cur.execute(sql)
        conn.commit()
        log("  ✅ trade_key UNIQUE constraint")
    except Exception as e:
        safe_rollback(conn)
        log(f"  ⚠️ trade_key constraint: {e}")


def ensure_pgcrypto(conn) -> None:
    """
    Zorgt dat pgcrypto extension aanwezig is.
    Nodig voor gen_random_uuid() in pending_approvals.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        conn.commit()
        log("  ✅ pgcrypto extension")
    except Exception as e:
        safe_rollback(conn)
        log(f"  ⚠️ pgcrypto: {e}")


# ============================================================
# INDEXES
# v3.0: statement_timeout=0 + IF NOT EXISTS
# ============================================================
def create_indexes(conn) -> None:
    """Maakt performante indexes aan. Idempotent via IF NOT EXISTS."""
    indexes = [
        # experience_trades
        ("idx_exp_trades_source",
         "CREATE INDEX IF NOT EXISTS idx_exp_trades_source ON public.experience_trades (source)"),
        ("idx_exp_trades_outcome",
         "CREATE INDEX IF NOT EXISTS idx_exp_trades_outcome ON public.experience_trades (outcome)"),
        ("idx_exp_trades_coin_source",
         "CREATE INDEX IF NOT EXISTS idx_exp_trades_coin_source ON public.experience_trades (coin, source)"),
        ("idx_exp_trades_exit_time",
         "CREATE INDEX IF NOT EXISTS idx_exp_trades_exit_time ON public.experience_trades (exit_time)"),
        ("idx_exp_trades_source_outcome",
         "CREATE INDEX IF NOT EXISTS idx_exp_trades_source_outcome "
         "ON public.experience_trades (source, outcome, exit_time)"),

        # pending_approvals
        ("idx_pending_status",
         "CREATE INDEX IF NOT EXISTS idx_pending_status ON public.pending_approvals (status)"),
        ("idx_pending_symbol",
         "CREATE INDEX IF NOT EXISTS idx_pending_symbol ON public.pending_approvals (symbol)"),
        ("idx_pending_expires",
         "CREATE INDEX IF NOT EXISTS idx_pending_expires ON public.pending_approvals (expires_at)"),

        # experience_scoreboard
        ("idx_scoreboard_lookup",
         "CREATE INDEX IF NOT EXISTS idx_scoreboard_lookup "
         "ON public.experience_scoreboard (symbol, setup_type, regime)"),

        # candles
        ("idx_candles_symbol_tf",
         "CREATE INDEX IF NOT EXISTS idx_candles_symbol_tf ON public.candles (symbol, timeframe)"),
        ("idx_candles_open_time",
         "CREATE INDEX IF NOT EXISTS idx_candles_open_time ON public.candles (open_time DESC)"),
        ("idx_candles_sym_tf_time",
         "CREATE INDEX IF NOT EXISTS idx_candles_sym_tf_time "
         "ON public.candles (symbol, timeframe, open_time DESC)"),

        # btc_regime_4h
        ("idx_btc_regime_time",
         "CREATE INDEX IF NOT EXISTS idx_btc_regime_time ON public.btc_regime_4h (open_time DESC)"),

        # market_regime
        ("idx_market_regime_symbol",
         "CREATE INDEX IF NOT EXISTS idx_market_regime_symbol "
         "ON public.market_regime (symbol, asof_ts DESC)"),

        # coach_events (v3.0)
        ("idx_coach_events_type",
         "CREATE INDEX IF NOT EXISTS idx_coach_events_type ON public.coach_events (event_type)"),
        ("idx_coach_events_symbol",
         "CREATE INDEX IF NOT EXISTS idx_coach_events_symbol ON public.coach_events (symbol, created_at DESC)"),
    ]

    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 0")
            for idx_name, sql in indexes:
                try:
                    cur.execute(sql)
                    log(f"  ✅ Index: {idx_name}")
                except Exception as e:
                    log(f"  ⚠️ Index {idx_name}: {e}")
        conn.commit()
    except Exception as e:
        safe_rollback(conn)
        log(f"  ⚠️ create_indexes fout: {e}")


# ============================================================
# DATA SYNCHRONISATIE
# ============================================================
def sync_is_shadow_column(conn) -> None:
    """source='SHADOW' -> is_shadow=TRUE synchroniseren."""
    try:
        with conn.cursor() as cur:
            if not column_exists(cur, "experience_trades", "is_shadow"):
                log("  ⚠️ is_shadow kolom ontbreekt — skip")
                return
            cur.execute("""
            UPDATE public.experience_trades
            SET is_shadow = TRUE
            WHERE UPPER(COALESCE(source,'')) = 'SHADOW'
              AND (is_shadow IS NULL OR is_shadow = FALSE)
            """)
            updated = cur.rowcount
            if updated > 0:
                log(f"  ✅ is_shadow sync: {updated} rijen")
        conn.commit()
    except Exception as e:
        safe_rollback(conn)
        log(f"  ⚠️ is_shadow sync fout: {e}")


def sync_result_r_column(conn) -> None:
    """
    result_r = pnl_eur / amount_eur * 100.
    Dashboard gebruikt dit voor R-multiple tonen.
    """
    try:
        with conn.cursor() as cur:
            if not column_exists(cur, "experience_trades", "result_r"):
                log("  ⚠️ result_r kolom ontbreekt — skip")
                return
            cur.execute("""
            UPDATE public.experience_trades
            SET
                result_r = CASE
                    WHEN COALESCE(amount_eur, 0) > 0 AND pnl_eur IS NOT NULL
                    THEN ROUND((pnl_eur / amount_eur * 100)::numeric, 2)
                    ELSE 0
                END,
                pnl_r = CASE
                    WHEN COALESCE(amount_eur, 0) > 0 AND pnl_eur IS NOT NULL
                    THEN ROUND((pnl_eur / amount_eur * 100)::numeric, 2)
                    ELSE 0
                END
            WHERE (result_r IS NULL OR result_r = 0)
              AND pnl_eur IS NOT NULL
              AND COALESCE(amount_eur, 0) > 0
            """)
            updated = cur.rowcount
            if updated > 0:
                log(f"  ✅ result_r sync: {updated} rijen")
        conn.commit()
    except Exception as e:
        safe_rollback(conn)
        log(f"  ⚠️ result_r sync fout: {e}")


# ============================================================
# BOT STATE INITIALISATIE
# ============================================================
def initialize_bot_state(conn) -> None:
    """
    Zorgt dat essentiële bot_state keys bestaan.
    ON CONFLICT DO NOTHING — overschrijft nooit bestaande waarden.
    """
    defaults = {
        "bot_active":              "false",
        "bot_paused":              "false",
        "bot_paused_until":        "",
        "bot_paused_reason":       "",
        "live_trader_busy":        "false",
        "live_trader_last_action": "",
        "live_trader_last_ts":     "",
        "live_trader_error":       "",
        "trade_monitor_last_check": "",
    }
    try:
        with conn.cursor() as cur:
            for key, value in defaults.items():
                cur.execute("""
                INSERT INTO public.bot_state (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO NOTHING
                """, (key, value))
        conn.commit()
        log(f"  ✅ bot_state defaults ({len(defaults)} keys)")
    except Exception as e:
        safe_rollback(conn)
        log(f"  ⚠️ bot_state init fout: {e}")


# ============================================================
# VERIFICATIE
# ============================================================
def verify_schema(conn) -> Dict[str, Any]:
    """Controleert alle kritieke kolommen. Geeft rapport terug."""
    kritiek = [
        # experience_trades
        ("experience_trades", "trade_key"),
        ("experience_trades", "source"),
        ("experience_trades", "is_shadow"),
        ("experience_trades", "coin"),
        ("experience_trades", "outcome"),
        ("experience_trades", "pnl_eur"),
        ("experience_trades", "result_r"),
        ("experience_trades", "exit_time"),
        ("experience_trades", "setup_type"),
        ("experience_trades", "market_regime"),
        ("experience_trades", "mfe_r"),
        ("experience_trades", "mae_r"),
        ("experience_trades", "amount_eur"),
        ("experience_trades", "fee_eur"),

        # pending_approvals
        ("pending_approvals", "id"),
        ("pending_approvals", "symbol"),
        ("pending_approvals", "status"),
        ("pending_approvals", "bitvavo_market"),
        ("pending_approvals", "expires_at"),
        ("pending_approvals", "live_toegestaan"),
        ("pending_approvals", "score_details"),

        # bot_state
        ("bot_state", "key"),
        ("bot_state", "value"),

        # experience_scoreboard
        ("experience_scoreboard", "symbol"),
        ("experience_scoreboard", "win_rate"),
        ("experience_scoreboard", "n"),

        # coach_events (v3.0)
        ("coach_events", "event_type"),
        ("coach_events", "symbol"),
        ("coach_events", "event_data"),

        # btc_regime_4h
        ("btc_regime_4h", "open_time"),
        ("btc_regime_4h", "regime"),
        ("btc_regime_4h", "strength"),
        ("btc_regime_4h", "pct_from_ema"),

        # candles
        ("candles", "symbol"),
        ("candles", "timeframe"),
        ("candles", "open_time"),
        ("candles", "close"),
    ]

    rapport: Dict[str, Any] = {"ok": True, "missend": [], "aanwezig": []}

    with conn.cursor() as cur:
        for table, column in kritiek:
            try:
                if not table_exists(cur, table):
                    rapport["missend"].append(f"{table} (tabel ontbreekt)")
                    rapport["ok"] = False
                    continue
                exists = column_exists(cur, table, column)
                if exists:
                    rapport["aanwezig"].append(f"{table}.{column}")
                else:
                    rapport["missend"].append(f"{table}.{column}")
                    rapport["ok"] = False
            except Exception:
                rapport["missend"].append(f"{table}.{column} (check fout)")
                rapport["ok"] = False

    return rapport


# ============================================================
# HOOFD SYNC FUNCTIE
# ============================================================
def sync_schema() -> None:
    """
    Volledige schema sync. Idempotent — veilig meerdere keren uitvoeren.

    Stappen:
    1. pgcrypto extension
    2. Alle tabellen aanmaken
    3. Ontbrekende kolommen toevoegen
    4. Constraints
    5. Indexes
    6. Data synchronisatie
    7. bot_state initialiseren
    8. Verificatie
    """
    conn = None
    try:
        conn = db_connect()

        log("=" * 52)
        log("Schema sync v3.0 gestart...")
        log("=" * 52)

        # ── Stap 1: Extensions ───────────────────────
        log("\n📋 Stap 1: Extensions...")
        ensure_pgcrypto(conn)

        # ── Stap 2: Tabellen aanmaken ─────────────────
        log("\n📋 Stap 2: Tabellen aanmaken...")
        create_bot_state_table(conn)
        create_experience_trades_table(conn)
        create_pending_approvals_table(conn)
        create_experience_scoreboard_table(conn)
        create_btc_regime_table(conn)
        create_btc_regime_changes_table(conn)
        create_market_regime_table(conn)
        create_candles_table(conn)
        create_fetcher_state_table(conn)
        create_coach_events_table(conn)

        # ── Stap 3: Kolommen toevoegen ────────────────
        log("\n📋 Stap 3: Ontbrekende kolommen...")
        n = add_missing_columns(conn, "experience_trades", EXPERIENCE_TRADES_COLUMNS)
        log(f"  → experience_trades: {n} kolommen toegevoegd")

        n = add_missing_columns(conn, "pending_approvals", PENDING_APPROVALS_COLUMNS)
        log(f"  → pending_approvals: {n} kolommen toegevoegd")

        n = add_missing_columns(conn, "experience_scoreboard", EXPERIENCE_SCOREBOARD_COLUMNS)
        log(f"  → experience_scoreboard: {n} kolommen toegevoegd")

        # ── Stap 4: Constraints ───────────────────────
        log("\n📋 Stap 4: Constraints...")
        ensure_trade_key_unique(conn)

        # ── Stap 5: Indexes ───────────────────────────
        log("\n📋 Stap 5: Indexes...")
        create_indexes(conn)

        # ── Stap 6: Data sync ─────────────────────────
        log("\n📋 Stap 6: Data synchronisatie...")
        sync_is_shadow_column(conn)
        sync_result_r_column(conn)

        # ── Stap 7: bot_state defaults ────────────────
        log("\n📋 Stap 7: bot_state initialiseren...")
        initialize_bot_state(conn)

        # ── Stap 8: Verificatie ───────────────────────
        log("\n📋 Stap 8: Verificatie...")
        rapport = verify_schema(conn)

        if rapport["ok"]:
            log(f"  ✅ Alle {len(rapport['aanwezig'])} kritieke kolommen aanwezig")
        else:
            log(f"  ⚠️ Missende kolommen ({len(rapport['missend'])}):")
            for col in rapport["missend"]:
                log(f"    ❌ {col}")
            send_whatsapp(
                f"⚠️ SCHEMA SYNC WAARSCHUWING\n"
                f"{'─' * 28}\n\n"
                f"Missende kolommen:\n"
                + "\n".join(f"• {col}" for col in rapport["missend"][:5]) +
                f"\n\nBot kan problemen ondervinden.\n"
                f"Check Render logs.",
                rate_key="schema_missend",
            )

        log("\n" + "=" * 52)
        log("✅ Schema sync v3.0 voltooid!")
        log("=" * 52)

    except Exception as e:
        safe_rollback(conn)
        log(f"❌ Schema sync fout: {type(e).__name__}: {e}")

        prompt = f"""Je bent een crypto bot database beheerder.
Schema sync fout: {type(e).__name__}: {str(e)[:200]}

Geef in 2 zinnen Nederlands:
1. Wat er mis is
2. Wat de gebruiker moet doen"""

        uitleg = _claude_analyse(prompt, max_tokens=150)

        send_whatsapp(
            f"🚨 SCHEMA SYNC FOUT\n"
            f"{'─' * 28}\n\n"
            f"⚠️ Fout: {type(e).__name__}\n"
            f"{str(e)[:150]}\n\n"
            f"🧠 Claude:\n{uitleg or 'Schema sync mislukt — check logs.'}\n\n"
            f"📋 WAT TE DOEN:\n"
            f"1. Check Render logs\n"
            f"2. Controleer DATABASE_URL\n"
            f"3. Herstart de service\n\n"
            f"Bot kan data niet correct opslaan.",
            rate_key="schema_fout",
        )
        raise

    finally:
        if conn:
            conn.close()


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    log("=" * 60)
    log("Fix Experience Schema v3.0 — gestart")
    log("=" * 60)
    log(f"Database:   {'✅' if DATABASE_URL else '❌ ONTBREEKT'}")
    log(f"Twilio:     {'✅' if TWILIO_ACCOUNT_SID else '⚠️ niet ingesteld'}")
    log(f"Claude API: {'✅' if ANTHROPIC_API_KEY else '⚠️ niet ingesteld'}")
    log("=" * 60)

    if not DATABASE_URL:
        log("❌ DATABASE_URL ontbreekt — kan niet doorgaan")
        sys.exit(1)

    try:
        sync_schema()

        # Verificatie rapport afdrukken
        conn = db_connect()
        rapport = verify_schema(conn)
        conn.close()

        log(f"\n📋 Eindrapport:")
        if rapport["ok"]:
            log(f"✅ Alle {len(rapport['aanwezig'])} kritieke kolommen aanwezig")
        else:
            log(f"⚠️ {len(rapport['missend'])} kolommen missend:")
            for col in rapport["missend"]:
                log(f"  ❌ {col}")

        log("✅ Klaar")

    except KeyboardInterrupt:
        log("⛔ Gestopt door gebruiker")
        sys.exit(0)
    except Exception as e:
        log(f"❌ Fatale fout: {type(e).__name__}: {e}")
        sys.exit(1)
