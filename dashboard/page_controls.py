"""Control Board — view and edit bot settings."""
import streamlit as st
from dashboard.db import get_bot_state, set_bot_val
from dashboard.styles import card, badge

def render():
    st.markdown("### Control Board")
    st.caption("Wijzigingen worden direct opgeslagen in de database.")

    state = get_bot_state()

    # Bot toggles
    st.markdown("#### Bot Status")
    c1, c2, c3, c4 = st.columns(4)

    toggles = [
        (c1, "bot_active", "Bot Actief"),
        (c2, "bot_running", "Bot Running"),
        (c3, "bot_paused", "Gepauzeerd"),
        (c4, "shadow_trading_actief", "Shadow Trading"),
    ]
    for col, key, label in toggles:
        with col:
            current = state.get(key, "false") == "true"
            new_val = st.toggle(label, value=current, key=f"t_{key}")
            if new_val != current:
                set_bot_val(key, "true" if new_val else "false")
                st.rerun()

    st.markdown("---")

    # Editable parameters
    st.markdown("#### Trading Parameters")
    params = [
        ("max_per_trade_eur", "Max EUR/trade", "float", "15.0"),
        ("position_size_eur", "Positie EUR", "float", "15.0"),
        ("max_open_trades", "Max open trades", "int", "10"),
        ("max_trades_per_dag", "Max trades/dag", "int", "10"),
        ("daily_stop_loss_eur", "Daily stop loss €", "float", "5.0"),
        ("coin_cooldown_hours", "Cooldown (uren)", "int", "24"),
        ("score_drempel_bull", "Score BULL", "int", "90"),
        ("score_drempel_range", "Score RANGE", "int", "90"),
        ("score_drempel_bear", "Score BEAR", "int", "90"),
        ("atr_multiplier", "ATR multiplier", "float", "1.4"),
        ("trading_hours_start", "Start uur (UTC)", "int", "0"),
        ("trading_hours_end", "Eind uur (UTC)", "int", "24"),
    ]

    cols = st.columns(3)
    for i, (key, label, dtype, default) in enumerate(params):
        current = state.get(key, default)
        with cols[i % 3]:
            try:
                if dtype == "float":
                    val = st.number_input(label, value=float(current or default),
                                          step=0.5, format="%.2f", key=f"p_{key}")
                    changed = abs(val - float(current or default)) > 0.001
                else:
                    val = st.number_input(label, value=int(float(current or default)),
                                          step=1, key=f"p_{key}")
                    changed = val != int(float(current or default))
                if changed:
                    if st.button("Opslaan", key=f"s_{key}", type="primary"):
                        set_bot_val(key, str(val))
                        st.success(f"{key} = {val}")
                        st.rerun()
            except (ValueError, TypeError):
                st.text_input(label, value=str(current)[:30], disabled=True, key=f"p_{key}")

    st.markdown("---")

    # Read-only status
    st.markdown("#### Status (alleen-lezen)")
    ro = [
        ("btc_regime_huidig", "BTC Regime"),
        ("drawdown_modus", "Drawdown"),
        ("monitor_status", "Monitor"),
        ("monitor_runs_totaal", "Monitor Runs"),
        ("monitor_live_open", "Live Open"),
        ("monitor_shadow_open", "Shadow Open"),
        ("coach_bitvavo_portfolio_eur", "Portfolio €"),
        ("live_trader_last_action", "Laatste Actie"),
        ("scanner_versie", "Scanner"),
        ("fetcher_kwaliteit_score", "Fetcher Score"),
    ]
    cols = st.columns(3)
    for i, (key, label) in enumerate(ro):
        with cols[i % 3]:
            v = str(state.get(key, "—"))[:40]
            st.text_input(label, value=v, disabled=True, key=f"r_{key}")

    # Raw browser
    with st.expander("Alle bot_state waarden"):
        q = st.text_input("Zoek", key="bs_search")
        items = {k:v for k,v in sorted(state.items()) if q.lower() in k.lower()} if q else dict(sorted(state.items()))
        for k, v in items.items():
            st.code(f"{k}: {str(v)[:120]}", language=None)
