import os
import json
import time
import hmac
import hashlib
import requests
import streamlit as st
import pandas as pd

# =========================
# Config
# =========================
API_KEY = os.getenv("BITVAVO_API_KEY")
API_SECRET = os.getenv("BITVAVO_API_SECRET")
SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", "/data/account_snapshot.json")
BASE_URL = "https://api.bitvavo.com/v2"

st.set_page_config(page_title="Crypto AI Dashboard", layout="wide")
st.title("📊 Crypto AI Dashboard")
st.caption("Live overzicht van saldo, assets en bot-status")

# =========================
# Bitvavo helper
# =========================
def bitvavo_request(path):
    timestamp = str(int(time.time() * 1000))
    message = timestamp + "GET" + path
    signature = hmac.new(
        API_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Signature": signature,
        "Bitvavo-Access-Timestamp": timestamp,
        "Bitvavo-Access-Window": "10000"
    }

    r = requests.get(BASE_URL + path, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()

# =========================
# Snapshot ophalen
# =========================
st.subheader("Snapshot pad")
st.code(SNAPSHOT_PATH)

if st.button("🔄 Snapshot nu verversen"):
    try:
        balances = bitvavo_request("/balance")
        snapshot = {
            "timestamp": int(time.time()),
            "balances": balances
        }
        os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
        with open(SNAPSHOT_PATH, "w") as f:
            json.dump(snapshot, f, indent=2)
        st.success("Snapshot succesvol bijgewerkt")
    except Exception as e:
        st.error(f"Snapshot fout: {e}")

# =========================
# Snapshot laden
# =========================
if not os.path.exists(SNAPSHOT_PATH):
    st.warning("Nog geen snapshot gevonden.")
    st.stop()

with open(SNAPSHOT_PATH) as f:
    snapshot = json.load(f)

balances = snapshot["balances"]

# =========================
# Data verwerken
# =========================
rows = []
total_eur = 0.0

for b in balances:
    total = float(b["available"]) + float(b["inOrder"])
    if total > 0:
        rows.append({
            "symbol": b["symbol"],
            "available": float(b["available"]),
            "inOrder": float(b["inOrder"]),
            "total": total
        })
        if b["symbol"] == "EUR":
            total_eur = total

df = pd.DataFrame(rows)

# =========================
# Overzicht
# =========================
st.success("✅ Bitvavo saldo opgehaald")

st.metric("💶 EUR beschikbaar", f"€ {total_eur:,.2f}")

st.subheader("Assets (alleen > 0)")
st.dataframe(df, use_container_width=True)

# =========================
# Percentuele verdeling
# =========================
st.subheader("📈 Assetverdeling (%)")

if total_eur > 0:
    df["percentage"] = (df["total"] / df["total"].sum()) * 100
    chart_df = df.set_index("symbol")["percentage"]
    st.bar_chart(chart_df)
else:
    st.info("Geen EUR saldo om percentages te berekenen")

st.caption("Volgende stap: trades, bot-logs, performance & AI-analyses")
