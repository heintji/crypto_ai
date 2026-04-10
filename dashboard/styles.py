"""CSS styles for Crypto AI Dashboard — dark cyber theme."""

DARK_CSS = """
<style>
/* Dark theme base */
.stApp { background-color: #0a0e17; color: #e0e0e0; }
section[data-testid="stSidebar"] { background-color: #0d1117; border-right: 1px solid #1a2332; }

/* Metric cards */
.metric-card {
    background: linear-gradient(135deg, #0d1117 0%, #161b22 100%);
    border: 1px solid #1a2332;
    border-radius: 8px;
    padding: 16px;
    margin: 4px 0;
}
.metric-card .label { color: #8b949e; font-size: 0.8em; text-transform: uppercase; }
.metric-card .value { color: #e6edf3; font-size: 1.6em; font-weight: 700; }
.metric-card .delta-pos { color: #3fb950; font-size: 0.9em; }
.metric-card .delta-neg { color: #f85149; font-size: 0.9em; }

/* Status badges */
.badge { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 0.8em; font-weight: 600; }
.badge-green { background: #0d3320; color: #3fb950; border: 1px solid #238636; }
.badge-red { background: #3d1117; color: #f85149; border: 1px solid #da3633; }
.badge-yellow { background: #3d2e00; color: #d29922; border: 1px solid #9e6a03; }
.badge-blue { background: #0c2d6b; color: #58a6ff; border: 1px solid #1f6feb; }

/* Tables */
.stDataFrame { border: 1px solid #1a2332; border-radius: 8px; }

/* Win/loss colors */
.win { color: #3fb950; }
.loss { color: #f85149; }

/* Section headers */
h2, h3 { color: #58a6ff !important; border-bottom: 1px solid #1a2332; padding-bottom: 8px; }

/* Tabs */
.stTabs [data-baseweb="tab-list"] { gap: 4px; }
.stTabs [data-baseweb="tab"] { background-color: #161b22; border-radius: 6px; color: #8b949e; }
.stTabs [aria-selected="true"] { background-color: #1f6feb !important; color: white !important; }

/* Buttons */
.stButton > button { background-color: #21262d; color: #c9d1d9; border: 1px solid #30363d; }
.stButton > button:hover { background-color: #30363d; border-color: #58a6ff; }

/* Sidebar navigation */
.nav-item { padding: 8px 12px; margin: 2px 0; border-radius: 6px; cursor: pointer; }
.nav-active { background-color: #1f6feb; color: white; }
</style>
"""


def inject_css():
    """Inject dark theme CSS."""
    import streamlit as st
    st.markdown(DARK_CSS, unsafe_allow_html=True)


def metric_html(label: str, value: str, delta: str = "", positive: bool = True) -> str:
    """Generate HTML for a metric card."""
    delta_class = "delta-pos" if positive else "delta-neg"
    delta_html = f'<div class="{delta_class}">{delta}</div>' if delta else ""
    return f'''<div class="metric-card">
        <div class="label">{label}</div>
        <div class="value">{value}</div>
        {delta_html}
    </div>'''


def badge(text: str, color: str = "blue") -> str:
    """Generate HTML badge. color: green/red/yellow/blue."""
    return f'<span class="badge badge-{color}">{text}</span>'


def status_badge(status: str) -> str:
    """Auto-colored status badge."""
    color_map = {
        "OPEN": "blue", "CLOSED": "green", "WIN": "green", "LOSS": "red",
        "PENDING": "yellow", "CONSUMED": "green", "EXPIRED": "red",
        "OK": "green", "ERROR": "red", "ACTIEF": "green", "GESTOPT": "red",
    }
    return badge(status, color_map.get(status.upper(), "blue"))
