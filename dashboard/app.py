import json
import os
from datetime import datetime

import pandas as pd
import streamlit as st
import plotly.express as px

# ======================
# PAGINA INSTELLING
# ======================
st.set_page_config(
    page_title="Crypto AI Dashboard",
    layout="wide"
)

st.title("📊 Crypto AI Dashboard")
st.caption("Live overzicht van saldo, assets en bot-status")

# ======================
# DATA PAD
# ======================
SNAPSHOT_PATH = os.path.join(
    os.getcwd(),
    "data",
    "account_snapshot.json"
)

# ======================
# DATA LADEN
# ======================
if not os.path.exists(SNAPSHOT_PATH):
    st.error("❌ account_snapshot.json niet gevonden")
    st.stop()

with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
    snapshot = json.load(f)

# ======================
# BASIS INFO
# ======================
timestamp = datetime.fromtimestamp(snapshot["timestamp"])
status = snapshot.get("status", "UNKNOWN")
eur_available = snapshot.get("eur_available", 0.0)
assets = snapshot.get("assets", {})
open_orders = snapshot.get("open_orders", 0)

# ======================
# BOVENSTE CARDS
# ======================
col1, col2, col3, col4 = st.columns(4)

col1.metric("💶 EUR beschikbaar", f"€ {eur_available:,.2f}")
col2.metric("📦 Assets", len(assets))
col3.metric("📑 Open orders", open_orders)
col4.metric("⚙️ Status", status)

st.divider()

# ======================
# ASSETS TABEL
# ======================
if assets:
    df_assets = pd.DataFrame(
        [{"Asset": k, "Aantal": v} for k, v in assets.items()]
    )

    st.subheader("📋 Assets overzicht")
    st.dataframe(df_assets, use_container_width=True)

    # ======================
    # BAR CHART
    # ======================
    st.subheader("📊 Asset verdeling")

    fig = px.bar(
        df_assets,
        x="Asset",
        y="Aantal",
        text="Aantal",
        height=400
    )
    fig.update_traces(textposition="outside")

    st.plotly_chart(fig, use_container_width=True)

else:
    st.info("Geen assets gevonden")

st.divider()

# ======================
# FOOTER
# ======================
st.caption(f"Laatst bijgewerkt: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}")

