"""Trade pages — reusable for LIVE/SHADOW/SIM."""
import streamlit as st
import pandas as pd
from dashboard.db import get_trades, get_open_trades

def render(source: str, title: str):
    st.markdown(f"### {title}")

    tab_open, tab_closed = st.tabs(["🟢 Open", "📋 Gesloten"])

    with tab_open:
        df = get_open_trades(source)
        if df.empty:
            st.info(f"Geen open {source.lower()} trades")
        else:
            st.caption(f"{len(df)} open posities")
            cols = [c for c in ["symbol","entry","stop","target","amount_eur",
                                "mfe_r","mae_r","max_price_seen","min_price_seen",
                                "score","setup_type","market_regime","entry_time"]
                    if c in df.columns]
            display = df[cols].copy()
            # Round floats
            for c in display.select_dtypes(include=["float64","float32"]).columns:
                display[c] = display[c].round(6)
            st.dataframe(display, use_container_width=True, hide_index=True)

    with tab_closed:
        df = get_trades(source=source, status="CLOSED", limit=300)
        if df.empty:
            st.info(f"Geen gesloten {source.lower()} trades")
            return

        # Filter out non-real outcomes
        real = df[df["outcome"].isin(["WIN","LOSS"])]
        wins = len(real[real["outcome"]=="WIN"])
        losses = len(real[real["outcome"]=="LOSS"])
        total = wins + losses
        wr = round(wins/total*100,1) if total > 0 else 0
        pnl = real["pnl_eur"].sum() if "pnl_eur" in real.columns else 0
        avg_r = real["result_r"].mean() if "result_r" in real.columns and not real["result_r"].isna().all() else 0

        c1, c2, c3, c4 = st.columns(4)
        with c1: st.metric("Win Rate", f"{wr}%")
        with c2: st.metric("W / L", f"{wins} / {losses}")
        with c3: st.metric("Totaal PnL", f"€{pnl:+.2f}" if pd.notna(pnl) else "€0")
        with c4: st.metric("Avg R", f"{avg_r:.2f}" if pd.notna(avg_r) else "0")

        # Trade table
        cols = [c for c in ["symbol","outcome","entry","exit_price","pnl_eur",
                            "result_r","mfe_r","mae_r","setup_type","market_regime",
                            "entry_time","exit_time","exit_reden","score","amount_eur",
                            "stop","target","max_price_seen","min_price_seen"]
                if c in df.columns]
        display = df[cols].copy()
        for c in display.select_dtypes(include=["float64","float32"]).columns:
            display[c] = display[c].round(4)
        st.dataframe(display, use_container_width=True, hide_index=True,
                     column_config={
                         "outcome": st.column_config.TextColumn("Outcome", width="small"),
                         "pnl_eur": st.column_config.NumberColumn("PnL €", format="%.4f"),
                     })
