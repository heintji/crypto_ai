"""Bot Brain — Knowledge base, decisions, and learning process."""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from dashboard.db import run_query
from dashboard.styles import card, badge


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

@st.cache_data(ttl=30)
def get_all_knowledge():
    return run_query("""
        SELECT id, topic, category, content, confidence, evidence,
               win_rate, avg_r, total_pnl,
               links, metadata,
               created_at::text AS created_at,
               updated_at::text AS updated_at
        FROM brain_knowledge
        ORDER BY updated_at DESC NULLS LAST
    """)


@st.cache_data(ttl=30)
def get_category_summary():
    return run_query("""
        SELECT category,
               COUNT(*) AS n,
               ROUND(AVG(confidence)::numeric, 1) AS avg_conf,
               ROUND(AVG(win_rate)::numeric, 1) AS avg_wr,
               SUM(evidence) AS total_evidence
        FROM brain_knowledge
        GROUP BY category ORDER BY n DESC
    """)


@st.cache_data(ttl=30)
def get_coin_rankings():
    return run_query("""
        SELECT topic, content, confidence, evidence,
               win_rate, avg_r, total_pnl,
               links, metadata,
               updated_at::text AS updated_at
        FROM brain_knowledge
        WHERE category = 'coin'
        ORDER BY win_rate DESC NULLS LAST
    """)


@st.cache_data(ttl=30)
def get_decisions():
    return run_query("""
        SELECT topic, content, confidence, evidence,
               win_rate, metadata,
               created_at::text AS created_at,
               updated_at::text AS updated_at
        FROM brain_knowledge
        WHERE category = 'decision'
        ORDER BY updated_at DESC NULLS LAST
    """)


@st.cache_data(ttl=30)
def get_advice():
    return run_query("""
        SELECT topic, content, confidence, metadata,
               created_at::text AS created_at,
               updated_at::text AS updated_at
        FROM brain_knowledge
        WHERE category = 'advice'
        ORDER BY updated_at DESC NULLS LAST
    """)


@st.cache_data(ttl=30)
def get_setup_verdicts():
    return run_query("""
        SELECT topic, content, confidence, evidence, win_rate, avg_r
        FROM brain_knowledge
        WHERE category = 'setup'
        ORDER BY win_rate DESC NULLS LAST
    """)


@st.cache_data(ttl=30)
def get_score_analysis():
    return run_query("""
        SELECT topic, content, confidence, evidence, win_rate, avg_r, total_pnl
        FROM brain_knowledge
        WHERE category = 'score'
        ORDER BY topic
    """)


@st.cache_data(ttl=30)
def get_exit_analysis():
    return run_query("""
        SELECT topic, content, confidence, evidence, win_rate, avg_r, total_pnl
        FROM brain_knowledge
        WHERE category = 'exit'
        ORDER BY win_rate DESC NULLS LAST
    """)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _verdict_badge(win_rate):
    """Return KOPEN/VERMIJDEN/NEUTRAAL badge based on win_rate."""
    if win_rate is None:
        return badge("NEUTRAAL", "yellow")
    if win_rate >= 55:
        return badge("KOPEN", "green")
    if win_rate < 40:
        return badge("VERMIJDEN", "red")
    return badge("NEUTRAAL", "yellow")


def _setup_badge(win_rate):
    """Return STERK/GEMIDDELD/ZWAK badge for setups."""
    if win_rate is None:
        return badge("GEMIDDELD", "yellow")
    if win_rate >= 60:
        return badge("STERK", "green")
    if win_rate < 40:
        return badge("ZWAK", "red")
    return badge("GEMIDDELD", "yellow")


def _confidence_bar(confidence):
    """Return a simple HTML progress bar for confidence 0-100."""
    val = min(max(float(confidence or 0), 0), 100)
    if val >= 70:
        color = "#10b981"
    elif val >= 40:
        color = "#f59e0b"
    else:
        color = "#ef4444"
    return (
        '<div style="background:#1a1f2e;border-radius:4px;height:14px;width:100%;'
        'border:1px solid #2d3748;overflow:hidden;">'
        '<div style="width:{pct}%;background:{clr};height:100%;border-radius:3px;">'
        '</div></div>'
    ).format(pct=val, clr=color)


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Plotly charts
# ---------------------------------------------------------------------------

def _chart_confidence_distribution(df):
    """Histogram of confidence values."""
    vals = df["confidence"].dropna().astype(float)
    if vals.empty:
        return None
    fig = go.Figure(go.Histogram(
        x=vals, nbinsx=20,
        marker_color="#3b82f6", marker_line_color="#1a1f2e", marker_line_width=1,
    ))
    fig.update_layout(
        title="Confidence Verdeling",
        xaxis_title="Confidence (%)",
        yaxis_title="Aantal",
        template="plotly_dark",
        paper_bgcolor="#0b0f19",
        plot_bgcolor="#111827",
        margin=dict(l=40, r=20, t=40, b=40),
        height=300,
    )
    return fig


def _chart_winrate_by_category(cat_df):
    """Bar chart of avg win_rate per category."""
    if cat_df.empty:
        return None
    fig = go.Figure(go.Bar(
        x=cat_df["category"],
        y=cat_df["avg_wr"],
        marker_color=["#10b981" if v and v >= 55 else "#f59e0b" if v and v >= 40 else "#ef4444"
                       for v in cat_df["avg_wr"]],
        text=[("{:.1f}%".format(v) if v else "n/a") for v in cat_df["avg_wr"]],
        textposition="auto",
    ))
    fig.update_layout(
        title="Gem. Win Rate per Categorie",
        yaxis_title="Win Rate (%)",
        template="plotly_dark",
        paper_bgcolor="#0b0f19",
        plot_bgcolor="#111827",
        margin=dict(l=40, r=20, t=40, b=40),
        height=300,
    )
    return fig


def _chart_coin_winrates(coins_df):
    """Horizontal bar chart of coin win rates."""
    if coins_df.empty:
        return None
    top = coins_df.head(20).copy()
    top = top.iloc[::-1]  # reverse for horizontal bar
    colors = []
    for wr in top["win_rate"]:
        w = _safe_float(wr)
        if w >= 55:
            colors.append("#10b981")
        elif w >= 40:
            colors.append("#f59e0b")
        else:
            colors.append("#ef4444")
    fig = go.Figure(go.Bar(
        y=top["topic"],
        x=top["win_rate"].astype(float),
        orientation="h",
        marker_color=colors,
        text=["{:.1f}%".format(_safe_float(v)) for v in top["win_rate"]],
        textposition="auto",
    ))
    fig.update_layout(
        title="Coin Win Rates (Top 20)",
        xaxis_title="Win Rate (%)",
        template="plotly_dark",
        paper_bgcolor="#0b0f19",
        plot_bgcolor="#111827",
        margin=dict(l=100, r=20, t=40, b=40),
        height=max(300, len(top) * 28),
    )
    return fig


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render():
    st.markdown("### Bot Brain -- Knowledge Base")
    st.caption("Wat het brein weet, welke coins het aanraadt, beslissingen en advies.")

    all_data = get_all_knowledge()
    cat_summary = get_category_summary()

    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "Overzicht", "Coin Rankings", "Beslissingen", "Claude Advies", "Volledige Kennis"
    ])

    # ===================================================================
    # TAB 1: Overview
    # ===================================================================
    with tab1:
        _render_overview(all_data, cat_summary)

    # ===================================================================
    # TAB 2: Coin Rankings
    # ===================================================================
    with tab2:
        _render_coin_rankings()

    # ===================================================================
    # TAB 3: Decisions
    # ===================================================================
    with tab3:
        _render_decisions()

    # ===================================================================
    # TAB 4: Claude Advice
    # ===================================================================
    with tab4:
        _render_advice()

    # ===================================================================
    # TAB 5: Full Knowledge
    # ===================================================================
    with tab5:
        _render_full_knowledge(all_data)


# ---------------------------------------------------------------------------
# Tab renderers
# ---------------------------------------------------------------------------

def _render_overview(all_data, cat_summary):
    # --- Summary cards ---
    total_topics = len(all_data) if not all_data.empty else 0
    coin_count = len(all_data[all_data["category"] == "coin"]) if not all_data.empty else 0
    last_run = "nooit"
    if not all_data.empty and "updated_at" in all_data.columns:
        last_run = str(all_data["updated_at"].iloc[0])[:16]

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown(card("Totaal Topics", str(total_topics), color="blue"), unsafe_allow_html=True)
    with c2:
        st.markdown(card("Coin Topics", str(coin_count), color="purple"), unsafe_allow_html=True)
    with c3:
        st.markdown(card("Laatste Update", last_run), unsafe_allow_html=True)

    st.markdown("---")

    # --- Top 5 KOPEN / VERMIJDEN ---
    coins = get_coin_rankings()
    if not coins.empty:
        coins_with_evidence = coins[coins["evidence"].apply(lambda x: _safe_int(x) >= 5)].copy()

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Top 5 KOPEN** (hoogste win rate, evidence >= 5)")
            top5 = coins_with_evidence.head(5)
            if top5.empty:
                st.info("Niet genoeg data (evidence >= 5)")
            else:
                for _, r in top5.iterrows():
                    wr = _safe_float(r["win_rate"])
                    ev = _safe_int(r["evidence"])
                    pnl = _safe_float(r["total_pnl"])
                    wr_text = "{:.1f}%".format(wr)
                    pnl_text = "{:+.2f}".format(pnl)
                    st.markdown(
                        badge(wr_text, "green")
                        + " **" + str(r["topic"]) + "**"
                        + " -- " + str(ev) + " trades, PnL: " + pnl_text,
                        unsafe_allow_html=True,
                    )

        with col2:
            st.markdown("**Top 5 VERMIJDEN** (laagste win rate, evidence >= 5)")
            bottom5 = coins_with_evidence.tail(5).iloc[::-1]
            if bottom5.empty:
                st.info("Niet genoeg data (evidence >= 5)")
            else:
                for _, r in bottom5.iterrows():
                    wr = _safe_float(r["win_rate"])
                    ev = _safe_int(r["evidence"])
                    pnl = _safe_float(r["total_pnl"])
                    wr_text = "{:.1f}%".format(wr)
                    pnl_text = "{:+.2f}".format(pnl)
                    st.markdown(
                        badge(wr_text, "red")
                        + " **" + str(r["topic"]) + "**"
                        + " -- " + str(ev) + " trades, PnL: " + pnl_text,
                        unsafe_allow_html=True,
                    )
    else:
        st.info("Geen coin data beschikbaar")

    st.markdown("---")

    # --- Setup verdicts ---
    col_a, col_b, col_c = st.columns(3)

    with col_a:
        st.markdown("**Setup Verdicts**")
        setups = get_setup_verdicts()
        if not setups.empty:
            for _, r in setups.iterrows():
                wr = _safe_float(r["win_rate"])
                bdg = _setup_badge(wr)
                ev = _safe_int(r["evidence"])
                st.markdown(
                    bdg + " **" + str(r["topic"]) + "** -- WR: "
                    + "{:.1f}%".format(wr) + ", " + str(ev) + " trades",
                    unsafe_allow_html=True,
                )
        else:
            st.info("Geen setup data")

    with col_b:
        st.markdown("**Score Ranges**")
        scores = get_score_analysis()
        if not scores.empty:
            for _, r in scores.iterrows():
                wr = _safe_float(r["win_rate"])
                avg_r = _safe_float(r["avg_r"])
                st.markdown(
                    _verdict_badge(wr) + " **" + str(r["topic"]) + "** -- WR: "
                    + "{:.1f}%".format(wr) + ", Avg R: " + "{:.2f}".format(avg_r),
                    unsafe_allow_html=True,
                )
        else:
            st.info("Geen score data")

    with col_c:
        st.markdown("**Exit Efficiency**")
        exits = get_exit_analysis()
        if not exits.empty:
            for _, r in exits.iterrows():
                wr = _safe_float(r["win_rate"])
                avg_r = _safe_float(r["avg_r"])
                pnl = _safe_float(r["total_pnl"])
                st.markdown(
                    _verdict_badge(wr) + " **" + str(r["topic"]) + "** -- WR: "
                    + "{:.1f}%".format(wr) + ", PnL: " + "{:+.2f}".format(pnl),
                    unsafe_allow_html=True,
                )
        else:
            st.info("Geen exit data")

    st.markdown("---")

    # --- Charts ---
    ch1, ch2 = st.columns(2)
    with ch1:
        if not all_data.empty:
            fig = _chart_confidence_distribution(all_data)
            if fig:
                st.plotly_chart(fig, use_container_width=True)
    with ch2:
        if not cat_summary.empty:
            fig = _chart_winrate_by_category(cat_summary)
            if fig:
                st.plotly_chart(fig, use_container_width=True)


def _render_coin_rankings():
    st.markdown("#### Coin Rankings")

    coins = get_coin_rankings()
    if coins.empty:
        st.info("Geen coin data -- brain moet eerst draaien.")
        return

    # Verdict filter
    filter_opt = st.selectbox(
        "Filter op verdict",
        ["Alle", "KOPEN", "VERMIJDEN", "NEUTRAAL"],
        key="brain_coin_filter",
    )

    rows = []
    for _, r in coins.iterrows():
        wr = _safe_float(r["win_rate"])
        if wr >= 55:
            verdict = "KOPEN"
        elif wr < 40:
            verdict = "VERMIJDEN"
        else:
            verdict = "NEUTRAAL"
        rows.append({
            "Topic": str(r["topic"]),
            "Verdict": verdict,
            "Win Rate %": round(wr, 1),
            "Confidence": round(_safe_float(r["confidence"]), 1),
            "Evidence": _safe_int(r["evidence"]),
            "Avg R": round(_safe_float(r["avg_r"]), 3),
            "Total PnL": round(_safe_float(r["total_pnl"]), 2),
            "Updated": str(r["updated_at"])[:16] if r["updated_at"] else "",
        })

    df = pd.DataFrame(rows)
    if df.empty:
        st.info("Geen data.")
        return

    if filter_opt != "Alle":
        df = df[df["Verdict"] == filter_opt]

    if df.empty:
        st.info("Geen coins voor dit filter.")
        return

    # Show count
    st.caption("{total} coins gevonden".format(total=len(df)))

    # Render badges above the table
    for _, r in df.iterrows():
        wr = r["Win Rate %"]
        topic = r["Topic"]
        conf = r["Confidence"]
        ev = r["Evidence"]
        avg_r = r["Avg R"]
        pnl = r["Total PnL"]
        verdict = r["Verdict"]

        if verdict == "KOPEN":
            bdg = badge(verdict, "green")
        elif verdict == "VERMIJDEN":
            bdg = badge(verdict, "red")
        else:
            bdg = badge(verdict, "yellow")

        conf_bar = _confidence_bar(conf)

        st.markdown(
            '<div style="display:flex;align-items:center;gap:12px;padding:6px 0;">'
            + bdg
            + '<strong style="min-width:100px;color:#f1f5f9;">' + topic + '</strong>'
            + '<span style="color:#94a3b8;font-size:0.85rem;">WR: '
            + "{:.1f}%".format(wr)
            + " | Ev: " + str(ev)
            + " | R: " + "{:.2f}".format(avg_r)
            + " | PnL: " + "{:+.2f}".format(pnl)
            + '</span>'
            + '<div style="flex:1;max-width:120px;">' + conf_bar + '</div>'
            + '</div>',
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # Full data table
    st.markdown("**Volledige tabel**")
    st.dataframe(df, use_container_width=True, hide_index=True)

    # Chart
    fig = _chart_coin_winrates(coins)
    if fig:
        st.plotly_chart(fig, use_container_width=True)


def _render_decisions():
    st.markdown("#### Beslissingen")
    st.caption("Wat het brein heeft veranderd en waarom.")

    decisions = get_decisions()
    if decisions.empty:
        st.info("Geen beslissingen gevonden.")
        return

    for _, r in decisions.iterrows():
        topic = str(r["topic"])
        content = str(r["content"]) if r["content"] else ""
        conf = _safe_float(r["confidence"])
        created = str(r["created_at"])[:16] if r["created_at"] else "?"
        updated = str(r["updated_at"])[:16] if r["updated_at"] else "?"
        wr = _safe_float(r["win_rate"])

        # Verification status
        if wr >= 55:
            status_text = badge("WERKT", "green")
        elif wr < 40:
            status_text = badge("FAALT", "red")
        elif r["win_rate"] is not None:
            status_text = badge("ONZEKER", "yellow")
        else:
            status_text = badge("NIET GEVERIFIEERD", "blue")

        meta_str = ""
        if r["metadata"] and isinstance(r["metadata"], dict):
            meta = r["metadata"]
            reason = meta.get("reason", meta.get("reden", ""))
            if reason:
                meta_str = "Reden: " + str(reason)

        header = topic + " (" + created + ")"
        with st.expander(header, expanded=False):
            st.markdown(status_text + " Confidence: **{:.0f}%**".format(conf), unsafe_allow_html=True)
            st.markdown(content)
            if meta_str:
                st.caption(meta_str)
            st.caption("Aangemaakt: " + created + " | Laatst bijgewerkt: " + updated)

    # Timeline view
    st.markdown("---")
    st.markdown("**Timeline**")
    timeline_rows = []
    for _, r in decisions.iterrows():
        created = str(r["created_at"])[:10] if r["created_at"] else "?"
        timeline_rows.append({
            "Datum": created,
            "Topic": str(r["topic"]),
            "Confidence": round(_safe_float(r["confidence"]), 1),
        })
    if timeline_rows:
        tl_df = pd.DataFrame(timeline_rows)
        st.dataframe(tl_df, use_container_width=True, hide_index=True)


def _render_advice():
    st.markdown("#### Claude Advies")
    st.caption("Adviezen van Claude, meest recent eerst.")

    advice = get_advice()
    if advice.empty:
        st.info("Nog geen advies ontvangen.")
        return

    for _, r in advice.iterrows():
        topic = str(r["topic"])
        content = str(r["content"]) if r["content"] else ""
        conf = _safe_float(r["confidence"])
        created = str(r["created_at"])[:16] if r["created_at"] else "?"
        updated = str(r["updated_at"])[:16] if r["updated_at"] else "?"

        header = topic + " (" + updated + ")"
        with st.expander(header, expanded=False):
            st.markdown(
                "Confidence: **{:.0f}%**".format(conf)
                + " " + _confidence_bar(conf),
                unsafe_allow_html=True,
            )
            st.markdown(content)
            st.caption("Aangemaakt: " + created)


def _render_full_knowledge(all_data):
    st.markdown("#### Volledige Knowledge Base")

    if all_data.empty:
        st.info("Knowledge base is leeg.")
        return

    # Search
    search = st.text_input(
        "Zoek in topics en content",
        key="brain_search",
        placeholder="bijv. BTC, momentum, stop-loss ...",
    )

    filtered = all_data.copy()
    if search:
        search_lower = search.lower()
        mask = (
            filtered["topic"].str.lower().str.contains(search_lower, na=False)
            | filtered["content"].fillna("").str.lower().str.contains(search_lower, na=False)
        )
        filtered = filtered[mask]

    if filtered.empty:
        st.warning("Geen resultaten gevonden.")
        return

    st.caption("{n} topics gevonden".format(n=len(filtered)))

    # Group by category
    categories = sorted(filtered["category"].dropna().unique())

    for cat in categories:
        subset = filtered[filtered["category"] == cat]
        with st.expander("{cat} ({n} items)".format(cat=cat.upper(), n=len(subset)), expanded=False):
            for _, r in subset.iterrows():
                topic = str(r["topic"])
                content = str(r["content"])[:300] if r["content"] else ""
                conf = _safe_float(r["confidence"])
                wr = r["win_rate"]
                ev = _safe_int(r["evidence"])
                updated = str(r["updated_at"])[:16] if r["updated_at"] else ""

                # Topic header with badges
                parts = ["**" + topic + "**"]
                if wr is not None:
                    parts.append(_verdict_badge(_safe_float(wr)))
                parts.append(
                    '<span style="color:#64748b;font-size:0.8rem;">'
                    + "Conf: {:.0f}%".format(conf)
                    + " | Ev: " + str(ev)
                    + " | " + updated
                    + '</span>'
                )
                st.markdown(" ".join(parts), unsafe_allow_html=True)

                # Content preview
                if content:
                    st.markdown(
                        '<div style="color:#94a3b8;font-size:0.85rem;padding:2px 0 4px 16px;">'
                        + content
                        + '</div>',
                        unsafe_allow_html=True,
                    )

                # Links
                links = r.get("links")
                if links and isinstance(links, list) and len(links) > 0:
                    link_badges = " ".join([badge(lnk, "blue") for lnk in links[:10]])
                    st.markdown(
                        "Links: " + link_badges,
                        unsafe_allow_html=True,
                    )

                st.markdown(
                    '<hr style="margin:4px 0;border-color:#2d3748;opacity:0.3;">',
                    unsafe_allow_html=True,
                )
