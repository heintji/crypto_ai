"""Database layer for Crypto AI Dashboard — all queries, no JSON."""
import os
import streamlit as st
import psycopg2
import psycopg2.extras
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

DATABASE_URL = os.getenv("DATABASE_URL", "")


@st.cache_resource
def get_conn():
    """Persistent DB connection."""
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def run_query(sql: str, params=None) -> pd.DataFrame:
    """Run SQL, return DataFrame. Reconnects on error."""
    try:
        conn = get_conn()
        return pd.read_sql_query(sql, conn, params=params)
    except Exception:
        st.cache_resource.clear()
        try:
            conn = get_conn()
            return pd.read_sql_query(sql, conn, params=params)
        except Exception as e:
            st.error(f"DB fout: {e}")
            return pd.DataFrame()


def run_scalar(sql: str, params=None, default=None):
    """Run SQL, return single value."""
    df = run_query(sql, params)
    if df.empty:
        return default
    return df.iloc[0, 0]


# --- Trade queries ---

@st.cache_data(ttl=15)
def get_trades(source: str = None, status: str = None, limit: int = 500) -> pd.DataFrame:
    """Get trades from experience_trades. source: LIVE/SHADOW/SIM/STRATEGY or None for all."""
    conditions = []
    params = []
    if source:
        conditions.append("source = %s")
        params.append(source)
    if status:
        conditions.append("status = %s")
        params.append(status)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    sql = f"""
        SELECT trade_key, symbol, source, status, outcome, setup_type, market_regime,
               entry, exit_price, stop, stop_loss, target, qty, amount_eur,
               pnl_eur, pnl_pct, result_r, mfe_r, mae_r,
               max_price_seen, min_price_seen, score,
               entry_time, exit_time, exit_reden, bitvavo_market,
               entry_time AS created_at
        FROM experience_trades
        {where}
        ORDER BY entry_time DESC NULLS LAST
        LIMIT {limit}
    """
    return run_query(sql, tuple(params) if params else None)


@st.cache_data(ttl=10)
def get_open_trades(source: str = None) -> pd.DataFrame:
    """Get only OPEN trades."""
    if source:
        return run_query("""
            SELECT symbol, source, entry, stop, stop_loss, target, amount_eur, qty,
                   max_price_seen, min_price_seen, mfe_r, mae_r, score, setup_type,
                   market_regime, entry_time, monitor_updated_at, trade_key, bitvavo_market
            FROM experience_trades WHERE status='OPEN' AND source=%s
            ORDER BY entry_time DESC
        """, (source,))
    return run_query("""
        SELECT symbol, source, entry, stop, stop_loss, target, amount_eur, qty,
               max_price_seen, min_price_seen, mfe_r, mae_r, score, setup_type,
               market_regime, entry_time, monitor_updated_at, trade_key, bitvavo_market
        FROM experience_trades WHERE status='OPEN'
        ORDER BY source, entry_time DESC
    """)


# --- Win rate queries ---

@st.cache_data(ttl=30)
def get_win_rate_summary() -> pd.DataFrame:
    """Win rate per source."""
    return run_query("""
        SELECT source,
               COUNT(*) as total,
               COUNT(*) FILTER(WHERE outcome='WIN') as wins,
               COUNT(*) FILTER(WHERE outcome='LOSS') as losses,
               ROUND(AVG(pnl_eur)::numeric, 4) as avg_pnl,
               ROUND(SUM(pnl_eur)::numeric, 2) as total_pnl,
               ROUND(AVG(result_r)::numeric, 3) as avg_r,
               ROUND(AVG(CASE WHEN outcome='WIN' THEN pnl_eur END)::numeric, 4) as avg_win,
               ROUND(AVG(CASE WHEN outcome='LOSS' THEN pnl_eur END)::numeric, 4) as avg_loss
        FROM experience_trades
        WHERE status='CLOSED' AND outcome IN ('WIN','LOSS')
        GROUP BY source ORDER BY source
    """)


@st.cache_data(ttl=30)
def get_win_rate_by(group_col: str, source: str = None) -> pd.DataFrame:
    """Win rate grouped by setup_type, market_regime, or symbol."""
    source_filter = "AND source = %s" if source else ""
    params = (source,) if source else None
    valid_cols = {"setup_type", "market_regime", "symbol"}
    if group_col not in valid_cols:
        return pd.DataFrame()
    return run_query(f"""
        SELECT {group_col} as groep,
               COUNT(*) as total,
               COUNT(*) FILTER(WHERE outcome='WIN') as wins,
               COUNT(*) FILTER(WHERE outcome='LOSS') as losses,
               ROUND(100.0 * COUNT(*) FILTER(WHERE outcome='WIN') / NULLIF(COUNT(*), 0), 1) as win_rate,
               ROUND(SUM(pnl_eur)::numeric, 2) as total_pnl,
               ROUND(AVG(result_r)::numeric, 3) as avg_r
        FROM experience_trades
        WHERE status='CLOSED' AND outcome IN ('WIN','LOSS') {source_filter}
        GROUP BY {group_col}
        HAVING COUNT(*) >= 3
        ORDER BY win_rate DESC
    """, params)


# --- Chart data ---

@st.cache_data(ttl=60)
def get_pnl_per_day(source: str = None, days: int = 30) -> pd.DataFrame:
    """PnL per dag voor equity curve."""
    src = f"AND source = '{source}'" if source else ""
    return run_query(f"""
        SELECT exit_time::date as dag,
               COUNT(*) as trades,
               COUNT(*) FILTER(WHERE outcome='WIN') as wins,
               COUNT(*) FILTER(WHERE outcome='LOSS') as losses,
               COALESCE(SUM(pnl_eur), 0) as pnl_dag,
               SUM(SUM(pnl_eur)) OVER (ORDER BY exit_time::date) as cum_pnl
        FROM experience_trades
        WHERE status='CLOSED' AND outcome IN ('WIN','LOSS')
        AND exit_time >= CURRENT_DATE - INTERVAL '{days} days'
        {src}
        GROUP BY exit_time::date
        ORDER BY dag
    """)

@st.cache_data(ttl=60)
def get_outcome_distribution(source: str = None) -> pd.DataFrame:
    """Win/loss verdeling voor pie chart."""
    src = f"AND source = '{source}'" if source else ""
    return run_query(f"""
        SELECT outcome, COUNT(*) as n
        FROM experience_trades
        WHERE status='CLOSED' AND outcome IN ('WIN','LOSS') {src}
        GROUP BY outcome
    """)

@st.cache_data(ttl=60)
def get_pnl_histogram(source: str = None) -> pd.DataFrame:
    """PnL verdeling per trade."""
    src = f"AND source = '{source}'" if source else ""
    return run_query(f"""
        SELECT pnl_eur
        FROM experience_trades
        WHERE status='CLOSED' AND outcome IN ('WIN','LOSS')
        AND pnl_eur IS NOT NULL {src}
        ORDER BY pnl_eur
    """)


# --- Bot state ---

@st.cache_data(ttl=10)
def get_bot_state() -> Dict[str, str]:
    """Get all bot_state as dict."""
    df = run_query("SELECT key, value FROM bot_state ORDER BY key")
    if df.empty:
        return {}
    return dict(zip(df["key"], df["value"]))


def get_bot_val(key: str, default: str = "") -> str:
    """Get single bot_state value."""
    state = get_bot_state()
    return state.get(key, default)


def set_bot_val(key: str, value: str) -> bool:
    """Set bot_state value. Returns True on success."""
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO bot_state (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=%s",
            (key, value, value))
        conn.commit()
        st.cache_data.clear()  # invalidate cached state
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        st.error(f"Opslaan mislukt: {e}")
        return False


# --- Signals ---

@st.cache_data(ttl=15)
def get_pending_signals(limit: int = 100) -> pd.DataFrame:
    return run_query(f"""
        SELECT id, symbol, score, status, entry, stop, target, kelly_grootte_eur,
               live_toegestaan, created_at, expires_at, buy_attempts
        FROM pending_approvals
        ORDER BY created_at DESC LIMIT {limit}
    """)


@st.cache_data(ttl=15)
def get_signal_stats() -> pd.DataFrame:
    return run_query("SELECT status, COUNT(*) as n FROM pending_approvals GROUP BY status ORDER BY n DESC")


# --- Health ---

@st.cache_data(ttl=10)
def get_bot_health() -> pd.DataFrame:
    return run_query("""
        SELECT service, status, heartbeat_sec, laatste_run, details, fout
        FROM bot_health ORDER BY service
    """)


# --- Summary stats ---

@st.cache_data(ttl=15)
def get_portfolio_value() -> float:
    val = get_bot_val("coach_bitvavo_portfolio_eur", "0")
    try:
        return float(val)
    except Exception:
        return 0.0


@st.cache_data(ttl=15)
def get_daily_pnl() -> Tuple[int, int, float]:
    df = run_query("""
        SELECT COUNT(*) FILTER(WHERE outcome='WIN') as wins,
               COUNT(*) FILTER(WHERE outcome='LOSS') as losses,
               COALESCE(SUM(pnl_eur), 0) as pnl
        FROM experience_trades
        WHERE status='CLOSED' AND source='LIVE'
        AND exit_time::date = CURRENT_DATE
    """)
    if df.empty:
        return 0, 0, 0.0
    return int(df.iloc[0]["wins"]), int(df.iloc[0]["losses"]), float(df.iloc[0]["pnl"])
