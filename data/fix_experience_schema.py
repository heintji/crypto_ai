from __future__ import annotations

import os
import psycopg2


# ==========================================================
# ENV
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing for fix_experience_schema.py")


# ==========================================================
# REQUIRED SCHEMA
# ==========================================================
EXPERIENCE_TRADES_COLUMNS = {
    "trade_key": "TEXT",
    "source": "TEXT",
    "timestamp": "TIMESTAMPTZ",
    "entry_time": "TIMESTAMPTZ",
    "exit_time": "TIMESTAMPTZ",
    "coin": "TEXT",
    "entry_timeframe": "TEXT",
    "regime_timeframe": "TEXT",
    "setup_type": "TEXT",
    "market_regime": "TEXT",
    "grade": "TEXT",
    "entry": "DOUBLE PRECISION",
    "stop": "DOUBLE PRECISION",
    "target": "DOUBLE PRECISION",
    "decision": "TEXT",
    "outcome": "TEXT",
    "mfe": "DOUBLE PRECISION",
    "mae": "DOUBLE PRECISION",
    "time_minutes": "INTEGER",
    "why": "TEXT",
    "market_condition": "TEXT",
    "bot_confidence": "INTEGER",
    "overextended": "DOUBLE PRECISION",
    "created_at": "TIMESTAMPTZ DEFAULT NOW()",
    "updated_at": "TIMESTAMPTZ DEFAULT NOW()",
}

EXPERIENCE_SCOREBOARD_COLUMNS = {
    "score_key": "TEXT",
    "setup_type": "TEXT",
    "market_regime": "TEXT",
    "grade": "TEXT",
    "n": "INTEGER DEFAULT 0",
    "wins": "INTEGER DEFAULT 0",
    "losses": "INTEGER DEFAULT 0",
    "timeouts": "INTEGER DEFAULT 0",
    "avg_mfe": "DOUBLE PRECISION DEFAULT 0",
    "avg_mae": "DOUBLE PRECISION DEFAULT 0",
    "avg_time_minutes": "DOUBLE PRECISION DEFAULT 0",
    "win_rate": "DOUBLE PRECISION DEFAULT 0",
    "updated_at": "TIMESTAMPTZ DEFAULT NOW()",
}


# ==========================================================
# DB HELPERS
# ==========================================================
def pg_connect():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def table_exists(cur, table_name: str) -> bool:
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
        );
        """,
        (table_name,),
    )
    return bool(cur.fetchone()[0])


def get_columns(cur, table_name: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s;
        """,
        (table_name,),
    )
    return {row[0] for row in cur.fetchall()}


# ==========================================================
# EXPERIENCE_TRADES
# ==========================================================
def ensure_experience_trades(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.experience_trades (
            id BIGSERIAL PRIMARY KEY
        );
        """
    )

    existing = get_columns(cur, "experience_trades")

    for col, col_type in EXPERIENCE_TRADES_COLUMNS.items():
        if col not in existing:
            print(f"➕ adding public.experience_trades.{col}")
            cur.execute(
                f"""
                ALTER TABLE public.experience_trades
                ADD COLUMN IF NOT EXISTS {col} {col_type};
                """
            )

    # Zorg dat trade_key uniek is voor ON CONFLICT
    cur.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'experience_trades_trade_key_key'
            ) THEN
                BEGIN
                    ALTER TABLE public.experience_trades
                    ADD CONSTRAINT experience_trades_trade_key_key UNIQUE (trade_key);
                EXCEPTION
                    WHEN duplicate_table THEN
                        NULL;
                    WHEN duplicate_object THEN
                        NULL;
                END;
            END IF;
        END $$;
        """
    )

    # Handige indexes
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_experience_trades_coin
        ON public.experience_trades(coin);
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_experience_trades_timestamp
        ON public.experience_trades(timestamp DESC);
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_experience_trades_setup_regime
        ON public.experience_trades(setup_type, market_regime, grade);
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_experience_trades_outcome
        ON public.experience_trades(outcome);
        """
    )


# ==========================================================
# EXPERIENCE_SCOREBOARD
# ==========================================================
def ensure_experience_scoreboard(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.experience_scoreboard (
            score_key TEXT PRIMARY KEY
        );
        """
    )

    existing = get_columns(cur, "experience_scoreboard")

    for col, col_type in EXPERIENCE_SCOREBOARD_COLUMNS.items():
        if col not in existing:
            print(f"➕ adding public.experience_scoreboard.{col}")
            cur.execute(
                f"""
                ALTER TABLE public.experience_scoreboard
                ADD COLUMN IF NOT EXISTS {col} {col_type};
                """
            )

    # Zorg dat score_key PK/unique bruikbaar blijft
    cur.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'experience_scoreboard_pkey'
            ) THEN
                BEGIN
                    ALTER TABLE public.experience_scoreboard
                    ADD PRIMARY KEY (score_key);
                EXCEPTION
                    WHEN duplicate_table THEN
                        NULL;
                    WHEN duplicate_object THEN
                        NULL;
                END;
            END IF;
        END $$;
        """
    )


# ==========================================================
# MAIN SYNC
# ==========================================================
def sync_schema():
    """
    Zorgt dat:
    - public.experience_trades bestaat
    - alle benodigde kolommen bestaan
    - trade_key UNIQUE is
    - public.experience_scoreboard bestaat
    - alle scoreboard kolommen bestaan
    """
    print("🔧 sync_schema: connecting to database...")

    conn = None
    try:
        conn = pg_connect()
        with conn.cursor() as cur:
            ensure_experience_trades(cur)
            ensure_experience_scoreboard(cur)

        conn.commit()
        print("✅ sync_schema: schema is correct.")

    except Exception:
        if conn:
            conn.rollback()
        raise

    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    sync_schema()
