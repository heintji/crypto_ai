import streamlit as st
from dashboard.db import get_pending_signals, get_signal_stats
from dashboard.styles import status_badge

def render():
    st.header("Pre-BUY Signals")

    # Stats summary
    stats = get_signal_stats()
    if not stats.empty:
        cols = st.columns(len(stats))
        for i, (_, row) in enumerate(stats.iterrows()):
            with cols[i % len(cols)]:
                st.metric(row["status"], int(row["n"]))

    st.divider()

    # Signal list
    signals = get_pending_signals()
    if signals.empty:
        st.info("Geen recente signalen")
        return

    # Filter
    status_filter = st.multiselect("Filter op status",
        signals["status"].unique().tolist(),
        default=["PENDING", "CONSUMED", "SHADOW"])

    filtered = signals[signals["status"].isin(status_filter)] if status_filter else signals

    cols_show = [c for c in ["symbol", "score", "status", "entry", "stop", "target",
                              "kelly_grootte_eur", "live_toegestaan", "created_at", "expires_at"]
                if c in filtered.columns]

    st.dataframe(filtered[cols_show], use_container_width=True, hide_index=True)
