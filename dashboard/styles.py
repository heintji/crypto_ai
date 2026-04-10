"""Crypto AI Dashboard — Modern Dark Crypto Theme"""
import streamlit as st

CRYPTO_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');

:root {
    --bg-primary: #0b0f19;
    --bg-secondary: #111827;
    --bg-card: #1a1f2e;
    --bg-card-hover: #1f2937;
    --border: #2d3748;
    --text-primary: #f1f5f9;
    --text-secondary: #94a3b8;
    --text-muted: #64748b;
    --accent-blue: #3b82f6;
    --accent-cyan: #06b6d4;
    --accent-green: #10b981;
    --accent-red: #ef4444;
    --accent-yellow: #f59e0b;
    --accent-purple: #8b5cf6;
    --glow-blue: rgba(59,130,246,0.15);
    --glow-green: rgba(16,185,129,0.15);
    --glow-red: rgba(239,68,68,0.15);
}

/* Base */
.stApp {
    background: var(--bg-primary) !important;
    font-family: 'Inter', sans-serif !important;
}
html, body, [class*="css"] { font-family: 'Inter', sans-serif !important; }

/* Hide streamlit chrome */
#MainMenu, footer, header { visibility: hidden; }
.stDeployButton { display: none; }

/* Sidebar */
section[data-testid="stSidebar"] {
    background: var(--bg-secondary) !important;
    border-right: 1px solid var(--border);
}
section[data-testid="stSidebar"] .stButton > button {
    background: transparent !important;
    border: 1px solid transparent !important;
    color: var(--text-secondary) !important;
    text-align: left !important;
    padding: 10px 16px !important;
    border-radius: 8px !important;
    font-weight: 500 !important;
    transition: all 0.2s !important;
}
section[data-testid="stSidebar"] .stButton > button:hover {
    background: var(--bg-card) !important;
    color: var(--text-primary) !important;
    border-color: var(--border) !important;
}
section[data-testid="stSidebar"] .stButton > button[kind="primary"] {
    background: linear-gradient(135deg, var(--accent-blue), var(--accent-cyan)) !important;
    color: white !important;
    border: none !important;
    box-shadow: 0 0 20px var(--glow-blue) !important;
}

/* Metric cards */
.crypto-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    position: relative;
    overflow: hidden;
}
.crypto-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
    background: linear-gradient(90deg, var(--accent-blue), var(--accent-cyan));
}
.crypto-card.green::before { background: linear-gradient(90deg, var(--accent-green), #34d399); }
.crypto-card.red::before { background: linear-gradient(90deg, var(--accent-red), #f87171); }
.crypto-card.yellow::before { background: linear-gradient(90deg, var(--accent-yellow), #fbbf24); }
.crypto-card.purple::before { background: linear-gradient(90deg, var(--accent-purple), #a78bfa); }
.crypto-card .card-label {
    color: var(--text-muted); font-size: 0.75rem;
    text-transform: uppercase; letter-spacing: 0.05em;
    margin-bottom: 8px; font-weight: 600;
}
.crypto-card .card-value {
    color: var(--text-primary); font-size: 1.8rem;
    font-weight: 800; line-height: 1.2;
    font-family: 'JetBrains Mono', monospace;
}
.crypto-card .card-delta { font-size: 0.85rem; margin-top: 4px; font-weight: 500; }
.card-delta.up { color: var(--accent-green); }
.card-delta.down { color: var(--accent-red); }

/* Badges */
.badge {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 3px 12px; border-radius: 20px;
    font-size: 0.75rem; font-weight: 600;
    letter-spacing: 0.03em;
}
.badge-green { background: var(--glow-green); color: var(--accent-green); border: 1px solid rgba(16,185,129,0.3); }
.badge-red { background: var(--glow-red); color: var(--accent-red); border: 1px solid rgba(239,68,68,0.3); }
.badge-yellow { background: rgba(245,158,11,0.12); color: var(--accent-yellow); border: 1px solid rgba(245,158,11,0.3); }
.badge-blue { background: var(--glow-blue); color: var(--accent-blue); border: 1px solid rgba(59,130,246,0.3); }
.badge-purple { background: rgba(139,92,246,0.12); color: var(--accent-purple); border: 1px solid rgba(139,92,246,0.3); }

/* Status dot */
.status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
.status-dot.green { background: var(--accent-green); box-shadow: 0 0 8px var(--accent-green); }
.status-dot.red { background: var(--accent-red); box-shadow: 0 0 8px var(--accent-red); }

/* Headers */
h1 {
    color: var(--text-primary) !important;
    font-weight: 800 !important;
    background: linear-gradient(135deg, var(--accent-blue), var(--accent-cyan));
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}
h2 { color: var(--text-primary) !important; font-weight: 700 !important; }
h3 { color: var(--text-secondary) !important; font-weight: 600 !important; }

/* Dataframe */
.stDataFrame { border-radius: 12px !important; overflow: hidden; }
.stDataFrame [data-testid="stTable"] { background: var(--bg-card) !important; }

/* Tabs */
.stTabs [data-baseweb="tab-list"] { gap: 8px; background: var(--bg-secondary); padding: 4px; border-radius: 10px; }
.stTabs [data-baseweb="tab"] {
    background: transparent; border-radius: 8px;
    color: var(--text-muted); font-weight: 600;
    padding: 8px 20px;
}
.stTabs [aria-selected="true"] {
    background: var(--accent-blue) !important;
    color: white !important;
    box-shadow: 0 0 12px var(--glow-blue);
}

/* Expanders */
.streamlit-expanderHeader {
    background: var(--bg-card) !important;
    border-radius: 8px !important;
    border: 1px solid var(--border) !important;
}

/* Metrics */
[data-testid="stMetric"] {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px !important;
}
[data-testid="stMetricLabel"] { color: var(--text-muted) !important; font-weight: 600 !important; text-transform: uppercase; font-size: 0.75rem !important; }
[data-testid="stMetricValue"] { color: var(--text-primary) !important; font-family: 'JetBrains Mono', monospace !important; }

/* Toggle */
.stToggle label { color: var(--text-secondary) !important; }

/* Divider */
hr { border-color: var(--border) !important; opacity: 0.5; }

/* Number input */
.stNumberInput input { background: var(--bg-card) !important; color: var(--text-primary) !important; border-color: var(--border) !important; }

/* Title bar glow effect */
.title-glow {
    text-align: center;
    padding: 8px 0 16px 0;
}
.title-glow h1 {
    font-size: 1.5rem !important;
    margin: 0 !important;
}
</style>
"""

def inject_css():
    st.markdown(CRYPTO_CSS, unsafe_allow_html=True)

def card(label: str, value: str, delta: str = "", color: str = "", up: bool = True) -> str:
    cls = f"crypto-card {color}"
    delta_html = ""
    if delta:
        d_cls = "up" if up else "down"
        arrow = "▲" if up else "▼"
        delta_html = f'<div class="card-delta {d_cls}">{arrow} {delta}</div>'
    return f'<div class="{cls}"><div class="card-label">{label}</div><div class="card-value">{value}</div>{delta_html}</div>'

def badge(text: str, color: str = "blue") -> str:
    return f'<span class="badge badge-{color}">{text}</span>'

def status_badge(status: str) -> str:
    cmap = {"OPEN": "blue", "CLOSED": "green", "WIN": "green", "LOSS": "red",
            "PENDING": "yellow", "CONSUMED": "green", "EXPIRED": "red",
            "OK": "green", "ERROR": "red", "ACTIEF": "green", "GESTOPT": "red",
            "BULL": "green", "BEAR": "red", "RANGE": "yellow"}
    return badge(status, cmap.get(str(status).upper(), "blue"))

def status_dot(ok: bool) -> str:
    c = "green" if ok else "red"
    return f'<span class="status-dot {c}"></span>'
