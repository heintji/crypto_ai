from __future__ import annotations

import os
import json
import time
import hmac
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
import psycopg2
import requests
import streamlit as st


# ==========================================================
# CONFIG
# ==========================================================
API_KEY = (os.getenv("BITVAVO_API_KEY", "") or "").strip().strip('"').strip("'")
API_SECRET = (os.getenv("BITVAVO_API_SECRET", "") or "").strip().strip('"').strip("'")
DATABASE_URL = (os.getenv("DATABASE_URL", "") or "").strip()

BASE_URL = "https://api.bitvavo.com"
ACCESS_WINDOW_MS = os.getenv("BITVAVO_ACCESS_WINDOW_MS", "10000")

SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", "/data/account_snapshot.json")
PORTFOLIO_HISTORY_CSV = os.getenv("PORTFOLIO_HISTORY_CSV", "/data/portfolio_history.csv")

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "12"))
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "5"))

# bewust UIT om lange laadtijd te voorkomen
AUTO_REFRESH_SNAPSHOT_ON_LOAD = False

# limieten voor snelle startup
PENDING_LIMIT = 20
REAL_LIMIT = 120
SHADOW_LIMIT = 120
SCOREBOARD_LIMIT = 20
HISTORY_LIMIT = 600


# ==========================================================
# BASIC HELPERS
# ==========================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(float(x))
    except Exception:
        return default


def safe_str(x: Any, default: str = "") -> str:
    if x is None:
        return default
    try:
        s = str(x).strip()
        return s if s else default
    except Exception:
        return default


def parse_dt(x: Any) -> pd.Timestamp:
    try:
        return pd.to_datetime(x, utc=True, errors="coerce")
    except Exception:
        return pd.NaT


def format_dt_short(x: Any) -> str:
    dt = parse_dt(x)
    if pd.isna(dt):
        return "-"
    return dt.strftime("%Y.%m.%d %H:%M:%S")


def format_money(x: Any) -> str:
    v = safe_float(x, 0.0)
    return f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_pnl(x: Any) -> str:
    v = safe_float(x, 0.0)
    sign = "+" if v > 0 else ""
    return f"{sign}€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def age_minutes(epoch_seconds: float) -> float:
    return (time.time() - float(epoch_seconds)) / 60.0


def file_meta(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"exists": False, "path": path}
    stat = os.stat(path)
    return {
        "exists": True,
        "path": path,
        "size_kb": round(stat.st_size / 1024, 2),
        "modified_epoch": stat.st_mtime,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }


def safe_read_json(path: str) -> Tuple[Optional[dict], Optional[str]]:
    try:
        if not os.path.exists(path):
            return None, f"Bestand niet gevonden: {path}"
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        return None, f"JSON leesfout ({path}): {e}"


def pnl_color(pnl: float, trade_type: str = "REAL") -> str:
    trade_type = safe_str(trade_type).upper()

    if trade_type == "SHADOW":
        if pnl > 0:
            return "#2ecc71"   # groen
        if pnl < 0:
            return "#ff8c42"   # oranje
        return "#b0b7c3"       # grijs

    if pnl > 0:
        return "#5aa2ff"       # blauw
    if pnl < 0:
        return "#ff5a5f"       # rood
    return "#b0b7c3"


def outcome_color(outcome: str, trade_type: str = "REAL") -> str:
    o = safe_str(outcome).upper()
    trade_type = safe_str(trade_type).upper()

    if trade_type == "SHADOW":
        if o == "WIN":
            return "#2ecc71"
        if o == "LOSS":
            return "#ff8c42"
        return "#b0b7c3"

    if o == "WIN":
        return "#5aa2ff"
    if o == "LOSS":
        return "#ff5a5f"
    return "#b0b7c3"


def render_perf_card(title: str, value: str, color: str):
    st.markdown(
        f"""
        <div style="
            background:#111827;
            border:1px solid #1f2937;
            border-left:6px solid {color};
            border-radius:18px;
            padding:18px 18px 14px 18px;
            min-height:110px;
        ">
            <div style="color:#94a3b8;font-size:14px;font-weight:600;margin-bottom:10px;">{title}</div>
            <div style="color:{color};font-size:28px;font-weight:800;line-height:1.1;">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_status_card(title: str, value: str, ok: bool = True):
    color = "#2ecc71" if ok else "#ff5a5f"
    st.markdown(
        f"""
        <div style="
            background:#0f172a;
            border:1px solid #1e293b;
            border-left:6px solid {color};
            border-radius:16px;
            padding:14px 16px;
            margin-bottom:10px;
        ">
            <div style="color:#94a3b8;font-size:13px;font-weight:600;">{title}</div>
            <div style="color:{color};font-size:20px;font-weight:800;margin-top:6px;">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def empty_trade_df() -> pd.DataFrame:
    cols = [
        "id", "symbol", "setup_type", "timeframe", "regime", "label",
        "score", "raw_score", "chance", "confidence",
        "entry", "stop", "target", "result_r", "outcome",
        "is_shadow", "created_at", "closed_at",
        "datetime_raw", "datetime", "entry_price", "exit_price", "pnl", "trade_type"
    ]
    return pd.DataFrame(columns=cols)


# ==========================================================
# DB HELPERS
# ==========================================================
def db_ready() -> bool:
    return bool(DATABASE_URL)


def get_db_conn():
    if not DATABASE_URL:
        return None
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
        connect_timeout=DB_CONNECT_TIMEOUT,
    )


@st.cache_data(ttl=30, show_spinner=False)
def table_exists(table_name: str, schema: str = "public") -> bool:
    if not db_ready():
        return False
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXISTS(
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema=%s AND table_name=%s
                    )
                    """,
                    (schema, table_name),
                )
                row = cur.fetchone()
                return bool(row[0]) if row else False
    except Exception:
        return False


@st.cache_data(ttl=30, show_spinner=False)
def get_table_columns(table_name: str, schema: str = "public") -> List[str]:
    if not db_ready():
        return []
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema=%s AND table_name=%s
                    ORDER BY ordinal_position
                    """,
                    (schema, table_name),
                )
                return [row[0] for row in cur.fetchall()]
    except Exception:
        return []


def read_sql_df(sql: str, params: Optional[tuple] = None) -> pd.DataFrame:
    if not db_ready():
        return pd.DataFrame([])
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                rows = cur.fetchall()
                cols = [desc[0] for desc in cur.description]
        return pd.DataFrame(rows, columns=cols)
    except Exception:
        return pd.DataFrame([])


@st.cache_data(ttl=30, show_spinner=False)
def table_count(table_name: str, schema: str = "public") -> int:
    if not table_exists(table_name, schema):
        return 0
    df = read_sql_df(f"SELECT COUNT(*) AS n FROM {schema}.{table_name}")
    if df.empty or "n" not in df.columns:
        return 0
    return safe_int(df.iloc[0]["n"], 0)


# ==========================================================
# BITVAVO / SNAPSHOT
# ==========================================================
def bitvavo_request(method: str, path: str, body: str = ""):
    if not API_KEY or not API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY of BITVAVO_API_SECRET ontbreken.")

    method_u = method.upper()
    timestamp = str(int(time.time() * 1000))
    body = body or ""

    message = f"{timestamp}{method_u}{path}{body}"
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Signature": signature,
        "Bitvavo-Access-Timestamp": timestamp,
        "Bitvavo-Access-Window": ACCESS_WINDOW_MS,
        "Content-Type": "application/json",
    }

    url = f"{BASE_URL}{path}"

    if method_u == "GET":
        r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    elif method_u == "POST":
        r = requests.post(url, headers=headers, data=body, timeout=HTTP_TIMEOUT)
    else:
        raise ValueError("Alleen GET/POST ondersteund.")

    if r.status_code >= 400:
        try:
            err = r.json()
        except Exception:
            err = {"error": r.text}
        raise RuntimeError(f"Bitvavo error {r.status_code}: {err}")

    return r.json()


@st.cache_data(ttl=60, show_spinner=False)
def fetch_all_market_prices():
    url = f"{BASE_URL}/v2/ticker/price"
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    prices = {}
    for row in data:
        market = row.get("market")
        price = row.get("price")
        if market and price is not None:
            try:
                prices[market] = float(price)
            except Exception:
                pass
    return prices


def price_in_eur(symbol: str, prices: dict):
    if symbol == "EUR":
        return 1.0, "EUR"

    direct = f"{symbol}-EUR"
    if direct in prices:
        return prices[direct], direct

    a = f"{symbol}-USDT"
    b = "USDT-EUR"
    if a in prices and b in prices:
        return prices[a] * prices[b], f"{a} * {b}"

    a = f"{symbol}-BTC"
    b = "BTC-EUR"
    if a in prices and b in prices:
        return prices[a] * prices[b], f"{a} * {b}"

    return None, "NO_ROUTE"


def build_snapshot_with_eur_values():
    balances = bitvavo_request("GET", "/v2/balance")
    prices = fetch_all_market_prices()

    assets = []
    eur_available = 0.0

    for row in balances:
        symbol = row.get("symbol")
        available = float(row.get("available", 0) or 0)
        in_order = float(row.get("inOrder", 0) or 0)
        total = available + in_order

        if symbol == "EUR":
            eur_available = available

        if total > 0:
            p_eur, route = price_in_eur(symbol, prices)
            eur_value = None
            if p_eur is not None:
                eur_value = float(total) * float(p_eur)

            assets.append({
                "symbol": symbol,
                "available": available,
                "inOrder": in_order,
                "total": total,
                "price_eur": p_eur,
                "eur_value": eur_value,
                "price_route": route,
            })

    crypto_assets_eur = 0.0
    for a in assets:
        if a["symbol"] != "EUR" and a.get("eur_value") is not None:
            crypto_assets_eur += float(a["eur_value"])

    total_portfolio_eur = float(eur_available) + float(crypto_assets_eur)

    snapshot = {
        "status": "OK",
        "ts": now_iso(),
        "eur_available": float(eur_available),
        "crypto_assets_eur": float(crypto_assets_eur),
        "total_portfolio_eur": float(total_portfolio_eur),
        "assets": sorted(assets, key=lambda x: (x["symbol"] != "EUR", x["symbol"])),
    }

    ensure_parent_dir(SNAPSHOT_PATH)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    return snapshot


def read_snapshot_only() -> Tuple[dict, str]:
    snapshot, snapshot_err = safe_read_json(SNAPSHOT_PATH)
    if snapshot is None:
        snapshot = {
            "status": "MISSING",
            "ts": None,
            "eur_available": 0.0,
            "crypto_assets_eur": 0.0,
            "total_portfolio_eur": 0.0,
            "assets": [],
        }
        return snapshot, snapshot_err or "Snapshot niet gevonden"
    return snapshot, "read-only snapshot"


# ==========================================================
# POSTGRES LOADERS
# ==========================================================
@st.cache_data(ttl=20, show_spinner=False)
def load_pending_orders_db() -> pd.DataFrame:
    if not table_exists("pending_approvals"):
        return pd.DataFrame([])

    cols = set(get_table_columns("pending_approvals"))
    wanted = [
        "id", "symbol", "status", "setup_type", "regime",
        "score", "chance", "confidence", "entry", "stop", "target",
        "timeframe", "created_at", "expires_at"
    ]
    selected = [c for c in wanted if c in cols]
    if not selected:
        return pd.DataFrame([])

    sql = f"""
        SELECT {", ".join(selected)}
        FROM public.pending_approvals
        WHERE COALESCE(status, 'PENDING') IN ('PENDING', 'APPROVED')
        ORDER BY COALESCE(chance, 0) DESC, COALESCE(score, 0) DESC, created_at DESC NULLS LAST
        LIMIT {PENDING_LIMIT}
    """
    df = read_sql_df(sql)
    if df.empty:
        return df

    for col in ["score", "chance", "confidence", "entry", "stop", "target"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    if "created_at" in df.columns:
        df["created_at_raw"] = pd.to_datetime(df["created_at"], errors="coerce", utc=True)
    if "expires_at" in df.columns:
        df["expires_at_raw"] = pd.to_datetime(df["expires_at"], errors="coerce", utc=True)

    return df


def normalize_trade_df(df: pd.DataFrame, trade_type: str) -> pd.DataFrame:
    if df.empty:
        return empty_trade_df()

    numeric_cols = ["entry", "stop", "target", "result_r", "score", "raw_score", "chance", "confidence"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        else:
            df[col] = 0.0

    df["created_at"] = pd.to_datetime(df.get("created_at"), errors="coerce", utc=True)
    df["closed_at"] = pd.to_datetime(df.get("closed_at"), errors="coerce", utc=True)

    # voor lijsten
    df["datetime_raw"] = df["closed_at"].where(~df["closed_at"].isna(), df["created_at"])
    df["datetime"] = df["datetime_raw"].apply(format_dt_short)

    df["entry_price"] = df["entry"]
    df["exit_price"] = df["target"]
    df["pnl"] = df["result_r"]
    df["trade_type"] = trade_type
    df["outcome"] = df.get("outcome", "UNKNOWN").fillna("UNKNOWN").astype(str).str.upper()

    return df


@st.cache_data(ttl=20, show_spinner=False)
def load_real_trades_db() -> pd.DataFrame:
    if not table_exists("experience_trades"):
        return empty_trade_df()

    cols = set(get_table_columns("experience_trades"))
    wanted = [
        "id", "symbol", "setup_type", "timeframe", "regime", "label",
        "score", "raw_score", "chance", "confidence",
        "entry", "stop", "target", "result_r", "outcome",
        "is_shadow", "created_at", "closed_at"
    ]
    selected = [c for c in wanted if c in cols]
    if not selected:
        return empty_trade_df()

    sql = f"""
        SELECT {", ".join(selected)}
        FROM public.experience_trades
        WHERE COALESCE(is_shadow, false) = false
        ORDER BY COALESCE(closed_at, created_at) DESC NULLS LAST
        LIMIT {REAL_LIMIT}
    """
    df = read_sql_df(sql)
    return normalize_trade_df(df, "REAL")


@st.cache_data(ttl=20, show_spinner=False)
def load_shadow_trades_db() -> pd.DataFrame:
    if not table_exists("experience_trades"):
        return empty_trade_df()

    cols = set(get_table_columns("experience_trades"))
    wanted = [
        "id", "symbol", "setup_type", "timeframe", "regime", "label",
        "score", "raw_score", "chance", "confidence",
        "entry", "stop", "target", "result_r", "outcome",
        "is_shadow", "created_at", "closed_at"
    ]
    selected = [c for c in wanted if c in cols]
    if not selected:
        return empty_trade_df()

    sql = f"""
        SELECT {", ".join(selected)}
        FROM public.experience_trades
        WHERE COALESCE(is_shadow, false) = true
        ORDER BY COALESCE(closed_at, created_at) DESC NULLS LAST
        LIMIT {SHADOW_LIMIT}
    """
    df = read_sql_df(sql)
    return normalize_trade_df(df, "SHADOW")


@st.cache_data(ttl=30, show_spinner=False)
def load_history_trades_db() -> pd.DataFrame:
    if not table_exists("experience_trades"):
        return empty_trade_df()

    cols = set(get_table_columns("experience_trades"))
    wanted = [
        "id", "symbol", "setup_type", "timeframe", "regime", "label",
        "score", "raw_score", "chance", "confidence",
        "entry", "stop", "target", "result_r", "outcome",
        "is_shadow", "created_at", "closed_at"
    ]
    selected = [c for c in wanted if c in cols]
    if not selected:
        return empty_trade_df()

    sql = f"""
        SELECT {", ".join(selected)}
        FROM public.experience_trades
        ORDER BY COALESCE(closed_at, created_at) DESC NULLS LAST
        LIMIT {HISTORY_LIMIT}
    """
    df = read_sql_df(sql)
    if df.empty:
        return empty_trade_df()

    df["trade_type"] = df["is_shadow"].apply(lambda x: "SHADOW" if bool(x) else "REAL")
    return normalize_trade_df(df, "MIXED")


@st.cache_data(ttl=20, show_spinner=False)
def load_positions_db() -> pd.DataFrame:
    candidate_tables = [
        "open_positions",
        "positions",
        "open_trades",
        "live_positions",
        "paper_positions",
    ]
    for table_name in candidate_tables:
        if table_exists(table_name):
            cols = get_table_columns(table_name)
            if cols:
                sql = f"SELECT {', '.join(cols)} FROM public.{table_name} LIMIT 100"
                df = read_sql_df(sql)
                if not df.empty:
                    return df
    return pd.DataFrame([])


@st.cache_data(ttl=20, show_spinner=False)
def load_scoreboard_db() -> pd.DataFrame:
    if not table_exists("experience_scoreboard"):
        return pd.DataFrame([])

    cols = set(get_table_columns("experience_scoreboard"))
    wanted = [
        "exchange", "timeframe", "setup_type", "regime",
        "n_total", "n_win", "n_loss", "winrate", "avg_r",
        "expectancy", "updated_at"
    ]
    selected = [c for c in wanted if c in cols]
    if not selected:
        return pd.DataFrame([])

    sql = f"""
        SELECT {", ".join(selected)}
        FROM public.experience_scoreboard
        ORDER BY COALESCE(n_total, 0) DESC NULLS LAST
        LIMIT {SCOREBOARD_LIMIT}
    """
    df = read_sql_df(sql)
    if df.empty:
        return df

    for col in ["n_total", "n_win", "n_loss", "winrate", "avg_r", "expectancy"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    if "updated_at" in df.columns:
        df["updated_at"] = pd.to_datetime(df["updated_at"], errors="coerce", utc=True).dt.strftime("%Y.%m.%d %H:%M:%S")
    return df


# ==========================================================
# BUILDERS
# ==========================================================
def build_activity_feed(orders_df: pd.DataFrame, real_df: pd.DataFrame, shadow_df: pd.DataFrame) -> List[Dict[str, Any]]:
    feed: List[Dict[str, Any]] = []

    if not orders_df.empty:
        for _, row in orders_df.head(5).iterrows():
            feed.append({
                "ts": parse_dt(row.get("created_at")),
                "title": f"PRE-BUY {safe_str(row.get('symbol'), '-')}",
                "sub": f"Chance {safe_int(row.get('chance'), 0)} | Score {safe_int(row.get('score'), 0)} | {safe_str(row.get('regime'), '-')}",
                "kind": "prebuy",
            })

    if not real_df.empty:
        for _, row in real_df.head(8).iterrows():
            pnl = safe_float(row.get("pnl"), 0.0)
            feed.append({
                "ts": row.get("datetime_raw"),
                "title": f"{safe_str(row.get('symbol'), '-')} REAL",
                "sub": f"Outcome {safe_str(row.get('outcome'), '-')} | {format_pnl(pnl)}",
                "kind": "real",
            })

    if not shadow_df.empty:
        for _, row in shadow_df.head(8).iterrows():
            pnl = safe_float(row.get("pnl"), 0.0)
            feed.append({
                "ts": row.get("datetime_raw"),
                "title": f"{safe_str(row.get('symbol'), '-')} SHADOW",
                "sub": f"Outcome {safe_str(row.get('outcome'), '-')} | {format_pnl(pnl)}",
                "kind": "shadow",
            })

    def _sort_key(x):
        ts = x.get("ts")
        if ts is None or pd.isna(ts):
            return pd.Timestamp.min.tz_localize("UTC")
        return ts

    return sorted(feed, key=_sort_key, reverse=True)[:15]


def build_main_chart_df(real_df: pd.DataFrame, shadow_df: pd.DataFrame) -> pd.DataFrame:
    rows = []

    if not real_df.empty:
        real_closed = real_df[
            real_df["outcome"].isin(["WIN", "LOSS", "FLAT", "BREAKEVEN"])
            & real_df["datetime_raw"].notna()
        ].copy()

        for _, row in real_closed.iterrows():
            ts = row.get("datetime_raw")
            pnl = safe_float(row.get("pnl"), 0.0)
            rows.append({
                "ts": ts,
                "real_profit": max(pnl, 0.0),
                "real_loss": min(pnl, 0.0),
                "shadow_profit": 0.0,
                "missed_good": 0.0,
                "missed_bad": 0.0,
            })

    if not shadow_df.empty:
        shadow_closed = shadow_df[
            shadow_df["outcome"].isin(["WIN", "LOSS", "FLAT", "BREAKEVEN"])
            & shadow_df["closed_at"].notna()
            & shadow_df["datetime_raw"].notna()
        ].copy()

        for _, row in shadow_closed.iterrows():
            ts = row.get("datetime_raw")
            pnl = safe_float(row.get("pnl"), 0.0)
            rows.append({
                "ts": ts,
                "real_profit": 0.0,
                "real_loss": 0.0,
                "shadow_profit": pnl,
                "missed_good": max(pnl, 0.0),
                "missed_bad": min(pnl, 0.0),
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["ts"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
    df = df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    if df.empty:
        return df

    for col in ["real_profit", "real_loss", "shadow_profit", "missed_good", "missed_bad"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).cumsum()

    return df


def prepare_history_df(history_df: pd.DataFrame) -> pd.DataFrame:
    if history_df.empty:
        return history_df

    out = history_df.copy()

    # trade_type opnieuw forceren vanuit is_shadow
    if "is_shadow" in out.columns:
        out["trade_type"] = out["is_shadow"].apply(lambda x: "SHADOW" if bool(x) else "REAL")
    else:
        out["trade_type"] = "REAL"

    out["symbol"] = out["symbol"].fillna("-").astype(str)
    out["setup_type"] = out["setup_type"].fillna("-").astype(str)
    out["regime"] = out["regime"].fillna("-").astype(str)
    out["timeframe"] = out["timeframe"].fillna("-").astype(str)
    out["outcome"] = out["outcome"].fillna("UNKNOWN").astype(str).str.upper()

    out["sort_ts"] = out["closed_at"].where(~out["closed_at"].isna(), out["created_at"])
    out["sort_ts"] = pd.to_datetime(out["sort_ts"], errors="coerce", utc=True)
    out = out.sort_values("sort_ts").reset_index(drop=True)

    return out


def filter_history_df(
    df: pd.DataFrame,
    type_filter: str,
    outcome_filter: str,
    coin_filter: str,
    setup_filter: str,
    regime_filter: str,
    timeframe_filter: str,
    days_filter: str,
) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()

    if type_filter != "ALLES":
        out = out[out["trade_type"] == type_filter]

    if outcome_filter != "ALLES":
        out = out[out["outcome"] == outcome_filter]

    if coin_filter != "ALLES":
        out = out[out["symbol"] == coin_filter]

    if setup_filter != "ALLES":
        out = out[out["setup_type"] == setup_filter]

    if regime_filter != "ALLES":
        out = out[out["regime"] == regime_filter]

    if timeframe_filter != "ALLES":
        out = out[out["timeframe"] == timeframe_filter]

    if days_filter != "ALLES":
        days_map = {"7D": 7, "30D": 30, "90D": 90, "180D": 180}
        days = days_map.get(days_filter)
        if days:
            cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
            out = out[out["sort_ts"] >= cutoff]

    return out


def history_cumulative_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame([])

    rows = []
    for _, row in df.iterrows():
        if pd.isna(row.get("sort_ts")):
            continue

        trade_type = safe_str(row.get("trade_type")).upper()
        pnl = safe_float(row.get("pnl"), 0.0)

        rows.append({
            "ts": row.get("sort_ts"),
            "real_profit": max(pnl, 0.0) if trade_type == "REAL" else 0.0,
            "real_loss": min(pnl, 0.0) if trade_type == "REAL" else 0.0,
            "shadow_profit": max(pnl, 0.0) if trade_type == "SHADOW" else 0.0,
            "shadow_loss": min(pnl, 0.0) if trade_type == "SHADOW" else 0.0,
        })

    out = pd.DataFrame(rows)
    if out.empty:
        return out

    out = out.sort_values("ts").reset_index(drop=True)
    for col in ["real_profit", "real_loss", "shadow_profit", "shadow_loss"]:
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0).cumsum()
    return out


# ==========================================================
# PAGE SETUP / STYLE
# ==========================================================
st.set_page_config(page_title="Crypto AI Terminal", layout="wide")

st.markdown("""
<style>
    .stApp {
        background:#0a0f18;
        color:#f8fafc;
    }
    .block-container {
        max-width:1800px;
        padding-top:1rem;
        padding-bottom:2rem;
    }
    div[data-testid="stMetric"] {
        background:#111827;
        border:1px solid #1f2937;
        border-radius:18px;
        padding:10px 14px;
    }
    div[data-testid="stMetric"] label {
        color:#94a3b8 !important;
    }
    div[data-testid="stMetricValue"] {
        color:#ffffff !important;
    }
    button[role="tab"] {
        color:#e5e7eb !important;
        font-weight:700 !important;
    }
    .terminal-box {
        background:#0f172a;
        border:1px solid #1e293b;
        border-radius:22px;
        padding:16px;
        height:100%;
        box-shadow:0 10px 30px rgba(0,0,0,0.25);
    }
    .section-title {
        font-size:20px;
        font-weight:800;
        color:#f8fafc;
        margin-bottom:14px;
    }
    .subtle {
        color:#94a3b8;
        font-size:13px;
    }
    .info-row {
        display:flex;
        justify-content:space-between;
        padding:8px 0;
        border-bottom:1px solid rgba(255,255,255,0.06);
        font-size:14px;
    }
    .info-left {
        color:#cbd5e1;
        font-weight:600;
    }
    .info-right {
        color:#ffffff;
        font-weight:700;
        text-align:right;
    }
    .legend-item {
        display:flex;
        align-items:center;
        gap:10px;
        color:#e5e7eb;
        font-size:13px;
        font-weight:600;
    }
    .dot {
        width:14px;
        height:14px;
        border-radius:50%;
        display:inline-block;
    }
    .deal-row {
        display:flex;
        justify-content:space-between;
        align-items:flex-start;
        border-bottom:1px solid rgba(255,255,255,0.05);
        padding:14px 4px;
    }
    .deal-left {
        width:68%;
    }
    .deal-main {
        font-size:20px;
        font-weight:800;
        color:#f8fafc;
        line-height:1.2;
    }
    .deal-sub {
        color:#cbd5e1;
        margin-top:4px;
        font-size:14px;
    }
    .deal-right {
        width:32%;
        text-align:right;
    }
    .deal-dt {
        color:#94a3b8;
        font-size:12px;
        margin-bottom:8px;
    }
    .deal-pnl {
        font-size:28px;
        font-weight:800;
        line-height:1;
    }
    .activity-item {
        border-bottom:1px solid rgba(255,255,255,0.05);
        padding:10px 0;
    }
    .activity-title {
        color:#f8fafc;
        font-weight:700;
        font-size:14px;
    }
    .activity-sub {
        color:#94a3b8;
        font-size:13px;
        margin-top:3px;
    }
    .panel-highlight {
        background:linear-gradient(180deg,#111827 0%,#0f172a 100%);
        border:1px solid #1f2937;
        border-left:6px solid #5aa2ff;
        border-radius:18px;
        padding:16px;
        margin-bottom:12px;
    }
</style>
""", unsafe_allow_html=True)


# ==========================================================
# UI FIRST
# ==========================================================
st.markdown("## Crypto AI Terminal")

with st.sidebar:
    st.markdown("### Dashboard")
    if st.button("Refresh data"):
        st.cache_data.clear()
        st.rerun()

    st.markdown("### Snapshot")
    if st.button("Refresh snapshot now"):
        try:
            build_snapshot_with_eur_values()
            st.success("Snapshot vernieuwd.")
        except Exception as e:
            st.error(f"Snapshot refresh mislukt: {e}")

status_placeholder = st.empty()
status_placeholder.caption("Dashboard wordt geladen...")


# ==========================================================
# SAFE DATA LOAD
# ==========================================================
snapshot, snapshot_state = read_snapshot_only()

positions_df = load_positions_db()
orders_df = load_pending_orders_db()
real_df = load_real_trades_db()
shadow_df = load_shadow_trades_db()
scoreboard_df = load_scoreboard_db()
history_df = load_history_trades_db()

feed = build_activity_feed(orders_df, real_df, shadow_df)
chart_df = build_main_chart_df(real_df, shadow_df)

eur_available = safe_float((snapshot or {}).get("eur_available"), 0.0)
crypto_assets_eur = safe_float((snapshot or {}).get("crypto_assets_eur"), 0.0)
total_portfolio_eur = safe_float((snapshot or {}).get("total_portfolio_eur"), 0.0)

# alleen gesloten + bruikbare outcomes voor echte performance cijfers
real_perf_df = real_df[real_df["outcome"].isin(["WIN", "LOSS", "FLAT", "BREAKEVEN"])].copy() if not real_df.empty else empty_trade_df()
shadow_perf_df = shadow_df[
    shadow_df["outcome"].isin(["WIN", "LOSS", "FLAT", "BREAKEVEN"]) & shadow_df["closed_at"].notna()
].copy() if not shadow_df.empty else empty_trade_df()

real_profit = float(pd.to_numeric(real_perf_df["pnl"], errors="coerce").fillna(0).sum()) if not real_perf_df.empty else 0.0
shadow_profit = float(pd.to_numeric(shadow_perf_df["pnl"], errors="coerce").fillna(0).sum()) if not shadow_perf_df.empty else 0.0

today_pnl = 0.0
if not real_perf_df.empty:
    tmp_real = real_perf_df.copy()
    tmp_real["date_only"] = pd.to_datetime(tmp_real["datetime_raw"], errors="coerce", utc=True).dt.date
    if not tmp_real["date_only"].isna().all():
        last_day = tmp_real["date_only"].dropna().max()
        today_pnl = float(pd.to_numeric(tmp_real.loc[tmp_real["date_only"] == last_day, "pnl"], errors="coerce").fillna(0).sum())

open_trades_count = len(positions_df) if not positions_df.empty else 0
pending_count = len(orders_df) if not orders_df.empty else 0
missed_count = len(shadow_df) if not shadow_df.empty else 0

missed_good_count = int((shadow_perf_df["pnl"] > 0).sum()) if not shadow_perf_df.empty else 0
missed_bad_count = int((shadow_perf_df["pnl"] < 0).sum()) if not shadow_perf_df.empty else 0

shadow_winrate = 0.0
if not shadow_perf_df.empty:
    valid_shadow = len(shadow_perf_df)
    wins_shadow = int((shadow_perf_df["pnl"] > 0).sum())
    shadow_winrate = (wins_shadow / valid_shadow * 100.0) if valid_shadow else 0.0

real_winrate = 0.0
if not real_perf_df.empty:
    valid_real = len(real_perf_df)
    wins_real = int((real_perf_df["pnl"] > 0).sum())
    real_winrate = (wins_real / valid_real * 100.0) if valid_real else 0.0

status_placeholder.caption(f"Snapshot status: {snapshot_state} | Data source: {'Postgres' if db_ready() else 'Geen DATABASE_URL'}")


# ==========================================================
# TOP METRICS
# ==========================================================
top_metrics_1 = st.columns(4)
top_metrics_1[0].metric("Balance", format_money(eur_available))
top_metrics_1[1].metric("Crypto", format_money(crypto_assets_eur))
top_metrics_1[2].metric("Total", format_money(total_portfolio_eur))
top_metrics_1[3].metric("Profit", format_pnl(real_profit))

top_metrics_2 = st.columns(4)
top_metrics_2[0].metric("Open Trades", str(open_trades_count))
top_metrics_2[1].metric("Pending Orders", str(pending_count))
top_metrics_2[2].metric("Missed Trades", str(missed_count))
top_metrics_2[3].metric("Today PnL", format_pnl(today_pnl))

st.divider()


# ==========================================================
# TOP LAYOUT
# ==========================================================
col_left, col_center, col_right = st.columns([0.9, 2.2, 0.9], gap="large")

with col_left:
    st.markdown('<div class="terminal-box">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Trade Panel</div>', unsafe_allow_html=True)

    if orders_df.empty:
        st.markdown('<div class="subtle">Geen pending Pre-BUY gevonden in Postgres.</div>', unsafe_allow_html=True)
    else:
        best = orders_df.iloc[0]
        chance_val = safe_int(best.get("chance"), 0)
        panel_color = "#5aa2ff" if chance_val >= 85 else "#f59e0b"

        st.markdown(
            f"""
            <div class="panel-highlight" style="border-left-color:{panel_color};">
                <div style="font-size:26px;font-weight:900;color:#f8fafc;">{safe_str(best.get('symbol'), '-')}</div>
                <div style="margin-top:6px;color:#94a3b8;font-size:13px;font-weight:600;">
                    {safe_str(best.get('setup_type'), '-')} • {safe_str(best.get('regime'), '-')}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        info_rows = [
            ("Chance", str(safe_int(best.get("chance"), 0))),
            ("Score", str(safe_int(best.get("score"), 0))),
            ("Confidence", str(safe_int(best.get("confidence"), 0))),
            ("Entry", f"{safe_float(best.get('entry'), 0.0):.6f}"),
            ("Stop", f"{safe_float(best.get('stop'), 0.0):.6f}"),
            ("Target", f"{safe_float(best.get('target'), 0.0):.6f}"),
            ("Timeframe", safe_str(best.get("timeframe"), "-")),
            ("Status", safe_str(best.get("status"), "PENDING")),
            ("Expires", format_dt_short(best.get("expires_at"))),
        ]

        for left_label, right_val in info_rows:
            st.markdown(
                f"""
                <div class="info-row">
                    <div class="info-left">{left_label}</div>
                    <div class="info-right">{right_val}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown("</div>", unsafe_allow_html=True)

with col_center:
    st.markdown('<div class="terminal-box">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Performance Overview</div>', unsafe_allow_html=True)

    lg1, lg2, lg3, lg4, lg5 = st.columns(5)
    lg1.markdown('<div class="legend-item"><span class="dot" style="background:#5aa2ff;"></span> Blauw = echte winst</div>', unsafe_allow_html=True)
    lg2.markdown('<div class="legend-item"><span class="dot" style="background:#ff5a5f;"></span> Rood = echte verlies</div>', unsafe_allow_html=True)
    lg3.markdown('<div class="legend-item"><span class="dot" style="background:#b0b7c3;"></span> Grijs = shadow winst/flat</div>', unsafe_allow_html=True)
    lg4.markdown('<div class="legend-item"><span class="dot" style="background:#2ecc71;"></span> Groen = gemiste trade goed</div>', unsafe_allow_html=True)
    lg5.markdown('<div class="legend-item"><span class="dot" style="background:#ff8c42;"></span> Oranje = gemiste trade slecht</div>', unsafe_allow_html=True)

    if chart_df.empty:
        st.info("Nog niet genoeg gesloten data uit Postgres voor de 5-lijnen grafiek.")
    else:
        fig = go.Figure()

        fig.add_trace(go.Scatter(
            x=chart_df["ts"],
            y=chart_df["real_profit"],
            mode="lines",
            name="Echte winst",
            line=dict(color="#5aa2ff", width=4),
        ))
        fig.add_trace(go.Scatter(
            x=chart_df["ts"],
            y=chart_df["real_loss"],
            mode="lines",
            name="Echte verlies",
            line=dict(color="#ff5a5f", width=4),
        ))
        fig.add_trace(go.Scatter(
            x=chart_df["ts"],
            y=chart_df["shadow_profit"],
            mode="lines",
            name="Shadow profit",
            line=dict(color="#b0b7c3", width=4),
        ))
        fig.add_trace(go.Scatter(
            x=chart_df["ts"],
            y=chart_df["missed_good"],
            mode="lines",
            name="Gemiste trade goed",
            line=dict(color="#2ecc71", width=4),
        ))
        fig.add_trace(go.Scatter(
            x=chart_df["ts"],
            y=chart_df["missed_bad"],
            mode="lines",
            name="Gemiste trade slecht",
            line=dict(color="#ff8c42", width=4),
        ))

        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0f172a",
            plot_bgcolor="#0f172a",
            font=dict(color="#f8fafc"),
            height=620,
            margin=dict(l=10, r=10, t=10, b=10),
            showlegend=False,
            xaxis=dict(title="Tijd", gridcolor="rgba(255,255,255,0.07)"),
            yaxis=dict(title="Resultaat (R)", gridcolor="rgba(255,255,255,0.07)", zerolinecolor="rgba(255,255,255,0.15)"),
        )
        st.plotly_chart(fig, use_container_width=True)

    perf1, perf2, perf3, perf4 = st.columns(4)
    with perf1:
        render_perf_card("Real Profit", format_pnl(real_profit), pnl_color(real_profit, "REAL"))
    with perf2:
        render_perf_card("Shadow Profit", format_pnl(shadow_profit), pnl_color(shadow_profit, "SHADOW"))
    with perf3:
        render_perf_card("Real Winrate", f"{real_winrate:.1f}%", "#5aa2ff")
    with perf4:
        render_perf_card("Shadow Winrate", f"{shadow_winrate:.1f}%", "#2ecc71" if shadow_winrate >= 50 else "#ff8c42")

    st.markdown("</div>", unsafe_allow_html=True)

with col_right:
    st.markdown('<div class="terminal-box">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Bot Activity</div>', unsafe_allow_html=True)

    if not feed:
        st.markdown('<div class="subtle">Nog geen activity gevonden in Postgres.</div>', unsafe_allow_html=True)
    else:
        for item in feed:
            kind = item.get("kind", "")
            color = "#f59e0b"
            if kind == "real":
                color = "#5aa2ff"
            elif kind == "shadow":
                sub_txt = safe_str(item.get("sub"), "").upper()
                if "WIN" in sub_txt:
                    color = "#2ecc71"
                elif "LOSS" in sub_txt:
                    color = "#ff8c42"
                else:
                    color = "#b0b7c3"

            st.markdown(
                f"""
                <div class="activity-item">
                    <div class="activity-title" style="color:{color};">{safe_str(item.get('title'), '-')}</div>
                    <div class="activity-sub">{safe_str(item.get('sub'), '-')}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-title">Bot Health</div>', unsafe_allow_html=True)

    render_status_card("DATABASE_URL", "OK" if db_ready() else "MIST", db_ready())
    render_status_card("pending_approvals", str(table_count("pending_approvals")) if table_exists("pending_approvals") else "MIST", table_exists("pending_approvals"))
    render_status_card("experience_trades", str(table_count("experience_trades")) if table_exists("experience_trades") else "MIST", table_exists("experience_trades"))
    render_status_card("experience_scoreboard", str(table_count("experience_scoreboard")) if table_exists("experience_scoreboard") else "MIST", table_exists("experience_scoreboard"))
    render_status_card("snapshot file", "OK" if file_meta(SNAPSHOT_PATH).get("exists") else "MIST", file_meta(SNAPSHOT_PATH).get("exists"))

    st.markdown("</div>", unsafe_allow_html=True)

st.divider()


# ==========================================================
# TABS
# ==========================================================
tabs = st.tabs(["Positions", "Orders", "Deals", "Shadow Trades", "Performance", "Geschiedenis", "Settings"])

with tabs[0]:
    st.subheader("Open Positions")
    if positions_df.empty:
        st.info("Geen open positions gevonden in Postgres.")
    else:
        st.dataframe(positions_df, use_container_width=True, hide_index=True)

with tabs[1]:
    st.subheader("Orders / Pending Pre-BUY")
    if orders_df.empty:
        st.info("Geen pending orders gevonden in Postgres.")
    else:
        show = orders_df.copy()
        if "expires_at" in show.columns:
            show["expires_at"] = pd.to_datetime(show["expires_at"], errors="coerce", utc=True).dt.strftime("%Y.%m.%d %H:%M:%S")
        if "created_at" in show.columns:
            show["created_at"] = pd.to_datetime(show["created_at"], errors="coerce", utc=True).dt.strftime("%Y.%m.%d %H:%M:%S")
        st.dataframe(show, use_container_width=True, hide_index=True)

with tabs[2]:
    st.subheader("Deals / Echte Trades")
    if real_df.empty:
        st.info("Nog geen echte trades gevonden in Postgres.")
    else:
        for _, row in real_df.head(80).iterrows():
            sym = safe_str(row.get("symbol"), "-")
            outcome = safe_str(row.get("outcome"), "-").upper()
            entry_price = safe_float(row.get("entry_price"), 0.0)
            exit_price = safe_float(row.get("exit_price"), 0.0)
            pnl = safe_float(row.get("pnl"), 0.0)
            dt_txt = safe_str(row.get("datetime"), "-")

            st.markdown(
                f"""
                <div class="deal-row">
                    <div class="deal-left">
                        <div class="deal-main">
                            {sym}
                            <span style="color:{outcome_color(outcome, 'REAL')};font-weight:800;"> {outcome}</span>
                        </div>
                        <div class="deal-sub">{entry_price:.6f} → {exit_price:.6f}</div>
                    </div>
                    <div class="deal-right">
                        <div class="deal-dt">{dt_txt}</div>
                        <div class="deal-pnl" style="color:{pnl_color(pnl, 'REAL')};">{format_pnl(pnl)}</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

with tabs[3]:
    st.subheader("Shadow Trades / Niet genomen trades")
    if shadow_df.empty:
        st.info("Nog geen shadow trades gevonden in Postgres.")
    else:
        for _, row in shadow_df.head(80).iterrows():
            sym = safe_str(row.get("symbol"), "-")
            outcome = safe_str(row.get("outcome"), "-").upper()
            entry_price = safe_float(row.get("entry_price"), 0.0)
            exit_price = safe_float(row.get("exit_price"), 0.0)
            pnl = safe_float(row.get("pnl"), 0.0)
            dt_txt = safe_str(row.get("datetime"), "-")

            st.markdown(
                f"""
                <div class="deal-row">
                    <div class="deal-left">
                        <div class="deal-main">
                            {sym}
                            <span style="color:{outcome_color(outcome, 'SHADOW')};font-weight:800;"> {outcome}</span>
                        </div>
                        <div class="deal-sub">{entry_price:.6f} → {exit_price:.6f}</div>
                    </div>
                    <div class="deal-right">
                        <div class="deal-dt">{dt_txt}</div>
                        <div class="deal-pnl" style="color:{pnl_color(pnl, 'SHADOW')};">{format_pnl(pnl)}</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

with tabs[4]:
    st.subheader("Performance")

    p1, p2, p3, p4 = st.columns(4)
    with p1:
        render_perf_card("Real Profit", format_pnl(real_profit), pnl_color(real_profit, "REAL"))
    with p2:
        render_perf_card("Shadow Profit", format_pnl(shadow_profit), pnl_color(shadow_profit, "SHADOW"))
    with p3:
        render_perf_card("Missed Wins", str(missed_good_count), "#2ecc71")
    with p4:
        render_perf_card("Missed Losses", str(missed_bad_count), "#ff8c42")

    st.markdown("<br>", unsafe_allow_html=True)

    if scoreboard_df.empty:
        st.info("Geen experience_scoreboard data gevonden in Postgres.")
    else:
        st.markdown("#### Setup / Regime Scoreboard")
        st.dataframe(scoreboard_df, use_container_width=True, hide_index=True)

with tabs[5]:
    st.subheader("Geschiedenis")

    hist_df = prepare_history_df(history_df)

    f1, f2, f3, f4 = st.columns(4)
    with f1:
        type_filter = st.selectbox("Type", ["ALLES", "REAL", "SHADOW"], index=0)
    with f2:
        outcome_opts = ["ALLES"] + sorted([x for x in hist_df["outcome"].dropna().unique().tolist()]) if not hist_df.empty else ["ALLES"]
        outcome_filter = st.selectbox("Outcome", outcome_opts, index=0)
    with f3:
        coin_opts = ["ALLES"] + sorted(hist_df["symbol"].dropna().astype(str).unique().tolist()) if not hist_df.empty else ["ALLES"]
        coin_filter = st.selectbox("Coin", coin_opts, index=0)
    with f4:
        days_filter = st.selectbox("Periode", ["ALLES", "7D", "30D", "90D", "180D"], index=0)

    f5, f6, f7 = st.columns(3)
    with f5:
        setup_opts = ["ALLES"] + sorted(hist_df["setup_type"].dropna().astype(str).unique().tolist()) if not hist_df.empty else ["ALLES"]
        setup_filter = st.selectbox("Setup", setup_opts, index=0)
    with f6:
        regime_opts = ["ALLES"] + sorted(hist_df["regime"].dropna().astype(str).unique().tolist()) if not hist_df.empty else ["ALLES"]
        regime_filter = st.selectbox("Regime", regime_opts, index=0)
    with f7:
        tf_opts = ["ALLES"] + sorted(hist_df["timeframe"].dropna().astype(str).unique().tolist()) if not hist_df.empty else ["ALLES"]
        timeframe_filter = st.selectbox("Timeframe", tf_opts, index=0)

    filtered_hist = filter_history_df(
        hist_df,
        type_filter=type_filter,
        outcome_filter=outcome_filter,
        coin_filter=coin_filter,
        setup_filter=setup_filter,
        regime_filter=regime_filter,
        timeframe_filter=timeframe_filter,
        days_filter=days_filter,
    )

    gh1, gh2, gh3, gh4 = st.columns(4)
    total_hist = len(filtered_hist)
    real_hist = int((filtered_hist["trade_type"] == "REAL").sum()) if not filtered_hist.empty else 0
    shadow_hist = int((filtered_hist["trade_type"] == "SHADOW").sum()) if not filtered_hist.empty else 0
    hist_r = float(pd.to_numeric(filtered_hist["pnl"], errors="coerce").fillna(0).sum()) if not filtered_hist.empty else 0.0

    with gh1:
        render_perf_card("Totaal Trades", str(total_hist), "#94a3b8")
    with gh2:
        render_perf_card("Real Trades", str(real_hist), "#5aa2ff")
    with gh3:
        render_perf_card("Shadow Trades", str(shadow_hist), "#2ecc71")
    with gh4:
        render_perf_card("Totaal Resultaat", f"{hist_r:+.2f} R", "#5aa2ff" if hist_r >= 0 else "#ff5a5f")

    st.markdown("<br>", unsafe_allow_html=True)

    # hoofdgrafiek geschiedenis
    hist_curve = history_cumulative_df(filtered_hist)
    if hist_curve.empty:
        st.info("Geen bruikbare geschiedenis-data voor grafieken.")
    else:
        fig_hist = go.Figure()
        fig_hist.add_trace(go.Scatter(
            x=hist_curve["ts"], y=hist_curve["real_profit"],
            mode="lines", name="Real winst", line=dict(color="#5aa2ff", width=4)
        ))
        fig_hist.add_trace(go.Scatter(
            x=hist_curve["ts"], y=hist_curve["real_loss"],
            mode="lines", name="Real verlies", line=dict(color="#ff5a5f", width=4)
        ))
        fig_hist.add_trace(go.Scatter(
            x=hist_curve["ts"], y=hist_curve["shadow_profit"],
            mode="lines", name="Shadow winst", line=dict(color="#2ecc71", width=4)
        ))
        fig_hist.add_trace(go.Scatter(
            x=hist_curve["ts"], y=hist_curve["shadow_loss"],
            mode="lines", name="Shadow verlies", line=dict(color="#ff8c42", width=4)
        ))
        fig_hist.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0f172a",
            plot_bgcolor="#0f172a",
            font=dict(color="#f8fafc"),
            height=500,
            margin=dict(l=10, r=10, t=10, b=10),
            showlegend=True,
            xaxis=dict(title="Tijd", gridcolor="rgba(255,255,255,0.07)"),
            yaxis=dict(title="Cumulatief resultaat (R)", gridcolor="rgba(255,255,255,0.07)"),
        )
        st.plotly_chart(fig_hist, use_container_width=True)

    c1, c2 = st.columns(2)

    with c1:
        st.markdown("#### Win / Loss verhouding")
        if filtered_hist.empty:
            st.info("Geen data.")
        else:
            outcome_counts = filtered_hist["outcome"].value_counts()
            bar_fig = go.Figure()
            for label, color in [
                ("WIN", "#2ecc71"),
                ("LOSS", "#ff5a5f"),
                ("FLAT", "#b0b7c3"),
                ("BREAKEVEN", "#b0b7c3"),
                ("UNKNOWN", "#b0b7c3"),
            ]:
                val = safe_int(outcome_counts.get(label, 0), 0)
                if val > 0:
                    bar_fig.add_bar(x=[label], y=[val], marker_color=color, name=label)
            bar_fig.update_layout(
                template="plotly_dark",
                paper_bgcolor="#0f172a",
                plot_bgcolor="#0f172a",
                font=dict(color="#f8fafc"),
                height=380,
                showlegend=False,
                xaxis=dict(title="Outcome"),
                yaxis=dict(title="Aantal"),
            )
            st.plotly_chart(bar_fig, use_container_width=True)

    with c2:
        st.markdown("#### Resultaat per coin")
        if filtered_hist.empty:
            st.info("Geen data.")
        else:
            per_coin = (
                filtered_hist.groupby("symbol", dropna=False)["pnl"]
                .sum()
                .reset_index()
                .sort_values("pnl", ascending=False)
                .head(15)
            )
            coin_fig = go.Figure()
            coin_fig.add_bar(
                x=per_coin["symbol"],
                y=per_coin["pnl"],
                marker_color=["#5aa2ff" if v >= 0 else "#ff5a5f" for v in per_coin["pnl"]],
            )
            coin_fig.update_layout(
                template="plotly_dark",
                paper_bgcolor="#0f172a",
                plot_bgcolor="#0f172a",
                font=dict(color="#f8fafc"),
                height=380,
                showlegend=False,
                xaxis=dict(title="Coin"),
                yaxis=dict(title="Resultaat (R)"),
            )
            st.plotly_chart(coin_fig, use_container_width=True)

    c3, c4 = st.columns(2)

    with c3:
        st.markdown("#### Resultaat per setup")
        if filtered_hist.empty:
            st.info("Geen data.")
        else:
            per_setup = (
                filtered_hist.groupby("setup_type", dropna=False)["pnl"]
                .sum()
                .reset_index()
                .sort_values("pnl", ascending=False)
            )
            setup_fig = go.Figure()
            setup_fig.add_bar(
                x=per_setup["setup_type"],
                y=per_setup["pnl"],
                marker_color=["#2ecc71" if v >= 0 else "#ff8c42" for v in per_setup["pnl"]],
            )
            setup_fig.update_layout(
                template="plotly_dark",
                paper_bgcolor="#0f172a",
                plot_bgcolor="#0f172a",
                font=dict(color="#f8fafc"),
                height=380,
                showlegend=False,
                xaxis=dict(title="Setup"),
                yaxis=dict(title="Resultaat (R)"),
            )
            st.plotly_chart(setup_fig, use_container_width=True)

    with c4:
        st.markdown("#### Resultaat per regime")
        if filtered_hist.empty:
            st.info("Geen data.")
        else:
            per_regime = (
                filtered_hist.groupby("regime", dropna=False)["pnl"]
                .sum()
                .reset_index()
                .sort_values("pnl", ascending=False)
            )
            regime_fig = go.Figure()
            regime_fig.add_bar(
                x=per_regime["regime"],
                y=per_regime["pnl"],
                marker_color=["#2ecc71" if v >= 0 else "#ff8c42" for v in per_regime["pnl"]],
            )
            regime_fig.update_layout(
                template="plotly_dark",
                paper_bgcolor="#0f172a",
                plot_bgcolor="#0f172a",
                font=dict(color="#f8fafc"),
                height=380,
                showlegend=False,
                xaxis=dict(title="Regime"),
                yaxis=dict(title="Resultaat (R)"),
            )
            st.plotly_chart(regime_fig, use_container_width=True)

    st.markdown("#### Volledige trade-geschiedenis")
    if filtered_hist.empty:
        st.info("Geen trades gevonden met deze filters.")
    else:
        show_cols = [
            "datetime", "symbol", "trade_type", "setup_type", "regime", "timeframe",
            "entry", "target", "pnl", "outcome", "label", "score", "chance"
        ]
        show_cols = [c for c in show_cols if c in filtered_hist.columns]
        show_df = filtered_hist[show_cols].copy()

        rename_map = {
            "datetime": "Datum",
            "symbol": "Coin",
            "trade_type": "Type",
            "setup_type": "Setup",
            "regime": "Regime",
            "timeframe": "TF",
            "entry": "Entry",
            "target": "Exit/Target",
            "pnl": "Resultaat (R)",
            "outcome": "Outcome",
            "label": "Label",
            "score": "Score",
            "chance": "Chance",
        }
        show_df = show_df.rename(columns=rename_map)
        st.dataframe(show_df.sort_values("Datum", ascending=False), use_container_width=True, hide_index=True)

with tabs[6]:
    st.subheader("Settings / Controle")
    rows = [
        {"item": "DATABASE_URL", "status": "OK" if db_ready() else "MIST"},
        {"item": "pending_approvals", "status": f"OK ({table_count('pending_approvals')})" if table_exists("pending_approvals") else "MIST"},
        {"item": "experience_trades", "status": f"OK ({table_count('experience_trades')})" if table_exists("experience_trades") else "MIST"},
        {"item": "experience_scoreboard", "status": f"OK ({table_count('experience_scoreboard')})" if table_exists("experience_scoreboard") else "MIST"},
        {"item": "snapshot file", "status": "OK" if file_meta(SNAPSHOT_PATH).get("exists") else "MIST"},
        {"item": "portfolio_history.csv", "status": "OK" if file_meta(PORTFOLIO_HISTORY_CSV).get("exists") else "MIST"},
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.markdown("#### Snapshot preview")
    st.json(snapshot)
