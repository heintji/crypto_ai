"""Performance & Win Rate Analysis."""
import streamlit as st
import pandas as pd
from dashboard.db import get_win_rate_summary, get_win_rate_by
from dashboard.styles import card

def render():
    st.markdown("### Performance & Win Rate")

    summary = get_win_rate_summary()
    if summary.empty:
        st.info("Geen gesloten trades voor analyse")
        return

    # Summary cards per source
    cols = st.columns(len(summary))
    for i, (_, row) in enumerate(summary.iterrows()):
        total = int(row["total"])
        wins = int(row["wins"])
        losses = int(row["losses"])
        wr = round(wins/total*100,1) if total > 0 else 0
        pnl = float(row["total_pnl"] or 0)
        with cols[i]:
            color = "green" if wr >= 50 else "red"
            st.markdown(card(
                row["source"],
                f"{wr}%",
                f"{wins}W/{losses}L · €{pnl:+.2f} · R:{float(row['avg_r'] or 0):.2f}",
                color=color, up=wr>=50
            ), unsafe_allow_html=True)

    st.markdown("")

    # Filter
    source_opts = ["Alle"] + list(summary["source"])
    sel = st.selectbox("Filter op type", source_opts, key="perf_filter")
    src = None if sel == "Alle" else sel

    # Tabs for breakdown
    tab1, tab2, tab3 = st.tabs(["Per Setup", "Per Regime", "Per Coin"])

    with tab1:
        df = get_win_rate_by("setup_type", src)
        if df.empty:
            st.info("Niet genoeg data (min 3 trades per setup)")
        else:
            df = df.rename(columns={"groep":"Setup","total":"Trades","wins":"W","losses":"L",
                                     "win_rate":"Win%","total_pnl":"PnL €","avg_r":"Avg R"})
            st.dataframe(df, use_container_width=True, hide_index=True)
            # Best vs worst
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**✅ Best**")
                for _, r in df.head(3).iterrows():
                    st.markdown(f"- **{r['Setup']}**: {r['Win%']}% ({r['Trades']} trades, €{float(r['PnL €'] or 0):+.2f})")
            with c2:
                st.markdown("**❌ Worst**")
                for _, r in df.tail(3).iterrows():
                    st.markdown(f"- **{r['Setup']}**: {r['Win%']}% ({r['Trades']} trades, €{float(r['PnL €'] or 0):+.2f})")

    with tab2:
        df = get_win_rate_by("market_regime", src)
        if df.empty:
            st.info("Niet genoeg data")
        else:
            st.dataframe(df.rename(columns={"groep":"Regime","total":"Trades","wins":"W","losses":"L",
                                             "win_rate":"Win%","total_pnl":"PnL €","avg_r":"Avg R"}),
                        use_container_width=True, hide_index=True)

    with tab3:
        df = get_win_rate_by("symbol", src)
        if df.empty:
            st.info("Niet genoeg data")
        else:
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Top coins**")
                top = df.head(10).rename(columns={"groep":"Coin","win_rate":"Win%","total":"Trades","total_pnl":"PnL €"})
                st.dataframe(top[["Coin","Win%","Trades","PnL €"]], hide_index=True)
            with c2:
                st.markdown("**Slechtste coins**")
                bot = df.tail(10).rename(columns={"groep":"Coin","win_rate":"Win%","total":"Trades","total_pnl":"PnL €"})
                st.dataframe(bot[["Coin","Win%","Trades","PnL €"]], hide_index=True)
