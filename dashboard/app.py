import os
import json
import time
import hmac
import hashlib
import requests
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt


# =========================
# Config
# =========================
API_KEY = os.getenv("BITVAVO_API_KEY")
API_SECRET = os.getenv("BITVAVO_API_SECRET")
SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", "/data/account_snapshot.json")
BASE_URL = "https://api.bitvavo.com/v2"


# =========================
# Helpers
# =========================
def bitvavo_request(path: str):
    """
    Signed Bitvavo request.
    """
    if not API_KEY or not API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY of BITVAVO_API_SECRET ontbreekt in Environment Variables.")

    timestamp = str(int(time.time() * 1000))
    message = timestamp + "GET" + path
    signature = hmac.new(API_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

    headers = {
        "bitvavo-access-key": API_KEY,
        "bitvavo-access-signature": signature,
        "bitvavo-access-timestamp": timestamp,
        "bitvavo-access-window": "10000",
    }

    url = BASE_URL + path
    r = requests.get(url, headers=headers, timeout=15)
    if r.status_code != 200:
        try:
            err = r.json()
        except Exception:
            err = r.text
        raise RuntimeError(f"Bitvavo error {r.status_code}: {err}")
    return r.json()


def load_snapshot():
    if not os.path.exists(SNAPSHOT_PATH):
        return None
    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_snapshot(data: dict):
    # Zorg dat map bestaat
    folder = os.path.dirname(SNAPSHOT_PATH)
    if folder and not os.path.exists(folder):
        os.makedirs(folder, exist_ok=True)

    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def fetch_and_build_snapshot():
    """
    Haalt LIVE balance op en bouwt een snapshot JSON die dashboard kan tonen.
    """
    balances = bitvavo_request("/balance")  # list dicts

    # Alleen assets > 0
    assets = []
    eur_available = 0.0

    for b in balances:
        symbol = b.get("symbol")
        available = float(b.get("available", 0) or 0)
        in_order = float(b.get("inOrder", 0) or 0)
        total = available + in_order

        if symbol == "EUR":
            eur_available = available

        if total > 0:
            assets.append(
                {
                    "symbol": symbol,
                    "available": available,
                    "inOrder": in_order,
                    "total": total,
                }
            )

    snapshot = {
        "status": "OK",
        "timestamp": int(time.time()),
        "eur_available": eur_available,
        "assets": assets,
    }

    save_snapshot(snapshot)
    return snapshot


# =========================
# UI
# =========================
st.set_page_config(page_title="Crypto AI Dashboard", layout="wide")
st.title("📊 Crypto AI Dashboard")
st.caption("Live overzicht van saldo, assets en bot-status")

# Snapshot pad blok
st.subheader("Snapshot pad")
st.code(SNAPSHOT_PATH)

colA, colB = st.columns([1, 3])
with colA:
    refresh = st.button("🔄 Snapshot nu verversen")

# Load huidige snapshot (of maak nieuw als je klikt)
snapshot = load_snapshot()

if refresh:
    try:
        snapshot = fetch_and_build_snapshot()
        st.success("✅ Snapshot gemaakt / ververst")
    except Exception as e:
        st.error(f"Verversen mislukt: {e}")

# Als nog geen snapshot bestaat
if not snapshot:
    st.warning("⚠️ account_snapshot.json niet gevonden. Klik op 'Snapshot nu verversen' om hem aan te maken.")
    st.stop()

# Status
if snapshot.get("status") != "OK":
    st.error(f"Snapshot status is niet OK: {snapshot}")
    st.stop()

# Saldo
st.success("✅ Bitvavo saldo opgehaald")
st.metric("EUR beschikbaar", f"€ {snapshot.get('eur_available', 0):.2f}")

# Assets tabel
assets = snapshot.get("assets", [])
df = pd.DataFrame(assets)

st.subheader("Assets (alleen > 0)")
if df.empty:
    st.info("Geen assets gevonden.")
else:
    st.dataframe(df, use_container_width=True)

# =========================
# Simpele grafiek (voorbeeld)
# =========================
# Dit is nog basic: hij maakt een grafiek van totals per coin.
# Later koppelen we hier bot-logs, trades, winrate, PnL etc.
if not df.empty:
    st.subheader("Grafiek (voorbeeld): Total per asset")
    fig, ax = plt.subplots()
    ax.bar(df["symbol"], df["total"])
    ax.set_xlabel("Asset")
    ax.set_ylabel("Total")
    st.pyplot(fig)

st.caption("Als dit werkt, koppelen we hierna je bot-logs en trades hieronder.")
