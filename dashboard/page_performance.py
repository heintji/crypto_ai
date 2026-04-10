import streamlit as st
import pandas as pd
from dashboard.db import get_win_rate_summary, get_win_rate_by, get_trades

def render():
    st.header("Performance & Win Rate Analyse")

    # Overall summary
    st.subheader("Totaal per Trade Type")
    summary = get_win_rate_summary()
    if not summary.empty:
        for _, row in summary.iterrows():
            total = int(row["total"])
            wins = int(row["wins"])
            losses = int(row["losses"])
            wr = round(wins / total * 100, 1) if total > 0 else 0
            with st.expander(f"{row['source']} — {wr}% win rate ({total} trades)", expanded=True):
                c1, c2, c3, c4, c5 = st.columns(5)
                with c1: st.metric("Win Rate", f"{wr}%")
                with c2: st.metric("Wins / Losses", f"{wins} / {losses}")
                with c3: st.metric("Totaal PnL", f"€{float(row['total_pnl'] or 0):+.2f}")
                with c4: st.metric("Avg Win", f"€{float(row['avg_win'] or 0):+.4f}")
                with c5: st.metric("Avg Loss", f"€{float(row['avg_loss'] or 0):.4f}")

    st.divider()

    # Source filter for breakdown
    source_options = ["Alle"] + (list(summary["source"]) if not summary.empty else [])
    selected_source = st.selectbox("Filter op type", source_options)
    source_param = None if selected_source == "Alle" else selected_source

    # Win rate by setup
    st.subheader("Win Rate per Setup Type")
    setup_df = get_win_rate_by("setup_type", source_param)
    if not setup_df.empty:
        # Color code: green for good, red for bad
        st.dataframe(setup_df.rename(columns={
            "groep": "Setup", "total": "Trades", "wins": "Wins", "losses": "Losses",
            "win_rate": "Win %", "total_pnl": "PnL €", "avg_r": "Avg R"
        }), use_container_width=True, hide_index=True)

        # Visual: best vs worst
        best = setup_df.head(3)
        worst = setup_df.tail(3)
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**✅ Beste setups:**")
            for _, r in best.iterrows():
                st.markdown(f"- {r['groep']}: **{r['win_rate']}%** ({r['total']} trades, €{float(r['total_pnl'] or 0):+.2f})")
        with col2:
            st.markdown("**❌ Slechtste setups:**")
            for _, r in worst.iterrows():
                st.markdown(f"- {r['groep']}: **{r['win_rate']}%** ({r['total']} trades, €{float(r['total_pnl'] or 0):+.2f})")

    st.divider()

    # Win rate by regime
    st.subheader("Win Rate per BTC Regime")
    regime_df = get_win_rate_by("market_regime", source_param)
    if not regime_df.empty:
        st.dataframe(regime_df.rename(columns={
            "groep": "Regime", "total": "Trades", "wins": "Wins", "losses": "Losses",
            "win_rate": "Win %", "total_pnl": "PnL €", "avg_r": "Avg R"
        }), use_container_width=True, hide_index=True)

    st.divider()

    # Win rate by coin (top/bottom)
    st.subheader("Win Rate per Coin")
    coin_df = get_win_rate_by("symbol", source_param)
    if not coin_df.empty:
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Top coins:**")
            top = coin_df.head(10)
            st.dataframe(top.rename(columns={"groep": "Coin", "win_rate": "Win %", "total": "Trades", "total_pnl": "PnL €"})[["Coin", "Win %", "Trades", "PnL €"]], hide_index=True)
        with col2:
            st.markdown("**Slechtste coins:**")
            bottom = coin_df.tail(10)
            st.dataframe(bottom.rename(columns={"groep": "Coin", "win_rate": "Win %", "total": "Trades", "total_pnl": "PnL €"})[["Coin", "Win %", "Trades", "PnL €"]], hide_index=True)
