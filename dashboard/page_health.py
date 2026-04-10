"""Service Health page."""
import streamlit as st
from datetime import datetime, timezone
from dashboard.db import get_bot_health, get_bot_state
from dashboard.styles import status_dot, card

def render():
    st.markdown("### Service Health")

    health = get_bot_health()
    state = get_bot_state()
    now = datetime.now(timezone.utc)

    if health.empty:
        st.warning("Geen health data")
        return

    # Service grid
    cols = st.columns(3)
    for i, (_, row) in enumerate(health.iterrows()):
        svc = row["service"]
        lr = row["laatste_run"]
        hb = row["heartbeat_sec"] or 60

        if lr:
            lr = lr.replace(tzinfo=timezone.utc) if lr.tzinfo is None else lr
            mins = int((now - lr).total_seconds() / 60)
            ok = mins < (hb * 2 // 60 + 1)
            if mins < 60:
                t = f"{mins}m geleden"
            elif mins < 1440:
                t = f"{mins//60}h {mins%60}m geleden"
            else:
                t = f"{mins//1440}d geleden"
        else:
            ok = False
            t = "nooit"

        with cols[i % 3]:
            color = "green" if ok else "red"
            st.markdown(card(svc, t, row["details"] or "", color=color, up=ok), unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("### Timestamps")
    ts_keys = [
        ("live_trader_last_ts", "Live Trader"),
        ("trade_monitor_last_check", "Trade Monitor"),
        ("scanner_last_run", "Scanner"),
        ("coach_laatste_volledige_run", "AI Coach"),
        ("dagrapport_datum", "Dagrapport"),
    ]
    c1, c2 = st.columns(2)
    for i, (key, label) in enumerate(ts_keys):
        val = str(state.get(key, "—"))[:25]
        with (c1 if i % 2 == 0 else c2):
            st.text(f"{label}: {val}")
