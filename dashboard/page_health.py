import streamlit as st
from datetime import datetime, timezone
from dashboard.db import get_bot_health, get_bot_state

def render():
    st.header("Service Health")

    health = get_bot_health()
    state = get_bot_state()
    now = datetime.now(timezone.utc)

    if health.empty:
        st.warning("Geen health data beschikbaar")
        return

    for _, row in health.iterrows():
        service = row["service"]
        status = row["status"]
        lr = row["laatste_run"]
        hb_sec = row["heartbeat_sec"] or 60
        details = row["details"] or ""
        fout = row["fout"] or ""

        if lr:
            if lr.tzinfo is None:
                lr = lr.replace(tzinfo=timezone.utc)
            mins = int((now - lr).total_seconds() / 60)
            max_mins = hb_sec * 2 // 60
            is_ok = mins < max_mins
            time_str = f"{mins} min geleden"
        else:
            is_ok = False
            time_str = "nooit"
            mins = 999

        icon = "🟢" if is_ok else "🔴"

        with st.expander(f"{icon} {service} — {time_str}", expanded=not is_ok):
            c1, c2, c3 = st.columns(3)
            with c1: st.metric("Status", status)
            with c2: st.metric("Interval", f"{hb_sec}s")
            with c3: st.metric("Laatste run", time_str)
            if details:
                st.text(f"Details: {details[:200]}")
            if fout:
                st.error(f"Fout: {fout[:200]}")

    # Extra: key timestamps from bot_state
    st.divider()
    st.subheader("Bot Timestamps")
    timestamps = {
        "Live Trader": state.get("live_trader_last_ts", "?"),
        "Trade Monitor": state.get("trade_monitor_last_check", "?"),
        "Scanner": state.get("scanner_last_run", "?"),
        "Coach": state.get("coach_laatste_volledige_run", "?"),
        "Dagrapport": state.get("dagrapport_datum", "?"),
    }
    for name, ts in timestamps.items():
        st.text(f"{name}: {str(ts)[:25]}")
