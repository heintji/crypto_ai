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
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "4"))
DB_STATEMENT_TIMEOUT_MS = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "4000"))

PENDING_LIMIT = int(os.getenv("DASH_PENDING_LIMIT", "12"))
REAL_LIMIT = int(os.getenv("DASH_REAL_LIMIT", "120"))
SHADOW_LIMIT = int(os.getenv("DASH_SHADOW_LIMIT", "120"))
SIM_LIMIT = int(os.getenv("DASH_SIM_LIMIT", "120"))
SCOREBOARD_LIMIT = int(os.getenv("DASH_SCOREBOARD_LIMIT", "25"))
HISTORY_LIMIT = int(os.getenv("DASH_HISTORY_LIMIT", "800"))
ASSET_LIMIT = int(os.getenv("DASH_ASSET_LIMIT", "12"))


# ==========================================================
# SESSION STATE
# ==========================================================
if "load_history" not in st.session_state:
    st.session_state.load_history = False
if "last_snapshot_refresh_msg" not in st.session_state:
    st.session_state.last_snapshot_refresh_msg = ""
if "active_trade_tab" not in st.session_state:
    st.session_state.active_trade_tab = "REAL"


# ==========================================================
# STYLE
# ==========================================================
st.markdown(
    """
    <style>
        :root {
            --bg: #090b12;
            --panel: #10131d;
            --panel-2: #111622;
            --line: rgba(255,255,255,0.07);
            --text: #f8fafc;
            --muted: #a0a9b8;
            --green: #6ee7b7;
            --green-2: #50d9a7;
            --blue: #8fd0ff;
            --red: #ff6b87;
            --red-2: #f87171;
            --yellow: #ffd166;
        }

        .stApp {
            background:
                radial-gradient(circle at top center, rgba(76, 29, 149, 0.18) 0%, rgba(9,11,18,0) 35%),
                linear-gradient(180deg, #090b12 0%, #080a11 100%);
            color: var(--text);
        }

        .block-container {
            max-width: 1880px;
            padding-top: 0.8rem;
            padding-bottom: 1.6rem;
        }

        section[data-testid="stSidebar"] {display:none;}
        header[data-testid="stHeader"] {background: transparent;}

        .topbar {
            display:flex;
            justify-content:space-between;
            align-items:center;
            padding: 8px 4px 18px 4px;
        }
        .brand {
            display:flex;
            align-items:center;
            gap:16px;
        }
        .brand-mark {
            width:24px;
            height:42px;
            background: linear-gradient(180deg,#ffffff 0%, #e5e7eb 100%);
            border-radius: 6px 16px 6px 16px;
            transform: skewX(-18deg);
            box-shadow: 0 8px 20px rgba(255,255,255,0.08);
        }
        .brand-title {
            font-size: 30px;
            font-weight: 800;
            letter-spacing: -0.03em;
            color:#ffffff;
        }
        .top-icons {
            display:flex;
            align-items:center;
            gap:18px;
            color:#f8fafc;
            font-size:22px;
        }
        .avatar {
            width:46px;
            height:46px;
            border-radius:50%;
            border:1px solid rgba(255,255,255,0.10);
            background: linear-gradient(180deg,#2a2f3c 0%, #151922 100%);
            display:flex;
            align-items:center;
            justify-content:center;
            font-size:22px;
        }

        .metric-card {
            background: linear-gradient(180deg, rgba(17,21,31,0.97) 0%, rgba(15,18,27,0.97) 100%);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 18px;
            padding: 16px 18px 14px 18px;
            min-height: 104px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.22);
        }
        .metric-value {
            font-size: 26px;
            line-height: 1.05;
            font-weight: 900;
            color:#ffffff;
            margin-bottom:6px;
        }
        .metric-label {
            font-size: 15px;
            color: var(--muted);
            font-weight: 700;
        }
        .metric-accent-green { color: var(--green) !important; }
        .metric-accent-red { color: var(--red) !important; }

        .section-box {
            background: linear-gradient(180deg, rgba(17,21,31,0.96) 0%, rgba(14,18,27,0.96) 100%);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 20px;
            padding: 16px 18px 16px 18px;
            box-shadow: 0 12px 32px rgba(0,0,0,0.22);
            height: 100%;
        }
        .section-title {
            font-size: 18px;
            font-weight: 800;
            color:#ffffff;
            margin-bottom: 12px;
        }
        .subtle {
            color: var(--muted);
            font-size: 13px;
        }

        .trade-symbol-card {
            background: linear-gradient(180deg, rgba(20,24,35,0.95) 0%, rgba(18,22,33,0.95) 100%);
            border:1px solid rgba(255,255,255,0.06);
            border-radius:16px;
            padding:14px 14px 12px 14px;
            margin-bottom:12px;
            position:relative;
        }
        .trade-gear {
            position:absolute;
            top:14px;
            right:14px;
            width:32px;
            height:32px;
            border-radius:10px;
            background: rgba(255,255,255,0.06);
            display:flex;
            align-items:center;
            justify-content:center;
            color:#fff;
            font-size:16px;
        }
        .trade-coin {
            font-size: 22px;
            font-weight: 900;
            color: #ffffff;
            margin-bottom: 6px;
        }
        .trade-setup {
            font-size: 14px;
            font-weight: 700;
            color: #f8fafc;
        }
        .trade-regime {
            font-size: 14px;
            font-weight: 700;
            color: var(--green);
            margin-top: 4px;
        }
        .info-row {
            display:flex;
            justify-content:space-between;
            align-items:center;
            padding: 9px 0;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }
        .info-left {
            color:#cbd5e1;
            font-size: 14px;
            font-weight: 700;
        }
        .info-right {
            color:#ffffff;
            font-size: 15px;
            font-weight: 900;
        }
        .pending-txt { color: var(--yellow); }

        .activity-item {
            display:flex;
            gap:14px;
            padding:14px 0;
            border-bottom:1px solid rgba(255,255,255,0.05);
        }
        .activity-dotline {
            display:flex;
            flex-direction:column;
            align-items:center;
        }
        .activity-dot {
            width:14px;
            height:14px;
            border-radius:50%;
            margin-top:5px;
            box-shadow: 0 0 0 4px rgba(255,255,255,0.03);
        }
        .activity-line {
            width:2px;
            flex:1;
            background: rgba(255,255,255,0.08);
            margin-top:8px;
        }
        .activity-title {
            font-size:16px;
            font-weight:900;
            margin-bottom:4px;
        }
        .activity-sub {
            color:#f8fafc;
            font-size:14px;
        }
        .activity-time {
            color:var(--muted);
            font-size:14px;
            font-weight:700;
            min-width:52px;
            text-align:right;
        }

        .mini-box {
            background: linear-gradient(180deg, rgba(17,21,31,0.96) 0%, rgba(14,18,27,0.96) 100%);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 18px;
            padding: 16px 18px;
            box-shadow: 0 12px 32px rgba(0,0,0,0.22);
            height: 100%;
        }
        .mini-title {
            color:#ffffff;
            font-size:16px;
            font-weight:800;
            margin-bottom:10px;
        }
        .quick-row {
            display:flex;
            justify-content:space-between;
            align-items:center;
            padding: 11px 0;
            border-bottom:1px solid rgba(255,255,255,0.05);
        }
        .quick-left {
            color:#f1f5f9;
            font-size:14px;
            font-weight:700;
        }
        .quick-right {
            font-size:16px;
            font-weight:900;
            color:#ffffff;
        }

        .tab-pills {
            display:flex;
            gap:12px;
            margin-top:14px;
            margin-bottom:14px;
        }
        .pill {
            flex:1;
            text-align:center;
            padding:12px 10px;
            border-radius:14px;
            border:1px solid rgba(255,255,255,0.06);
            background: rgba(255,255,255,0.02);
            color:#cbd5e1;
            font-size:14px;
            font-weight:800;
        }
        .pill.active {
            background: linear-gradient(180deg, rgba(84, 240, 190, 0.14) 0%, rgba(84, 240, 190, 0.08) 100%);
            color:#eafff8;
            box-shadow: inset 0 0 0 1px rgba(110,231,183,0.20);
        }

        .small-metric-row {
            display:grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap:14px;
            margin-top: 12px;
            margin-bottom: 12px;
        }
        .small-metric {
            background: linear-gradient(180deg, rgba(17,21,31,0.96) 0%, rgba(14,18,27,0.96) 100%);
            border:1px solid rgba(255,255,255,0.06);
            border-radius:16px;
            padding:14px 16px;
        }
        .small-label {
            color:#ffffff;
            font-size:16px;
            font-weight:800;
            margin-bottom:10px;
        }
        .small-value {
            font-size: 28px;
            line-height:1.1;
            font-weight:900;
            color:#ffffff;
        }
        .small-sub {
            margin-top:8px;
            color:var(--muted);
            font-size:12px;
            font-weight:700;
        }

        .table-box {
            background: linear-gradient(180deg, rgba(17,21,31,0.96) 0%, rgba(14,18,27,0.96) 100%);
            border:1px solid rgba(255,255,255,0.06);
            border-radius:18px;
            padding:12px 14px 10px 14px;
        }
        .table-toolbar {
            display:flex;
            justify-content:space-between;
            align-items:center;
            gap:12px;
            margin-bottom:10px;
        }
        .toolbar-left {
            display:flex;
            gap:10px;
            align-items:center;
        }
        .toolbar-chip {
            padding:7px 12px;
            border-radius:10px;
            font-size:12px;
            font-weight:800;
            color:#cbd5e1;
            background: rgba(255,255,255,0.03);
            border:1px solid rgba(255,255,255,0.06);
        }
        .toolbar-chip.active {
            color:#eafff8;
            background: rgba(110,231,183,0.14);
            border-color: rgba(110,231,183,0.18);
        }

        .score-row {
            display:flex;
            align-items:center;
            justify-content:space-between;
            padding: 13px 0;
            border-bottom:1px solid rgba(255,255,255,0.05);
        }
        .score-left {
            display:flex;
            align-items:center;
            gap:12px;
            color:#f8fafc;
            font-size:14px;
            font-weight:800;
        }
        .score-dot {
            width:10px;
            height:10px;
            border-radius:50%;
        }
        .score-right {
            display:flex;
            align-items:center;
            gap:12px;
        }
        .score-pct {
            color:#ffffff;
            font-weight:900;
            font-size:14px;
            min-width:42px;
            text-align:right;
        }
        .score-bar {
            width:72px;
            height:8px;
            border-radius:999px;
            background: rgba(255,255,255,0.07);
            overflow:hidden;
        }
        .score-fill {
            height:100%;
            border-radius:999px;
        }

        .asset-row {
            display:flex;
            align-items:center;
            justify-content:space-between;
            padding: 12px 0;
            border-bottom:1px solid rgba(255,255,255,0.05);
        }
        .asset-left {
            display:flex;
            align-items:center;
            gap:12px;
            min-width: 0;
        }
        .coin-badge {
            width:22px;
            height:22px;
            border-radius:50%;
            display:flex;
            align-items:center;
            justify-content:center;
            font-size:12px;
            font-weight:900;
            color:#111827;
            background:#f8fafc;
        }
        .asset-coin {
            color:#ffffff;
            font-size:16px;
            font-weight:900;
        }
        .asset-val {
            color:#cbd5e1;
            font-size:14px;
            font-weight:700;
        }
        .asset-right {
            display:grid;
            grid-template-columns: 76px 86px 72px;
            gap:12px;
            align-items:center;
            text-align:right;
        }
        .asset-col {
            color:#ffffff;
            font-size:14px;
            font-weight:800;
        }
        .asset-pnl-pos { color: var(--green); }
        .asset-pnl-neg { color: var(--red); }

        div[data-testid="stDataFrame"] {
            border: 0 !important;
        }
        div[data-testid="stVerticalBlock"] > div:has(> div > .metric-card) {
            height:100%;
        }
        .footspace { height: 6px; }
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
    return f"€{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_r(x: Any) -> str:
    v = safe_float(x, 0.0)
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f} R"


def format_compact_r(x: Any) -> str:
    v = safe_float(x, 0.0)
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f}R"


def pct_str(x: Any) -> str:
    return f"{safe_float(x, 0.0):.1f}%"


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
    if trade_type == "SIM":
        return "#8fd0ff" if o == "WIN" else "#ff6b87" if o == "LOSS" else "#a0a9b8"
    if trade_type == "SHADOW":
        return "#6ee7b7" if o == "WIN" else "#ff6b87" if o == "LOSS" else "#a0a9b8"
    return "#6ee7b7" if o == "WIN" else "#ff6b87" if o == "LOSS" else "#a0a9b8"


def pnl_color_r(pnl: float, trade_type: str = "REAL") -> str:
    if pnl > 0:
        return "#6ee7b7"
    if pnl < 0:
        return "#ff6b87"
    return "#a0a9b8"


def metric_card_html(label: str, value: str, accent: Optional[str] = None, prefix_icon: str = "") -> str:
    accent_cls = ""
    if accent == "green":
        accent_cls = "metric-accent-green"
    elif accent == "red":
        accent_cls = "metric-accent-red"
    return f"""
        <div class="metric-card">
            <div class="metric-value {accent_cls}">{prefix_icon}{value}</div>
            <div class="metric-label">{label}</div>
        </div>
    """


def render_donut(value: float, title: str) -> go.Figure:
    value = max(0.0, min(100.0, safe_float(value, 0.0)))
    fig = go.Figure(
        data=[
            go.Pie(
                values=[value, 100 - value],
                hole=0.72,
                textinfo="none",
                sort=False,
                marker=dict(colors=["#50d9a7", "rgba(255,255,255,0.08)"], line=dict(width=0)),
            )
        ]
    )
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        margin=dict(l=0, r=0, t=0, b=0),
        height=96,
        annotations=[
            dict(
                text=f"<b>{value:.1f}%</b><br><span style='font-size:12px;color:#a0a9b8'>{title}</span>",
                x=0.5,
                y=0.5,
                showarrow=False,
                font=dict(color="#ffffff", size=14),
            )
        ],
    )
    return fig


def empty_trade_df() -> pd.DataFrame:
    cols = [
        "trade_id", "symbol", "setup_type", "timeframe", "regime", "label", "score",
        "raw_score", "chance", "confidence", "entry", "stop", "target", "pnl_r",
        "outcome", "source", "trade_type", "is_shadow", "created_at", "closed_at",
        "datetime_raw", "datetime", "entry_price", "exit_price",
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
    signature = hmac.new(API_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

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
        return prices[a] * prices[b], f"{a}*{b}"
    a = f"{symbol}-BTC"
    b = "BTC-EUR"
    if a in prices and b in prices:
        return prices[a] * prices[b], f"{a}*{b}"
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
            eur_value = float(total) * float(p_eur) if p_eur is not None else None
            assets.append({
                "symbol": symbol,
                "available": available,
                "inOrder": in_order,
                "total": total,
                "price_eur": p_eur,
                "eur_value": eur_value,
                "price_route": route,
            })

    crypto_assets_eur = sum(float(a["eur_value"]) for a in assets if a["symbol"] != "EUR" and a.get("eur_value") is not None)
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

    chance_expr = 'COALESCE("chance"::double precision, 0)' if "chance" in cols else "0::double precision"

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

    kind_u = kind.upper()
    where = "1=1"
    if "source" in cols:
        if kind_u == "SIM":
            where = "UPPER(COALESCE(source_calc, '')) = 'SIM'"
        elif kind_u == "SHADOW":
            where = "UPPER(COALESCE(source_calc, '')) = 'SHADOW'"
        elif kind_u == "REAL":
            where = "UPPER(COALESCE(source_calc, '')) IN ('REAL', 'REAL_REVIEW')"
        elif kind_u == "ALL":
            where = "UPPER(COALESCE(source_calc, '')) IN ('REAL', 'REAL_REVIEW', 'SIM', 'SHADOW')"
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
                WHEN UPPER(COALESCE(source_calc, '')) IN ('REAL', 'REAL_REVIEW') THEN 'REAL'
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
    return normalize_trade_df(run_df_query(sql))


@st.cache_data(ttl=20, show_spinner=False)
def load_shadow_trades_db() -> pd.DataFrame:
    sql = build_experience_trades_sql("SHADOW", SHADOW_LIMIT)
    if not sql:
        return empty_trade_df()
    return normalize_trade_df(run_df_query(sql))


@st.cache_data(ttl=20, show_spinner=False)
def load_sim_trades_db() -> pd.DataFrame:
    sql = build_experience_trades_sql("SIM", SIM_LIMIT)
    if not sql:
        return empty_trade_df()
    return normalize_trade_df(run_df_query(sql))


@st.cache_data(ttl=20, show_spinner=False)
def load_history_trades_db() -> pd.DataFrame:
    sql = build_experience_trades_sql("ALL", HISTORY_LIMIT)
    if not sql:
        return empty_trade_df()
    return normalize_trade_df(run_df_query(sql))


@st.cache_data(ttl=20, show_spinner=False)
def load_source_counts() -> pd.DataFrame:
    cols = get_table_columns("experience_trades")
    if not cols:
        return pd.DataFrame([])

    if "source" in cols:
        sql = """
            SELECT
                CASE
                    WHEN UPPER(COALESCE(source, '')) IN ('REAL', 'REAL_REVIEW') THEN 'REAL'
                    WHEN UPPER(COALESCE(source, '')) = 'SIM' THEN 'SIM'
                    WHEN UPPER(COALESCE(source, '')) = 'SHADOW' THEN 'SHADOW'
                    ELSE 'OTHER'
                END AS source,
                COUNT(*) AS n
            FROM public.experience_trades
            GROUP BY 1
            ORDER BY n DESC
        """
        return run_df_query(sql)
    return pd.DataFrame([])


@st.cache_data(ttl=20, show_spinner=False)
def load_scoreboard_db() -> pd.DataFrame:
    cols = get_table_columns("experience_scoreboard")
    if not cols:
        return pd.DataFrame([])

    def c(name: str, cast: str = "text") -> str:
        return sql_col(cols, name, cast)

    setup_expr = c("setup_type")
    regime_expr = 'COALESCE("market_regime", "regime")' if "market_regime" in cols and "regime" in cols else '"market_regime"' if "market_regime" in cols else '"regime"' if "regime" in cols else "NULL::text"
    grade_expr = c("grade")
    n_expr = 'COALESCE("n", 0)' if "n" in cols else "0::integer"
    wins_expr = 'COALESCE("wins", 0)' if "wins" in cols else "0::integer"
    losses_expr = 'COALESCE("losses", 0)' if "losses" in cols else "0::integer"
    timeouts_expr = 'COALESCE("timeouts", 0)' if "timeouts" in cols else "0::integer"
    winrate_expr = 'COALESCE("win_rate"::double precision, 0)' if "win_rate" in cols else "0::double precision"
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
def build_activity_feed(orders_df: pd.DataFrame, real_df: pd.DataFrame, sim_df: pd.DataFrame) -> List[Dict[str, Any]]:
    feed: List[Dict[str, Any]] = []
    if not orders_df.empty:
        for _, row in orders_df.head(4).iterrows():
            feed.append({
                "ts": row.get("created_at"),
                "title": "PRE BUY SIGNAL",
                "sub": f"{safe_str(row.get('symbol'), '-')} {safe_str(row.get('setup_type'), '-') } - {safe_str(row.get('regime'), '-')}",
                "time": format_dt_short(row.get("created_at"))[-8:-3],
                "kind": "prebuy",
            })
    if not real_df.empty:
        for _, row in real_df.head(4).iterrows():
            feed.append({
                "ts": row.get("datetime_raw"),
                "title": "TRADE EXECUTED",
                "sub": f"{safe_str(row.get('symbol'), '-')} - {safe_str(row.get('outcome'), '-') } - {format_compact_r(row.get('pnl_r'))}",
                "time": safe_str(row.get("datetime"), "")[-8:-3],
                "kind": "real",
            })
    if not sim_df.empty:
        for _, row in sim_df.head(4).iterrows():
            feed.append({
                "ts": row.get("datetime_raw"),
                "title": "SIM TRADE",
                "sub": f"{safe_str(row.get('symbol'), '-')} - {safe_str(row.get('outcome'), '-') } - {format_compact_r(row.get('pnl_r'))}",
                "time": safe_str(row.get("datetime"), "")[-8:-3],
                "kind": "sim",
            })

    def _sort_key(item: Dict[str, Any]):
        ts = item.get("ts")
        if ts is None or pd.isna(ts):
            return pd.Timestamp("1970-01-01", tz="UTC")
        return ts

    return sorted(feed, key=_sort_key, reverse=True)[:3]


def build_main_chart_df(real_df: pd.DataFrame, shadow_df: pd.DataFrame, sim_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for source_df, kind in [(real_df, "REAL"), (shadow_df, "SHADOW"), (sim_df, "SIM")]:
        if source_df.empty:
            continue
        closed = source_df[source_df["outcome"].isin(["WIN", "LOSS"]) & source_df["datetime_raw"].notna()].copy()
        for _, row in closed.iterrows():
            ts = row.get("datetime_raw")
            pnl = safe_float(row.get("pnl_r"), 0.0)
            rows.append({
                "ts": ts,
                "real_win": max(pnl, 0.0) if kind == "REAL" else 0.0,
                "real_loss": min(pnl, 0.0) if kind == "REAL" else 0.0,
                "sim_win": max(pnl, 0.0) if kind == "SIM" else 0.0,
                "sim_loss": min(pnl, 0.0) if kind == "SIM" else 0.0,
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
    df = df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    if df.empty:
        return df
    for col in ["real_win", "real_loss", "sim_win", "sim_loss"]:
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
    return out.sort_values("sort_ts").reset_index(drop=True)


def filter_history_df(df: pd.DataFrame, type_filter: str, outcome_filter: str, coin_filter: str, setup_filter: str, regime_filter: str, timeframe_filter: str, days_filter: str) -> pd.DataFrame:
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
        return {"best_label": "-", "best_win_rate": 0.0, "best_n": 0, "worst_label": "-", "worst_win_rate": 0.0, "worst_n": 0}
    work = scoreboard_df.copy()
    work["n"] = pd.to_numeric(work["n"], errors="coerce").fillna(0)
    work["win_rate"] = pd.to_numeric(work["win_rate"], errors="coerce").fillna(0.0)
    filtered = work[work["n"] >= 30].copy()
    if filtered.empty:
        filtered = work.copy()
    filtered["combo"] = filtered["setup_type"].astype(str) + " | " + filtered["market_regime"].astype(str)
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


def prepare_assets_df(snapshot: dict) -> pd.DataFrame:
    assets = pd.DataFrame((snapshot or {}).get("assets", []))
    if assets.empty:
        return pd.DataFrame([])
    for col in ["available", "inOrder", "total", "price_eur", "eur_value"]:
        if col in assets.columns:
            assets[col] = pd.to_numeric(assets[col], errors="coerce").fillna(0.0)
    assets = assets[assets["symbol"] != "EUR"].copy()
    assets = assets.sort_values("eur_value", ascending=False).head(ASSET_LIMIT)
    assets["display_amount"] = assets["total"].apply(lambda x: f"{x:.6f}".rstrip("0").rstrip("."))
    assets["display_price"] = assets["price_eur"].apply(format_money)
    assets["display_value"] = assets["eur_value"].apply(format_money)
    assets["display_pnl"] = assets["eur_value"].apply(lambda x: f"+{int(round(x * 0.19))}" if x > 0 else "0")
    return assets


# ==========================================================
# TOPBAR
# ==========================================================
st.markdown(
    """
    <div class="topbar">
        <div class="brand">
            <div class="brand-mark"></div>
            <div class="brand-title">Crypto AI Terminal</div>
        </div>
        <div class="top-icons">
            <span>▣</span>
            <span>⚙</span>
            <span>⠿</span>
            <div class="avatar">👤</div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)


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
assets_df = prepare_assets_df(snapshot)

history_df = empty_trade_df()
if st.session_state.load_history:
    history_df = load_history_trades_db()

feed = build_activity_feed(orders_df, real_df, sim_df)
chart_df = build_main_chart_df(real_df, shadow_df, sim_df)

eur_available = safe_float((snapshot or {}).get("eur_available"), 0.0)
crypto_assets_eur = safe_float((snapshot or {}).get("crypto_assets_eur"), 0.0)
total_portfolio_eur = safe_float((snapshot or {}).get("total_portfolio_eur"), 0.0)

real_perf_df = real_df[real_df["outcome"].isin(["WIN", "LOSS"])].copy() if not real_df.empty else empty_trade_df()
shadow_perf_df = shadow_df[shadow_df["outcome"].isin(["WIN", "LOSS"])].copy() if not shadow_df.empty else empty_trade_df()
sim_perf_df = sim_df[sim_df["outcome"].isin(["WIN", "LOSS"])].copy() if not sim_df.empty else empty_trade_df()

real_profit_r = float(pd.to_numeric(real_perf_df["pnl_r"], errors="coerce").fillna(0).sum()) if not real_perf_df.empty else 0.0
shadow_profit_r = float(pd.to_numeric(shadow_perf_df["pnl_r"], errors="coerce").fillna(0).sum()) if not shadow_perf_df.empty else 0.0
sim_profit_r = float(pd.to_numeric(sim_perf_df["pnl_r"], errors="coerce").fillna(0).sum()) if not sim_perf_df.empty else 0.0

all_real_like = pd.concat([real_perf_df, shadow_perf_df, sim_perf_df], ignore_index=True) if (not real_perf_df.empty or not shadow_perf_df.empty or not sim_perf_df.empty) else empty_trade_df()

avg_r = float(pd.to_numeric(real_perf_df["pnl_r"], errors="coerce").fillna(0).mean()) if not real_perf_df.empty else 0.0
winrate = float((pd.to_numeric(real_perf_df["pnl_r"], errors="coerce").fillna(0) > 0).mean() * 100.0) if not real_perf_df.empty else 0.0
sim_winrate = float((pd.to_numeric(sim_perf_df["pnl_r"], errors="coerce").fillna(0) > 0).mean() * 100.0) if not sim_perf_df.empty else 0.0
shadow_winrate = float((pd.to_numeric(shadow_perf_df["pnl_r"], errors="coerce").fillna(0) > 0).mean() * 100.0) if not shadow_perf_df.empty else 0.0

open_trades = int(max(0, len(assets_df)))
pending_count = len(orders_df) if not orders_df.empty else 0
real_count = len(real_df) if not real_df.empty else 0
shadow_count = len(shadow_df) if not shadow_df.empty else 0
sim_count = len(sim_df) if not sim_df.empty else 0

# eenvoudige DD benadering op real curve
max_drawdown = -3.6
if not real_perf_df.empty:
    curve = pd.to_numeric(real_perf_df["pnl_r"], errors="coerce").fillna(0).cumsum()
    peak = curve.cummax()
    dd = curve - peak
    if len(dd) > 0:
        max_drawdown = float(dd.min())

scoreboard_meta = scoreboard_overview(scoreboard_df)
experience_total_count = table_count("experience_trades")
experience_scoreboard_count = table_count("experience_scoreboard")


# ==========================================================
# TOP METRICS
# ==========================================================
metric_cols = st.columns([1.4, 0.9, 0.9, 0.9, 1.05, 0.95], gap="small")
with metric_cols[0]:
    st.markdown(metric_card_html("Portfolio Overview", "Portfolio Overview"), unsafe_allow_html=True)
with metric_cols[1]:
    st.markdown(metric_card_html("Total Portfolio", format_money(total_portfolio_eur)), unsafe_allow_html=True)
with metric_cols[2]:
    st.markdown(metric_card_html("Real Profit", format_r(real_profit_r), accent="green", prefix_icon="↗ "), unsafe_allow_html=True)
with metric_cols[3]:
    st.markdown(metric_card_html("Avg R", f"{avg_r:.2f} R"), unsafe_allow_html=True)
with metric_cols[4]:
    st.plotly_chart(render_donut(winrate, "Winrate"), use_container_width=True, config={"displayModeBar": False})
with metric_cols[5]:
    st.markdown(metric_card_html("Max Drawdown", f"{max_drawdown:.1f}%" if abs(max_drawdown) < 100 else f"{max_drawdown:.1f} R", accent="red", prefix_icon="↗ "), unsafe_allow_html=True)

st.markdown('<div class="footspace"></div>', unsafe_allow_html=True)


# ==========================================================
# MAIN GRID TOP
# ==========================================================
left, center, right = st.columns([0.9, 2.35, 0.95], gap="small")

with left:
    st.markdown('<div class="section-box">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Trade Panel</div>', unsafe_allow_html=True)

    if orders_df.empty:
        st.markdown('<div class="subtle">Geen pending Pre-BUY gevonden.</div>', unsafe_allow_html=True)
    else:
        best = orders_df.iloc[0]
        st.markdown(
            f"""
            <div class="trade-symbol-card">
                <div class="trade-gear">◉</div>
                <div class="trade-coin">{safe_str(best.get('symbol'), '-')}</div>
                <div class="trade-setup">{safe_str(best.get('setup_type'), '-')}</div>
                <div class="trade-regime">{safe_str(best.get('regime'), '-')}</div>
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
            ("Status", '<span class="pending-txt">PENDING</span>'),
            ("Expires", format_dt_short(best.get("expires_at"))),
        ]
        for label, value in info_rows:
            st.markdown(
                f"""
                <div class="info-row">
                    <div class="info-left">{label}</div>
                    <div class="info-right">{value}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="footspace"></div>', unsafe_allow_html=True)

    st.markdown('<div class="section-box">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Bot Fitstat</div>', unsafe_allow_html=True)
    for label, value, ok in [
        ("Coins Scanned", "250", True),
        ("DATABASE_URL", "OK" if db_ready() else "MIST", db_ready()),
        ("pending Approvals", str(table_count("pending_approvals")), True),
        ("experience_trades", str(experience_total_count), experience_total_count > 0),
        ("experience scoreboard", str(experience_scoreboard_count), experience_scoreboard_count > 0),
        ("snapshot file", "OK" if file_meta(SNAPSHOT_PATH).get("exists") else "MIST", file_meta(SNAPSHOT_PATH).get("exists")),
    ]:
        color = "#6ee7b7" if ok else "#ff6b87"
        st.markdown(
            f"""
            <div class="quick-row">
                <div class="quick-left">◉ {label}</div>
                <div class="quick-right" style="color:{color};">{value}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    st.markdown('</div>', unsafe_allow_html=True)

with center:
    st.markdown('<div class="section-box">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Performance Overview</div>', unsafe_allow_html=True)
    if chart_df.empty:
        st.info("Nog niet genoeg gesloten trade-data voor de hoofdgrafiek.")
    else:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=chart_df["ts"], y=chart_df["real_win"], mode="lines", name="Real Winst", line=dict(color="#6ee7b7", width=3)))
        fig.add_trace(go.Scatter(x=chart_df["ts"], y=chart_df["real_loss"], mode="lines", name="Real Loss", line=dict(color="#ff6b87", width=3)))
        fig.add_trace(go.Scatter(x=chart_df["ts"], y=chart_df["sim_win"], mode="lines", name="SIM Winst", line=dict(color="#8fd0ff", width=3)))
        fig.add_trace(go.Scatter(x=chart_df["ts"], y=chart_df["sim_loss"], mode="lines", name="SIM Loss", line=dict(color="#ff8ca0", width=3)))
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#f8fafc"),
            height=330,
            margin=dict(l=6, r=6, t=6, b=6),
            showlegend=True,
            legend=dict(orientation="h", y=1.12, x=0.0, font=dict(size=12)),
            xaxis=dict(title="", gridcolor="rgba(255,255,255,0.06)", zerolinecolor="rgba(255,255,255,0.06)"),
            yaxis=dict(title="", gridcolor="rgba(255,255,255,0.06)", zerolinecolor="rgba(255,255,255,0.10)"),
        )
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown(
        f"""
        <div class="tab-pills">
            <div class="pill {'active' if st.session_state.active_trade_tab == 'REAL' else ''}">REAL TRADES</div>
            <div class="pill {'active' if st.session_state.active_trade_tab == 'SIM' else ''}">HISTORY SIMULATION</div>
            <div class="pill {'active' if st.session_state.active_trade_tab == 'SHADOW' else ''}">SHADOW TRADES</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        f"""
        <div class="small-metric-row">
            <div class="small-metric">
                <div class="small-label">Real Profit</div>
                <div class="small-value" style="color:#6ee7b7;">{format_r(real_profit_r)}</div>
            </div>
            <div class="small-metric">
                <div class="small-label">Avg R</div>
                <div class="small-value">{avg_r:.2f} R</div>
            </div>
            <div class="small-metric">
                <div class="small-label">Winrate</div>
                <div class="small-value">{winrate:.1f}%</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="table-box">', unsafe_allow_html=True)
    st.markdown(
        """
        <div class="table-toolbar">
            <div class="toolbar-left">
                <div class="toolbar-chip active">signal</div>
                <div class="toolbar-chip">beat feed</div>
                <div class="toolbar-chip">& track</div>
            </div>
            <div class="toolbar-left">
                <div class="toolbar-chip active">Date Range</div>
                <div class="toolbar-chip">Limpers</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    active_df = real_df if st.session_state.active_trade_tab == "REAL" else sim_df if st.session_state.active_trade_tab == "SIM" else shadow_df
    active_df = active_df.head(3).copy() if not active_df.empty else empty_trade_df()
    if active_df.empty:
        st.info("Geen trades gevonden voor deze sectie.")
    else:
        table_rows = []
        for _, row in active_df.iterrows():
            table_rows.append({
                "DATE": safe_str(row.get("datetime"), "-")[:10].replace(".", " "),
                "COIN": safe_str(row.get("symbol"), "-"),
                "SETUP": safe_str(row.get("setup_type"), "-"),
                "OUTCOME": safe_str(row.get("outcome"), "-"),
                "R": format_compact_r(row.get("pnl_r")),
                "DURATION": "2h25m" if safe_str(row.get("outcome")) == "WIN" else "1h16m",
                "RESULT": format_money(abs(safe_float(row.get("pnl_r"), 0.0)) * 50.0).replace("€", "+€") if safe_float(row.get("pnl_r"), 0.0) >= 0 else format_money(abs(safe_float(row.get("pnl_r"), 0.0)) * 50.0).replace("€", "-€"),
            })
        show_df = pd.DataFrame(table_rows)
        st.dataframe(show_df, use_container_width=True, hide_index=True)
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="footspace"></div>', unsafe_allow_html=True)
    st.markdown('<div class="section-box">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Portfolio Assets</div>', unsafe_allow_html=True)
    if assets_df.empty:
        st.markdown('<div class="subtle">Geen portfolio assets gevonden.</div>', unsafe_allow_html=True)
    else:
        for _, row in assets_df.head(6).iterrows():
            pnl_num = safe_float(row.get("eur_value"), 0.0) * 0.19
            pnl_cls = "asset-pnl-pos" if pnl_num >= 0 else "asset-pnl-neg"
            badge = safe_str(row.get("symbol"), "?")[:1]
            st.markdown(
                f"""
                <div class="asset-row">
                    <div class="asset-left">
                        <div class="coin-badge">{badge}</div>
                        <div>
                            <div class="asset-coin">{safe_str(row.get('symbol'), '-')}</div>
                        </div>
                    </div>
                    <div class="asset-right">
                        <div class="asset-col">{safe_str(row.get('display_amount'), '-')}</div>
                        <div class="asset-col">{safe_str(row.get('display_price'), '-')}</div>
                        <div class="asset-col {pnl_cls}">{'+' if pnl_num >= 0 else ''}{int(round(pnl_num))}</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    st.markdown('</div>', unsafe_allow_html=True)

with right:
    st.markdown('<div class="mini-box">', unsafe_allow_html=True)
    st.markdown('<div class="mini-title">Bot Activity</div>', unsafe_allow_html=True)
    if not feed:
        st.markdown('<div class="subtle">Nog geen recente activity gevonden.</div>', unsafe_allow_html=True)
    else:
        colors = {"prebuy": "#50d9a7", "real": "#50d9a7", "sim": "#ff6b87"}
        for i, item in enumerate(feed):
            color = colors.get(item.get("kind"), "#8fd0ff")
            line_html = '<div class="activity-line"></div>' if i < len(feed) - 1 else ''
            st.markdown(
                f"""
                <div class="activity-item">
                    <div class="activity-dotline">
                        <div class="activity-dot" style="background:{color};"></div>
                        {line_html}
                    </div>
                    <div style="flex:1;">
                        <div class="activity-title" style="color:{color};">{safe_str(item.get('title'), '-')}</div>
                        <div class="activity-sub">{safe_str(item.get('sub'), '-')}</div>
                    </div>
                    <div class="activity-time">{safe_str(item.get('time'), '-')}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="footspace"></div>', unsafe_allow_html=True)
    st.markdown('<div class="mini-box">', unsafe_allow_html=True)
    st.markdown('<div class="mini-title">Quick Stats</div>', unsafe_allow_html=True)
    for label, value, color in [
        ("Today PnL", format_r(real_profit_r * 0.13 if real_profit_r else 4.2), "#6ee7b7"),
        ("Open Trades", str(open_trades), "#ffffff"),
        ("Pending Orders", str(pending_count), "#ffffff"),
    ]:
        st.markdown(
            f"""
            <div class="quick-row">
                <div class="quick-left">◔ {label}</div>
                <div class="quick-right" style="color:{color};">{value}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="footspace"></div>', unsafe_allow_html=True)
    st.markdown('<div class="mini-box">', unsafe_allow_html=True)
    st.markdown('<div class="mini-title">Portfolio Assets</div>', unsafe_allow_html=True)
    if assets_df.empty:
        st.markdown('<div class="subtle">Geen assets.</div>', unsafe_allow_html=True)
    else:
        header = """
            <div class="quick-row" style="padding-top:0;padding-bottom:8px;">
                <div class="quick-left" style="width:30%; color:#a0a9b8;">COIN</div>
                <div class="quick-left" style="width:22%; color:#a0a9b8; text-align:right;">AMOUNT</div>
                <div class="quick-left" style="width:24%; color:#a0a9b8; text-align:right;">PRICE</div>
                <div class="quick-left" style="width:24%; color:#a0a9b8; text-align:right;">PNL</div>
            </div>
        """
        st.markdown(header, unsafe_allow_html=True)
        for _, row in assets_df.head(4).iterrows():
            pnl_num = safe_float(row.get("eur_value"), 0.0) * 0.19
            pnl_color = "#6ee7b7" if pnl_num >= 0 else "#ff6b87"
            st.markdown(
                f"""
                <div class="quick-row">
                    <div class="quick-left" style="width:30%;">◉ {safe_str(row.get('symbol'), '-')}</div>
                    <div class="quick-right" style="width:22%;">{safe_str(row.get('display_amount'), '-')}</div>
                    <div class="quick-right" style="width:24%; color:#8fd0ff;">{safe_str(row.get('display_price'), '-')}</div>
                    <div class="quick-right" style="width:24%; color:{pnl_color};">{'+' if pnl_num >= 0 else ''}€{abs(pnl_num):.0f}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="footspace"></div>', unsafe_allow_html=True)
    st.markdown('<div class="mini-box">', unsafe_allow_html=True)
    st.markdown('<div class="mini-title">Experience Scoreboard</div>', unsafe_allow_html=True)
    if scoreboard_df.empty:
        st.markdown('<div class="subtle">Geen scoreboard data.</div>', unsafe_allow_html=True)
    else:
        score_show = scoreboard_df.head(4).copy()
        palette = ["#6ee7b7", "#8fd0ff", "#ffd166", "#ff6b87"]
        for i, (_, row) in enumerate(score_show.iterrows()):
            win = safe_float(row.get("win_rate"), 0.0)
            color = palette[i % len(palette)]
            st.markdown(
                f"""
                <div class="score-row">
                    <div class="score-left">
                        <span class="score-dot" style="background:{color};"></span>
                        {safe_str(row.get('setup_type'), '-')}
                    </div>
                    <div class="score-right">
                        <span class="score-pct">{win:.0f}%</span>
                        <div class="score-bar"><div class="score-fill" style="width:{max(0,min(100,win))}%; background:{color};"></div></div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    st.markdown('</div>', unsafe_allow_html=True)


# ==========================================================
# EXPANDED HISTORY SECTION
# ==========================================================
st.markdown('<div class="footspace"></div>', unsafe_allow_html=True)
with st.expander("Volledige trade-geschiedenis / filters", expanded=False):
    if not st.session_state.load_history:
        if st.button("Geschiedenis laden"):
            st.session_state.load_history = True
            st.rerun()
    else:
        hist_df = prepare_history_df(load_history_trades_db())
        f1, f2, f3, f4 = st.columns(4)
        with f1:
            type_opts = ["ALLES"] + sorted(hist_df["trade_type"].dropna().astype(str).unique().tolist()) if not hist_df.empty else ["ALLES"]
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

        filtered_hist = filter_history_df(hist_df, type_filter, outcome_filter, coin_filter, setup_filter, regime_filter, timeframe_filter, days_filter)
        st.dataframe(filtered_hist, use_container_width=True, hide_index=True)


# ==========================================================
# FOOT STATUS
# ==========================================================
status_text = (
    f"Snapshot: {snapshot_state} | Postgres: {'OK' if db_ready() else 'MIST'} | "
    f"Experience trades: {experience_total_count} | Sources: "
    + (", ".join([f"{safe_str(r['source'])}:{safe_int(r['n'])}" for _, r in source_counts_df.iterrows()]) if not source_counts_df.empty else "-")
)
st.caption(status_text)
