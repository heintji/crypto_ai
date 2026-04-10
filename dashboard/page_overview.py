"""Overview page — at-a-glance bot status."""
import streamlit as st
import pandas as pd
from datetime import datetime, timezone
from dashboard.db import (get_bot_state, get_open_trades, get_daily_pnl,
                          get_portfolio_value, get_win_rate_summary, get_bot_health)
from dashboard.styles import card, badge, status_badge, status_dot

def render():
    state = get_bot_state()
    portfolio = get_portfolio_value()
    wins_today, losses_today, pnl_today = get_daily_pnl()
    open_live = get_open_trades("LIVE")
    open_shadow = get_open_trades("SHADOW")

    bot_on = state.get("bot_active","false") == "true" and state.get("bot_running","false") == "true"
    regime = state.get("btc_regime_huidig", "?")

    # Hero cards
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        color = "green" if bot_on else "red"
        st.markdown(card("Bot Status", "ACTIEF" if bot_on else "UIT", color=color), unsafe_allow_html=True)
    with c2:
        st.markdown(card("Portfolio", f"€{portfolio:.2f}", color="purple"), unsafe_allow_html=True)
    with c3:
        up = pnl_today >= 0
        st.markdown(card("PnL Vandaag", f"€{pnl_today:+.2f}",
                        f"{wins_today}W / {losses_today}L", up=up,
                        color="green" if up else "red"), unsafe_allow_html=True)
    with c4:
        st.markdown(card("Open Live", str(len(open_live)),
                        f"{len(open_shadow)} shadow"), unsafe_allow_html=True)
    with c5:
        r_color = {"BULL":"green","BEAR":"red","RANGE":"yellow"}.get(regime,"")
        st.markdown(card("BTC Regime", regime, color=r_color), unsafe_allow_html=True)

    st.markdown("")

    # Open positions table
    col_left, col_right = st.columns([2, 1])

    with col_left:
        st.markdown("### Open Live Trades")
        if not open_live.empty:
            df = open_live[["symbol","entry","stop","target","amount_eur","mfe_r","mae_r","score","setup_type","entry_time"]].copy()
            df.columns = ["Coin","Entry","Stop","Target","EUR","MFE(R)","MAE(R)","Score","Setup","Sinds"]
            # Round numeric columns
            for c in ["Entry","Stop","Target","MFE(R)","MAE(R)"]:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce").round(6)
            df["EUR"] = pd.to_numeric(df["EUR"], errors="coerce").round(2)
            df["Score"] = pd.to_numeric(df["Score"], errors="coerce").round(0)
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("Geen open live trades")

    with col_right:
        st.markdown("### Services")
        health = get_bot_health()
        now = datetime.now(timezone.utc)
        if not health.empty:
            for _, row in health.iterrows():
                lr = row["laatste_run"]
                if lr:
                    lr = lr.replace(tzinfo=timezone.utc) if lr.tzinfo is None else lr
                    mins = int((now - lr).total_seconds() / 60)
                    hb = row["heartbeat_sec"] or 60
                    ok = mins < (hb * 2 // 60 + 1)
                    label = f"{mins}m" if mins < 60 else f"{mins//60}h{mins%60}m"
                    st.markdown(f"{status_dot(ok)} **{row['service']}** · {label}", unsafe_allow_html=True)
                else:
                    st.markdown(f"{status_dot(False)} **{row['service']}** · nooit", unsafe_allow_html=True)

    # Win rate summary
    st.markdown("### Win Rate")
    wr_df = get_win_rate_summary()
    if not wr_df.empty:
        cols = st.columns(len(wr_df))
        for i, (_, row) in enumerate(wr_df.iterrows()):
            total = int(row["total"])
            wins = int(row["wins"])
            wr = round(wins/total*100,1) if total > 0 else 0
            with cols[i]:
                st.metric(row["source"], f"{wr}%", f"{wins}W/{int(row['losses'])}L · €{float(row['total_pnl'] or 0):+.2f}")
    else:
        st.info("Geen gesloten trades")
