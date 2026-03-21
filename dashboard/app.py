
"""
Crypto AI Terminal
Uitgebreide, lange en expliciete versie met:

- werkend linkermenu met klikbare navigatie
- duidelijke page-routing op basis van session_state
- data-pipeline per pagina
- grafieken die altijd proberen te werken
- fallback demo-data als database leeg of onbereikbaar is
- portfolio snapshot met Bitvavo of veilige fallback
- filters, zoeken, tabellen, debug, uitleg
- dark UI zonder witte knoppen of ruwe HTML blokken

Belangrijk ontwerp:
1. Dashboard gebruikt gecombineerde history-data
2. Live Performance gebruikt alleen REAL trades
3. Simulator gebruikt alleen SIM trades
4. Shadow Review gebruikt alleen SHADOW trades
5. Portfolio gebruikt alleen snapshot/assets data
6. Help legt de koppelingen en logica uit

Pijplijn:
LOAD -> NORMALIZE -> SELECTEER DATASET -> FILTER -> VISUALISEER -> UITLEG/DEBUG
"""

from __future__ import annotations

import os
import json
import time
import hmac
import hashlib
import math
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
    initial_sidebar_state="collapsed",
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

REAL_LIMIT = int(os.getenv("DASH_REAL_LIMIT", "400"))
SIM_LIMIT = int(os.getenv("DASH_SIM_LIMIT", "400"))
SHADOW_LIMIT = int(os.getenv("DASH_SHADOW_LIMIT", "400"))
HISTORY_LIMIT = int(os.getenv("DASH_HISTORY_LIMIT", "100000"))
PENDING_LIMIT = int(os.getenv("DASH_PENDING_LIMIT", "1000"))
SCOREBOARD_LIMIT = int(os.getenv("DASH_SCOREBOARD_LIMIT", "500"))
ASSET_LIMIT = int(os.getenv("DASH_ASSET_LIMIT", "20"))


# ==========================================================
# SESSION STATE
# ==========================================================
SESSION_DEFAULTS = {
    "page": "dashboard",
    "selected_trade_id": None,
    "selected_page_trade_id": None,
    "status_notice": "",
    "load_history": True,
    "show_debug": False,
    "show_help_expanded": False,
    "search_text": "",
    "global_days_filter": "ALLES",
    "global_trade_type_filter": "ALLES",
    "global_setup_filter": "ALLES",
    "global_regime_filter": "ALLES",
    "global_outcome_filter": "ALLES",
    "global_symbol_filter": "ALLES",
    "portfolio_search": "",
    "last_error_text": "",
    "debug_events": [],
}
for key, value in SESSION_DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value


# ==========================================================
# STYLE
# ==========================================================
st.markdown(
    """
    <style>
        :root {
            --bg: #050914;
            --bg2: #0a1020;
            --panel: #0d1528;
            --panel2: #101b31;
            --line: rgba(255,255,255,0.08);
            --text: #f8fafc;
            --muted: #94a3b8;
            --blue: #60a5fa;
            --cyan: #67e8f9;
            --green: #6ee7b7;
            --green2: #34d399;
            --yellow: #fbbf24;
            --red: #fb7185;
            --purple: #c084fc;
        }

        .stApp {
            background:
                radial-gradient(circle at top center, rgba(76,29,149,0.18) 0%, rgba(4,10,20,0) 30%),
                linear-gradient(180deg, #050914 0%, #030712 100%);
            color: var(--text);
        }

        header[data-testid="stHeader"] { background: transparent; }
        section[data-testid="stSidebar"] { display: none; }

        .block-container {
            max-width: 1880px;
            padding-top: 0.6rem;
            padding-bottom: 1.1rem;
        }

        .shell {
            border-radius: 28px;
            border: 1px solid rgba(255,255,255,0.08);
            background: linear-gradient(180deg, rgba(9,13,25,0.96), rgba(6,10,20,0.96));
            box-shadow: 0 24px 60px rgba(0,0,0,0.35);
            padding: 18px;
        }

        .topbar {
            display:flex;
            justify-content:space-between;
            align-items:center;
            gap:16px;
            padding: 4px 2px 14px 2px;
        }

        .brand {
            display:flex;
            align-items:center;
            gap:16px;
        }

        .brand-mark {
            width:20px;
            height:42px;
            border-radius: 8px 18px 8px 18px;
            background: linear-gradient(180deg,#ffffff 0%, #e5e7eb 100%);
            transform: skewX(-18deg);
            box-shadow: 0 8px 22px rgba(255,255,255,0.10);
        }

        .brand-title {
            font-size: 25px;
            line-height: 1.05;
            font-weight: 900;
            letter-spacing: -0.03em;
            color: #ffffff;
        }

        .top-icons {
            display:flex;
            align-items:center;
            gap:10px;
        }

        .top-icon-chip {
            display:inline-flex;
            align-items:center;
            justify-content:center;
            width: 34px;
            height: 34px;
            border-radius: 12px;
            background: rgba(255,255,255,0.04);
            border:1px solid rgba(255,255,255,0.08);
            color:#f8fafc;
            font-size: 14px;
            font-weight: 800;
        }

        .avatar {
            width: 38px;
            height: 38px;
            border-radius: 999px;
            background: linear-gradient(180deg,#20283b 0%, #141b2c 100%);
            border:1px solid rgba(255,255,255,0.10);
            display:flex;
            align-items:center;
            justify-content:center;
            font-size:14px;
        }

        .panel, .panel-tight {
            background: linear-gradient(180deg, rgba(15,22,40,0.96), rgba(9,15,28,0.96));
            border:1px solid rgba(255,255,255,0.07);
            border-radius: 22px;
            box-shadow: 0 12px 34px rgba(0,0,0,0.24);
        }

        .panel { padding: 14px; }
        .panel-tight { padding: 10px 12px; }

        .metric-card {
            background: linear-gradient(180deg, rgba(14,22,40,0.98), rgba(10,16,30,0.98));
            border:1px solid rgba(255,255,255,0.08);
            border-radius: 18px;
            padding: 16px;
            min-height: 96px;
        }

        .metric-value {
            color:#ffffff;
            font-size: 24px;
            line-height: 1.05;
            font-weight: 900;
            margin-bottom: 6px;
        }

        .metric-label {
            color: var(--muted);
            font-size: 13px;
            font-weight: 700;
        }

        .accent-blue { color: var(--blue) !important; }
        .accent-green { color: var(--green) !important; }
        .accent-red { color: var(--red) !important; }
        .accent-purple { color: var(--purple) !important; }
        .accent-yellow { color: var(--yellow) !important; }

        .section-title {
            color:#ffffff;
            font-size: 22px;
            font-weight: 900;
            margin-bottom: 8px;
        }

        .section-subtitle {
            color: var(--muted);
            font-size: 13px;
            line-height: 1.6;
            margin-bottom: 12px;
        }

        .divider {
            height:1px;
            background: rgba(255,255,255,0.07);
            border-radius:999px;
            margin: 10px 0 12px 0;
        }

        .nav-header {
            color:#ffffff;
            font-size:13px;
            font-weight:900;
            text-transform:uppercase;
            margin-bottom: 10px;
            letter-spacing: 0.02em;
        }

        .nav-caption {
            color: var(--muted);
            font-size: 12px;
            line-height: 1.5;
            margin-bottom: 12px;
        }

        
        .top-hero-grid {
            display:grid;
            grid-template-columns: 0.92fr 1.9fr 1.0fr;
            gap: 18px;
            align-items: stretch;
            margin-top: 10px;
            margin-bottom: 18px;
        }
        .top-left-stack {
            display:flex;
            flex-direction:column;
            gap:16px;
            height:100%;
        }
        .top-mini-card, .top-main-card, .top-stats-panel {
            background:
                radial-gradient(circle at top center, rgba(83,19,80,0.18), rgba(0,0,0,0) 42%),
                linear-gradient(180deg, rgba(10,13,22,0.98), rgba(8,11,18,0.98));
            border:1px solid rgba(255,255,255,0.07);
            border-radius:22px;
            box-shadow: 0 18px 45px rgba(0,0,0,0.28);
            position: relative;
            overflow: hidden;
        }
        .top-mini-card::before, .top-main-card::before, .top-stats-panel::before {
            content:'';
            position:absolute;
            inset:0;
            border-radius:22px;
            pointer-events:none;
            box-shadow: inset 0 1px 0 rgba(255,255,255,0.03);
        }
        .top-mini-card { padding: 18px; min-height: 254px; }
        .top-main-card { padding: 20px 20px 10px 20px; min-height: 520px; }
        .top-main-card::after {
            content:'';
            position:absolute;
            left:10%;
            right:10%;
            top:8%;
            height:110px;
            border-radius:999px;
            background: radial-gradient(circle, rgba(239,68,68,0.18) 0%, rgba(239,68,68,0.06) 32%, rgba(0,0,0,0) 72%);
            filter: blur(16px);
            pointer-events:none;
        }
        .top-stats-panel { padding: 18px; min-height: 520px; }
        .mini-title {
            font-size: 19px;
            font-weight: 900;
            margin-bottom: 10px;
            color:#ffffff;
        }
        .mini-title .green { color:#34d399; }
        .mini-title .red { color:#ef4444; }
        .top-main-caption {
            font-size: 34px;
            line-height: 1;
            font-weight: 900;
            color:#ffffff;
            margin-bottom:8px;
        }
        .top-main-kicker {
            font-size: 18px;
            color:#ffffff;
            margin-bottom: 10px;
        }
        .top-legend {
            display:flex;
            align-items:center;
            gap:28px;
            justify-content:center;
            margin-top: -8px;
            margin-bottom: 8px;
            color:#ffffff;
            font-size:15px;
            font-weight:800;
        }
        .top-legend-dot {
            display:inline-block;
            width:12px;
            height:12px;
            border-radius:50%;
            margin-right:8px;
            box-shadow: 0 0 12px rgba(255,255,255,0.18);
        }
        .dominance-pill {
            display:inline-flex;
            align-items:center;
            gap:8px;
            padding:10px 16px;
            border-radius:999px;
            font-size:14px;
            font-weight:900;
            letter-spacing:0.02em;
            margin-bottom:16px;
            border:1px solid rgba(255,255,255,0.10);
            width: fit-content;
        }
        .dominance-pill.red {
            color:#ffffff;
            background: linear-gradient(90deg, rgba(127, 29, 29, 0.35), rgba(239,68,68,0.12));
            box-shadow: 0 0 24px rgba(239,68,68,0.18);
        }
        .dominance-pill.green {
            color:#ffffff;
            background: linear-gradient(90deg, rgba(6, 78, 59, 0.35), rgba(52,211,153,0.12));
            box-shadow: 0 0 24px rgba(52,211,153,0.18);
        }

        .mini-kicker {
            font-size: 13px;
            color:#cbd5e1;
            margin-top:-4px;
            margin-bottom:8px;
        }
        .stats-row {
            display:flex;
            justify-content:space-between;
            align-items:center;
            padding: 11px 0;
            border-bottom:1px solid rgba(255,255,255,0.06);
        }
        .stats-label {
            color:#e2e8f0;
            font-size:15px;
            font-weight:700;
        }
        .stats-value {
            color:#ffffff;
            font-size:15px;
            font-weight:900;
            text-align:right;
        }
        .metric-badge {
            display:inline-flex;
            margin-left:10px;
            padding:4px 9px;
            border-radius:999px;
            background: rgba(255,255,255,0.08);
            font-size: 12px;
            font-weight: 900;
            color:#f8fafc;
        }
        .top-legend {
            display:flex;
            align-items:center;
            justify-content:center;
            gap:26px;
            margin-top:8px;
            margin-bottom:4px;
            color:#f8fafc;
            font-size:14px;
            font-weight:800;
        }
        .top-legend-dot {
            display:inline-block;
            width:12px;
            height:12px;
            border-radius:999px;
            margin-right:8px;
        }
        .dominance-pill {
            display:inline-flex;
            align-items:center;
            padding:8px 14px;
            border-radius:999px;
            font-size:12px;
            font-weight:900;
            letter-spacing:0.03em;
            margin-bottom:16px;
        }
        .dominance-pill.green {
            color:#34d399;
            background: rgba(52,211,153,0.12);
            border:1px solid rgba(52,211,153,0.32);
            box-shadow: 0 0 18px rgba(52,211,153,0.10);
        }
        .dominance-pill.red {
            color:#ef4444;
            background: rgba(239,68,68,0.12);
            border:1px solid rgba(239,68,68,0.32);
            box-shadow: 0 0 18px rgba(239,68,68,0.12);
        }

        .small-muted {
            color: var(--muted);
            font-size: 12px;
            line-height: 1.55;
        }

        .trade-chip-row {
            display:flex;
            flex-wrap:wrap;
            gap:8px;
            margin-bottom:10px;
        }

        .trade-chip {
            display:inline-flex;
            align-items:center;
            gap:6px;
            padding:5px 9px;
            border-radius:999px;
            border:1px solid rgba(255,255,255,0.08);
            background: rgba(255,255,255,0.04);
            color:#dbe4f0;
            font-size:11px;
            font-weight:900;
        }

        .trade-note {
            background: rgba(255,255,255,0.03);
            border:1px solid rgba(255,255,255,0.08);
            border-radius:14px;
            padding:12px;
            color:#e2e8f0;
            font-size:13px;
            line-height: 1.65;
        }

        .status-ok, .status-warn, .status-bad {
            display:inline-block;
            padding: 4px 8px;
            border-radius: 999px;
            font-size: 11px;
            font-weight: 900;
            margin-right: 6px;
        }

        .status-ok   { background: rgba(34,197,94,0.15);  color:#86efac; border:1px solid rgba(34,197,94,0.28); }
        .status-warn { background: rgba(245,158,11,0.15); color:#fde68a; border:1px solid rgba(245,158,11,0.28); }
        .status-bad  { background: rgba(239,68,68,0.15);  color:#fda4af; border:1px solid rgba(239,68,68,0.28); }

        .activity-card {
            background: rgba(255,255,255,0.03);
            border:1px solid rgba(255,255,255,0.08);
            border-radius: 16px;
            padding: 12px;
            margin-bottom: 10px;
        }

        .activity-title {
            font-size: 14px;
            font-weight: 900;
            margin-bottom: 4px;
        }

        .activity-sub {
            color: #e2e8f0;
            font-size: 13px;
            line-height: 1.45;
        }

        .activity-time {
            color: var(--muted);
            font-size: 12px;
            margin-top: 6px;
        }

        .list-row {
            display:flex;
            justify-content:space-between;
            align-items:flex-start;
            gap:12px;
            padding: 12px 0;
            border-bottom: 1px solid rgba(255,255,255,0.07);
        }

        .list-left {
            color: #ffffff;
            font-size: 13px;
            font-weight: 800;
        }

        .list-right {
            color: #cbd5e1;
            font-size: 13px;
            font-weight: 700;
            text-align:right;
        }

        div.stButton > button,
        div.stFormSubmitButton > button,
        div.stDownloadButton > button,
        button[kind="secondary"],
        button[kind="primary"] {
            width: 100%;
            background: linear-gradient(180deg, #172033, #131c2c) !important;
            background-color: #172033 !important;
            color: #f8fafc !important;
            border: 1px solid rgba(255,255,255,0.14) !important;
            border-radius: 14px !important;
            font-weight: 800 !important;
            min-height: 42px !important;
            box-shadow: none !important;
            opacity: 1 !important;
        }

        div.stButton > button:hover,
        div.stFormSubmitButton > button:hover,
        div.stDownloadButton > button:hover,
        button[kind="secondary"]:hover,
        button[kind="primary"]:hover {
            background: linear-gradient(180deg, #1d2942, #172135) !important;
            background-color: #1d2942 !important;
            color: #ffffff !important;
            border-color: rgba(96,165,250,0.40) !important;
        }

        div.stButton > button:disabled,
        div.stFormSubmitButton > button:disabled,
        div.stDownloadButton > button:disabled,
        button[kind="secondary"]:disabled,
        button[kind="primary"]:disabled {
            background: linear-gradient(180deg, #172033, #131c2c) !important;
            background-color: #172033 !important;
            color: #cbd5e1 !important;
            border-color: rgba(255,255,255,0.14) !important;
            opacity: 1 !important;
        }

        
        .page-chip {
            display:inline-flex;
            align-items:center;
            gap:8px;
            padding:8px 12px;
            border-radius:999px;
            background: linear-gradient(90deg, rgba(52,211,153,0.16), rgba(96,165,250,0.16));
            border:1px solid rgba(255,255,255,0.12);
            color:#ffffff;
            font-size:12px;
            font-weight:900;
            margin-bottom:14px;
            letter-spacing:0.02em;
            text-transform:uppercase;
        }

        .nav-button-active {
            margin-bottom: 10px;
        }

.nav-button-active div.stButton > button {
            background: linear-gradient(90deg, rgba(52,211,153,0.22), rgba(96,165,250,0.24), rgba(244,114,182,0.18)) !important;
            border-color: rgba(255,255,255,0.26) !important;
            color: #ffffff !important;
            box-shadow: 0 0 0 1px rgba(255,255,255,0.05), 0 0 34px rgba(96,165,250,0.18) !important;
            transform: translateY(-1px) scale(1.01);
        }

        .nav-button-active div.stButton > button::before {
            content: "";
            position: absolute;
            left: 10px;
            top: 10px;
            bottom: 10px;
            width: 4px;
            border-radius: 999px;
            background: linear-gradient(180deg, #34d399, #60a5fa);
        }

        .nav-button-active div.stButton > button {
            position: relative !important;
            padding-left: 20px !important;
        }

        .portfolio-kicker {
            color: #cbd5e1;
            font-size: 12px;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            margin-bottom: 6px;
        }

        .portfolio-explain {
            color: #94a3b8;
            font-size: 13px;
            line-height: 1.55;
            margin-bottom: 12px;
        }

        .holding-row {
            display:flex;
            align-items:center;
            justify-content:space-between;
            gap:10px;
            padding:10px 0;
            border-bottom:1px solid rgba(255,255,255,0.06);
        }

        .holding-left {
            display:flex;
            flex-direction:column;
            gap:2px;
        }

        .holding-symbol {
            color:#ffffff;
            font-size:14px;
            font-weight:900;
        }

        .holding-sub {
            color:#94a3b8;
            font-size:12px;
        }

        .holding-right {
            text-align:right;
        }

        .holding-value {
            color:#e2e8f0;
            font-size:14px;
            font-weight:900;
        }

        .holding-share {
            color:#60a5fa;
            font-size:12px;
            font-weight:700;
        }

        .tiny-button div.stButton > button {
            min-height: 38px !important;
            border-radius: 12px !important;
            font-size: 12px !important;
        }

        .trade-button div.stButton > button {
            text-align: left !important;
            min-height: 50px !important;
        }

        .filter-card {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 16px;
            padding: 10px;
        }

        [data-testid="stTextInput"] input,
        [data-testid="stSelectbox"] div[data-baseweb="select"] > div,
        [data-testid="stMultiSelect"] div[data-baseweb="select"] > div {
            background: rgba(255,255,255,0.04) !important;
            color:#ffffff !important;
            border-radius: 12px !important;
            border:1px solid rgba(255,255,255,0.10) !important;
        }

        [data-testid="stDataFrame"] {
            border: 0 !important;
        }

        pre, code, .stCodeBlock, [data-testid="stCodeBlock"] {
            background: #0f172a !important;
            color: #e2e8f0 !important;
            border-radius: 12px !important;
            border: 1px solid rgba(255,255,255,0.08) !important;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


# ==========================================================
# HELPERS
# ==========================================================
def append_debug_event(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    st.session_state.debug_events = [f"{stamp} | {message}"] + st.session_state.debug_events[:49]


def capture_exception(prefix: str, err: Exception) -> None:
    text = f"{prefix}: {type(err).__name__}: {err}"
    st.session_state.last_error_text = text
    append_debug_event(text)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        s = str(value).strip()
        return s if s else default
    except Exception:
        return default


def parse_dt(value: Any) -> pd.Timestamp:
    try:
        return pd.to_datetime(value, utc=True, errors="coerce")
    except Exception:
        return pd.NaT


def format_dt_short(value: Any) -> str:
    dt = parse_dt(value)
    if pd.isna(dt):
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_money(value: Any) -> str:
    v = safe_float(value, 0.0)
    return f"€{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_r(value: Any) -> str:
    v = safe_float(value, 0.0)
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f} R"


def format_compact_r(value: Any) -> str:
    v = safe_float(value, 0.0)
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f}R"


def format_price(value: Any) -> str:
    v = safe_float(value, 0.0)
    if v >= 1000:
        return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if v >= 1:
        return f"{v:.4f}"
    return f"{v:.6f}"


def pct_str(value: Any) -> str:
    return f"{safe_float(value, 0.0):.1f}%"


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def safe_read_json(path: str) -> Tuple[Optional[dict], Optional[str]]:
    try:
        if not os.path.exists(path):
            return None, f"Bestand niet gevonden: {path}"
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle), None
    except Exception as exc:
        capture_exception("safe_read_json", exc)
        return None, f"JSON leesfout: {exc}"


def metric_card_html(label: str, value: str, accent: str = "") -> str:
    accent_class = ""
    if accent == "blue":
        accent_class = "accent-blue"
    elif accent == "green":
        accent_class = "accent-green"
    elif accent == "red":
        accent_class = "accent-red"
    elif accent == "purple":
        accent_class = "accent-purple"
    elif accent == "yellow":
        accent_class = "accent-yellow"

    return f"""
        <div class="metric-card">
            <div class="metric-value {accent_class}">{value}</div>
            <div class="metric-label">{label}</div>
        </div>
    """


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
        "pnl_eur",
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
        "day",
    ]
    return pd.DataFrame(columns=cols)


def get_status_badge(status: str) -> str:
    status_u = safe_str(status).upper()
    if status_u in {"OK", "CONNECTED", "READY"}:
        return f'<span class="status-ok">{status_u}</span>'
    if status_u in {"WARN", "WARNING", "DEMO", "FALLBACK"}:
        return f'<span class="status-warn">{status_u}</span>'
    return f'<span class="status-bad">{status_u}</span>'


def render_donut(value: float, title: str, color: str = "#34d399", subtitle: str = "", height: int = 210) -> go.Figure:
    value = max(0.0, min(100.0, safe_float(value, 0.0)))
    loss_value = max(0.0, 100.0 - value)
    dominant_red = loss_value > value
    glow_rgb = "239,68,68" if dominant_red else "52,211,153"

    fig = go.Figure()
    # soft outer glow ring
    fig.add_trace(
        go.Pie(
            values=[max(value, 0.0001), max(loss_value, 0.0001)],
            hole=0.62,
            sort=False,
            direction="clockwise",
            rotation=270,
            textinfo="none",
            marker=dict(
                colors=[f"rgba({glow_rgb},0.18)", f"rgba({glow_rgb},0.06)"],
                line=dict(color="rgba(255,255,255,0)", width=0),
            ),
            showlegend=False,
        )
    )
    # actual ring
    fig.add_trace(
        go.Pie(
            values=[max(value, 0.0001), max(loss_value, 0.0001)],
            labels=["Win", "Loss"],
            hole=0.76,
            sort=False,
            direction="clockwise",
            rotation=270,
            textinfo="none",
            marker=dict(
                colors=["#34d399", "#ef4444"],
                line=dict(color="rgba(255,255,255,0)", width=0),
            ),
            showlegend=False,
        )
    )

    annotations = [
        dict(text=f"<b>{value:.1f}%</b>", x=0.5, y=0.60, showarrow=False, font=dict(color="#ffffff", size=24)),
        dict(text=f"<span style='font-size:14px;color:#ffffff'><b>{title}</b></span>", x=0.5, y=0.41, showarrow=False, font=dict(color="#ffffff", size=14)),
    ]
    if subtitle:
        annotations.append(dict(text=f"<span style='font-size:11px;color:#cbd5e1'>{subtitle}</span>", x=0.5, y=0.23, showarrow=False, font=dict(color="#cbd5e1", size=11)))

    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=0, r=0, t=0, b=0),
        height=height,
        width=height,
        showlegend=False,
        annotations=annotations,
    )
    return fig


def chart_combined_performance_donut(df: pd.DataFrame, title: str = "REAL Performance") -> go.Figure:
    summary = real_trade_money_summary(df)
    win_pct = safe_float(summary["winrate"], 0.0)
    loss_pct = safe_float(summary["lossrate"], 0.0)
    net_eur = safe_float(summary["net_eur"], 0.0)
    dominant = safe_str(summary["dominant"], "red")

    is_win_dominant = dominant == "green"
    dominant_glow = "52,211,153" if is_win_dominant else "239,68,68"
    secondary_glow = "239,68,68" if is_win_dominant else "52,211,153"

    fig = go.Figure()

    # Soft outer halo with dominant glow clearly stronger than the secondary side
    fig.add_trace(
        go.Pie(
            values=[max(win_pct, 0.0001), max(loss_pct, 0.0001)],
            hole=0.56,
            sort=False,
            direction="clockwise",
            rotation=270,
            textinfo="none",
            marker=dict(
                colors=[
                    f"rgba({dominant_glow},0.34)",
                    f"rgba({secondary_glow},0.08)",
                ],
                line=dict(color="rgba(255,255,255,0)", width=0),
            ),
            showlegend=False,
        )
    )

    # Main visible ring stays green/red
    fig.add_trace(
        go.Pie(
            values=[max(win_pct, 0.0001), max(loss_pct, 0.0001)],
            labels=["Win", "Loss"],
            hole=0.78,
            sort=False,
            direction="clockwise",
            rotation=270,
            textinfo="none",
            marker=dict(
                colors=["#34d399", "#ef4444"],
                line=dict(color="rgba(255,255,255,0)", width=0),
            ),
            showlegend=False,
        )
    )

    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        width=700,
        height=620,
        margin=dict(l=0, r=0, t=0, b=0),
        showlegend=False,
        annotations=[
            dict(
                text=f"<b>{format_money(net_eur)}</b>",
                x=0.5,
                y=0.63,
                showarrow=False,
                font=dict(color="#ffffff", size=38),
            ),
            dict(
                text="<span style='font-size:22px;color:#ffffff'>Win Rate</span>",
                x=0.5,
                y=0.48,
                showarrow=False,
                font=dict(color="#ffffff", size=22),
            ),
            dict(
                text=f"<span style='font-size:20px;color:#ffffff'><b>{win_pct:.1f}%</b></span>",
                x=0.5,
                y=0.34,
                showarrow=False,
                font=dict(color="#ffffff", size=20),
            ),
            dict(
                text="<span style='font-size:12px;color:#94a3b8'>Alleen echte trades</span>",
                x=0.5,
                y=0.20,
                showarrow=False,
                font=dict(color="#94a3b8", size=12),
            ),
        ],
        shapes=[
            dict(
                type="line",
                xref="paper",
                yref="paper",
                x0=0.30,
                x1=0.70,
                y0=0.43,
                y1=0.43,
                line=dict(color=f"rgba({dominant_glow},0.90)", width=3),
            ),
            dict(
                type="circle",
                xref="paper",
                yref="paper",
                x0=0.08,
                y0=0.08,
                x1=0.92,
                y1=0.92,
                line=dict(color=f"rgba({dominant_glow},0.38)", width=24),
            ),
            dict(
                type="circle",
                xref="paper",
                yref="paper",
                x0=0.14,
                y0=0.14,
                x1=0.86,
                y1=0.86,
                line=dict(color=f"rgba({dominant_glow},0.14)", width=12),
            ),
        ],
    )
    return fig


def overall_trade_win_summary(df: pd.DataFrame) -> Dict[str, float]:
    work = df[df["outcome"].isin(["WIN", "LOSS"])].copy() if not df.empty else pd.DataFrame([])
    wins = int((work["outcome"] == "WIN").sum()) if not work.empty else 0
    losses = int((work["outcome"] == "LOSS").sum()) if not work.empty else 0
    total = wins + losses
    winrate = float((wins / total) * 100.0) if total > 0 else 0.0
    lossrate = 100.0 - winrate if total > 0 else 0.0
    return {
        "wins": wins,
        "losses": losses,
        "count": total,
        "winrate": winrate,
        "lossrate": lossrate,
    }


def euro_five_sim_summary(df: pd.DataFrame, stake_eur: float = 5.0) -> Dict[str, float]:
    work = df[df["outcome"].isin(["WIN", "LOSS"])].copy() if not df.empty else pd.DataFrame([])
    if work.empty:
        return {
            "stake_eur": stake_eur,
            "total_eur": 0.0,
            "gross_profit_eur": 0.0,
            "gross_loss_eur": 0.0,
            "money_winrate": 0.0,
            "wins": 0,
            "losses": 0,
        }

    entry = pd.to_numeric(work.get("entry", pd.Series(index=work.index, dtype=float)), errors="coerce")
    target = pd.to_numeric(work.get("target", pd.Series(index=work.index, dtype=float)), errors="coerce")
    stop = pd.to_numeric(work.get("stop", pd.Series(index=work.index, dtype=float)), errors="coerce")
    pnl_r = pd.to_numeric(work.get("pnl_r", pd.Series(index=work.index, dtype=float)), errors="coerce").fillna(0.0)

    sim_eur = pd.Series(0.0, index=work.index, dtype=float)

    valid_win = (work["outcome"] == "WIN") & entry.gt(0) & target.notna()
    sim_eur.loc[valid_win] = stake_eur * ((target[valid_win] - entry[valid_win]) / entry[valid_win])

    valid_loss = (work["outcome"] == "LOSS") & entry.gt(0) & stop.notna()
    sim_eur.loc[valid_loss] = stake_eur * ((stop[valid_loss] - entry[valid_loss]) / entry[valid_loss])

    unresolved = sim_eur.eq(0.0) & pnl_r.ne(0.0)
    sim_eur.loc[unresolved] = pnl_r[unresolved] * stake_eur

    gross_profit = float(sim_eur[sim_eur > 0].sum())
    gross_loss = float(abs(sim_eur[sim_eur < 0].sum()))
    total_net = float(sim_eur.sum())
    base = gross_profit + gross_loss
    money_winrate = float((gross_profit / base) * 100.0) if base > 0 else 0.0

    wins = int((sim_eur > 0).sum())
    losses = int((sim_eur < 0).sum())

    return {
        "stake_eur": stake_eur,
        "total_eur": total_net,
        "gross_profit_eur": gross_profit,
        "gross_loss_eur": gross_loss,
        "money_winrate": money_winrate,
        "wins": wins,
        "losses": losses,
    }


def real_trade_money_summary(df: pd.DataFrame) -> Dict[str, float]:
    work = df[df["outcome"].isin(["WIN", "LOSS"])].copy() if not df.empty else pd.DataFrame([])
    if work.empty:
        return {
            "wins": 0, "losses": 0, "count": 0, "winrate": 0.0, "lossrate": 0.0,
            "gross_profit_eur": 0.0, "gross_loss_eur": 0.0, "net_eur": 0.0, "dominant": "neutral",
        }
    pnl_eur = pd.to_numeric(work.get("pnl_eur", pd.Series(index=work.index, dtype=float)), errors="coerce")
    pnl_r = pd.to_numeric(work.get("pnl_r", pd.Series(index=work.index, dtype=float)), errors="coerce").fillna(0.0)
    pnl_eur = pnl_eur.fillna(pnl_r * 25.0)
    wins = int((work["outcome"] == "WIN").sum())
    losses = int((work["outcome"] == "LOSS").sum())
    total = wins + losses
    winrate = float((wins / total) * 100.0) if total > 0 else 0.0
    lossrate = 100.0 - winrate if total > 0 else 0.0
    gross_profit = float(pnl_eur[pnl_eur > 0].sum())
    gross_loss = float(abs(pnl_eur[pnl_eur < 0].sum()))
    net = float(pnl_eur.sum())
    dominant = "green" if winrate >= lossrate else "red"
    return {
        "wins": wins, "losses": losses, "count": total, "winrate": winrate, "lossrate": lossrate,
        "gross_profit_eur": gross_profit, "gross_loss_eur": gross_loss, "net_eur": net, "dominant": dominant,
    }

def render_real_stats_panel(summary: Dict[str, float]) -> None:
    dominant_label = "WINST DOMINEERT" if safe_str(summary.get("dominant")) == "green" else "VERLIES DOMINEERT"
    dominant_cls = "green" if safe_str(summary.get("dominant")) == "green" else "red"

    st.markdown('<div class="top-stats-panel">', unsafe_allow_html=True)
    st.markdown(f'<div class="dominance-pill {dominant_cls}">{dominant_label}</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Statistieken</div>', unsafe_allow_html=True)

    rows = [
        ("Aantal echte trades", str(int(summary.get("count", 0))), ""),
        ("Winst Trades", str(int(summary.get("wins", 0))), f'{safe_float(summary.get("winrate"), 0.0):.1f}%'),
        ("Verlies Trades", str(int(summary.get("losses", 0))), f'{safe_float(summary.get("lossrate"), 0.0):.1f}%'),
        ("Totale Winst", format_money(summary.get("gross_profit_eur", 0.0)), ""),
        ("Totale Verlies", f'-{format_money(summary.get("gross_loss_eur", 0.0)).replace("€", "€")}' if safe_float(summary.get("gross_loss_eur", 0.0)) > 0 else "€0,00", ""),
        ("Netto Resultaat", format_money(summary.get("net_eur", 0.0)), ""),
    ]
    for label, value, badge in rows:
        badge_html = f'<span class="metric-badge">{badge}</span>' if badge else ""
        st.markdown(f'<div class="stats-row"><div class="stats-label">{label}</div><div class="stats-value">{value} {badge_html}</div></div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

def get_selected_trade(df: pd.DataFrame, state_key: str = "selected_page_trade_id") -> Optional[pd.Series]:
    if df.empty:
        return None

    selected_id = st.session_state.get(state_key)
    if selected_id is None:
        st.session_state[state_key] = safe_str(df.iloc[0]["trade_id"])
        return df.iloc[0]

    selected = df[df["trade_id"].astype(str) == str(selected_id)]
    if not selected.empty:
        return selected.iloc[0]

    st.session_state[state_key] = safe_str(df.iloc[0]["trade_id"])
    return df.iloc[0]


# ==========================================================
# CHARTS
# ==========================================================
def empty_figure(message: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=300,
        margin=dict(l=20, r=20, t=30, b=20),
        annotations=[
            dict(
                text=message,
                x=0.5,
                y=0.5,
                xref="paper",
                yref="paper",
                showarrow=False,
                font=dict(color="#94a3b8", size=14),
            )
        ],
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
    )
    return fig


def style_chart(fig: go.Figure, height: int = 320) -> go.Figure:
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e2e8f0"),
        height=height,
        margin=dict(l=22, r=18, t=40, b=22),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0.0),
        xaxis=dict(gridcolor="rgba(255,255,255,0.06)"),
        yaxis=dict(gridcolor="rgba(255,255,255,0.06)", zerolinecolor="rgba(255,255,255,0.08)"),
    )
    return fig


def chart_equity_curve(df: pd.DataFrame, title: str) -> go.Figure:
    if df.empty:
        return empty_figure("Geen trade-data beschikbaar voor de equity curve.")
    work = df[df["outcome"].isin(["WIN", "LOSS"])].copy()
    if work.empty:
        return empty_figure("Geen win/loss-trades beschikbaar voor de equity curve.")

    work = work.sort_values("datetime_raw").copy()
    work["cum_r"] = pd.to_numeric(work["pnl_r"], errors="coerce").fillna(0.0).cumsum()
    work = downsample_dataframe(work, 900)

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=work["datetime_raw"],
            y=work["cum_r"],
            mode="lines+markers",
            name="Equity Curve",
            line=dict(color="#34d399", width=3),
            marker=dict(size=7, color="#34d399"),
            hovertemplate="Tijd: %{x}<br>Cumulatief R: %{y:.2f}<extra></extra>",
        )
    )
    fig.update_layout(title=title)
    return style_chart(fig, 330)


def chart_win_loss(df: pd.DataFrame, title: str) -> go.Figure:
    if df.empty:
        return empty_figure("Geen trade-data beschikbaar voor de win/loss chart.")
    work = df[df["outcome"].isin(["WIN", "LOSS"])].copy()
    if work.empty:
        return empty_figure("Geen win/loss-uitkomsten gevonden.")

    counts = work["outcome"].value_counts()
    wins = safe_int(counts.get("WIN", 0))
    losses = safe_int(counts.get("LOSS", 0))

    fig = go.Figure(
        data=[
            go.Bar(x=["WIN", "LOSS"], y=[wins, losses], marker=dict(color=["#34d399", "#fb7185"]), text=[wins, losses], textposition="outside")
        ]
    )
    fig.update_layout(title=title)
    return style_chart(fig, 300)


def chart_setup_performance(df: pd.DataFrame, title: str) -> go.Figure:
    if df.empty:
        return empty_figure("Geen trade-data beschikbaar voor setup performance.")
    work = df[df["outcome"].isin(["WIN", "LOSS"])].copy()
    if work.empty:
        return empty_figure("Geen win/loss-data beschikbaar voor setup performance.")

    grouped = (
        work.groupby("setup_type", dropna=False)
        .agg(n=("trade_id", "count"), avg_r=("pnl_r", "mean"))
        .reset_index()
        .sort_values(["n", "avg_r"], ascending=[False, False])
        .head(10)
    )

    fig = go.Figure(
        data=[
            go.Bar(
                x=grouped["setup_type"],
                y=grouped["avg_r"],
                text=grouped["n"].astype(int),
                textposition="outside",
                marker=dict(color="#60a5fa"),
                hovertemplate="Setup: %{x}<br>Gemiddelde R: %{y:.2f}<br>Aantal: %{text}<extra></extra>",
            )
        ]
    )
    fig.update_layout(title=title, xaxis_title="Setup", yaxis_title="Gemiddelde R")
    return style_chart(fig, 320)


def chart_trade_timeline(df: pd.DataFrame, title: str) -> go.Figure:
    if df.empty:
        return empty_figure("Geen trade-data beschikbaar voor de trade timeline.")
    work = df.copy().sort_values("datetime_raw")
    work = downsample_dataframe(work, 650)
    if work.empty:
        return empty_figure("Geen timeline-data beschikbaar.")

    colors = []
    for _, row in work.iterrows():
        outcome = safe_str(row.get("outcome")).upper()
        ttype = safe_str(row.get("trade_type")).upper()
        if outcome == "WIN":
            colors.append("#34d399")
        elif outcome == "LOSS":
            colors.append("#fb7185")
        elif ttype == "SIM":
            colors.append("#c084fc")
        elif ttype == "SHADOW":
            colors.append("#fbbf24")
        else:
            colors.append("#60a5fa")

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=work["datetime_raw"],
            y=work["symbol"],
            mode="markers",
            marker=dict(size=12, color=colors),
            text=work.apply(
                lambda r: f"{r['symbol']} | {r['setup_type']} | {r['trade_type']} | {r['outcome']} | {format_compact_r(r['pnl_r'])}",
                axis=1,
            ),
            hovertemplate="%{text}<br>%{x}<extra></extra>",
            name="Trades",
        )
    )
    fig.update_layout(title=title, xaxis_title="Tijd", yaxis_title="Symbol")
    return style_chart(fig, 350)


def chart_daily_r(df: pd.DataFrame, title: str) -> go.Figure:
    if df.empty:
        return empty_figure("Geen trade-data beschikbaar voor dagresultaten.")
    work = df[df["outcome"].isin(["WIN", "LOSS"])].copy()
    if work.empty:
        return empty_figure("Geen win/loss-data beschikbaar voor dagresultaten.")

    work["day"] = pd.to_datetime(work["datetime_raw"], errors="coerce", utc=True).dt.strftime("%Y-%m-%d")
    grouped = work.groupby("day", dropna=False)["pnl_r"].sum().reset_index()

    colors = ["#34d399" if v >= 0 else "#fb7185" for v in grouped["pnl_r"]]
    fig = go.Figure(
        data=[
            go.Bar(
                x=grouped["day"],
                y=grouped["pnl_r"],
                marker=dict(color=colors),
                hovertemplate="Dag: %{x}<br>R resultaat: %{y:.2f}<extra></extra>",
            )
        ]
    )
    fig.update_layout(title=title, xaxis_title="Dag", yaxis_title="R")
    return style_chart(fig, 300)


def chart_portfolio_allocation(assets_df: pd.DataFrame, title: str) -> go.Figure:
    if assets_df.empty:
        return empty_figure("Geen portfolio-data beschikbaar.")
    work = assets_df.copy()
    work = work[work["eur_value"] > 0].copy()
    if work.empty:
        return empty_figure("Geen portfolio-waardes beschikbaar.")
    work = work.sort_values("eur_value", ascending=False).reset_index(drop=True)
    if len(work) > 8:
        top = work.head(8).copy()
        rest_value = float(work.iloc[8:]["eur_value"].sum())
        if rest_value > 0:
            top = pd.concat([top, pd.DataFrame([{"symbol": "OVERIG", "eur_value": rest_value}])], ignore_index=True)
        work = top
    fig = go.Figure(
        data=[
            go.Pie(
                labels=work["symbol"],
                values=work["eur_value"],
                hole=0.56,
                textinfo="label+percent",
                marker=dict(line=dict(width=0)),
            )
        ]
    )
    fig.update_layout(
        title=title,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e2e8f0"),
        margin=dict(l=20, r=20, t=50, b=20),
        height=360,
        showlegend=True,
    )
    return fig


def build_trade_detail_chart(row: pd.Series) -> go.Figure:
    entry = safe_float(row.get("entry"), 0.0)
    stop = safe_float(row.get("stop"), 0.0)
    target = safe_float(row.get("target"), 0.0)

    if entry <= 0:
        return empty_figure("Geen geldig entryniveau beschikbaar voor trade-detail chart.")

    base = entry
    x_vals = list(range(20))
    opens: List[float] = []
    highs: List[float] = []
    lows: List[float] = []
    closes: List[float] = []

    for i in x_vals:
        drift = (i - 8) * 0.003 * base
        wave = ((i % 4) - 1.5) * 0.004 * base
        o = base + drift + wave
        c = o + ((-1) ** i) * 0.0035 * base
        h = max(o, c) + 0.005 * base
        l = min(o, c) - 0.005 * base
        opens.append(o)
        highs.append(h)
        lows.append(l)
        closes.append(c)

    fig = go.Figure()
    fig.add_trace(
        go.Candlestick(
            x=x_vals,
            open=opens,
            high=highs,
            low=lows,
            close=closes,
            increasing_line_color="#34d399",
            decreasing_line_color="#fb7185",
            increasing_fillcolor="#34d399",
            decreasing_fillcolor="#fb7185",
            showlegend=False,
            name="Price",
        )
    )

    fig.add_hline(y=entry, line_width=2, line_color="#60a5fa", annotation_text="ENTRY", annotation_position="right")
    if stop > 0:
        fig.add_hline(y=stop, line_width=2, line_color="#fb7185", annotation_text="STOP", annotation_position="right")
    if target > 0:
        fig.add_hline(y=target, line_width=2, line_color="#34d399", annotation_text="TARGET", annotation_position="right")

    fig.update_layout(
        title="Trade detail chart",
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#f8fafc"),
        height=360,
        margin=dict(l=8, r=8, t=40, b=8),
        xaxis=dict(showgrid=False, showticklabels=False),
        yaxis=dict(gridcolor="rgba(255,255,255,0.06)", zerolinecolor="rgba(255,255,255,0.08)"),
    )
    return fig


# ==========================================================
# PAGE HELPERS
# ==========================================================
def page_display_name(page_key: str) -> str:
    names = {
        "dashboard": "Dashboard",
        "live": "Live Performance",
        "sim": "Simulator",
        "shadow": "Shadow Review",
        "portfolio": "Portfolio",
        "help": "Help & Data Mapping",
    }
    return names.get(page_key, page_key.title())


def nav_button(label: str, page_key: str) -> None:
    active = st.session_state.page == page_key
    wrapper = "nav-button-active" if active else ""
    st.markdown(f'<div class="{wrapper}">', unsafe_allow_html=True)
    if st.button(label, key=f"nav_{page_key}", use_container_width=True):
        st.session_state.page = page_key
        st.session_state.selected_page_trade_id = None
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


def reset_global_filters() -> None:
    st.session_state.search_text = ""
    st.session_state.global_days_filter = "ALLES"
    st.session_state.global_trade_type_filter = "ALLES"
    st.session_state.global_setup_filter = "ALLES"
    st.session_state.global_regime_filter = "ALLES"
    st.session_state.global_outcome_filter = "ALLES"
    st.session_state.global_symbol_filter = "ALLES"
    st.session_state.status_notice = "Filters zijn gereset."


def render_filters(df: pd.DataFrame, include_trade_type: bool = True) -> pd.DataFrame:
    st.markdown('<div class="filter-card">', unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4, gap="small")
    with c1:
        st.session_state.search_text = st.text_input("Zoeken", value=st.session_state.search_text, placeholder="coin, setup, trade_id")
    with c2:
        st.session_state.global_days_filter = st.selectbox(
            "Periode",
            ["ALLES", "7D", "30D", "90D", "180D", "365D"],
            index=["ALLES", "7D", "30D", "90D", "180D", "365D"].index(st.session_state.global_days_filter)
            if st.session_state.global_days_filter in ["ALLES", "7D", "30D", "90D", "180D", "365D"]
            else 2,
        )
    with c3:
        setups = ["ALLES"] + sorted(df["setup_type"].dropna().astype(str).unique().tolist()) if not df.empty else ["ALLES"]
        current = st.session_state.global_setup_filter if st.session_state.global_setup_filter in setups else "ALLES"
        st.session_state.global_setup_filter = st.selectbox("Setup", setups, index=setups.index(current))
    with c4:
        symbols = ["ALLES"] + sorted(df["symbol"].dropna().astype(str).unique().tolist()) if not df.empty else ["ALLES"]
        current = st.session_state.global_symbol_filter if st.session_state.global_symbol_filter in symbols else "ALLES"
        st.session_state.global_symbol_filter = st.selectbox("Coin", symbols, index=symbols.index(current))

    c5, c6, c7, c8 = st.columns(4, gap="small")
    with c5:
        regimes = ["ALLES"] + sorted(df["regime"].dropna().astype(str).unique().tolist()) if not df.empty else ["ALLES"]
        current = st.session_state.global_regime_filter if st.session_state.global_regime_filter in regimes else "ALLES"
        st.session_state.global_regime_filter = st.selectbox("Regime", regimes, index=regimes.index(current))
    with c6:
        outcomes = ["ALLES"] + sorted(df["outcome"].dropna().astype(str).unique().tolist()) if not df.empty else ["ALLES"]
        current = st.session_state.global_outcome_filter if st.session_state.global_outcome_filter in outcomes else "ALLES"
        st.session_state.global_outcome_filter = st.selectbox("Outcome", outcomes, index=outcomes.index(current))
    with c7:
        if include_trade_type:
            trade_types = ["ALLES"] + sorted(df["trade_type"].dropna().astype(str).unique().tolist()) if not df.empty else ["ALLES"]
            current = st.session_state.global_trade_type_filter if st.session_state.global_trade_type_filter in trade_types else "ALLES"
            st.session_state.global_trade_type_filter = st.selectbox("Trade type", trade_types, index=trade_types.index(current))
        else:
            st.markdown('<div class="small-muted" style="padding-top: 24px;">Trade type is op deze pagina vastgezet.</div>', unsafe_allow_html=True)
    with c8:
        st.markdown('<div class="tiny-button">', unsafe_allow_html=True)
        if st.button("Reset filters", key="reset_filters", use_container_width=True):
            reset_global_filters()
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)

    return apply_trade_filters(
        df,
        search_text=st.session_state.search_text,
        days_filter=st.session_state.global_days_filter,
        trade_type_filter=st.session_state.global_trade_type_filter if include_trade_type else "ALLES",
        setup_filter=st.session_state.global_setup_filter,
        regime_filter=st.session_state.global_regime_filter,
        outcome_filter=st.session_state.global_outcome_filter,
        symbol_filter=st.session_state.global_symbol_filter,
    )


def render_trade_list(df: pd.DataFrame, state_key: str = "selected_page_trade_id", title: str = "Trades") -> None:
    st.markdown(f'<div class="section-title">{title}</div>', unsafe_allow_html=True)
    if df.empty:
        st.info("Geen trades gevonden voor deze selectie.")
        return

    list_df = df.head(10).copy()
    for _, row in list_df.iterrows():
        trade_id = safe_str(row.get("trade_id"))
        label = (
            f"{safe_str(row.get('symbol'), '-')}"
            f" | {safe_str(row.get('setup_type'), '-')}"
            f" | {safe_str(row.get('outcome'), '-')}"
            f" | {format_compact_r(row.get('pnl_r'))}"
            f" | {safe_str(row.get('datetime'), '-')}"
        )
        active = st.session_state.get(state_key) == trade_id
        wrapper = "trade-button nav-button-active" if active else "trade-button"
        st.markdown(f'<div class="{wrapper}">', unsafe_allow_html=True)
        if st.button(label, key=f"{state_key}_{trade_id}", use_container_width=True):
            st.session_state[state_key] = trade_id
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)


def render_trade_detail(row: Optional[pd.Series]) -> None:
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Trade detail</div>', unsafe_allow_html=True)
    if row is None:
        st.info("Geen trade gevonden.")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    st.markdown(
        f"""
        <div class="trade-chip-row">
            <div class="trade-chip">{safe_str(row.get("symbol"), "-")}</div>
            <div class="trade-chip">{safe_str(row.get("trade_type"), "-")}</div>
            <div class="trade-chip">{safe_str(row.get("setup_type"), "-")}</div>
            <div class="trade-chip">{safe_str(row.get("timeframe"), "-")}</div>
            <div class="trade-chip">{safe_str(row.get("regime"), "-")}</div>
            <div class="trade-chip">{safe_str(row.get("outcome"), "-")}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2 = st.columns(2, gap="small")
    with c1:
        for label, value in [
            ("Trade ID", safe_str(row.get("trade_id"), "-")),
            ("Score", safe_str(row.get("score"), "-")),
            ("Chance", safe_str(row.get("chance"), "-")),
            ("Confidence", safe_str(row.get("confidence"), "-")),
            ("Entry", format_price(row.get("entry"))),
            ("Stop", format_price(row.get("stop"))),
            ("Target", format_price(row.get("target"))),
        ]:
            st.markdown(f'<div class="list-row"><div class="list-left">{label}</div><div class="list-right">{value}</div></div>', unsafe_allow_html=True)

    with c2:
        created = format_dt_short(row.get("created_at"))
        closed = format_dt_short(row.get("closed_at"))
        duration = "-"
        if not pd.isna(parse_dt(row.get("created_at"))) and not pd.isna(parse_dt(row.get("closed_at"))):
            diff = parse_dt(row.get("closed_at")) - parse_dt(row.get("created_at"))
            mins = int(diff.total_seconds() // 60)
            duration = f"{mins // 60}h {mins % 60}m"

        for label, value in [
            ("P&L in R", format_compact_r(row.get("pnl_r"))),
            ("Open", created),
            ("Close", closed),
            ("Duration", duration),
            ("Label", safe_str(row.get("label"), "-")),
            ("Source", safe_str(row.get("source"), "-")),
        ]:
            st.markdown(f'<div class="list-row"><div class="list-left">{label}</div><div class="list-right">{value}</div></div>', unsafe_allow_html=True)

    st.plotly_chart(build_trade_detail_chart(row), use_container_width=True, config={"displayModeBar": False})

    st.markdown(
        f'<div class="trade-note"><b>Trade-analyse:</b> {trade_reason_text(row)}</div>',
        unsafe_allow_html=True,
    )
    st.markdown("</div>", unsafe_allow_html=True)


def render_activity_panel(feed: List[Dict[str, Any]], source_mode: str, history_df: pd.DataFrame, real_df: pd.DataFrame, sim_df: pd.DataFrame, shadow_df: pd.DataFrame, snapshot_mode: str) -> None:
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Systeemstatus</div>', unsafe_allow_html=True)

    status_html = (
        get_status_badge("CONNECTED" if db_ready() else "DEMO")
        + get_status_badge(source_mode)
        + get_status_badge(snapshot_mode)
    )
    st.markdown(status_html, unsafe_allow_html=True)
    st.markdown(
        '<div class="small-muted">Alle knoppen zijn geforceerd donker gestyled. Activity-items gebruiken veilige rendering zonder ruwe HTML output.</div>',
        unsafe_allow_html=True,
    )

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Data health</div>', unsafe_allow_html=True)
    reasons: List[str] = []
    if history_df.empty:
        reasons.append("history_df is leeg: dashboardgrafieken zouden zonder fallback leeg zijn.")
    if real_df.empty:
        reasons.append("real_df is leeg: Live Performance heeft geen echte trades.")
    if sim_df.empty:
        reasons.append("sim_df is leeg: Simulatorgrafieken tonen geen simulaties.")
    if shadow_df.empty:
        reasons.append("shadow_df is leeg: Shadow Review heeft niets om te evalueren.")
    if not db_ready():
        reasons.append("DATABASE_URL ontbreekt: de app draait daarom met demo/fallback voor consistente output.")
    if not reasons:
        reasons.append("Alle primaire datasets bevatten data of zijn succesvol vervangen door geldige fallback-output.")

    for reason in reasons:
        st.markdown(f'<div class="small-muted">• {reason}</div>', unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Broncontrole</div>', unsafe_allow_html=True)
    table_rows = table_count("experience_trades") if db_ready() else 0
    st.markdown(f'<div class="small-muted">• source_mode: <b>{source_mode}</b></div>', unsafe_allow_html=True)
    st.markdown(f'<div class="small-muted">• experience_trades rows in PostgreSQL: <b>{table_rows}</b></div>', unsafe_allow_html=True)
    st.markdown(f'<div class="small-muted">• history_df rows in app: <b>{len(history_df)}</b></div>', unsafe_allow_html=True)
    st.markdown(f'<div class="small-muted">• real_df rows in app: <b>{len(real_df)}</b></div>', unsafe_allow_html=True)
    st.markdown(f'<div class="small-muted">• sim_df rows in app: <b>{len(sim_df)}</b></div>', unsafe_allow_html=True)
    st.markdown(f'<div class="small-muted">• shadow_df rows in app: <b>{len(shadow_df)}</b></div>', unsafe_allow_html=True)
    st.markdown('<div class="small-muted">• DEMO_NOODMODUS = alleen fallback. DB_PRIORITY = echte PostgreSQL-data actief. DB_MAP_ISSUE = PostgreSQL heeft rows, maar de app kon de mapping nog niet goed opbouwen.</div>', unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Recent activity</div>', unsafe_allow_html=True)
    if not feed:
        st.markdown('<div class="small-muted">Nog geen activiteit beschikbaar.</div>', unsafe_allow_html=True)
    else:
        for item in feed:
            activity_time = format_dt_short(item.get("ts"))
            st.markdown(
                f"""
                <div class="activity-card">
                    <div class="activity-title" style="color:{safe_str(item.get("color"), "#60a5fa")};">{safe_str(item.get("title"), "-")}</div>
                    <div class="activity-sub">{safe_str(item.get("sub"), "-")}</div>
                    <div class="activity-time">{activity_time}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    if st.session_state.status_notice:
        st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
        st.info(st.session_state.status_notice)

    if st.session_state.show_debug:
        st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Debug</div>', unsafe_allow_html=True)
        st.json(
            {
                "database_url_present": bool(DATABASE_URL),
                "api_key_present": bool(API_KEY),
                "api_secret_present": bool(API_SECRET),
                "history_rows": len(history_df),
                "real_rows": len(real_df),
                "sim_rows": len(sim_df),
                "shadow_rows": len(shadow_df),
                "last_error_text": st.session_state.last_error_text,
                "debug_events": st.session_state.debug_events[:10],
            }
        )
    st.markdown("</div>", unsafe_allow_html=True)


def render_table_export(df: pd.DataFrame, file_name: str) -> None:
    if df.empty:
        st.info("Geen data om te exporteren.")
        return
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV",
        data=csv_bytes,
        file_name=file_name,
        mime="text/csv",
        use_container_width=True,
    )


# ==========================================================
# PAGE RENDERS
# ==========================================================

def chart_source_distribution(df: pd.DataFrame, title: str) -> go.Figure:
    if df.empty:
        fig = go.Figure()
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=320,
            margin=dict(l=10, r=10, t=50, b=20),
            title=title,
        )
        return fig

    work = df.copy()
    counts = work["trade_type"].fillna("UNKNOWN").astype(str).value_counts().reset_index()
    counts.columns = ["trade_type", "count"]

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=counts["trade_type"],
            y=counts["count"],
            text=counts["count"],
            textposition="outside",
        )
    )
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=320,
        margin=dict(l=10, r=10, t=50, b=20),
        title=title,
        xaxis_title="Trade type",
        yaxis_title="Aantal trades",
    )
    return fig


def render_dashboard_page(history_df: pd.DataFrame, real_df: pd.DataFrame, sim_df: pd.DataFrame, shadow_df: pd.DataFrame, scoreboard_df: pd.DataFrame) -> None:
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Dashboard</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-subtitle">Bovenste sectie opgebouwd als hero-layout: twee kleine donuts links onder elkaar, een grote performance-donut centraal en statistieken rechts, met neon glow zoals in je referentiebeeld.</div>', unsafe_allow_html=True)

    filtered = render_filters(history_df, include_trade_type=True)
    summary = performance_summary(filtered)

    m1, m2, m3, m4, m5 = st.columns(5, gap="small")
    with m1:
        st.markdown(metric_card_html("Trades in selectie", str(int(summary["count"])), "blue"), unsafe_allow_html=True)
    with m2:
        st.markdown(metric_card_html("Totale R", format_r(summary["total_r"]), "green"), unsafe_allow_html=True)
    with m3:
        st.markdown(metric_card_html("Gemiddelde R", f"{summary['avg_r']:.2f} R", "purple"), unsafe_allow_html=True)
    with m4:
        st.markdown(metric_card_html("Expectancy", f"{summary['expectancy']:.2f} R", "yellow"), unsafe_allow_html=True)
    with m5:
        st.markdown(metric_card_html("Totaal verdiend (€)", format_money(summary["total_eur"]), "blue"), unsafe_allow_html=True)

    overall_trade_summary = overall_trade_win_summary(history_df if history_df is not None else pd.DataFrame([]))
    euro_sim5_summary = euro_five_sim_summary(history_df if history_df is not None else pd.DataFrame([]), stake_eur=5.0)
    real_money_summary = real_trade_money_summary(real_df if real_df is not None else pd.DataFrame([]))

    left_col, main_col, right_col = st.columns([0.84, 1.78, 0.92], gap="small")

    with left_col:
        st.markdown('<div class="top-left-stack">', unsafe_allow_html=True)

        st.markdown('<div class="top-mini-card">', unsafe_allow_html=True)
        st.markdown('<div class="mini-title"><span class="green">Trade</span> Win Rate</div>', unsafe_allow_html=True)
        st.plotly_chart(
            render_donut(
                safe_float(overall_trade_summary["winrate"], 0.0),
                "Trade Win Rate",
                "#34d399",
                subtitle=f"{int(overall_trade_summary['wins'])} win / {int(overall_trade_summary['losses'])} loss",
                height=190,
            ),
            use_container_width=True,
            config={"displayModeBar": False},
        )
        st.markdown(f'<div class="small-muted">{int(overall_trade_summary["wins"])} win / {int(overall_trade_summary["losses"])} loss / {max(0, int(len(history_df) - overall_trade_summary["count"])) if history_df is not None and not history_df.empty else 0} open</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

        st.markdown('<div class="top-mini-card">', unsafe_allow_html=True)
        st.markdown('<div class="mini-title"><span class="green">Euro</span> Win Rate</div>', unsafe_allow_html=True)
        st.plotly_chart(
            render_donut(
                safe_float(euro_sim5_summary["money_winrate"], 0.0),
                "Euro Win Rate",
                "#34d399",
                subtitle=f"{format_money(euro_sim5_summary['total_eur'])} @ €5/trade",
                height=190,
            ),
            use_container_width=True,
            config={"displayModeBar": False},
        )
        st.markdown(f'<div class="small-muted">{format_money(euro_sim5_summary["gross_profit_eur"])} / €-{safe_float(euro_sim5_summary["gross_loss_eur"],0.0):,.2f}'.replace(",", "X").replace(".", ",").replace("X", ".") + '</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

        st.markdown('</div>', unsafe_allow_html=True)

    with main_col:
        st.markdown('<div class="top-main-card">', unsafe_allow_html=True)
        st.markdown(f'<div class="top-main-caption">{format_money(real_money_summary.get("net_eur", 0.0))}</div>', unsafe_allow_html=True)
        st.markdown('<div class="top-main-kicker">Win Rate</div>', unsafe_allow_html=True)
        st.plotly_chart(
            chart_combined_performance_donut(real_df if real_df is not None else pd.DataFrame([]), "REAL Performance"),
            use_container_width=True,
            config={"displayModeBar": False},
        )
        st.markdown('<div class="top-legend"><span><span class="top-legend-dot" style="background:#34d399;"></span>Win</span><span><span class="top-legend-dot" style="background:#ef4444;"></span>Loss</span></div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

    with right_col:
        render_real_stats_panel(real_money_summary)

    st.caption("Links: alle trades samen. Midden: euro-simulatie over alle trades samen met €5 vaste inzet per trade. Grote hero-donut en statistieken rechts: alleen echte live trades.")

    tab_overview, tab_analytics, tab_tapelist = st.tabs(["Overview", "Analytics", "Trade Tape"])

    with tab_overview:
        row1_left, row1_right = st.columns([1.45, 1.0], gap="small")
        with row1_left:
            st.plotly_chart(chart_equity_curve(filtered, "Equity Curve"), use_container_width=True, config={"displayModeBar": False})
        with row1_right:
            st.plotly_chart(chart_win_loss(filtered, "Win / Loss Verdeling"), use_container_width=True, config={"displayModeBar": False})

        row2_left, row2_right = st.columns([1.05, 1.0], gap="small")
        with row2_left:
            st.plotly_chart(chart_setup_performance(filtered, "Setup Performance"), use_container_width=True, config={"displayModeBar": False})
        with row2_right:
            best_setup = "-"
            if not scoreboard_df.empty and "setup_type" in scoreboard_df.columns:
                tmp = scoreboard_df.copy()
                tmp["win_rate"] = pd.to_numeric(tmp["win_rate"], errors="coerce").fillna(0.0)
                tmp["n"] = pd.to_numeric(tmp["n"], errors="coerce").fillna(0.0)
                tmp = tmp.sort_values(["win_rate", "n"], ascending=[False, False])
                if not tmp.empty:
                    top = tmp.iloc[0]
                    best_setup = f"{safe_str(top.get('setup_type'), '-')} | {safe_str(top.get('market_regime'), '-')}"
            st.markdown('<div class="panel-tight">', unsafe_allow_html=True)
            st.markdown('<div class="section-title">Wat hier pro voelt</div>', unsafe_allow_html=True)
            st.markdown(
                f"""
                <div class="trade-note">
                    <b>1. Hero-section met duidelijke hiërarchie</b><br>
                    Kleine context links, hoofdperformance in het midden, details rechts.<br><br>
                    <b>2. Glow alleen waar het telt</b><br>
                    De hoofdperformance krijgt alle nadruk.<br><br>
                    <b>3. Beste setup nu:</b> {best_setup}
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.markdown('</div>', unsafe_allow_html=True)

    with tab_analytics:
        a1, a2 = st.columns([1.0, 1.0], gap="small")
        with a1:
            st.plotly_chart(chart_daily_r(filtered, "Dagresultaten in R"), use_container_width=True, config={"displayModeBar": False})
        with a2:
            st.plotly_chart(chart_trade_timeline(filtered, "Trade Timeline"), use_container_width=True, config={"displayModeBar": False})

        b1, b2 = st.columns([1.0, 1.0], gap="small")
        with b1:
            st.plotly_chart(chart_source_distribution(filtered, "Bronverdeling in selectie"), use_container_width=True, config={"displayModeBar": False})
        with b2:
            st.plotly_chart(chart_daily_r(filtered, "Dagresultaten in R (detail)"), use_container_width=True, config={"displayModeBar": False})

    with tab_tapelist:
        t1, t2 = st.columns([1.15, 0.95], gap="small")
        with t1:
            render_trade_list(filtered, state_key="selected_page_trade_id", title="Dashboard trade tape")
        with t2:
            selected = get_selected_trade(filtered, state_key="selected_page_trade_id")
            render_trade_detail(selected)

    st.markdown("</div>", unsafe_allow_html=True)

def render_trade_page(page_name: str, df: pd.DataFrame, include_trade_type: bool = False) -> None:
    title_map = {
        "live": "Live Performance",
        "sim": "Simulator",
        "shadow": "Shadow Review",
    }
    subtitle_map = {
        "live": "Deze pagina gebruikt alleen real_df. Hierdoor blijven live performance, expectancy en trade-details vrij van simulator- of shadow-ruis.",
        "sim": "Deze pagina gebruikt alleen sim_df. Historische en hypothetische signalen blijven hiermee gescheiden van echte trades.",
        "shadow": "Deze pagina gebruikt alleen shadow_df. Gemiste of bewust niet-uitgevoerde trades worden hier apart geëvalueerd.",
    }

    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown(f'<div class="section-title">{title_map.get(page_name, "Trades")}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="section-subtitle">{subtitle_map.get(page_name, "")}</div>', unsafe_allow_html=True)

    filtered = render_filters(df, include_trade_type=include_trade_type)
    summary = performance_summary(filtered)

    m1, m2, m3, m4 = st.columns(4, gap="small")
    with m1:
        st.markdown(metric_card_html("Trades", str(int(summary["count"])), "blue"), unsafe_allow_html=True)
    with m2:
        st.markdown(metric_card_html("Totale R", format_r(summary["total_r"]), "green"), unsafe_allow_html=True)
    with m3:
        st.markdown(metric_card_html("Win Rate", pct_str(summary["winrate"]), "purple"), unsafe_allow_html=True)
    with m4:
        st.markdown(metric_card_html("Max Drawdown", f"{summary['max_drawdown']:.2f} R", "red"), unsafe_allow_html=True)

    chart_left, chart_right = st.columns([1.4, 1.0], gap="small")
    with chart_left:
        st.plotly_chart(chart_equity_curve(filtered, f"{title_map.get(page_name, 'Trades')} Equity Curve"), use_container_width=True, config={"displayModeBar": False})
    with chart_right:
        st.plotly_chart(chart_win_loss(filtered, f"{title_map.get(page_name, 'Trades')} Win/Loss"), use_container_width=True, config={"displayModeBar": False})

    lower_left, lower_right = st.columns([1.2, 1.0], gap="small")
    with lower_left:
        render_trade_list(filtered, state_key="selected_page_trade_id", title=f"{title_map.get(page_name, 'Trades')} lijst")
    with lower_right:
        selected = get_selected_trade(filtered, state_key="selected_page_trade_id")
        render_trade_detail(selected)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.plotly_chart(chart_setup_performance(filtered, f"{title_map.get(page_name, 'Trades')} Setup Performance"), use_container_width=True, config={"displayModeBar": False})

    display_cols = [c for c in ["trade_id", "symbol", "trade_type", "setup_type", "regime", "timeframe", "outcome", "pnl_r", "score", "chance", "confidence", "datetime"] if c in filtered.columns]
    if display_cols:
        st.dataframe(filtered[display_cols], use_container_width=True, hide_index=True)

    render_table_export(filtered, f"{page_name}_trades_export.csv")
    st.markdown("</div>", unsafe_allow_html=True)



def render_portfolio_page(snapshot: dict, assets_df: pd.DataFrame, snapshot_mode: str) -> None:
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="portfolio-kicker">Portfolio Overzicht</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Portfolio</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="portfolio-explain">Deze pagina is duidelijker opgebouwd zoals je van een pro dashboard verwacht: eerst totaalbeeld, daarna cash-vs-crypto, daarna top holdings en een simpele holdings-tabel.</div>',
        unsafe_allow_html=True,
    )

    eur_available = safe_float(snapshot.get("eur_available"), 0.0)
    crypto_assets_eur = safe_float(snapshot.get("crypto_assets_eur"), 0.0)
    total_portfolio_eur = safe_float(snapshot.get("total_portfolio_eur"), 0.0)
    holdings_df = prepare_portfolio_table(assets_df, total_portfolio_eur)

    m1, m2, m3, m4 = st.columns(4, gap="small")
    with m1:
        st.markdown(metric_card_html("Total Portfolio Value", format_money(total_portfolio_eur), "blue"), unsafe_allow_html=True)
    with m2:
        st.markdown(metric_card_html("Available EUR Balance", format_money(eur_available), "green"), unsafe_allow_html=True)
    with m3:
        st.markdown(metric_card_html("Crypto Assets Value", format_money(crypto_assets_eur), "purple"), unsafe_allow_html=True)
    with m4:
        cash_pct = (eur_available / total_portfolio_eur * 100.0) if total_portfolio_eur > 0 else 0.0
        st.markdown(metric_card_html("Cash aandeel", f"{cash_pct:.1f}%", "yellow"), unsafe_allow_html=True)

    top_left, top_right = st.columns([1.08, 0.92], gap="small")
    with top_left:
        st.plotly_chart(chart_portfolio_allocation(holdings_df, "Portfolio Allocation"), use_container_width=True, config={"displayModeBar": False})
    with top_right:
        st.markdown('<div class="panel-tight">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Snapshot status</div>', unsafe_allow_html=True)
        st.markdown(get_status_badge(snapshot_mode) + get_status_badge(safe_str(snapshot.get("status"), snapshot_mode)), unsafe_allow_html=True)
        st.markdown(f'<div class="small-muted">Laatste snapshot: {format_dt_short(snapshot.get("ts"))}</div>', unsafe_allow_html=True)
        st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
        if holdings_df.empty:
            st.markdown('<div class="small-muted">Geen holdings gevonden.</div>', unsafe_allow_html=True)
        else:
            top_symbol = safe_str(holdings_df.iloc[0].get("symbol"), "-")
            top_share = safe_float(holdings_df.iloc[0].get("share_pct"), 0.0)
            st.markdown(f'<div class="small-muted">Grootste holding: <b>{top_symbol}</b></div>', unsafe_allow_html=True)
            st.markdown(f'<div class="small-muted">Aandeel grootste holding: <b>{top_share:.1f}%</b></div>', unsafe_allow_html=True)
            st.markdown(f'<div class="small-muted">Aantal zichtbare holdings: <b>{len(holdings_df)}</b></div>', unsafe_allow_html=True)
        st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="trade-note"><b>Leeswijzer</b><br>Links zie je de verdeling over coins. Boven zie je cash vs crypto. Onder staat een compacte holdings-tabel met aandeel per coin.</div>',
            unsafe_allow_html=True,
        )
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Top holdings</div>', unsafe_allow_html=True)

    if holdings_df.empty:
        st.info("Geen portfolio-data beschikbaar.")
    else:
        table_df = holdings_df.copy().head(12)
        keep_cols = [c for c in ["symbol", "display_amount", "display_price", "display_value", "share_pct"] if c in table_df.columns]
        rename_map = {
            "symbol": "Coin",
            "display_amount": "Amount",
            "display_price": "Prijs",
            "display_value": "Waarde",
            "share_pct": "Aandeel %",
        }
        table_df = table_df[keep_cols].rename(columns=rename_map)
        if "Aandeel %" in table_df.columns:
            table_df["Aandeel %"] = pd.to_numeric(table_df["Aandeel %"], errors="coerce").fillna(0.0).map(lambda x: f"{x:.1f}%")
        st.dataframe(table_df, use_container_width=True, hide_index=True)

    st.markdown("</div>", unsafe_allow_html=True)


def render_help_page(history_df: pd.DataFrame, real_df: pd.DataFrame, sim_df: pd.DataFrame, shadow_df: pd.DataFrame, source_mode: str, snapshot_mode: str) -> None:
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Help & Data Mapping</div>', unsafe_allow_html=True)
    st.markdown('<div class="small-muted">Deze Help-pagina is klikbaar via het linkermenu en via de S-knop. Hier staat expliciet welke databron elk scherm gebruikt en hoe de grafieken worden gevuld.</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-subtitle">Deze pagina legt expliciet uit welke data naar welke schermdelen gaat, zodat grafieken en metrics logisch gekoppeld blijven.</div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="trade-note">
            <b>Data mapping in deze app</b><br><br>
            1. <b>Dashboard</b> gebruikt <b>history_df</b> = REAL + SIM + SHADOW.<br>
            2. <b>Live Performance</b> gebruikt alleen <b>real_df</b>.<br>
            3. <b>Simulator</b> gebruikt alleen <b>sim_df</b>.<br>
            4. <b>Shadow Review</b> gebruikt alleen <b>shadow_df</b>.<br>
            5. <b>Portfolio</b> gebruikt alleen <b>snapshot</b> en <b>assets_df</b>.<br><br>
            Daardoor hoort de data op de juiste plek en blijven grafieken, metrics en tabellen synchroon.<br><br>
            <b>Nieuwe extra metric:</b> naast de gewone <b>Trade Win Rate</b> toont de app nu ook <b>Totaal verdiend</b>.
            Die tweede donut meet hoeveel van het totale geldresultaat uit winsten komt ten opzichte van winsten + verliezen in euro.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Wanneer demo mode nog wordt geactiveerd</div>', unsafe_allow_html=True)
    st.markdown(
        """
        <div class="trade-note">
            Demo mode wordt nu alleen nog gebruikt als:
            <br>1. <b>experience_trades</b> niets bruikbaars teruggeeft
            <br>2. <b>real_df</b>, <b>sim_df</b>, <b>shadow_df</b> allemaal leeg zijn
            <br>3. de app dus anders volledig lege grafieken en lege schermen zou tonen
            <br><br>
            Zodra PostgreSQL bruikbare rows levert, krijgt de echte database prioriteit boven fallback-data.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Meer PostgreSQL-data dan nu zichtbaar</div>', unsafe_allow_html=True)
    st.markdown(
        """
        <div class="trade-note">
            Ja, in PostgreSQL kan veel meer staan dan nu op het scherm zichtbaar is.
            Naast de hoofdvelden zoals symbol, setup_type, regime, outcome, result_r en timestamps,
            kunnen ook velden bestaan zoals quantity, size, leverage, pnl_eur, result_eur, fee, commission,
            slippage, strategy_name, exchange, order_id, entry_reason, exit_reason, notes en execution details.
            Deze versie leest nu expliciet jouw schema met trade_key, source, coin, setup_type, market_regime, entry_timeframe, outcome, entry, stop, target, timestamp, entry_time, exit_time, mfe en mae, plus meerdere mogelijke euro-PnL kolommen.
            Als jij wilt, kan de volgende versie ook fees, trade size, quantity en echte netto euro-winst per setup apart tonen.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Waarom grafieken soms leeg lijken</div>', unsafe_allow_html=True)
    reasons = []
    if history_df.empty:
        reasons.append("Dashboardgrafieken hebben geen history_df.")
    if real_df.empty:
        reasons.append("Live Performance heeft geen echte trades.")
    if sim_df.empty:
        reasons.append("Simulator heeft geen sim-data.")
    if shadow_df.empty:
        reasons.append("Shadow Review heeft geen shadow-data.")
    if not reasons:
        reasons.append("Geen probleem gedetecteerd: datasets bevatten data of geldige fallback-output.")

    for item in reasons:
        st.markdown(f'<div class="small-muted">• {item}</div>', unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Extra uitleg over PostgreSQL-data</div>', unsafe_allow_html=True)
    st.markdown("""
    <div class="trade-note">
        Deze app leest nu vooral uit <b>experience_trades</b>, <b>experience_scoreboard</b> en <b>pending_approvals</b>.
        In PostgreSQL kunnen echter veel meer bruikbare velden aanwezig zijn, zoals bijvoorbeeld quantity, fee, pnl_eur,
        result_eur, entry_reason, exit_reason, strategy_name, exchange, order_id, commission, slippage of notes.
        Als die kolommen aanwezig zijn, kunnen we ze ook op aparte kaarten, tabellen en grafieken tonen.
        De huidige versie gebruikt bewust alleen de stabiele hoofdvelden zodat het dashboard niet breekt bij schema-verschillen.
    </div>
    """, unsafe_allow_html=True)
    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Debug controls</div>', unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3, gap="small")
    with c1:
        if st.button("Toggle debug", use_container_width=True):
            st.session_state.show_debug = not st.session_state.show_debug
            st.rerun()
    with c2:
        if st.button("Clear cache", use_container_width=True):
            st.cache_data.clear()
            st.session_state.status_notice = "Cache geleegd."
            st.rerun()
    with c3:
        if st.button("Reset app state", use_container_width=True):
            for key, value in SESSION_DEFAULTS.items():
                st.session_state[key] = value
            st.rerun()

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.json(
        {
            "source_mode": source_mode,
            "snapshot_mode": snapshot_mode,
            "history_rows": len(history_df),
            "real_rows": len(real_df),
            "sim_rows": len(sim_df),
            "shadow_rows": len(shadow_df),
            "db_ready": db_ready(),
            "tables": {
                "experience_trades": table_count("experience_trades"),
                "experience_scoreboard": table_count("experience_scoreboard"),
                "pending_approvals": table_count("pending_approvals"),
            },
        }
    )
    st.markdown("</div>", unsafe_allow_html=True)


# ==========================================================
# DATA LOAD
# ==========================================================
real_df, sim_df, shadow_df, history_df, source_mode = get_all_trade_data()
snapshot, snapshot_mode = read_snapshot_with_fallback()
assets_df = prepare_assets_df(snapshot)
orders_df = load_pending_orders_db()
scoreboard_df = load_scoreboard_db()
source_counts_df = load_source_counts()
feed = build_activity_feed(orders_df, real_df, sim_df, shadow_df)

history_summary = performance_summary(history_df)
live_summary = performance_summary(real_df)
overall_trade_summary = overall_trade_win_summary(history_df)
euro_sim5_summary = euro_five_sim_summary(history_df, stake_eur=5.0)
real_money_summary = real_trade_money_summary(real_df)

eur_available = safe_float(snapshot.get("eur_available"), 0.0)
total_portfolio_eur = safe_float(snapshot.get("total_portfolio_eur"), 0.0)


# ==========================================================
# HEADER / METRICS
# ==========================================================
st.markdown('<div class="shell">', unsafe_allow_html=True)
st.markdown(
    """
    <div class="topbar">
        <div class="brand">
            <div class="brand-mark"></div>
            <div class="brand-title">Crypto AI Terminal</div>
        </div>
        <div class="top-icons">
            <div class="top-icon-chip">◫</div>
            <div class="top-icon-chip">⚙</div>
            <div class="top-icon-chip">⠿</div>
            <div class="avatar">👤</div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

metric_cols = st.columns([1.0, 0.95, 0.9, 0.9, 0.62, 0.62, 0.88], gap="small")
with metric_cols[0]:
    st.markdown(metric_card_html("Total Portfolio Value", format_money(total_portfolio_eur), "blue"), unsafe_allow_html=True)
with metric_cols[1]:
    st.markdown(metric_card_html("Available EUR Balance", format_money(eur_available), "green"), unsafe_allow_html=True)
with metric_cols[2]:
    st.markdown(metric_card_html("Live Realized Profit", format_r(live_summary["total_r"]), "green"), unsafe_allow_html=True)
with metric_cols[3]:
    st.markdown(metric_card_html("Average R per Live Trade", f"{live_summary['avg_r']:.2f} R", "purple"), unsafe_allow_html=True)
with metric_cols[4]:
    st.plotly_chart(
        render_donut(
            safe_float(overall_trade_summary["winrate"], 0.0),
            "Trade Win Rate",
            "#34d399",
            subtitle=f"{int(overall_trade_summary['wins'])} win / {int(overall_trade_summary['losses'])} loss",
            height=118,
        ),
        use_container_width=True,
        config={"displayModeBar": False},
    )
with metric_cols[5]:
    st.plotly_chart(
        render_donut(
            safe_float(euro_sim5_summary["money_winrate"], 0.0),
            "Euro Win Rate",
            "#60a5fa",
            subtitle=f"{format_money(euro_sim5_summary['total_eur'])} @ €5/trade",
            height=118,
        ),
        use_container_width=True,
        config={"displayModeBar": False},
    )
with metric_cols[6]:
    st.markdown(metric_card_html("Maximum Drawdown", f"{live_summary['max_drawdown']:.2f} R", "red"), unsafe_allow_html=True)

combined_row = st.columns([0.42, 1.35, 0.95], gap="small")
with combined_row[1]:
    st.plotly_chart(
        chart_combined_performance_donut(real_df if not real_df.empty else pd.DataFrame([]), "REAL Performance"),
        use_container_width=True,
        config={"displayModeBar": False},
    )
with combined_row[2]:
    render_real_stats_panel(real_money_summary)

st.caption("Bovenste kleine donuts: links = algemene winrate over alle gesloten trades. Midden = euro-simulatie over alle gesloten trades met €5 vaste inzet per trade. Grote donut + statistieken = alleen echte live trades.")

# ==========================================================
# MAIN LAYOUT
# ==========================================================
left_col, main_col, right_col = st.columns([0.78, 2.12, 0.98], gap="small")

with left_col:
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="page-chip">Nu actief: {}</div>'.format(page_display_name(st.session_state.page)), unsafe_allow_html=True)
    st.markdown('<div class="nav-header">Trading Overview</div>', unsafe_allow_html=True)
    st.markdown('<div class="nav-caption">Klikbare navigatie. De actieve pagina krijgt een duidelijke highlight zodat je altijd ziet waar je zit.</div>', unsafe_allow_html=True)

    hk1, hk2 = st.columns(2, gap="small")
    with hk1:
        st.markdown('<div class="tiny-button">', unsafe_allow_html=True)
        if st.button("W ⚡ Volgende trade", use_container_width=True):
            if not history_df.empty:
                ids = history_df["trade_id"].astype(str).tolist()
                current = st.session_state.selected_page_trade_id
                if current in ids:
                    idx = ids.index(current)
                    st.session_state.selected_page_trade_id = ids[(idx + 1) % len(ids)]
                else:
                    st.session_state.selected_page_trade_id = ids[0]
                st.session_state.status_notice = "Volgende trade geselecteerd."
            else:
                st.session_state.status_notice = "Geen trades beschikbaar."
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
    with hk2:
        st.markdown('<div class="tiny-button">', unsafe_allow_html=True)
        if st.button("D 🧠 Debug", use_container_width=True):
            st.session_state.show_debug = not st.session_state.show_debug
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    hk3, hk4 = st.columns(2, gap="small")
    with hk3:
        st.markdown('<div class="tiny-button">', unsafe_allow_html=True)
        if st.button("S 📖 Help", use_container_width=True):
            st.session_state.page = "help"
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
    with hk4:
        st.markdown('<div class="tiny-button">', unsafe_allow_html=True)
        if st.button("Reset app", use_container_width=True):
            for key, value in SESSION_DEFAULTS.items():
                st.session_state[key] = value
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    nav_button("◉ Dashboard", "dashboard")
    nav_button("◉ Live Performance", "live")
    nav_button("◉ Simulator", "sim")
    nav_button("◉ Shadow Review", "shadow")
    nav_button("◉ Portfolio", "portfolio")
    nav_button("◉ Help & Data Mapping", "help")

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    if st.button("↳ Log Out", key="logout_button", use_container_width=True):
        st.session_state.clear()
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

with main_col:
    if st.session_state.page == "dashboard":
        render_dashboard_page(history_df, real_df, sim_df, shadow_df, scoreboard_df)
    elif st.session_state.page == "live":
        render_trade_page("live", real_df, include_trade_type=False)
    elif st.session_state.page == "sim":
        render_trade_page("sim", sim_df, include_trade_type=False)
    elif st.session_state.page == "shadow":
        render_trade_page("shadow", shadow_df, include_trade_type=False)
    elif st.session_state.page == "portfolio":
        render_portfolio_page(snapshot, assets_df, snapshot_mode)
    elif st.session_state.page == "help":
        render_help_page(history_df, real_df, sim_df, shadow_df, source_mode, snapshot_mode)
    else:
        st.error("Onbekende pagina.")

with right_col:
    render_activity_panel(feed, source_mode, history_df, real_df, sim_df, shadow_df, snapshot_mode)

st.markdown("</div>", unsafe_allow_html=True)

st.caption("Links zie je Trade Win Rate, daarnaast Euro Win Rate, en daaronder de gecombineerde premium performance-cirkel.")

status_text = (
    f"Data mode: {source_mode} | "
    f"Snapshot mode: {snapshot_mode} | "
    f"DB: {'OK' if db_ready() else 'DEMO/FALLBACK'} | "
    f"History rows: {len(history_df)} | "
    f"Live rows: {len(real_df)} | "
    f"Sim rows: {len(sim_df)} | "
    f"Shadow rows: {len(shadow_df)}"
)
st.caption(status_text)

source_text = (
    "Bronverdeling: "
    + (
        ", ".join([f"{safe_str(r['source'])}:{safe_int(r['n'])}" for _, r in source_counts_df.iterrows()])
        if not source_counts_df.empty
        else "geen brondata beschikbaar"
    )
)
st.caption(source_text)
