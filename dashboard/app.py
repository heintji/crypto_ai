"""Crypto AI Terminal v4.0 — Modulair Dashboard"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
st.set_page_config(page_title="Crypto AI Terminal", page_icon="⚡", layout="wide", initial_sidebar_state="expanded")

from dashboard.styles import inject_css
inject_css()

from dashboard import page_overview, page_trades, page_signals, page_performance, page_health, page_controls, page_analyse, page_brain

if "page" not in st.session_state:
    st.session_state.page = "overview"

# Sidebar
with st.sidebar:
    st.markdown('<div class="title-glow"><h1>⚡ Crypto AI</h1></div>', unsafe_allow_html=True)
    st.caption("Trading Terminal v4.0")
    st.markdown("---")

    nav = [
        ("overview",    "📊  Overview"),
        ("live",        "💰  Live Trades"),
        ("shadow",      "👻  Shadow Trades"),
        ("replay",      "🔄  Signal Replay"),
        ("review",      "📝  Trade Review"),
        ("sim",         "🧪  Strategy Sim"),
        ("signals",     "📡  Signals"),
        ("brain",       "🧠  Brain"),
        ("analyse",     "🔬  Analyse"),
        ("performance", "📈  Performance"),
        ("health",      "🏥  Health"),
        ("controls",    "⚙️  Controls"),
    ]
    for key, label in nav:
        btn_type = "primary" if st.session_state.page == key else "secondary"
        if st.button(label, key=f"nav_{key}", use_container_width=True, type=btn_type):
            st.session_state.page = key
            st.rerun()

# Routing
p = st.session_state.page
if p == "overview":     page_overview.render()
elif p == "live":       page_trades.render("LIVE", "Live Trades")
elif p == "shadow":     page_trades.render("SHADOW", "Shadow Trades")
elif p == "replay":     page_trades.render("REPLAY", "Signal Replay")
elif p == "review":     page_trades.render("REAL_REVIEW", "Trade Review")
elif p == "sim":        page_trades.render("SIM", "Strategy Sim")
elif p == "signals":    page_signals.render()
elif p == "brain":      page_brain.render()
elif p == "analyse":    page_analyse.render()
elif p == "performance":page_performance.render()
elif p == "health":     page_health.render()
elif p == "controls":   page_controls.render()

# Footer
from datetime import datetime, timezone
st.markdown("---")
st.caption(f"⚡ Crypto AI Terminal v4.0 · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
