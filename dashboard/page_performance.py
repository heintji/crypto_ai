"""Performance & Win Rate Analysis with charts."""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from dashboard.db import (get_win_rate_summary, get_win_rate_by,
                          get_pnl_per_day, get_outcome_distribution, get_pnl_histogram)
from dashboard.styles import card

def render():
    st.markdown("### Performance & Win Rate")

    summary = get_win_rate_summary()
    if summary.empty:
        st.info("Geen gesloten trades voor analyse")
        return

    # Summary cards per source — win + loss rate
    cols = st.columns(len(summary))
    for i, (_, row) in enumerate(summary.iterrows()):
        total = int(row["total"])
        wins = int(row["wins"])
        losses = int(row["losses"])
        wr = round(wins / total * 100, 1) if total > 0 else 0
        lr = round(losses / total * 100, 1) if total > 0 else 0
        pnl = float(row["total_pnl"] or 0)
        with cols[i]:
            color = "green" if wr >= 50 else "red"
            st.markdown(card(
                row["source"],
                f"{wr}% win",
                f"{lr}% loss | {wins}W/{losses}L | \u20ac{pnl:+.2f} | R:{float(row['avg_r'] or 0):.2f}",
                color=color, up=wr >= 50
            ), unsafe_allow_html=True)

    st.markdown("")

    # Filter
    source_opts = ["Alle"] + list(summary["source"])
    sel = st.selectbox("Filter op type", source_opts, key="perf_filter")
    src = None if sel == "Alle" else sel

    # Charts row
    ch1, ch2 = st.columns(2)

    with ch1:
        st.markdown("#### Equity Curve")
        pnl_df = get_pnl_per_day(source=src, days=60)
        if not pnl_df.empty and "cum_pnl" in pnl_df.columns:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=pnl_df["dag"], y=pnl_df["cum_pnl"],
                mode="lines", line=dict(color="#3b82f6", width=2),
                fill="tozeroy", fillcolor="rgba(59,130,246,0.08)",
            ))
            # Daily bars
            colors = ["#10b981" if v >= 0 else "#ef4444" for v in pnl_df["pnl_dag"]]
            fig.add_trace(go.Bar(
                x=pnl_df["dag"], y=pnl_df["pnl_dag"],
                marker_color=colors, opacity=0.4, yaxis="y2",
            ))
            fig.update_layout(
                template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)", height=300,
                margin=dict(l=0, r=0, t=10, b=0), showlegend=False,
                yaxis=dict(title="Cum PnL \u20ac", gridcolor="rgba(255,255,255,0.05)"),
                yaxis2=dict(overlaying="y", side="right", showgrid=False, showticklabels=False),
                xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Geen data")

    with ch2:
        st.markdown("#### PnL Verdeling")
        hist_df = get_pnl_histogram(source=src)
        if not hist_df.empty:
            fig = go.Figure()
            fig.add_trace(go.Histogram(
                x=hist_df["pnl_eur"],
                nbinsx=40,
                marker_color=["#10b981" if v >= 0 else "#ef4444"
                              for v in hist_df["pnl_eur"]],
            ))
            fig.update_layout(
                template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)", height=300,
                margin=dict(l=0, r=0, t=10, b=0), showlegend=False,
                xaxis=dict(title="PnL \u20ac", gridcolor="rgba(255,255,255,0.05)"),
                yaxis=dict(title="Trades", gridcolor="rgba(255,255,255,0.05)"),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Geen data")

    # Win/Loss donut
    dist = get_outcome_distribution(source=src)
    if not dist.empty:
        wins = int(dist[dist["outcome"] == "WIN"]["n"].sum()) if "WIN" in dist["outcome"].values else 0
        losses = int(dist[dist["outcome"] == "LOSS"]["n"].sum()) if "LOSS" in dist["outcome"].values else 0
        total = wins + losses
        wr = round(wins / total * 100, 1) if total > 0 else 0
        lr = round(losses / total * 100, 1) if total > 0 else 0

        d1, d2, d3 = st.columns([1, 1, 2])
        with d1:
            st.metric("Win Rate", f"{wr}%", f"{wins} trades")
        with d2:
            st.metric("Loss Rate", f"{lr}%", f"{losses} trades", delta_color="inverse")
        with d3:
            avg_win = float(summary[summary["source"] == src]["avg_win"].iloc[0]) if src and src in summary["source"].values else float(summary["avg_win"].mean())
            avg_loss = float(summary[summary["source"] == src]["avg_loss"].iloc[0]) if src and src in summary["source"].values else float(summary["avg_loss"].mean())
            st.metric("Avg Win", f"\u20ac{avg_win:+.4f}")
            st.metric("Avg Loss", f"\u20ac{avg_loss:.4f}")

    st.markdown("---")

    # Tabs for breakdown
    tab1, tab2, tab3 = st.tabs(["Per Setup", "Per Regime", "Per Coin"])

    with tab1:
        df = get_win_rate_by("setup_type", src)
        if df.empty:
            st.info("Niet genoeg data (min 3 trades per setup)")
        else:
            # Add loss rate
            df["loss_rate"] = (100 - df["win_rate"]).round(1)
            renamed = df.rename(columns={
                "groep": "Setup", "total": "Trades", "wins": "W", "losses": "L",
                "win_rate": "Win%", "loss_rate": "Loss%", "total_pnl": "PnL \u20ac", "avg_r": "Avg R"
            })
            st.dataframe(renamed, use_container_width=True, hide_index=True)

            # Bar chart
            fig = go.Figure()
            fig.add_trace(go.Bar(name="Win%", x=df["groep"], y=df["win_rate"],
                                 marker_color="#10b981"))
            fig.add_trace(go.Bar(name="Loss%", x=df["groep"], y=100 - df["win_rate"],
                                 marker_color="#ef4444"))
            fig.update_layout(
                barmode="stack", template="plotly_dark",
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                height=250, margin=dict(l=0, r=0, t=10, b=0),
                showlegend=True, legend=dict(orientation="h", y=1.1),
                yaxis=dict(title="%", gridcolor="rgba(255,255,255,0.05)"),
            )
            st.plotly_chart(fig, use_container_width=True)

    with tab2:
        df = get_win_rate_by("market_regime", src)
        if df.empty:
            st.info("Niet genoeg data")
        else:
            df["loss_rate"] = (100 - df["win_rate"]).round(1)
            st.dataframe(df.rename(columns={
                "groep": "Regime", "total": "Trades", "wins": "W", "losses": "L",
                "win_rate": "Win%", "loss_rate": "Loss%", "total_pnl": "PnL \u20ac", "avg_r": "Avg R"
            }), use_container_width=True, hide_index=True)

            # Regime bar chart
            fig = go.Figure()
            regime_colors = {"BULL": "#10b981", "BEAR": "#ef4444", "RANGE": "#f59e0b"}
            for _, row in df.iterrows():
                fig.add_trace(go.Bar(
                    name=row["groep"], x=[row["groep"]], y=[row["win_rate"]],
                    marker_color=regime_colors.get(str(row["groep"]).upper(), "#3b82f6"),
                    text=f'{row["win_rate"]}%', textposition="auto",
                ))
            fig.update_layout(
                template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)", height=250,
                margin=dict(l=0, r=0, t=10, b=0), showlegend=False,
                yaxis=dict(title="Win %", range=[0, 100], gridcolor="rgba(255,255,255,0.05)"),
            )
            st.plotly_chart(fig, use_container_width=True)

    with tab3:
        df = get_win_rate_by("symbol", src)
        if df.empty:
            st.info("Niet genoeg data")
        else:
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Top coins**")
                top = df.head(10)
                top["loss_rate"] = (100 - top["win_rate"]).round(1)
                st.dataframe(top.rename(columns={
                    "groep": "Coin", "win_rate": "Win%", "loss_rate": "Loss%",
                    "total": "Trades", "total_pnl": "PnL \u20ac"
                })[["Coin", "Win%", "Loss%", "Trades", "PnL \u20ac"]], hide_index=True)
            with c2:
                st.markdown("**Slechtste coins**")
                bot = df.tail(10)
                bot["loss_rate"] = (100 - bot["win_rate"]).round(1)
                st.dataframe(bot.rename(columns={
                    "groep": "Coin", "win_rate": "Win%", "loss_rate": "Loss%",
                    "total": "Trades", "total_pnl": "PnL \u20ac"
                })[["Coin", "Win%", "Loss%", "Trades", "PnL \u20ac"]], hide_index=True)
