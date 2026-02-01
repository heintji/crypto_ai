import os
import json
import time
import hmac
import hashlib
import requests
import streamlit as st

# ===============================
# Config
# ===============================
API_KEY = os.getenv("BITVAVO_API_KEY")
API_SECRET = os.getenv("BITVAVO_API_SECRET")
SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", "/data/account_snapshot.json")

BASE_URL = "https://api.bitvavo.com/v2"

st.set_page_config(page_title="Crypto AI Dashboard", layout="wide")
st.title("📊 Crypto AI Dashboard")
st.caption("Live overzicht van saldo, assets en bot-status")

# ===============================
# Bitvavo helper
# ===============================
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

# ===============================
# Snapshot maken
# ===============================
def create_snapshot():
    balances = bitvavo_request("/balance")
    snapshot = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "balances": balances
    }

    os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
    with open(SNAPSHOT_PATH, "w") as f:
        json.dump(snapshot, f, indent=2)

    return snapshot

# ===============================
# Snapshot laden of maken
# ===============================
try:
    if not os.path.exists(SNAPSHOT_PATH):
        st.info("📥 Snapshot niet gevonden, wordt nu aangemaakt...")
        data = create_snapshot()
    else:
        with open(SNAPSHOT_PATH, "r") as f:
            data = json.load(f)

except Exception as e:
    st.error("❌ Fout bij ophalen snapshot")
    st.exception(e)
    st.stop()

# ===============================
# Weergave
# ===============================
st.success(f"✅ Snapshot geladen ({data['timestamp']})")

balances = data.get("balances", [])
non_zero = [b for b in balances if float(b.get("available", 0)) > 0]

if not non_zero:
    st.warning("Geen assets met saldo gevonden.")
else:
    st.subheader("💰 Assets")
    st.table([
        {
            "Asset": b["symbol"],
            "Beschikbaar": b["available"],
            "In orders": b["inOrder"]
        }
        for b in non_zero
    ])
