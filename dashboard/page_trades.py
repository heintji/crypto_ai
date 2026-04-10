import streamlit as st
import pandas as pd
from dashboard.db import get_trades, get_open_trades

def render(source: str, title: str):
    st.header(title)

    # Tabs: open vs closed
    tab_open, tab_closed = st.tabs(["Open", "Gesloten"])

    with tab_open:
        open_df = get_open_trades(source)
        if open_df.empty:
            st.info(f"Geen open {source} trades")
        else:
            st.metric("Open posities", len(open_df))

            # Format for display
            display = open_df.copy()
            cols_show = [c for c in ["symbol", "entry", "stop", "target", "amount_eur",
                                      "mfe_r", "mae_r", "score", "setup_type",
                                      "market_regime", "entry_time", "max_price_seen", "min_price_seen"]
                        if c in display.columns]
            st.dataframe(display[cols_show], use_container_width=True, hide_index=True)

    with tab_closed:
        closed_df = get_trades(source=source, status="CLOSED", limit=200)
        if closed_df.empty:
            st.info(f"Geen gesloten {source} trades")
        else:
            # Summary metrics
            wins = len(closed_df[closed_df["outcome"] == "WIN"])
            losses = len(closed_df[closed_df["outcome"] == "LOSS"])
            total = wins + losses
            wr = round(wins / total * 100, 1) if total > 0 else 0
            total_pnl = closed_df["pnl_eur"].sum() if "pnl_eur" in closed_df.columns else 0
            avg_r = closed_df["result_r"].mean() if "result_r" in closed_df.columns else 0

            c1, c2, c3, c4 = st.columns(4)
            with c1: st.metric("Win Rate", f"{wr}%")
            with c2: st.metric("W/L", f"{wins} / {losses}")
            with c3: st.metric("Totaal PnL", f"€{total_pnl:+.2f}")
            with c4: st.metric("Avg R", f"{avg_r:.2f}" if pd.notna(avg_r) else "0")

            # Trade table
            cols_show = [c for c in ["symbol", "outcome", "entry", "exit_price", "pnl_eur",
                                      "result_r", "mfe_r", "mae_r", "setup_type", "market_regime",
                                      "entry_time", "exit_time", "exit_reden", "score", "amount_eur",
                                      "stop", "target", "max_price_seen", "min_price_seen"]
                        if c in closed_df.columns]

            # Color the outcome column
            def color_outcome(val):
                if val == "WIN": return "color: #3fb950"
                if val == "LOSS": return "color: #f85149"
                return ""

            styled = closed_df[cols_show].style.applymap(color_outcome, subset=["outcome"] if "outcome" in cols_show else [])
            st.dataframe(styled, use_container_width=True, hide_index=True)
