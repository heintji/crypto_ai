# fix_experience_schema.py
# ============================================================
# Crypto AI Bot — Schema Sync v2.0
# ============================================================
# Synchroniseert het PostgreSQL database schema.
# Veilig om meerdere keren te draaien (idempotent).
#
# WAT DIT DOET:
#   1. Maakt bot_state tabel aan (als die niet bestaat)
#   2. Voegt ontbrekende kolommen toe aan experience_trades
#   3. Voegt ontbrekende kolommen toe aan pending_approvals
#   4. Voegt ontbrekende kolommen toe aan experience_scoreboard
#   5. Maakt indexes aan voor performante queries
#   6. Synchroniseert is_shadow kolom met source kolom
#   7. Berekent result_r / pnl_r op basis van pnl_eur
#   8. Verifieert dat alle kritieke kolommen aanwezig zijn
#
# SAMENWERKING MET ANDERE BESTANDEN:
#   → experience_trades: wordt geschreven door live_trader.py
#     en trade_monitor.py, gelezen door whatsapp_webhook.py
#     voor rapporten en app.py voor dashboard
#   → pending_approvals: wordt geschreven door multi_coin_score.py,
#     gelezen door whatsapp_webhook.py voor auto_buy
#   → experience_scoreboard: wordt geschreven door history_simulator.py,
#     gelezen door multi_coin_score.py voor scoring
#   → bot_state: wordt geschreven en gelezen door alle bestanden
#     voor bot aan/uit/gepauzeerd status
#
# BUGS GEFIXED vs origineel:
#   ✅ sslmode="require" ontbrak
#   ✅ is_shadow kolom ontbrak
#   ✅ result_r / pnl_r kolom ontbrak
#   ✅ source kolom niet geïndexeerd
#   ✅ Kolommen die dashboard verwacht ontbraken
#
# IDENTIEK AAN ALLE ANDERE BESTANDEN:
#   ✅ Zelfde sslmode="require" op DB connectie
#   ✅ Zelfde send_whatsapp() implementatie
#   ✅ Zelfde Claude health monitoring
#   ✅ Zelfde safe_str / safe_int helpers
#   ✅ Zelfde now_utc() patroon
#   ✅ Zelfde log() patroon
# ============================================================

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

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
# WHATSAPP — identiek aan alle andere bestanden
# Alleen voor kritieke schema fouten
# ============================================================
def send_whatsapp(message: str) -> bool:
    """
    Stuurt WhatsApp bericht via Twilio.
    Identieke implementatie in alle bestanden.
    Alleen bij kritieke schema sync fouten.
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
# CLAUDE AI — identiek aan alle andere bestanden
# ============================================================
def _claude_analyse(prompt: str, max_tokens: int = 200) -> str:
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


# ============================================================
# DATABASE — sslmode="require" identiek aan alle bestanden
# ============================================================
def db_connect():
    """
    DB verbinding met sslmode=require.
    Identiek in alle bestanden — verplicht voor Render.
    """
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


# ============================================================
# SCHEMA DEFINITIES
# Alle kolommen die elk bestand verwacht
# ============================================================

# experience_trades — gelezen door webhook, trader, monitor, app
EXPERIENCE_TRADES_COLUMNS: List[Tuple[str, str, Optional[str]]] = [
    # Primaire identificatie
    ("trade_key",       "TEXT",             None),
    ("source",          "TEXT",             "'UNKNOWN'"),
    ("is_shadow",       "BOOLEAN",          "FALSE"),

    # Coin identificatie
    ("coin",            "TEXT",             None),
    ("symbol",          "TEXT",             None),
    ("timeframe",       "TEXT",             "'4h'"),
    ("bitvavo_market",  "TEXT",             None),

    # Setup informatie
    ("setup_type",      "TEXT",             None),
    ("market_regime",   "TEXT",             None),
    ("regime",          "TEXT",             None),
    ("label",           "TEXT",             None),
    ("why_tag",         "TEXT",             None),
    ("claude_beoordeling", "TEXT",          None),

    # Timing
    ("timestamp",       "TIMESTAMPTZ",      "NOW()"),
    ("entry_time",      "TIMESTAMPTZ",      None),
    ("exit_time",       "TIMESTAMPTZ",      None),
    ("updated_at",      "TIMESTAMPTZ",      "NOW()"),
    ("created_at",      "TIMESTAMPTZ",      "NOW()"),

    # Prijzen
    ("entry",           "DOUBLE PRECISION", None),
    ("stop",            "DOUBLE PRECISION", None),
    ("stop_loss",       "DOUBLE PRECISION", None),
    ("target",          "DOUBLE PRECISION", None),
    ("exit_price",      "DOUBLE PRECISION", None),

    # Hoeveelheden
    ("qty",             "DOUBLE PRECISION", None),
    ("position_size",   "DOUBLE PRECISION", None),
    ("amount_eur",      "DOUBLE PRECISION", None),

    # Scores — gelezen door multi_coin_score en app
    ("bot_confidence",  "INTEGER",          "0"),
    ("score",           "INTEGER",          "0"),
    ("raw_score",       "INTEGER",          "0"),
    ("chance",          "INTEGER",          "0"),
    ("confidence",      "INTEGER",          "0"),

    # Experience data
    ("exp_n",           "INTEGER",          "0"),
    ("exp_win_rate",    "DOUBLE PRECISION", "0.5"),
    ("exp_bias",        "TEXT",             "'NEUTRAL'"),

    # Uitkomst
    ("outcome",         "TEXT",             None),
    ("pnl_eur",         "DOUBLE PRECISION", "0.0"),
    ("pnl_r",           "DOUBLE PRECISION", "0.0"),
    ("result_r",        "DOUBLE PRECISION", "0.0"),
    ("r_multiple",      "DOUBLE PRECISION", "0.0"),
    ("fee_eur",         "DOUBLE PRECISION", "0.0"),

    # MFE/MAE tracking — gelezen door trade_monitor en app
    ("mfe",             "DOUBLE PRECISION", "0.0"),
    ("mae",             "DOUBLE PRECISION", "0.0"),
    ("mfe_r",           "DOUBLE PRECISION", "0.0"),
    ("mae_r",           "DOUBLE PRECISION", "0.0"),
    ("max_r",           "DOUBLE PRECISION", "0.0"),
    ("max_price_seen",  "DOUBLE PRECISION", None),
    ("min_price_seen",  "DOUBLE PRECISION", None),
    ("time_minutes",    "DOUBLE PRECISION", "0.0"),

    # Metadata
    ("prebuy_id",       "TEXT",             None),
    ("user_decision",   "TEXT",             None),
    ("bot_decision",    "TEXT",             None),
    ("market_condition","TEXT",             None),
    ("why",             "TEXT",             None),
    ("why_full",        "TEXT",             None),
    ("notes",           "TEXT",             None),
    ("order_id",        "TEXT",             None),
]

# pending_approvals — geschreven door scanner, gelezen door webhook + app
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
]

# experience_scoreboard — geschreven door simulator, gelezen door scanner + app
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
# SCHEMA SYNC FUNCTIES
# ============================================================
def table_exists(cur, table: str) -> bool:
    """Controleert of een tabel bestaat in public schema."""
    cur.execute("""
    SELECT EXISTS(
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name   = %s
    )
    """, (table,))
    return bool(cur.fetchone()[0])


def column_exists(cur, table: str, column: str) -> bool:
    """Controleert of een kolom bestaat in een tabel."""
    cur.execute("""
    SELECT EXISTS(
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = %s
          AND column_name  = %s
    )
    """, (table, column))
    return bool(cur.fetchone()[0])


def add_missing_columns(
    conn,
    table:   str,
    columns: List[Tuple[str, str, Optional[str]]],
) -> int:
    """
    Voegt ontbrekende kolommen toe aan een tabel.
    Veilig — doet niets als kolom al bestaat.
    Geeft aantal toegevoegde kolommen terug.
    """
    added = 0

    with conn.cursor() as cur:
        if not table_exists(cur, table):
            log(f"⚠️ Tabel public.{table} bestaat niet — sla over")
            return 0

        for col_name, col_type, col_default in columns:
            if not column_exists(cur, table, col_name):
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
                    log(f"  ⚠️ {table}.{col_name} toevoegen mislukt: {e}")

    conn.commit()
    return added


def create_experience_trades_table(conn) -> None:
    """
    Maakt experience_trades tabel aan als die niet bestaat.
    Dit is de hoofd trade data tabel — alle bestanden gebruiken hem.
    """
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.experience_trades (
            trade_key    TEXT PRIMARY KEY,
            source       TEXT          DEFAULT 'UNKNOWN',
            coin         TEXT,
            timestamp    TIMESTAMPTZ   DEFAULT NOW(),
            created_at   TIMESTAMPTZ   DEFAULT NOW(),
            updated_at   TIMESTAMPTZ   DEFAULT NOW()
        );
        """)
    conn.commit()
    log("  ✅ experience_trades tabel aangemaakt/gecontroleerd")


def create_pending_approvals_table(conn) -> None:
    """
    Maakt pending_approvals tabel aan als die niet bestaat.
    Geschreven door multi_coin_score, gelezen door whatsapp_webhook.
    """
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.pending_approvals (
            id         TEXT PRIMARY KEY,
            symbol     TEXT,
            score      INTEGER DEFAULT 0,
            status     TEXT    DEFAULT 'PENDING',
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)
    conn.commit()
    log("  ✅ pending_approvals tabel aangemaakt/gecontroleerd")


def create_experience_scoreboard_table(conn) -> None:
    """
    Maakt experience_scoreboard tabel aan als die niet bestaat.
    Geschreven door history_simulator, gelezen door multi_coin_score.
    """
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.experience_scoreboard (
            symbol      TEXT,
            setup_type  TEXT,
            regime      TEXT,
            n           INTEGER DEFAULT 0,
            win_rate    DOUBLE PRECISION DEFAULT 0.0,
            updated_at  TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (symbol, setup_type, regime)
        );
        """)
    conn.commit()
    log("  ✅ experience_scoreboard tabel aangemaakt/gecontroleerd")


def create_bot_state_table(conn) -> None:
    """
    Maakt bot_state tabel aan als die niet bestaat.
    Gebruikt door ALLE bestanden voor bot aan/uit/gepauzeerd.
    """
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.bot_state (
            key        TEXT PRIMARY KEY,
            value      TEXT,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)
    conn.commit()
    log("  ✅ bot_state tabel aangemaakt/gecontroleerd")


def create_btc_regime_table(conn) -> None:
    """
    Maakt btc_regime_4h tabel aan als die niet bestaat.
    Geschreven door build_btc_regime.py,
    gelezen door multi_coin_score.py.
    """
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.btc_regime_4h (
            open_time    BIGINT PRIMARY KEY,
            ts_utc       TIMESTAMPTZ,
            close        DOUBLE PRECISION,
            ema200       DOUBLE PRECISION,
            ema200_slope DOUBLE PRECISION,
            regime       TEXT,
            strength     DOUBLE PRECISION DEFAULT 0.0,
            pct_from_ema DOUBLE PRECISION DEFAULT 0.0,
            updated_at   TIMESTAMPTZ DEFAULT NOW()
        );
        """)
    conn.commit()
    log("  ✅ btc_regime_4h tabel aangemaakt/gecontroleerd")


def create_market_regime_table(conn) -> None:
    """
    Maakt market_regime tabel aan als die niet bestaat.
    Geschreven door regime_labeler.py,
    optioneel gelezen door multi_coin_score.py.
    """
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.market_regime (
            symbol      TEXT NOT NULL,
            timeframe   TEXT NOT NULL,
            asof_ts     TIMESTAMPTZ NOT NULL,
            regime      TEXT,
            strength    DOUBLE PRECISION,
            sma50       DOUBLE PRECISION,
            sma200      DOUBLE PRECISION,
            score       INTEGER,
            updated_at  TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (symbol, timeframe)
        );
        """)
    conn.commit()
    log("  ✅ market_regime tabel aangemaakt/gecontroleerd")


def create_candles_table(conn) -> None:
    """
    Maakt candles tabel aan als die niet bestaat.
    Geschreven door history_fetcher.py,
    gelezen door build_btc_regime.py en regime_labeler.py.
    """
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.candles (
            exchange    TEXT        NOT NULL DEFAULT 'binance',
            symbol      TEXT        NOT NULL,
            timeframe   TEXT        NOT NULL,
            open_time   BIGINT      NOT NULL,
            open        DOUBLE PRECISION,
            high        DOUBLE PRECISION,
            low         DOUBLE PRECISION,
            close       DOUBLE PRECISION,
            volume      DOUBLE PRECISION,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            CONSTRAINT candles_unique_key
                UNIQUE (exchange, symbol, timeframe, open_time)
        );
        """)
    conn.commit()
    log("  ✅ candles tabel aangemaakt/gecontroleerd")


def create_fetcher_state_table(conn) -> None:
    """
    Maakt fetcher_state tabel aan als die niet bestaat.
    Gebruikt door history_fetcher.py voor batch rotatie.
    """
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.fetcher_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """)
    conn.commit()
    log("  ✅ fetcher_state tabel aangemaakt/gecontroleerd")


def create_btc_regime_changes_table(conn) -> None:
    """
    Maakt btc_regime_changes tabel aan als die niet bestaat.
    Gebruikt door build_btc_regime.py voor regime verandering log.
    """
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.btc_regime_changes (
            id          SERIAL PRIMARY KEY,
            old_regime  TEXT,
            new_regime  TEXT,
            close       DOUBLE PRECISION,
            ema200      DOUBLE PRECISION,
            strength    DOUBLE PRECISION,
            ts          TIMESTAMPTZ DEFAULT NOW()
        );
        """)
    conn.commit()
    log("  ✅ btc_regime_changes tabel aangemaakt/gecontroleerd")


def create_indexes(conn) -> None:
    """
    Maakt performante indexes aan voor veelgebruikte queries.

    Queries die gebaat zijn bij indexes:
    - Dagrapport in webhook: filtert op source + datum
    - Cooldown check in scanner: filtert op coin + source + outcome
    - Scoreboard lookup: filtert op symbol + setup_type + regime
    - Pending approvals: filtert op status + expires_at
    - BTC regime: sorteert op open_time DESC
    """
    indexes = [
        # experience_trades — meest gebruikte filters
        (
            "idx_exp_trades_source",
            "CREATE INDEX IF NOT EXISTS idx_exp_trades_source "
            "ON public.experience_trades (source)"
        ),
        (
            "idx_exp_trades_outcome",
            "CREATE INDEX IF NOT EXISTS idx_exp_trades_outcome "
            "ON public.experience_trades (outcome)"
        ),
        (
            "idx_exp_trades_coin_source",
            "CREATE INDEX IF NOT EXISTS idx_exp_trades_coin_source "
            "ON public.experience_trades (coin, source)"
        ),
        (
            "idx_exp_trades_exit_time",
            "CREATE INDEX IF NOT EXISTS idx_exp_trades_exit_time "
            "ON public.experience_trades (exit_time)"
        ),
        (
            "idx_exp_trades_source_outcome",
            "CREATE INDEX IF NOT EXISTS idx_exp_trades_source_outcome "
            "ON public.experience_trades (source, outcome, exit_time)"
        ),

        # pending_approvals — status filtering
        (
            "idx_pending_status",
            "CREATE INDEX IF NOT EXISTS idx_pending_status "
            "ON public.pending_approvals (status)"
        ),
        (
            "idx_pending_symbol",
            "CREATE INDEX IF NOT EXISTS idx_pending_symbol "
            "ON public.pending_approvals (symbol)"
        ),
        (
            "idx_pending_expires",
            "CREATE INDEX IF NOT EXISTS idx_pending_expires "
            "ON public.pending_approvals (expires_at)"
        ),

        # experience_scoreboard — lookup op setup/regime
        (
            "idx_scoreboard_lookup",
            "CREATE INDEX IF NOT EXISTS idx_scoreboard_lookup "
            "ON public.experience_scoreboard (symbol, setup_type, regime)"
        ),

        # candles — symbol + timeframe combinatie
        (
            "idx_candles_symbol_tf",
            "CREATE INDEX IF NOT EXISTS idx_candles_symbol_tf "
            "ON public.candles (symbol, timeframe)"
        ),
        (
            "idx_candles_open_time",
            "CREATE INDEX IF NOT EXISTS idx_candles_open_time "
            "ON public.candles (open_time DESC)"
        ),

        # btc_regime_4h — altijd open_time DESC sort
        (
            "idx_btc_regime_time",
            "CREATE INDEX IF NOT EXISTS idx_btc_regime_time "
            "ON public.btc_regime_4h (open_time DESC)"
        ),

        # market_regime — symbol lookup
        (
            "idx_market_regime_symbol",
            "CREATE INDEX IF NOT EXISTS idx_market_regime_symbol "
            "ON public.market_regime (symbol, asof_ts DESC)"
        ),
    ]

    with conn.cursor() as cur:
        for idx_name, sql in indexes:
            try:
                cur.execute(sql)
                log(f"  ✅ Index: {idx_name}")
            except Exception as e:
                log(f"  ⚠️ Index mislukt {idx_name}: {e}")

    conn.commit()


def ensure_trade_key_unique(conn) -> None:
    """
    Zorgt dat trade_key UNIQUE constraint bestaat op experience_trades.
    Gebruikt DO $$ blok zodat het veilig is als het al bestaat.
    """
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
        -- Constraint bestaat al of kan niet worden toegevoegd
        NULL;
    END
    $$;
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        log("  ✅ trade_key UNIQUE constraint gecontroleerd")
    except Exception as e:
        log(f"  ⚠️ trade_key constraint: {e}")


def sync_is_shadow_column(conn) -> None:
    """
    Synchroniseert is_shadow kolom met source kolom.
    source='SHADOW' → is_shadow=TRUE.

    Fix: is_shadow ontbrak in origineel schema.
    Nu correct gesynchroniseerd.
    """
    try:
        with conn.cursor() as cur:
            if not column_exists(cur, "experience_trades", "is_shadow"):
                log("  ⚠️ is_shadow kolom niet gevonden — skip sync")
                return
            cur.execute("""
            UPDATE public.experience_trades
            SET is_shadow = TRUE
            WHERE UPPER(COALESCE(source,'')) = 'SHADOW'
              AND (is_shadow IS NULL OR is_shadow = FALSE)
            """)
            updated = cur.rowcount
            if updated > 0:
                log(f"  ✅ is_shadow gesynchroniseerd: {updated} rijen")
        conn.commit()
    except Exception as e:
        log(f"  ⚠️ is_shadow sync fout: {e}")


def sync_result_r_column(conn) -> None:
    """
    Berekent result_r / pnl_r op basis van pnl_eur en amount_eur.
    Fix: result_r ontbrak — dashboard kon geen R-multiples tonen.

    result_r = (pnl_eur / amount_eur) * 100
    """
    try:
        with conn.cursor() as cur:
            if not column_exists(cur, "experience_trades", "result_r"):
                log("  ⚠️ result_r kolom niet gevonden — skip sync")
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
                log(f"  ✅ result_r gesynchroniseerd: {updated} rijen")
        conn.commit()
    except Exception as e:
        log(f"  ⚠️ result_r sync fout: {e}")


def initialize_bot_state(conn) -> None:
    """
    Zorgt dat de essentiële bot_state waarden bestaan.
    Als bot_active niet bestaat → zet op 'false' (veilig default).
    """
    defaults = {
        "bot_active":       "false",
        "bot_paused":       "false",
        "bot_paused_until": "",
        "bot_paused_reason": "",
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
        log("  ✅ bot_state defaults geïnitialiseerd")
    except Exception as e:
        log(f"  ⚠️ bot_state init fout: {e}")


# ============================================================
# VERIFICATIE
# ============================================================
def verify_schema(conn) -> dict:
    """
    Verifieert dat alle kritieke kolommen aanwezig zijn.
    Geeft rapport terug met missende en aanwezige kolommen.

    Kritieke kolommen zijn degene die door meerdere bestanden
    worden gebruikt en dus aanwezig moeten zijn.
    """
    kritiek = [
        # experience_trades — kern van het systeem
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

        # pending_approvals — scanner → webhook
        ("pending_approvals", "id"),
        ("pending_approvals", "symbol"),
        ("pending_approvals", "status"),
        ("pending_approvals", "bitvavo_market"),
        ("pending_approvals", "expires_at"),

        # bot_state — alle bestanden
        ("bot_state", "key"),
        ("bot_state", "value"),

        # experience_scoreboard — simulator → scanner
        ("experience_scoreboard", "symbol"),
        ("experience_scoreboard", "win_rate"),
        ("experience_scoreboard", "n"),
    ]

    rapport = {"ok": True, "missend": [], "aanwezig": []}

    with conn.cursor() as cur:
        for table, column in kritiek:
            try:
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
def sync_schema(conn=None) -> None:
    """
    Voert volledige schema synchronisatie uit.
    Veilig om meerdere keren te draaien (idempotent).

    Stappen:
    1. Alle tabellen aanmaken als ze niet bestaan
    2. Ontbrekende kolommen toevoegen
    3. UNIQUE constraint op trade_key
    4. Indexes aanmaken
    5. Data synchronisatie (is_shadow, result_r)
    6. bot_state initialiseren
    7. Verificatie
    """
    owns_conn = conn is None
    if owns_conn:
        conn = db_connect()

    try:
        log("=" * 50)
        log("Schema sync gestart...")
        log("=" * 50)

        # ── Stap 1: Tabellen aanmaken ────────────────
        log("\n📋 Stap 1: Tabellen aanmaken...")
        create_bot_state_table(conn)
        create_experience_trades_table(conn)
        create_pending_approvals_table(conn)
        create_experience_scoreboard_table(conn)
        create_btc_regime_table(conn)
        create_btc_regime_changes_table(conn)
        create_market_regime_table(conn)
        create_candles_table(conn)
        create_fetcher_state_table(conn)

        # ── Stap 2: Kolommen toevoegen ───────────────
        log("\n📋 Stap 2: Ontbrekende kolommen toevoegen...")

        n = add_missing_columns(conn, "experience_trades", EXPERIENCE_TRADES_COLUMNS)
        log(f"  → experience_trades: {n} kolommen toegevoegd")

        n = add_missing_columns(conn, "pending_approvals", PENDING_APPROVALS_COLUMNS)
        log(f"  → pending_approvals: {n} kolommen toegevoegd")

        n = add_missing_columns(conn, "experience_scoreboard", EXPERIENCE_SCOREBOARD_COLUMNS)
        log(f"  → experience_scoreboard: {n} kolommen toegevoegd")

        # ── Stap 3: UNIQUE constraint ─────────────────
        log("\n📋 Stap 3: Constraints aanmaken...")
        ensure_trade_key_unique(conn)

        # ── Stap 4: Indexes ───────────────────────────
        log("\n📋 Stap 4: Indexes aanmaken...")
        create_indexes(conn)

        # ── Stap 5: Data synchronisatie ───────────────
        log("\n📋 Stap 5: Data synchronisatie...")
        sync_is_shadow_column(conn)
        sync_result_r_column(conn)

        # ── Stap 6: bot_state initialiseren ───────────
        log("\n📋 Stap 6: bot_state initialiseren...")
        initialize_bot_state(conn)

        # ── Stap 7: Verificatie ───────────────────────
        log("\n📋 Stap 7: Verificatie...")
        rapport = verify_schema(conn)

        if rapport["ok"]:
            log(f"  ✅ Alle {len(rapport['aanwezig'])} kritieke kolommen aanwezig")
        else:
            log(f"  ⚠️ Missende kolommen: {rapport['missend']}")
            send_whatsapp(
                f"⚠️ SCHEMA SYNC WAARSCHUWING\n"
                f"{'─' * 28}\n\n"
                f"Missende kolommen:\n"
                + "\n".join(f"• {col}" for col in rapport["missend"][:5]) +
                f"\n\n🤖 Bot kan problemen ondervinden.\n"
                f"Check Render logs voor details."
            )

        log("\n" + "=" * 50)
        log("✅ Schema sync voltooid!")
        log("=" * 50)

    except Exception as e:
        log(f"❌ Schema sync fout: {type(e).__name__}: {e}")

        prompt = f"""
Je bent een crypto bot database beheerder.
Er is een schema sync fout opgetreden.

Fout: {type(e).__name__}: {str(e)[:200]}

Geef in 2 zinnen Nederlands:
1. Wat er mis is
2. Wat de gebruiker moet doen
""".strip()

        uitleg = _claude_analyse(prompt, max_tokens=150)

        send_whatsapp(
            f"🚨 SCHEMA SYNC FOUT\n"
            f"{'─' * 28}\n\n"
            f"⚠️ Fout: {type(e).__name__}\n"
            f"{str(e)[:150]}\n\n"
            f"🧠 Claude:\n{uitleg or 'Schema sync mislukt — check logs.'}\n\n"
            f"📋 WAT TE DOEN:\n"
            f"1. Check Render logs voor details\n"
            f"2. Controleer DATABASE_URL\n"
            f"3. Herstart de service\n\n"
            f"Bot kan data niet correct opslaan."
        )

        if owns_conn:
            try:
                conn.rollback()
            except Exception:
                pass
        raise

    finally:
        if owns_conn:
            try:
                conn.close()
            except Exception:
                pass


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    log("=" * 60)
    log("Fix Experience Schema v2.0 — gestart")
    log("=" * 60)
    log(f"Database:   {'✅' if DATABASE_URL else '❌ ONTBREEKT'}")
    log(f"Twilio:     {'✅' if TWILIO_ACCOUNT_SID else '⚠️ niet ingesteld'}")
    log(f"Claude API: {'✅' if ANTHROPIC_API_KEY else '⚠️ niet ingesteld'}")
    log("=" * 60)

    if not DATABASE_URL:
        log("❌ DATABASE_URL ontbreekt — kan niet doorgaan")
        sys.exit(1)

    try:
        conn = db_connect()
        log("✅ Database verbonden")

        # Voer schema sync uit
        sync_schema(conn)

        # Verificatie rapport
        log("\n📋 Verificatie rapport:")
        rapport = verify_schema(conn)

        if rapport["ok"]:
            log(f"✅ Alle kritieke kolommen aanwezig ({len(rapport['aanwezig'])} totaal)")
        else:
            log(f"⚠️ Missende kolommen ({len(rapport['missend'])}):")
            for col in rapport["missend"]:
                log(f"  ❌ {col}")

        conn.close()
        log("✅ Schema sync succesvol afgerond")

    except KeyboardInterrupt:
        log("⛔ Schema sync gestopt door gebruiker")
        sys.exit(0)
    except Exception as e:
        log(f"❌ Fatale fout: {type(e).__name__}: {e}")
        sys.exit(1)
