"""Bot Brain — Geheugen, beslissingen en leerproces zichtbaar."""
import streamlit as st
import pandas as pd
import json
from datetime import datetime, timezone
from dashboard.db import run_query, get_bot_state
from dashboard.styles import card, badge

@st.cache_data(ttl=30)
def get_brain_memories():
    return run_query("""
        SELECT type, sleutel, waarde, score, bijgewerkt::text
        FROM coach_memory
        ORDER BY bijgewerkt DESC NULLS LAST
    """)

@st.cache_data(ttl=30)
def get_brain_logs():
    return run_query("""
        SELECT sleutel, waarde, bijgewerkt::text
        FROM coach_memory
        WHERE type = 'brain_log'
        ORDER BY bijgewerkt DESC LIMIT 10
    """)

@st.cache_data(ttl=30)
def get_coin_scores():
    return run_query("""
        SELECT sleutel as coin, score as win_rate, waarde, bijgewerkt::text
        FROM coach_memory
        WHERE type = 'coin_score'
        ORDER BY score DESC
    """)

def render():
    st.markdown("### Bot Brain — Trading Geheugen")
    st.caption("Wat het brein onthoudt, wat het heeft veranderd, en wat het adviseert.")

    state = get_bot_state()

    # Brain status
    c1, c2, c3 = st.columns(3)
    with c1:
        bl = json.loads(state.get("coin_blacklist", "[]"))
        st.markdown(card("Blacklist", str(len(bl)), f"{', '.join(bl[:5])}{'...' if len(bl)>5 else ''}", color="red"), unsafe_allow_html=True)
    with c2:
        wl = json.loads(state.get("coin_whitelist", "[]"))
        st.markdown(card("Whitelist", str(len(wl)), f"{', '.join(wl[:5])}{'...' if len(wl)>5 else ''}", color="green"), unsafe_allow_html=True)
    with c3:
        logs = get_brain_logs()
        last_run = str(logs.iloc[0]["bijgewerkt"])[:16] if not logs.empty else "nooit"
        st.markdown(card("Laatste Brain Run", last_run), unsafe_allow_html=True)

    st.markdown("---")

    # Tabs
    tab1, tab2, tab3, tab4 = st.tabs(["Coin Rankings", "Beslissingen", "Claude Advies", "Geheugen"])

    with tab1:
        st.markdown("#### Coin Rankings (op basis van win rate)")
        scores = get_coin_scores()
        if not scores.empty:
            # Parse de JSON waarde
            parsed = []
            for _, row in scores.iterrows():
                try:
                    data = json.loads(row["waarde"])
                    parsed.append({
                        "Coin": row["coin"],
                        "Win%": float(row["win_rate"]),
                        "Trades": data.get("trades", 0),
                        "Wins": data.get("wins", 0),
                        "Losses": data.get("losses", 0),
                        "PnL": float(data.get("pnl", 0)),
                        "Avg R": float(data.get("avg_r", 0)),
                        "Status": "WHITELIST" if row["coin"] in wl else ("BLACKLIST" if row["coin"] in bl else ""),
                    })
                except Exception:
                    pass

            if parsed:
                df = pd.DataFrame(parsed)

                # Best vs worst
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown("**Top coins (brein adviseert: KOPEN)**")
                    top = df.head(10)
                    for _, r in top.iterrows():
                        color = "green" if r["Win%"] >= 60 else "blue"
                        wr_text = str(r["Win%"]) + "%"
                        status = f" {badge(r['Status'], 'green')}" if r["Status"] == "WHITELIST" else ""
                        pnl = float(r["PnL"])
                        st.markdown(f"{badge(wr_text, color)} **{r['Coin']}** - {r['Trades']} trades, {pnl:+.2f}{status}", unsafe_allow_html=True)

                with col2:
                    st.markdown("**Slechtste coins (brein adviseert: VERMIJDEN)**")
                    bottom = df.tail(10).iloc[::-1]
                    for _, r in bottom.iterrows():
                        color = "red" if r["Win%"] < 30 else "yellow"
                        wr_text = str(r["Win%"]) + "%"
                        status = f" {badge(r['Status'], 'red')}" if r["Status"] == "BLACKLIST" else ""
                        pnl = float(r["PnL"])
                        st.markdown(f"{badge(wr_text, color)} **{r['Coin']}** - {r['Trades']} trades, {pnl:+.2f}{status}", unsafe_allow_html=True)

                st.markdown("---")
                st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("Nog geen coin rankings — brain moet eerst draaien")

    with tab2:
        st.markdown("#### Beslissingen & Aanpassingen")
        logs = get_brain_logs()
        if not logs.empty:
            for _, row in logs.iterrows():
                try:
                    data = json.loads(row["waarde"])
                    datum = data.get("datum", row["bijgewerkt"])[:16]
                    changes = data.get("aanpassingen", [])
                    insights = data.get("inzichten", [])

                    with st.expander(f"{datum} — {len(changes)} aanpassingen, {len(insights)} inzichten", expanded=True):
                        if changes:
                            st.markdown("**Aanpassingen:**")
                            for c in changes:
                                st.markdown(f"- {c}")
                        else:
                            st.caption("Geen aanpassingen deze run")

                        if insights:
                            st.markdown("**Inzichten:**")
                            for ins in insights[:5]:
                                st.markdown(f"- {ins[:200]}")
                except Exception:
                    st.text(str(row["waarde"])[:200])
        else:
            st.info("Nog geen brain logs — brain moet eerst draaien")

    with tab3:
        st.markdown("#### Claude Advies")
        memories = get_brain_memories()
        if not memories.empty:
            advice = memories[memories["type"] == "claude_advice"].head(5)
            if not advice.empty:
                for _, row in advice.iterrows():
                    datum = str(row["bijgewerkt"])[:16]
                    st.markdown(f"**{datum}:**")
                    st.markdown(row["waarde"])
                    st.markdown("---")
            else:
                st.info("Nog geen Claude advies")
        else:
            st.info("Nog geen memories")

    with tab4:
        st.markdown("#### Volledig Geheugen")
        memories = get_brain_memories()
        if not memories.empty:
            # Group by type
            types = memories["type"].unique()
            for t in sorted(types):
                subset = memories[memories["type"] == t]
                with st.expander(f"{t} ({len(subset)} items)"):
                    for _, row in subset.iterrows():
                        val = str(row["waarde"])[:150]
                        st.code(f"{row['sleutel']}: {val}", language=None)
        else:
            st.info("Geheugen is leeg")
