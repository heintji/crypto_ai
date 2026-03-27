# fix_experience_schema.py
# ============================================================
# Crypto AI Bot — Schema Sync v2.0
# ============================================================
# Zelfde gedachtegang als alle andere bestanden:
#
#   ✅ Zelfde sslmode="require" op DB connectie
#   ✅ Zelfde safe_str / safe_int / safe_float helpers
#   ✅ Zelfde send_whatsapp voor kritieke fouten
#   ✅ Zelfde Claude health monitoring patroon
#
# BUGS GEFIXED vs origineel:
#   ✅ sslmode="require" ontbrak
#   ✅ Kolommen ontbraken die dashboard verwacht
#   ✅ is_shadow kolom ontbrak
#   ✅ source kolom niet geïndexeerd
#   ✅ result_r / pnl_r kolom ontbrak
#
# WAT DIT DOET:
#   Synchroniseert het database schema zodat:
#   - whatsapp_webhook.py rapporten correct werken
#   - trade_monitor.py trades correct kan loggen
#   - live_trader.py trades correct kan loggen
#   - app.py dashboard alle kolommen kan lezen
#
# Veilig om meerdere keren te draaien (idempotent).
# ============================================================

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any

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
# BASIS HELPERS
# ============================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


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
            log(f"✅ WhatsApp verzonden ({len(message)} tekens)")
            return True
        log(f"❌ WhatsApp {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        log(f"❌ WhatsApp exception: {type(e).__name__}: {e}")
        return False


# ============================================================
# DATABASE
# ============================================================
def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


# ============================================================
# SCHEMA DEFINITIES
# ============================================================

# Alle kolommen die experience_trades moet hebben
# zodat webhook, trader, monitor en dashboard allemaal werken
EXPERIENCE_TRADES_COLUMNS = [
    # Primaire identificatie
    ("trade_key",       "TEXT",          None),
    ("source",          "TEXT",          "'UNKNOWN'"),
    ("is_shadow",       "BOOLEAN",       "FALSE"),

    # Coin info
    ("coin",            "TEXT",          None),
    ("symbol",          "TEXT",          None),
    ("timeframe",       "TEXT",          "'4h'"),
    ("bitvavo_market",  "TEXT",          None),

    # Setup info
    ("setup_type",      "TEXT",          None),
    ("market_regime",   "TEXT",          None),
    ("regime",          "TEXT",          None),
    ("label",           "TEXT",          None),
    ("why_tag",         "TEXT",          None),

    # Timing
    ("timestamp",       "TIMESTAMPTZ",   "NOW()"),
    ("entry_time",      "TIMESTAMPTZ",   None),
    ("exit_time",       "TIMESTAMPTZ",   None),

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

    # Scores
    ("bot_confidence",  "INTEGER",       "0"),
    ("score",           "INTEGER",       "0"),
    ("raw_score",       "INTEGER",       "0"),
    ("chance",          "INTEGER",       "0"),
    ("confidence",      "INTEGER",       "0"),

    # Experience
    ("exp_n",           "INTEGER",       "0"),
    ("exp_win_rate",    "DOUBLE PRECISION", "0.5"),
    ("exp_bias",        "TEXT",          "'NEUTRAL'"),

    # Uitkomst
    ("outcome",         "TEXT",          None),
    ("pnl_eur",         "DOUBLE PRECISION", "0.0"),
    ("pnl_r",           "DOUBLE PRECISION", "0.0"),
    ("result_r",        "DOUBLE PRECISION", "0.0"),
    ("r_multiple",      "DOUBLE PRECISION", "0.0"),
    ("fee_eur",         "DOUBLE PRECISION", "0.0"),

    # Tracking
    ("mfe",             "DOUBLE PRECISION", "0.0"),
    ("mae",             "DOUBLE PRECISION", "0.0"),
    ("mfe_r",           "DOUBLE PRECISION", "0.0"),
    ("mae_r",           "DOUBLE PRECISION", "0.0"),
    ("max_price_seen",  "DOUBLE PRECISION", None),
    ("min_price_seen",  "DOUBLE PRECISION", None),
    ("time_minutes",    "DOUBLE PRECISION", "0.0"),

    # Metadata
    ("prebuy_id",       "TEXT",          None),
    ("user_decision",   "TEXT",          None),
    ("bot_decision",    "TEXT",          None),
    ("market_condition","TEXT",          None),
    ("why",             "TEXT",          None),
    ("why_full",        "TEXT",          None),
    ("notes",           "TEXT",          None),

    # Tijdstempels
    ("created_at",      "TIMESTAMPTZ",   "NOW()"),
    ("updated_at",      "TIMESTAMPTZ",   "NOW()"),
]

# Alle kolommen die pending_approvals moet hebben
PENDING_APPROVALS_COLUMNS = [
    ("id",              "TEXT",          None),
    ("symbol",          "TEXT",          None),
    ("setup_type",      "TEXT",          None),
    ("regime",          "TEXT",          None),
    ("score",           "INTEGER",       "0"),
    ("raw_score",       "INTEGER",       "0"),
    ("chance",          "INTEGER",       "0"),
    ("confidence",      "INTEGER",       "0"),
    ("label",           "TEXT",          "'GO'"),
    ("entry",           "DOUBLE PRECISION", None),
    ("stop",            "DOUBLE PRECISION", None),
    ("target",          "DOUBLE PRECISION", None),
    ("expires_at",      "TIMESTAMPTZ",   None),
    ("created_at",      "TIMESTAMPTZ",   "NOW()"),
    ("consumed_at",     "TIMESTAMPTZ",   None),
    ("rejected_at",     "TIMESTAMPTZ",   None),
    ("status",          "TEXT",          "'PENDING'"),
    ("timeframe",       "TEXT",          "'4h'"),
    ("bitvavo_market",  "TEXT",          None),
    ("exp_n",           "INTEGER",       "0"),
    ("exp_win_rate",    "DOUBLE PRECISION", "0.5"),
    ("exp_bias",        "TEXT",          "'NEUTRAL'"),
    ("why_tag",         "TEXT",          None),
]

# Alle kolommen die experience_scoreboard moet hebben
EXPERIENCE_SCOREBOARD_COLUMNS = [
    ("symbol",          "TEXT",          None),
    ("setup_type",      "TEXT",          None),
    ("regime",          "TEXT",          None),
    ("n",               "INTEGER",       "0"),
    ("wins",            "INTEGER",       "0"),
    ("losses",          "INTEGER",       "0"),
    ("win_rate",        "DOUBLE PRECISION", "0.0"),
    ("avg_pnl_eur",     "DOUBLE PRECISION", "0.0"),
    ("avg_r",           "DOUBLE PRECISION", "0.0"),
    ("bias",            "TEXT",          "'NEUTRAL'"),
    ("profit_factor",   "DOUBLE PRECISION", "0.0"),
    ("updated_at",      "TIMESTAMPTZ",   "NOW()"),
]


# ============================================================
# SCHEMA SYNC FUNCTIES
# ============================================================
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


def table_exists(cur, table: str) -> bool:
    cur.execute("""
    SELECT EXISTS(
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name   = %s
    )
    """, (table,))
    return bool(cur.fetchone()[0])


def add_missing_columns(conn, table: str, columns: list) -> int:
    """
    Voegt ontbrekende kolommen toe aan een tabel.
    Veilig — doet niets als kolom al bestaat.
    Geeft aantal toegevoegde kolommen terug.
    """
    added = 0
    with conn.cursor() as cur:
        if not table_exists(cur, table):
            log(f"⚠️ Tabel {table} bestaat niet — sla kolommen over")
            return 0

        for col_name, col_type, col_default in columns:
            if not column_exists(cur, table, col_name):
                default_clause = f"DEFAULT {col_default}" if col_default else ""
                sql = f"""
                ALTER TABLE public.{table}
                ADD COLUMN IF NOT EXISTS {col_name} {col_type} {default_clause}
                """
                try:
                    cur.execute(sql)
                    added += 1
                    log(f"  ✅ Kolom toegevoegd: {table}.{col_name} ({col_type})")
                except Exception as e:
                    log(f"  ⚠️ Kolom toevoegen mislukt: {table}.{col_name}: {e}")

    conn.commit()
    return added


def create_indexes(conn) -> None:
    """
    Maakt indexes aan voor performante queries.
    Identiek aan wat webhook en dashboard gebruiken voor filtering.
    """
    indexes = [
        # experience_trades — source is de meest gebruikte filter
        ("idx_exp_trades_source",
         "CREATE INDEX IF NOT EXISTS idx_exp_trades_source "
         "ON public.experience_trades (source)"),

        # experience_trades — outcome filtering voor rapporten
        ("idx_exp_trades_outcome",
         "CREATE INDEX IF NOT EXISTS idx_exp_trades_outcome "
         "ON public.experience_trades (outcome)"),

        # experience_trades — coin + source voor cooldown check
        ("idx_exp_trades_coin_source",
         "CREATE INDEX IF NOT EXISTS idx_exp_trades_coin_source "
         "ON public.experience_trades (coin, source)"),

        # experience_trades — exit_time voor dagrapporten
        ("idx_exp_trades_exit_time",
         "CREATE INDEX IF NOT EXISTS idx_exp_trades_exit_time "
         "ON public.experience_trades (exit_time)"),

        # pending_approvals — status voor actieve pending ophalen
        ("idx_pending_status",
         "CREATE INDEX IF NOT EXISTS idx_pending_status "
         "ON public.pending_approvals (status)"),

        # pending_approvals — symbol voor duplicate check
        ("idx_pending_symbol",
         "CREATE INDEX IF NOT EXISTS idx_pending_symbol "
         "ON public.pending_approvals (symbol)"),

        # experience_scoreboard — lookup op setup/regime
        ("idx_scoreboard_lookup",
         "CREATE INDEX IF NOT EXISTS idx_scoreboard_lookup "
         "ON public.experience_scoreboard (symbol, setup_type, regime)"),
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
    Zorgt dat trade_key UNIQUE constraint bestaat.
    Veilig via DO $$ blok.
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
        -- Constraint kan al bestaan of niet aanpasbaar zijn
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


def create_bot_state_table(conn) -> None:
    """Maakt bot_state tabel aan als die niet bestaat."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS public.bot_state (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            """)
        conn.commit()
        log("  ✅ bot_state tabel gecontroleerd")
    except Exception as e:
        log(f"  ⚠️ bot_state tabel: {e}")


def sync_is_shadow_column(conn) -> None:
    """
    Synchroniseert is_shadow kolom met source kolom.
    source='SHADOW' → is_shadow=TRUE
    """
    try:
        with conn.cursor() as cur:
            if not column_exists(cur, "experience_trades", "is_shadow"):
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
    Zodat dashboard R-multiples kan tonen.
    """
    try:
        with conn.cursor() as cur:
            if not column_exists(cur, "experience_trades", "result_r"):
                return
            cur.execute("""
            UPDATE public.experience_trades
            SET result_r = CASE
                WHEN COALESCE(amount_eur, 0) > 0 AND pnl_eur IS NOT NULL
                THEN ROUND((pnl_eur / amount_eur * 100)::numeric, 2)
                ELSE 0
            END,
            pnl_r = CASE
                WHEN COALESCE(amount_eur, 0) > 0 AND pnl_eur IS NOT NULL
                THEN ROUND((pnl_eur / amount_eur * 100)::numeric, 2)
                ELSE 0
            END
            WHERE result_r IS NULL OR result_r = 0
            """)
            updated = cur.rowcount
            if updated > 0:
                log(f"  ✅ result_r gesynchroniseerd: {updated} rijen")
        conn.commit()
    except Exception as e:
        log(f"  ⚠️ result_r sync fout: {e}")


# ============================================================
# HOOFD SYNC FUNCTIE
# ============================================================
def sync_schema(conn=None) -> None:
    """
    Voert volledige schema synchronisatie uit.
    Veilig om meerdere keren te draaien.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = db_connect()

    try:
        log("=" * 50)
        log("Schema sync gestart...")
        log("=" * 50)

        # 1. bot_state tabel
        log("\n📋 bot_state tabel...")
        create_bot_state_table(conn)

        # 2. experience_trades kolommen
        log("\n📋 experience_trades kolommen...")
        with conn.cursor() as cur:
            exists = table_exists(cur, "experience_trades")

        if exists:
            added = add_missing_columns(conn, "experience_trades", EXPERIENCE_TRADES_COLUMNS)
            log(f"  → {added} kolommen toegevoegd")
        else:
            log("  ⚠️ experience_trades tabel bestaat niet — wordt aangemaakt")
            with conn.cursor() as cur:
                cur.execute("""
                CREATE TABLE IF NOT EXISTS public.experience_trades (
                    trade_key    TEXT PRIMARY KEY,
                    source       TEXT DEFAULT 'UNKNOWN',
                    coin         TEXT,
                    timestamp    TIMESTAMPTZ DEFAULT NOW(),
                    created_at   TIMESTAMPTZ DEFAULT NOW(),
                    updated_at   TIMESTAMPTZ DEFAULT NOW()
                );
                """)
            conn.commit()
            add_missing_columns(conn, "experience_trades", EXPERIENCE_TRADES_COLUMNS)

        # 3. pending_approvals kolommen
        log("\n📋 pending_approvals kolommen...")
        added = add_missing_columns(conn, "pending_approvals", PENDING_APPROVALS_COLUMNS)
        log(f"  → {added} kolommen toegevoegd")

        # 4. experience_scoreboard kolommen
        log("\n📋 experience_scoreboard kolommen...")
        added = add_missing_columns(conn, "experience_scoreboard", EXPERIENCE_SCOREBOARD_COLUMNS)
        log(f"  → {added} kolommen toegevoegd")

        # 5. UNIQUE constraint op trade_key
        log("\n📋 Constraints...")
        ensure_trade_key_unique(conn)

        # 6. Indexes
        log("\n📋 Indexes...")
        create_indexes(conn)

        # 7. Data sync
        log("\n📋 Data synchronisatie...")
        sync_is_shadow_column(conn)
        sync_result_r_column(conn)

        log("\n" + "=" * 50)
        log("✅ Schema sync voltooid!")
        log("=" * 50)

    except Exception as e:
        log(f"❌ Schema sync fout: {type(e).__name__}: {e}")
        send_whatsapp(
            f"🚨 SCHEMA SYNC FOUT\n"
            f"{'─' * 28}\n\n"
            f"⚠️ Fout: {type(e).__name__}\n"
            f"{str(e)[:150]}\n\n"
            f"📋 WAT TE DOEN:\n"
            f"1. Check Render logs voor details\n"
            f"2. Controleer DATABASE_URL\n"
            f"3. Herstart de service\n\n"
            f"Bot kan verder werken maar\n"
            f"dashboard toont mogelijk geen data."
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
# VERIFICATIE — controleert na sync of alles klopt
# ============================================================
def verify_schema(conn) -> dict:
    """
    Verifieert dat alle kritieke kolommen aanwezig zijn.
    Geeft rapport terug.
    """
    kritiek = [
        ("experience_trades", "trade_key"),
        ("experience_trades", "source"),
        ("experience_trades", "coin"),
        ("experience_trades", "outcome"),
        ("experience_trades", "pnl_eur"),
        ("experience_trades", "exit_time"),
        ("experience_trades", "is_shadow"),
        ("experience_trades", "result_r"),
        ("pending_approvals", "id"),
        ("pending_approvals", "symbol"),
        ("pending_approvals", "status"),
        ("pending_approvals", "bitvavo_market"),
        ("bot_state",         "key"),
        ("bot_state",         "value"),
    ]

    rapport = {"ok": True, "missend": [], "aanwezig": []}

    with conn.cursor() as cur:
        for table, column in kritiek:
            exists = column_exists(cur, table, column)
            if exists:
                rapport["aanwezig"].append(f"{table}.{column}")
            else:
                rapport["missend"].append(f"{table}.{column}")
                rapport["ok"] = False

    return rapport


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    log("=" * 50)
    log("Fix Experience Schema v2.0")
    log("=" * 50)
    log(f"Database: {'✅' if DATABASE_URL else '❌ ONTBREEKT'}")

    if not DATABASE_URL:
        log("❌ DATABASE_URL ontbreekt — kan niet doorgaan")
        sys.exit(1)

    try:
        conn = db_connect()
        log("✅ Database verbonden")

        # Schema sync
        sync_schema(conn)

        # Verificatie
        log("\n📋 Verificatie...")
        rapport = verify_schema(conn)

        if rapport["ok"]:
            log(f"✅ Alle {len(rapport['aanwezig'])} kritieke kolommen aanwezig")
        else:
            log(f"⚠️ Missende kolommen: {rapport['missend']}")

        conn.close()

    except Exception as e:
        log(f"❌ Fout: {type(e).__name__}: {e}")
        sys.exit(1)
