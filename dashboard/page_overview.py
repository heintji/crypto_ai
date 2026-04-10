import streamlit as st
import pandas as pd
from datetime import datetime, timezone
from dashboard.db import (get_bot_state, get_bot_val, get_open_trades,
                          get_daily_pnl, get_portfolio_value, get_win_rate_summary,
                          get_bot_health)
from dashboard.styles import metric_html, badge, status_badge

def render():
    st.header("Overview")

    # Top metrics row
    state = get_bot_state()
    portfolio = get_portfolio_value()
    wins_today, losses_today, pnl_today = get_daily_pnl()
    open_live = get_open_trades("LIVE")
    open_shadow = get_open_trades("SHADOW")

    bot_active = state.get("bot_active", "false") == "true"
    bot_running = state.get("bot_running", "false") == "true"
    regime = state.get("btc_regime_huidig", "UNKNOWN")

    # Status row
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        status = "ACTIEF" if (bot_active and bot_running) else "GESTOPT"
        color = "green" if status == "ACTIEF" else "red"
        st.markdown(metric_html("Bot Status", status_badge(status)), unsafe_allow_html=True)
    with c2:
        st.metric("Portfolio", f"€{portfolio:.2f}")
    with c3:
        st.metric("PnL Vandaag", f"€{pnl_today:+.2f}", f"{wins_today}W / {losses_today}L")
    with c4:
        st.metric("Open Live", str(len(open_live)))
    with c5:
        regime_colors = {"BULL": "green", "BEAR": "red", "RANGE": "yellow"}
        st.markdown(metric_html("BTC Regime", f'<span class="badge badge-{regime_colors.get(regime, "blue")}">{regime}</span>'), unsafe_allow_html=True)

    st.divider()

    # Open positions
    if not open_live.empty:
        st.subheader(f"Open Live Trades ({len(open_live)})")
        display_df = open_live[["symbol", "entry", "stop", "target", "amount_eur",
                                "mfe_r", "mae_r", "score", "setup_type", "entry_time"]].copy()
        display_df.columns = ["Coin", "Entry", "Stop", "Target", "EUR", "MFE (R)", "MAE (R)", "Score", "Setup", "Sinds"]
        st.dataframe(display_df, use_container_width=True, hide_index=True)
    else:
        st.info("Geen open live trades")

    # Win rate summary
    wr_df = get_win_rate_summary()
    if not wr_df.empty:
        st.subheader("Win Rate per Type")
        for _, row in wr_df.iterrows():
            total = int(row["total"])
            wins = int(row["wins"])
            wr = round(wins / total * 100, 1) if total > 0 else 0
            source = row["source"]
            col1, col2, col3, col4 = st.columns(4)
            with col1: st.metric(source, f"{wr}%")
            with col2: st.metric("Trades", f"{wins}W / {int(row['losses'])}L")
            with col3: st.metric("Totaal PnL", f"€{float(row['total_pnl'] or 0):+.2f}")
            with col4: st.metric("Avg R", f"{float(row['avg_r'] or 0):.2f}")

    # Service health
    st.subheader("Services")
    health = get_bot_health()
    if not health.empty:
        now = datetime.now(timezone.utc)
        cols = st.columns(len(health))
        for i, (_, row) in enumerate(health.iterrows()):
            with cols[i % len(cols)]:
                lr = row["laatste_run"]
                if lr:
                    if lr.tzinfo is None:
                        lr = lr.replace(tzinfo=timezone.utc)
                    mins = int((now - lr).total_seconds() / 60)
                    ok = mins < (row["heartbeat_sec"] * 2 // 60)
                    st.markdown(f"{'🟢' if ok else '🔴'} **{row['service']}** ({mins}m)")
                else:
                    st.markdown(f"⚪ **{row['service']}** (nooit)")
