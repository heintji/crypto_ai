"""Analyse & Diagnostiek — Wat werkt, wat niet, waar komt verlies vandaan."""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from dashboard.db import run_query
from dashboard.styles import card, badge

@st.cache_data(ttl=60)
def get_full_analysis(source: str = None) -> pd.DataFrame:
    """Alle gesloten trades voor analyse."""
    src = f"AND source = '{source}'" if source else ""
    return run_query(f"""
        SELECT symbol, source, outcome, setup_type, market_regime, score,
               pnl_eur, result_r, mfe_r, mae_r, entry_time, exit_reden
        FROM experience_trades
        WHERE status='CLOSED' AND outcome IN ('WIN','LOSS') {src}
    """)

def calc_stats(df: pd.DataFrame) -> dict:
    """Bereken stats voor een groep trades."""
    if df.empty:
        return {"n": 0, "wins": 0, "losses": 0, "wr": 0, "lr": 0,
                "pnl": 0, "avg_r": 0, "avg_mfe": 0, "avg_mae": 0}
    n = len(df)
    wins = len(df[df["outcome"] == "WIN"])
    losses = n - wins
    return {
        "n": n, "wins": wins, "losses": losses,
        "wr": round(wins / n * 100, 1) if n > 0 else 0,
        "lr": round(losses / n * 100, 1) if n > 0 else 0,
        "pnl": round(df["pnl_eur"].sum(), 2) if "pnl_eur" in df else 0,
        "avg_r": round(df["result_r"].mean(), 3) if "result_r" in df and not df["result_r"].isna().all() else 0,
        "avg_mfe": round(df["mfe_r"].mean(), 2) if "mfe_r" in df and not df["mfe_r"].isna().all() else 0,
        "avg_mae": round(df["mae_r"].mean(), 2) if "mae_r" in df and not df["mae_r"].isna().all() else 0,
    }

def show_breakdown(df: pd.DataFrame, group_col: str, title: str, min_trades: int = 3):
    """Toon breakdown per groep met scorecard."""
    if df.empty or group_col not in df.columns:
        st.info("Geen data")
        return

    groups = df.groupby(group_col)
    rows = []
    for name, group in groups:
        if len(group) < min_trades or not name:
            continue
        s = calc_stats(group)
        s["groep"] = str(name)
        rows.append(s)

    if not rows:
        st.info(f"Niet genoeg data (min {min_trades} trades per groep)")
        return

    result = pd.DataFrame(rows).sort_values("wr", ascending=False)

    # Scorecard: best vs worst
    best = result.head(3)
    worst = result.tail(3)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("##### ✅ Werkt goed")
        for _, r in best.iterrows():
            color = "green" if r["wr"] >= 50 else "yellow"
            verdict = "HOUDEN" if r["wr"] >= 50 else "MONITOREN"
            st.markdown(
                f'{badge(verdict, color)} **{r["groep"]}** — '
                f'{r["wr"]}% win | {r["n"]} trades | '
                f'\u20ac{r["pnl"]:+.2f} | R:{r["avg_r"]:.2f}',
                unsafe_allow_html=True)
    with c2:
        st.markdown("##### \u274c Werkt niet")
        for _, r in worst.iterrows():
            color = "red" if r["wr"] < 40 else "yellow"
            verdict = "UITSCHAKELEN" if r["wr"] < 30 else "AANPASSEN"
            st.markdown(
                f'{badge(verdict, color)} **{r["groep"]}** — '
                f'{r["wr"]}% win | {r["n"]} trades | '
                f'\u20ac{r["pnl"]:+.2f} | R:{r["avg_r"]:.2f}',
                unsafe_allow_html=True)

    # Table
    display = result.rename(columns={
        "groep": title, "n": "Trades", "wins": "W", "losses": "L",
        "wr": "Win%", "lr": "Loss%", "pnl": "PnL \u20ac",
        "avg_r": "Avg R", "avg_mfe": "MFE", "avg_mae": "MAE"
    })
    st.dataframe(display, use_container_width=True, hide_index=True)

    # Bar chart
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=result["groep"], y=result["wr"],
        marker_color=["#10b981" if v >= 50 else "#ef4444" for v in result["wr"]],
        text=[f'{v}%' for v in result["wr"]], textposition="auto",
    ))
    fig.update_layout(
        template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)", height=250,
        margin=dict(l=0, r=0, t=10, b=0), showlegend=False,
        yaxis=dict(title="Win %", range=[0, 100], gridcolor="rgba(255,255,255,0.05)"),
    )
    st.plotly_chart(fig, use_container_width=True)


def render():
    st.markdown("### Analyse & Diagnostiek")
    st.caption("Wat werkt, wat niet, waar komt verlies vandaan?")

    # Source filter
    sources = ["Alle", "LIVE", "SHADOW", "REPLAY", "SIM"]
    sel = st.selectbox("Data bron", sources, key="analyse_src")
    src = None if sel == "Alle" else sel

    df = get_full_analysis(src)
    if df.empty:
        st.warning("Geen gesloten trades voor analyse")
        return

    # Overall stats
    s = calc_stats(df)
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    with c1: st.markdown(card("Win Rate", f'{s["wr"]}%', color="green" if s["wr"]>=50 else "red"), unsafe_allow_html=True)
    with c2: st.markdown(card("Loss Rate", f'{s["lr"]}%', color="red"), unsafe_allow_html=True)
    with c3: st.markdown(card("PnL", f'\u20ac{s["pnl"]:+.2f}', color="green" if s["pnl"]>=0 else "red"), unsafe_allow_html=True)
    with c4: st.markdown(card("Avg R", f'{s["avg_r"]:.2f}', color="green" if s["avg_r"]>=0 else "red"), unsafe_allow_html=True)
    with c5: st.markdown(card("Avg MFE", f'{s["avg_mfe"]:.2f}R'), unsafe_allow_html=True)
    with c6: st.markdown(card("Avg MAE", f'{s["avg_mae"]:.2f}R'), unsafe_allow_html=True)

    st.markdown("---")

    # Score bucket analyse
    st.markdown("#### Score Buckets — Welke scores werken?")
    df["score_bucket"] = pd.cut(
        df["score"].fillna(0).astype(float),
        bins=[0, 80, 85, 90, 95, 100],
        labels=["<80", "80-84", "85-89", "90-94", "95-100"],
        right=True
    )
    show_breakdown(df, "score_bucket", "Score Range", min_trades=3)

    st.markdown("---")

    # Tabs for deep analysis
    tab1, tab2, tab3, tab4 = st.tabs(["Per Setup", "Per Regime", "Per Coin", "Per Source"])

    with tab1:
        st.markdown("#### Setup Types — Welke strategieen werken?")
        show_breakdown(df, "setup_type", "Setup", min_trades=3)

    with tab2:
        st.markdown("#### BTC Regime — Wanneer werkt de bot?")
        show_breakdown(df, "market_regime", "Regime", min_trades=3)

    with tab3:
        st.markdown("#### Coins — Welke coins zijn winstgevend?")
        show_breakdown(df, "symbol", "Coin", min_trades=5)

    with tab4:
        st.markdown("#### Data Bronnen — Live vs Shadow vs Replay")
        show_breakdown(df, "source", "Source", min_trades=3)

    st.markdown("---")

    # Diagnose samenvatting
    st.markdown("#### Diagnose Samenvatting")

    # Auto-generate insights
    insights = []

    # Score analysis
    if "score_bucket" in df.columns:
        bucket_stats = df.groupby("score_bucket").apply(
            lambda x: len(x[x["outcome"]=="WIN"]) / len(x) * 100 if len(x) > 0 else 0
        ).to_dict()
        best_bucket = max(bucket_stats, key=bucket_stats.get) if bucket_stats else None
        worst_bucket = min(bucket_stats, key=bucket_stats.get) if bucket_stats else None
        if best_bucket:
            insights.append(f"Beste score range: **{best_bucket}** ({bucket_stats[best_bucket]:.1f}% WR)")
        if worst_bucket and worst_bucket != best_bucket:
            insights.append(f"Slechtste score range: **{worst_bucket}** ({bucket_stats[worst_bucket]:.1f}% WR)")

    # Setup analysis
    if "setup_type" in df.columns:
        setup_stats = df[df["setup_type"].notna()].groupby("setup_type").apply(
            lambda x: (len(x[x["outcome"]=="WIN"]) / len(x) * 100, len(x)) if len(x) >= 3 else (0, 0)
        ).to_dict()
        for setup, (wr, n) in setup_stats.items():
            if n >= 3 and wr < 35:
                insights.append(f"Setup **{setup}** heeft slechte WR ({wr:.1f}%) — overweeg aanpassing")
            elif n >= 3 and wr > 60:
                insights.append(f"Setup **{setup}** presteert goed ({wr:.1f}%) — houden!")

    # Overall
    if s["avg_r"] > 0 and s["wr"] < 40:
        insights.append("Win rate is laag maar Avg R is positief — je wins zijn groter dan je losses. Focus op meer wins.")
    if s["avg_mfe"] > 1.5 and s["avg_r"] < 0.5:
        insights.append(f"MFE ({s['avg_mfe']:.1f}R) is veel hoger dan Avg R ({s['avg_r']:.2f}) — trades bereiken hun doel maar worden te laat of verkeerd gesloten.")
    if s["avg_mae"] > 2:
        insights.append(f"MAE ({s['avg_mae']:.1f}R) is hoog — trades gaan ver onder water voordat ze sluiten. Overweeg strakkere stops.")

    if insights:
        for insight in insights:
            st.markdown(f"- {insight}")
    else:
        st.info("Niet genoeg data voor automatische diagnose")
