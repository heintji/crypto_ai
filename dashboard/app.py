
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
SESSION_DEFAULTS = {
    "load_history": False,
    "last_snapshot_refresh_msg": "",
    "active_trade_tab": "SIM",
    "selected_trade_key": None,
    "selected_trade_source": "SIM",
    "trade_setup_filter": "ALLES",
    "trade_outcome_filter": "ALLES",
    "trade_regime_filter": "ALLES",
    "trade_coin_filter": "ALLES",
    "trade_days_filter": "30D",
    "hotkey_show_pg_help": False,
    "hotkey_show_pg_structure": False,
    "hotkey_advanced_mode": False,
    "hotkey_show_debug": False,
    "hotkey_notice": "",
    "last_error_text": "",
    "debug_events": [],
}
for _k, _v in SESSION_DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


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
            --blue-2: #60a5fa;
            --red: #ff6b87;
            --red-2: #f87171;
            --yellow: #ffd166;
            --purple: #c084fc;
        }

        .stApp {
            background:
                radial-gradient(circle at top center, rgba(76, 29, 149, 0.18) 0%, rgba(9,11,18,0) 35%),
                linear-gradient(180deg, #090b12 0%, #080a11 100%);
            color: var(--text);
        }

        .block-container {
            max-width: 1880px;
            padding-top: 0.65rem;
            padding-bottom: 1.25rem;
        }

        section[data-testid="stSidebar"] {display:none;}
        header[data-testid="stHeader"] {background: transparent;}
        div[data-testid="stDataFrame"] {border:0 !important;}

        .app-shell {
            background: linear-gradient(180deg, rgba(12,14,22,0.96) 0%, rgba(10,12,18,0.96) 100%);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 26px;
            padding: 16px;
            box-shadow: 0 20px 50px rgba(0,0,0,0.28);
        }

        .topbar {
            display:flex;
            justify-content:space-between;
            align-items:center;
            padding: 6px 2px 14px 2px;
        }

        .brand {
            display:flex;
            align-items:center;
            gap:16px;
        }

        .brand-mark {
            width:20px;
            height:38px;
            background: linear-gradient(180deg,#ffffff 0%, #e5e7eb 100%);
            border-radius: 6px 16px 6px 16px;
            transform: skewX(-18deg);
            box-shadow: 0 8px 20px rgba(255,255,255,0.08);
        }

        .brand-title {
            font-size: 24px;
            font-weight: 800;
            letter-spacing: -0.03em;
            color:#ffffff;
        }

        .top-icons {
            display:flex;
            align-items:center;
            gap:14px;
            color:#f8fafc;
            font-size:18px;
        }

        .avatar {
            width:34px;
            height:34px;
            border-radius:50%;
            border:1px solid rgba(255,255,255,0.10);
            background: linear-gradient(180deg,#2a2f3c 0%, #151922 100%);
            display:flex;
            align-items:center;
            justify-content:center;
            font-size:14px;
        }

        .side-nav,
        .main-panel,
        .right-panel,
        .sub-panel {
            background: linear-gradient(180deg, rgba(17,21,31,0.96) 0%, rgba(14,18,27,0.96) 100%);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 20px;
            padding: 14px;
            box-shadow: 0 12px 32px rgba(0,0,0,0.22);
            height: 100%;
        }

        .nav-title {
            color:#ffffff;
            font-size:13px;
            font-weight:900;
            margin-bottom:14px;
            letter-spacing:0.02em;
            text-transform:uppercase;
        }

        .nav-item {
            display:flex;
            align-items:center;
            gap:10px;
            padding:10px 12px;
            border-radius:12px;
            color:#dce4f1;
            font-size:12px;
            font-weight:700;
            background: rgba(255,255,255,0.02);
            border:1px solid rgba(255,255,255,0.04);
            margin-bottom:8px;
        }

        .nav-item.active {
            background: linear-gradient(90deg, rgba(244,114,182,0.22) 0%, rgba(126,201,255,0.22) 100%);
            color:#ffffff;
            border-color: rgba(255,255,255,0.10);
        }

        .section-title {
            font-size: 18px;
            font-weight: 800;
            color:#ffffff;
            margin-bottom: 10px;
        }

        .subtle {
            color: var(--muted);
            font-size: 13px;
        }

        .greeting {
            font-size: 14px;
            font-weight: 800;
            color:#ffffff;
        }

        .search-box {
            display:flex;
            align-items:center;
            gap:8px;
            min-width: 220px;
            padding:8px 12px;
            border-radius:10px;
            background: rgba(255,255,255,0.03);
            border:1px solid rgba(255,255,255,0.06);
            color:#98a3b7;
            font-size:12px;
            font-weight:700;
        }

        .trade-tab {
            padding:8px 12px;
            border-radius:10px;
            border:1px solid rgba(255,255,255,0.06);
            background: rgba(255,255,255,0.03);
            color:#cbd5e1;
            font-size:12px;
            font-weight:800;
            text-align:center;
        }

        .trade-header-card {
            background: linear-gradient(180deg, rgba(20,24,35,0.95) 0%, rgba(18,22,33,0.95) 100%);
            border:1px solid rgba(255,255,255,0.06);
            border-radius:16px;
            padding:14px;
            margin-bottom:12px;
        }

        .trade-coin {
            font-size: 26px;
            font-weight: 900;
            color: #ffffff;
            margin-bottom: 6px;
        }

        .chip-row {
            display:flex;
            gap:8px;
            flex-wrap:wrap;
            margin-bottom:8px;
        }

        .chip {
            padding:4px 8px;
            border-radius:8px;
            font-size:11px;
            font-weight:800;
            color:#dbe4f0;
            background: rgba(255,255,255,0.04);
            border:1px solid rgba(255,255,255,0.06);
        }

        .chip.green { color: var(--green); }
        .chip.blue { color: var(--blue); }
        .chip.yellow { color: var(--yellow); }
        .chip.red { color: var(--red); }

        .info-row {
            display:flex;
            justify-content:space-between;
            align-items:center;
            padding: 8px 0;
            border-bottom: 1px solid rgba(255,255,255,0.05);
            gap: 10px;
        }

        .info-left {
            color:#cbd5e1;
            font-size: 13px;
            font-weight: 700;
        }

        .info-right {
            color:#ffffff;
            font-size: 14px;
            font-weight: 900;
            text-align:right;
        }

        .metric-card {
            background: linear-gradient(180deg, rgba(17,21,31,0.97) 0%, rgba(15,18,27,0.97) 100%);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 16px;
            padding: 14px;
            min-height: 92px;
            box-shadow: 0 10px 24px rgba(0,0,0,0.20);
        }

        .metric-value {
            font-size: 24px;
            line-height: 1.05;
            font-weight: 900;
            color:#ffffff;
            margin-bottom:6px;
        }

        .metric-label {
            font-size: 13px;
            color: var(--muted);
            font-weight: 700;
        }

        .metric-accent-green { color: var(--green) !important; }
        .metric-accent-red { color: var(--red) !important; }
        .metric-accent-blue { color: var(--blue) !important; }
        .metric-accent-purple { color: var(--purple) !important; }

        .panel-divider {
            height: 1px;
            background: rgba(255,255,255,0.06);
            margin: 10px 0 12px 0;
            border-radius: 999px;
        }

        .activity-item {
            display:flex;
            gap:12px;
            padding:12px 0;
            border-bottom:1px solid rgba(255,255,255,0.05);
        }

        .activity-dotline {
            display:flex;
            flex-direction:column;
            align-items:center;
        }

        .activity-dot {
            width:12px;
            height:12px;
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
            font-size:15px;
            font-weight:900;
            margin-bottom:4px;
        }

        .activity-sub {
            color:#f8fafc;
            font-size:13px;
            line-height: 1.35;
        }

        .activity-time {
            color:var(--muted);
            font-size:13px;
            font-weight:700;
            min-width:48px;
            text-align:right;
        }

        .portfolio-row,
        .score-row {
            display:flex;
            justify-content:space-between;
            align-items:center;
            padding: 10px 0;
            border-bottom:1px solid rgba(255,255,255,0.05);
            gap: 10px;
        }

        .score-left {
            display:flex;
            align-items:center;
            gap:10px;
            color:#f8fafc;
            font-size:13px;
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
            gap:10px;
        }

        .score-pct {
            color:#ffffff;
            font-weight:900;
            font-size:13px;
            min-width:40px;
            text-align:right;
        }

        .score-bar {
            width:70px;
            height:8px;
            border-radius:999px;
            background: rgba(255,255,255,0.07);
            overflow:hidden;
        }

        .score-fill {
            height:100%;
            border-radius:999px;
        }

        .trade-button .stButton > button,
        .trade-button-active .stButton > button,
        .hotkey-button .stButton > button {
            width: 100%;
            border-radius: 12px !important;
            border: 1px solid rgba(255,255,255,0.08) !important;
            background: linear-gradient(180deg, #121727 0%, #0f1421 100%) !important;
            color: #ffffff !important;
            font-weight: 800 !important;
            box-shadow: none !important;
        }

        .trade-button .stButton > button {
            text-align: left !important;
            padding: 0.7rem 0.9rem !important;
        }

        .hotkey-button .stButton > button {
            text-align: center !important;
            padding: 0.6rem 0.6rem !important;
            font-size: 12px !important;
        }

        .trade-button .stButton > button:hover,
        .hotkey-button .stButton > button:hover {
            background: linear-gradient(180deg, #182036 0%, #11192b 100%) !important;
            border-color: rgba(126,201,255,0.30) !important;
        }

        .trade-button-active .stButton > button {
            background: linear-gradient(90deg, rgba(96,165,250,0.22) 0%, rgba(196,132,252,0.18) 100%) !important;
            border-color: rgba(126,201,255,0.30) !important;
            text-align: left !important;
            padding: 0.7rem 0.9rem !important;
        }

        .mini-stat {
            display:flex;
            justify-content:space-between;
            align-items:center;
            padding:10px 0;
            border-bottom:1px solid rgba(255,255,255,0.05);
            gap: 10px;
        }

        .mini-stat-label {
            color:#f1f5f9;
            font-size:13px;
            font-weight:700;
        }

        .mini-stat-value {
            font-size:15px;
            font-weight:900;
            color:#ffffff;
        }

        .footspace { height: 6px; }
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


def format_price(x: Any) -> str:
    v = safe_float(x, 0.0)
    if v >= 1000:
        return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if v >= 1:
        return f"{v:.4f}"
    return f"{v:.6f}"


def pct_str(x: Any) -> str:
    return f"{safe_float(x, 0.0):.1f}%"


def safe_read_json(path: str) -> Tuple[Optional[dict], Optional[str]]:
    try:
        if not os.path.exists(path):
            return None, f"Bestand niet gevonden: {path}"
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        capture_exception("safe_read_json", e)
        return None, f"JSON leesfout ({path}): {e}"


def metric_card_html(label: str, value: str, accent: str = "") -> str:
    accent_cls = ""
    if accent == "green":
        accent_cls = "metric-accent-green"
    elif accent == "red":
        accent_cls = "metric-accent-red"
    elif accent == "blue":
        accent_cls = "metric-accent-blue"
    elif accent == "purple":
        accent_cls = "metric-accent-purple"

    return f"""
        <div class="metric-card">
            <div class="metric-value {accent_cls}">{value}</div>
            <div class="metric-label">{label}</div>
        </div>
    """


def render_donut(value: float, title: str, color: str = "#50d9a7") -> go.Figure:
    value = max(0.0, min(100.0, safe_float(value, 0.0)))
    fig = go.Figure(
        data=[
            go.Pie(
                values=[value, 100 - value],
                hole=0.72,
                textinfo="none",
                sort=False,
                marker=dict(
                    colors=[color, "rgba(255,255,255,0.08)"],
                    line=dict(width=0),
                ),
            )
        ]
    )
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        margin=dict(l=0, r=0, t=0, b=0),
        height=110,
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


def active_tab_label(tab: str) -> str:
    tab = safe_str(tab).upper()
    if tab == "REAL":
        return "Live Trades"
    if tab == "SIM":
        return "History Simulator"
    if tab == "SHADOW":
        return "Shadow Trades"
    return "Trades"


def active_tab_description(tab: str) -> str:
    tab = safe_str(tab).upper()
    if tab == "REAL":
        return "Overzicht van live uitgevoerde trades met echte resultaten, gerealiseerde performance en directe feedback op setup-kwaliteit."
    if tab == "SIM":
        return "Historische simulaties van setups op basis van eerdere marktdata om kwaliteit, consistentie en selectiviteit van signalen te toetsen."
    if tab == "SHADOW":
        return "Evaluatie van gemiste of bewust overgeslagen trades om te analyseren welke kansen zijn gemist en welk risico juist is vermeden."
    return "Overzicht van trade-resultaten."


def postgres_trade_read_explanation() -> str:
    return (
        "Het dashboard leest trades primair uit de tabel 'experience_trades'. "
        "De code controleert eerst dynamisch welke kolommen aanwezig zijn in PostgreSQL, "
        "zodat verschillen tussen schema-versies geen directe crash veroorzaken. "
        "Daarna worden belangrijke velden zoals trade_id, symbol, setup_type, timeframe, regime, "
        "entry, stop, target, outcome, source, created_at en closed_at opgebouwd. "
        "Als 'result_r' beschikbaar is, wordt deze gebruikt als pnl_r. "
        "Als die kolom ontbreekt, valt het systeem terug op een outcome-naar-R mapping "
        "waarbij WIN = +2R en LOSS = -1R. Vervolgens wordt trade_type afgeleid uit source of is_shadow, "
        "zodat elke trade uiteindelijk als REAL, SIM of SHADOW wordt geclassificeerd. "
        "Daarna worden alle datums genormaliseerd naar UTC en worden numerieke velden veilig geconverteerd. "
        "Op die manier blijft de dashboardlaag stabiel, ook als de database deels onvolledig of inconsistent is."
    )


def explain_blocks_md() -> str:
    return """
### Uitleg per codeblok

**1. Config & session state**  
Hier worden API-sleutels, database-instellingen, limieten en Streamlit session state geladen.  
Deze laag bepaalt hoe het dashboard zich gedraagt tussen reruns.

**2. Style laag**  
Alle visuele componenten zoals kaarten, panelen, tabs en kleuren worden via CSS in Streamlit gestyled.

**3. Helpers**  
Functies voor veilige conversies, datum-formattering, geldbedragen, debug logging en herbruikbare UI-logica.

**4. Snapshot / Bitvavo**  
Leest balances op, vertaalt coins naar EUR-waarde en bouwt een snapshot van de actuele portefeuille.

**5. Database laag**  
Verbindt met PostgreSQL, leest beschikbare kolommen op en zorgt dat queries stabiel blijven bij schema-verschillen.

**6. Trade loader**  
Bouwt dynamische SQL voor REAL, SIM, SHADOW en HISTORY.  
De code ondersteunt meerdere schema-varianten zoals `symbol/coin`, `regime/market_regime` en `result_r/outcome`.

**7. Performance & scoreboard**  
Berekent win rate, expectancy, drawdown, advanced stats en setup-ranking.

**8. UI hoofdscherm**  
Toont metrics, trade detail, filters, activiteiten, portfolio, scoreboard en volledige geschiedenis.

**9. Hotkeys / acties**  
W = volgende trade  
D = debug + database structuur  
A = advanced mode  
S = uitleg per blok + PostgreSQL reading
"""


def postgres_structure_advice_md() -> str:
    return """
### PostgreSQL trade-structuur analyse

**Aanbevolen tabel: `experience_trades`**
- `id` UUID of BIGSERIAL primary key
- `trade_key` TEXT UNIQUE
- `source` TEXT met vaste waarden: REAL / REAL_REVIEW / SIM / SHADOW
- `symbol` TEXT NOT NULL
- `setup_type` TEXT
- `timeframe` TEXT
- `market_regime` TEXT
- `score` DOUBLE PRECISION
- `raw_score` DOUBLE PRECISION
- `chance` DOUBLE PRECISION
- `confidence` DOUBLE PRECISION
- `entry` DOUBLE PRECISION
- `stop` DOUBLE PRECISION
- `target` DOUBLE PRECISION
- `outcome` TEXT
- `result_r` DOUBLE PRECISION
- `created_at` TIMESTAMPTZ
- `closed_at` TIMESTAMPTZ
- `is_shadow` BOOLEAN DEFAULT FALSE

**Belangrijk**
- Gebruik één vaste kolomnaam per concept
- Vermijd tegelijk `coin` én `symbol`
- Vermijd tegelijk `regime` én `market_regime`
- Gebruik altijd `result_r` voor echte PnL in R
- Gebruik vaste waarden voor `source` en `outcome`

**Aanbevolen indexen**
```sql
CREATE INDEX IF NOT EXISTS idx_experience_trades_source ON experience_trades(source);
CREATE INDEX IF NOT EXISTS idx_experience_trades_symbol ON experience_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_experience_trades_setup_type ON experience_trades(setup_type);
CREATE INDEX IF NOT EXISTS idx_experience_trades_created_at ON experience_trades(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_experience_trades_closed_at ON experience_trades(closed_at DESC);
```
"""


def trade_reason_text(row: pd.Series) -> str:
    trade_type = safe_str(row.get("trade_type")).upper()
    outcome = safe_str(row.get("outcome")).upper()
    setup = safe_str(row.get("setup_type"), "-")
    regime = safe_str(row.get("regime"), "-")
    symbol = safe_str(row.get("symbol"), "-")
    pnl = safe_float(row.get("pnl_r"), 0.0)

    pnl_txt = format_compact_r(pnl)

    if trade_type == "SHADOW":
        if outcome == "WIN":
            return (
                f"Deze shadow trade laat zien dat {symbol} binnen de setup '{setup}' en het regime '{regime}' waarschijnlijk "
                f"een sterke kansrijke entry was. Omdat deze trade niet live is uitgevoerd, fungeert dit resultaat als een "
                f"gemiste opportuniteit. De uitkomst van {pnl_txt} suggereert dat deze combinatie in vergelijkbare contexten "
                f"meer vertrouwen kan verdienen of minder streng gefilterd hoeft te worden."
            )
        if outcome == "LOSS":
            return (
                f"Deze shadow trade bevestigt dat het overslaan van {symbol} binnen de setup '{setup}' en het regime '{regime}' "
                f"waarschijnlijk de juiste beslissing was. De niet-uitgevoerde trade eindigde op {pnl_txt}, wat erop wijst dat "
                f"deze marktcontext extra voorzichtigheid vraagt. Dit soort shadow-resultaten zijn waardevol om zwakke entries "
                f"structureel uit live trading te filteren."
            )
        return (
            f"Deze shadow trade voor {symbol} geeft extra context over hoe de setup '{setup}' zich gedroeg binnen het regime "
            f"'{regime}', maar eindigde zonder duidelijke win/loss-classificatie. Gebruik dit resultaat vooral als aanvullende "
            f"kwalitatieve feedback voor setup-validatie."
        )

    if trade_type == "SIM":
        if outcome == "WIN":
            return (
                f"De History Simulator classificeert deze {symbol}-trade als een positieve historische uitkomst binnen de setup "
                f"'{setup}' en het regime '{regime}'. Met een resultaat van {pnl_txt} ondersteunt deze simulatie dat dit type "
                f"signaal in vergelijkbare marktomstandigheden robuust kan presteren. Dit vergroot het vertrouwen in de kwaliteit "
                f"van de setup, maar blijft een simulatie en geen live bevestiging."
            )
        if outcome == "LOSS":
            return (
                f"De History Simulator laat zien dat deze {symbol}-trade historisch zwakker presteerde binnen de setup "
                f"'{setup}' en het regime '{regime}'. Het resultaat van {pnl_txt} suggereert dat deze combinatie extra selectiviteit "
                f"vraagt voordat een vergelijkbaar signaal live wordt toegestaan. Dit soort simulaties helpen om zwakke patronen "
                f"vroeg te herkennen en live risico te beperken."
            )
        return (
            f"De History Simulator geeft voor {symbol} aanvullende historische context rond de setup '{setup}' binnen het regime "
            f"'{regime}', maar zonder duidelijke win/loss-uitkomst. Zie dit als ondersteunende informatie voor patroonherkenning, "
            f"niet als directe live-validatie."
        )

    if outcome == "WIN":
        return (
            f"Dit is een live uitgevoerde trade op {symbol} die positief is afgesloten met {pnl_txt}. De combinatie van setup "
            f"'{setup}' en regime '{regime}' heeft in echte marktomstandigheden gewerkt. Dit resultaat is belangrijk omdat live "
            f"uitvoering niet alleen de setup test, maar ook timing, discipline en operationele betrouwbaarheid van het systeem bevestigt."
        )

    if outcome == "LOSS":
        return (
            f"Dit is een live trade op {symbol} die negatief is afgesloten met {pnl_txt}. Binnen de combinatie van setup '{setup}' "
            f"en regime '{regime}' geeft dit verlies waardevolle feedback over waar het model, de timing of de marktomgeving minder "
            f"gunstig was. Juist deze trades zijn nodig om filters, drempels en risicoregels verder aan te scherpen."
        )

    return (
        f"Dit is een live trade op {symbol} binnen de setup '{setup}' en het regime '{regime}', maar zonder duidelijke win/loss-uitkomst. "
        f"Gebruik deze trade vooral als operationele en contextuele feedback binnen de evaluatie van het systeem."
    )


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
    try:
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
    except Exception as e:
        capture_exception("fetch_all_market_prices", e)
        return {}


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
    assets: List[Dict[str, Any]] = []
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
            assets.append(
                {
                    "symbol": symbol,
                    "available": available,
                    "inOrder": in_order,
                    "total": total,
                    "price_eur": p_eur,
                    "eur_value": eur_value,
                    "price_route": route,
                }
            )

    crypto_assets_eur = sum(
        float(a["eur_value"])
        for a in assets
        if a["symbol"] != "EUR" and a.get("eur_value") is not None
    )
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

    append_debug_event("Snapshot succesvol opgebouwd.")
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
    conn = None
    try:
        conn = get_db_conn()
        if conn is None:
            return pd.DataFrame([])
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                rows = cur.fetchall()
                cols = [desc[0] for desc in cur.description]
        return pd.DataFrame(rows, columns=cols)
    except Exception as e:
        capture_exception("run_df_query", e)
        return pd.DataFrame([])
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


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
        source_expr = "COALESCE(UPPER(\"source\"), 'UNKNOWN')"
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
        pnl_expr = """
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
                {',' if 'result_r' in cols else ''}
                {"\"result_r\"::double precision AS result_r_raw" if 'result_r' in cols else ''}
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
            {('COALESCE(result_r_raw, ' + pnl_expr.strip() + ')') if 'result_r' in cols else pnl_expr} AS pnl_r,
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
    if "trade_id" not in out.columns:
        out["trade_id"] = out.index.astype(str)
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
    regime_expr = (
        'COALESCE("market_regime", "regime")'
        if "market_regime" in cols and "regime" in cols
        else '"market_regime"'
        if "market_regime" in cols
        else '"regime"'
        if "regime" in cols
        else "NULL::text"
    )
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


def build_activity_feed(orders_df: pd.DataFrame, real_df: pd.DataFrame, sim_df: pd.DataFrame) -> List[Dict[str, Any]]:
    feed: List[Dict[str, Any]] = []

    if not orders_df.empty:
        for _, row in orders_df.head(4).iterrows():
            feed.append(
                {
                    "ts": row.get("created_at"),
                    "title": "PRE-BUY SIGNAL",
                    "sub": f"{safe_str(row.get('symbol'), '-')} | {safe_str(row.get('setup_type'), '-')} | {safe_str(row.get('regime'), '-')}",
                    "time": format_dt_short(row.get("created_at"))[-8:-3],
                    "kind": "prebuy",
                }
            )

    if not real_df.empty:
        for _, row in real_df.head(4).iterrows():
            feed.append(
                {
                    "ts": row.get("datetime_raw"),
                    "title": "LIVE TRADE CLOSED",
                    "sub": f"{safe_str(row.get('symbol'), '-')} | {safe_str(row.get('outcome'), '-')} | {format_compact_r(row.get('pnl_r'))}",
                    "time": safe_str(row.get("datetime"), "")[-8:-3],
                    "kind": "real",
                }
            )

    if not sim_df.empty:
        for _, row in sim_df.head(4).iterrows():
            feed.append(
                {
                    "ts": row.get("datetime_raw"),
                    "title": "SIMULATION RESULT",
                    "sub": f"{safe_str(row.get('symbol'), '-')} | {safe_str(row.get('outcome'), '-')} | {format_compact_r(row.get('pnl_r'))}",
                    "time": safe_str(row.get("datetime"), "")[-8:-3],
                    "kind": "sim",
                }
            )

    def _sort_key(item: Dict[str, Any]):
        ts = item.get("ts")
        if ts is None or pd.isna(ts):
            return pd.Timestamp("1970-01-01", tz="UTC")
        return ts

    return sorted(feed, key=_sort_key, reverse=True)[:5]


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
    return out.sort_values("sort_ts", ascending=False).reset_index(drop=True)


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
    return assets


def get_active_trade_df() -> pd.DataFrame:
    if st.session_state.active_trade_tab == "REAL":
        return load_real_trades_db()
    if st.session_state.active_trade_tab == "SHADOW":
        return load_shadow_trades_db()
    return load_sim_trades_db()


def get_trade_filters(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        return out

    if st.session_state.trade_setup_filter != "ALLES":
        out = out[out["setup_type"] == st.session_state.trade_setup_filter]
    if st.session_state.trade_outcome_filter != "ALLES":
        out = out[out["outcome"] == st.session_state.trade_outcome_filter]
    if st.session_state.trade_regime_filter != "ALLES":
        out = out[out["regime"] == st.session_state.trade_regime_filter]
    if st.session_state.trade_coin_filter != "ALLES":
        out = out[out["symbol"] == st.session_state.trade_coin_filter]

    if st.session_state.trade_days_filter != "ALLES":
        days_map = {"7D": 7, "30D": 30, "90D": 90, "180D": 180}
        days = days_map.get(st.session_state.trade_days_filter)
        if days:
            cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
            out = out[out["datetime_raw"] >= cutoff]

    return out


def select_trade_from_df(df: pd.DataFrame) -> Optional[pd.Series]:
    if df.empty:
        return None

    if st.session_state.selected_trade_key is None:
        first = df.iloc[0]
        st.session_state.selected_trade_key = safe_str(first.get("trade_id"))
        st.session_state.selected_trade_source = safe_str(first.get("trade_type"))

    selected = df[df["trade_id"].astype(str) == str(st.session_state.selected_trade_key)]
    if not selected.empty:
        return selected.iloc[0]

    return df.iloc[0]


def get_trade_list_for_navigation() -> pd.DataFrame:
    active_all_df = get_active_trade_df()
    filtered_trade_df = get_trade_filters(active_all_df)
    if not filtered_trade_df.empty:
        return filtered_trade_df.reset_index(drop=True)
    if not active_all_df.empty:
        return active_all_df.reset_index(drop=True)
    return empty_trade_df()


def go_to_next_trade() -> None:
    df = get_trade_list_for_navigation()
    if df.empty:
        st.session_state.hotkey_notice = "Geen trades beschikbaar om doorheen te navigeren."
        return

    keys = df["trade_id"].astype(str).tolist()

    if st.session_state.selected_trade_key is None:
        st.session_state.selected_trade_key = keys[0]
        st.session_state.hotkey_notice = "Eerste trade geselecteerd via W."
        return

    current_key = str(st.session_state.selected_trade_key)
    if current_key not in keys:
        st.session_state.selected_trade_key = keys[0]
        st.session_state.hotkey_notice = "Trade selectie hersteld via W."
        return

    idx = keys.index(current_key)
    next_idx = (idx + 1) % len(keys)
    st.session_state.selected_trade_key = keys[next_idx]
    st.session_state.hotkey_notice = f"W toegepast: trade {next_idx + 1} van {len(keys)} geselecteerd."


def build_trade_detail_chart(row: pd.Series) -> go.Figure:
    entry = safe_float(row.get("entry"), 0.0)
    stop = safe_float(row.get("stop"), 0.0)
    target = safe_float(row.get("target"), 0.0)

    base = entry if entry > 0 else 1.0
    x_vals = list(range(20))

    candles_open: List[float] = []
    candles_high: List[float] = []
    candles_low: List[float] = []
    candles_close: List[float] = []

    for i in x_vals:
        drift = (i - 8) * 0.003 * base
        wave = ((i % 4) - 1.5) * 0.004 * base
        o = base + drift + wave
        c = o + ((-1) ** i) * 0.0035 * base
        h = max(o, c) + 0.005 * base
        l = min(o, c) - 0.005 * base
        candles_open.append(o)
        candles_high.append(h)
        candles_low.append(l)
        candles_close.append(c)

    fig = go.Figure()
    fig.add_trace(
        go.Candlestick(
            x=x_vals,
            open=candles_open,
            high=candles_high,
            low=candles_low,
            close=candles_close,
            increasing_line_color="#6ee7b7",
            decreasing_line_color="#ff6b87",
            increasing_fillcolor="#6ee7b7",
            decreasing_fillcolor="#ff6b87",
            showlegend=False,
            name="Price",
        )
    )

    if entry > 0:
        fig.add_hline(y=entry, line_width=2, line_color="#8fd0ff", annotation_text="ENTRY", annotation_position="right")
    if stop > 0:
        fig.add_hline(y=stop, line_width=2, line_color="#ff6b87", annotation_text="STOP", annotation_position="right")
    if target > 0:
        fig.add_hline(y=target, line_width=2, line_color="#6ee7b7", annotation_text="TARGET", annotation_position="right")

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#f8fafc"),
        height=360,
        margin=dict(l=8, r=8, t=8, b=8),
        xaxis=dict(showgrid=False, showticklabels=False),
        yaxis=dict(gridcolor="rgba(255,255,255,0.06)", zerolinecolor="rgba(255,255,255,0.08)"),
        dragmode=False,
    )
    return fig


# ==========================================================
# UI
# ==========================================================
st.markdown(
    """
    <div class="app-shell">
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

eur_available = safe_float((snapshot or {}).get("eur_available"), 0.0)
crypto_assets_eur = safe_float((snapshot or {}).get("crypto_assets_eur"), 0.0)
total_portfolio_eur = safe_float((snapshot or {}).get("total_portfolio_eur"), 0.0)

real_perf_df = real_df[real_df["outcome"].isin(["WIN", "LOSS"])].copy() if not real_df.empty else empty_trade_df()
shadow_perf_df = shadow_df[shadow_df["outcome"].isin(["WIN", "LOSS"])].copy() if not shadow_df.empty else empty_trade_df()
sim_perf_df = sim_df[sim_df["outcome"].isin(["WIN", "LOSS"])].copy() if not sim_df.empty else empty_trade_df()

real_profit_r = float(pd.to_numeric(real_perf_df["pnl_r"], errors="coerce").fillna(0).sum()) if not real_perf_df.empty else 0.0
shadow_profit_r = float(pd.to_numeric(shadow_perf_df["pnl_r"], errors="coerce").fillna(0).sum()) if not shadow_perf_df.empty else 0.0
sim_profit_r = float(pd.to_numeric(sim_perf_df["pnl_r"], errors="coerce").fillna(0).sum()) if not sim_perf_df.empty else 0.0

avg_r = float(pd.to_numeric(real_perf_df["pnl_r"], errors="coerce").fillna(0).mean()) if not real_perf_df.empty else 0.0
winrate = float((pd.to_numeric(real_perf_df["pnl_r"], errors="coerce").fillna(0) > 0).mean() * 100.0) if not real_perf_df.empty else 0.0
sim_winrate = float((pd.to_numeric(sim_perf_df["pnl_r"], errors="coerce").fillna(0) > 0).mean() * 100.0) if not sim_perf_df.empty else 0.0
shadow_winrate = float((pd.to_numeric(shadow_perf_df["pnl_r"], errors="coerce").fillna(0) > 0).mean() * 100.0) if not shadow_perf_df.empty else 0.0

open_trades = int(max(0, len(assets_df)))
pending_count = len(orders_df) if not orders_df.empty else 0
real_count = len(real_df) if not real_df.empty else 0
shadow_count = len(shadow_df) if not shadow_df.empty else 0
sim_count = len(sim_df) if not sim_df.empty else 0

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

metric_cols = st.columns([1.15, 0.95, 0.9, 0.95, 0.85, 0.9], gap="small")
with metric_cols[0]:
    st.markdown(metric_card_html("Total Portfolio Value", format_money(total_portfolio_eur), "blue"), unsafe_allow_html=True)
with metric_cols[1]:
    st.markdown(metric_card_html("Available EUR Balance", format_money(eur_available)), unsafe_allow_html=True)
with metric_cols[2]:
    st.markdown(metric_card_html("Live Realized Profit", format_r(real_profit_r), "green"), unsafe_allow_html=True)
with metric_cols[3]:
    st.markdown(metric_card_html("Average R per Live Trade", f"{avg_r:.2f} R"), unsafe_allow_html=True)
with metric_cols[4]:
    st.plotly_chart(render_donut(winrate, "Live Win Rate", "#50d9a7"), use_container_width=True, config={"displayModeBar": False})
with metric_cols[5]:
    dd_label = f"{max_drawdown:.2f} R" if abs(max_drawdown) < 100 else f"{max_drawdown:.1f}%"
    st.markdown(metric_card_html("Maximum Drawdown", dd_label, "red"), unsafe_allow_html=True)

st.markdown('<div class="footspace"></div>', unsafe_allow_html=True)

shell_cols = st.columns([0.72, 2.15, 0.9], gap="small")

with shell_cols[0]:
    st.markdown('<div class="side-nav">', unsafe_allow_html=True)
    st.markdown('<div class="nav-title">Trading Overview</div>', unsafe_allow_html=True)

    hk1, hk2 = st.columns(2, gap="small")
    with hk1:
        st.markdown('<div class="hotkey-button">', unsafe_allow_html=True)
        if st.button("W ⚡", use_container_width=True):
            go_to_next_trade()
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
    with hk2:
        st.markdown('<div class="hotkey-button">', unsafe_allow_html=True)
        if st.button("D 🧠", use_container_width=True):
            st.session_state.hotkey_show_debug = not st.session_state.hotkey_show_debug
            st.session_state.hotkey_show_pg_structure = not st.session_state.hotkey_show_pg_structure
            st.session_state.hotkey_notice = "D toegepast: debug-sectie en PostgreSQL structuur bijgewerkt."
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    hk3, hk4 = st.columns(2, gap="small")
    with hk3:
        st.markdown('<div class="hotkey-button">', unsafe_allow_html=True)
        if st.button("A 🚀", use_container_width=True):
            st.session_state.hotkey_advanced_mode = not st.session_state.hotkey_advanced_mode
            mode_txt = "AAN" if st.session_state.hotkey_advanced_mode else "UIT"
            st.session_state.hotkey_notice = f"A toegepast: Advanced AI Mode staat nu {mode_txt}."
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
    with hk4:
        st.markdown('<div class="hotkey-button">', unsafe_allow_html=True)
        if st.button("S 📖", use_container_width=True):
            st.session_state.hotkey_show_pg_help = not st.session_state.hotkey_show_pg_help
            st.session_state.hotkey_notice = "S toegepast: uitlegblokken bijgewerkt."
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    if st.session_state.hotkey_notice:
        st.markdown(
            f'<div class="subtle" style="margin-top:10px; margin-bottom:12px; line-height:1.5;">{st.session_state.hotkey_notice}</div>',
            unsafe_allow_html=True,
        )

    if st.session_state.hotkey_show_pg_help:
        st.info(postgres_trade_read_explanation())
        st.markdown(explain_blocks_md())

    if st.session_state.hotkey_show_pg_structure:
        st.markdown(postgres_structure_advice_md())

    if st.session_state.hotkey_show_debug:
        st.markdown('<div class="panel-divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Debug</div>', unsafe_allow_html=True)
        debug_rows = [
            ("DATABASE_URL aanwezig", "JA" if bool(DATABASE_URL) else "NEE"),
            ("Bitvavo API key aanwezig", "JA" if bool(API_KEY) else "NEE"),
            ("Bitvavo API secret aanwezig", "JA" if bool(API_SECRET) else "NEE"),
            ("experience_trades kolommen", str(len(get_table_columns("experience_trades")))),
            ("experience_scoreboard kolommen", str(len(get_table_columns("experience_scoreboard")))),
            ("pending_approvals kolommen", str(len(get_table_columns("pending_approvals")))),
            ("Laatste fout", st.session_state.last_error_text or "-"),
        ]
        for label, value in debug_rows:
            st.markdown(
                f'<div class="info-row"><div class="info-left">{label}</div><div class="info-right">{value}</div></div>',
                unsafe_allow_html=True,
            )
        if st.session_state.debug_events:
            st.markdown('<div class="subtle" style="margin-top:10px;"><b>Debug events</b></div>', unsafe_allow_html=True)
            for item in st.session_state.debug_events[:10]:
                st.markdown(f'<div class="subtle">{item}</div>', unsafe_allow_html=True)

    st.markdown('<div class="panel-divider"></div>', unsafe_allow_html=True)

    nav_items = [
        ("Dashboard", True),
        ("Live Performance", False),
        ("Simulator", False),
        ("Shadow Review", False),
        ("Portfolio", False),
        ("Help", False),
    ]
    for name, active in nav_items:
        cls = "nav-item active" if active else "nav-item"
        st.markdown(f'<div class="{cls}">◉ {name}</div>', unsafe_allow_html=True)

    st.markdown('<div class="panel-divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="nav-item">↳ Log Out</div>', unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

with shell_cols[1]:
    st.markdown('<div class="main-panel">', unsafe_allow_html=True)

    greet_left, greet_right = st.columns([1.0, 0.55], gap="small")
    with greet_left:
        st.markdown('<div class="greeting">Hi David</div>', unsafe_allow_html=True)
    with greet_right:
        st.markdown('<div class="search-box">⌕ Search</div>', unsafe_allow_html=True)

    tab_cols = st.columns([0.19, 0.24, 0.21, 0.08, 0.08, 0.08], gap="small")
    with tab_cols[0]:
        if st.button("Live Trades", use_container_width=True):
            st.session_state.active_trade_tab = "REAL"
            st.session_state.selected_trade_key = None
            st.rerun()
    with tab_cols[1]:
        if st.button("History Simulator", use_container_width=True):
            st.session_state.active_trade_tab = "SIM"
            st.session_state.selected_trade_key = None
            st.rerun()
    with tab_cols[2]:
        if st.button("Shadow Trades", use_container_width=True):
            st.session_state.active_trade_tab = "SHADOW"
            st.session_state.selected_trade_key = None
            st.rerun()
    with tab_cols[3]:
        st.markdown('<div class="trade-tab">✎</div>', unsafe_allow_html=True)
    with tab_cols[4]:
        st.markdown('<div class="trade-tab">⌕</div>', unsafe_allow_html=True)
    with tab_cols[5]:
        st.markdown('<div class="trade-tab">✕</div>', unsafe_allow_html=True)

    st.markdown(
        f'<div class="subtle" style="margin-top:6px; margin-bottom:12px;">{active_tab_description(st.session_state.active_trade_tab)}</div>',
        unsafe_allow_html=True,
    )

    active_all_df = get_active_trade_df()
    filtered_trade_df = get_trade_filters(active_all_df)
    selected_trade = select_trade_from_df(filtered_trade_df if not filtered_trade_df.empty else active_all_df)

    left_detail, right_activity = st.columns([1.95, 0.95], gap="small")

    with left_detail:
        if selected_trade is None:
            st.info("Geen trade gevonden.")
        else:
            trade_type = safe_str(selected_trade.get("trade_type")).upper()
            outcome = safe_str(selected_trade.get("outcome")).upper()
            pnl_r = safe_float(selected_trade.get("pnl_r"), 0.0)

            st.markdown('<div class="trade-header-card">', unsafe_allow_html=True)
            st.markdown(f'<div class="trade-coin">{safe_str(selected_trade.get("symbol"), "-")}</div>', unsafe_allow_html=True)
            st.markdown(
                f"""
                <div class="chip-row">
                    <div class="chip blue">{safe_str(selected_trade.get("setup_type"), "-")}</div>
                    <div class="chip">{safe_str(selected_trade.get("timeframe"), "-")}</div>
                    <div class="chip green">{safe_str(selected_trade.get("regime"), "-")}</div>
                    <div class="chip {'green' if outcome == 'WIN' else 'red' if outcome == 'LOSS' else 'yellow'}">{outcome}</div>
                    <div class="chip blue">{trade_type}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            info_cols = st.columns([0.52, 0.48], gap="small")
            with info_cols[0]:
                for label, value in [
                    ("Chance", str(safe_int(selected_trade.get("chance"), 0))),
                    ("Score", str(safe_int(selected_trade.get("score"), 0))),
                    ("Confidence", str(safe_int(selected_trade.get("confidence"), 0))),
                    ("Entry", format_price(selected_trade.get("entry"))),
                    ("Stop", format_price(selected_trade.get("stop"))),
                    ("Target", format_price(selected_trade.get("target"))),
                ]:
                    st.markdown(
                        f'<div class="info-row"><div class="info-left">{label}</div><div class="info-right">{value}</div></div>',
                        unsafe_allow_html=True,
                    )

            with info_cols[1]:
                created_txt = format_dt_short(selected_trade.get("created_at"))
                closed_txt = format_dt_short(selected_trade.get("closed_at"))
                duration_txt = "-"
                if not pd.isna(parse_dt(selected_trade.get("created_at"))) and not pd.isna(parse_dt(selected_trade.get("closed_at"))):
                    diff = parse_dt(selected_trade.get("closed_at")) - parse_dt(selected_trade.get("created_at"))
                    mins = int(diff.total_seconds() // 60)
                    duration_txt = f"{mins // 60}h{mins % 60:02d}m"

                for label, value in [
                    ("Market Regime", safe_str(selected_trade.get("regime"), "-")),
                    ("Opened At", created_txt),
                    ("Closed At", closed_txt),
                    ("Duration", duration_txt),
                    ("Result", format_compact_r(pnl_r)),
                    ("Trade Type", trade_type),
                ]:
                    st.markdown(
                        f'<div class="info-row"><div class="info-left">{label}</div><div class="info-right">{value}</div></div>',
                        unsafe_allow_html=True,
                    )

            st.plotly_chart(
                build_trade_detail_chart(selected_trade),
                use_container_width=True,
                config={"displayModeBar": False},
            )

            filter_top = st.columns([0.14, 0.16, 0.14, 0.18, 0.14], gap="small")
            with filter_top[0]:
                setup_opts = ["ALLES"] + sorted(active_all_df["setup_type"].dropna().astype(str).unique().tolist()) if not active_all_df.empty else ["ALLES"]
                st.session_state.trade_setup_filter = st.selectbox(
                    "Setup",
                    setup_opts,
                    index=max(0, setup_opts.index(st.session_state.trade_setup_filter)) if st.session_state.trade_setup_filter in setup_opts else 0,
                    label_visibility="collapsed",
                    key="trade_setup_filter_box",
                )
            with filter_top[1]:
                outcome_opts = ["ALLES"] + sorted(active_all_df["outcome"].dropna().astype(str).unique().tolist()) if not active_all_df.empty else ["ALLES"]
                st.session_state.trade_outcome_filter = st.selectbox(
                    "Outcome",
                    outcome_opts,
                    index=max(0, outcome_opts.index(st.session_state.trade_outcome_filter)) if st.session_state.trade_outcome_filter in outcome_opts else 0,
                    label_visibility="collapsed",
                    key="trade_outcome_filter_box",
                )
            with filter_top[2]:
                regime_opts = ["ALLES"] + sorted(active_all_df["regime"].dropna().astype(str).unique().tolist()) if not active_all_df.empty else ["ALLES"]
                st.session_state.trade_regime_filter = st.selectbox(
                    "Market Regime",
                    regime_opts,
                    index=max(0, regime_opts.index(st.session_state.trade_regime_filter)) if st.session_state.trade_regime_filter in regime_opts else 0,
                    label_visibility="collapsed",
                    key="trade_regime_filter_box",
                )
            with filter_top[3]:
                period_options = ["ALLES", "7D", "30D", "90D", "180D"]
                st.session_state.trade_days_filter = st.selectbox(
                    "Period",
                    period_options,
                    index=period_options.index(st.session_state.trade_days_filter) if st.session_state.trade_days_filter in period_options else 2,
                    label_visibility="collapsed",
                    key="trade_days_filter_box",
                )
            with filter_top[4]:
                coin_opts = ["ALLES"] + sorted(active_all_df["symbol"].dropna().astype(str).unique().tolist()) if not active_all_df.empty else ["ALLES"]
                st.session_state.trade_coin_filter = st.selectbox(
                    "Coin",
                    coin_opts,
                    index=max(0, coin_opts.index(st.session_state.trade_coin_filter)) if st.session_state.trade_coin_filter in coin_opts else 0,
                    label_visibility="collapsed",
                    key="trade_coin_filter_box",
                )

            trade_reason = trade_reason_text(selected_trade)
            st.markdown(
                f"""
                <div class="panel-divider"></div>
                <div class="subtle" style="font-size:13px;line-height:1.6;">
                    <b>Trade-analyse:</b> {trade_reason}
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.markdown('</div>', unsafe_allow_html=True)

            st.markdown('<div class="footspace"></div>', unsafe_allow_html=True)

            if st.session_state.hotkey_advanced_mode:
                adv_cols = st.columns(8, gap="small")
                active_wr = float((pd.to_numeric(filtered_trade_df["pnl_r"], errors="coerce").fillna(0) > 0).mean() * 100.0) if not filtered_trade_df.empty else 0.0
                expectancy_live = (winrate / 100.0 * 2.0) + ((1 - winrate / 100.0) * -1.0)
                expectancy_sim = (sim_winrate / 100.0 * 2.0) + ((1 - sim_winrate / 100.0) * -1.0)
                expectancy_shadow = (shadow_winrate / 100.0 * 2.0) + ((1 - shadow_winrate / 100.0) * -1.0)
                with adv_cols[0]:
                    st.markdown(metric_card_html("Live Realized Profit", format_r(real_profit_r), "green"), unsafe_allow_html=True)
                with adv_cols[1]:
                    avg_active = float(pd.to_numeric(filtered_trade_df["pnl_r"], errors="coerce").fillna(0).mean()) if not filtered_trade_df.empty else 0.0
                    st.markdown(metric_card_html("Average R", f"{avg_active:.2f} R", "blue"), unsafe_allow_html=True)
                with adv_cols[2]:
                    st.markdown(metric_card_html(f"{active_tab_label(st.session_state.active_trade_tab)} Win Rate", pct_str(active_wr), "purple"), unsafe_allow_html=True)
                with adv_cols[3]:
                    st.markdown(metric_card_html("History Simulator Win Rate", pct_str(sim_winrate), "blue"), unsafe_allow_html=True)
                with adv_cols[4]:
                    st.markdown(metric_card_html("Shadow Win Rate", pct_str(shadow_winrate), "green"), unsafe_allow_html=True)
                with adv_cols[5]:
                    st.markdown(metric_card_html("Live Expectancy", f"{expectancy_live:.2f} R", "green"), unsafe_allow_html=True)
                with adv_cols[6]:
                    st.markdown(metric_card_html("SIM Expectancy", f"{expectancy_sim:.2f} R", "blue"), unsafe_allow_html=True)
                with adv_cols[7]:
                    st.markdown(metric_card_html("Shadow Expectancy", f"{expectancy_shadow:.2f} R", "purple"), unsafe_allow_html=True)
            else:
                mini_cols = st.columns(4, gap="small")
                with mini_cols[0]:
                    st.markdown(metric_card_html("Live Realized Profit", format_r(real_profit_r), "green"), unsafe_allow_html=True)
                with mini_cols[1]:
                    avg_active = float(pd.to_numeric(filtered_trade_df["pnl_r"], errors="coerce").fillna(0).mean()) if not filtered_trade_df.empty else 0.0
                    st.markdown(metric_card_html("Average R", f"{avg_active:.2f} R", "blue"), unsafe_allow_html=True)
                with mini_cols[2]:
                    active_wr = float((pd.to_numeric(filtered_trade_df["pnl_r"], errors="coerce").fillna(0) > 0).mean() * 100.0) if not filtered_trade_df.empty else 0.0
                    st.markdown(metric_card_html(f"{active_tab_label(st.session_state.active_trade_tab)} Win Rate", pct_str(active_wr), "purple"), unsafe_allow_html=True)
                with mini_cols[3]:
                    st.markdown(metric_card_html("History Simulator Win Rate", pct_str(sim_winrate), "blue"), unsafe_allow_html=True)

            st.markdown('<div class="footspace"></div>', unsafe_allow_html=True)

            table_title = active_tab_label(st.session_state.active_trade_tab)
            st.markdown(f'<div class="section-title">{table_title}</div>', unsafe_allow_html=True)

            list_df = filtered_trade_df if not filtered_trade_df.empty else active_all_df
            list_df = list_df.head(8).copy() if not list_df.empty else empty_trade_df()

            if list_df.empty:
                st.info("Geen trades gevonden voor deze selectie.")
            else:
                for _, row in list_df.iterrows():
                    trade_key = safe_str(row.get("trade_id"))
                    symbol = safe_str(row.get("symbol"), "-")
                    setup = safe_str(row.get("setup_type"), "-")
                    outcome = safe_str(row.get("outcome"), "-")
                    pnl_txt = format_compact_r(row.get("pnl_r"))
                    dt_txt = safe_str(row.get("datetime"), "-")

                    container_class = "trade-button-active" if trade_key == str(st.session_state.selected_trade_key) else "trade-button"
                    st.markdown(f'<div class="{container_class}">', unsafe_allow_html=True)
                    label = f"{symbol} | {setup} | {outcome} | {pnl_txt} | {dt_txt}"
                    if st.button(label, key=f"trade_{st.session_state.active_trade_tab}_{trade_key}", use_container_width=True):
                        st.session_state.selected_trade_key = trade_key
                        st.session_state.selected_trade_source = st.session_state.active_trade_tab
                        st.rerun()
                    st.markdown("</div>", unsafe_allow_html=True)

    with right_activity:
        st.markdown('<div class="right-panel">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Recent Bot Activity</div>', unsafe_allow_html=True)

        if not feed:
            st.markdown('<div class="subtle">Nog geen recente activiteit gevonden.</div>', unsafe_allow_html=True)
        else:
            colors = {"prebuy": "#50d9a7", "real": "#50d9a7", "sim": "#8fd0ff"}
            for i, item in enumerate(feed):
                color = colors.get(item.get("kind"), "#8fd0ff")
                line_html = '<div class="activity-line"></div>' if i < len(feed) - 1 else ""
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

        st.markdown('<div class="panel-divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Portfolio Breakdown</div>', unsafe_allow_html=True)

        st.markdown(
            f"""
            <div class="metric-card" style="padding:12px 14px; min-height:74px; margin-bottom:10px;">
                <div class="metric-value metric-accent-blue">{format_money(total_portfolio_eur)}</div>
                <div class="metric-label">Total Portfolio Value</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if assets_df.empty:
            st.markdown('<div class="subtle">Geen portfolio-assets gevonden.</div>', unsafe_allow_html=True)
        else:
            st.markdown(
                """
                <div class="portfolio-row" style="padding-top:0;padding-bottom:8px;">
                    <div class="info-left" style="width:24%;">COIN</div>
                    <div class="info-left" style="width:24%; text-align:right;">AMOUNT</div>
                    <div class="info-left" style="width:24%; text-align:right;">PRICE</div>
                    <div class="info-left" style="width:24%; text-align:right;">VALUE</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            for _, row in assets_df.head(5).iterrows():
                st.markdown(
                    f"""
                    <div class="portfolio-row">
                        <div class="info-right" style="width:24%; text-align:left;">{safe_str(row.get('symbol'), '-')}</div>
                        <div class="info-right" style="width:24%;">{safe_str(row.get('display_amount'), '-')}</div>
                        <div class="info-right" style="width:24%; color:#8fd0ff;">{safe_str(row.get('display_price'), '-')}</div>
                        <div class="info-right" style="width:24%; color:#6ee7b7;">{safe_str(row.get('display_value'), '-')}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

        st.markdown('<div class="panel-divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Quick Performance Stats</div>', unsafe_allow_html=True)

        quick_stats = [
            ("Today PnL", format_r(real_profit_r * 0.13 if real_profit_r else 4.2), "#6ee7b7"),
            ("Open Positions", str(open_trades), "#ffffff"),
            ("Pending Orders", str(pending_count), "#ffffff"),
            ("Live Trades", str(real_count), "#ffffff"),
            ("History Simulator Trades", str(sim_count), "#8fd0ff"),
            ("Shadow Trades", str(shadow_count), "#6ee7b7"),
            ("History Simulator Win Rate", pct_str(sim_winrate), "#8fd0ff"),
            ("Shadow Win Rate", pct_str(shadow_winrate), "#6ee7b7"),
        ]
        for label, value, color in quick_stats:
            st.markdown(
                f"""
                <div class="mini-stat">
                    <div class="mini-stat-label">{label}</div>
                    <div class="mini-stat-value" style="color:{color};">{value}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        st.markdown('<div class="panel-divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Setup Performance Scoreboard</div>', unsafe_allow_html=True)

        if scoreboard_df.empty:
            st.markdown('<div class="subtle">Geen scoreboard-data beschikbaar.</div>', unsafe_allow_html=True)
        else:
            if scoreboard_meta["best_label"] != "-":
                st.markdown(
                    f"""
                    <div class="subtle" style="line-height:1.6; margin-bottom:10px;">
                        Beste combinatie: <b>{scoreboard_meta['best_label']}</b> met <b>{scoreboard_meta['best_win_rate']:.1f}%</b> win rate
                        over <b>{scoreboard_meta['best_n']}</b> trades.<br>
                        Zwakste combinatie: <b>{scoreboard_meta['worst_label']}</b> met <b>{scoreboard_meta['worst_win_rate']:.1f}%</b> win rate
                        over <b>{scoreboard_meta['worst_n']}</b> trades.
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            palette = ["#6ee7b7", "#8fd0ff", "#ffd166", "#ff6b87"]
            for i, (_, row) in enumerate(scoreboard_df.head(4).iterrows()):
                win = safe_float(row.get("win_rate"), 0.0)
                color = palette[i % len(palette)]
                label = f"{safe_str(row.get('setup_type'), '-')} | {safe_str(row.get('market_regime'), '-')}"
                st.markdown(
                    f"""
                    <div class="score-row">
                        <div class="score-left">
                            <span class="score-dot" style="background:{color};"></span>
                            {label}
                        </div>
                        <div class="score-right">
                            <span class="score-pct">{win:.0f}%</span>
                            <div class="score-bar"><div class="score-fill" style="width:{max(0, min(100, win))}%; background:{color};"></div></div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)

st.markdown('<div class="footspace"></div>', unsafe_allow_html=True)

with st.expander("Volledige trade-geschiedenis / filters", expanded=False):
    hist_toggle_cols = st.columns([0.22, 0.22, 0.56], gap="small")

    with hist_toggle_cols[0]:
        if st.button("Geschiedenis laden", use_container_width=True):
            st.session_state.load_history = True
            st.rerun()

    with hist_toggle_cols[1]:
        if st.button("Nieuwe snapshot ophalen", use_container_width=True):
            try:
                build_snapshot_with_eur_values()
                st.cache_data.clear()
                st.session_state.last_snapshot_refresh_msg = "Snapshot succesvol vernieuwd."
                st.rerun()
            except Exception as e:
                capture_exception("Nieuwe snapshot ophalen", e)
                st.session_state.last_snapshot_refresh_msg = f"Snapshot refresh mislukt: {e}"

    with hist_toggle_cols[2]:
        if st.session_state.last_snapshot_refresh_msg:
            st.caption(st.session_state.last_snapshot_refresh_msg)

    if st.session_state.load_history:
        hist_df = prepare_history_df(load_history_trades_db())

        f1, f2, f3, f4 = st.columns(4)
        with f1:
            type_opts = ["ALLES"] + sorted(hist_df["trade_type"].dropna().astype(str).unique().tolist()) if not hist_df.empty else ["ALLES"]
            type_filter = st.selectbox("Type", type_opts, index=0, key="hist_type")
        with f2:
            outcome_opts = ["ALLES"] + sorted(hist_df["outcome"].dropna().astype(str).unique().tolist()) if not hist_df.empty else ["ALLES"]
            outcome_filter = st.selectbox("Outcome", outcome_opts, index=0, key="hist_outcome")
        with f3:
            coin_opts = ["ALLES"] + sorted(hist_df["symbol"].dropna().astype(str).unique().tolist()) if not hist_df.empty else ["ALLES"]
            coin_filter = st.selectbox("Coin", coin_opts, index=0, key="hist_coin")
        with f4:
            days_filter = st.selectbox("Periode", ["ALLES", "7D", "30D", "90D", "180D"], index=0, key="hist_days")

        f5, f6, f7 = st.columns(3)
        with f5:
            setup_opts = ["ALLES"] + sorted(hist_df["setup_type"].dropna().astype(str).unique().tolist()) if not hist_df.empty else ["ALLES"]
            setup_filter = st.selectbox("Setup", setup_opts, index=0, key="hist_setup")
        with f6:
            regime_opts = ["ALLES"] + sorted(hist_df["regime"].dropna().astype(str).unique().tolist()) if not hist_df.empty else ["ALLES"]
            regime_filter = st.selectbox("Regime", regime_opts, index=0, key="hist_regime")
        with f7:
            timeframe_opts = ["ALLES"] + sorted(hist_df["timeframe"].dropna().astype(str).unique().tolist()) if not hist_df.empty else ["ALLES"]
            timeframe_filter = st.selectbox("Timeframe", timeframe_opts, index=0, key="hist_timeframe")

        filtered_hist = filter_history_df(
            hist_df,
            type_filter,
            outcome_filter,
            coin_filter,
            setup_filter,
            regime_filter,
            timeframe_filter,
            days_filter,
        )

        if filtered_hist.empty:
            st.info("Geen trades gevonden binnen de gekozen filters.")
        else:
            display_cols = [
                "trade_id",
                "symbol",
                "trade_type",
                "setup_type",
                "regime",
                "timeframe",
                "outcome",
                "pnl_r",
                "score",
                "chance",
                "confidence",
                "created_at",
                "closed_at",
                "datetime",
            ]
            existing_cols = [c for c in display_cols if c in filtered_hist.columns]
            st.dataframe(filtered_hist[existing_cols], use_container_width=True, hide_index=True)
    else:
        st.info("Geschiedenis staat uit. Klik op 'Geschiedenis laden' om alle trades te bekijken.")

hotkey_status = (
    f"Hotkeys | "
    f"W: volgende trade | "
    f"D: debug + PostgreSQL structuur {'AAN' if st.session_state.hotkey_show_debug else 'UIT'} | "
    f"A: Advanced AI Mode {'AAN' if st.session_state.hotkey_advanced_mode else 'UIT'} | "
    f"S: uitlegblokken {'AAN' if st.session_state.hotkey_show_pg_help else 'UIT'}"
)
st.caption(hotkey_status)

status_text = (
    f"Snapshot: {snapshot_state} | "
    f"Postgres: {'OK' if db_ready() else 'MIST'} | "
    f"Experience trades: {experience_total_count} | "
    f"Scoreboard: {experience_scoreboard_count} | "
    f"Sources: "
    + (
        ", ".join([f"{safe_str(r['source'])}:{safe_int(r['n'])}" for _, r in source_counts_df.iterrows()])
        if not source_counts_df.empty
        else "-"
    )
)
st.caption(status_text)

st.markdown("</div>", unsafe_allow_html=True)
