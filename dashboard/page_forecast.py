"""Forecast pagina — Monte Carlo voorspelling + regime analyse"""
import streamlit as st
import json
import psycopg2
import os


def get_forecast_data():
    """Haal Monte Carlo forecast data uit bot_state."""
    try:
        conn = psycopg2.connect(os.environ.get("DATABASE_URL", ""), sslmode="require", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT value FROM bot_state WHERE key='monte_carlo_forecast'")
        r = cur.fetchone()
        conn.close()
        if r and r[0]:
            return json.loads(r[0])
    except Exception:
        pass
    return None


def render():
    st.markdown("## Forecast — Toekomstvoorspelling")

    data = get_forecast_data()
    if not data:
        st.warning("Geen forecast data. Draai eerst: `python research/monte_carlo_forecast.py`")
        return

    fc = data.get("forecast", {})
    regime = data.get("regime", {})
    exits = data.get("exit_analyse", {})

    # Header metrics
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Kans op winst", f"{fc.get('kans_winst', 0)}%")
    with col2:
        st.metric("Verwachte PnL", f"EUR {fc.get('verwacht_pnl', 0):+.2f}",
                   delta=f"over {fc.get('forecast_trades', 100)} trades")
    with col3:
        st.metric("Per maand", f"EUR {fc.get('verwacht_pnl_per_maand', 0):+.2f}")
    with col4:
        st.metric("Max drawdown (95%)", f"EUR {fc.get('dd_p95', 0):.2f}")

    st.markdown("---")

    # PnL verdeling
    col1, col2 = st.columns(2)

    with col1:
        st.markdown("### PnL Voorspelling")
        st.markdown(f"""
        | Scenario | PnL |
        |----------|-----|
        | Best case (5%) | EUR {fc.get('pnl_p95', 0):+.2f} |
        | Optimistisch (25%) | EUR {fc.get('pnl_p75', 0):+.2f} |
        | **Mediaan (50%)** | **EUR {fc.get('pnl_mediaan', 0):+.2f}** |
        | Pessimistisch (75%) | EUR {fc.get('pnl_p25', 0):+.2f} |
        | Worst case (95%) | EUR {fc.get('pnl_p5', 0):+.2f} |
        | Absoluut minimum | EUR {fc.get('pnl_min', 0):+.2f} |
        | Absoluut maximum | EUR {fc.get('pnl_max', 0):+.2f} |
        """)

    with col2:
        st.markdown("### Risico Analyse")
        st.markdown(f"""
        | Metric | Waarde |
        |--------|--------|
        | Kans op winst | **{fc.get('kans_winst', 0)}%** |
        | Kans verlies > EUR 20 | {fc.get('kans_verlies_20', 0)}% |
        | Gem. max drawdown | EUR {fc.get('verwacht_max_dd', 0):.2f} |
        | Max drawdown (95%) | EUR {fc.get('dd_p95', 0):.2f} |
        | Gem. verliesreeks | {fc.get('verwacht_max_loss_streak', 0):.0f} trades |
        | Max verliesreeks (95%) | {fc.get('loss_streak_p95', 0)} trades |
        | Gem. winstreeks | {fc.get('verwacht_max_win_streak', 0):.0f} trades |
        """)

    st.markdown("---")

    # Equity curves
    curves = fc.get("equity_curves_sample", [])
    if curves:
        st.markdown("### Monte Carlo Simulatie — 20 scenario's")
        import plotly.graph_objects as go
        fig = go.Figure()
        for i, curve in enumerate(curves):
            color = "rgba(0, 200, 100, 0.3)" if curve[-1] > 0 else "rgba(255, 80, 80, 0.3)"
            fig.add_trace(go.Scatter(
                y=curve, mode="lines", name=f"Sim {i+1}",
                line=dict(width=1, color=color),
                showlegend=False,
            ))
        # Gemiddelde lijn
        max_len = max(len(c) for c in curves)
        avg_curve = []
        for j in range(max_len):
            vals = [c[j] for c in curves if j < len(c)]
            avg_curve.append(sum(vals) / len(vals))
        fig.add_trace(go.Scatter(
            y=avg_curve, mode="lines", name="Gemiddelde",
            line=dict(width=3, color="white"),
        ))
        fig.update_layout(
            xaxis_title="Trade #",
            yaxis_title="PnL (EUR)",
            template="plotly_dark",
            height=400,
            margin=dict(l=50, r=20, t=20, b=50),
        )
        fig.add_hline(y=0, line_dash="dash", line_color="gray")
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")

    # Regime analyse
    if regime:
        st.markdown("### Performance per Markt Regime")
        cols = st.columns(len(regime))
        for i, (reg_name, reg_data) in enumerate(sorted(regime.items())):
            with cols[i]:
                wr = reg_data.get("wr", 0)
                color = "normal" if wr >= 55 else ("off" if wr >= 45 else "inverse")
                st.metric(reg_name, f"{wr}% WR",
                          delta=f"EUR {reg_data.get('pnl', 0):+.2f}",
                          delta_color=color)
                st.caption(f"{reg_data.get('trades', 0)} trades | ~EUR {reg_data.get('verwacht_per_30_trades', 0):+.2f}/maand")

    st.markdown("---")

    # Exit strategie analyse
    if exits:
        st.markdown("### Exit Strategie Effectiviteit")
        for reden, edata in sorted(exits.items(), key=lambda x: x[1].get("trades", 0), reverse=True):
            wr = edata.get("wr", 0)
            icon = "+" if wr >= 55 else ("-" if wr < 45 else "~")
            avg_r = edata.get("avg_r", 0)
            st.markdown(
                f"**{reden}** — {edata.get('trades', 0)} trades | "
                f"{wr}% WR | avg R = {avg_r:+.3f} | "
                f"PnL = EUR {edata.get('pnl', 0):+.2f}"
            )

    # Footer
    st.markdown("---")
    st.caption(
        f"Gebaseerd op {fc.get('historische_trades', 0)} trades | "
        f"{fc.get('simulaties', 0)} Monte Carlo simulaties | "
        f"Laatste run: {fc.get('run_date', 'onbekend')[:16]}"
    )
