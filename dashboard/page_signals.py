"""Pre-BUY Signals page."""
import streamlit as st
from dashboard.db import get_pending_signals, get_signal_stats
from dashboard.styles import badge

def render():
    st.markdown("### Pre-BUY Signals")

    stats = get_signal_stats()
    if not stats.empty:
        cols = st.columns(min(len(stats), 6))
        for i, (_, row) in enumerate(stats.iterrows()):
            with cols[i % len(cols)]:
                color_map = {"PENDING":"yellow","CONSUMED":"green","SHADOW":"blue",
                             "EXPIRED":"red","FAILED":"red","SKIPPED":"purple"}
                c = color_map.get(row["status"], "blue")
                label = f'{row["status"]}: {int(row["n"])}'
                st.markdown(badge(label, c), unsafe_allow_html=True)

    st.markdown("")
    signals = get_pending_signals()
    if signals.empty:
        st.info("Geen recente signalen")
        return

    statuses = signals["status"].unique().tolist()
    defaults = [s for s in ["PENDING","CONSUMED","SHADOW"] if s in statuses]
    selected = st.multiselect("Filter", statuses, default=defaults, key="sig_filter")

    filtered = signals[signals["status"].isin(selected)] if selected else signals
    cols = [c for c in ["symbol","score","status","entry","stop","target",
                        "kelly_grootte_eur","live_toegestaan","created_at"]
            if c in filtered.columns]
    st.dataframe(filtered[cols], use_container_width=True, hide_index=True)
