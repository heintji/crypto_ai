"""
Crypto AI Terminal — Streamlit Dashboard v4.0
Modulair, DB-only, geen JSON.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import streamlit as st

st.set_page_config(
    page_title="Crypto AI Terminal",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Inject CSS
from dashboard.styles import inject_css
inject_css()

# Page imports
from dashboard import page_overview, page_trades, page_signals, page_performance, page_health, page_controls

# Initialize session state
if "page" not in st.session_state:
    st.session_state.page = "overview"

# Sidebar navigation
with st.sidebar:
    st.title("Crypto AI")
    st.caption("Trading Terminal v4.0")
    st.divider()

    pages = {
        "overview": "📊 Overview",
        "live": "💰 Live Trades",
        "shadow": "👻 Shadow Trades",
        "sim": "🧪 Sim Trades",
        "strategy": "📈 Strategy Trades",
        "signals": "📡 Signals",
        "performance": "📉 Performance",
        "health": "🏥 Health",
        "controls": "⚙️ Control Board",
    }

    for key, label in pages.items():
        if st.button(label, key=f"nav_{key}", use_container_width=True,
                     type="primary" if st.session_state.page == key else "secondary"):
            st.session_state.page = key
            st.rerun()

# Page routing
page = st.session_state.page

if page == "overview":
    page_overview.render()
elif page == "live":
    page_trades.render("LIVE", "💰 Live Trades")
elif page == "shadow":
    page_trades.render("SHADOW", "👻 Shadow Trades")
elif page == "sim":
    page_trades.render("SIM", "🧪 Sim Trades")
elif page == "strategy":
    page_trades.render("STRATEGY", "📈 Strategy Trades")
elif page == "signals":
    page_signals.render()
elif page == "performance":
    page_performance.render()
elif page == "health":
    page_health.render()
elif page == "controls":
    page_controls.render()
else:
    st.error(f"Onbekende pagina: {page}")

# Footer
st.divider()
from datetime import datetime, timezone
st.caption(f"Crypto AI Terminal v4.0 | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
