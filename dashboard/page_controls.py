import streamlit as st
from dashboard.db import get_bot_state, get_bot_val, set_bot_val

def render():
    st.header("Control Board")
    st.caption("Wijzig bot instellingen. Wijzigingen worden direct opgeslagen in de database.")

    state = get_bot_state()

    # Section 1: Bot On/Off
    st.subheader("Bot Status")
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        bot_active = state.get("bot_active", "false") == "true"
        new_active = st.toggle("Bot Actief", value=bot_active, key="ctrl_bot_active")
        if new_active != bot_active:
            set_bot_val("bot_active", "true" if new_active else "false")
            st.success("bot_active opgeslagen")
            st.rerun()

    with col2:
        bot_running = state.get("bot_running", "false") == "true"
        new_running = st.toggle("Bot Running", value=bot_running, key="ctrl_bot_running")
        if new_running != bot_running:
            set_bot_val("bot_running", "true" if new_running else "false")
            st.success("bot_running opgeslagen")
            st.rerun()

    with col3:
        bot_paused = state.get("bot_paused", "false") == "true"
        new_paused = st.toggle("Bot Gepauzeerd", value=bot_paused, key="ctrl_bot_paused")
        if new_paused != bot_paused:
            set_bot_val("bot_paused", "true" if new_paused else "false")
            st.success("bot_paused opgeslagen")
            st.rerun()

    with col4:
        shadow_active = state.get("shadow_trading_actief", "false") == "true"
        new_shadow = st.toggle("Shadow Trading", value=shadow_active, key="ctrl_shadow")
        if new_shadow != shadow_active:
            set_bot_val("shadow_trading_actief", "true" if new_shadow else "false")
            st.success("shadow_trading_actief opgeslagen")
            st.rerun()

    st.divider()

    # Section 2: Trading Parameters (editable)
    st.subheader("Trading Parameters")

    editable_params = [
        ("max_per_trade_eur", "Max EUR per trade", "float", "15.0"),
        ("position_size_eur", "Positie grootte EUR", "float", "15.0"),
        ("max_open_trades", "Max open trades", "int", "10"),
        ("max_trades_per_dag", "Max trades per dag", "int", "10"),
        ("daily_stop_loss_eur", "Daily stop loss EUR", "float", "5.0"),
        ("coin_cooldown_hours", "Coin cooldown (uren)", "int", "24"),
        ("score_drempel_bull", "Score drempel BULL", "int", "90"),
        ("score_drempel_range", "Score drempel RANGE", "int", "90"),
        ("score_drempel_bear", "Score drempel BEAR", "int", "90"),
        ("atr_multiplier", "ATR multiplier", "float", "1.4"),
        ("trading_hours_start", "Trading start (UTC)", "int", "0"),
        ("trading_hours_end", "Trading eind (UTC)", "int", "24"),
    ]

    # Display in 3 columns
    cols = st.columns(3)
    for i, (key, label, dtype, default) in enumerate(editable_params):
        current = state.get(key, default)
        with cols[i % 3]:
            if dtype == "float":
                new_val = st.number_input(label, value=float(current or default), step=0.5, key=f"ctrl_{key}", format="%.2f")
                if str(new_val) != str(current):
                    if st.button(f"Opslaan", key=f"save_{key}"):
                        set_bot_val(key, str(new_val))
                        st.success(f"{key} = {new_val}")
                        st.rerun()
            elif dtype == "int":
                new_val = st.number_input(label, value=int(float(current or default)), step=1, key=f"ctrl_{key}")
                if str(new_val) != str(int(float(current or default))):
                    if st.button(f"Opslaan", key=f"save_{key}"):
                        set_bot_val(key, str(new_val))
                        st.success(f"{key} = {new_val}")
                        st.rerun()

    st.divider()

    # Section 3: Read-only status values
    st.subheader("Status (alleen-lezen)")

    readonly_keys = [
        ("btc_regime_huidig", "BTC Regime"),
        ("drawdown_modus", "Drawdown Modus"),
        ("monitor_status", "Monitor Status"),
        ("monitor_runs_totaal", "Monitor Runs"),
        ("monitor_live_open", "Monitor Live Open"),
        ("monitor_shadow_open", "Monitor Shadow Open"),
        ("coach_bitvavo_portfolio_eur", "Portfolio EUR"),
        ("live_trader_last_action", "Laatste Live Actie"),
        ("scanner_versie", "Scanner Versie"),
        ("fetcher_kwaliteit_score", "Fetcher Kwaliteit"),
    ]

    cols = st.columns(3)
    for i, (key, label) in enumerate(readonly_keys):
        val = state.get(key, "—")
        with cols[i % 3]:
            val_display = str(val)[:50]
            st.text_input(label, value=val_display, disabled=True, key=f"ro_{key}")

    st.divider()

    # Section 4: Raw bot_state browser
    with st.expander("Alle bot_state waarden (gevorderd)"):
        search = st.text_input("Zoek key", key="state_search")
        filtered = {k: v for k, v in state.items() if search.lower() in k.lower()} if search else state
        for key in sorted(filtered.keys()):
            val = str(filtered[key])[:100]
            st.text(f"{key}: {val}")
