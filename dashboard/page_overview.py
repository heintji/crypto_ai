"""Overview page — at-a-glance bot status with charts."""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timezone
from dashboard.db import (get_bot_state, get_open_trades, get_daily_pnl,
                          get_portfolio_value, get_win_rate_summary, get_bot_health,
                          get_pnl_per_day, get_outcome_distribution)
from dashboard.styles import card, status_dot

def render():
    state = get_bot_state()
    portfolio = get_portfolio_value()
    wins_today, losses_today, pnl_today = get_daily_pnl()
    open_live = get_open_trades("LIVE")
    open_shadow = get_open_trades("SHADOW")

    bot_on = state.get("bot_active", "false") == "true" and state.get("bot_running", "false") == "true"
    regime = state.get("btc_regime_huidig", "?")

    # Hero cards
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        color = "green" if bot_on else "red"
        st.markdown(card("Bot Status", "ACTIEF" if bot_on else "UIT", color=color), unsafe_allow_html=True)
    with c2:
        st.markdown(card("Portfolio", f"\u20ac{portfolio:.2f}", color="purple"), unsafe_allow_html=True)
    with c3:
        up = pnl_today >= 0
        st.markdown(card("PnL Vandaag", f"\u20ac{pnl_today:+.2f}",
                         f"{wins_today}W / {losses_today}L", up=up,
                         color="green" if up else "red"), unsafe_allow_html=True)
    with c4:
        st.markdown(card("Open Live", str(len(open_live)),
                         f"{len(open_shadow)} shadow"), unsafe_allow_html=True)
    with c5:
        r_color = {"BULL": "green", "BEAR": "red", "RANGE": "yellow"}.get(regime, "")
        st.markdown(card("BTC Regime", regime, color=r_color), unsafe_allow_html=True)

    st.markdown("")

    # Charts row
    chart_left, chart_right = st.columns([2, 1])

    with chart_left:
        st.markdown("### Equity Curve (30d)")
        pnl_df = get_pnl_per_day(days=30)
        if not pnl_df.empty and "cum_pnl" in pnl_df.columns:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=pnl_df["dag"], y=pnl_df["cum_pnl"],
                mode="lines+markers",
                line=dict(color="#3b82f6", width=2),
                marker=dict(size=5),
                fill="tozeroy",
                fillcolor="rgba(59,130,246,0.1)",
                name="Cumulatieve PnL"
            ))
            fig.update_layout(
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                margin=dict(l=0, r=0, t=10, b=0),
                height=250,
                yaxis=dict(title="PnL (\u20ac)", gridcolor="rgba(255,255,255,0.05)"),
                xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
                showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Geen trade data voor equity curve")

    with chart_right:
        st.markdown("### Win / Loss")
        dist = get_outcome_distribution()
        if not dist.empty:
            wins = int(dist[dist["outcome"] == "WIN"]["n"].sum()) if "WIN" in dist["outcome"].values else 0
            losses = int(dist[dist["outcome"] == "LOSS"]["n"].sum()) if "LOSS" in dist["outcome"].values else 0
            total = wins + losses
            wr = round(wins / total * 100, 1) if total > 0 else 0
            lr = round(losses / total * 100, 1) if total > 0 else 0

            fig = go.Figure(data=[go.Pie(
                labels=["Win", "Loss"],
                values=[wins, losses],
                hole=0.6,
                marker=dict(colors=["#10b981", "#ef4444"]),
                textinfo="value",
                textfont=dict(size=14, color="white"),
            )])
            fig.update_layout(
                template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                margin=dict(l=0, r=0, t=10, b=0),
                height=200,
                showlegend=False,
                annotations=[dict(text=f"{wr}%", x=0.5, y=0.5,
                                  font_size=24, font_color="#10b981",
                                  showarrow=False)],
            )
            st.plotly_chart(fig, use_container_width=True)
            st.caption(f"Win: {wins} ({wr}%) | Loss: {losses} ({lr}%)")
        else:
            st.info("Geen data")

    # Open positions + Services
    col_left, col_right = st.columns([2, 1])

    with col_left:
        st.markdown("### Open Live Trades")
        if not open_live.empty:
            df = open_live[["symbol", "entry", "stop", "target", "amount_eur",
                            "mfe_r", "mae_r", "score", "setup_type", "entry_time"]].copy()
            df.columns = ["Coin", "Entry", "Stop", "Target", "EUR", "MFE(R)", "MAE(R)", "Score", "Setup", "Sinds"]
            for c in ["Entry", "Stop", "Target", "MFE(R)", "MAE(R)"]:
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
                    label = f"{mins}m" if mins < 60 else f"{mins // 60}h{mins % 60}m"
                    st.markdown(f"{status_dot(ok)} **{row['service']}** \u00b7 {label}", unsafe_allow_html=True)
                else:
                    st.markdown(f"{status_dot(False)} **{row['service']}** \u00b7 nooit", unsafe_allow_html=True)

    # Win/Loss rate per type
    st.markdown("### Win & Loss Rate per Type")
    wr_df = get_win_rate_summary()
    if not wr_df.empty:
        cols = st.columns(len(wr_df))
        for i, (_, row) in enumerate(wr_df.iterrows()):
            total = int(row["total"])
            wins = int(row["wins"])
            losses = int(row["losses"])
            wr = round(wins / total * 100, 1) if total > 0 else 0
            lr = round(losses / total * 100, 1) if total > 0 else 0
            with cols[i]:
                st.metric(f"{row['source']} Win Rate", f"{wr}%",
                          f"{wins}W/{losses}L ({lr}% loss)")
                st.metric("PnL", f"\u20ac{float(row['total_pnl'] or 0):+.2f}",
                          f"Avg R: {float(row['avg_r'] or 0):.2f}")
    else:
        st.info("Geen gesloten trades")
