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
# PAGE CONFIG
# ==========================================================
st.set_page_config(
    page_title="Crypto AI Terminal",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ==========================================================
# CONFIG
# ==========================================================
API_KEY = (os.getenv("BITVAVO_API_KEY", "") or "").strip().strip('"').strip("'")
API_SECRET = (os.getenv("BITVAVO_API_SECRET", "") or "").strip().strip('"').strip("'")
DATABASE_URL = (os.getenv("DATABASE_URL", "") or "").strip()

BASE_URL = "https://api.bitvavo.com"
ACCESS_WINDOW_MS = os.getenv("BITVAVO_ACCESS_WINDOW_MS", "10000")

SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", "/data/account_snapshot.json")
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "10"))

# Heel belangrijk:
# korte timeouts zodat dashboard NOOIT eindeloos blijft hangen
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "4"))
DB_STATEMENT_TIMEOUT_MS = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "4000"))

# Snelle limits voor eerste load
PENDING_LIMIT = int(os.getenv("DASH_PENDING_LIMIT", "12"))
REAL_LIMIT = int(os.getenv("DASH_REAL_LIMIT", "80"))
SHADOW_LIMIT = int(os.getenv("DASH_SHADOW_LIMIT", "80"))
SCOREBOARD_LIMIT = int(os.getenv("DASH_SCOREBOARD_LIMIT", "12"))
HISTORY_LIMIT = int(os.getenv("DASH_HISTORY_LIMIT", "400"))


# ==========================================================
# SESSION STATE
# ==========================================================
if "load_history" not in st.session_state:
    st.session_state.load_history = False

if "last_snapshot_refresh_msg" not in st.session_state:
    st.session_state.last_snapshot_refresh_msg = ""


# ==========================================================
# STYLE
# ==========================================================
st.markdown(
    """
    <style>
        .stApp {
            background: #09111c;
            color: #f8fafc;
        }

        .block-container {
            max-width: 1800px;
            padding-top: 1.2rem;
            padding-bottom: 2rem;
        }

        section[data-testid="stSidebar"] {
            background: linear-gradient(180deg, #0d1522 0%, #101b2c 100%);
            border-right: 1px solid rgba(255,255,255,0.06);
        }

        section[data-testid="stSidebar"] .block-container {
            padding-top: 1rem;
        }

        .sidebar-brand {
            background: linear-gradient(180deg, #13233a 0%, #0f1b2d 100%);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 20px;
            padding: 18px 18px 16px 18px;
            margin-bottom: 18px;
            box-shadow: 0 10px 24px rgba(0,0,0,0.22);
        }

        .sidebar-brand-title {
            font-size: 22px;
            font-weight: 900;
            color: #ffffff;
            line-height: 1.1;
            margin-bottom: 6px;
        }

        .sidebar-brand-sub {
            color: #94a3b8;
            font-size: 13px;
            line-height: 1.4;
        }

        .sidebar-card {
            background: linear-gradient(180deg, #111c2c 0%, #0f1727 100%);
            border: 1px solid rgba(255,255,255,0.07);
            border-radius: 18px;
            padding: 14px 14px 12px 14px;
            margin-bottom: 12px;
        }

        .sidebar-card-title {
            color: #ffffff;
            font-size: 15px;
            font-weight: 800;
            margin-bottom: 4px;
        }

        .sidebar-card-sub {
            color: #94a3b8;
            font-size: 12px;
            line-height: 1.4;
        }

        .sidebar-sep {
            height: 1px;
            background: rgba(255,255,255,0.06);
            margin: 14px 0 14px 0;
            border-radius: 999px;
        }

        div[data-testid="stMetric"] {
            background: linear-gradient(180deg, #111827 0%, #0f172a 100%);
            border: 1px solid #1f2937;
            border-radius: 18px;
            padding: 12px 14px;
            box-shadow: 0 10px 24px rgba(0,0,0,0.18);
        }

        div[data-testid="stMetric"] label {
            color: #94a3b8 !important;
            font-weight: 700 !important;
        }

        div[data-testid="stMetricValue"] {
            color: #ffffff !important;
            font-weight: 900 !important;
        }

        button[role="tab"] {
            color: #e5e7eb !important;
            font-weight: 800 !important;
            font-size: 15px !important;
        }

        .terminal-box {
            background: linear-gradient(180deg, #0f172a 0%, #0d1524 100%);
            border: 1px solid #1e293b;
            border-radius: 22px;
            padding: 18px;
            height: 100%;
            box-shadow: 0 12px 28px rgba(0,0,0,0.22);
        }

        .section-title {
            font-size: 20px;
            font-weight: 900;
            color: #f8fafc;
            margin-bottom: 14px;
        }

        .subtle {
            color: #94a3b8;
            font-size: 13px;
        }

        .info-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 9px 0;
            border-bottom: 1px solid rgba(255,255,255,0.06);
            font-size: 14px;
        }

        .info-left {
            color: #cbd5e1;
            font-weight: 700;
        }

        .info-right {
            color: #ffffff;
            font-weight: 900;
            text-align: right;
        }

        .legend-item {
            display: flex;
            align-items: center;
            gap: 10px;
            color: #e5e7eb;
            font-size: 13px;
            font-weight: 700;
        }

        .dot {
            width: 14px;
            height: 14px;
            border-radius: 50%;
            display: inline-block;
        }

        .activity-item {
            border-bottom: 1px solid rgba(255,255,255,0.05);
            padding: 10px 0;
        }

        .activity-title {
            color: #f8fafc;
            font-weight: 800;
            font-size: 14px;
        }

        .activity-sub {
            color: #94a3b8;
            font-size: 13px;
            margin-top: 3px;
        }

        .trade-card {
            background: linear-gradient(180deg, #111827 0%, #0f172a 100%);
            border: 1px solid #1f2937;
            border-radius: 18px;
            padding: 16px;
            margin-bottom: 12px;
        }

        .trade-card-symbol {
            font-size: 22px;
            font-weight: 900;
            color: #f8fafc;
            line-height: 1.1;
        }

        .trade-card-meta {
            color: #94a3b8;
            font-size: 12px;
            margin-top: 6px;
            font-weight: 700;
        }

        .deal-row {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            border-bottom: 1px solid rgba(255,255,255,0.05);
            padding: 14px 4px;
        }

        .deal-left {
            width: 68%;
        }

        .deal-main {
            font-size: 20px;
            font-weight: 900;
            color: #f8fafc;
            line-height: 1.2;
        }

        .deal-sub {
            color: #cbd5e1;
            margin-top: 4px;
            font-size: 14px;
        }

        .deal-right {
            width: 32%;
            text-align: right;
        }

        .deal-dt {
            color: #94a3b8;
            font-size: 12px;
            margin-bottom: 8px;
        }

        .deal-pnl {
            font-size: 28px;
            font-weight: 900;
            line-height: 1;
        }

        .perf-card {
            background: linear-gradient(180deg, #111827 0%, #0f172a 100%);
            border: 1px solid #1f2937;
            border-left: 6px solid #5aa2ff;
            border-radius: 18px;
            padding: 18px 18px 14px 18px;
            min-height: 110px;
            box-shadow: 0 10px 22px rgba(0,0,0,0.18);
        }

        .perf-card-title {
            color: #94a3b8;
            font-size: 14px;
            font-weight: 700;
            margin-bottom: 10px;
        }

        .perf-card-value {
            font-size: 28px;
            font-weight: 900;
            line-height: 1.1;
        }

        .health-card {
            background: linear-gradient(180deg, #111827 0%, #0f172a 100%);
            border: 1px solid #1f2937;
            border-left: 6px solid #2ecc71;
            border-radius: 16px;
            padding: 14px 16px;
            margin-bottom: 10px;
        }

        .health-card-title {
            color: #94a3b8;
            font-size: 13px;
            font-weight: 700;
        }

        .health-card-value {
            color: #ffffff;
            font-size: 20px;
            font-weight: 900;
            margin-top: 6px;
        }

        div.stButton > button {
            width: 100%;
            border-radius: 14px !important;
            border: 1px solid rgba(255,255,255,0.08) !important;
            background: linear-gradient(180deg, #173052 0%, #132743 100%) !important;
            color: #ffffff !important;
            font-weight: 800 !important;
            padding-top: 10px !important;
            padding-bottom: 10px !important;
            box-shadow: 0 10px 20px rgba(0,0,0,0.16);
        }

        div.stButton > button:hover {
            border: 1px solid rgba(255,255,255,0.14) !important;
            background: linear-gradient(180deg, #1a3a63 0%, #15304f 100%) !important;
            color: #ffffff !important;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


# ==========================================================
# HELPERS
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


def outcome_color(outcome: str, trade_type: str = "REAL") -> str:
    o = safe_str(outcome).upper()
    trade_type = safe_str(trade_type).upper()

    if trade_type == "SHADOW":
        if o == "WIN":
            return "#2ecc71"
        if o == "LOSS":
            return "#ff8c42"
        if o in {"FLAT", "BREAKEVEN"}:
            return "#b0b7c3"
        return "#94a3b8"

    if o == "WIN":
        return "#5aa2ff"
    if o == "LOSS":
        return "#ff5a5f"
    if o in {"FLAT", "BREAKEVEN"}:
        return "#b0b7c3"
    return "#94a3b8"


def pnl_color(pnl: float, trade_type: str = "REAL") -> str:
    trade_type = safe_str(trade_type).upper()

    if trade_type == "SHADOW":
        if pnl > 0:
            return "#2ecc71"
        if pnl < 0:
            return "#ff8c42"
        return "#b0b7c3"

    if pnl > 0:
        return "#5aa2ff"
    if pnl < 0:
        return "#ff5a5f"
    return "#b0b7c3"


def render_perf_card(title: str, value: str, color: str):
    st.markdown(
        f"""
        <div class="perf-card" style="border-left-color:{color};">
            <div class="perf-card-title">{title}</div>
            <div class="perf-card-value" style="color:{color};">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_health_card(title: str, value: str, ok: bool = True):
    color = "#2ecc71" if ok else "#ff5a5f"
    st.markdown(
        f"""
        <div class="health-card" style="border-left-color:{color};">
            <div class="health-card-title">{title}</div>
            <div class="health-card-value" style="color:{color};">{value}</div>
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
# SNAPSHOT / BITVAVO
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
def fetch_all_market_prices() -> Dict[str, float]:
    url = f"{BASE_URL}/v2/ticker/price"
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    prices: Dict[str, float] = {}
    for row in data:
        market = row.get("market")
        price = row.get("price")
        if market and price is not None:
            try:
                prices[market] = float(price)
            except Exception:
                pass
    return prices


def price_in_eur(symbol: str, prices: Dict[str, float]) -> Tuple[Optional[float], str]:
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


def build_snapshot_with_eur_values() -> dict:
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
# DATABASE
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
        options=f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
    )


def run_df_query(sql: str, params: Optional[tuple] = None) -> pd.DataFrame:
    if not db_ready():
        return pd.DataFrame([])

    try:
        conn = get_db_conn()
        if conn is None:
            return pd.DataFrame([])

        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                rows = cur.fetchall()
                cols = [desc[0] for desc in cur.description]
        conn.close()
        return pd.DataFrame(rows, columns=cols)
    except Exception:
        return pd.DataFrame([])


@st.cache_data(ttl=20, show_spinner=False)
def table_count(table_name: str) -> int:
    df = run_df_query(f"SELECT COUNT(*) AS n FROM public.{table_name}")
    if df.empty or "n" not in df.columns:
        return 0
    return safe_int(df.iloc[0]["n"], 0)


@st.cache_data(ttl=20, show_spinner=False)
def load_pending_orders_db() -> pd.DataFrame:
    sql = f"""
        SELECT
            id,
            symbol,
            status,
            setup_type,
            regime,
            score,
            chance,
            confidence,
            entry,
            stop,
            target,
            timeframe,
            created_at,
            expires_at
        FROM public.pending_approvals
        WHERE COALESCE(status, 'PENDING') IN ('PENDING', 'APPROVED')
        ORDER BY COALESCE(chance, 0) DESC,
                 COALESCE(score, 0) DESC,
                 created_at DESC NULLS LAST
        LIMIT {PENDING_LIMIT}
    """
    df = run_df_query(sql)
    if df.empty:
        return df

    for col in ["score", "chance", "confidence", "entry", "stop", "target"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    if "created_at" in df.columns:
        df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce", utc=True)
    if "expires_at" in df.columns:
        df["expires_at"] = pd.to_datetime(df["expires_at"], errors="coerce", utc=True)

    return df


def normalize_trade_df(df: pd.DataFrame, trade_type: str) -> pd.DataFrame:
    if df.empty:
        return empty_trade_df()

    out = df.copy()

    numeric_cols = ["score", "raw_score", "chance", "confidence", "entry", "stop", "target", "result_r"]
    for col in numeric_cols:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)
        else:
            out[col] = 0.0

    out["created_at"] = pd.to_datetime(out.get("created_at"), errors="coerce", utc=True)
    out["closed_at"] = pd.to_datetime(out.get("closed_at"), errors="coerce", utc=True)
    out["datetime_raw"] = out["closed_at"].where(~out["closed_at"].isna(), out["created_at"])
    out["datetime"] = out["datetime_raw"].apply(format_dt_short)
    out["entry_price"] = out["entry"]
    out["exit_price"] = out["target"]
    out["pnl"] = out["result_r"]
    out["trade_type"] = trade_type
    out["outcome"] = out["outcome"].fillna("UNKNOWN").astype(str).str.upper()

    return out


@st.cache_data(ttl=20, show_spinner=False)
def load_real_trades_db() -> pd.DataFrame:
    sql = f"""
        SELECT
            id,
            symbol,
            setup_type,
            timeframe,
            regime,
            label,
            score,
            raw_score,
            chance,
            confidence,
            entry,
            stop,
            target,
            result_r,
            outcome,
            is_shadow,
            created_at,
            closed_at
        FROM public.experience_trades
        WHERE COALESCE(is_shadow, false) = false
        ORDER BY COALESCE(closed_at, created_at) DESC NULLS LAST
        LIMIT {REAL_LIMIT}
    """
    df = run_df_query(sql)
    return normalize_trade_df(df, "REAL")


@st.cache_data(ttl=20, show_spinner=False)
def load_shadow_trades_db() -> pd.DataFrame:
    sql = f"""
        SELECT
            id,
            symbol,
            setup_type,
            timeframe,
            regime,
            label,
            score,
            raw_score,
            chance,
            confidence,
            entry,
            stop,
            target,
            result_r,
            outcome,
            is_shadow,
            created_at,
            closed_at
        FROM public.experience_trades
        WHERE COALESCE(is_shadow, false) = true
        ORDER BY COALESCE(closed_at, created_at) DESC NULLS LAST
        LIMIT {SHADOW_LIMIT}
    """
    df = run_df_query(sql)
    return normalize_trade_df(df, "SHADOW")


@st.cache_data(ttl=20, show_spinner=False)
def load_scoreboard_db() -> pd.DataFrame:
    sql = f"""
        SELECT
            exchange,
            timeframe,
            setup_type,
            regime,
            n_total,
            n_win,
            n_loss,
            winrate,
            avg_r,
            expectancy,
            updated_at
        FROM public.experience_scoreboard
        ORDER BY COALESCE(n_total, 0) DESC NULLS LAST
        LIMIT {SCOREBOARD_LIMIT}
    """
    df = run_df_query(sql)
    if df.empty:
        return df

    for col in ["n_total", "n_win", "n_loss", "winrate", "avg_r", "expectancy"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    if "updated_at" in df.columns:
        df["updated_at"] = pd.to_datetime(df["updated_at"], errors="coerce", utc=True)
        df["updated_at"] = df["updated_at"].dt.strftime("%Y.%m.%d %H:%M:%S")

    return df


@st.cache_data(ttl=20, show_spinner=False)
def load_history_trades_db() -> pd.DataFrame:
    sql = f"""
        SELECT
            id,
            symbol,
            setup_type,
            timeframe,
            regime,
            label,
            score,
            raw_score,
            chance,
            confidence,
            entry,
            stop,
            target,
            result_r,
            outcome,
            is_shadow,
            created_at,
            closed_at
        FROM public.experience_trades
        ORDER BY COALESCE(closed_at, created_at) DESC NULLS LAST
        LIMIT {HISTORY_LIMIT}
    """
    df = run_df_query(sql)
    if df.empty:
        return empty_trade_df()

    df["trade_type"] = df["is_shadow"].apply(lambda x: "SHADOW" if bool(x) else "REAL")
    df = normalize_trade_df(df, "MIXED")
    if "is_shadow" in df.columns:
        df["trade_type"] = df["is_shadow"].apply(lambda x: "SHADOW" if bool(x) else "REAL")
    return df


# ==========================================================
# DATA BUILDERS
# ==========================================================
def build_activity_feed(
    orders_df: pd.DataFrame,
    real_df: pd.DataFrame,
    shadow_df: pd.DataFrame,
) -> List[Dict[str, Any]]:
    feed: List[Dict[str, Any]] = []

    if not orders_df.empty:
        for _, row in orders_df.head(5).iterrows():
            feed.append({
                "ts": row.get("created_at"),
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

    def _sort_key(item: Dict[str, Any]):
        ts = item.get("ts")
        if ts is None or pd.isna(ts):
            return pd.Timestamp("1970-01-01", tz="UTC")
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
                "shadow_loss": 0.0,
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
                "shadow_profit": max(pnl, 0.0),
                "shadow_loss": min(pnl, 0.0),
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

    for col in ["real_profit", "real_loss", "shadow_profit", "shadow_loss", "missed_good", "missed_bad"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0).cumsum()

    return df


def prepare_history_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()
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


def history_curve_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame([])

    rows = []
    for _, row in df.iterrows():
        ts = row.get("sort_ts")
        if pd.isna(ts):
            continue

        trade_type = safe_str(row.get("trade_type")).upper()
        pnl = safe_float(row.get("pnl"), 0.0)

        rows.append({
            "ts": ts,
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
# SIDEBAR
# ==========================================================
with st.sidebar:
    st.markdown(
        """
        <div class="sidebar-brand">
            <div class="sidebar-brand-title">Crypto AI Terminal</div>
            <div class="sidebar-brand-sub">
                Snel dashboard voor Pre-BUYs, echte trades, shadow trades en performance.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="sidebar-card">
            <div class="sidebar-card-title">Dashboard refresh</div>
            <div class="sidebar-card-sub">
                Herlaad alleen dashboarddata zonder zware extra calls.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.button("Dashboard vernieuwen"):
        st.cache_data.clear()
        st.rerun()

    st.markdown('<div class="sidebar-sep"></div>', unsafe_allow_html=True)

    st.markdown(
        """
        <div class="sidebar-card">
            <div class="sidebar-card-title">Snapshot vernieuwen</div>
            <div class="sidebar-card-sub">
                Alleen gebruiken als je echt een nieuwe Bitvavo snapshot wilt ophalen.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.button("Nieuwe snapshot ophalen"):
        try:
            build_snapshot_with_eur_values()
            st.session_state.last_snapshot_refresh_msg = "Snapshot succesvol vernieuwd."
            st.cache_data.clear()
            st.rerun()
        except Exception as e:
            st.session_state.last_snapshot_refresh_msg = f"Snapshot refresh mislukt: {e}"

    if st.session_state.last_snapshot_refresh_msg:
        st.caption(st.session_state.last_snapshot_refresh_msg)

    st.markdown('<div class="sidebar-sep"></div>', unsafe_allow_html=True)

    st.markdown(
        """
        <div class="sidebar-card">
            <div class="sidebar-card-title">Geschiedenis</div>
            <div class="sidebar-card-sub">
                Geschiedenis staat standaard uit voor snelle laadtijd. Zet hem pas aan als je hem nodig hebt.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not st.session_state.load_history:
        if st.button("Geschiedenis laden"):
            st.session_state.load_history = True
            st.rerun()
    else:
        if st.button("Geschiedenis uitzetten"):
            st.session_state.load_history = False
            st.rerun()


# ==========================================================
# HEADER
# ==========================================================
st.markdown("## Crypto AI Terminal")
status_placeholder = st.empty()
status_placeholder.caption("Dashboard wordt geladen...")


# ==========================================================
# LOAD DATA
# ==========================================================
snapshot, snapshot_state = read_snapshot_only()

orders_df = load_pending_orders_db()
real_df = load_real_trades_db()
shadow_df = load_shadow_trades_db()
scoreboard_df = load_scoreboard_db()

history_df = empty_trade_df()
if st.session_state.load_history:
    history_df = load_history_trades_db()

feed = build_activity_feed(orders_df, real_df, shadow_df)
chart_df = build_main_chart_df(real_df, shadow_df)

eur_available = safe_float((snapshot or {}).get("eur_available"), 0.0)
crypto_assets_eur = safe_float((snapshot or {}).get("crypto_assets_eur"), 0.0)
total_portfolio_eur = safe_float((snapshot or {}).get("total_portfolio_eur"), 0.0)

real_perf_df = real_df[
    real_df["outcome"].isin(["WIN", "LOSS", "FLAT", "BREAKEVEN"])
].copy() if not real_df.empty else empty_trade_df()

shadow_perf_df = shadow_df[
    shadow_df["outcome"].isin(["WIN", "LOSS", "FLAT", "BREAKEVEN"])
    & shadow_df["closed_at"].notna()
].copy() if not shadow_df.empty else empty_trade_df()

real_profit = float(pd.to_numeric(real_perf_df["pnl"], errors="coerce").fillna(0).sum()) if not real_perf_df.empty else 0.0
shadow_profit = float(pd.to_numeric(shadow_perf_df["pnl"], errors="coerce").fillna(0).sum()) if not shadow_perf_df.empty else 0.0

today_pnl = 0.0
if not real_perf_df.empty:
    tmp_real = real_perf_df.copy()
    tmp_real["date_only"] = pd.to_datetime(tmp_real["datetime_raw"], errors="coerce", utc=True).dt.date
    if not tmp_real["date_only"].isna().all():
        last_day = tmp_real["date_only"].dropna().max()
        today_pnl = float(
            pd.to_numeric(
                tmp_real.loc[tmp_real["date_only"] == last_day, "pnl"],
                errors="coerce"
            ).fillna(0).sum()
        )

pending_count = len(orders_df) if not orders_df.empty else 0
real_count = len(real_df) if not real_df.empty else 0
shadow_count = len(shadow_df) if not shadow_df.empty else 0

real_winrate = 0.0
if not real_perf_df.empty:
    wins_real = int((real_perf_df["pnl"] > 0).sum())
    real_winrate = (wins_real / len(real_perf_df) * 100.0) if len(real_perf_df) else 0.0

shadow_winrate = 0.0
if not shadow_perf_df.empty:
    wins_shadow = int((shadow_perf_df["pnl"] > 0).sum())
    shadow_winrate = (wins_shadow / len(shadow_perf_df) * 100.0) if len(shadow_perf_df) else 0.0

missed_good_count = int((shadow_perf_df["pnl"] > 0).sum()) if not shadow_perf_df.empty else 0
missed_bad_count = int((shadow_perf_df["pnl"] < 0).sum()) if not shadow_perf_df.empty else 0

status_text = f"Snapshot: {snapshot_state} | Postgres: {'OK' if db_ready() else 'MIST'} | Geschiedenis: {'AAN' if st.session_state.load_history else 'UIT'}"
status_placeholder.caption(status_text)


# ==========================================================
# TOP METRICS
# ==========================================================
m1, m2, m3, m4 = st.columns(4)
m1.metric("Balance", format_money(eur_available))
m2.metric("Crypto", format_money(crypto_assets_eur))
m3.metric("Total", format_money(total_portfolio_eur))
m4.metric("Today PnL", format_pnl(today_pnl))

m5, m6, m7, m8 = st.columns(4)
m5.metric("Pending Orders", str(pending_count))
m6.metric("Real Trades", str(real_count))
m7.metric("Shadow Trades", str(shadow_count))
m8.metric("Real Profit", format_pnl(real_profit))

st.divider()


# ==========================================================
# TOP LAYOUT
# ==========================================================
col_left, col_center, col_right = st.columns([0.9, 2.2, 0.9], gap="large")

with col_left:
    st.markdown('<div class="terminal-box">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Trade Panel</div>', unsafe_allow_html=True)

    if orders_df.empty:
        st.markdown('<div class="subtle">Geen pending Pre-BUY gevonden.</div>', unsafe_allow_html=True)
    else:
        best = orders_df.iloc[0]
        best_symbol = safe_str(best.get("symbol"), "-")
        best_setup = safe_str(best.get("setup_type"), "-")
        best_regime = safe_str(best.get("regime"), "-")
        best_chance = safe_int(best.get("chance"), 0)

        border_color = "#5aa2ff" if best_chance >= 85 else "#f59e0b"

        st.markdown(
            f"""
            <div class="trade-card" style="border-left:6px solid {border_color};">
                <div class="trade-card-symbol">{best_symbol}</div>
                <div class="trade-card-meta">{best_setup} • {best_regime}</div>
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

    lg1, lg2, lg3, lg4, lg5, lg6 = st.columns(6)
    lg1.markdown('<div class="legend-item"><span class="dot" style="background:#5aa2ff;"></span> Real winst</div>', unsafe_allow_html=True)
    lg2.markdown('<div class="legend-item"><span class="dot" style="background:#ff5a5f;"></span> Real verlies</div>', unsafe_allow_html=True)
    lg3.markdown('<div class="legend-item"><span class="dot" style="background:#2ecc71;"></span> Shadow winst</div>', unsafe_allow_html=True)
    lg4.markdown('<div class="legend-item"><span class="dot" style="background:#ff8c42;"></span> Shadow verlies</div>', unsafe_allow_html=True)
    lg5.markdown('<div class="legend-item"><span class="dot" style="background:#2ecc71;"></span> Gemist goed</div>', unsafe_allow_html=True)
    lg6.markdown('<div class="legend-item"><span class="dot" style="background:#ff8c42;"></span> Gemist slecht</div>', unsafe_allow_html=True)

    if chart_df.empty:
        st.info("Nog niet genoeg gesloten trade-data voor de hoofdgrafiek.")
    else:
        fig = go.Figure()

        fig.add_trace(go.Scatter(
            x=chart_df["ts"],
            y=chart_df["real_profit"],
            mode="lines",
            name="Real winst",
            line=dict(color="#5aa2ff", width=4),
        ))
        fig.add_trace(go.Scatter(
            x=chart_df["ts"],
            y=chart_df["real_loss"],
            mode="lines",
            name="Real verlies",
            line=dict(color="#ff5a5f", width=4),
        ))
        fig.add_trace(go.Scatter(
            x=chart_df["ts"],
            y=chart_df["shadow_profit"],
            mode="lines",
            name="Shadow winst",
            line=dict(color="#2ecc71", width=4),
        ))
        fig.add_trace(go.Scatter(
            x=chart_df["ts"],
            y=chart_df["shadow_loss"],
            mode="lines",
            name="Shadow verlies",
            line=dict(color="#ff8c42", width=4),
        ))
        fig.add_trace(go.Scatter(
            x=chart_df["ts"],
            y=chart_df["missed_good"],
            mode="lines",
            name="Gemiste trade goed",
            line=dict(color="#2ecc71", width=2, dash="dot"),
        ))
        fig.add_trace(go.Scatter(
            x=chart_df["ts"],
            y=chart_df["missed_bad"],
            mode="lines",
            name="Gemiste trade slecht",
            line=dict(color="#ff8c42", width=2, dash="dot"),
        ))

        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0f172a",
            plot_bgcolor="#0f172a",
            font=dict(color="#f8fafc"),
            height=560,
            margin=dict(l=10, r=10, t=10, b=10),
            showlegend=False,
            xaxis=dict(title="Tijd", gridcolor="rgba(255,255,255,0.07)"),
            yaxis=dict(title="Resultaat (R)", gridcolor="rgba(255,255,255,0.07)", zerolinecolor="rgba(255,255,255,0.14)"),
        )
        st.plotly_chart(fig, use_container_width=True)

    p1, p2, p3, p4 = st.columns(4)
    with p1:
        render_perf_card("Real Profit", format_pnl(real_profit), pnl_color(real_profit, "REAL"))
    with p2:
        render_perf_card("Shadow Profit", format_pnl(shadow_profit), pnl_color(shadow_profit, "SHADOW"))
    with p3:
        render_perf_card("Real Winrate", f"{real_winrate:.1f}%", "#5aa2ff")
    with p4:
        render_perf_card("Shadow Winrate", f"{shadow_winrate:.1f}%", "#2ecc71" if shadow_winrate >= 50 else "#ff8c42")

    st.markdown("</div>", unsafe_allow_html=True)

with col_right:
    st.markdown('<div class="terminal-box">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Bot Activity</div>', unsafe_allow_html=True)

    if not feed:
        st.markdown('<div class="subtle">Nog geen recente activity gevonden.</div>', unsafe_allow_html=True)
    else:
        for item in feed:
            kind = item.get("kind", "")
            sub_txt = safe_str(item.get("sub"), "").upper()

            if kind == "real":
                color = "#5aa2ff"
            elif kind == "shadow":
                if "WIN" in sub_txt:
                    color = "#2ecc71"
                elif "LOSS" in sub_txt:
                    color = "#ff8c42"
                else:
                    color = "#94a3b8"
            else:
                color = "#f59e0b"

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

    render_health_card("DATABASE_URL", "OK" if db_ready() else "MIST", db_ready())
    render_health_card("pending_approvals", str(table_count("pending_approvals")), table_count("pending_approvals") >= 0)
    render_health_card("experience_trades", str(table_count("experience_trades")), table_count("experience_trades") >= 0)
    render_health_card("experience_scoreboard", str(table_count("experience_scoreboard")), table_count("experience_scoreboard") >= 0)
    render_health_card("snapshot file", "OK" if file_meta(SNAPSHOT_PATH).get("exists") else "MIST", file_meta(SNAPSHOT_PATH).get("exists"))

    st.markdown("</div>", unsafe_allow_html=True)

st.divider()


# ==========================================================
# TABS
# ==========================================================
tabs = st.tabs([
    "Orders",
    "Deals",
    "Shadow Trades",
    "Performance",
    "Geschiedenis",
    "Settings",
])

with tabs[0]:
    st.subheader("Orders / Pending Pre-BUY")
    if orders_df.empty:
        st.info("Geen pending orders gevonden in Postgres.")
    else:
        show = orders_df.copy()
        if "created_at" in show.columns:
            show["created_at"] = show["created_at"].dt.strftime("%Y.%m.%d %H:%M:%S")
        if "expires_at" in show.columns:
            show["expires_at"] = show["expires_at"].dt.strftime("%Y.%m.%d %H:%M:%S")
        st.dataframe(show, use_container_width=True, hide_index=True)

with tabs[1]:
    st.subheader("Deals / Echte Trades")
    if real_df.empty:
        st.info("Nog geen echte trades gevonden.")
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
                            <span style="color:{outcome_color(outcome, 'REAL')};font-weight:900;"> {outcome}</span>
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

with tabs[2]:
    st.subheader("Shadow Trades / Niet genomen trades")
    if shadow_df.empty:
        st.info("Nog geen shadow trades gevonden.")
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
                            <span style="color:{outcome_color(outcome, 'SHADOW')};font-weight:900;"> {outcome}</span>
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

with tabs[3]:
    st.subheader("Performance")

    pp1, pp2, pp3, pp4 = st.columns(4)
    with pp1:
        render_perf_card("Real Profit", format_pnl(real_profit), pnl_color(real_profit, "REAL"))
    with pp2:
        render_perf_card("Shadow Profit", format_pnl(shadow_profit), pnl_color(shadow_profit, "SHADOW"))
    with pp3:
        render_perf_card("Missed Wins", str(missed_good_count), "#2ecc71")
    with pp4:
        render_perf_card("Missed Losses", str(missed_bad_count), "#ff8c42")

    st.markdown("<br>", unsafe_allow_html=True)

    if scoreboard_df.empty:
        st.info("Geen experience_scoreboard data gevonden.")
    else:
        st.markdown("#### Setup / Regime Scoreboard")
        st.dataframe(scoreboard_df, use_container_width=True, hide_index=True)

with tabs[4]:
    st.subheader("Geschiedenis")

    if not st.session_state.load_history:
        st.info("Geschiedenis staat uit voor snelle laadtijd. Zet hem links aan met 'Geschiedenis laden'.")
    else:
        hist_df = prepare_history_df(history_df)

        f1, f2, f3, f4 = st.columns(4)
        with f1:
            type_filter = st.selectbox("Type", ["ALLES", "REAL", "SHADOW"], index=0)
        with f2:
            outcome_opts = ["ALLES"] + sorted(hist_df["outcome"].dropna().astype(str).unique().tolist()) if not hist_df.empty else ["ALLES"]
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
            timeframe_opts = ["ALLES"] + sorted(hist_df["timeframe"].dropna().astype(str).unique().tolist()) if not hist_df.empty else ["ALLES"]
            timeframe_filter = st.selectbox("Timeframe", timeframe_opts, index=0)

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

        h1, h2, h3, h4 = st.columns(4)
        total_hist = len(filtered_hist)
        real_hist = int((filtered_hist["trade_type"] == "REAL").sum()) if not filtered_hist.empty else 0
        shadow_hist = int((filtered_hist["trade_type"] == "SHADOW").sum()) if not filtered_hist.empty else 0
        hist_r = float(pd.to_numeric(filtered_hist["pnl"], errors="coerce").fillna(0).sum()) if not filtered_hist.empty else 0.0

        with h1:
            render_perf_card("Totaal Trades", str(total_hist), "#94a3b8")
        with h2:
            render_perf_card("Real Trades", str(real_hist), "#5aa2ff")
        with h3:
            render_perf_card("Shadow Trades", str(shadow_hist), "#2ecc71")
        with h4:
            render_perf_card("Totaal Resultaat", f"{hist_r:+.2f} R", "#5aa2ff" if hist_r >= 0 else "#ff5a5f")

        st.markdown("<br>", unsafe_allow_html=True)

        curve_df = history_curve_df(filtered_hist)
        if curve_df.empty:
            st.info("Geen bruikbare geschiedenis-data voor grafieken.")
        else:
            fig_hist = go.Figure()
            fig_hist.add_trace(go.Scatter(
                x=curve_df["ts"],
                y=curve_df["real_profit"],
                mode="lines",
                name="Real winst",
                line=dict(color="#5aa2ff", width=4),
            ))
            fig_hist.add_trace(go.Scatter(
                x=curve_df["ts"],
                y=curve_df["real_loss"],
                mode="lines",
                name="Real verlies",
                line=dict(color="#ff5a5f", width=4),
            ))
            fig_hist.add_trace(go.Scatter(
                x=curve_df["ts"],
                y=curve_df["shadow_profit"],
                mode="lines",
                name="Shadow winst",
                line=dict(color="#2ecc71", width=4),
            ))
            fig_hist.add_trace(go.Scatter(
                x=curve_df["ts"],
                y=curve_df["shadow_loss"],
                mode="lines",
                name="Shadow verlies",
                line=dict(color="#ff8c42", width=4),
            ))
            fig_hist.update_layout(
                template="plotly_dark",
                paper_bgcolor="#0f172a",
                plot_bgcolor="#0f172a",
                font=dict(color="#f8fafc"),
                height=480,
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
                    ("UNKNOWN", "#94a3b8"),
                ]:
                    val = safe_int(outcome_counts.get(label, 0), 0)
                    if val > 0:
                        bar_fig.add_bar(x=[label], y=[val], marker_color=color, name=label)

                bar_fig.update_layout(
                    template="plotly_dark",
                    paper_bgcolor="#0f172a",
                    plot_bgcolor="#0f172a",
                    font=dict(color="#f8fafc"),
                    height=360,
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
                    height=360,
                    showlegend=False,
                    xaxis=dict(title="Coin"),
                    yaxis=dict(title="Resultaat (R)"),
                )
                st.plotly_chart(coin_fig, use_container_width=True)

        st.markdown("#### Volledige trade-geschiedenis")
        if filtered_hist.empty:
            st.info("Geen trades gevonden met deze filters.")
        else:
            show_cols = [
                "datetime",
                "symbol",
                "trade_type",
                "setup_type",
                "regime",
                "timeframe",
                "entry",
                "target",
                "pnl",
                "outcome",
                "label",
                "score",
                "chance",
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

with tabs[5]:
    st.subheader("Settings / Controle")

    rows = [
        {"item": "DATABASE_URL", "status": "OK" if db_ready() else "MIST"},
        {"item": "pending_approvals", "status": f"OK ({table_count('pending_approvals')})"},
        {"item": "experience_trades", "status": f"OK ({table_count('experience_trades')})"},
        {"item": "experience_scoreboard", "status": f"OK ({table_count('experience_scoreboard')})"},
        {"item": "snapshot file", "status": "OK" if file_meta(SNAPSHOT_PATH).get("exists") else "MIST"},
        {"item": "history mode", "status": "AAN" if st.session_state.load_history else "UIT"},
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.markdown("#### Snapshot preview")
    st.json(snapshot)
