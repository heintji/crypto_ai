import os
import psycopg2
import pandas as pd

DATABASE_URL = os.getenv("DATABASE_URL")

conn = psycopg2.connect(DATABASE_URL)

# Load BTC 4h candles
query = """
SELECT open_time, close
FROM candles
WHERE symbol = 'BTCUSDT'
AND timeframe = '4h'
ORDER BY open_time ASC;
"""

df = pd.read_sql(query, conn)

# Calculate EMA200
df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

# Regime logic
def get_regime(row):
    if row["close"] > row["ema200"]:
        return "BULL"
    else:
        return "BEAR"

df["regime"] = df.apply(get_regime, axis=1)

# Write to table
cur = conn.cursor()

cur.execute("DELETE FROM btc_regime_4h;")

for _, row in df.iterrows():
    cur.execute(
        """
        INSERT INTO btc_regime_4h (open_time, regime, ema200, close)
        VALUES (%s, %s, %s, %s)
        """,
        (row["open_time"], row["regime"], row["ema200"], row["close"]),
    )

conn.commit()
cur.close()
conn.close()

print("BTC regime build complete.")
