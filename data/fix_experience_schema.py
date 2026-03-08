import os
import psycopg2

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable not set")


REQUIRED_COLUMNS = {
    "trade_key": "TEXT PRIMARY KEY",
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
    "bot_confidence": "DOUBLE PRECISION",
    "overextended": "DOUBLE PRECISION",

    "created_at": "TIMESTAMPTZ DEFAULT NOW()",
    "updated_at": "TIMESTAMPTZ DEFAULT NOW()"
}


def connect():
    return psycopg2.connect(DATABASE_URL)


def table_exists(cur):
    cur.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables 
            WHERE table_name = 'experience_trades'
        );
    """)
    return cur.fetchone()[0]


def create_table(cur):

    base_schema = """
    CREATE TABLE IF NOT EXISTS experience_trades (
        trade_key TEXT PRIMARY KEY
    );
    """

    cur.execute(base_schema)


def get_existing_columns(cur):

    cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'experience_trades';
    """)

    return {row[0] for row in cur.fetchall()}


def sync_schema():

    print("Connecting to database...")

    conn = connect()
    cur = conn.cursor()

    if not table_exists(cur):
        print("Table does not exist. Creating base table...")
        create_table(cur)

    existing_columns = get_existing_columns(cur)

    print("Existing columns:", existing_columns)

    for column, column_type in REQUIRED_COLUMNS.items():

        if column not in existing_columns:

            print(f"Adding missing column: {column}")

            alter = f"""
            ALTER TABLE experience_trades
            ADD COLUMN IF NOT EXISTS {column} {column_type};
            """

            cur.execute(alter)

    conn.commit()

    cur.close()
    conn.close()

    print("Schema sync complete.")


if __name__ == "__main__":
    sync_schema()
