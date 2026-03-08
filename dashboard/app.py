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

# Korte timeouts zodat dashboard nooit blijft hangen
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "4"))
DB_STATEMENT_TIMEOUT_MS = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "4000"))

# Snelle limits voor eerste load
PENDING_LIMIT = int(os.getenv("DASH_PENDING_LIMIT", "12"))
REAL_LIMIT = int(os.getenv("DASH_REAL_LIMIT", "80"))
SHADOW_LIMIT = int(os.getenv("DASH_SHADOW_LIMIT", "80"))
SCOREBOARD_LIMIT = int(os.getenv("DASH_SCOREBOARD_LIMIT", "25"))
HISTORY_LIMIT = int(os.getenv("DASH_HISTORY_LIMIT", "800"))


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


def format_pnl_eur(x: Any) -> str:
    v = safe_float(x, 0.0)
    sign = "+" if v > 0 else ""
    return f"{sign}€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_r(x: Any) -> str:
    v = safe_float(x, 0.0)
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f} R"


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
        if o in {"FLAT", "BREAKEVEN", "TIMEOUT"}:
            return "#b0b7c3"
        return "#94a3b8"

    if trade_type == "SIM":
        if o == "WIN":
            return "#a855f7"
        if o == "LOSS":
            return "#64748b"
        if o in {"FLAT", "BREAKEVEN", "TIMEOUT"}:
            return "#b0b7c3"
        return "#94a3b8"

    if o == "WIN":
        return "#5aa2ff"
    if o == "LOSS":
        return "#ff5a5f"
    if o in {"FLAT", "BREAKEVEN", "TIMEOUT"}:
        return "#b0b7c3"
    return "#94a3b8"


def pnl_color_r(pnl: float, trade_type: str = "REAL") -> str:
    trade_type = safe_str(trade_type).upper()

    if trade_type == "SHADOW":
        if pnl > 0:
            return "#2ecc71"
        if pnl < 0:
            return "#ff8c42"
        return "#b0b7c3"

    if trade_type == "SIM":
        if pnl > 0:
            return "#a855f7"
        if pnl < 0:
            return "#64748b"
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
        "trade_id",
        "symbol",
        "setup_type",
        "timeframe",
        "regime",
        "label",
        "score",
        "raw_score",
        "chance",
        "confidence",
        "entry",
        "stop",
        "target",
        "pnl_r",
        "outcome",
        "source",
        "trade_type",
        "is_shadow",
        "created_at",
        "closed_at",
        "datetime_raw",
        "datetime",
        "entry_price",
        "exit_price",
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
def get_table_columns(table_name: str) -> List[str]:
    sql = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        ORDER BY ordinal_position
    """
    df = run_df_query(sql, (table_name,))
    if df.empty or "column_name" not in df.columns:
        return []
    return [str(x) for x in df["column_name"].tolist()]


def has_columns(table_name: str) -> bool:
    return len(get_table_columns(table_name)) > 0


def sql_col(cols: List[str], name: str, cast: str = "text") -> str:
    if name in cols:
        return f'"{name}"'
    return f"NULL::{cast}"


def sql_bool_false() -> str:
    return "FALSE"


@st.cache_data(ttl=20, show_spinner=False)
def table_count(table_name: str) -> int:
    if not has_columns(table_name):
        return 0
    df = run_df_query(f"SELECT COUNT(*) AS n FROM public.{table_name}")
    if df.empty or "n" not in df.columns:
        return 0
    return safe_int(df.iloc[0]["n"], 0)


@st.cache_data(ttl=20, show_spinner=False)
def load_pending_orders_db() -> pd.DataFrame:
    if not has_columns("pending_approvals"):
        return pd.DataFrame([])

    cols = get_table_columns("pending_approvals")

    def c(name: str, cast: str = "text") -> str:
        return sql_col(cols, name, cast)

    sql = f"""
        SELECT
            {c("id")} AS id,
            {c("symbol")} AS symbol,
            {c("status")} AS status,
            {c("setup_type")} AS setup_type,
            {c("regime")} AS regime,
            {c("score", "double precision")} AS score,
            {c("chance", "double precision")} AS chance,
            {c("confidence", "double precision")} AS confidence,
            {c("entry", "double precision")} AS entry,
            {c("stop", "double precision")} AS stop,
            {c("target", "double precision")} AS target,
            {c("timeframe")} AS timeframe,
            {c("created_at", "timestamptz")} AS created_at,
            {c("expires_at", "timestamptz")} AS expires_at
        FROM public.pending_approvals
        WHERE COALESCE({c("status")}, 'PENDING') IN ('PENDING', 'APPROVED')
        ORDER BY COALESCE({c("chance", "double precision")}, 0) DESC,
                 COALESCE({c("score", "double precision")}, 0) DESC,
                 {c("created_at", "timestamptz")} DESC NULLS LAST
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


def build_experience_trades_sql(kind: str, limit: int) -> str:
    cols = get_table_columns("experience_trades")
    if not cols:
        return ""

    trade_id_expr = "NULL::text"
    if "trade_key" in cols and "id" in cols:
        trade_id_expr = 'COALESCE("trade_key"::text, "id"::text)'
    elif "trade_key" in cols:
        trade_id_expr = '"trade_key"::text'
    elif "id" in cols:
        trade_id_expr = '"id"::text'

    symbol_expr = "NULL::text"
    if "coin" in cols and "symbol" in cols:
        symbol_expr = 'COALESCE("coin", "symbol")'
    elif "coin" in cols:
        symbol_expr = '"coin"'
    elif "symbol" in cols:
        symbol_expr = '"symbol"'

    timeframe_expr = "NULL::text"
    if "entry_timeframe" in cols and "timeframe" in cols:
        timeframe_expr = 'COALESCE("entry_timeframe", "timeframe")'
    elif "entry_timeframe" in cols:
        timeframe_expr = '"entry_timeframe"'
    elif "timeframe" in cols:
        timeframe_expr = '"timeframe"'

    regime_expr = "NULL::text"
    if "market_regime" in cols and "regime" in cols:
        regime_expr = 'COALESCE("market_regime", "regime")'
    elif "market_regime" in cols:
        regime_expr = '"market_regime"'
    elif "regime" in cols:
        regime_expr = '"regime"'

    label_expr = "NULL::text"
    if "label" in cols and "grade" in cols:
        label_expr = 'COALESCE("label", "grade")'
    elif "label" in cols:
        label_expr = '"label"'
    elif "grade" in cols:
        label_expr = '"grade"'

    score_expr = "0::double precision"
    if "score" in cols and "bot_confidence" in cols:
        score_expr = 'COALESCE("score"::double precision, "bot_confidence"::double precision, 0)'
    elif "score" in cols:
        score_expr = 'COALESCE("score"::double precision, 0)'
    elif "bot_confidence" in cols:
        score_expr = 'COALESCE("bot_confidence"::double precision, 0)'

    raw_score_expr = "0::double precision"
    if "raw_score" in cols:
        raw_score_expr = 'COALESCE("raw_score"::double precision, 0)'
    elif "score" in cols:
        raw_score_expr = 'COALESCE("score"::double precision, 0)'
    elif "bot_confidence" in cols:
        raw_score_expr = 'COALESCE("bot_confidence"::double precision, 0)'

    chance_expr = "0::double precision"
    if "chance" in cols:
        chance_expr = 'COALESCE("chance"::double precision, 0)'

    confidence_expr = "0::double precision"
    if "confidence" in cols and "bot_confidence" in cols:
        confidence_expr = 'COALESCE("confidence"::double precision, "bot_confidence"::double precision, 0)'
    elif "confidence" in cols:
        confidence_expr = 'COALESCE("confidence"::double precision, 0)'
    elif "bot_confidence" in cols:
        confidence_expr = 'COALESCE("bot_confidence"::double precision, 0)'

    entry_expr = sql_col(cols, "entry", "double precision")
    stop_expr = sql_col(cols, "stop", "double precision")
    target_expr = sql_col(cols, "target", "double precision")

    outcome_expr = sql_col(cols, "outcome")
    created_expr = "NULL::timestamptz"
    if "created_at" in cols and "entry_time" in cols and "timestamp" in cols:
        created_expr = 'COALESCE("created_at", "entry_time", "timestamp")'
    elif "created_at" in cols and "entry_time" in cols:
        created_expr = 'COALESCE("created_at", "entry_time")'
    elif "created_at" in cols and "timestamp" in cols:
        created_expr = 'COALESCE("created_at", "timestamp")'
    elif "created_at" in cols:
        created_expr = '"created_at"'
    elif "entry_time" in cols:
        created_expr = '"entry_time"'
    elif "timestamp" in cols:
        created_expr = '"timestamp"'

    closed_expr = "NULL::timestamptz"
    if "closed_at" in cols and "exit_time" in cols:
        closed_expr = 'COALESCE("closed_at", "exit_time")'
    elif "closed_at" in cols:
        closed_expr = '"closed_at"'
    elif "exit_time" in cols:
        closed_expr = '"exit_time"'

    source_expr = "'UNKNOWN'::text"
    if "source" in cols and "is_shadow" in cols:
        source_expr = """
            COALESCE(
                UPPER("source"),
                CASE WHEN COALESCE("is_shadow", FALSE) THEN 'SHADOW' ELSE 'REAL' END
            )
        """
    elif "source" in cols:
        source_expr = 'COALESCE(UPPER("source"), \'UNKNOWN\')'
    elif "is_shadow" in cols:
        source_expr = "CASE WHEN COALESCE(\"is_shadow\", FALSE) THEN 'SHADOW' ELSE 'REAL' END"

    is_shadow_expr = "FALSE"
    if "is_shadow" in cols:
        is_shadow_expr = 'COALESCE("is_shadow", FALSE)'
    elif "source" in cols:
        is_shadow_expr = "CASE WHEN UPPER(COALESCE(\"source\", '')) = 'SHADOW' THEN TRUE ELSE FALSE END"

    pnl_expr = """
        CASE
            WHEN UPPER(COALESCE(outcome_calc, '')) = 'WIN' THEN 2.0
            WHEN UPPER(COALESCE(outcome_calc, '')) = 'LOSS' THEN -1.0
            ELSE 0.0
        END
    """
    if "result_r" in cols:
        pnl_expr = f"""
            COALESCE(
                "result_r"::double precision,
                CASE
                    WHEN UPPER(COALESCE(outcome_calc, '')) = 'WIN' THEN 2.0
                    WHEN UPPER(COALESCE(outcome_calc, '')) = 'LOSS' THEN -1.0
                    ELSE 0.0
                END
            )
        """

    where = "1=1"
    kind_u = kind.upper()

    if "source" in cols:
        if kind_u == "SIM":
            where = "UPPER(COALESCE(source_calc, '')) = 'SIM'"
        elif kind_u == "SHADOW":
            where = "UPPER(COALESCE(source_calc, '')) = 'SHADOW'"
        elif kind_u == "REAL":
            where = "UPPER(COALESCE(source_calc, '')) = 'REAL'"
    elif "is_shadow" in cols:
        if kind_u == "SHADOW":
            where = "COALESCE(is_shadow_calc, FALSE) = TRUE"
        elif kind_u == "REAL":
            where = "COALESCE(is_shadow_calc, FALSE) = FALSE"
        elif kind_u == "SIM":
            where = "1=0"

    sql = f"""
        WITH base AS (
            SELECT
                {trade_id_expr} AS trade_id,
                {symbol_expr} AS symbol,
                {sql_col(cols, "setup_type")} AS setup_type,
                {timeframe_expr} AS timeframe,
                {regime_expr} AS regime,
                {label_expr} AS label,
                {score_expr} AS score,
                {raw_score_expr} AS raw_score,
                {chance_expr} AS chance,
                {confidence_expr} AS confidence,
                {entry_expr} AS entry,
                {stop_expr} AS stop,
                {target_expr} AS target,
                {outcome_expr} AS outcome_calc,
                {source_expr} AS source_calc,
                {is_shadow_expr} AS is_shadow_calc,
                {created_expr} AS created_at,
                {closed_expr} AS closed_at
            FROM public.experience_trades
        )
        SELECT
            trade_id,
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
            {pnl_expr} AS pnl_r,
            COALESCE(outcome_calc, 'UNKNOWN') AS outcome,
            COALESCE(source_calc, 'UNKNOWN') AS source,
            CASE
                WHEN UPPER(COALESCE(source_calc, '')) = 'SIM' THEN 'SIM'
                WHEN UPPER(COALESCE(source_calc, '')) = 'SHADOW' THEN 'SHADOW'
                WHEN UPPER(COALESCE(source_calc, '')) = 'REAL' THEN 'REAL'
                WHEN COALESCE(is_shadow_calc, FALSE) THEN 'SHADOW'
                ELSE 'REAL'
            END AS trade_type,
            COALESCE(is_shadow_calc, FALSE) AS is_shadow,
            created_at,
            closed_at
        FROM base
        WHERE {where}
        ORDER BY COALESCE(closed_at, created_at) DESC NULLS LAST
        LIMIT {limit}
    """
    return sql


def normalize_trade_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return empty_trade_df()

    out = df.copy()

    numeric_cols = ["score", "raw_score", "chance", "confidence", "entry", "stop", "target", "pnl_r"]
    for col in numeric_cols:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)
        else:
            out[col] = 0.0

    for col in ["created_at", "closed_at"]:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors="coerce", utc=True)
        else:
            out[col] = pd.NaT

    out["datetime_raw"] = out["closed_at"].where(~out["closed_at"].isna(), out["created_at"])
    out["datetime"] = out["datetime_raw"].apply(format_dt_short)
    out["entry_price"] = out["entry"]
    out["exit_price"] = out["target"]
    out["trade_type"] = out["trade_type"].fillna("UNKNOWN").astype(str).str.upper()
    out["source"] = out["source"].fillna("UNKNOWN").astype(str).str.upper()
    out["outcome"] = out["outcome"].fillna("UNKNOWN").astype(str).str.upper()
    out["label"] = out["label"].fillna("-").astype(str)
    out["symbol"] = out["symbol"].fillna("-").astype(str)
    out["setup_type"] = out["setup_type"].fillna("-").astype(str)
    out["regime"] = out["regime"].fillna("-").astype(str)
    out["timeframe"] = out["timeframe"].fillna("-").astype(str)

    return out


@st.cache_data(ttl=20, show_spinner=False)
def load_real_trades_db() -> pd.DataFrame:
    sql = build_experience_trades_sql("REAL", REAL_LIMIT)
    if not sql:
        return empty_trade_df()
    df = run_df_query(sql)
    return normalize_trade_df(df)


@st.cache_data(ttl=20, show_spinner=False)
def load_shadow_trades_db() -> pd.DataFrame:
    sql = build_experience_trades_sql("SHADOW", SHADOW_LIMIT)
    if not sql:
        return empty_trade_df()
    df = run_df_query(sql)
    return normalize_trade_df(df)


@st.cache_data(ttl=20, show_spinner=False)
def load_sim_trades_db() -> pd.DataFrame:
    sql = build_experience_trades_sql("SIM", HISTORY_LIMIT)
    if not sql:
        return empty_trade_df()
    df = run_df_query(sql)
    return normalize_trade_df(df)


@st.cache_data(ttl=20, show_spinner=False)
def load_history_trades_db() -> pd.DataFrame:
    sql = build_experience_trades_sql("ALL", HISTORY_LIMIT)
    if not sql:
        return empty_trade_df()
    df = run_df_query(sql)
    return normalize_trade_df(df)


@st.cache_data(ttl=20, show_spinner=False)
def load_source_counts() -> pd.DataFrame:
    cols = get_table_columns("experience_trades")
    if not cols:
        return pd.DataFrame([])

    if "source" in cols:
        sql = """
            SELECT UPPER(COALESCE(source, 'UNKNOWN')) AS source, COUNT(*) AS n
            FROM public.experience_trades
            GROUP BY UPPER(COALESCE(source, 'UNKNOWN'))
            ORDER BY n DESC
        """
    elif "is_shadow" in cols:
        sql = """
            SELECT
                CASE WHEN COALESCE(is_shadow, FALSE) THEN 'SHADOW' ELSE 'REAL' END AS source,
                COUNT(*) AS n
            FROM public.experience_trades
            GROUP BY CASE WHEN COALESCE(is_shadow, FALSE) THEN 'SHADOW' ELSE 'REAL' END
            ORDER BY n DESC
        """
    else:
        return pd.DataFrame([])

    return run_df_query(sql)


@st.cache_data(ttl=20, show_spinner=False)
def load_scoreboard_db() -> pd.DataFrame:
    cols = get_table_columns("experience_scoreboard")
    if not cols:
        return pd.DataFrame([])

    def c(name: str, cast: str = "text") -> str:
        return sql_col(cols, name, cast)

    setup_expr = c("setup_type")
    regime_expr = "NULL::text"
    if "market_regime" in cols and "regime" in cols:
        regime_expr = 'COALESCE("market_regime", "regime")'
    elif "market_regime" in cols:
        regime_expr = '"market_regime"'
    elif "regime" in cols:
        regime_expr = '"regime"'

    grade_expr = c("grade")
    n_expr = "0::integer"
    if "n" in cols and "n_total" in cols:
        n_expr = 'COALESCE("n", "n_total", 0)'
    elif "n" in cols:
        n_expr = 'COALESCE("n", 0)'
    elif "n_total" in cols:
        n_expr = 'COALESCE("n_total", 0)'

    wins_expr = "0::integer"
    if "wins" in cols and "n_win" in cols:
        wins_expr = 'COALESCE("wins", "n_win", 0)'
    elif "wins" in cols:
        wins_expr = 'COALESCE("wins", 0)'
    elif "n_win" in cols:
        wins_expr = 'COALESCE("n_win", 0)'

    losses_expr = "0::integer"
    if "losses" in cols and "n_loss" in cols:
        losses_expr = 'COALESCE("losses", "n_loss", 0)'
    elif "losses" in cols:
        losses_expr = 'COALESCE("losses", 0)'
    elif "n_loss" in cols:
        losses_expr = 'COALESCE("n_loss", 0)'

    timeouts_expr = "0::integer"
    if "timeouts" in cols:
        timeouts_expr = 'COALESCE("timeouts", 0)'

    winrate_expr = "0::double precision"
    if "win_rate" in cols and "winrate" in cols:
        winrate_expr = 'COALESCE("win_rate"::double precision, "winrate"::double precision, 0)'
    elif "win_rate" in cols:
        winrate_expr = 'COALESCE("win_rate"::double precision, 0)'
    elif "winrate" in cols:
        winrate_expr = 'COALESCE("winrate"::double precision, 0)'

    avg_mfe_expr = 'COALESCE("avg_mfe"::double precision, 0)' if "avg_mfe" in cols else "0::double precision"
    avg_mae_expr = 'COALESCE("avg_mae"::double precision, 0)' if "avg_mae" in cols else "0::double precision"
    avg_time_expr = 'COALESCE("avg_time_minutes"::double precision, 0)' if "avg_time_minutes" in cols else "0::double precision"
    updated_expr = c("updated_at", "timestamptz")
    score_key_expr = c("score_key")

    sql = f"""
        SELECT
            {score_key_expr} AS score_key,
            {setup_expr} AS setup_type,
            {regime_expr} AS market_regime,
            {grade_expr} AS grade,
            {n_expr} AS n,
            {wins_expr} AS wins,
            {losses_expr} AS losses,
            {timeouts_expr} AS timeouts,
            {winrate_expr} AS win_rate,
            {avg_mfe_expr} AS avg_mfe,
            {avg_mae_expr} AS avg_mae,
            {avg_time_expr} AS avg_time_minutes,
            {updated_expr} AS updated_at
        FROM public.experience_scoreboard
        ORDER BY COALESCE({n_expr}, 0) DESC NULLS LAST
        LIMIT {SCOREBOARD_LIMIT}
    """
    df = run_df_query(sql)
    if df.empty:
        return df

    for col in ["n", "wins", "losses", "timeouts", "win_rate", "avg_mfe", "avg_mae", "avg_time_minutes"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    if "updated_at" in df.columns:
        df["updated_at"] = pd.to_datetime(df["updated_at"], errors="coerce", utc=True)
        df["updated_at"] = df["updated_at"].dt.strftime("%Y.%m.%d %H:%M:%S")

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
            pnl = safe_float(row.get("pnl_r"), 0.0)
            feed.append({
                "ts": row.get("datetime_raw"),
                "title": f"{safe_str(row.get('symbol'), '-')} REAL",
                "sub": f"Outcome {safe_str(row.get('outcome'), '-')} | {format_r(pnl)}",
                "kind": "real",
            })

    if not shadow_df.empty:
        for _, row in shadow_df.head(8).iterrows():
            pnl = safe_float(row.get("pnl_r"), 0.0)
            feed.append({
                "ts": row.get("datetime_raw"),
                "title": f"{safe_str(row.get('symbol'), '-')} SHADOW",
                "sub": f"Outcome {safe_str(row.get('outcome'), '-')} | {format_r(pnl)}",
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
            real_df["outcome"].isin(["WIN", "LOSS", "FLAT", "BREAKEVEN", "TIMEOUT"])
            & real_df["datetime_raw"].notna()
        ].copy()

        for _, row in real_closed.iterrows():
            ts = row.get("datetime_raw")
            pnl = safe_float(row.get("pnl_r"), 0.0)
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
            shadow_df["outcome"].isin(["WIN", "LOSS", "FLAT", "BREAKEVEN", "TIMEOUT"])
            & shadow_df["datetime_raw"].notna()
        ].copy()

        for _, row in shadow_closed.iterrows():
            ts = row.get("datetime_raw")
            pnl = safe_float(row.get("pnl_r"), 0.0)
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
    out["trade_type"] = out["trade_type"].fillna("UNKNOWN").astype(str).str.upper()
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
        pnl = safe_float(row.get("pnl_r"), 0.0)

        rows.append({
            "ts": ts,
            "real_profit": max(pnl, 0.0) if trade_type == "REAL" else 0.0,
            "real_loss": min(pnl, 0.0) if trade_type == "REAL" else 0.0,
            "shadow_profit": max(pnl, 0.0) if trade_type == "SHADOW" else 0.0,
            "shadow_loss": min(pnl, 0.0) if trade_type == "SHADOW" else 0.0,
            "sim_profit": max(pnl, 0.0) if trade_type == "SIM" else 0.0,
            "sim_loss": min(pnl, 0.0) if trade_type == "SIM" else 0.0,
        })

    out = pd.DataFrame(rows)
    if out.empty:
        return out

    out = out.sort_values("ts").reset_index(drop=True)
    for col in ["real_profit", "real_loss", "shadow_profit", "shadow_loss", "sim_profit", "sim_loss"]:
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0).cumsum()

    return out


def scoreboard_overview(scoreboard_df: pd.DataFrame) -> Dict[str, Any]:
    if scoreboard_df.empty:
        return {
            "best_label": "-",
            "best_win_rate": 0.0,
            "best_n": 0,
            "worst_label": "-",
            "worst_win_rate": 0.0,
            "worst_n": 0,
        }

    work = scoreboard_df.copy()
    work["n"] = pd.to_numeric(work["n"], errors="coerce").fillna(0)
    work["win_rate"] = pd.to_numeric(work["win_rate"], errors="coerce").fillna(0.0)

    filtered = work[work["n"] >= 50].copy()
    if filtered.empty:
        filtered = work.copy()

    filtered["combo"] = (
        filtered["setup_type"].astype(str)
        + " | "
        + filtered["market_regime"].astype(str)
        + " | "
        + filtered["grade"].astype(str)
    )

    best = filtered.sort_values(["win_rate", "n"], ascending=[False, False]).iloc[0]
    worst = filtered.sort_values(["win_rate", "n"], ascending=[True, False]).iloc[0]

    return {
        "best_label": safe_str(best.get("combo"), "-"),
        "best_win_rate": safe_float(best.get("win_rate"), 0.0),
        "best_n": safe_int(best.get("n"), 0),
        "worst_label": safe_str(worst.get("combo"), "-"),
        "worst_win_rate": safe_float(worst.get("win_rate"), 0.0),
        "worst_n": safe_int(worst.get("n"), 0),
    }


# ==========================================================
# SIDEBAR
# ==========================================================
with st.sidebar:
    st.markdown(
        """
        <div class="sidebar-brand">
            <div class="sidebar-brand-title">Crypto AI Terminal</div>
            <div class="sidebar-brand-sub">
                Snel dashboard voor Pre-BUYs, echte trades, shadow trades, simulaties en performance.
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
sim_df = load_sim_trades_db()
scoreboard_df = load_scoreboard_db()
source_counts_df = load_source_counts()

history_df = empty_trade_df()
if st.session_state.load_history:
    history_df = load_history_trades_db()

feed = build_activity_feed(orders_df, real_df, shadow_df)
chart_df = build_main_chart_df(real_df, shadow_df)

eur_available = safe_float((snapshot or {}).get("eur_available"), 0.0)
crypto_assets_eur = safe_float((snapshot or {}).get("crypto_assets_eur"), 0.0)
total_portfolio_eur = safe_float((snapshot or {}).get("total_portfolio_eur"), 0.0)

real_perf_df = real_df[
    real_df["outcome"].isin(["WIN", "LOSS", "FLAT", "BREAKEVEN", "TIMEOUT"])
].copy() if not real_df.empty else empty_trade_df()

shadow_perf_df = shadow_df[
    shadow_df["outcome"].isin(["WIN", "LOSS", "FLAT", "BREAKEVEN", "TIMEOUT"])
].copy() if not shadow_df.empty else empty_trade_df()

sim_perf_df = sim_df[
    sim_df["outcome"].isin(["WIN", "LOSS", "FLAT", "BREAKEVEN", "TIMEOUT"])
].copy() if not sim_df.empty else empty_trade_df()

real_profit_r = float(pd.to_numeric(real_perf_df["pnl_r"], errors="coerce").fillna(0).sum()) if not real_perf_df.empty else 0.0
shadow_profit_r = float(pd.to_numeric(shadow_perf_df["pnl_r"], errors="coerce").fillna(0).sum()) if not shadow_perf_df.empty else 0.0
sim_profit_r = float(pd.to_numeric(sim_perf_df["pnl_r"], errors="coerce").fillna(0).sum()) if not sim_perf_df.empty else 0.0

today_pnl_r = 0.0
if not real_perf_df.empty:
    tmp_real = real_perf_df.copy()
    tmp_real["date_only"] = pd.to_datetime(tmp_real["datetime_raw"], errors="coerce", utc=True).dt.date
    if not tmp_real["date_only"].isna().all():
        last_day = tmp_real["date_only"].dropna().max()
        today_pnl_r = float(
            pd.to_numeric(
                tmp_real.loc[tmp_real["date_only"] == last_day, "pnl_r"],
                errors="coerce"
            ).fillna(0).sum()
        )

pending_count = len(orders_df) if not orders_df.empty else 0
real_count = len(real_df) if not real_df.empty else 0
shadow_count = len(shadow_df) if not shadow_df.empty else 0
sim_count = len(sim_df) if not sim_df.empty else 0

real_winrate = 0.0
if not real_perf_df.empty:
    wins_real = int((real_perf_df["pnl_r"] > 0).sum())
    real_winrate = (wins_real / len(real_perf_df) * 100.0) if len(real_perf_df) else 0.0

shadow_winrate = 0.0
if not shadow_perf_df.empty:
    wins_shadow = int((shadow_perf_df["pnl_r"] > 0).sum())
    shadow_winrate = (wins_shadow / len(shadow_perf_df) * 100.0) if len(shadow_perf_df) else 0.0

sim_winrate = 0.0
if not sim_perf_df.empty:
    wins_sim = int((sim_perf_df["pnl_r"] > 0).sum())
    sim_winrate = (wins_sim / len(sim_perf_df) * 100.0) if len(sim_perf_df) else 0.0

missed_good_count = int((shadow_perf_df["pnl_r"] > 0).sum()) if not shadow_perf_df.empty else 0
missed_bad_count = int((shadow_perf_df["pnl_r"] < 0).sum()) if not shadow_perf_df.empty else 0

scoreboard_meta = scoreboard_overview(scoreboard_df)
experience_total_count = table_count("experience_trades")
experience_scoreboard_count = table_count("experience_scoreboard")

status_text = (
    f"Snapshot: {snapshot_state} | "
    f"Postgres: {'OK' if db_ready() else 'MIST'} | "
    f"Experience trades: {experience_total_count} | "
    f"Geschiedenis: {'AAN' if st.session_state.load_history else 'UIT'}"
)
status_placeholder.caption(status_text)


# ==========================================================
# TOP METRICS
# ==========================================================
m1, m2, m3, m4 = st.columns(4)
m1.metric("Balance", format_money(eur_available))
m2.metric("Crypto", format_money(crypto_assets_eur))
m3.metric("Total", format_money(total_portfolio_eur))
m4.metric("Today PnL", format_r(today_pnl_r))

m5, m6, m7, m8 = st.columns(4)
m5.metric("Pending Orders", str(pending_count))
m6.metric("Real Trades", str(real_count))
m7.metric("Shadow Trades", str(shadow_count))
m8.metric("SIM Trades", str(sim_count))

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
        st.info("Nog niet genoeg gesloten real/shadow trade-data voor de hoofdgrafiek.")
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
        render_perf_card("Real Profit", format_r(real_profit_r), pnl_color_r(real_profit_r, "REAL"))
    with p2:
        render_perf_card("Shadow Profit", format_r(shadow_profit_r), pnl_color_r(shadow_profit_r, "SHADOW"))
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
    render_health_card("experience_trades", str(table_count("experience_trades")), table_count("experience_trades") > 0)
    render_health_card("experience_scoreboard", str(table_count("experience_scoreboard")), table_count("experience_scoreboard") > 0)
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
            pnl = safe_float(row.get("pnl_r"), 0.0)
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
                        <div class="deal-pnl" style="color:{pnl_color_r(pnl, 'REAL')};">{format_r(pnl)}</div>
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
            pnl = safe_float(row.get("pnl_r"), 0.0)
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
                        <div class="deal-pnl" style="color:{pnl_color_r(pnl, 'SHADOW')};">{format_r(pnl)}</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

with tabs[3]:
    st.subheader("Performance")

    pp1, pp2, pp3, pp4 = st.columns(4)
    with pp1:
        render_perf_card("Real Profit", format_r(real_profit_r), pnl_color_r(real_profit_r, "REAL"))
    with pp2:
        render_perf_card("Shadow Profit", format_r(shadow_profit_r), pnl_color_r(shadow_profit_r, "SHADOW"))
    with pp3:
        render_perf_card("SIM Profit", format_r(sim_profit_r), pnl_color_r(sim_profit_r, "SIM"))
    with pp4:
        render_perf_card("Learning Winrate", f"{sim_winrate:.1f}%", "#a855f7")

    st.markdown("<br>", unsafe_allow_html=True)

    st.markdown("#### Experience Overview")
    eo1, eo2, eo3, eo4 = st.columns(4)
    with eo1:
        render_perf_card("Experience Trades", str(experience_total_count), "#94a3b8")
    with eo2:
        render_perf_card("Scoreboard Combinaties", str(experience_scoreboard_count), "#5aa2ff")
    with eo3:
        render_perf_card(
            "Beste Combo",
            f"{scoreboard_meta['best_win_rate']:.1f}%",
            "#2ecc71"
        )
        st.caption(f"{scoreboard_meta['best_label']} | n={scoreboard_meta['best_n']}")
    with eo4:
        render_perf_card(
            "Slechtste Combo",
            f"{scoreboard_meta['worst_win_rate']:.1f}%",
            "#ff8c42"
        )
        st.caption(f"{scoreboard_meta['worst_label']} | n={scoreboard_meta['worst_n']}")

    st.markdown("<br>", unsafe_allow_html=True)

    sc1, sc2 = st.columns([1.2, 1.0], gap="large")

    with sc1:
        st.markdown("#### Setup / Regime Scoreboard")
        if scoreboard_df.empty:
            st.info("Geen experience_scoreboard data gevonden.")
        else:
            show_scoreboard = scoreboard_df.copy()
            show_scoreboard = show_scoreboard.rename(columns={
                "setup_type": "Setup",
                "market_regime": "Regime",
                "grade": "Grade",
                "n": "Trades",
                "wins": "Wins",
                "losses": "Losses",
                "timeouts": "Timeouts",
                "win_rate": "Winrate %",
                "avg_mfe": "Avg MFE",
                "avg_mae": "Avg MAE",
                "avg_time_minutes": "Avg Tijd (min)",
                "updated_at": "Updated",
            })
            st.dataframe(show_scoreboard, use_container_width=True, hide_index=True)

    with sc2:
        st.markdown("#### Experience bronverdeling")
        if source_counts_df.empty:
            st.info("Geen bronverdeling gevonden.")
        else:
            pie_fig = go.Figure(
                data=[
                    go.Pie(
                        labels=source_counts_df["source"],
                        values=source_counts_df["n"],
                        hole=0.45,
                    )
                ]
            )
            pie_fig.update_layout(
                template="plotly_dark",
                paper_bgcolor="#0f172a",
                plot_bgcolor="#0f172a",
                font=dict(color="#f8fafc"),
                height=380,
                margin=dict(l=10, r=10, t=10, b=10),
                showlegend=True,
            )
            st.plotly_chart(pie_fig, use_container_width=True)

with tabs[4]:
    st.subheader("Geschiedenis")

    if not st.session_state.load_history:
        st.info("Geschiedenis staat uit voor snelle laadtijd. Zet hem links aan met 'Geschiedenis laden'.")
    else:
        hist_df = prepare_history_df(history_df)

        f1, f2, f3, f4 = st.columns(4)
        with f1:
            type_opts = ["ALLES"]
            if not hist_df.empty:
                type_opts += sorted(hist_df["trade_type"].dropna().astype(str).unique().tolist())
            type_filter = st.selectbox("Type", type_opts, index=0)

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
        sim_hist = int((filtered_hist["trade_type"] == "SIM").sum()) if not filtered_hist.empty else 0
        hist_r = float(pd.to_numeric(filtered_hist["pnl_r"], errors="coerce").fillna(0).sum()) if not filtered_hist.empty else 0.0

        with h1:
            render_perf_card("Totaal Trades", str(total_hist), "#94a3b8")
        with h2:
            render_perf_card("Real Trades", str(real_hist), "#5aa2ff")
        with h3:
            render_perf_card("Shadow Trades", str(shadow_hist), "#2ecc71")
        with h4:
            render_perf_card("SIM Trades", str(sim_hist), "#a855f7")

        st.markdown("<br>", unsafe_allow_html=True)

        sum1, sum2 = st.columns(2)
        with sum1:
            render_perf_card("Totaal Resultaat", format_r(hist_r), "#5aa2ff" if hist_r >= 0 else "#ff5a5f")
        with sum2:
            hist_winrate = 0.0
            if not filtered_hist.empty:
                closed_like = filtered_hist[filtered_hist["outcome"].isin(["WIN", "LOSS", "TIMEOUT", "FLAT", "BREAKEVEN"])]
                if not closed_like.empty:
                    hist_winrate = float((closed_like["pnl_r"] > 0).sum()) / float(len(closed_like)) * 100.0
            render_perf_card("Winrate", f"{hist_winrate:.1f}%", "#2ecc71" if hist_winrate >= 50 else "#ff8c42")

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
            fig_hist.add_trace(go.Scatter(
                x=curve_df["ts"],
                y=curve_df["sim_profit"],
                mode="lines",
                name="SIM winst",
                line=dict(color="#a855f7", width=4),
            ))
            fig_hist.add_trace(go.Scatter(
                x=curve_df["ts"],
                y=curve_df["sim_loss"],
                mode="lines",
                name="SIM verlies",
                line=dict(color="#64748b", width=4),
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
                    ("TIMEOUT", "#b0b7c3"),
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
                    filtered_hist.groupby("symbol", dropna=False)["pnl_r"]
                    .sum()
                    .reset_index()
                    .sort_values("pnl_r", ascending=False)
                    .head(15)
                )

                coin_fig = go.Figure()
                coin_fig.add_bar(
                    x=per_coin["symbol"],
                    y=per_coin["pnl_r"],
                    marker_color=["#5aa2ff" if v >= 0 else "#ff5a5f" for v in per_coin["pnl_r"]],
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
                "pnl_r",
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
                "pnl_r": "Resultaat (R)",
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
        {"item": "sources", "status": ", ".join([f"{safe_str(r['source'])}:{safe_int(r['n'])}" for _, r in source_counts_df.iterrows()]) if not source_counts_df.empty else "-"},
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.markdown("#### Snapshot preview")
    st.json(snapshot)
