# app.py
# ============================================================
# Crypto AI Terminal — Streamlit Dashboard v3.0
# Bloomberg Terminal Stijl — Professioneel & Uitgebreid
# ============================================================
#
# WAT DIT BESTAND DOET:
# ─────────────────────────────────────────────────────────────
# Dit is het visuele controlecentrum van de gehele bot.
# Volledig herschreven voor een professionelere uitstraling
# met betere navigatie, meer data en Claude AI integratie.
#
# PAGINA'S:
# ─────────────────────────────────────────────────────────────
# 1. dashboard     → Overzicht: hero metrics + equity curve
# 2. live          → Live Performance: alleen REAL trades
# 3. sim           → Simulator: alleen SIM trades
# 4. shadow        → Shadow Review: alleen SHADOW trades
# 5. portfolio     → Portfolio: Bitvavo assets + snapshot
# 6. signals       → Pre-BUY Signals: actieve scanner signals
# 7. scoreboard    → Experience Scoreboard: win rates
# 8. regime        → BTC Regime + Markt overzicht
# 9. settings      → Bot instellingen + WhatsApp commands
# 10. help         → Data mapping + debug + uitleg
#
# DATA BRONNEN:
# ─────────────────────────────────────────────────────────────
# - public.experience_trades    (REAL, SIM, SHADOW trades)
# - public.experience_scoreboard (win rates per setup/regime)
# - public.pending_approvals    (Pre-BUY signals van scanner)
# - public.bot_state            (bot actief/gepauzeerd/gestopt)
# - public.btc_regime_4h        (BTC regime data)
# - public.market_regime        (coin regime data)
# - /data/live_state.json       (open live trades)
# - /data/shadow_trades.json    (open shadow trades)
# - /data/account_snapshot.json (Bitvavo portfolio)
# - Bitvavo API (live portfolio als snapshot ontbreekt)
#
# BUGS GEFIXED vs origineel:
# ─────────────────────────────────────────────────────────────
# ✅ with main_col / right_col buiten scope → crash opgelost
# ✅ Dubbele page routing verwijderd
# ✅ Witte knoppen → donker gestijld
# ✅ HMAC digestmod= fix in Bitvavo signing
# ✅ Demo data alleen als DB leeg is
#
# NIEUWE FEATURES vs origineel:
# ─────────────────────────────────────────────────────────────
# ✅ Bot status widget in top balk (ACTIEF/GEPAUZEERD/GESTOPT)
# ✅ BTC regime live display in top balk
# ✅ Profit Factor per periode
# ✅ Consecutive losses waarschuwing
# ✅ Coin blacklist pagina
# ✅ Pre-BUY Signals pagina (was niet aanwezig)
# ✅ Experience Scoreboard pagina
# ✅ BTC Regime pagina
# ✅ Bot Settings pagina
# ✅ Claude AI analyse knoppen per pagina
# ✅ WhatsApp command shortcuts in Settings
# ✅ Dagelijkse activiteit grafiek
# ✅ R/R ratio display per trade
# ✅ Cooldown coins display
# ✅ Edge decay pagina uitgebreid
# ✅ Auto-refresh optie
# ─────────────────────────────────────────────────────────────

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
import psycopg2
import psycopg2.extras
import requests
import streamlit as st


# ============================================================
# PAGINA CONFIGURATIE — altijd als eerste
# ============================================================
st.set_page_config(
    page_title="Crypto AI Terminal",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ============================================================
# CONFIGURATIE — via Render Environment Variables
# ============================================================
API_KEY              = (os.getenv("BITVAVO_API_KEY", "") or "").strip().strip('"\'')
API_SECRET           = (os.getenv("BITVAVO_API_SECRET", "") or "").strip().strip('"\'')
DATABASE_URL         = (os.getenv("DATABASE_URL", "") or "").strip()
ANTHROPIC_API_KEY    = (os.getenv("ANTHROPIC_API_KEY", "") or "").strip()
BASE_URL             = "https://api.bitvavo.com"
ACCESS_WINDOW_MS     = os.getenv("BITVAVO_ACCESS_WINDOW_MS", "10000")
SNAPSHOT_PATH        = os.getenv("SNAPSHOT_PATH", "/data/account_snapshot.json")
HTTP_TIMEOUT         = int(os.getenv("HTTP_TIMEOUT", "10"))
DB_CONNECT_TIMEOUT   = int(os.getenv("DB_CONNECT_TIMEOUT", "4"))
DB_STATEMENT_TIMEOUT = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "4000"))
DASHBOARD_REFRESH    = int(os.getenv("DASHBOARD_REFRESH_SEC", "30"))

# Data limieten
REAL_LIMIT       = int(os.getenv("DASH_REAL_LIMIT", "400"))
SIM_LIMIT        = int(os.getenv("DASH_SIM_LIMIT", "400"))
SHADOW_LIMIT     = int(os.getenv("DASH_SHADOW_LIMIT", "400"))
HISTORY_LIMIT    = int(os.getenv("DASH_HISTORY_LIMIT", "100000"))
PENDING_LIMIT    = int(os.getenv("DASH_PENDING_LIMIT", "1000"))
SCOREBOARD_LIMIT = int(os.getenv("DASH_SCOREBOARD_LIMIT", "500"))

# Bot limieten (voor display — identiek aan alle andere bestanden)
MAX_PER_TRADE_EUR       = float(os.getenv("MAX_PER_TRADE_EUR", "0.50"))
MAX_REAL_TRADES_PER_DAY = int(os.getenv("MAX_REAL_TRADES_PER_DAY", "10"))
MAX_OPEN_REAL_TRADES    = int(os.getenv("MAX_OPEN_REAL_TRADES", "5"))
DAILY_STOP_LOSS_EUR     = float(os.getenv("DAILY_STOP_LOSS_EUR", "5.00"))
MIN_SCORE_TO_TRADE      = int(os.getenv("MIN_SCORE_TO_TRADE", "85"))
TRADING_HOURS_START     = int(os.getenv("TRADING_HOURS_START", "8"))
TRADING_HOURS_END       = int(os.getenv("TRADING_HOURS_END", "22"))
BOT_STATE_TABLE         = "public.bot_state"


def _get_data_dir() -> str:
    d = (os.getenv("DATA_DIR") or "").strip()
    if d:
        return d
    return "/data" if os.path.isdir("/data") else "/tmp/data"


DATA_DIR          = _get_data_dir()
LIVE_STATE_PATH   = os.path.join(DATA_DIR, "live_state.json")
SHADOW_STATE_PATH = os.path.join(DATA_DIR, "shadow_trades.json")


# ============================================================
# SESSION STATE
# ============================================================
SESSION_DEFAULTS = {
    "page":                      "dashboard",
    "selected_page_trade_id":    None,
    "status_notice":             "",
    "show_debug":                False,
    "search_text":               "",
    "global_days_filter":        "ALLES",
    "global_trade_type_filter":  "ALLES",
    "global_setup_filter":       "ALLES",
    "global_regime_filter":      "ALLES",
    "global_outcome_filter":     "ALLES",
    "global_symbol_filter":      "ALLES",
    "debug_events":              [],
    "last_error_text":           "",
    "auto_refresh":              False,
    "portfolio_search":          "",
    "coach_messages":            [],   # chat geschiedenis AI Coach
    "coach_context":             {},   # live data context voor coach
}
for _k, _v in SESSION_DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ============================================================
# BLOOMBERG TERMINAL STYLING
# Professioneel donker thema met oranje accenten.
# Gebaseerd op Bloomberg Terminal + TradingView Dark.
# ============================================================
st.markdown("""
<style>
/* ── ROOT KLEUREN ─────────────────────────────────────────── */
:root {
    --bg:      #050914;
    --bg2:     #0a1020;
    --panel:   #0d1528;
    --panel2:  #101b31;
    --line:    rgba(255,255,255,0.08);
    --text:    #f8fafc;
    --muted:   #94a3b8;
    --blue:    #60a5fa;
    --cyan:    #67e8f9;
    --green:   #6ee7b7;
    --green2:  #34d399;
    --yellow:  #fbbf24;
    --red:     #fb7185;
    --orange:  #ff8c00;
    --purple:  #c084fc;
}

/* ── GLOBALE ACHTERGROND ──────────────────────────────────── */
.stApp {
    background:
        radial-gradient(circle at top center, rgba(76,29,149,0.18) 0%, rgba(4,10,20,0) 30%),
        linear-gradient(180deg, #050914 0%, #030712 100%);
    color: var(--text);
    font-family: 'Courier New', monospace;
}

header[data-testid="stHeader"] { background: transparent; }
section[data-testid="stSidebar"] { display: none; }

.block-container {
    max-width: 1880px;
    padding-top: 0.5rem;
    padding-bottom: 1rem;
}

/* ── SHELL / CONTAINER ─────────────────────────────────────── */
.shell {
    border-radius: 24px;
    border: 1px solid rgba(255,255,255,0.08);
    background: linear-gradient(180deg,rgba(9,13,25,0.96),rgba(6,10,20,0.96));
    box-shadow: 0 24px 60px rgba(0,0,0,0.35);
    padding: 14px;
}

/* ── TOP BAR ───────────────────────────────────────────────── */
.topbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    padding: 4px 2px 12px 2px;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    margin-bottom: 12px;
}

.brand {
    display: flex;
    align-items: center;
    gap: 12px;
}

.brand-mark {
    width: 18px;
    height: 38px;
    border-radius: 8px 16px 8px 16px;
    background: linear-gradient(180deg,#ff8c00 0%, #e07000 100%);
    transform: skewX(-18deg);
    box-shadow: 0 8px 22px rgba(255,140,0,0.30);
}

.brand-title {
    font-size: 22px;
    line-height: 1.05;
    font-weight: 900;
    letter-spacing: -0.03em;
    color: #ffffff;
}

.brand-sub {
    font-size: 11px;
    color: #94a3b8;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-top: 1px;
}

.top-status-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
}

.top-status-chip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 5px 10px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 900;
    letter-spacing: 0.04em;
    border: 1px solid rgba(255,255,255,0.10);
}

.chip-green  { background: rgba(52,211,153,0.15); color: #34d399; border-color: rgba(52,211,153,0.25); }
.chip-red    { background: rgba(239,68,68,0.15);  color: #fb7185; border-color: rgba(239,68,68,0.25); }
.chip-yellow { background: rgba(251,191,36,0.15); color: #fbbf24; border-color: rgba(251,191,36,0.25); }
.chip-blue   { background: rgba(96,165,250,0.15); color: #60a5fa; border-color: rgba(96,165,250,0.25); }
.chip-orange { background: rgba(255,140,0,0.15);  color: #ff8c00; border-color: rgba(255,140,0,0.25); }
.chip-purple { background: rgba(192,132,252,0.15);color: #c084fc; border-color: rgba(192,132,252,0.25);}
.chip-gray   { background: rgba(255,255,255,0.05);color: #94a3b8; border-color: rgba(255,255,255,0.10);}

/* ── PANELS ────────────────────────────────────────────────── */
.panel, .panel-tight {
    background: linear-gradient(180deg,rgba(15,22,40,0.96),rgba(9,15,28,0.96));
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 20px;
    box-shadow: 0 12px 34px rgba(0,0,0,0.24);
}
.panel       { padding: 14px; }
.panel-tight { padding: 10px 12px; }

/* ── METRIC CARDS ──────────────────────────────────────────── */
.metric-card {
    background: linear-gradient(180deg,rgba(14,22,40,0.98),rgba(10,16,30,0.98));
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 16px;
    padding: 14px;
    min-height: 88px;
    position: relative;
    overflow: hidden;
}

.metric-card::before {
    content: "";
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: linear-gradient(90deg,rgba(255,140,0,0.4),rgba(96,165,250,0.4));
    border-radius: 999px;
}

.metric-value {
    color: #ffffff;
    font-size: 22px;
    line-height: 1.05;
    font-weight: 900;
    margin-bottom: 5px;
}

.metric-delta {
    font-size: 11px;
    font-weight: 700;
    margin-bottom: 4px;
}

.metric-label {
    color: var(--muted);
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
}

.metric-card.green-accent { border-top: 2px solid #34d399; }
.metric-card.red-accent   { border-top: 2px solid #fb7185; }
.metric-card.blue-accent  { border-top: 2px solid #60a5fa; }
.metric-card.orange-accent{ border-top: 2px solid #ff8c00; }
.metric-card.purple-accent{ border-top: 2px solid #c084fc; }

.accent-blue   { color: var(--blue)   !important; }
.accent-green  { color: var(--green2) !important; }
.accent-red    { color: var(--red)    !important; }
.accent-purple { color: var(--purple) !important; }
.accent-yellow { color: var(--yellow) !important; }
.accent-orange { color: var(--orange) !important; }

/* ── SECTION TITELS ────────────────────────────────────────── */
.section-title {
    color: #ffffff;
    font-size: 20px;
    font-weight: 900;
    margin-bottom: 8px;
    letter-spacing: -0.02em;
}

.section-subtitle {
    color: var(--muted);
    font-size: 12px;
    line-height: 1.6;
    margin-bottom: 12px;
}

.section-shell {
    background: linear-gradient(180deg,rgba(255,255,255,0.012),rgba(255,255,255,0.006));
    border: 1px solid rgba(255,255,255,0.035);
    border-radius: 18px;
    padding: 14px;
    margin-top: 10px;
    margin-bottom: 10px;
}

.divider {
    height: 1px;
    background: rgba(255,255,255,0.05);
    border-radius: 999px;
    margin: 10px 0 12px 0;
}

.section-divider-subtle {
    height: 1px;
    background: linear-gradient(90deg,rgba(255,255,255,0.00),rgba(255,255,255,0.045),rgba(255,255,255,0.00));
    margin: 12px 2px;
    border-radius: 999px;
}

/* ── NAVIGATIE ─────────────────────────────────────────────── */
.nav-header {
    color: #ffffff;
    font-size: 12px;
    font-weight: 900;
    text-transform: uppercase;
    margin-bottom: 8px;
    letter-spacing: 0.04em;
}

.nav-caption {
    color: var(--muted);
    font-size: 11px;
    line-height: 1.5;
    margin-bottom: 10px;
}

.page-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 10px;
    border-radius: 999px;
    background: linear-gradient(90deg,rgba(255,140,0,0.16),rgba(96,165,250,0.16));
    border: 1px solid rgba(255,255,255,0.10);
    color: #ffffff;
    font-size: 11px;
    font-weight: 900;
    margin-bottom: 12px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
}

/* ── KNOPPEN — altijd donker ──────────────────────────────── */
div.stButton > button,
div.stFormSubmitButton > button,
div.stDownloadButton > button {
    width: 100%;
    background: linear-gradient(180deg,#172033,#131c2c) !important;
    background-color: #172033 !important;
    color: #f8fafc !important;
    border: 1px solid rgba(255,255,255,0.14) !important;
    border-radius: 12px !important;
    font-weight: 800 !important;
    font-family: 'Courier New', monospace !important;
    font-size: 12px !important;
    min-height: 40px !important;
    box-shadow: none !important;
    opacity: 1 !important;
    transition: all 0.15s ease !important;
}

div.stButton > button:hover,
div.stFormSubmitButton > button:hover {
    background: linear-gradient(180deg,#1d2942,#172135) !important;
    color: #ffffff !important;
    border-color: rgba(96,165,250,0.35) !important;
    transform: translateY(-1px) !important;
}

/* ── ACTIEVE NAV KNOP ──────────────────────────────────────── */
.nav-button-active div.stButton > button {
    background: linear-gradient(90deg,rgba(255,140,0,0.20),rgba(96,165,250,0.22)) !important;
    border-color: rgba(255,255,255,0.22) !important;
    color: #ffffff !important;
    box-shadow: 0 0 0 1px rgba(255,255,255,0.04), 0 0 28px rgba(96,165,250,0.14) !important;
    position: relative !important;
    padding-left: 20px !important;
}

/* ── TRADE CARDS ───────────────────────────────────────────── */
.tc {
    background: #0e0e0e;
    border: 1px solid #2a2a2a;
    border-radius: 14px;
    padding: 10px 12px;
    margin-bottom: 6px;
    font-size: 12px;
    line-height: 1.6;
}
.tc-win  { border-left: 3px solid #34d399; }
.tc-loss { border-left: 3px solid #fb7185; }
.tc-open { border-left: 3px solid #ff8c00; }
.tc-shad { border-left: 3px solid #c084fc; }
.tc-pend { border-left: 3px solid #60a5fa; }

/* ── TRADE CHIP ROW ────────────────────────────────────────── */
.trade-chip-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 8px;
}
.trade-chip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 4px 8px;
    border-radius: 999px;
    border: 1px solid rgba(255,255,255,0.08);
    background: rgba(255,255,255,0.04);
    color: #dbe4f0;
    font-size: 11px;
    font-weight: 900;
}

/* ── TRADE NOTE / BESCHRIJVING ─────────────────────────────── */
.trade-note {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 12px;
    padding: 12px;
    color: #e2e8f0;
    font-size: 12px;
    line-height: 1.65;
}

/* ── STATUS BADGES ─────────────────────────────────────────── */
.status-ok, .status-warn, .status-bad {
    display: inline-block;
    padding: 3px 8px;
    border-radius: 999px;
    font-size: 10px;
    font-weight: 900;
    margin-right: 4px;
}
.status-ok   { background: rgba(34,197,94,0.15);  color: #86efac; border: 1px solid rgba(34,197,94,0.25); }
.status-warn { background: rgba(245,158,11,0.15); color: #fde68a; border: 1px solid rgba(245,158,11,0.25); }
.status-bad  { background: rgba(239,68,68,0.15);  color: #fda4af; border: 1px solid rgba(239,68,68,0.25); }

/* ── ACTIVITY CARDS ────────────────────────────────────────── */
.activity-card {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 14px;
    padding: 10px;
    margin-bottom: 8px;
}
.activity-title { font-size: 12px; font-weight: 900; margin-bottom: 3px; }
.activity-sub   { color: #e2e8f0; font-size: 11px; line-height: 1.45; }
.activity-time  { color: var(--muted); font-size: 10px; margin-top: 4px; }

/* ── LIJST ROWS ────────────────────────────────────────────── */
.list-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    padding: 10px 0;
    border-bottom: 1px solid rgba(255,255,255,0.06);
}
.list-left  { color: #ffffff; font-size: 12px; font-weight: 700; }
.list-right { color: #cbd5e1; font-size: 12px; font-weight: 700; text-align: right; }

/* ── HOLDING ROWS ──────────────────────────────────────────── */
.holding-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 9px 0;
    border-bottom: 1px solid rgba(255,255,255,0.06);
}
.holding-symbol { color: #ffffff; font-size: 13px; font-weight: 900; }
.holding-sub    { color: #94a3b8; font-size: 11px; }
.holding-value  { color: #e2e8f0; font-size: 13px; font-weight: 900; text-align: right; }
.holding-share  { color: #60a5fa; font-size: 11px; font-weight: 700; text-align: right; }

/* ── TEKST HULPEN ──────────────────────────────────────────── */
.small-muted { color: var(--muted); font-size: 11px; line-height: 1.55; }
p, li, span  { color: #aaa !important; }
code         { background: #111 !important; color: #ff8c00 !important; }

/* ── FILTER CARD ───────────────────────────────────────────── */
.filter-card {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 14px;
    padding: 10px;
    margin-bottom: 10px;
}

/* ── INPUT STIJLEN ─────────────────────────────────────────── */
[data-testid="stTextInput"] input,
[data-testid="stSelectbox"] div[data-baseweb="select"] > div {
    background: rgba(255,255,255,0.04) !important;
    color: #ffffff !important;
    border-radius: 10px !important;
    border: 1px solid rgba(255,255,255,0.10) !important;
}

/* ── DATAFRAME ─────────────────────────────────────────────── */
[data-testid="stDataFrame"] {
    border: 1px solid rgba(255,255,255,0.08) !important;
    border-radius: 12px !important;
}

/* ── CODE BLOCKS ───────────────────────────────────────────── */
pre, code, .stCodeBlock {
    background: #0f172a !important;
    color: #e2e8f0 !important;
    border-radius: 12px !important;
    border: 1px solid rgba(255,255,255,0.08) !important;
}

/* ── CAPTION ───────────────────────────────────────────────── */
.stCaption { color: #555 !important; font-size: 11px !important; }

/* ── HERO SECTION ──────────────────────────────────────────── */
.hero-card {
    background: linear-gradient(180deg,rgba(10,13,22,0.98),rgba(8,11,18,0.98));
    border: 1px solid rgba(255,255,255,0.05);
    border-radius: 20px;
    padding: 20px;
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.03);
    overflow: hidden;
    min-height: 100%;
}

.hero-main-amount {
    font-size: 32px;
    font-weight: 900;
    color: #ffffff;
    line-height: 1;
    margin-bottom: 6px;
}

.hero-stat-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 0;
    border-bottom: 1px solid rgba(255,255,255,0.06);
}

.hero-stat-label { font-size: 13px; color: #f8fafc; font-weight: 700; }
.hero-stat-value { font-size: 14px; color: #ffffff; font-weight: 900; text-align: right; }

.hero-stat-badge {
    display: inline-block;
    padding: 3px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 900;
    margin-left: 8px;
}
.hero-stat-badge.green { background: rgba(52,211,153,0.16); color: #34d399; }
.hero-stat-badge.red   { background: rgba(239,68,68,0.16);  color: #ef4444; }
.hero-stat-badge.blue  { background: rgba(96,165,250,0.16); color: #60a5fa; }

.dominance-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 8px 14px;
    border-radius: 999px;
    margin-bottom: 14px;
    font-size: 12px;
    font-weight: 900;
    letter-spacing: 0.04em;
    border: 1px solid rgba(255,255,255,0.10);
}
.dominance-pill.green {
    color: #ffffff;
    background: linear-gradient(90deg,rgba(6,78,59,0.36),rgba(52,211,153,0.10));
    box-shadow: 0 0 20px rgba(52,211,153,0.14);
}
.dominance-pill.red {
    color: #ffffff;
    background: linear-gradient(90deg,rgba(127,29,29,0.36),rgba(239,68,68,0.10));
    box-shadow: 0 0 20px rgba(239,68,68,0.14);
}

/* ── SCOREBOARD ────────────────────────────────────────────── */
.score-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 12px;
    border-radius: 10px;
    margin-bottom: 4px;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06);
}
.score-left  { color: #ffffff; font-size: 12px; font-weight: 800; }
.score-right { color: #cbd5e1; font-size: 12px; font-weight: 700; text-align: right; }

/* ── SIGNAL CARDS ──────────────────────────────────────────── */
.signal-card {
    background: rgba(96,165,250,0.05);
    border: 1px solid rgba(96,165,250,0.15);
    border-radius: 14px;
    padding: 12px;
    margin-bottom: 6px;
}
.signal-symbol   { color: #ffffff; font-size: 14px; font-weight: 900; }
.signal-score    { color: #34d399; font-size: 13px; font-weight: 900; }
.signal-details  { color: #94a3b8; font-size: 11px; margin-top: 4px; }

/* ── OVERALL WIN/LOSS BAR — permanent bovenaan ─────────────── */
.winloss-bar {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 14px;
    border-radius: 14px;
    border: 1px solid rgba(255,255,255,0.08);
    background: rgba(255,255,255,0.03);
    margin-bottom: 10px;
    flex-wrap: wrap;
}
.winloss-source {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 12px;
    border-radius: 10px;
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.06);
    min-width: 180px;
}
.winloss-label  { color: #94a3b8; font-size: 10px; font-weight: 700; text-transform: uppercase; }
.winloss-pct-w  { color: #34d399; font-size: 15px; font-weight: 900; }
.winloss-pct-l  { color: #fb7185; font-size: 15px; font-weight: 900; }
.winloss-count  { color: #94a3b8; font-size: 11px; }
.winloss-divider{ width: 1px; height: 28px; background: rgba(255,255,255,0.08); }

/* ── FLOATING PNL CARDS ────────────────────────────────────── */
.float-card {
    background: linear-gradient(180deg,rgba(14,22,40,0.98),rgba(10,16,30,0.98));
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 14px;
    padding: 12px;
    margin-bottom: 6px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.float-left  { color: #ffffff; font-size: 13px; font-weight: 800; }
.float-right { font-size: 13px; font-weight: 900; text-align: right; }
.float-green { color: #34d399; }
.float-red   { color: #fb7185; }
.float-sub   { color: #94a3b8; font-size: 11px; margin-top: 2px; }

/* ── SCANNER STATUS ────────────────────────────────────────── */
.scanner-card {
    background: rgba(96,165,250,0.05);
    border: 1px solid rgba(96,165,250,0.12);
    border-radius: 14px;
    padding: 14px;
    margin-bottom: 8px;
}
.scanner-title { color: #ffffff; font-size: 13px; font-weight: 900; margin-bottom: 6px; }
.scanner-row   { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.05); }
.scanner-key   { color: #94a3b8; font-size: 12px; }
.scanner-val   { color: #ffffff; font-size: 12px; font-weight: 700; }

/* ── DAGBUDGET BAR ─────────────────────────────────────────── */
.budget-bar {
    height: 8px;
    border-radius: 999px;
    background: rgba(255,255,255,0.08);
    overflow: hidden;
    margin: 6px 0;
}
.budget-fill-safe { height: 100%; border-radius: 999px; background: linear-gradient(90deg,#34d399,#60a5fa); }
.budget-fill-warn { height: 100%; border-radius: 999px; background: linear-gradient(90deg,#fbbf24,#fb7185); }

/* ── KALENDER HEATMAP ──────────────────────────────────────── */
.cal-grid {
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 3px;
    margin-top: 8px;
}
.cal-day {
    aspect-ratio: 1;
    border-radius: 4px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 9px;
    font-weight: 700;
    cursor: default;
    min-height: 28px;
}
.cal-win   { background: rgba(52,211,153,0.35); color: #34d399; }
.cal-loss  { background: rgba(239,68,68,0.35);  color: #fb7185; }
.cal-flat  { background: rgba(255,255,255,0.05);color: #94a3b8; }
.cal-empty { background: transparent; }
.cal-header{ color: #94a3b8; font-size: 10px; font-weight: 700; text-align: center; padding: 2px 0; }

/* ── CORRELATIE BARS ───────────────────────────────────────── */
.corr-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 0;
    border-bottom: 1px solid rgba(255,255,255,0.05);
}
.corr-label { color: #ffffff; font-size: 12px; font-weight: 800; width: 80px; flex-shrink: 0; }
.corr-bar-wrap { flex: 1; height: 8px; background: rgba(255,255,255,0.08); border-radius: 999px; overflow: hidden; }
.corr-bar-fill { height: 100%; border-radius: 999px; }
.corr-pct   { color: #ffffff; font-size: 12px; font-weight: 900; width: 48px; text-align: right; flex-shrink: 0; }
.corr-count { color: #94a3b8; font-size: 11px; width: 60px; text-align: right; flex-shrink: 0; }

/* ── STREAK HISTORY ────────────────────────────────────────── */
.streak-row {
    display: flex;
    align-items: center;
    gap: 4px;
    margin-bottom: 6px;
    flex-wrap: wrap;
}
.streak-dot-w { width: 14px; height: 14px; border-radius: 3px; background: #34d399; display: inline-block; }
.streak-dot-l { width: 14px; height: 14px; border-radius: 3px; background: #fb7185; display: inline-block; }

/* ── BESTE/SLECHTSTE TRADES ────────────────────────────────── */
.top-trade-card {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 12px;
    padding: 10px 12px;
    margin-bottom: 5px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.top-trade-left  { color: #ffffff; font-size: 12px; font-weight: 800; }
.top-trade-right { font-size: 13px; font-weight: 900; }
.top-trade-sub   { color: #94a3b8; font-size: 10px; margin-top: 2px; }

/* ── FEE TRACKING ──────────────────────────────────────────── */
.fee-card {
    background: rgba(251,191,36,0.05);
    border: 1px solid rgba(251,191,36,0.12);
    border-radius: 14px;
    padding: 14px;
    margin-bottom: 8px;
}

/* ── COIN STATUS RIJEN ─────────────────────────────────────── */
.coin-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 10px;
    border-radius: 10px;
    margin-bottom: 4px;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.06);
}
.coin-row-black { border-left: 3px solid #fb7185; }
.coin-row-cool  { border-left: 3px solid #fbbf24; }
.coin-row-white { border-left: 3px solid #34d399; }
.coin-name      { color: #ffffff; font-size: 12px; font-weight: 800; }
.coin-stats     { color: #94a3b8; font-size: 11px; }

/* ── AI COACH CHAT ─────────────────────────────────────────── */
.chat-container {
    display: flex;
    flex-direction: column;
    gap: 12px;
    max-height: 520px;
    overflow-y: auto;
    padding: 14px;
    background: rgba(5,9,20,0.80);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 16px;
    margin-bottom: 12px;
    scroll-behavior: smooth;
}

.chat-msg-user {
    display: flex;
    justify-content: flex-end;
}

.chat-msg-coach {
    display: flex;
    justify-content: flex-start;
    align-items: flex-start;
    gap: 8px;
}

.chat-bubble-user {
    background: linear-gradient(135deg, rgba(96,165,250,0.25), rgba(52,211,153,0.20));
    border: 1px solid rgba(96,165,250,0.30);
    border-radius: 18px 18px 4px 18px;
    padding: 10px 14px;
    color: #ffffff;
    font-size: 13px;
    line-height: 1.55;
    max-width: 75%;
    word-wrap: break-word;
}

.chat-bubble-coach {
    background: linear-gradient(135deg, rgba(14,22,40,0.98), rgba(10,16,30,0.98));
    border: 1px solid rgba(255,140,0,0.20);
    border-radius: 4px 18px 18px 18px;
    padding: 12px 14px;
    color: #e2e8f0;
    font-size: 13px;
    line-height: 1.65;
    max-width: 82%;
    word-wrap: break-word;
    white-space: pre-wrap;
}

.chat-avatar-coach {
    width: 32px;
    height: 32px;
    border-radius: 999px;
    background: linear-gradient(135deg, #ff8c00, #ff6000);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 16px;
    flex-shrink: 0;
    box-shadow: 0 0 12px rgba(255,140,0,0.30);
}

.chat-time {
    color: #555;
    font-size: 10px;
    margin-top: 4px;
    text-align: right;
}

.chat-context-card {
    background: rgba(255,140,0,0.05);
    border: 1px solid rgba(255,140,0,0.12);
    border-radius: 12px;
    padding: 10px 12px;
    margin-bottom: 10px;
    font-size: 11px;
    color: #94a3b8;
}

.chat-context-title {
    color: #ff8c00;
    font-size: 11px;
    font-weight: 900;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 6px;
}

.chat-suggestion {
    display: inline-block;
    padding: 5px 10px;
    border-radius: 999px;
    background: rgba(96,165,250,0.10);
    border: 1px solid rgba(96,165,250,0.20);
    color: #60a5fa;
    font-size: 11px;
    font-weight: 700;
    cursor: pointer;
    margin: 3px;
}

.chat-action-card {
    background: rgba(52,211,153,0.08);
    border: 1px solid rgba(52,211,153,0.20);
    border-radius: 12px;
    padding: 10px 12px;
    margin-top: 8px;
    font-size: 12px;
    color: #34d399;
}

.chat-empty {
    text-align: center;
    padding: 40px 20px;
    color: #555;
    font-size: 13px;
}

/* ── TINY BUTTON ───────────────────────────────────────────── */
.tiny-button div.stButton > button {
    min-height: 36px !important;
    border-radius: 10px !important;
    font-size: 11px !important;
}

/* ── TRADE BUTTON ──────────────────────────────────────────── */
.trade-button div.stButton > button {
    text-align: left !important;
    min-height: 46px !important;
    font-size: 11px !important;
    border-radius: 10px !important;
}

/* ── BOT STATUS BAR ────────────────────────────────────────── */
.bot-status-bar {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 12px;
    border-radius: 12px;
    border: 1px solid rgba(255,255,255,0.08);
    background: rgba(255,255,255,0.03);
    margin-bottom: 12px;
}

/* ── PROFIT FACTOR BAR ─────────────────────────────────────── */
.pf-bar {
    height: 6px;
    border-radius: 999px;
    background: rgba(255,255,255,0.08);
    overflow: hidden;
    margin-top: 4px;
}
.pf-fill {
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg,#34d399,#60a5fa);
}

/* ── REGIME BADGE ──────────────────────────────────────────── */
.regime-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 12px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 900;
    letter-spacing: 0.04em;
}
.regime-bull   { background: rgba(52,211,153,0.20); color: #34d399; border: 1px solid rgba(52,211,153,0.30); }
.regime-bear   { background: rgba(239,68,68,0.20);  color: #fb7185; border: 1px solid rgba(239,68,68,0.30); }
.regime-range  { background: rgba(251,191,36,0.20); color: #fbbf24; border: 1px solid rgba(251,191,36,0.30); }
.regime-unkown { background: rgba(255,255,255,0.05);color: #94a3b8; border: 1px solid rgba(255,255,255,0.10);}
</style>
""", unsafe_allow_html=True)


# ============================================================
# HELPERS — basis functies
# ============================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log_debug(msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    st.session_state.debug_events = [f"{stamp} | {msg}"] + st.session_state.debug_events[:49]


def capture_exc(prefix: str, err: Exception) -> None:
    text = f"{prefix}: {type(err).__name__}: {err}"
    st.session_state.last_error_text = text
    log_debug(text)


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


def format_money(v: Any) -> str:
    val = safe_float(v)
    sign = "+" if val > 0 else ""
    return f"{sign}€{abs(val):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_r(v: Any) -> str:
    val = safe_float(v)
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.2f} R"


def format_pct(v: Any) -> str:
    return f"{safe_float(v):.1f}%"


def format_price(v: Any) -> str:
    val = safe_float(v)
    if val >= 1000:
        return f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if val >= 1:
        return f"{val:.4f}"
    return f"{val:.6f}"


def format_dt(v: Any) -> str:
    try:
        dt = pd.to_datetime(v, utc=True, errors="coerce")
        if pd.isna(dt):
            return "-"
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


def parse_dt(v: Any) -> Any:
    try:
        return pd.to_datetime(v, utc=True, errors="coerce")
    except Exception:
        return pd.NaT


def ensure_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def safe_json(path: str) -> Tuple[Optional[dict], Optional[str]]:
    try:
        if not os.path.exists(path):
            return None, f"Niet gevonden: {path}"
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        return None, str(e)


def get_status_badge(status: str) -> str:
    s = safe_str(status).upper()
    if s in {"OK", "CONNECTED", "READY", "DB_PRIORITY"}:
        return f'<span class="status-ok">{s}</span>'
    if s in {"WARN", "WARNING", "DEMO", "FALLBACK", "DEMO_NOODMODUS", "DB_MAP_ISSUE"}:
        return f'<span class="status-warn">{s}</span>'
    return f'<span class="status-bad">{s}</span>'


def regime_badge_html(regime: str) -> str:
    r = safe_str(regime).upper()
    cls = {"BULL": "regime-bull", "BEAR": "regime-bear", "RANGE": "regime-range"}.get(r, "regime-unkown")
    emoji = {"BULL": "🟢", "BEAR": "🔴", "RANGE": "🟡"}.get(r, "⚪")
    return f'<span class="regime-badge {cls}">{emoji} {r}</span>'


def section_open() -> None:
    st.markdown('<div class="section-shell">', unsafe_allow_html=True)


def section_close() -> None:
    st.markdown("</div>", unsafe_allow_html=True)


def subtle_divider() -> None:
    st.markdown('<div class="section-divider-subtle"></div>', unsafe_allow_html=True)


def metric_card(label: str, value: str, delta: str = "", accent: str = "blue", delta_color: str = "") -> str:
    acc_class = {
        "blue": "blue-accent", "green": "green-accent",
        "red": "red-accent", "orange": "orange-accent",
        "purple": "purple-accent",
    }.get(accent, "blue-accent")

    val_class = {
        "blue": "accent-blue", "green": "accent-green",
        "red": "accent-red", "orange": "accent-orange",
        "purple": "accent-purple",
    }.get(accent, "")

    delta_html = ""
    if delta:
        d_color = delta_color or ("#34d399" if not delta.startswith("-") else "#fb7185")
        delta_html = f'<div class="metric-delta" style="color:{d_color}">{delta}</div>'

    return f"""
    <div class="metric-card {acc_class}">
        <div class="metric-value {val_class}">{value}</div>
        {delta_html}
        <div class="metric-label">{label}</div>
    </div>
    """


def downsample(df: pd.DataFrame, max_pts: int = 1000) -> pd.DataFrame:
    if df.empty or len(df) <= max_pts:
        return df
    step = max(1, math.ceil(len(df) / max_pts))
    return df.iloc[::step].copy()


# ============================================================
# DATABASE — sslmode="require" identiek aan alle bestanden
# ============================================================
def db_ready() -> bool:
    return bool(DATABASE_URL)


@st.cache_resource
def get_db_conn():
    if not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(
            DATABASE_URL,
            sslmode="require",
            connect_timeout=DB_CONNECT_TIMEOUT,
            options=f"-c statement_timeout={DB_STATEMENT_TIMEOUT}",
        )
        conn.autocommit = True
        return conn
    except Exception as e:
        capture_exc("get_db_conn", e)
        return None


def run_query(sql: str, params: Optional[tuple] = None) -> pd.DataFrame:
    conn = get_db_conn()
    if not conn:
        return pd.DataFrame()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            rows = cur.fetchall()
            return pd.DataFrame([dict(r) for r in rows])
    except Exception as e:
        capture_exc("run_query", e)
        try:
            st.cache_resource.clear()
        except Exception:
            pass
        return pd.DataFrame()


def run_scalar(sql: str, params: Optional[tuple] = None, default: Any = None) -> Any:
    df = run_query(sql, params)
    if df.empty:
        return default
    return df.iloc[0, 0]


@st.cache_data(ttl=25, show_spinner=False)
def table_exists(table: str) -> bool:
    result = run_scalar(
        "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s)",
        (table,), False,
    )
    return bool(result)


@st.cache_data(ttl=25, show_spinner=False)
def get_table_cols(table: str) -> List[str]:
    df = run_query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
        (table,),
    )
    if df.empty or "column_name" not in df.columns:
        return []
    return df["column_name"].tolist()


@st.cache_data(ttl=25, show_spinner=False)
def table_count(table: str) -> int:
    if not table_exists(table):
        return 0
    result = run_scalar(f"SELECT COUNT(*) FROM public.{table}", default=0)
    return safe_int(result)


def sql_col(cols: List[str], name: str, cast: str = "text") -> str:
    return f'"{name}"' if name in cols else f"NULL::{cast}"


# ============================================================
# BOT STATE — leest actuele bot status
# ============================================================
@st.cache_data(ttl=15, show_spinner=False)
def get_bot_state_val(key: str, default: str = "") -> str:
    if not table_exists("bot_state"):
        return default
    df = run_query(f"SELECT value FROM {BOT_STATE_TABLE} WHERE key=%s LIMIT 1", (key,))
    if df.empty or "value" not in df.columns:
        return default
    return safe_str(df.iloc[0]["value"], default)


def get_bot_status() -> Tuple[str, str, str]:
    """Geeft (label, emoji, status_type) terug voor de top balk."""
    active = get_bot_state_val("bot_active", "false").lower() == "true"
    paused = get_bot_state_val("bot_paused", "false").lower() == "true"

    if not active:
        return "GESTOPT", "🔴", "stopped"
    if paused:
        reason = get_bot_state_val("bot_paused_reason", "onbekend")
        until  = get_bot_state_val("bot_paused_until", "")
        label  = f"GEPAUZEERD — {reason}"
        if until:
            try:
                until_dt = datetime.fromisoformat(until)
                mins = int((until_dt - now_utc()).total_seconds() / 60)
                if mins > 0:
                    label += f" ({mins}m)"
            except Exception:
                pass
        return label, "⏸️", "paused"
    return "ACTIEF", "🟢", "active"


# ============================================================
# BTC REGIME
# ============================================================
@st.cache_data(ttl=60, show_spinner=False)
def get_btc_regime() -> Dict[str, Any]:
    if not table_exists("btc_regime_4h"):
        return {"regime": "UNKNOWN", "strength": 0.0, "close": 0.0, "ema200": 0.0}
    df = run_query(
        "SELECT regime, strength, pct_from_ema, close, ema200, ts_utc "
        "FROM public.btc_regime_4h ORDER BY open_time DESC LIMIT 1"
    )
    return dict(df.iloc[0]) if not df.empty else {"regime": "UNKNOWN", "strength": 0.0}


# ============================================================
# TRADE DATA — laden en normaliseren
# ============================================================
def empty_trade_df() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "trade_id", "symbol", "setup_type", "timeframe", "regime",
        "label", "score", "chance", "confidence",
        "entry", "stop", "target", "pnl_r", "pnl_eur",
        "outcome", "source", "trade_type", "is_shadow",
        "created_at", "closed_at", "datetime", "day",
    ])


def normalize_trade_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return empty_trade_df()
    out = df.copy()

    for col in ["score", "chance", "confidence", "entry", "stop", "target", "pnl_r", "pnl_eur"]:
        out[col] = pd.to_numeric(out.get(col, 0), errors="coerce").fillna(0.0) if col in out.columns else 0.0

    for col in ["created_at", "closed_at"]:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors="coerce", utc=True)
        else:
            out[col] = pd.NaT

    for col in ["trade_id", "symbol", "setup_type", "timeframe", "regime", "label", "outcome", "source", "trade_type"]:
        if col not in out.columns:
            out[col] = "-"
        out[col] = out[col].fillna("-").astype(str)

    if "is_shadow" not in out.columns:
        out["is_shadow"] = False
    out["is_shadow"] = out["is_shadow"].fillna(False)

    out["trade_type"] = out["trade_type"].str.upper()
    out["source"]     = out["source"].str.upper()
    out["outcome"]    = out["outcome"].str.upper()

    datetime_raw = out["closed_at"].where(~out["closed_at"].isna(), out["created_at"])
    out["datetime"] = datetime_raw.apply(format_dt)
    out["day"] = pd.to_datetime(datetime_raw, errors="coerce", utc=True).dt.strftime("%Y-%m-%d")
    out["_datetime_raw"] = datetime_raw

    if "pnl_eur" not in out.columns or out["pnl_eur"].abs().sum() < 0.001:
        out["pnl_eur"] = out["pnl_r"] * 25.0

    return out


def build_trades_sql(kind: str, limit: int) -> str:
    """Bouwt een SQL query voor experience_trades op basis van de beschikbare kolommen."""
    cols = get_table_cols("experience_trades")
    if not cols:
        return ""

    def first(*names: str) -> Optional[str]:
        for n in names:
            if n in cols:
                return n
        return None

    def txt(*names: str) -> str:
        n = first(*names)
        return f'"{n}"::text' if n else "NULL::text"

    def num(*names: str) -> str:
        n = first(*names)
        return f'"{n}"::double precision' if n else "NULL::double precision"

    def dt(*names: str) -> str:
        candidates = [n for n in names if n in cols]
        if not candidates:
            return "NULL::timestamptz"
        return "COALESCE(" + ", ".join(f'"{n}"' for n in candidates) + ")"

    def bool_col(*names: str) -> str:
        n = first(*names)
        return f'COALESCE("{n}", FALSE)' if n else "FALSE"

    result_r  = first("result_r", "pnl_r", "r_multiple", "realized_r")
    pnl_eur_c = first("pnl_eur", "result_eur", "realized_pnl_eur", "profit_eur", "net_pnl_eur")

    pnl_r_expr   = f'"{result_r}"::double precision'  if result_r  else "NULL::double precision"
    pnl_eur_expr = f'"{pnl_eur_c}"::double precision' if pnl_eur_c else "NULL::double precision"

    kind_u = kind.upper()
    if "source" in cols:
        if kind_u == "SIM":
            where = "UPPER(COALESCE(src_calc, '')) = 'SIM'"
        elif kind_u == "SHADOW":
            where = "UPPER(COALESCE(src_calc, '')) = 'SHADOW'"
        elif kind_u == "REAL":
            where = "UPPER(COALESCE(src_calc, '')) IN ('REAL', 'LIVE', 'REAL_REVIEW')"
        else:
            where = "1=1"
    elif "is_shadow" in cols:
        if kind_u == "SHADOW":
            where = "COALESCE(is_shadow_calc, FALSE) = TRUE"
        elif kind_u == "REAL":
            where = "COALESCE(is_shadow_calc, FALSE) = FALSE AND src_calc NOT IN ('SIM')"
        elif kind_u == "SIM":
            where = "1=0"
        else:
            where = "1=1"
    else:
        where = "1=1"

    return f"""
    WITH base AS (
        SELECT
            {txt("trade_key","id")}        AS trade_id,
            {txt("coin","symbol")}          AS symbol,
            {txt("setup_type")}             AS setup_type,
            {txt("entry_timeframe","timeframe","regime_timeframe")} AS timeframe,
            {txt("market_regime","regime")} AS regime,
            {txt("grade","label")}          AS label,
            COALESCE({num("score","bot_confidence","confidence")}, 0) AS score,
            COALESCE({num("chance")}, 0)    AS chance,
            COALESCE({num("confidence","bot_confidence")}, 0) AS confidence,
            {num("entry")}                  AS entry,
            {num("stop")}                   AS stop,
            {num("target")}                 AS target,
            {num("mfe")}                    AS mfe,
            {num("mae")}                    AS mae,
            {pnl_r_expr}                    AS pnl_r_raw,
            {pnl_eur_expr}                  AS pnl_eur_raw,
            {txt("outcome")}                AS outcome_raw,
            {txt("source")}                 AS src_raw,
            {bool_col("is_shadow")}         AS is_shadow_calc,
            {dt("entry_time","timestamp","created_at")} AS created_at,
            {dt("exit_time","closed_at")}               AS closed_at
        FROM public.experience_trades
    ),
    mapped AS (
        SELECT *,
            UPPER(COALESCE(src_raw, ''))   AS src_calc,
            UPPER(COALESCE(outcome_raw,'')) AS outcome_u
        FROM base
    )
    SELECT
        trade_id, symbol, setup_type, timeframe, regime, label,
        score, chance, confidence, entry, stop, target, mfe, mae,
        COALESCE(pnl_r_raw,
            CASE outcome_u WHEN 'WIN' THEN 2.0 WHEN 'LOSS' THEN -1.0 ELSE 0.0 END
        ) AS pnl_r,
        COALESCE(pnl_eur_raw,
            CASE outcome_u WHEN 'WIN' THEN 50.0 WHEN 'LOSS' THEN -25.0 ELSE 0.0 END
        ) AS pnl_eur,
        outcome_u AS outcome,
        src_calc AS source,
        CASE
            WHEN src_calc = 'SIM'    THEN 'SIM'
            WHEN src_calc = 'SHADOW' THEN 'SHADOW'
            WHEN src_calc IN ('REAL','LIVE','REAL_REVIEW') THEN 'REAL'
            WHEN is_shadow_calc THEN 'SHADOW'
            ELSE 'OTHER'
        END AS trade_type,
        is_shadow_calc AS is_shadow,
        created_at, closed_at
    FROM mapped
    WHERE {where}
    ORDER BY COALESCE(closed_at, created_at) DESC NULLS LAST
    LIMIT {int(limit)}
    """


@st.cache_data(ttl=25, show_spinner=False)
def load_real_trades()   -> pd.DataFrame:
    sql = build_trades_sql("REAL", REAL_LIMIT)
    return normalize_trade_df(run_query(sql)) if sql else empty_trade_df()


@st.cache_data(ttl=25, show_spinner=False)
def load_sim_trades()    -> pd.DataFrame:
    sql = build_trades_sql("SIM", SIM_LIMIT)
    return normalize_trade_df(run_query(sql)) if sql else empty_trade_df()


@st.cache_data(ttl=25, show_spinner=False)
def load_shadow_trades() -> pd.DataFrame:
    sql = build_trades_sql("SHADOW", SHADOW_LIMIT)
    return normalize_trade_df(run_query(sql)) if sql else empty_trade_df()


@st.cache_data(ttl=25, show_spinner=False)
def load_all_trades()    -> pd.DataFrame:
    sql = build_trades_sql("ALL", HISTORY_LIMIT)
    return normalize_trade_df(run_query(sql)) if sql else empty_trade_df()


@st.cache_data(ttl=25, show_spinner=False)
def load_pending_signals() -> pd.DataFrame:
    if not table_exists("pending_approvals"):
        return pd.DataFrame()
    cols = get_table_cols("pending_approvals")

    def c(n: str, cast: str = "text") -> str:
        return sql_col(cols, n, cast)

    sql = f"""
    SELECT
        {c("id")}          AS id,
        COALESCE({c("symbol")},{c("coin")}) AS symbol,
        {c("status")}      AS status,
        {c("setup_type")}  AS setup_type,
        COALESCE({c("regime")},{c("market_regime")}) AS regime,
        COALESCE({c("score","double precision")},0)  AS score,
        COALESCE({c("chance","double precision")},0) AS chance,
        COALESCE({c("confidence","double precision")},0) AS confidence,
        {c("entry","double precision")} AS entry,
        {c("stop","double precision")}  AS stop,
        {c("target","double precision")} AS target,
        COALESCE({c("timeframe")},{c("entry_timeframe")}) AS timeframe,
        {c("why_tag")}      AS why_tag,
        {c("exp_win_rate","double precision")} AS exp_win_rate,
        {c("exp_n","double precision")}        AS exp_n,
        COALESCE({c("created_at","timestamptz")},{c("timestamp","timestamptz")}) AS created_at,
        {c("expires_at","timestamptz")} AS expires_at
    FROM public.pending_approvals
    WHERE COALESCE({c("status")},'PENDING') IN ('PENDING','APPROVED')
      AND ({c("expires_at","timestamptz")} IS NULL OR {c("expires_at","timestamptz")} > NOW())
    ORDER BY COALESCE({c("score","double precision")},0) DESC
    LIMIT {PENDING_LIMIT}
    """
    df = run_query(sql)
    if df.empty:
        return df
    for col in ["score","chance","confidence","entry","stop","target","exp_win_rate","exp_n"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    for col in ["created_at","expires_at"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
    return df


@st.cache_data(ttl=25, show_spinner=False)
def load_scoreboard() -> pd.DataFrame:
    if not table_exists("experience_scoreboard"):
        return pd.DataFrame()
    cols = get_table_cols("experience_scoreboard")

    def c(n: str, cast: str = "text") -> str:
        return sql_col(cols, n, cast)

    regime_expr = (
        'COALESCE("market_regime","regime")' if "market_regime" in cols and "regime" in cols
        else '"market_regime"' if "market_regime" in cols
        else '"regime"' if "regime" in cols
        else "NULL::text"
    )

    sql = f"""
    SELECT
        {c("setup_type")}  AS setup_type,
        {regime_expr}      AS market_regime,
        COALESCE({c("n","double precision")},0) AS n,
        COALESCE({c("wins","double precision")},0) AS wins,
        COALESCE({c("losses","double precision")},0) AS losses,
        COALESCE({c("win_rate","double precision")},0) AS win_rate,
        COALESCE({c("avg_pnl_eur","double precision")},0) AS avg_pnl,
        COALESCE({c("avg_r","double precision")},0) AS avg_r,
        {c("updated_at","timestamptz")} AS updated_at
    FROM public.experience_scoreboard
    ORDER BY COALESCE({c("n","double precision")},0) DESC
    LIMIT {SCOREBOARD_LIMIT}
    """
    df = run_query(sql)
    if df.empty:
        return df
    for col in ["n","wins","losses","win_rate","avg_pnl","avg_r"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    return df


@st.cache_data(ttl=25, show_spinner=False)
def load_market_regime_overview() -> pd.DataFrame:
    if not table_exists("market_regime"):
        return pd.DataFrame()
    return run_query("""
    SELECT COALESCE(regime,'UNKNOWN') AS regime, COUNT(*) AS n,
           ROUND(AVG(COALESCE(strength,0))::numeric,1) AS gem_strength
    FROM public.market_regime
    WHERE asof_ts >= NOW() - INTERVAL '8 hours'
    GROUP BY 1 ORDER BY n DESC
    """)


def get_all_trade_data() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, str]:
    """Laadt alle trade data — met fallback naar demo data als DB leeg is."""
    real_df   = load_real_trades()
    sim_df    = load_sim_trades()
    shadow_df = load_shadow_trades()
    all_df    = load_all_trades()

    frames = [df for df in [real_df, sim_df, shadow_df] if not df.empty]
    combined = normalize_trade_df(pd.concat(frames, ignore_index=True)) if frames else empty_trade_df()

    if not all_df.empty:
        history_df = all_df
        source_mode = "DB_PRIORITY"
    elif not combined.empty:
        history_df = combined
        source_mode = "DB_PARTIAL"
    else:
        history_df = _demo_trades()
        real_df    = history_df[history_df["trade_type"] == "REAL"].copy()
        sim_df     = history_df[history_df["trade_type"] == "SIM"].copy()
        shadow_df  = history_df[history_df["trade_type"] == "SHADOW"].copy()
        source_mode = "DEMO_NOODMODUS"

    return real_df, sim_df, shadow_df, history_df, source_mode


def _demo_trades() -> pd.DataFrame:
    """Fallback demo data — alleen als DB leeg is."""
    rows = [
        {"trade_id":"r1","symbol":"BTCUSDT","setup_type":"BREAKOUT","timeframe":"1H","regime":"BULL","label":"A","score":84,"chance":77,"confidence":82,"entry":67200,"stop":66500,"target":68800,"pnl_r":2.0,"pnl_eur":48.0,"outcome":"WIN","source":"REAL","trade_type":"REAL","is_shadow":False,"created_at":"2026-03-15T09:10:00Z","closed_at":"2026-03-15T13:40:00Z"},
        {"trade_id":"r2","symbol":"ETHUSDT","setup_type":"TREND_PULLBACK","timeframe":"4H","regime":"BULL","label":"B","score":73,"chance":69,"confidence":70,"entry":3550,"stop":3488,"target":3660,"pnl_r":-1.0,"pnl_eur":-24.0,"outcome":"LOSS","source":"REAL","trade_type":"REAL","is_shadow":False,"created_at":"2026-03-16T08:20:00Z","closed_at":"2026-03-16T11:15:00Z"},
        {"trade_id":"r3","symbol":"DOGEUSDT","setup_type":"TREND_PULLBACK","timeframe":"1H","regime":"BULL","label":"A","score":79,"chance":75,"confidence":78,"entry":0.212,"stop":0.207,"target":0.225,"pnl_r":2.0,"pnl_eur":48.0,"outcome":"WIN","source":"REAL","trade_type":"REAL","is_shadow":False,"created_at":"2026-03-17T13:10:00Z","closed_at":"2026-03-17T18:20:00Z"},
        {"trade_id":"s1","symbol":"SOLUSDT","setup_type":"BREAKOUT","timeframe":"4H","regime":"BULL","label":"A","score":88,"chance":82,"confidence":86,"entry":168,"stop":162,"target":182,"pnl_r":2.0,"pnl_eur":48.0,"outcome":"WIN","source":"SIM","trade_type":"SIM","is_shadow":False,"created_at":"2026-03-15T10:00:00Z","closed_at":"2026-03-15T22:20:00Z"},
        {"trade_id":"s2","symbol":"AVAXUSDT","setup_type":"RANGE_RECLAIM","timeframe":"1H","regime":"RANGE","label":"B","score":62,"chance":58,"confidence":61,"entry":39.1,"stop":37.9,"target":41.8,"pnl_r":-1.0,"pnl_eur":-24.0,"outcome":"LOSS","source":"SIM","trade_type":"SIM","is_shadow":False,"created_at":"2026-03-16T11:45:00Z","closed_at":"2026-03-16T15:05:00Z"},
        {"trade_id":"sh1","symbol":"STORJUSDT","setup_type":"TREND_PULLBACK","timeframe":"1H","regime":"BULL","label":"C","score":57,"chance":55,"confidence":58,"entry":0.79,"stop":0.76,"target":0.86,"pnl_r":2.0,"pnl_eur":48.0,"outcome":"WIN","source":"SHADOW","trade_type":"SHADOW","is_shadow":True,"created_at":"2026-03-18T08:10:00Z","closed_at":"2026-03-18T16:25:00Z"},
        {"trade_id":"sh2","symbol":"DOGEUSDT","setup_type":"BREAKOUT","timeframe":"15M","regime":"CHOPPY","label":"C","score":49,"chance":46,"confidence":50,"entry":0.204,"stop":0.199,"target":0.214,"pnl_r":-1.0,"pnl_eur":-24.0,"outcome":"LOSS","source":"SHADOW","trade_type":"SHADOW","is_shadow":True,"created_at":"2026-03-19T09:10:00Z","closed_at":"2026-03-19T11:15:00Z"},
    ]
    return normalize_trade_df(pd.DataFrame(rows))


# ============================================================
# STATISTIEKEN
# ============================================================
def perf_summary(df: pd.DataFrame) -> Dict[str, float]:
    """Berekent volledige performance statistieken uit een trade dataframe."""
    empty = {
        "count":0.0,"winrate":0.0,"total_r":0.0,"avg_r":0.0,
        "expectancy":0.0,"max_dd":0.0,"total_eur":0.0,"avg_eur":0.0,
        "gross_profit":0.0,"gross_loss":0.0,"money_winrate":0.0,
        "profit_factor":0.0,
    }
    if df.empty:
        return empty

    work = df[df["outcome"].isin(["WIN","LOSS"])].copy()
    if work.empty:
        return {**empty, "count": float(len(df))}

    pnl   = pd.to_numeric(work["pnl_r"],   errors="coerce").fillna(0.0)
    euros = pd.to_numeric(work["pnl_eur"], errors="coerce").fillna(pnl * 25.0)

    total_r = float(pnl.sum())
    avg_r   = float(pnl.mean())
    wins    = float((pnl > 0).mean() * 100)
    exp     = (wins / 100 * 2.0) + ((1 - wins / 100) * -1.0)

    gross_p = float(euros[euros > 0].sum())
    gross_l = float(abs(euros[euros < 0].sum()))
    money_base = gross_p + gross_l
    money_wr   = (gross_p / money_base * 100) if money_base > 0 else 0.0
    pf         = (gross_p / max(gross_l, 0.001))

    curve = pnl.cumsum()
    peak  = curve.cummax()
    dd    = float((curve - peak).min()) if len(curve) else 0.0

    return {
        "count": float(len(work)),
        "winrate": wins,
        "total_r": total_r,
        "avg_r": avg_r,
        "expectancy": exp,
        "max_dd": dd,
        "total_eur": float(euros.sum()),
        "avg_eur": float(euros.mean()),
        "gross_profit": gross_p,
        "gross_loss": gross_l,
        "money_winrate": money_wr,
        "profit_factor": round(pf, 2),
    }


def get_profit_factor_30d(df: pd.DataFrame) -> float:
    """Berekent profit factor over de laatste 30 dagen."""
    if df.empty:
        return 0.0
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)
    work = df[
        df["outcome"].isin(["WIN","LOSS"]) &
        (df["_datetime_raw"] >= cutoff)
    ].copy() if "_datetime_raw" in df.columns else df[df["outcome"].isin(["WIN","LOSS"])].copy()
    if work.empty:
        return 0.0
    euros = pd.to_numeric(work["pnl_eur"], errors="coerce").fillna(0.0)
    gross_p = float(euros[euros > 0].sum())
    gross_l = float(abs(euros[euros < 0].sum()))
    return round(gross_p / max(gross_l, 0.001), 2)


def get_consecutive_losses(df: pd.DataFrame) -> int:
    """Telt opeenvolgende verliezen."""
    if df.empty:
        return 0
    work = df[df["outcome"].isin(["WIN","LOSS"])].copy()
    if "_datetime_raw" in work.columns:
        work = work.sort_values("_datetime_raw", ascending=False)
    count = 0
    for _, row in work.head(10).iterrows():
        if row["outcome"] == "LOSS":
            count += 1
        else:
            break
    return count


def get_daily_pnl_today(df: pd.DataFrame) -> float:
    """PnL van vandaag."""
    if df.empty:
        return 0.0
    today = now_utc().strftime("%Y-%m-%d")
    work = df[
        df["outcome"].isin(["WIN","LOSS"]) &
        (df["day"] == today)
    ].copy()
    if work.empty:
        return 0.0
    euros = pd.to_numeric(work["pnl_eur"], errors="coerce").fillna(0.0)
    return float(euros.sum())


# ============================================================
# NIEUWE DATA FUNCTIES — alle ontbrekende metrics
# ============================================================
def get_overall_winloss(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Berekent overall win/loss % per source.
    Dit is de centrale metric die altijd zichtbaar moet zijn.
    """
    result: Dict[str, Any] = {}
    for source_key, sources in [
        ("ALLES",  ["WIN","LOSS"]),
        ("REAL",   ["WIN","LOSS"]),
        ("SIM",    ["WIN","LOSS"]),
        ("SHADOW", ["WIN","LOSS"]),
    ]:
        if source_key == "ALLES":
            work = df[df["outcome"].isin(["WIN","LOSS"])].copy()
        else:
            work = df[
                (df["trade_type"] == source_key) &
                df["outcome"].isin(["WIN","LOSS"])
            ].copy()

        if work.empty:
            result[source_key] = {"wins":0,"losses":0,"total":0,"win_pct":0.0,"loss_pct":0.0}
            continue

        wins   = int((work["outcome"] == "WIN").sum())
        losses = int((work["outcome"] == "LOSS").sum())
        total  = wins + losses
        result[source_key] = {
            "wins":     wins,
            "losses":   losses,
            "total":    total,
            "win_pct":  (wins / total * 100) if total > 0 else 0.0,
            "loss_pct": (losses / total * 100) if total > 0 else 0.0,
        }
    return result


@st.cache_data(ttl=30, show_spinner=False)
def get_floating_pnl() -> List[Dict[str, Any]]:
    """
    Haalt open live trades op met huidige prijs van Bitvavo.
    Berekent floating PnL per open trade.
    """
    live_state, _ = safe_json(LIVE_STATE_PATH)
    if not live_state:
        return []

    positions = (live_state or {}).get("positions", {})
    if not positions:
        return []

    try:
        prices = fetch_bitvavo_prices()
    except Exception:
        prices = {}

    result = []
    for symbol, pos in positions.items():
        entry  = safe_float(pos.get("entry"))
        qty    = safe_float(pos.get("qty"))
        stop   = safe_float(pos.get("stop_loss") or pos.get("stop"))
        target = safe_float(pos.get("target"))
        amount = safe_float(pos.get("amount_eur"))
        setup  = safe_str(pos.get("setup_type"), "-")
        opened = safe_float(pos.get("opened_at"))
        market = safe_str(pos.get("market"), f"{symbol[:-4]}-EUR" if symbol.endswith("USDT") else symbol)

        current = prices.get(market)
        if current is None:
            base = symbol[:-4] if symbol.endswith("USDT") else symbol
            current = prices.get(f"{base}-EUR")

        if current and entry > 0 and qty > 0:
            float_pnl = (current - entry) * qty
            float_r   = (current - entry) / abs(entry - stop) if stop > 0 and abs(entry - stop) > 0 else 0.0
        else:
            float_pnl = 0.0
            float_r   = 0.0

        hold_min = (time.time() - opened) / 60 if opened > 0 else 0
        rr = abs(target - entry) / abs(entry - stop) if stop > 0 and target > 0 and abs(entry-stop) > 0 else 0.0

        result.append({
            "symbol":     symbol,
            "market":     market,
            "setup":      setup,
            "entry":      entry,
            "stop":       stop,
            "target":     target,
            "current":    current or 0.0,
            "qty":        qty,
            "amount_eur": amount,
            "float_pnl":  float_pnl,
            "float_r":    float_r,
            "hold_min":   hold_min,
            "rr":         rr,
        })

    return sorted(result, key=lambda x: x["float_pnl"], reverse=True)


@st.cache_data(ttl=20, show_spinner=False)
def get_scanner_status() -> Dict[str, Any]:
    """
    Haalt scanner status op uit bot_state tabel en pending_approvals.
    Toont wanneer de scanner voor het last draaide en hoeveel signals.
    """
    result = {
        "bot_active":        get_bot_state_val("bot_active", "false"),
        "bot_paused":        get_bot_state_val("bot_paused", "false"),
        "last_scan":         get_bot_state_val("last_scan_ts", ""),
        "signals_today":     0,
        "signals_executed":  0,
        "signals_pending":   0,
        "mins_since_scan":   -1,
    }

    # Toon hoelang geleden de laatste scan was
    if result["last_scan"]:
        try:
            last = datetime.fromisoformat(result["last_scan"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            result["mins_since_scan"] = int((now_utc() - last).total_seconds() / 60)
        except Exception:
            pass

    # Signals van vandaag
    if table_exists("pending_approvals"):
        today = now_utc().strftime("%Y-%m-%d")
        df_today = run_query(
            "SELECT status, COUNT(*) AS n FROM public.pending_approvals "
            "WHERE DATE(created_at AT TIME ZONE 'UTC') = %s GROUP BY 1",
            (today,)
        )
        if not df_today.empty and "status" in df_today.columns:
            for _, row in df_today.iterrows():
                s = safe_str(row.get("status","")).upper()
                n = safe_int(row.get("n",0))
                result["signals_today"] += n
                if s in ("CONSUMED","EXECUTED"):
                    result["signals_executed"] += n
                if s in ("PENDING","APPROVED"):
                    result["signals_pending"] += n

    return result


@st.cache_data(ttl=20, show_spinner=False)
def get_dagbudget_status(real_df: pd.DataFrame) -> Dict[str, Any]:
    """
    Berekent hoeveel van het dagbudget gebruikt is.
    Dagbudget = DAILY_STOP_LOSS_EUR (€5.00 standaard).
    """
    today = now_utc().strftime("%Y-%m-%d")
    work  = real_df[
        real_df["outcome"].isin(["WIN","LOSS"]) &
        (real_df["day"] == today)
    ].copy() if not real_df.empty else pd.DataFrame()

    wins_today   = int((work["outcome"] == "WIN").sum()) if not work.empty else 0
    losses_today = int((work["outcome"] == "LOSS").sum()) if not work.empty else 0
    pnl_today    = float(pd.to_numeric(work.get("pnl_eur", pd.Series()), errors="coerce").fillna(0.0).sum()) if not work.empty else 0.0
    trades_today = wins_today + losses_today

    verlies_abs = abs(min(pnl_today, 0.0))
    budget_pct  = min((verlies_abs / max(DAILY_STOP_LOSS_EUR, 0.01)) * 100, 100.0)
    ruimte      = max(DAILY_STOP_LOSS_EUR - verlies_abs, 0.0)

    return {
        "pnl_today":     pnl_today,
        "verlies_abs":   verlies_abs,
        "budget_max":    DAILY_STOP_LOSS_EUR,
        "budget_pct":    budget_pct,
        "ruimte":        ruimte,
        "trades_today":  trades_today,
        "wins_today":    wins_today,
        "losses_today":  losses_today,
        "max_trades":    MAX_REAL_TRADES_PER_DAY,
    }


@st.cache_data(ttl=60, show_spinner=False)
def get_blacklist_cooldown_coins() -> Dict[str, List[Dict]]:
    """
    Haalt coins op die geblokkeerd zijn.
    Blacklist: win rate <30% na 20+ trades.
    Cooldown:  24u na verlies.
    Whitelist: win rate >60% na 20+ trades.
    """
    if not table_exists("experience_trades"):
        return {"blacklist":[], "cooldown":[], "whitelist":[]}

    # Blacklist — slechte performers
    df_bl = run_query("""
    SELECT COALESCE(coin,'?') AS coin, COUNT(*) AS n,
           COUNT(*) FILTER (WHERE UPPER(outcome)='WIN') AS wins,
           ROUND(COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')::numeric/NULLIF(COUNT(*),0)*100,1) AS win_rate
    FROM public.experience_trades
    WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE','SIM','SHADOW')
      AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
    GROUP BY 1
    HAVING COUNT(*) >= 20
      AND COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')::numeric/NULLIF(COUNT(*),0) < 0.30
    ORDER BY win_rate ASC LIMIT 20
    """)

    # Cooldown — verlies in laatste 24u
    df_cd = run_query("""
    SELECT DISTINCT ON (coin) coin,
           MAX(exit_time) AS last_loss,
           ROUND(EXTRACT(EPOCH FROM (NOW()-MAX(exit_time)))/3600,1) AS hours_since
    FROM public.experience_trades
    WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
      AND UPPER(outcome) = 'LOSS'
      AND exit_time IS NOT NULL
      AND exit_time >= NOW() - INTERVAL '24 hours'
    GROUP BY coin
    ORDER BY coin, last_loss DESC
    LIMIT 30
    """)

    # Whitelist — goede performers
    df_wl = run_query("""
    SELECT COALESCE(coin,'?') AS coin, COUNT(*) AS n,
           COUNT(*) FILTER (WHERE UPPER(outcome)='WIN') AS wins,
           ROUND(COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')::numeric/NULLIF(COUNT(*),0)*100,1) AS win_rate,
           ROUND(AVG(COALESCE(pnl_eur,0))::numeric,4) AS avg_pnl
    FROM public.experience_trades
    WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE','SIM','SHADOW')
      AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
    GROUP BY 1
    HAVING COUNT(*) >= 20
      AND COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')::numeric/NULLIF(COUNT(*),0) >= 0.60
    ORDER BY win_rate DESC LIMIT 20
    """)

    def df_to_list(df: pd.DataFrame) -> List[Dict]:
        return [dict(r) for _, r in df.iterrows()] if not df.empty else []

    return {
        "blacklist": df_to_list(df_bl),
        "cooldown":  df_to_list(df_cd),
        "whitelist": df_to_list(df_wl),
    }


@st.cache_data(ttl=60, show_spinner=False)
def get_fee_stats(real_df: pd.DataFrame) -> Dict[str, float]:
    """
    Schat totale fees betaald op basis van trades.
    Bitvavo fee = 0.25% per kant.
    """
    if real_df.empty:
        return {"total_fees": 0.0, "fee_impact_pct": 0.0, "avg_fee_per_trade": 0.0, "gross_profit": 0.0}

    work = real_df[real_df["outcome"].isin(["WIN","LOSS"])].copy()
    if work.empty:
        return {"total_fees": 0.0, "fee_impact_pct": 0.0, "avg_fee_per_trade": 0.0, "gross_profit": 0.0}

    euros  = pd.to_numeric(work.get("pnl_eur", pd.Series()), errors="coerce").fillna(0.0)
    fees_col = work.get("fee_eur") if "fee_eur" in work.columns else None
    if fees_col is not None:
        total_fees = float(pd.to_numeric(fees_col, errors="coerce").fillna(0.0).sum())
    else:
        total_fees = float(len(work) * MAX_PER_TRADE_EUR * 0.005)  # schatting: 0.5% round-trip

    gross_profit = float(euros[euros > 0].sum())
    fee_impact   = (total_fees / max(gross_profit + total_fees, 0.001)) * 100
    avg_fee      = total_fees / max(len(work), 1)

    return {
        "total_fees":        round(total_fees, 4),
        "fee_impact_pct":    round(fee_impact, 1),
        "avg_fee_per_trade": round(avg_fee, 4),
        "gross_profit":      round(gross_profit, 4),
    }


@st.cache_data(ttl=60, show_spinner=False)
def get_best_worst_trades(df: pd.DataFrame, n: int = 5) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Geeft de N beste en slechtste trades terug op basis van pnl_r."""
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()
    work = df[df["outcome"].isin(["WIN","LOSS"])].copy()
    if work.empty:
        return pd.DataFrame(), pd.DataFrame()
    work["pnl_r_num"] = pd.to_numeric(work["pnl_r"], errors="coerce").fillna(0.0)
    best  = work.nlargest(n, "pnl_r_num")
    worst = work.nsmallest(n, "pnl_r_num")
    return best, worst


@st.cache_data(ttl=60, show_spinner=False)
def get_streak_history(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    Berekent alle win/loss streaks in de trade geschiedenis.
    Geeft lijst van {type, length, start, end}.
    """
    if df.empty:
        return []
    work = df[df["outcome"].isin(["WIN","LOSS"])].copy()
    if "_datetime_raw" in work.columns:
        work = work.sort_values("_datetime_raw")
    if work.empty:
        return []

    streaks = []
    current_type   = None
    current_count  = 0
    current_start  = None

    for _, row in work.iterrows():
        outcome = row["outcome"]
        dt      = row.get("_datetime_raw")
        if outcome == current_type:
            current_count += 1
        else:
            if current_type is not None:
                streaks.append({"type": current_type, "length": current_count, "start": current_start, "end": dt})
            current_type  = outcome
            current_count = 1
            current_start = dt

    if current_type:
        streaks.append({"type": current_type, "length": current_count, "start": current_start, "end": None})

    return streaks


@st.cache_data(ttl=60, show_spinner=False)
def get_avg_hold_time(df: pd.DataFrame) -> pd.DataFrame:
    """Gemiddelde houdtijd per setup type in minuten."""
    if df.empty:
        return pd.DataFrame()
    work = df[df["outcome"].isin(["WIN","LOSS"])].copy()
    work = work[~work["created_at"].isna() & ~work["closed_at"].isna()].copy()
    if work.empty:
        return pd.DataFrame()
    work["hold_min"] = (work["closed_at"] - work["created_at"]).dt.total_seconds() / 60
    grouped = (
        work.groupby("setup_type", dropna=False)
        .agg(n=("trade_id","count"), avg_hold=("hold_min","mean"),
             win_rate=("outcome", lambda x: (x=="WIN").mean()*100))
        .reset_index()
        .sort_values("avg_hold", ascending=False)
    )
    grouped["avg_hold"] = grouped["avg_hold"].round(1)
    grouped["win_rate"] = grouped["win_rate"].round(1)
    return grouped


@st.cache_data(ttl=60, show_spinner=False)
def get_rolling_winrate(df: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """Rolling win rate over de laatste N trades."""
    if df.empty:
        return pd.DataFrame()
    work = df[df["outcome"].isin(["WIN","LOSS"])].copy()
    if "_datetime_raw" in work.columns:
        work = work.sort_values("_datetime_raw")
    if len(work) < window:
        return pd.DataFrame()
    work["is_win"] = (work["outcome"] == "WIN").astype(float)
    work["rolling_wr"] = work["is_win"].rolling(window=window, min_periods=window).mean() * 100
    return work[["_datetime_raw","rolling_wr"]].dropna()


@st.cache_data(ttl=60, show_spinner=False)
def get_btc_correlation(df: pd.DataFrame) -> pd.DataFrame:
    """
    Berekent win rate per BTC regime.
    Toont hoe de bot presteert per marktomstandigheid.
    """
    if df.empty:
        return pd.DataFrame()
    work = df[df["outcome"].isin(["WIN","LOSS"])].copy()
    if "regime" not in work.columns or work.empty:
        return pd.DataFrame()
    grouped = (
        work.groupby("regime", dropna=False)
        .agg(n=("trade_id","count"),
             wins=("outcome", lambda x: (x=="WIN").sum()),
             win_rate=("outcome", lambda x: (x=="WIN").mean()*100),
             avg_r=("pnl_r","mean"))
        .reset_index()
        .sort_values("win_rate", ascending=False)
    )
    grouped["win_rate"] = grouped["win_rate"].round(1)
    grouped["avg_r"]    = grouped["avg_r"].round(2)
    return grouped


@st.cache_data(ttl=60, show_spinner=False)
def get_trade_frequency(df: pd.DataFrame) -> pd.DataFrame:
    """Aantal trades per dag voor de laatste 30 dagen."""
    if df.empty:
        return pd.DataFrame()
    work = df.copy()
    if "day" not in work.columns:
        return pd.DataFrame()
    cutoff = (now_utc() - timedelta(days=30)).strftime("%Y-%m-%d")
    work   = work[work["day"] >= cutoff].copy()
    if work.empty:
        return pd.DataFrame()
    grouped = (
        work.groupby("day")
        .agg(n=("trade_id","count"),
             wins=("outcome", lambda x: (x=="WIN").sum()),
             losses=("outcome", lambda x: (x=="LOSS").sum()))
        .reset_index()
        .sort_values("day")
    )
    return grouped


def get_recovery_factor(df: pd.DataFrame) -> float:
    """
    Recovery Factor = Totale netto PnL / Max Drawdown (absoluut).
    Doel: >2.0
    """
    summary = perf_summary(df)
    total_r = summary["total_r"]
    max_dd  = abs(summary["max_dd"])
    if max_dd == 0:
        return 0.0
    return round(total_r / max_dd, 2)


@st.cache_data(ttl=60, show_spinner=False)
def get_calendar_pnl(df: pd.DataFrame, year: int, month: int) -> Dict[str, float]:
    """P&L per dag voor de kalender heatmap."""
    if df.empty:
        return {}
    work = df[df["outcome"].isin(["WIN","LOSS"])].copy()
    if work.empty:
        return {}
    target_prefix = f"{year}-{month:02d}"
    work = work[work["day"].str.startswith(target_prefix)].copy() if "day" in work.columns else pd.DataFrame()
    if work.empty:
        return {}
    euros = pd.to_numeric(work.get("pnl_eur", pd.Series()), errors="coerce").fillna(0.0)
    work["pnl_eur_num"] = euros
    grouped = work.groupby("day")["pnl_eur_num"].sum()
    return dict(grouped)


# ============================================================
# NIEUWE GRAFIEKEN
# ============================================================
def chart_drawdown(df: pd.DataFrame, title: str = "Drawdown") -> go.Figure:
    """Drawdown grafiek — toont wanneer en hoe diep de drawdowns waren."""
    if df.empty:
        return empty_fig("Geen data voor drawdown grafiek")
    work = df[df["outcome"].isin(["WIN","LOSS"])].copy()
    if work.empty:
        return empty_fig("Geen WIN/LOSS data")
    if "_datetime_raw" in work.columns:
        work = work.sort_values("_datetime_raw")
    pnl  = pd.to_numeric(work["pnl_r"], errors="coerce").fillna(0.0)
    cum  = pnl.cumsum()
    peak = cum.cummax()
    dd   = cum - peak
    work = downsample(work.assign(dd=dd.values), 800)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=work.get("_datetime_raw", work.index),
        y=work["dd"],
        mode="lines",
        fill="tozeroy",
        name="Drawdown",
        line=dict(color="#fb7185", width=1.5),
        fillcolor="rgba(239,68,68,0.15)",
        hovertemplate="DD: %{y:.2f} R<extra></extra>",
    ))
    fig.add_hline(y=0, line_color="rgba(255,255,255,0.15)", line_width=1)
    return style_fig(fig, 280, title)


def chart_rolling_winrate(df: pd.DataFrame, window: int = 20) -> go.Figure:
    """Rolling win rate trend grafiek."""
    rwr = get_rolling_winrate(df, window)
    if rwr.empty:
        return empty_fig(f"Min {window} trades nodig voor rolling win rate")
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=rwr["_datetime_raw"],
        y=rwr["rolling_wr"],
        mode="lines",
        name=f"Rolling WR ({window})",
        line=dict(color="#c084fc", width=2.5),
        hovertemplate="WR: %{y:.1f}%<extra></extra>",
    ))
    fig.add_hline(y=50, line_dash="dash", line_color="rgba(255,255,255,0.2)",
                  annotation_text="50%", annotation_font_color="#94a3b8")
    fig.add_hline(y=60, line_dash="dot", line_color="rgba(52,211,153,0.3)",
                  annotation_text="60% doel", annotation_font_color="#34d399")
    return style_fig(fig, 280, f"Rolling Win Rate ({window} trades)")


def chart_trade_frequency(df: pd.DataFrame) -> go.Figure:
    """Trade frequentie per dag."""
    freq = get_trade_frequency(df)
    if freq.empty:
        return empty_fig("Geen frequentie data")
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=freq["day"],
        y=freq["wins"],
        name="Wins",
        marker=dict(color="#34d399"),
    ))
    fig.add_trace(go.Bar(
        x=freq["day"],
        y=freq["losses"],
        name="Losses",
        marker=dict(color="#fb7185"),
    ))
    fig.update_layout(barmode="stack")
    return style_fig(fig, 260, "Trade Frequentie (30 dagen)")


def chart_btc_correlation_bar(df: pd.DataFrame) -> go.Figure:
    """Win rate per marktregime — BTC correlatie grafiek."""
    corr = get_btc_correlation(df)
    if corr.empty:
        return empty_fig("Geen regime data")
    color_map = {"BULL":"#34d399","BEAR":"#fb7185","RANGE":"#fbbf24"}
    colors = [color_map.get(safe_str(r).upper(), "#60a5fa") for r in corr["regime"]]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=corr["regime"],
        y=corr["win_rate"],
        marker=dict(color=colors),
        text=[f"{v:.1f}% ({int(n)})" for v, n in zip(corr["win_rate"], corr["n"])],
        textposition="outside",
        hovertemplate="Regime: %{x}<br>WR: %{y:.1f}%<extra></extra>",
    ))
    fig.add_hline(y=50, line_dash="dash", line_color="rgba(255,255,255,0.2)")
    return style_fig(fig, 260, "Win Rate per Regime (BTC Correlatie)")


def chart_hold_time_bar(df: pd.DataFrame) -> go.Figure:
    """Gemiddelde houdtijd per setup type."""
    hold = get_avg_hold_time(df)
    if hold.empty:
        return empty_fig("Geen houdtijd data")
    fig = go.Figure(go.Bar(
        x=hold["setup_type"],
        y=hold["avg_hold"],
        text=[f"{v:.0f}m ({int(n)})" for v, n in zip(hold["avg_hold"], hold["n"])],
        textposition="outside",
        marker=dict(color="#60a5fa"),
        hovertemplate="Setup: %{x}<br>Gem. houdtijd: %{y:.0f} min<extra></extra>",
    ))
    return style_fig(fig, 260, "Gemiddelde Houdtijd per Setup (minuten)")


def chart_pnl_histogram(df: pd.DataFrame) -> go.Figure:
    """P&L histogram — verdeling van trade uitkomsten in euro."""
    if df.empty:
        return empty_fig("Geen data")
    work = df[df["outcome"].isin(["WIN","LOSS"])].copy()
    if work.empty:
        return empty_fig("Geen WIN/LOSS data")
    euros = pd.to_numeric(work["pnl_eur"], errors="coerce").dropna()
    if euros.empty:
        return empty_fig("Geen PnL data")
    fig = go.Figure(go.Histogram(
        x=euros,
        nbinsx=20,
        marker=dict(
            color=["#34d399" if v >= 0 else "#fb7185" for v in euros],
            line=dict(width=0),
        ),
        opacity=0.8,
    ))
    fig.add_vline(x=0, line_dash="dash", line_color="rgba(255,255,255,0.3)")
    return style_fig(fig, 260, "P&L Verdeling (euro)")


# ============================================================
# FILTERS
# ============================================================
def apply_filters(
    df: pd.DataFrame,
    search:      str = "",
    days:        str = "ALLES",
    trade_type:  str = "ALLES",
    setup:       str = "ALLES",
    regime:      str = "ALLES",
    outcome:     str = "ALLES",
    symbol:      str = "ALLES",
) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()

    if trade_type != "ALLES" and "trade_type" in out.columns:
        out = out[out["trade_type"] == trade_type]
    if setup   != "ALLES": out = out[out["setup_type"] == setup]
    if regime  != "ALLES": out = out[out["regime"] == regime]
    if outcome != "ALLES": out = out[out["outcome"] == outcome]
    if symbol  != "ALLES": out = out[out["symbol"] == symbol]

    if days != "ALLES":
        days_map = {"7D":7,"30D":30,"90D":90,"180D":180,"365D":365}
        d = days_map.get(days)
        if d and "_datetime_raw" in out.columns:
            cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=d)
            out = out[out["_datetime_raw"] >= cutoff]

    if search:
        s = search.lower()
        mask = (
            out["symbol"].str.lower().str.contains(s, na=False) |
            out["setup_type"].str.lower().str.contains(s, na=False) |
            out["regime"].str.lower().str.contains(s, na=False) |
            out["outcome"].str.lower().str.contains(s, na=False) |
            out["trade_id"].str.lower().str.contains(s, na=False)
        )
        out = out[mask]

    if "_datetime_raw" in out.columns:
        out = out.sort_values("_datetime_raw", ascending=False).reset_index(drop=True)

    return out


def render_filters(df: pd.DataFrame, include_trade_type: bool = True) -> pd.DataFrame:
    """Toont filter widgets en geeft gefilterd dataframe terug."""
    st.markdown('<div class="filter-card">', unsafe_allow_html=True)

    c1, c2, c3, c4 = st.columns(4, gap="small")
    with c1:
        st.session_state.search_text = st.text_input(
            "🔍 Zoeken", value=st.session_state.search_text,
            placeholder="coin, setup, id...",
        )
    with c2:
        opts = ["ALLES","7D","30D","90D","180D","365D"]
        idx  = opts.index(st.session_state.global_days_filter) if st.session_state.global_days_filter in opts else 0
        st.session_state.global_days_filter = st.selectbox("📅 Periode", opts, index=idx)
    with c3:
        setups = ["ALLES"] + sorted(df["setup_type"].dropna().astype(str).unique().tolist()) if not df.empty else ["ALLES"]
        cur = st.session_state.global_setup_filter if st.session_state.global_setup_filter in setups else "ALLES"
        st.session_state.global_setup_filter = st.selectbox("⚡ Setup", setups, index=setups.index(cur))
    with c4:
        symbols = ["ALLES"] + sorted(df["symbol"].dropna().astype(str).unique().tolist()) if not df.empty else ["ALLES"]
        cur = st.session_state.global_symbol_filter if st.session_state.global_symbol_filter in symbols else "ALLES"
        st.session_state.global_symbol_filter = st.selectbox("🪙 Coin", symbols, index=symbols.index(cur))

    c5, c6, c7, c8 = st.columns(4, gap="small")
    with c5:
        regimes = ["ALLES"] + sorted(df["regime"].dropna().astype(str).unique().tolist()) if not df.empty else ["ALLES"]
        cur = st.session_state.global_regime_filter if st.session_state.global_regime_filter in regimes else "ALLES"
        st.session_state.global_regime_filter = st.selectbox("🌍 Regime", regimes, index=regimes.index(cur))
    with c6:
        outcomes = ["ALLES","WIN","LOSS"]
        cur = st.session_state.global_outcome_filter if st.session_state.global_outcome_filter in outcomes else "ALLES"
        st.session_state.global_outcome_filter = st.selectbox("🎯 Uitkomst", outcomes, index=outcomes.index(cur))
    with c7:
        if include_trade_type:
            ttypes = ["ALLES","REAL","SIM","SHADOW"]
            cur = st.session_state.global_trade_type_filter if st.session_state.global_trade_type_filter in ttypes else "ALLES"
            st.session_state.global_trade_type_filter = st.selectbox("📊 Type", ttypes, index=ttypes.index(cur))
    with c8:
        st.markdown('<div class="tiny-button" style="margin-top:24px;">', unsafe_allow_html=True)
        if st.button("🔄 Reset", key="filter_reset_btn", use_container_width=True):
            for k in ["search_text","global_days_filter","global_trade_type_filter",
                      "global_setup_filter","global_regime_filter","global_outcome_filter","global_symbol_filter"]:
                st.session_state[k] = SESSION_DEFAULTS.get(k, "ALLES")
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)

    return apply_filters(
        df,
        search     = st.session_state.search_text,
        days       = st.session_state.global_days_filter,
        trade_type = st.session_state.global_trade_type_filter if include_trade_type else "ALLES",
        setup      = st.session_state.global_setup_filter,
        regime     = st.session_state.global_regime_filter,
        outcome    = st.session_state.global_outcome_filter,
        symbol     = st.session_state.global_symbol_filter,
    )


# ============================================================
# BITVAVO API — met correcte HMAC signing
# ============================================================
def bitvavo_request(method: str, path: str, body: str = "") -> Any:
    if not API_KEY or not API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY of BITVAVO_API_SECRET ontbreken.")
    m   = method.upper()
    ts  = str(int(time.time() * 1000))
    msg = f"{ts}{m}{path}{body}"
    sig = hmac.new(
        API_SECRET.encode("utf-8"),
        msg.encode("utf-8"),
        digestmod=hashlib.sha256,  # FIX: digestmod vereist
    ).hexdigest()
    headers = {
        "Bitvavo-Access-Key":       API_KEY,
        "Bitvavo-Access-Signature": sig,
        "Bitvavo-Access-Timestamp": ts,
        "Bitvavo-Access-Window":    ACCESS_WINDOW_MS,
        "Content-Type":             "application/json",
    }
    url  = f"{BASE_URL}{path}"
    resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT) if m == "GET" else requests.post(url, headers=headers, data=body, timeout=HTTP_TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"Bitvavo {resp.status_code}: {resp.text[:200]}")
    return resp.json()


@st.cache_data(ttl=60, show_spinner=False)
def fetch_bitvavo_prices() -> Dict[str, float]:
    try:
        resp = requests.get(f"{BASE_URL}/v2/ticker/price", timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return {r["market"]: float(r["price"]) for r in resp.json() if r.get("market") and r.get("price")}
    except Exception as e:
        capture_exc("fetch_bitvavo_prices", e)
        return {}


def price_eur(symbol: str, prices: Dict[str, float]) -> Tuple[Optional[float], str]:
    if symbol == "EUR":
        return 1.0, "EUR"
    for route, keys in [
        (f"{symbol}-EUR",  [f"{symbol}-EUR"]),
        (f"{symbol}xUSDT", [f"{symbol}-USDT","USDT-EUR"]),
    ]:
        if all(k in prices for k in keys):
            p = prices[keys[0]]
            for k in keys[1:]:
                p *= prices[k]
            return p, route
    return None, "NO_ROUTE"


def build_snapshot() -> dict:
    """Bouwt een portfolio snapshot via Bitvavo API."""
    balances = bitvavo_request("GET", "/v2/balance")
    prices   = fetch_bitvavo_prices()
    assets   = []
    eur_bal  = 0.0

    for row in balances:
        sym   = row.get("symbol")
        avail = safe_float(row.get("available"))
        order = safe_float(row.get("inOrder"))
        total = avail + order
        if sym == "EUR":
            eur_bal = avail
        if total > 0:
            p, route = price_eur(sym, prices)
            assets.append({
                "symbol":      sym,
                "available":   avail,
                "inOrder":     order,
                "total":       total,
                "price_eur":   p,
                "eur_value":   total * p if p else None,
                "price_route": route,
            })

    crypto_eur = sum(safe_float(a.get("eur_value")) for a in assets if a.get("symbol") != "EUR")
    snap = {
        "status": "OK", "ts": now_utc().isoformat(),
        "eur_available": eur_bal,
        "crypto_assets_eur": crypto_eur,
        "total_portfolio_eur": eur_bal + crypto_eur,
        "assets": sorted(assets, key=lambda x: (x.get("symbol") != "EUR", x.get("symbol",""))),
    }
    ensure_dir(SNAPSHOT_PATH)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2, ensure_ascii=False)
    return snap


def read_snapshot() -> Tuple[dict, str]:
    snap, err = safe_json(SNAPSHOT_PATH)
    if snap:
        return snap, "SNAPSHOT"
    if API_KEY and API_SECRET:
        try:
            return build_snapshot(), "LIVE_API"
        except Exception as e:
            capture_exc("build_snapshot", e)
    return {
        "status": "DEMO", "ts": now_utc().isoformat(),
        "eur_available": 100.0, "crypto_assets_eur": 25.0,
        "total_portfolio_eur": 125.0,
        "assets": [
            {"symbol":"BTC","available":0.0001,"inOrder":0.0,"total":0.0001,"price_eur":62000,"eur_value":6.2,"price_route":"BTC-EUR"},
            {"symbol":"ETH","available":0.003,"inOrder":0.0,"total":0.003,"price_eur":3200,"eur_value":9.6,"price_route":"ETH-EUR"},
            {"symbol":"EUR","available":100.0,"inOrder":0.0,"total":100.0,"price_eur":1.0,"eur_value":100.0,"price_route":"EUR"},
        ],
    }, "DEMO"


def prepare_assets_df(snapshot: dict) -> pd.DataFrame:
    assets = pd.DataFrame((snapshot or {}).get("assets", []))
    if assets.empty:
        return assets
    for col in ["available","inOrder","total","price_eur","eur_value"]:
        if col in assets.columns:
            assets[col] = pd.to_numeric(assets[col], errors="coerce").fillna(0.0)
    assets["symbol"] = assets["symbol"].astype(str)
    return assets.sort_values("eur_value", ascending=False).reset_index(drop=True)


# ============================================================
# GRAFIEKEN
# ============================================================
def empty_fig(msg: str = "Geen data", height: int = 300) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=height,
        margin=dict(l=16,r=16,t=30,b=16),
        annotations=[dict(text=msg, x=0.5, y=0.5, xref="paper", yref="paper",
                          showarrow=False, font=dict(color="#94a3b8",size=13))],
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
    )
    return fig


def style_fig(fig: go.Figure, height: int = 320, title: str = "") -> go.Figure:
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e2e8f0"),
        height=height,
        margin=dict(l=22,r=18,t=42,b=22),
        title=dict(text=title, font=dict(size=13,color="#ffffff")),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0.0, font=dict(size=11)),
        xaxis=dict(gridcolor="rgba(255,255,255,0.06)"),
        yaxis=dict(gridcolor="rgba(255,255,255,0.06)", zerolinecolor="rgba(255,255,255,0.10)"),
    )
    return fig


def chart_equity_curve(df: pd.DataFrame, title: str = "Equity Curve") -> go.Figure:
    if df.empty:
        return empty_fig("Geen trade data voor equity curve")
    work = df[df["outcome"].isin(["WIN","LOSS"])].copy()
    if work.empty:
        return empty_fig("Geen WIN/LOSS trades voor equity curve")
    if "_datetime_raw" in work.columns:
        work = work.sort_values("_datetime_raw")
    work["cum_r"]   = pd.to_numeric(work["pnl_r"],   errors="coerce").fillna(0.0).cumsum()
    work["cum_eur"] = pd.to_numeric(work["pnl_eur"], errors="coerce").fillna(0.0).cumsum()
    work = downsample(work, 800)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=work.get("_datetime_raw", work.index),
        y=work["cum_r"],
        mode="lines",
        name="Cumulatief R",
        line=dict(color="#34d399", width=2.5),
        hovertemplate="R: %{y:.2f}<extra></extra>",
    ))
    fig.add_hline(y=0, line_dash="dash", line_color="rgba(255,255,255,0.15)", line_width=1)
    return style_fig(fig, 320, title)


def chart_win_loss_bar(df: pd.DataFrame, title: str = "Win / Loss") -> go.Figure:
    if df.empty:
        return empty_fig("Geen trade data")
    work = df[df["outcome"].isin(["WIN","LOSS"])].copy()
    if work.empty:
        return empty_fig("Geen WIN/LOSS data")
    counts = work["outcome"].value_counts()
    fig = go.Figure(go.Bar(
        x=["WIN","LOSS"],
        y=[safe_int(counts.get("WIN",0)), safe_int(counts.get("LOSS",0))],
        marker=dict(color=["#34d399","#fb7185"]),
        text=[safe_int(counts.get("WIN",0)), safe_int(counts.get("LOSS",0))],
        textposition="outside",
    ))
    return style_fig(fig, 280, title)


def chart_setup_perf(df: pd.DataFrame, title: str = "Setup Performance") -> go.Figure:
    if df.empty:
        return empty_fig("Geen setup data")
    work = df[df["outcome"].isin(["WIN","LOSS"])].copy()
    if work.empty:
        return empty_fig("Geen setup data")
    grouped = (
        work.groupby("setup_type", dropna=False)
        .agg(n=("trade_id","count"), avg_r=("pnl_r","mean"))
        .reset_index()
        .sort_values("n", ascending=False)
        .head(10)
    )
    colors = ["#34d399" if v >= 0 else "#fb7185" for v in grouped["avg_r"]]
    fig = go.Figure(go.Bar(
        x=grouped["setup_type"],
        y=grouped["avg_r"],
        text=grouped["n"].astype(int),
        textposition="outside",
        marker=dict(color=colors),
        hovertemplate="Setup: %{x}<br>Gem. R: %{y:.2f}<br>N: %{text}<extra></extra>",
    ))
    return style_fig(fig, 300, title)


def chart_daily_r(df: pd.DataFrame, title: str = "Dagresultaten") -> go.Figure:
    if df.empty:
        return empty_fig("Geen data voor dagresultaten")
    work = df[df["outcome"].isin(["WIN","LOSS"])].copy()
    if work.empty:
        return empty_fig("Geen WIN/LOSS data")
    grouped = work.groupby("day", dropna=False)["pnl_r"].sum().reset_index()
    colors  = ["#34d399" if v >= 0 else "#fb7185" for v in grouped["pnl_r"]]
    fig = go.Figure(go.Bar(
        x=grouped["day"],
        y=grouped["pnl_r"],
        marker=dict(color=colors),
        hovertemplate="Dag: %{x}<br>R: %{y:.2f}<extra></extra>",
    ))
    return style_fig(fig, 280, title)


def chart_regime_dist(df: pd.DataFrame, title: str = "Regime Verdeling") -> go.Figure:
    if df.empty:
        return empty_fig("Geen data")
    counts = df["regime"].value_counts().reset_index()
    counts.columns = ["regime","n"]
    color_map = {"BULL":"#34d399","BEAR":"#fb7185","RANGE":"#fbbf24"}
    colors = [color_map.get(r,"#60a5fa") for r in counts["regime"]]
    fig = go.Figure(go.Bar(
        x=counts["regime"], y=counts["n"],
        marker=dict(color=colors),
        text=counts["n"], textposition="outside",
    ))
    return style_fig(fig, 260, title)


def chart_donut(win_pct: float, net_eur: float, title: str = "Win Rate") -> go.Figure:
    win = max(0.0, min(100.0, safe_float(win_pct)))
    loss = 100.0 - win
    fig = go.Figure(go.Pie(
        values=[max(win,0.001), max(loss,0.001)],
        labels=["Win","Loss"],
        hole=0.78,
        sort=False,
        direction="clockwise",
        rotation=270,
        textinfo="none",
        marker=dict(colors=["#34d399","#ef4444"], line=dict(width=0)),
        showlegend=False,
    ))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=200,
        margin=dict(l=0,r=0,t=0,b=0),
        showlegend=False,
        annotations=[
            dict(text=f"<b>{win:.1f}%</b>", x=0.5, y=0.58, showarrow=False, font=dict(color="#ffffff",size=22)),
            dict(text=f"<span style='color:#dbe4f0;font-size:13px'><b>{title}</b></span>", x=0.5, y=0.42, showarrow=False, font=dict(color="#dbe4f0",size=13)),
            dict(text=f"<span style='color:#94a3b8;font-size:11px'>{format_money(net_eur)}</span>", x=0.5, y=0.27, showarrow=False, font=dict(color="#94a3b8",size=11)),
        ],
    )
    return fig


def chart_portfolio_pie(assets_df: pd.DataFrame) -> go.Figure:
    if assets_df.empty:
        return empty_fig("Geen portfolio data")
    work = assets_df[assets_df["eur_value"] > 0].copy().head(9)
    if work.empty:
        return empty_fig("Geen portfolio waarden")
    fig = go.Figure(go.Pie(
        labels=work["symbol"],
        values=work["eur_value"],
        hole=0.55,
        textinfo="label+percent",
        marker=dict(line=dict(width=0)),
    ))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e2e8f0"),
        margin=dict(l=16,r=16,t=40,b=16),
        height=320,
        showlegend=True,
        legend=dict(orientation="h", y=-0.08, x=0.5, xanchor="center"),
    )
    return fig


def chart_scoreboard_bar(df: pd.DataFrame) -> go.Figure:
    if df.empty:
        return empty_fig("Geen scoreboard data")
    top = df.head(12).copy()
    top["label"] = top["setup_type"].astype(str) + " / " + top["market_regime"].astype(str)
    colors = ["#34d399" if v >= 60 else "#fbbf24" if v >= 45 else "#fb7185" for v in top["win_rate"]]
    fig = go.Figure(go.Bar(
        x=top["label"],
        y=top["win_rate"],
        marker=dict(color=colors),
        text=top["n"].astype(int),
        textposition="outside",
        hovertemplate="Setup/Regime: %{x}<br>Win rate: %{y:.1f}%<br>N: %{text}<extra></extra>",
    ))
    fig.add_hline(y=50, line_dash="dash", line_color="rgba(255,255,255,0.2)")
    return style_fig(fig, 300, "Scoreboard Win Rate")


def chart_pending_scores(df: pd.DataFrame) -> go.Figure:
    if df.empty:
        return empty_fig("Geen signals")
    fig = go.Figure()
    colors = ["#34d399" if s >= 85 else "#fbbf24" if s >= 75 else "#fb7185" for s in df["score"]]
    fig.add_trace(go.Bar(
        x=df["symbol"].astype(str) + " | " + df["setup_type"].astype(str),
        y=df["score"],
        marker=dict(color=colors),
        text=df["score"],
        textposition="outside",
        hovertemplate="Coin: %{x}<br>Score: %{y}<extra></extra>",
    ))
    fig.add_hline(y=85, line_dash="dot", line_color="rgba(52,211,153,0.5)",
                  annotation_text="Min score (85)", annotation_font_color="#34d399")
    return style_fig(fig, 300, "Pre-BUY Signal Scores")


def chart_trade_detail(row: pd.Series) -> go.Figure:
    """Simpele visuele weergave van een trade met entry/stop/target niveaus."""
    entry  = safe_float(row.get("entry"))
    stop   = safe_float(row.get("stop"))
    target = safe_float(row.get("target"))
    if entry <= 0:
        return empty_fig("Geen prijs data voor trade")

    x    = list(range(20))
    base = entry
    opens, highs, lows, closes = [], [], [], []
    for i in x:
        o = base + (i - 8) * 0.003 * base + ((i % 4) - 1.5) * 0.004 * base
        c = o + ((-1) ** i) * 0.0035 * base
        h = max(o, c) + 0.005 * base
        l = min(o, c) - 0.005 * base
        opens.append(o); closes.append(c); highs.append(h); lows.append(l)

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=x, open=opens, high=highs, low=lows, close=closes,
        increasing_line_color="#34d399", decreasing_line_color="#fb7185",
        showlegend=False, name="Prijs",
    ))
    fig.add_hline(y=entry, line_width=2, line_color="#60a5fa",
                  annotation_text=f"ENTRY {format_price(entry)}", annotation_position="right")
    if stop > 0:
        fig.add_hline(y=stop, line_width=2, line_color="#fb7185",
                      annotation_text=f"STOP {format_price(stop)}", annotation_position="right")
    if target > 0:
        fig.add_hline(y=target, line_width=2, line_color="#34d399",
                      annotation_text=f"TARGET {format_price(target)}", annotation_position="right")

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=320,
        margin=dict(l=8,r=8,t=36,b=8),
        title=dict(text=f"{safe_str(row.get('symbol'))} Trade Detail", font=dict(size=13,color="#fff")),
        xaxis=dict(showgrid=False, showticklabels=False),
        yaxis=dict(gridcolor="rgba(255,255,255,0.06)"),
    )
    return fig


# ============================================================
# CLAUDE AI — identiek aan alle andere bestanden
# ============================================================
def call_claude(prompt: str, max_tokens: int = 300) -> str:
    if not ANTHROPIC_API_KEY:
        return ""
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": max_tokens,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if resp.status_code == 200:
            content = resp.json().get("content", [])
            if content:
                return content[0]["text"].strip()
    except Exception as e:
        capture_exc("call_claude", e)
    return ""


def claude_btn(label: str, prompt: str, max_tokens: int = 300, key: str = "") -> None:
    """Claude AI analyse knop in het dashboard."""
    if not ANTHROPIC_API_KEY:
        st.caption("⚠️ ANTHROPIC_API_KEY niet ingesteld")
        return
    if st.button(f"🧠 {label}", key=key or f"claude_{label[:20]}", use_container_width=True):
        with st.spinner("Claude analyseert..."):
            result = call_claude(prompt, max_tokens)
            if result:
                st.success(result)
            else:
                st.error("Claude analyse mislukt")


# ============================================================
# ALARM BANNER — toont actieve alarmen boven elke pagina
# ============================================================
@st.cache_data(ttl=60, show_spinner=False)
def _load_actieve_alarmen() -> List[Dict]:
    """Laadt actieve (niet opgeloste) alarmen uit coach_anomalieen."""
    if not table_exists("coach_anomalieen"):
        return []
    df = run_query("""
        SELECT type, omschrijving, ernst
        FROM public.coach_anomalieen
        WHERE opgelost = FALSE
          AND tijdstip >= NOW() - INTERVAL '24 hours'
        ORDER BY
            CASE ernst WHEN 'KRITIEK' THEN 1 WHEN 'HOOG' THEN 2
                       WHEN 'MEDIUM' THEN 3 ELSE 4 END,
            tijdstip DESC
        LIMIT 10
    """)
    return df.to_dict("records") if not df.empty else []


def render_alarm_banner() -> None:
    """Rode/oranje banner boven de pagina als er actieve alarmen zijn."""
    alarmen = _load_actieve_alarmen()
    if not alarmen:
        return
    kritiek = [a for a in alarmen if a.get("ernst") == "KRITIEK"]
    hoog    = [a for a in alarmen if a.get("ernst") == "HOOG"]

    if kritiek:
        kleur = "#c0392b"
        label = f"🔴 {len(kritiek)} KRITIEK ALARM{'EN' if len(kritiek)>1 else ''}"
        tekst = " | ".join(a.get("omschrijving","") for a in kritiek[:3])
    elif hoog:
        kleur = "#e67e22"
        label = f"🟡 {len(hoog)} HOOG ALARM{'EN' if len(hoog)>1 else ''}"
        tekst = " | ".join(a.get("omschrijving","") for a in hoog[:3])
    else:
        kleur = "#2c3e50"
        label = f"⚪ {len(alarmen)} melding(en)"
        tekst = alarmen[0].get("omschrijving","")

    st.markdown(
        f"""<div style="background:{kleur};padding:8px 14px;border-radius:6px;
            margin-bottom:8px;display:flex;align-items:center;gap:12px;font-size:13px;">
            <b style="color:#fff;white-space:nowrap;">{label}</b>
            <span style="color:rgba(255,255,255,0.85);overflow:hidden;
                text-overflow:ellipsis;white-space:nowrap;">{tekst}</span>
            <span style="color:rgba(255,255,255,0.5);font-size:11px;
                white-space:nowrap;margin-left:auto;">→ Monitor pagina voor details</span>
        </div>""",
        unsafe_allow_html=True,
    )


# ============================================================
# DATA LOADERS — coach tabellen
# ============================================================
@st.cache_data(ttl=120, show_spinner=False)
def load_coach_dagboek(n: int = 20) -> pd.DataFrame:
    if not table_exists("coach_dagboek"):
        return pd.DataFrame()
    return run_query("""
        SELECT datum, tijdstip, samenvatting, beslissingen, anomalieen, run_duur_sec
        FROM public.coach_dagboek
        ORDER BY tijdstip DESC LIMIT %s
    """, (n,))


@st.cache_data(ttl=60, show_spinner=False)
def load_coach_events(uren: int = 48) -> pd.DataFrame:
    if not table_exists("coach_events"):
        return pd.DataFrame()
    return run_query("""
        SELECT tijdstip, categorie, event_type, omschrijving, ernst
        FROM public.coach_events
        WHERE tijdstip >= NOW() - INTERVAL '1 hour' * %s
        ORDER BY tijdstip DESC LIMIT 200
    """, (uren,))


@st.cache_data(ttl=60, show_spinner=False)
def load_coach_config_log(dagen: int = 30) -> pd.DataFrame:
    if not table_exists("coach_config_log"):
        return pd.DataFrame()
    return run_query("""
        SELECT tijdstip, parameter, oud_waarde, nieuw_waarde, bron, reden
        FROM public.coach_config_log
        WHERE tijdstip >= NOW() - INTERVAL '1 day' * %s
        ORDER BY tijdstip DESC LIMIT 200
    """, (dagen,))


@st.cache_data(ttl=120, show_spinner=False)
def load_coach_analyses(n: int = 10) -> pd.DataFrame:
    if not table_exists("coach_analyses"):
        return pd.DataFrame()
    return run_query("""
        SELECT run_datum, periode_dagen, n_trades, win_rate,
               profit_factor, aanpassingen, claude_advies
        FROM public.coach_analyses
        ORDER BY run_datum DESC LIMIT %s
    """, (n,))


@st.cache_data(ttl=60, show_spinner=False)
def load_coach_anomalieen(dagen: int = 7) -> pd.DataFrame:
    if not table_exists("coach_anomalieen"):
        return pd.DataFrame()
    return run_query("""
        SELECT tijdstip, type, omschrijving, waarde, drempel, ernst, opgelost
        FROM public.coach_anomalieen
        WHERE tijdstip >= NOW() - INTERVAL '1 day' * %s
        ORDER BY tijdstip DESC LIMIT 100
    """, (dagen,))


@st.cache_data(ttl=120, show_spinner=False)
def load_coach_regime_log(dagen: int = 30) -> pd.DataFrame:
    if not table_exists("coach_regime_log"):
        return pd.DataFrame()
    return run_query("""
        SELECT tijdstip, oud_regime, nieuw_regime, btc_prijs, reden
        FROM public.coach_regime_log
        WHERE tijdstip >= NOW() - INTERVAL '1 day' * %s
        ORDER BY tijdstip DESC
    """, (dagen,))


@st.cache_data(ttl=120, show_spinner=False)
def load_coach_bestand_checksums() -> pd.DataFrame:
    if not table_exists("coach_bestand_checksums"):
        return pd.DataFrame()
    return run_query("""
        SELECT bestandsnaam, regels, checksum, bijgewerkt, verandering_gedetecteerd
        FROM public.coach_bestand_checksums
        ORDER BY bijgewerkt DESC
    """)


@st.cache_data(ttl=60, show_spinner=False)
def load_trade_flow_7d() -> Dict:
    """Laadt trade flow statistieken: signaal → live → gesloten."""
    def sc(sql, default=0):
        v = run_scalar(sql)
        return int(v) if v is not None else default

    signalen    = sc("SELECT COUNT(*) FROM public.pending_approvals WHERE aangemaakt >= NOW() - INTERVAL '7 days'")
    goedgekeurd = sc("SELECT COUNT(*) FROM public.pending_approvals WHERE UPPER(COALESCE(status,'')) = 'APPROVED' AND aangemaakt >= NOW() - INTERVAL '7 days'")
    verlopen    = sc("""SELECT COUNT(*) FROM public.pending_approvals
                        WHERE COALESCE(expires_at, aangemaakt + INTERVAL '4 hours') < NOW()
                          AND UPPER(COALESCE(status,'PENDING')) = 'PENDING'
                          AND aangemaakt >= NOW() - INTERVAL '7 days'""")
    live_trades = sc("SELECT COUNT(*) FROM public.experience_trades WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE') AND COALESCE(created_at, entry_time) >= NOW() - INTERVAL '7 days'")
    gesloten    = sc("SELECT COUNT(*) FROM public.experience_trades WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE') AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS') AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '7 days'")

    return {
        "signalen": signalen,
        "goedgekeurd": goedgekeurd,
        "verlopen": verlopen,
        "live": live_trades,
        "gesloten": gesloten,
        "conversie_pct": round(live_trades / max(signalen, 1) * 100, 1),
    }


@st.cache_data(ttl=30, show_spinner=False)
def load_systeem_audit_score() -> int:
    """Laadt het laatste systeem audit score uit coach_memory."""
    if not table_exists("coach_memory"):
        return 100
    row = run_query("""
        SELECT waarde FROM public.coach_memory
        WHERE type='audit' AND sleutel='laatste'
        ORDER BY bijgewerkt DESC LIMIT 1
    """)
    if row.empty:
        return 100
    try:
        data = json.loads(row.iloc[0]["waarde"])
        return int(data.get("score", 100))
    except Exception:
        return 100


# ============================================================
# NIEUWE CHARTS — R-distributie, Score correlatie, Fee, Config
# ============================================================
def chart_r_distributie(df: pd.DataFrame) -> go.Figure:
    """Histogram van R-multiples — toont of de bot consistent 2R+ haalt."""
    r_col = next((c for c in ["pnl_r","result_r"] if c in df.columns), None)
    if not r_col or df.empty:
        return empty_fig("Geen R-data")

    vals = pd.to_numeric(df[r_col], errors="coerce").dropna()
    wins  = vals[vals >= 0]
    loss  = vals[vals < 0]

    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=loss, name="Verlies", nbinsx=20,
        marker_color="#e74c3c", opacity=0.8,
    ))
    fig.add_trace(go.Histogram(
        x=wins, name="Winst", nbinsx=20,
        marker_color="#2ecc71", opacity=0.8,
    ))
    fig.add_vline(x=0, line_dash="dash", line_color="#f39c12", line_width=1)
    fig.add_vline(x=float(vals.mean()), line_dash="dot", line_color="#3498db",
                  annotation_text=f"Gem {vals.mean():.2f}R", line_width=1)
    return style_fig(fig, height=300, title="R-Multiple Verdeling")


def chart_score_winrate_correlatie(df: pd.DataFrame) -> go.Figure:
    """Toont win rate per score bucket — correleert score met uitkomst?"""
    if df.empty or "score" not in df.columns or "outcome" not in df.columns:
        return empty_fig("Geen score data")

    df = df.copy()
    df["score_n"] = pd.to_numeric(df["score"], errors="coerce")
    df["win"]     = df["outcome"].str.upper().isin(["WIN", "1"])

    bins   = [0, 80, 85, 88, 90, 92, 95, 100]
    labels = ["<80","80-84","85-87","88-89","90-91","92-94","95+"]
    df["bucket"] = pd.cut(df["score_n"], bins=bins, labels=labels, right=False)

    grp = df.groupby("bucket", observed=True).agg(
        n=("win","count"), wins=("win","sum")
    ).reset_index()
    grp["wr"] = (grp["wins"] / grp["n"].replace(0,1) * 100).round(1)
    grp = grp[grp["n"] >= 3]

    if grp.empty:
        return empty_fig("Te weinig data per bucket")

    kleuren = ["#e74c3c" if w < 45 else "#f39c12" if w < 55 else "#2ecc71"
               for w in grp["wr"]]
    fig = go.Figure(go.Bar(
        x=grp["bucket"].astype(str),
        y=grp["wr"],
        text=[f"{w:.1f}%<br>n={n}" for w, n in zip(grp["wr"], grp["n"])],
        textposition="outside",
        marker_color=kleuren,
    ))
    fig.add_hline(y=50, line_dash="dash", line_color="#7f8c8d", line_width=1,
                  annotation_text="50%")
    return style_fig(fig, height=300, title="Score → Win Rate Correlatie")


def chart_fee_impact(df: pd.DataFrame) -> go.Figure:
    """Gestapelde bar: bruto winst vs fees vs netto — per maand."""
    if df.empty:
        return empty_fig("Geen data")

    df = df.copy()
    for col in ["pnl_eur","fee_eur","outcome"]:
        if col not in df.columns:
            df[col] = 0
    df["pnl_n"] = pd.to_numeric(df["pnl_eur"], errors="coerce").fillna(0)
    df["fee_n"] = pd.to_numeric(df.get("fee_eur", 0), errors="coerce").fillna(0)
    df["win"]   = df["outcome"].str.upper().isin(["WIN","1"])

    ts_col = next((c for c in ["exit_time","updated_at"] if c in df.columns), None)
    if not ts_col:
        return empty_fig("Geen tijdstip data")

    df["maand"] = pd.to_datetime(df[ts_col], errors="coerce").dt.to_period("M").astype(str)
    grp = df.groupby("maand").agg(
        bruto_winst=("pnl_n", lambda x: x[df.loc[x.index,"win"]].sum()),
        bruto_verlies=("pnl_n", lambda x: abs(x[~df.loc[x.index,"win"]].sum())),
        fees=("fee_n","sum"),
    ).reset_index().tail(6)

    grp["netto"] = grp["bruto_winst"] - grp["bruto_verlies"] - grp["fees"]

    fig = go.Figure()
    fig.add_trace(go.Bar(name="Bruto Winst",  x=grp["maand"], y=grp["bruto_winst"],  marker_color="#2ecc71"))
    fig.add_trace(go.Bar(name="Bruto Verlies",x=grp["maand"], y=-grp["bruto_verlies"],marker_color="#e74c3c"))
    fig.add_trace(go.Bar(name="Fees",         x=grp["maand"], y=-grp["fees"],         marker_color="#e67e22"))
    fig.add_trace(go.Scatter(name="Netto",    x=grp["maand"], y=grp["netto"],
                             mode="lines+markers", marker_color="#3498db", line_width=2))
    fig.update_layout(barmode="relative")
    return style_fig(fig, height=320, title="Fee Impact per Maand")


def chart_config_timeline(config_df: pd.DataFrame) -> go.Figure:
    """Timeline van parameter wijzigingen — wanneer werd wat aangepast?"""
    if config_df.empty:
        return empty_fig("Geen config wijzigingen")

    fig = go.Figure()
    kleuren_map: Dict[str, str] = {}
    palette = ["#3498db","#e74c3c","#2ecc71","#f39c12","#9b59b6","#1abc9c","#e67e22"]

    params = config_df["parameter"].unique() if "parameter" in config_df.columns else []
    for i, param in enumerate(params[:7]):
        kleuren_map[param] = palette[i % len(palette)]

    for _, row in config_df.iterrows():
        param  = str(row.get("parameter",""))
        ts     = row.get("tijdstip","")
        oud    = str(row.get("oud_waarde",""))[:15]
        nieuw  = str(row.get("nieuw_waarde",""))[:15]
        kleur  = kleuren_map.get(param, "#95a5a6")
        fig.add_trace(go.Scatter(
            x=[ts], y=[param],
            mode="markers+text",
            marker=dict(size=12, color=kleur, symbol="diamond"),
            text=[f"{oud}→{nieuw}"],
            textposition="top center",
            textfont=dict(size=9),
            name=param,
            showlegend=False,
            hovertemplate=f"<b>{param}</b><br>{oud} → {nieuw}<br>%{{x}}<extra></extra>",
        ))

    fig.update_layout(showlegend=False)
    return style_fig(fig, height=max(250, len(params) * 35 + 80), title="Config Wijzigingen Timeline")


def chart_trade_flow_funnel(flow: Dict) -> go.Figure:
    """Funnel chart: signalen → goedgekeurd → live → gesloten."""
    labels = ["Signalen", "Goedgekeurd", "Live trades", "Gesloten"]
    values = [
        flow.get("signalen", 0),
        flow.get("goedgekeurd", 0),
        flow.get("live", 0),
        flow.get("gesloten", 0),
    ]
    fig = go.Figure(go.Funnel(
        y=labels, x=values,
        textinfo="value+percent initial",
        marker_color=["#3498db","#f39c12","#2ecc71","#27ae60"],
    ))
    return style_fig(fig, height=280, title="Trade Flow Funnel (7 dagen)")


def chart_audit_gauge(score: int) -> go.Figure:
    """Gauge chart voor systeem audit score 0-100."""
    kleur = "#2ecc71" if score >= 90 else "#f39c12" if score >= 70 else "#e74c3c"
    fig = go.Figure(go.Indicator(
        mode="gauge+number+delta",
        value=score,
        domain={"x":[0,1],"y":[0,1]},
        title={"text":"Systeem Gezondheid","font":{"size":14,"color":"#aaa"}},
        delta={"reference":90,"increasing":{"color":"#2ecc71"},"decreasing":{"color":"#e74c3c"}},
        gauge={
            "axis":{"range":[0,100],"tickcolor":"#555","tickfont":{"color":"#aaa"}},
            "bar":{"color":kleur},
            "steps":[
                {"range":[0,60],"color":"#2c0a0a"},
                {"range":[60,80],"color":"#2c1a0a"},
                {"range":[80,100],"color":"#0a2c0a"},
            ],
            "threshold":{"line":{"color":"#fff","width":2},"thickness":0.75,"value":90},
        },
        number={"suffix":"/100","font":{"color":"#fff","size":36}},
    ))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        font={"color":"#ccc"},
        height=220,
        margin=dict(l=20,r=20,t=40,b=10),
    )
    return fig


# ============================================================
# PAGINA: COACH MONITOR
# Dashboard van alle coach activiteit — dagboek, events, config
# ============================================================
def render_coach_monitor_page() -> None:
    render_alarm_banner()
    st.markdown('<div class="section-title">📋 Coach Monitor</div>', unsafe_allow_html=True)
    st.caption("Volledig overzicht van alle coach activiteit, beslissingen en wijzigingen.")

    # ── TABS ─────────────────────────────────────────────────
    tab_dag, tab_events, tab_config, tab_anom, tab_regime, tab_files, tab_analyses = st.tabs([
        "📖 Dagboek", "⚡ Events", "⚙️ Config Log", "🔬 Anomalieën",
        "🔀 Regime Log", "📁 Bestanden", "🧠 Analyses",
    ])

    # ── DAGBOEK ──────────────────────────────────────────────
    with tab_dag:
        st.markdown("**Coach run history** — elke analyse run met samenvatting en beslissingen.")
        df = load_coach_dagboek(30)
        if df.empty:
            st.info("Nog geen dagboek entries — coach heeft nog niet gedraaid.")
        else:
            for _, row in df.iterrows():
                datum     = str(row.get("datum",""))
                sam       = str(row.get("samenvatting",""))
                besliss   = str(row.get("beslissingen",""))
                anom      = str(row.get("anomalieen",""))
                duur      = float(row.get("run_duur_sec",0) or 0)
                with st.expander(f"📅 {datum} | {sam} | {duur:.0f}s", expanded=False):
                    col1, col2 = st.columns(2)
                    with col1:
                        st.markdown("**Beslissingen:**")
                        st.text(besliss if besliss and besliss != "Geen aanpassingen" else "— geen —")
                    with col2:
                        st.markdown("**Anomalieën:**")
                        st.text(anom if anom and anom != "Geen" else "— geen —")

    # ── EVENTS ───────────────────────────────────────────────
    with tab_events:
        col_u, col_f = st.columns([3,1])
        with col_u:
            uren = st.selectbox("Periode", [6,12,24,48,72,168], index=2,
                                format_func=lambda x: f"{x}u" if x < 48 else f"{x//24}d",
                                key="events_uren")
        df = load_coach_events(uren)
        if df.empty:
            st.info("Geen events in deze periode.")
        else:
            ernst_kleuren = {
                "KRITIEK": "🔴", "HOOG": "🟡", "MEDIUM": "🟠", "INFO": "⚪",
            }
            # Filter op ernst
            with col_f:
                ernst_filter = st.selectbox("Ernst", ["ALLES","KRITIEK","HOOG","INFO"], key="events_ernst")
            if ernst_filter != "ALLES":
                df = df[df["ernst"] == ernst_filter]

            st.markdown(f"**{len(df)} events** in de afgelopen {uren}u")
            for _, row in df.head(100).iterrows():
                ts     = str(row.get("tijdstip",""))[:16]
                cat    = str(row.get("categorie",""))
                etype  = str(row.get("event_type",""))
                omschr = str(row.get("omschrijving",""))
                ernst  = str(row.get("ernst","INFO"))
                emoji  = ernst_kleuren.get(ernst,"⚪")
                st.markdown(
                    f'<div style="font-size:12px;padding:4px 8px;border-left:3px solid #333;margin:2px 0;">'
                    f'{emoji} <b style="color:#aaa;">{ts}</b> '
                    f'<span style="color:#f39c12;">[{cat}]</span> '
                    f'<span style="color:#3498db;">{etype}</span> '
                    f'<span style="color:#ccc;">{omschr[:120]}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # ── CONFIG LOG ───────────────────────────────────────────
    with tab_config:
        st.markdown("**Alle parameter wijzigingen** — wanneer, wat, door wie.")
        config_df = load_coach_config_log(30)
        if config_df.empty:
            st.info("Nog geen config wijzigingen gelogd.")
        else:
            st.markdown(f"**{len(config_df)} wijzigingen** in de afgelopen 30 dagen")
            # Timeline chart
            if len(config_df) > 1:
                st.plotly_chart(chart_config_timeline(config_df),
                                use_container_width=True,
                                config={"displayModeBar": False},
                                key="config_timeline")

            # Tabel
            weergave = config_df.copy()
            if "tijdstip" in weergave.columns:
                weergave["tijdstip"] = pd.to_datetime(
                    weergave["tijdstip"], errors="coerce"
                ).dt.strftime("%d-%m %H:%M")
            st.dataframe(
                weergave[["tijdstip","parameter","oud_waarde","nieuw_waarde","bron","reden"]],
                hide_index=True,
                use_container_width=True,
            )

            csv = config_df.to_csv(index=False)
            st.download_button("⬇️ Download config log", csv, "config_log.csv", "text/csv")

    # ── ANOMALIEËN ───────────────────────────────────────────
    with tab_anom:
        anom_df = load_coach_anomalieen(14)
        if anom_df.empty:
            st.success("✅ Geen anomalieën gedetecteerd in de afgelopen 14 dagen.")
        else:
            open_anom = anom_df[anom_df.get("opgelost",False) == False] if "opgelost" in anom_df.columns else anom_df
            opgelost  = anom_df[anom_df.get("opgelost",False) == True]  if "opgelost" in anom_df.columns else pd.DataFrame()

            a1, a2, a3 = st.columns(3)
            a1.metric("Totaal (14d)", len(anom_df))
            a2.metric("Open", len(open_anom), delta=f"-{len(opgelost)} opgelost" if not opgelost.empty else None)
            a3.metric("Kritiek", len(anom_df[anom_df.get("ernst","") == "KRITIEK"]) if "ernst" in anom_df.columns else 0)

            ernst_map = {"KRITIEK":"🔴","HOOG":"🟡","MEDIUM":"🟠","LAAG":"⚪"}
            for _, row in anom_df.iterrows():
                ernst  = str(row.get("ernst","MEDIUM"))
                emoji  = ernst_map.get(ernst,"⚪")
                type_  = str(row.get("type",""))
                omschr = str(row.get("omschrijving",""))
                waarde = float(row.get("waarde",0) or 0)
                drempel= float(row.get("drempel",0) or 0)
                opg    = bool(row.get("opgelost",False))
                ts     = str(row.get("tijdstip",""))[:16]
                status = "✅ Opgelost" if opg else "🔴 Open"

                st.markdown(
                    f'<div style="background:#1a1a2e;border-left:4px solid '
                    f'{"#e74c3c" if ernst=="KRITIEK" else "#e67e22" if ernst=="HOOG" else "#555"}'
                    f';padding:8px 12px;border-radius:4px;margin:4px 0;">'
                    f'<b>{emoji} {type_}</b> <span style="color:#aaa;font-size:11px;">{ts} | {status}</span><br>'
                    f'<span style="font-size:12px;color:#ccc;">{omschr}</span>'
                    f'<span style="font-size:11px;color:#888;margin-left:12px;">waarde: {waarde:.2f} | drempel: {drempel:.2f}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # ── REGIME LOG ───────────────────────────────────────────
    with tab_regime:
        regime_df = load_coach_regime_log(60)
        if regime_df.empty:
            st.info("Nog geen regime overgangen gelogd.")
        else:
            st.markdown(f"**{len(regime_df)} regime overgangen** in 60 dagen")
            for _, row in regime_df.iterrows():
                ts   = str(row.get("tijdstip",""))[:16]
                oud  = str(row.get("oud_regime","?"))
                nieuw= str(row.get("nieuw_regime","?"))
                btc  = float(row.get("btc_prijs",0) or 0)
                kleur_oud  = "#e74c3c" if "BEAR" in oud  else "#2ecc71" if "BULL" in oud  else "#f39c12"
                kleur_nieuw= "#e74c3c" if "BEAR" in nieuw else "#2ecc71" if "BULL" in nieuw else "#f39c12"
                st.markdown(
                    f'<div style="display:flex;align-items:center;gap:10px;padding:6px;'
                    f'border-bottom:1px solid #222;font-size:13px;">'
                    f'<span style="color:#888;width:110px;">{ts}</span>'
                    f'<span style="color:{kleur_oud};font-weight:bold;">{oud}</span>'
                    f'<span style="color:#555;">→</span>'
                    f'<span style="color:{kleur_nieuw};font-weight:bold;">{nieuw}</span>'
                    f'<span style="color:#888;margin-left:auto;">BTC: €{btc:,.0f}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # ── BESTANDEN ────────────────────────────────────────────
    with tab_files:
        files_df = load_coach_bestand_checksums()
        if files_df.empty:
            st.info("Nog geen bestand checksums — coach heeft nog niet gedraaid.")
        else:
            st.markdown("**Code bestand versies** — coach detecteert automatisch deploys.")
            for _, row in files_df.iterrows():
                naam     = str(row.get("bestandsnaam",""))
                regels   = int(row.get("regels",0) or 0)
                checksum = str(row.get("checksum",""))[:8]
                bijgew   = str(row.get("bijgewerkt",""))[:16]
                verand   = bool(row.get("verandering_gedetecteerd",False))
                badge    = '🟡 Gewijzigd' if verand else '✅ Ongewijzigd'
                st.markdown(
                    f'<div style="display:flex;align-items:center;gap:12px;padding:6px 0;'
                    f'border-bottom:1px solid #222;font-size:13px;">'
                    f'<code style="color:#3498db;width:220px;">{naam}</code>'
                    f'<span style="color:#888;">{regels:,} regels</span>'
                    f'<span style="color:#555;font-family:monospace;">{checksum}</span>'
                    f'<span style="color:#888;font-size:11px;">{bijgew}</span>'
                    f'<span style="margin-left:auto;">{badge}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # ── ANALYSES ────────────────────────────────────────────
    with tab_analyses:
        an_df = load_coach_analyses(10)
        if an_df.empty:
            st.info("Nog geen coach analyses opgeslagen.")
        else:
            for _, row in an_df.iterrows():
                datum = str(row.get("run_datum",""))[:10]
                dagen = int(row.get("periode_dagen",0) or 0)
                n     = int(row.get("n_trades",0) or 0)
                wr    = float(row.get("win_rate",0) or 0)
                pf    = float(row.get("profit_factor",0) or 0)
                aanp  = str(row.get("aanpassingen",""))
                advies= str(row.get("claude_advies",""))
                with st.expander(
                    f"📅 {datum} | {n} trades | {wr:.1f}% WR | PF {pf:.2f} | {dagen}d",
                    expanded=False
                ):
                    if aanp and aanp.strip():
                        st.markdown("**Aanpassingen:**")
                        st.text(aanp[:600])
                    if advies and advies.strip():
                        st.markdown("**Claude advies:**")
                        st.info(advies[:800])


# ============================================================
# PAGINA: SYSTEEM GEZONDHEID
# ============================================================
def render_health_page() -> None:
    render_alarm_banner()
    st.markdown('<div class="section-title">🏥 Systeem Gezondheid</div>', unsafe_allow_html=True)
    st.caption("Realtime overzicht van de bot gezondheid, data versheid en trade flow.")

    # ── AUDIT SCORE GAUGE ────────────────────────────────────
    score = load_systeem_audit_score()
    col_gauge, col_flow, col_info = st.columns([1, 1.4, 1.6])

    with col_gauge:
        st.plotly_chart(chart_audit_gauge(score),
                        use_container_width=True,
                        config={"displayModeBar": False},
                        key="audit_gauge")

    with col_flow:
        flow = load_trade_flow_7d()
        st.plotly_chart(chart_trade_flow_funnel(flow),
                        use_container_width=True,
                        config={"displayModeBar": False},
                        key="flow_funnel")

    with col_info:
        st.markdown("**Trade Flow (7d)**")
        items = [
            ("📨 Signalen gegenereerd", flow["signalen"]),
            ("✅ Goedgekeurd",           flow["goedgekeurd"]),
            ("⏰ Verlopen",              flow["verlopen"]),
            ("💶 Live trades",           flow["live"]),
            ("🏁 Gesloten",             flow["gesloten"]),
        ]
        for label, val in items:
            st.markdown(
                f'<div style="display:flex;justify-content:space-between;'
                f'padding:5px 0;border-bottom:1px solid #1e1e1e;font-size:13px;">'
                f'<span style="color:#aaa;">{label}</span>'
                f'<b style="color:#f39c12;">{val}</b>'
                f'</div>',
                unsafe_allow_html=True,
            )
        conv = flow.get("conversie_pct",0)
        kleur = "#2ecc71" if conv >= 5 else "#e74c3c" if conv < 2 else "#f39c12"
        st.markdown(
            f'<div style="margin-top:8px;font-size:12px;color:{kleur};">'
            f'Conversie: {conv:.1f}% signalen → live</div>',
            unsafe_allow_html=True,
        )

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    # ── DATA VERSHEID ────────────────────────────────────────
    st.markdown("**📡 Data Versheid**")
    versheid_checks = [
        {
            "naam": "Candles (1H)",
            "sql": "SELECT MAX(close_time) FROM public.candles WHERE interval_='1h'",
            "max_uren": 2,
        },
        {
            "naam": "Markt Regime",
            "sql": "SELECT MAX(updated_at) FROM public.market_regime",
            "max_uren": 6,
        },
        {
            "naam": "BTC Regime 4H",
            "sql": "SELECT MAX(created_at) FROM public.btc_regime_4h",
            "max_uren": 5,
        },
        {
            "naam": "Pending Signals",
            "sql": "SELECT MAX(aangemaakt) FROM public.pending_approvals",
            "max_uren": 4,
        },
        {
            "naam": "Live Trades",
            "sql": "SELECT MAX(COALESCE(updated_at, created_at)) FROM public.experience_trades WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')",
            "max_uren": 48,
        },
    ]

    cols = st.columns(len(versheid_checks))
    now  = datetime.now(timezone.utc)
    for i, check in enumerate(versheid_checks):
        with cols[i]:
            try:
                ts = run_scalar(check["sql"])
                if ts is None:
                    st.markdown(
                        f'<div class="metric-card"><div style="font-size:11px;color:#888;">{check["naam"]}</div>'
                        f'<div style="font-size:22px;">❓</div><div style="font-size:10px;color:#555;">Geen data</div></div>',
                        unsafe_allow_html=True)
                    continue
                if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                elif isinstance(ts, str):
                    ts = datetime.fromisoformat(ts.replace("Z","+00:00"))
                uren = (now - ts).total_seconds() / 3600
                ok   = uren < check["max_uren"]
                kleur= "#2ecc71" if ok else "#e74c3c"
                label= f"{uren:.1f}u" if uren < 48 else f"{uren/24:.1f}d"
                st.markdown(
                    f'<div class="metric-card"><div style="font-size:11px;color:#888;">{check["naam"]}</div>'
                    f'<div style="font-size:22px;color:{kleur};">{"✅" if ok else "⚠️"}</div>'
                    f'<div style="font-size:12px;color:{kleur};">{label} oud</div>'
                    f'<div style="font-size:10px;color:#555;">max {check["max_uren"]}u</div></div>',
                    unsafe_allow_html=True)
            except Exception:
                st.markdown(
                    f'<div class="metric-card"><div style="font-size:11px;color:#888;">{check["naam"]}</div>'
                    f'<div style="font-size:22px;">❌</div></div>',
                    unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    # ── DB TABEL STATISTIEKEN ────────────────────────────────
    st.markdown("**🗄️ Database Tabellen**")
    tabellen = [
        "experience_trades","pending_approvals","bot_state","market_regime",
        "btc_regime_4h","candles","coach_memory","coach_events","coach_dagboek",
        "coach_config_log","coach_anomalieen","coach_regime_log",
    ]
    tabel_data = []
    for tabel in tabellen:
        if table_exists(tabel):
            n = run_scalar(f"SELECT COUNT(*) FROM public.{tabel}") or 0
            tabel_data.append({"Tabel": tabel, "Records": int(n), "Status": "✅"})
        else:
            tabel_data.append({"Tabel": tabel, "Records": 0, "Status": "❌ Ontbreekt"})

    tabel_df = pd.DataFrame(tabel_data)
    st.dataframe(tabel_df, hide_index=True, use_container_width=True)

    # ── ALARMEN OVERZICHT ────────────────────────────────────
    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown("**🚨 Actieve Alarmen**")
    alarmen = _load_actieve_alarmen()
    if not alarmen:
        st.success("✅ Geen actieve alarmen.")
    else:
        for a in alarmen:
            ernst = str(a.get("ernst","MEDIUM"))
            kleur = "#e74c3c" if ernst=="KRITIEK" else "#e67e22" if ernst=="HOOG" else "#555"
            emoji = "🔴" if ernst=="KRITIEK" else "🟡"
            st.markdown(
                f'<div style="background:#111;border-left:4px solid {kleur};'
                f'padding:8px 12px;border-radius:4px;margin:4px 0;">'
                f'{emoji} <b>{a.get("type","")}</b> — {a.get("omschrijving","")}'
                f'</div>',
                unsafe_allow_html=True,
            )


# ============================================================
# PAGINA: BOT QUICK CONTROLS
# START/STOP/PAUSE direct vanuit dashboard + parameter sliders
# ============================================================
def _set_bot_state_key(key: str, value: str) -> bool:
    """Schrijft een waarde naar bot_state in de DB."""
    try:
        conn = get_db_conn()
        if not conn:
            return False
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO public.bot_state(key, value, updated_at)
                VALUES(%s, %s, NOW())
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
            """, (key, value))
        conn.commit()
        conn.close()
        st.cache_data.clear()
        return True
    except Exception as e:
        st.error(f"DB fout: {e}")
        return False


def _send_whatsapp_command(commando: str) -> bool:
    """Stuurt een WhatsApp commando via Twilio API."""
    sid   = os.getenv("TWILIO_ACCOUNT_SID","")
    token = os.getenv("TWILIO_AUTH_TOKEN","")
    van   = os.getenv("TWILIO_WHATSAPP_FROM","")
    naar  = os.getenv("TWILIO_WHATSAPP_TO","")
    if not all([sid, token, van, naar]):
        return False
    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            auth=(sid, token),
            data={"From": van, "To": naar, "Body": commando},
            timeout=8,
        )
        return resp.status_code == 201
    except Exception:
        return False


def render_controls_page() -> None:
    render_alarm_banner()
    st.markdown('<div class="section-title">🎮 Bot Quick Controls</div>', unsafe_allow_html=True)
    st.caption("Bestuur de bot direct vanuit het dashboard — geen WhatsApp nodig.")

    # ── BOT STATUS + GROTE KNOPPEN ───────────────────────────
    bot_actief_raw = get_bot_state_val("bot_active", "false").lower()
    bot_actief = bot_actief_raw == "true"
    bot_gepauz = get_bot_state_val("bot_paused", "false").lower() == "true"

    if bot_actief and not bot_gepauz:
        status_label = "🟢 BOT ACTIEF"
        status_kleur = "#2ecc71"
    elif bot_gepauz:
        status_label = "🟡 BOT GEPAUZEERD"
        status_kleur = "#f39c12"
    else:
        status_label = "🔴 BOT GESTOPT"
        status_kleur = "#e74c3c"

    st.markdown(
        f'<div style="text-align:center;background:#111;border:2px solid {status_kleur};'
        f'border-radius:12px;padding:20px;margin-bottom:16px;">'
        f'<div style="font-size:28px;font-weight:bold;color:{status_kleur};">{status_label}</div>'
        f'<div style="font-size:12px;color:#888;margin-top:4px;">Laatste check: {datetime.now(timezone.utc).strftime("%H:%M:%S UTC")}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    btn_c1, btn_c2, btn_c3, btn_c4 = st.columns(4)

    with btn_c1:
        if st.button("▶️ START", use_container_width=True, type="primary",
                     disabled=bot_actief and not bot_gepauz):
            if _set_bot_state_key("bot_active", "true") and _set_bot_state_key("bot_paused","false"):
                _send_whatsapp_command("START")
                st.success("✅ Bot gestart")
                st.rerun()

    with btn_c2:
        if st.button("⏸️ PAUZEER", use_container_width=True, disabled=not bot_actief or bot_gepauz):
            if _set_bot_state_key("bot_paused", "true"):
                st.warning("⏸️ Bot gepauzeerd")
                st.rerun()

    with btn_c3:
        if st.button("⏩ HERVAT", use_container_width=True, disabled=not bot_gepauz):
            if _set_bot_state_key("bot_paused", "false"):
                st.success("▶️ Bot hervat")
                st.rerun()

    with btn_c4:
        if st.button("⏹️ STOP", use_container_width=True,
                     disabled=not bot_actief, type="secondary"):
            if _set_bot_state_key("bot_active", "false"):
                _send_whatsapp_command("STOP")
                st.error("⏹️ Bot gestopt")
                st.rerun()

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    # ── PARAMETER SLIDERS ────────────────────────────────────
    st.markdown("**⚙️ Parameters direct aanpassen**")
    st.caption("Wijzigingen worden direct in bot_state opgeslagen én gelogd in coach_config_log.")

    p1, p2 = st.columns(2)
    with p1:
        huidig_score = int(get_bot_state_val("min_score_to_trade","92") or "92")
        nieuw_score  = st.slider("🎯 Min Score to Trade", 75, 99, huidig_score, 1,
                                 key="ctrl_score")
        if nieuw_score != huidig_score:
            if st.button(f"Opslaan score={nieuw_score}", key="save_score"):
                if _set_bot_state_key("min_score_to_trade", str(nieuw_score)):
                    st.success(f"✅ Score drempel → {nieuw_score}")
                    st.cache_data.clear()

        huidig_start = int(get_bot_state_val("trading_hours_start","9") or "9")
        huidig_end   = int(get_bot_state_val("trading_hours_end","17") or "17")
        nieuw_start, nieuw_end = st.select_slider(
            "⏰ Trading Uren (UTC)",
            options=list(range(0,24)),
            value=(huidig_start, huidig_end),
            key="ctrl_uren",
        )
        if (nieuw_start, nieuw_end) != (huidig_start, huidig_end):
            if st.button(f"Opslaan uren {nieuw_start}-{nieuw_end}u", key="save_uren"):
                ok = (_set_bot_state_key("trading_hours_start", str(nieuw_start)) and
                      _set_bot_state_key("trading_hours_end",   str(nieuw_end)))
                if ok:
                    st.success(f"✅ Trading uren → {nieuw_start}:00-{nieuw_end}:00 UTC")

    with p2:
        huidig_atr = float(get_bot_state_val("atr_multiplier","1.6") or "1.6")
        nieuw_atr  = st.slider("📏 ATR Multiplier (stop loss)", 0.8, 3.0, huidig_atr, 0.1,
                                key="ctrl_atr")
        if abs(nieuw_atr - huidig_atr) > 0.05:
            if st.button(f"Opslaan ATR={nieuw_atr:.1f}", key="save_atr"):
                if _set_bot_state_key("atr_multiplier", str(round(nieuw_atr,1))):
                    st.success(f"✅ ATR multiplier → {nieuw_atr:.1f}")

        huidig_size = float(get_bot_state_val("position_size_eur","0.5") or "0.5")
        nieuw_size  = st.slider("💶 Positiegrootte (EUR)", 0.10, 2.00, huidig_size, 0.05,
                                 format="€%.2f", key="ctrl_size")
        if abs(nieuw_size - huidig_size) > 0.01:
            if st.button(f"Opslaan positie=€{nieuw_size:.2f}", key="save_size"):
                if _set_bot_state_key("position_size_eur", str(round(nieuw_size,2))):
                    st.success(f"✅ Positiegrootte → €{nieuw_size:.2f}")

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    # ── WHATSAPP COMMANDO'S ──────────────────────────────────
    st.markdown("**📱 WhatsApp Commando's**")
    wa_cols = st.columns(5)
    commando_labels = {
        "STATUS":  ("📊 Status",   "wa_status"),
        "TRADES":  ("📋 Trades",   "wa_trades"),
        "RAPPORT": ("📈 Rapport",  "wa_rapport"),
        "HEALTH":  ("🏥 Health",   "wa_health"),
        "ANALYSE": ("🧠 Analyse",  "wa_analyse"),
    }
    for i, (cmd, (label, key)) in enumerate(commando_labels.items()):
        with wa_cols[i]:
            if st.button(label, key=key, use_container_width=True):
                if _send_whatsapp_command(cmd):
                    st.success(f"✅ {cmd} verstuurd")
                else:
                    st.warning("⚠️ Twilio niet geconfigureerd")

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    # ── DAGBUDGET RESET / EMERGENCY ─────────────────────────
    st.markdown("**🚨 Emergency Controls**")
    ec1, ec2, ec3 = st.columns(3)

    with ec1:
        if st.button("🔄 Reset Dagbudget", use_container_width=True):
            if _set_bot_state_key("daily_pnl","0"):
                st.success("✅ Dagbudget gereset naar €0")

    with ec2:
        if st.button("🧹 Leeg Cooldown", use_container_width=True):
            if _set_bot_state_key("coin_cooldown","{}"):
                st.success("✅ Cooldown lijst geleegd")

    with ec3:
        with st.expander("⚠️ Reset Drawdown Pause", expanded=False):
            st.warning("Dit heft de drawdown bescherming tijdelijk op.")
            if st.button("Reset Drawdown", key="reset_dd"):
                if _set_bot_state_key("drawdown_paused","false"):
                    st.success("✅ Drawdown pause opgeheven")

    # ── RECENTE WIJZIGINGEN ──────────────────────────────────
    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown("**📋 Recente parameter wijzigingen**")
    recent_cfg = load_coach_config_log(2)  # 2 dagen
    if recent_cfg.empty:
        st.caption("Nog geen wijzigingen via dashboard gelogd.")
    else:
        for _, row in recent_cfg.head(10).iterrows():
            ts   = str(row.get("tijdstip",""))[:16]
            param= str(row.get("parameter",""))
            oud  = str(row.get("oud_waarde",""))
            nieuw= str(row.get("nieuw_waarde",""))
            bron = str(row.get("bron",""))
            st.markdown(
                f'<div style="font-size:12px;padding:3px 0;border-bottom:1px solid #1e1e1e;">'
                f'<span style="color:#888;">{ts}</span> '
                f'<b style="color:#f39c12;">{param}</b>: '
                f'<span style="color:#e74c3c;">{oud}</span> → '
                f'<span style="color:#2ecc71;">{nieuw}</span> '
                f'<span style="color:#555;">[{bron}]</span>'
                f'</div>',
                unsafe_allow_html=True,
            )


# ============================================================
# UITBREIDING ANALYSE PAGINA — R, Score correlatie, Fees
# ============================================================
def _render_extra_analyse_tabs(df: pd.DataFrame, real_df: pd.DataFrame) -> None:
    """Extra analyse tabs: R-distributie, Score correlatie, Fees, Config timeline."""
    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown("**🔬 Geavanceerde Analyse**")

    tab_r, tab_sc, tab_fee, tab_cfg = st.tabs([
        "📐 R-Distributie", "📊 Score Correlatie", "💰 Fee Impact", "⚙️ Config Timeline",
    ])

    with tab_r:
        st.plotly_chart(chart_r_distributie(real_df if not real_df.empty else df),
                        use_container_width=True, config={"displayModeBar":False}, key="xr_dist")
        r_col = next((c for c in ["pnl_r","result_r"] if c in df.columns), None)
        if r_col and not df.empty:
            vals = pd.to_numeric(df[r_col], errors="coerce").dropna()
            wins = vals[vals >= 0]
            loss = vals[vals < 0]
            c1,c2,c3,c4 = st.columns(4)
            c1.metric("Gem Win R",  f"{wins.mean():.2f}R"  if not wins.empty  else "—")
            c2.metric("Gem Loss R", f"{loss.mean():.2f}R"  if not loss.empty  else "—")
            c3.metric("≥ 2R trades", f"{(vals>=2).sum()}")
            c4.metric("Expectancy",  f"{(wins.mean()*(len(wins)/len(vals)) - abs(loss.mean())*(len(loss)/len(vals))):.3f}R" if len(vals)>0 else "—")

    with tab_sc:
        st.plotly_chart(chart_score_winrate_correlatie(df),
                        use_container_width=True, config={"displayModeBar":False}, key="xsc")
        st.caption("Een gezond scoremodel toont stijgende win rate bij hogere score buckets.")

    with tab_fee:
        st.plotly_chart(chart_fee_impact(df),
                        use_container_width=True, config={"displayModeBar":False}, key="xfee")
        if "fee_eur" in df.columns:
            fee_sum  = pd.to_numeric(df.get("fee_eur",0), errors="coerce").fillna(0).sum()
            pnl_sum  = pd.to_numeric(df.get("pnl_eur",0), errors="coerce").fillna(0)
            win_pnl  = pnl_sum[pnl_sum > 0].sum()
            fee_pct  = fee_sum / max(win_pnl, 0.001) * 100
            f1,f2,f3 = st.columns(3)
            f1.metric("Totale fees", f"€{fee_sum:.4f}")
            f2.metric("Fees / bruto winst", f"{fee_pct:.1f}%",
                      delta="Te hoog" if fee_pct > 25 else "OK",
                      delta_color="inverse" if fee_pct > 25 else "normal")
            f3.metric("Gem fee / trade", f"€{fee_sum/max(len(df),1):.4f}")
        else:
            st.info("Fee data (fee_eur kolom) niet beschikbaar.")

    with tab_cfg:
        cfg_df = load_coach_config_log(90)
        if cfg_df.empty:
            st.info("Nog geen config wijzigingen gelogd door de coach.")
        else:
            st.plotly_chart(chart_config_timeline(cfg_df),
                            use_container_width=True, config={"displayModeBar":False}, key="xcfg")
            st.caption(f"{len(cfg_df)} wijzigingen in 90 dagen — elk punt = een parameter aanpassing.")


# ============================================================
# NAVIGATIE HELPERS
# ============================================================
PAGE_NAMES = {
    "dashboard":   "◉ Dashboard",
    "coach":       "◉ 🤖 AI Coach Chat",
    "positions":   "◉ Open Posities (P&L)",
    "monitor":     "◉ 📋 Coach Monitor",
    "health":      "◉ 🏥 Systeem Gezondheid",
    "controls":    "◉ 🎮 Bot Controls",
    "live":        "◉ Live Performance",
    "sim":         "◉ Simulator",
    "shadow":      "◉ Shadow Review",
    "analyse":     "◉ Analyse & Drawdown",
    "coins":       "◉ Coin Analyse",
    "kalender":    "◉ P&L Kalender",
    "correlatie":  "◉ BTC Correlatie",
    "portfolio":   "◉ Portfolio",
    "signals":     "◉ Pre-BUY Signals",
    "scoreboard":  "◉ Scoreboard",
    "regime":      "◉ BTC Regime",
    "settings":    "◉ Instellingen",
    "help":        "◉ Help & Debug",
}


def nav_btn(label: str, page_key: str) -> None:
    is_active = st.session_state.page == page_key
    wrapper   = "nav-button-active" if is_active else ""
    st.markdown(f'<div class="{wrapper}">', unsafe_allow_html=True)
    if st.button(label, key=f"nav_{page_key}", use_container_width=True):
        st.session_state.page = page_key
        st.session_state.selected_page_trade_id = None
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


def render_sidebar() -> None:
    """Linker navigatie sidebar."""
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    current_name = PAGE_NAMES.get(st.session_state.page, st.session_state.page)
    st.markdown(f'<div class="page-chip">📍 {current_name}</div>', unsafe_allow_html=True)
    st.markdown('<div class="nav-header">Navigatie</div>', unsafe_allow_html=True)
    st.markdown('<div class="nav-caption">Actieve pagina heeft oranje highlight.</div>', unsafe_allow_html=True)

    # Snelknoppen
    hk1, hk2 = st.columns(2, gap="small")
    with hk1:
        st.markdown('<div class="tiny-button">', unsafe_allow_html=True)
        if st.button("🔄 Refresh", key="sidebar_refresh", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
    with hk2:
        st.markdown('<div class="tiny-button">', unsafe_allow_html=True)
        if st.button("🐛 Debug", key="sidebar_debug", use_container_width=True):
            st.session_state.show_debug = not st.session_state.show_debug
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="nav-header">📊 Overzicht</div>', unsafe_allow_html=True)
    nav_btn("◉ Dashboard",           "dashboard")
    nav_btn("◉ 🤖 AI Coach Chat",    "coach")
    nav_btn("◉ Open Posities (P&L)", "positions")

    st.markdown('<div class="nav-header" style="margin-top:8px;">🤖 Bot Beheer</div>', unsafe_allow_html=True)
    nav_btn("◉ 🎮 Bot Controls",     "controls")
    nav_btn("◉ 🏥 Systeem Gezondheid","health")
    nav_btn("◉ 📋 Coach Monitor",    "monitor")

    st.markdown('<div class="nav-header" style="margin-top:8px;">📈 Trade Analyse</div>', unsafe_allow_html=True)
    nav_btn("◉ Live Performance",    "live")
    nav_btn("◉ Simulator",           "sim")
    nav_btn("◉ Shadow Review",       "shadow")
    nav_btn("◉ Analyse & Drawdown",  "analyse")
    nav_btn("◉ Coin Analyse",        "coins")

    st.markdown('<div class="nav-header" style="margin-top:8px;">🗓️ Kalender & Correlatie</div>', unsafe_allow_html=True)
    nav_btn("◉ P&L Kalender",        "kalender")
    nav_btn("◉ BTC Correlatie",      "correlatie")

    st.markdown('<div class="nav-header" style="margin-top:8px;">💼 Systeem</div>', unsafe_allow_html=True)
    nav_btn("◉ Portfolio",            "portfolio")
    nav_btn("◉ Pre-BUY Signals",      "signals")
    nav_btn("◉ Scoreboard",           "scoreboard")
    nav_btn("◉ BTC Regime",           "regime")
    nav_btn("◉ Instellingen",         "settings")
    nav_btn("◉ Help & Debug",         "help")

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    # Auto refresh — instelbaar interval
    auto = st.checkbox("⚡ Auto-refresh", value=st.session_state.auto_refresh)
    if auto != st.session_state.auto_refresh:
        st.session_state.auto_refresh = auto
    if auto:
        refresh_interval = st.select_slider(
            "Interval",
            options=[15, 30, 60, 120, 300],
            value=st.session_state.get("refresh_interval", DASHBOARD_REFRESH),
            format_func=lambda x: f"{x}s" if x < 60 else f"{x//60}min",
            key="sidebar_refresh_interval",
        )
        st.session_state["refresh_interval"] = refresh_interval
        st.caption(f"🔄 Ververst elke {refresh_interval}s")
        time.sleep(refresh_interval)
        st.rerun()

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown(f'<div class="small-muted">🕐 {now_utc().strftime("%H:%M:%S UTC")}</div>', unsafe_allow_html=True)
    st.markdown('<div class="small-muted">Crypto AI Terminal v3.0</div>', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


# ============================================================
# TRADE LIST + DETAIL
# ============================================================
def get_selected_trade(df: pd.DataFrame) -> Optional[pd.Series]:
    if df.empty:
        return None
    sel = st.session_state.get("selected_page_trade_id")
    if sel is None:
        st.session_state.selected_page_trade_id = safe_str(df.iloc[0]["trade_id"])
        return df.iloc[0]
    match = df[df["trade_id"].astype(str) == str(sel)]
    if not match.empty:
        return match.iloc[0]
    st.session_state.selected_page_trade_id = safe_str(df.iloc[0]["trade_id"])
    return df.iloc[0]


def render_trade_list(df: pd.DataFrame, title: str = "Trades") -> None:
    st.markdown(f'<div class="section-title">{title}</div>', unsafe_allow_html=True)
    if df.empty:
        st.info("Geen trades voor deze selectie.")
        return
    for _, row in df.head(12).iterrows():
        trade_id = safe_str(row.get("trade_id"))
        outcome  = safe_str(row.get("outcome"))
        pnl      = safe_float(row.get("pnl_r"))
        color    = "#34d399" if outcome == "WIN" else "#fb7185" if outcome == "LOSS" else "#94a3b8"
        label    = (
            f"[{outcome}] {safe_str(row.get('symbol'))} | "
            f"{safe_str(row.get('setup_type'))} | "
            f"{format_r(pnl)} | "
            f"{safe_str(row.get('datetime'))}"
        )
        active  = st.session_state.get("selected_page_trade_id") == trade_id
        wrapper = "trade-button nav-button-active" if active else "trade-button"
        st.markdown(f'<div class="{wrapper}">', unsafe_allow_html=True)
        if st.button(label, key=f"tl_{trade_id}", use_container_width=True):
            st.session_state.selected_page_trade_id = trade_id
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)


def render_trade_detail(row: Optional[pd.Series]) -> None:
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Trade Detail</div>', unsafe_allow_html=True)
    if row is None:
        st.info("Selecteer een trade links.")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    outcome   = safe_str(row.get("outcome"))
    pnl_r     = safe_float(row.get("pnl_r"))
    pnl_eur   = safe_float(row.get("pnl_eur"))
    entry     = safe_float(row.get("entry"))
    stop      = safe_float(row.get("stop"))
    target    = safe_float(row.get("target"))
    trade_type = safe_str(row.get("trade_type","")).upper()
    is_real   = trade_type in ("REAL","LIVE")
    rr        = abs(target - entry) / max(abs(entry - stop), 0.0001) if entry > 0 and stop > 0 and target > 0 else 0.0

    outcome_color = "#34d399" if outcome == "WIN" else "#fb7185" if outcome == "LOSS" else "#94a3b8"

    st.markdown(f"""
    <div class="trade-chip-row">
        <div class="trade-chip" style="color:{outcome_color}">{outcome}</div>
        <div class="trade-chip">{safe_str(row.get("symbol"))}</div>
        <div class="trade-chip">{trade_type}</div>
        <div class="trade-chip">{safe_str(row.get("setup_type"))}</div>
        <div class="trade-chip">{safe_str(row.get("timeframe"))}</div>
        <div class="trade-chip">{safe_str(row.get("regime"))}</div>
    </div>
    """, unsafe_allow_html=True)

    c1, c2 = st.columns(2, gap="small")
    with c1:
        for label, value in [
            ("Score",      str(safe_int(row.get("score")))),
            ("Chance",     format_pct(row.get("chance"))),
            ("Confidence", format_pct(row.get("confidence"))),
            ("Entry",      format_price(entry)),
            ("Stop",       format_price(stop)),
            ("Target",     format_price(target)),
            ("R/R",        f"1:{rr:.2f}"),
        ]:
            st.markdown(f'<div class="list-row"><div class="list-left">{label}</div><div class="list-right">{value}</div></div>', unsafe_allow_html=True)

    with c2:
        dur_str = "-"
        created = parse_dt(row.get("created_at"))
        closed  = parse_dt(row.get("closed_at"))
        if not pd.isna(created) and not pd.isna(closed):
            mins    = int((closed - created).total_seconds() / 60)
            dur_str = f"{mins//60}h {mins%60}m"

        # P&L EUR alleen tonen bij echte trades
        pnl_rij = [
            ("P&L",    format_r(pnl_r) + (f" / {format_money(pnl_eur)}" if is_real and abs(pnl_eur) > 0.0001 else "")),
        ]
        rest = [
            ("Open",   format_dt(row.get("created_at"))),
            ("Close",  format_dt(row.get("closed_at"))),
            ("Duur",   dur_str),
            ("Label",  safe_str(row.get("label"), "-")),
            ("MFE",    format_price(row.get("mfe")) if safe_float(row.get("mfe")) > 0 else "-"),
            ("MAE",    format_price(row.get("mae")) if safe_float(row.get("mae")) > 0 else "-"),
        ]
        for label, value in pnl_rij + rest:
            st.markdown(f'<div class="list-row"><div class="list-left">{label}</div><div class="list-right">{value}</div></div>', unsafe_allow_html=True)

    st.plotly_chart(chart_trade_detail(row), use_container_width=True,
                    config={"displayModeBar":False},
                    key=f"td_{safe_str(row.get('trade_id','x'))}")

    # Trade analyse tekst
    sym    = safe_str(row.get("symbol"))
    setup  = safe_str(row.get("setup_type"))
    regime = safe_str(row.get("regime"))
    eur_deel = f" ({format_money(pnl_eur)})" if is_real and abs(pnl_eur) > 0.0001 else ""
    analyse = (
        f"{'✅ WIN' if outcome=='WIN' else '❌ LOSS'} | {sym} | {setup} | {regime} | {trade_type}\n"
        f"Entry: {format_price(entry)} → Stop: {format_price(stop)} → Target: {format_price(target)} | R/R 1:{rr:.2f}\n"
        f"Resultaat: {format_r(pnl_r)}{eur_deel} | Duur: {dur_str}"
    )
    st.markdown(f'<div class="trade-note">{analyse}</div>', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


# ============================================================
# TOP BALK — bot status + BTC regime + key metrics
# ============================================================
def render_top_bar(
    bot_label:     str,
    bot_emoji:     str,
    bot_type:      str,
    btc:           Dict,
    pnl_today:     float,
    pf30:          float,
    cons:          int,
    open_count:    int,
    source_mode:   str,
) -> None:
    """
    Professionele top balk met:
    - Brand naam
    - Bot status
    - BTC regime
    - PnL vandaag
    - Profit Factor 30d
    - Consecutive losses
    - Open trades
    - Source mode
    """
    btc_regime   = safe_str(btc.get("regime"), "?").upper()
    btc_strength = safe_float(btc.get("strength"))
    btc_close    = safe_float(btc.get("close"))
    btc_ema200   = safe_float(btc.get("ema200"))

    bot_chip_cls = {"active":"chip-green","paused":"chip-yellow","stopped":"chip-red"}.get(bot_type,"chip-gray")
    btc_chip_cls = {"BULL":"chip-green","BEAR":"chip-red","RANGE":"chip-yellow"}.get(btc_regime,"chip-gray")
    pnl_cls      = "chip-green" if pnl_today >= 0 else "chip-red"
    pf_cls       = "chip-green" if pf30 >= 1.5 else "chip-red"
    cons_cls     = "chip-red"   if cons >= 3   else "chip-gray"
    src_cls      = "chip-green" if source_mode == "DB_PRIORITY" else "chip-yellow"

    pnl_sign = "+" if pnl_today >= 0 else ""
    db_ok    = "✅ DB" if db_ready() else "⚠️ DEMO"

    st.markdown(f"""
    <div class="topbar">
        <div class="brand">
            <div class="brand-mark"></div>
            <div>
                <div class="brand-title">Crypto AI Terminal</div>
                <div class="brand-sub">Bloomberg Terminal Stijl v3.0</div>
            </div>
        </div>
        <div class="top-status-row">
            <span class="top-status-chip {bot_chip_cls}">{bot_emoji} {bot_label}</span>
            <span class="top-status-chip {btc_chip_cls}">₿ BTC {btc_regime} {btc_strength:.0f}%</span>
            <span class="top-status-chip chip-blue">💱 €{btc_close:,.0f} vs EMA200 €{btc_ema200:,.0f}</span>
            <span class="top-status-chip {pnl_cls}">💶 Vandaag {pnl_sign}€{abs(pnl_today):.2f}</span>
            <span class="top-status-chip {pf_cls}">📈 PF30d {pf30:.2f}</span>
            <span class="top-status-chip {cons_cls}">📉 Streak {cons}x</span>
            <span class="top-status-chip chip-blue">📂 Open {open_count}</span>
            <span class="top-status-chip {src_cls}">{db_ok} | {source_mode}</span>
        </div>
    </div>
    """, unsafe_allow_html=True)


# ============================================================
# PAGINA RENDERS
# ============================================================

def render_dashboard(
    history_df: pd.DataFrame,
    real_df:    pd.DataFrame,
    sim_df:     pd.DataFrame,
    shadow_df:  pd.DataFrame,
    source_mode: str,
) -> None:
    """Dashboard pagina — hero metrics + grafieken + trade tape."""
    st.markdown('<div class="panel">', unsafe_allow_html=True)

    # ── PERMANENTE WIN/LOSS BAR ──────────────────────────────
    render_overall_winloss_bar(history_df)

    # ── SCANNER + DAGBUDGET WIDGETS ─────────────────────────
    wb1, wb2, wb3 = st.columns([1.5, 1.0, 1.0], gap="small")
    with wb1:
        render_scanner_status_widget()
    with wb2:
        render_dagbudget_widget(real_df)
    with wb3:
        # Consecutive losses waarschuwing
        cons = get_consecutive_losses(real_df)
        pf30 = get_profit_factor_30d(real_df)
        rf   = get_recovery_factor(real_df)
        st.markdown(metric_card("Verlies Streak nu", f"{cons}x {'🔴' if cons >= 3 else '🟡' if cons >= 1 else '✅'}", "verliezen op rij", "red" if cons >= 3 else "orange" if cons >= 1 else "green"), unsafe_allow_html=True)
        st.markdown(metric_card("Recovery Factor", f"{rf:.2f}", "doel: >2.0 | PF:" + f"{pf30:.2f}", "green" if rf >= 2.0 else "orange" if rf >= 1.0 else "red"), unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    summary_all  = perf_summary(history_df)
    summary_real = perf_summary(real_df)

    c1, c2, c3, c4, c5, c6 = st.columns(6, gap="small")
    with c1:
        st.markdown(metric_card("Trades (totaal)", str(int(summary_all["count"])), "", "blue"), unsafe_allow_html=True)
    with c2:
        wr_all = format_pct(summary_all["winrate"])
        st.markdown(metric_card("Win Rate (all)", wr_all, f"R: {format_r(summary_all['total_r'])}", "green"), unsafe_allow_html=True)
    with c3:
        wr_real = format_pct(summary_real["winrate"])
        eur_sub = f"€{summary_real['total_eur']:.4f}" if abs(summary_real['total_eur']) > 0 else "—"
        st.markdown(metric_card("Win Rate (live)", wr_real, eur_sub, "orange"), unsafe_allow_html=True)
    with c4:
        pf = summary_real["profit_factor"]
        pf_color = "green" if pf >= 1.5 else "red"
        st.markdown(metric_card("Profit Factor 30d", f"{pf:.2f}", "✅ OK" if pf >= 1.5 else "⚠️ Laag", pf_color), unsafe_allow_html=True)
    with c5:
        st.markdown(metric_card("Expectancy", f"{summary_all['expectancy']:.3f} R", "Per trade gemiddeld", "purple"), unsafe_allow_html=True)
    with c6:
        dd = summary_real["max_dd"]
        st.markdown(metric_card("Max Drawdown", f"{dd:.2f} R", "Live trades", "red"), unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    # Hero donuts + statistieken
    hero_l, hero_m, hero_r = st.columns([1.1, 1.5, 1.0], gap="medium")

    with hero_l:
        st.markdown('<div class="hero-card">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Trade Win Rate</div>', unsafe_allow_html=True)
        st.plotly_chart(
            chart_donut(summary_all["winrate"], summary_all["total_eur"], "Alle trades"),
            use_container_width=True, config={"displayModeBar":False}, key="hero_donut_all"
        )
        st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Live Win Rate</div>', unsafe_allow_html=True)
        st.plotly_chart(
            chart_donut(summary_real["winrate"], summary_real["total_eur"], "REAL trades"),
            use_container_width=True, config={"displayModeBar":False}, key="hero_donut_real"
        )
        st.markdown("</div>", unsafe_allow_html=True)

    with hero_m:
        st.markdown('<div class="hero-card">', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Equity Curve</div>', unsafe_allow_html=True)
        st.plotly_chart(
            chart_equity_curve(history_df, "Cumulatief R"),
            use_container_width=True, config={"displayModeBar":False}, key="hero_equity"
        )
        st.plotly_chart(
            chart_daily_r(history_df, "Dagresultaten"),
            use_container_width=True, config={"displayModeBar":False}, key="hero_daily"
        )
        st.markdown("</div>", unsafe_allow_html=True)

    with hero_r:
        dominant = "green" if summary_real["winrate"] >= 50 else "red"
        dom_label = "WINST DOMINEERT" if dominant == "green" else "VERLIES DOMINEERT"
        st.markdown('<div class="hero-card">', unsafe_allow_html=True)
        st.markdown(f'<div class="dominance-pill {dominant}">{"🟢" if dominant=="green" else "🔴"} {dom_label}</div>', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Live Statistieken</div>', unsafe_allow_html=True)

        stats_rows = [
            ("Live Trades",    str(int(summary_real["count"])),                          ""),
            ("Wins",           str(int(summary_real["count"] * summary_real["winrate"] / 100)), f'{summary_real["winrate"]:.1f}%'),
            ("Losses",         str(int(summary_real["count"] * (100 - summary_real["winrate"]) / 100)), f'{100-summary_real["winrate"]:.1f}%'),
            ("Netto R",        format_r(summary_real["total_r"]),                        ""),
            ("Netto EUR",      format_money(summary_real["total_eur"]),                  "echt geld"),
            ("Bruto Winst",    format_money(summary_real["gross_profit"]),               ""),
            ("Bruto Verlies",  format_money(summary_real["gross_loss"]),                 ""),
            ("Profit Factor",  f'{summary_real["profit_factor"]:.2f}',                  "doel: >1.5"),
            ("Expectancy",     f'{summary_real["expectancy"]:.3f} R',                   "per trade"),
        ]
        for label, value, badge in stats_rows:
            badge_html = f'<span class="hero-stat-badge blue">{badge}</span>' if badge else ""
            st.markdown(f'<div class="hero-stat-row"><div class="hero-stat-label">{label}</div><div class="hero-stat-value">{value}{badge_html}</div></div>', unsafe_allow_html=True)

        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    # Tabs voor analyse
    tab_filters, tab_charts, tab_tape = st.tabs(["🔍 Gefilterde Data", "📊 Analyse Charts", "📋 Trade Tape"])

    with tab_filters:
        filtered = render_filters(history_df, include_trade_type=True)
        fsummary = perf_summary(filtered)
        fm1, fm2, fm3, fm4 = st.columns(4, gap="small")
        with fm1: st.markdown(metric_card("Trades (filter)", str(int(fsummary["count"])), "", "blue"), unsafe_allow_html=True)
        with fm2: st.markdown(metric_card("Win Rate", format_pct(fsummary["winrate"]), "", "green"), unsafe_allow_html=True)
        with fm3: st.markdown(metric_card("Totale R", format_r(fsummary["total_r"]), "", "orange"), unsafe_allow_html=True)
        with fm4: st.markdown(metric_card("Profit Factor", f'{fsummary["profit_factor"]:.2f}', "", "purple"), unsafe_allow_html=True)

        fc1, fc2 = st.columns(2, gap="small")
        with fc1: st.plotly_chart(chart_equity_curve(filtered, "Equity Curve (filter)"), use_container_width=True, config={"displayModeBar":False}, key="filter_equity")
        with fc2: st.plotly_chart(chart_win_loss_bar(filtered, "Win/Loss (filter)"), use_container_width=True, config={"displayModeBar":False}, key="filter_wl")

    with tab_charts:
        tc1, tc2 = st.columns(2, gap="small")
        with tc1:
            st.plotly_chart(chart_setup_perf(history_df, "Setup Performance"), use_container_width=True, config={"displayModeBar":False}, key="dash_setup")
        with tc2:
            st.plotly_chart(chart_regime_dist(history_df, "Regime Verdeling"), use_container_width=True, config={"displayModeBar":False}, key="dash_regime")

        tc3, tc4 = st.columns(2, gap="small")
        with tc3:
            st.plotly_chart(chart_setup_perf(sim_df, "SIM Setup Performance"), use_container_width=True, config={"displayModeBar":False}, key="dash_sim_setup")
        with tc4:
            st.plotly_chart(chart_daily_r(real_df, "Live Dagresultaten"), use_container_width=True, config={"displayModeBar":False}, key="dash_real_daily")

    with tab_tape:
        tt1, tt2 = st.columns([1.2, 0.95], gap="small")
        with tt1:
            render_trade_list(history_df, "Alle Trades (meest recent)")
        with tt2:
            selected = get_selected_trade(history_df)
            render_trade_detail(selected)

    # Claude AI knoppen
    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown("### 🧠 Claude AI Analyses")
    ck1, ck2, ck3 = st.columns(3, gap="small")
    with ck1:
        claude_btn("Analyseer 30d performance", f"""
Je bent een crypto trading coach.
Analyseer in 4 zinnen Nederlands.
Win rate: {format_pct(summary_real['winrate'])}
PnL: {format_money(summary_real['total_eur'])}
Profit Factor: {summary_real['profit_factor']:.2f}
Expectancy: {summary_real['expectancy']:.2f} R
Wat zijn de sterkste punten en wat moet verbeteren?
""", 250, "cl_perf")
    with ck2:
        claude_btn("Setup advies", f"""
Je bent een crypto trading coach.
Top setups uit de data. Analyseer in 3 zinnen welke setup het beste werkt.
Win rate: {format_pct(summary_all['winrate'])}
Totale R: {format_r(summary_all['total_r'])}
Beste aanbeveling voor volgende week?
""", 200, "cl_setup")
    with ck3:
        claude_btn("Risico assessment", f"""
Je bent een crypto risk manager.
Beoordeel in 3 zinnen het risico profiel.
Max drawdown: {summary_real['max_dd']:.2f} R
Profit Factor: {summary_real['profit_factor']:.2f}
Expectancy: {summary_real['expectancy']:.2f} R
Is dit systeem gezond of te risicovol?
""", 200, "cl_risk")

    st.markdown("</div>", unsafe_allow_html=True)


def render_trade_page(
    page_name: str,
    df:        pd.DataFrame,
    title:     str,
    subtitle:  str,
) -> None:
    """Generieke pagina voor REAL, SIM, SHADOW trades."""
    render_alarm_banner()
    is_real_page = page_name == "live"
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown(f'<div class="section-title">{title}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="section-subtitle">{subtitle}</div>', unsafe_allow_html=True)

    filtered = render_filters(df, include_trade_type=False)
    summary  = perf_summary(filtered)

    # ── METRICS ──────────────────────────────────────────────
    m1, m2, m3, m4, m5 = st.columns(5, gap="small")
    with m1: st.markdown(metric_card("Trades", str(int(summary["count"])), "", "blue"), unsafe_allow_html=True)
    with m2: st.markdown(metric_card("Win Rate", format_pct(summary["winrate"]), f"E: {summary['expectancy']:.3f}R", "green"), unsafe_allow_html=True)
    with m3:
        if is_real_page:
            st.markdown(metric_card("Netto EUR", format_money(summary["total_r"] * MAX_PER_TRADE_EUR / 0.5 if summary["total_eur"] == 0 else summary["total_eur"]).replace("+",""), f"R: {format_r(summary['total_r'])}", "orange"), unsafe_allow_html=True)
        else:
            st.markdown(metric_card("Totale R", format_r(summary["total_r"]), "SIM — geen echt geld", "orange"), unsafe_allow_html=True)
    with m4: st.markdown(metric_card("Profit Factor", f'{summary["profit_factor"]:.2f}', "✅ OK" if summary["profit_factor"] >= 1.5 else "⚠️ Laag", "purple" if summary["profit_factor"] >= 1.5 else "red"), unsafe_allow_html=True)
    with m5: st.markdown(metric_card("Max Drawdown", f'{summary["max_dd"]:.2f} R', "", "red"), unsafe_allow_html=True)

    # ── CHARTS ──────────────────────────────────────────────
    c1, c2 = st.columns([1.4, 1.0], gap="small")
    with c1:
        st.plotly_chart(chart_equity_curve(filtered, f"{title} Equity Curve"),
                        use_container_width=True, config={"displayModeBar":False}, key=f"{page_name}_equity")
    with c2:
        st.plotly_chart(chart_win_loss_bar(filtered, "Win / Loss"),
                        use_container_width=True, config={"displayModeBar":False}, key=f"{page_name}_wl")

    c3, c4 = st.columns([1.4, 1.0], gap="small")
    with c3:
        st.plotly_chart(chart_setup_perf(filtered, "Setup Performance"),
                        use_container_width=True, config={"displayModeBar":False}, key=f"{page_name}_setup")
    with c4:
        st.plotly_chart(chart_daily_r(filtered, "Dagresultaten"),
                        use_container_width=True, config={"displayModeBar":False}, key=f"{page_name}_daily")

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    # ── TRADE LIJST + DETAIL ─────────────────────────────────
    tl, tr = st.columns([1.2, 0.95], gap="small")
    with tl:
        render_trade_list(filtered, f"{title} — Trade Lijst")
    with tr:
        selected = get_selected_trade(filtered)
        render_trade_detail(selected)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    # ── DATA TABEL — alleen relevante kolommen ──────────────
    # EUR kolommen alleen bij REAL pagina
    basis_cols = ["symbol","setup_type","regime","timeframe","outcome","pnl_r","score"]
    eur_cols   = ["pnl_eur"] if is_real_page else []
    extra_cols = ["datetime"] if "datetime" in filtered.columns else []
    display_cols = [c for c in basis_cols + eur_cols + extra_cols if c in filtered.columns]

    if not filtered.empty and display_cols:
        # Hernoem voor leesbaarheid
        rename = {
            "symbol": "Coin", "setup_type": "Setup", "regime": "Regime",
            "timeframe": "TF", "outcome": "Uitkomst", "pnl_r": "R",
            "pnl_eur": "EUR", "score": "Score", "datetime": "Datum",
        }
        weergave = filtered[display_cols].head(200).rename(columns=rename)
        # Kleur uitkomst kolom
        def kleur_uitkomst(val):
            if str(val).upper() == "WIN":
                return "color: #2ecc71"
            elif str(val).upper() == "LOSS":
                return "color: #e74c3c"
            return ""

        styled = weergave.style.applymap(kleur_uitkomst, subset=["Uitkomst"]) if "Uitkomst" in weergave.columns else weergave
        st.dataframe(styled, hide_index=True, use_container_width=True)

    csv = filtered.to_csv(index=False).encode("utf-8")
    st.download_button(f"⬇️ Download {title} CSV", csv, f"{page_name}_trades.csv", "text/csv", use_container_width=True)

    st.markdown("</div>", unsafe_allow_html=True)


def render_portfolio_page(snapshot: dict, assets_df: pd.DataFrame, snap_mode: str) -> None:
    """Portfolio pagina met Bitvavo assets."""
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Portfolio Overzicht</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="section-subtitle">Bitvavo portfolio snapshot. Mode: {snap_mode} | Bijgewerkt: {format_dt(snapshot.get("ts"))}</div>', unsafe_allow_html=True)

    eur_bal    = safe_float(snapshot.get("eur_available"))
    crypto_eur = safe_float(snapshot.get("crypto_assets_eur"))
    total_eur  = safe_float(snapshot.get("total_portfolio_eur"))
    cash_pct   = (eur_bal / max(total_eur, 0.01)) * 100

    m1, m2, m3, m4 = st.columns(4, gap="small")
    with m1: st.markdown(metric_card("Totaal Portfolio", format_money(total_eur).replace("+",""), "", "blue"), unsafe_allow_html=True)
    with m2: st.markdown(metric_card("Cash (EUR)", format_money(eur_bal).replace("+",""), f"{cash_pct:.1f}% van portfolio", "green"), unsafe_allow_html=True)
    with m3: st.markdown(metric_card("Crypto Waarde", format_money(crypto_eur).replace("+",""), f"{100-cash_pct:.1f}% van portfolio", "orange"), unsafe_allow_html=True)
    with m4:
        status_chip = f'<span class="top-status-chip {"chip-green" if snap_mode=="OK" or snap_mode=="LIVE_API" else "chip-yellow"}">{snap_mode}</span>'
        st.markdown(metric_card("Data Mode", snap_mode, "", "purple"), unsafe_allow_html=True)

    col_pie, col_table = st.columns([1.1, 0.9], gap="medium")

    with col_pie:
        st.plotly_chart(chart_portfolio_pie(assets_df), use_container_width=True, config={"displayModeBar":False}, key="portfolio_pie")

    with col_table:
        st.markdown('<div class="section-title">Holdings</div>', unsafe_allow_html=True)
        if assets_df.empty:
            st.info("Geen assets gevonden.")
        else:
            for _, row in assets_df.head(10).iterrows():
                sym   = safe_str(row.get("symbol"))
                val   = safe_float(row.get("eur_value"))
                total = safe_float(row.get("total"))
                price = safe_float(row.get("price_eur"))
                share = (val / max(total_eur, 0.01)) * 100
                st.markdown(f"""
                <div class="holding-row">
                    <div>
                        <div class="holding-symbol">{sym}</div>
                        <div class="holding-sub">{total:.8f} @ {format_money(price).replace("+","")}</div>
                    </div>
                    <div>
                        <div class="holding-value">{format_money(val).replace("+","")}</div>
                        <div class="holding-share">{share:.1f}%</div>
                    </div>
                </div>
                """, unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    if st.button("🔄 Refresh Portfolio van Bitvavo API", use_container_width=True, key="portfolio_refresh"):
        if API_KEY and API_SECRET:
            try:
                new_snap = build_snapshot()
                st.success(f"✅ Portfolio bijgewerkt! Totaal: {format_money(new_snap.get('total_portfolio_eur',0)).replace('+','')}")
                st.rerun()
            except Exception as e:
                st.error(f"❌ Bitvavo API fout: {e}")
        else:
            st.warning("⚠️ BITVAVO_API_KEY en BITVAVO_API_SECRET niet ingesteld in Render.")

    st.markdown("</div>", unsafe_allow_html=True)


def render_signals_page(pending_df: pd.DataFrame) -> None:
    """Pre-BUY Signals pagina — aangemaakt door multi_coin_score.py."""
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">📋 Pre-BUY Signals</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-subtitle">'
        'Actieve signals aangemaakt door multi_coin_score.py. '
        'Worden automatisch uitgevoerd via /auto_buy in whatsapp_webhook.py. '
        'Minimale score: 85. Verlopen na 4 uur.'
        '</div>',
        unsafe_allow_html=True,
    )

    if pending_df.empty:
        st.info(
            "Geen actieve signals. "
            "Scanner genereert signalen elke 30 minuten tijdens trading hours (08:00-22:00 UTC). "
            "Als BTC in BEAR regime staat, worden signals niet uitgevoerd."
        )
        st.markdown("</div>", unsafe_allow_html=True)
        return

    st.success(f"✅ {len(pending_df)} actief signal(s)")

    # Score histogram
    st.plotly_chart(chart_pending_scores(pending_df), use_container_width=True,
                    config={"displayModeBar":False}, key="signals_scores")

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    # Signal kaarten
    for _, row in pending_df.iterrows():
        sym     = safe_str(row.get("symbol"), "?")
        setup   = safe_str(row.get("setup_type"), "-")
        regime  = safe_str(row.get("regime"), "-")
        score   = safe_int(row.get("score"))
        chance  = safe_int(row.get("chance"))
        conf    = safe_int(row.get("confidence"))
        entry   = safe_float(row.get("entry"))
        stop_p  = safe_float(row.get("stop"))
        target  = safe_float(row.get("target"))
        exp_wr  = safe_float(row.get("exp_win_rate")) * 100
        exp_n   = safe_int(row.get("exp_n"))
        why     = safe_str(row.get("why_tag"), "")
        tf      = safe_str(row.get("timeframe"), "4h")
        expires = row.get("expires_at")
        exp_str = format_dt(expires) if expires is not None else "-"

        risk   = abs(entry - stop_p) if stop_p > 0 and entry > 0 else 0
        reward = abs(target - entry) / max(risk, 0.0001) if risk > 0 else 0

        score_cls = "chip-green" if score >= 85 else "chip-yellow" if score >= 75 else "chip-red"

        st.markdown(f"""
        <div class="signal-card">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
                <div class="signal-symbol">{sym} — {setup}/{regime} — {tf}</div>
                <span class="top-status-chip {score_cls}">Score: {score}</span>
            </div>
            <div class="signal-details">
                Kans: {chance}% | Conf: {conf}% | Experience: {exp_wr:.0f}% WR ({exp_n} trades)
            </div>
            <div class="signal-details">
                Entry: {format_price(entry)} | Stop: {format_price(stop_p)} | Target: {format_price(target)} | R/R: 1:{reward:.1f}
            </div>
            <div class="signal-details" style="margin-top:4px;">
                {why} | Verloopt: {exp_str}
            </div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)


def render_scoreboard_page(scoreboard_df: pd.DataFrame) -> None:
    """Experience Scoreboard pagina — lerende systeem van de bot."""
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">🏆 Experience Scoreboard</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-subtitle">'
        'Win rates per setup/regime combinatie. '
        'Gebouwd uit alle gesloten trades. '
        'multi_coin_score.py gebruikt dit als experience weight in de score berekening. '
        'Groen ≥60% | Geel ≥45% | Rood <45%.'
        '</div>',
        unsafe_allow_html=True,
    )

    if scoreboard_df.empty:
        st.info("Geen scoreboard data. Wordt gevuld na gesloten trades.")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    st.plotly_chart(chart_scoreboard_bar(scoreboard_df), use_container_width=True,
                    config={"displayModeBar":False}, key="scoreboard_chart")

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    # Filter
    col_min, col_regime, _ = st.columns([1, 1, 2], gap="small")
    with col_min:
        min_n = st.slider("Min. trades", 1, 50, 5)
    with col_regime:
        regimes = ["ALLES"] + sorted(scoreboard_df["market_regime"].dropna().astype(str).unique().tolist())
        sel_r   = st.selectbox("Regime", regimes)

    filtered_sb = scoreboard_df[scoreboard_df["n"] >= min_n].copy()
    if sel_r != "ALLES":
        filtered_sb = filtered_sb[filtered_sb["market_regime"] == sel_r]
    filtered_sb = filtered_sb.sort_values("win_rate", ascending=False)

    for _, row in filtered_sb.head(25).iterrows():
        setup   = safe_str(row.get("setup_type"), "-")
        regime  = safe_str(row.get("market_regime"), "-")
        n       = safe_int(row.get("n"))
        wins    = safe_int(row.get("wins"))
        losses  = safe_int(row.get("losses"))
        wr      = safe_float(row.get("win_rate"))
        avg_pnl = safe_float(row.get("avg_pnl"))
        avg_r   = safe_float(row.get("avg_r"))

        wr_cls = "chip-green" if wr >= 60 else "chip-yellow" if wr >= 45 else "chip-red"
        pnl_cls = "chip-green" if avg_pnl >= 0 else "chip-red"

        st.markdown(f"""
        <div class="score-row">
            <div class="score-left">{setup} / {regime} — {n} trades ({wins}W / {losses}L)</div>
            <div style="display:flex;gap:6px;align-items:center;">
                <span class="top-status-chip {wr_cls}">WR {wr:.1f}%</span>
                <span class="top-status-chip {pnl_cls}">PnL {format_money(avg_pnl)}</span>
                <span class="top-status-chip chip-blue">R {avg_r:+.2f}</span>
            </div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)


def render_regime_page() -> None:
    """BTC Regime + Markt overzicht pagina."""
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">📊 BTC Regime & Markt Overzicht</div>', unsafe_allow_html=True)

    btc = get_btc_regime()
    regime   = safe_str(btc.get("regime"), "UNKNOWN").upper()
    strength = safe_float(btc.get("strength"))
    close    = safe_float(btc.get("close"))
    ema200   = safe_float(btc.get("ema200"))
    pct_ema  = safe_float(btc.get("pct_from_ema"))
    ts       = btc.get("ts_utc")

    badge_cls = {"BULL":"regime-bull","BEAR":"regime-bear","RANGE":"regime-range"}.get(regime,"regime-unkown")
    regime_emoji = {"BULL":"🟢","BEAR":"🔴","RANGE":"🟡"}.get(regime,"⚪")

    c1, c2, c3, c4 = st.columns(4, gap="small")
    with c1:
        st.markdown(metric_card("BTC Regime", f"{regime_emoji} {regime}", f"sterkte {strength:.1f}%", "orange" if regime=="BULL" else "red"), unsafe_allow_html=True)
    with c2:
        st.markdown(metric_card("BTC Prijs", f"€{close:,.0f}", "", "blue"), unsafe_allow_html=True)
    with c3:
        st.markdown(metric_card("EMA200", f"€{ema200:,.0f}", f"{pct_ema:+.2f}% afstand", "purple"), unsafe_allow_html=True)
    with c4:
        st.markdown(metric_card("Bijgewerkt", format_dt(ts), "", "green"), unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    st.markdown(f"""
    <div class="trade-note">
        <b>BTC Regime Impact op de Bot:</b><br><br>
        🟢 <b>BULL</b> — Normale trading. auto_buy actief. Scanner genereert en voert signals uit.<br>
        🟡 <b>RANGE</b> — Voorzichtige trading. auto_buy actief maar scorer is conservatiever.<br>
        🔴 <b>BEAR</b> — auto_buy GEBLOKKEERD (BTC_SKIP_BEAR=True). Scanner genereert wel Pre-BUY signals
        maar /auto_buy wordt niet getriggerd. Shadow trades gaan gewoon door.<br><br>
        <b>Huidig regime:</b> <span class="regime-badge {badge_cls}">{regime_emoji} {regime}</span>
        sterkte {strength:.1f}% | BTC €{close:,.0f} vs EMA200 €{ema200:,.0f} ({pct_ema:+.2f}%)
    </div>
    """, unsafe_allow_html=True)

    # Marktregime verdeling
    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Marktregime Verdeling (alle coins)</div>', unsafe_allow_html=True)
    st.caption("Gebouwd door regime_labeler.py. Toont hoeveel coins per regime zitten.")

    reg_df = load_market_regime_overview()
    if not reg_df.empty:
        cols = st.columns(len(reg_df), gap="small")
        for i, (_, r) in enumerate(reg_df.iterrows()):
            r_name  = safe_str(r.get("regime"), "?").upper()
            n       = safe_int(r.get("n"))
            str_val = safe_float(r.get("gem_strength"))
            r_cls   = {"BULL":"green","BEAR":"red","RANGE":"orange"}.get(r_name,"blue")
            with cols[i]:
                st.markdown(metric_card(f"{r_name} coins", str(n), f"sterkte {str_val:.0f}%", r_cls), unsafe_allow_html=True)
    else:
        st.info("Geen market_regime data. Gebouwd door regime_labeler.py (draait dagelijks).")

    # Claude regime advies
    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    claude_btn(
        "Claude: regime advies",
        f"""
Je bent een crypto markt analyst.
Geef advies in 3 zinnen Nederlands.

BTC NU:
- Regime:  {regime}
- Sterkte: {strength:.1f}%
- Prijs:   €{close:,.0f}
- EMA200:  €{ema200:,.0f}
- Afstand: {pct_ema:+.2f}%

1. Wat betekent dit regime voor de bot?
2. Welke setups werken typisch het beste?
3. Aanbevolen actie?
""", 200, "regime_claude"
    )

    st.markdown("</div>", unsafe_allow_html=True)


def render_settings_page() -> None:
    """Bot Instellingen pagina met WhatsApp commands en configuratie overzicht."""
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">⚙️ Bot Instellingen</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-subtitle">'
        'Overzicht van alle bot limieten, WhatsApp commands en Render services. '
        'Alle instellingen worden beheerd via Render Environment Variables.'
        '</div>',
        unsafe_allow_html=True,
    )

    col_left, col_right = st.columns(2, gap="medium")

    with col_left:
        st.markdown("#### 📱 WhatsApp Commands")
        st.caption("Stuur via WhatsApp naar de bot om hem te bedienen.")
        commands = [
            ("START",        "Bot begint traden"),
            ("STOP",         "Bot stopt (jij beslist altijd)"),
            ("STATUS",       "Volledige bot status"),
            ("TRADES",       "Open trades overzicht"),
            ("RAPPORT",      "Dagrapport nu sturen"),
            ("WEEKRAPPORT",  "Weekoverzicht sturen"),
            ("MAANDRAPPORT", "Maandoverzicht sturen"),
            ("ADVIES",       "Claude leeranalyse"),
            ("HEALTH",       "Health check rapport"),
            ("HELP",         "Alle commands tonen"),
        ]
        for cmd, desc in commands:
            st.markdown(f'<div class="list-row"><div class="list-left"><code>{cmd}</code></div><div class="list-right">{desc}</div></div>', unsafe_allow_html=True)

        st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
        st.markdown("#### ⏰ Render Cron Jobs")
        st.caption("Automatische rapporten via Render Cron scheduler.")
        crons = [
            ("0 8 * * *",    "/send_daily_rapport",   "Dagelijks 08:00 UTC"),
            ("0 8 * * 1",    "/send_weekly_rapport",  "Maandag 08:00 UTC"),
            ("0 9 * * 1",    "/send_health_check",    "Maandag 09:00 UTC"),
            ("0 8 1,15 * *", "/send_leeranalyse",     "1e + 15e van maand"),
            ("0 8 1 * *",    "/send_monthly_rapport", "1e van maand"),
        ]
        for cron_expr, endpoint, desc in crons:
            st.markdown(f'<div class="list-row"><div class="list-left"><code>{cron_expr} {endpoint}</code></div><div class="list-right">{desc}</div></div>', unsafe_allow_html=True)

    with col_right:
        st.markdown("#### ⚙️ Fase 1 Limieten")
        st.caption("Huidige bot limieten zoals ingesteld in Render Environment Variables.")
        limits = [
            ("MAX_PER_TRADE_EUR",       f"€{MAX_PER_TRADE_EUR:.2f}",       "Max bedrag per trade"),
            ("MAX_REAL_TRADES_PER_DAY", str(MAX_REAL_TRADES_PER_DAY),      "Max trades per dag"),
            ("MAX_OPEN_REAL_TRADES",    str(MAX_OPEN_REAL_TRADES),         "Max open trades"),
            ("DAILY_STOP_LOSS_EUR",     f"€{DAILY_STOP_LOSS_EUR:.2f}",     "Dagbudget (informatief)"),
            ("MIN_SCORE_TO_TRADE",      str(MIN_SCORE_TO_TRADE),           "Min score voor BUY"),
            ("TRADING_HOURS_START",     f"{TRADING_HOURS_START}:00 UTC",   "Start trading"),
            ("TRADING_HOURS_END",       f"{TRADING_HOURS_END}:00 UTC",     "Einde trading"),
            ("BTC_SKIP_BEAR",           "True",                             "Skip bij BEAR regime"),
            ("COIN_COOLDOWN_HOURS",     "24u",                             "Cooldown na verlies"),
            ("MAX_HOLD_HOURS",          "48u",                             "Max houdtijd per trade"),
        ]
        for k, v, desc in limits:
            st.markdown(f'<div class="list-row"><div class="list-left"><code>{k}</code><br><span style="color:#555;font-size:10px">{desc}</span></div><div class="list-right">{v}</div></div>', unsafe_allow_html=True)

        st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
        st.markdown("#### 📋 Render Services")
        st.caption("Alle services die op Render draaien voor de bot.")
        services = [
            ("crypto-ai-webhook",   "Web Service",       "whatsapp_webhook.py"),
            ("Background Worker",   "Background Worker", "trade_monitor.py"),
            ("crypto-ai-scanner",   "Background Worker", "run_bot.py (scheduler)"),
            ("crypto-ai-dashboard", "Web Service",       "app.py (dit dashboard)"),
            ("history_simulator",   "Cron Job",          "history_simulator.py"),
            ("regime_labeler",      "Cron Job",          "regime_labeler.py"),
            ("build_btc_regime",    "Cron Job",          "build_btc_regime.py"),
            ("history_fetcher",     "Cron Job",          "history_fetcher.py"),
        ]
        for name, stype, file in services:
            st.markdown(f'<div class="list-row"><div class="list-left"><b>{name}</b><br><span style="color:#555;font-size:10px">{file}</span></div><div class="list-right" style="color:#94a3b8">{stype}</div></div>', unsafe_allow_html=True)

        st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
        st.markdown("#### 🔗 Bot Architectuur")
        st.markdown("""
        <div class="trade-note">
            <b>Laag 1 — Data:</b> history_fetcher → candles, build_btc_regime → btc_regime_4h,
            history_simulator → scoreboard, regime_labeler → market_regime<br><br>
            <b>Laag 2 — Signalen:</b> multi_coin_score → pending_approvals → /auto_buy op webhook<br><br>
            <b>Laag 3 — Uitvoering:</b> whatsapp_webhook → live_trader.buy_eur() → Bitvavo BUY<br><br>
            <b>Laag 4 — Bewaking:</b> trade_monitor → SELL via live_trader, shadow_trades meekijkend<br><br>
            <b>Laag 5 — Weergave:</b> app.py → dit dashboard (leest alles, schrijft niets)
        </div>
        """, unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)


def render_help_page(
    history_df:  pd.DataFrame,
    real_df:     pd.DataFrame,
    sim_df:      pd.DataFrame,
    shadow_df:   pd.DataFrame,
    source_mode: str,
    snap_mode:   str,
) -> None:
    """Help & Debug pagina."""
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">❓ Help & Data Mapping</div>', unsafe_allow_html=True)

    st.markdown("""
    <div class="trade-note">
        <b>Data mapping in dit dashboard:</b><br><br>
        1. <b>Dashboard</b> → history_df (REAL + SIM + SHADOW gecombineerd)<br>
        2. <b>Live Performance</b> → real_df (alleen source=REAL/LIVE)<br>
        3. <b>Simulator</b> → sim_df (alleen source=SIM)<br>
        4. <b>Shadow Review</b> → shadow_df (alleen source=SHADOW)<br>
        5. <b>Portfolio</b> → snapshot + assets_df (Bitvavo API)<br>
        6. <b>Pre-BUY Signals</b> → pending_approvals tabel<br>
        7. <b>Scoreboard</b> → experience_scoreboard tabel<br>
        8. <b>BTC Regime</b> → btc_regime_4h tabel<br><br>
        <b>Demo modus:</b> Alleen actief als experience_trades leeg is.
        Zodra PostgreSQL data heeft, krijgt DB altijd prioriteit.
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown('<div class="section-title">Data Status</div>', unsafe_allow_html=True)

    rows_status = [
        ("source_mode",       source_mode),
        ("snap_mode",         snap_mode),
        ("DB verbinding",     "✅ OK" if db_ready() else "❌ DEMO/FALLBACK"),
        ("history_df rows",   str(len(history_df))),
        ("real_df rows",      str(len(real_df))),
        ("sim_df rows",       str(len(sim_df))),
        ("shadow_df rows",    str(len(shadow_df))),
        ("experience_trades", str(table_count("experience_trades"))),
        ("experience_scoreboard", str(table_count("experience_scoreboard"))),
        ("pending_approvals", str(table_count("pending_approvals"))),
        ("btc_regime_4h",     str(table_count("btc_regime_4h"))),
        ("bot_state",         str(table_count("bot_state"))),
        ("ANTHROPIC_API",     "✅" if ANTHROPIC_API_KEY else "❌"),
        ("BITVAVO_API",       "✅" if API_KEY else "❌"),
    ]
    for label, value in rows_status:
        st.markdown(f'<div class="list-row"><div class="list-left">{label}</div><div class="list-right">{value}</div></div>', unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    dc1, dc2, dc3 = st.columns(3, gap="small")
    with dc1:
        if st.button("🔄 Clear cache", use_container_width=True, key="help_clear_cache"):
            st.cache_data.clear()
            st.success("Cache geleegd!")
            st.rerun()
    with dc2:
        if st.button("🐛 Toggle debug", use_container_width=True, key="help_debug"):
            st.session_state.show_debug = not st.session_state.show_debug
            st.rerun()
    with dc3:
        if st.button("🔁 Reset session", use_container_width=True, key="help_reset"):
            for k, v in SESSION_DEFAULTS.items():
                st.session_state[k] = v
            st.rerun()

    if st.session_state.show_debug:
        st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
        st.markdown('<div class="section-title">Debug Logs</div>', unsafe_allow_html=True)
        for event in st.session_state.debug_events[:15]:
            st.markdown(f'<div class="small-muted">• {event}</div>', unsafe_allow_html=True)

        st.json({
            "database_url": bool(DATABASE_URL),
            "api_key":      bool(API_KEY),
            "anthropic":    bool(ANTHROPIC_API_KEY),
            "history_rows": len(history_df),
            "real_rows":    len(real_df),
            "sim_rows":     len(sim_df),
            "shadow_rows":  len(shadow_df),
            "last_error":   st.session_state.last_error_text,
        })

    st.markdown("</div>", unsafe_allow_html=True)


# ============================================================
# PERMANENTE WIN/LOSS BAR — altijd zichtbaar
# ============================================================
def render_overall_winloss_bar(history_df: pd.DataFrame) -> None:
    """
    Permanente Win/Loss balk — altijd zichtbaar bovenaan dashboard.
    Gebruikt native Streamlit columns voor betrouwbare rendering.
    Toont win/loss procenten voor ALLES, REAL, SIM en SHADOW.
    """
    stats = get_overall_winloss(history_df)

    col_titel, col_all, col_real, col_sim, col_shadow = st.columns(
        [0.5, 1, 1, 1, 1], gap="small"
    )

    with col_titel:
        st.markdown(
            '<div style="padding-top:10px;color:#94a3b8;font-size:10px;'
            'font-weight:900;text-transform:uppercase;letter-spacing:0.06em;">'
            'WIN %<br>LOSS %</div>',
            unsafe_allow_html=True,
        )

    for col, key, emoji, label in [
        (col_all,    "ALLES",  "📊", "Alle Trades"),
        (col_real,   "REAL",   "💶", "Live (REAL)"),
        (col_sim,    "SIM",    "🔮", "Simulatie"),
        (col_shadow, "SHADOW", "🎭", "Shadow"),
    ]:
        s        = stats.get(key, {})
        win_pct  = safe_float(s.get("win_pct"))
        loss_pct = safe_float(s.get("loss_pct"))
        wins     = safe_int(s.get("wins"))
        losses   = safe_int(s.get("losses"))
        total    = safe_int(s.get("total"))

        with col:
            st.markdown(
                f'<div style="background:rgba(255,255,255,0.04);'
                f'border:1px solid rgba(255,255,255,0.08);'
                f'border-radius:12px;padding:8px 10px;">' 
                f'<div style="color:#94a3b8;font-size:10px;font-weight:700;'
                f'text-transform:uppercase;">{emoji} {label}</div>'
                f'<div style="display:flex;gap:8px;align-items:center;margin-top:4px;">'
                f'<span style="color:#34d399;font-size:15px;font-weight:900;">{win_pct:.1f}%</span>'
                f'<span style="color:#555;font-size:12px;">|</span>'
                f'<span style="color:#fb7185;font-size:15px;font-weight:900;">{loss_pct:.1f}%</span>'
                f'</div>'
                f'<div style="color:#94a3b8;font-size:10px;margin-top:3px;">'
                f'{wins}W / {losses}L — {total} trades</div>'
                f'</div>',
                unsafe_allow_html=True,
            )


# ============================================================
# SCANNER STATUS WIDGET
# ============================================================
def render_scanner_status_widget() -> None:
    """Toont scanner status — wanneer voor het last gescand, hoeveel signals."""
    scanner = get_scanner_status()
    bot_active = scanner["bot_active"].lower() == "true"
    mins = scanner["mins_since_scan"]

    if mins < 0:
        scan_str = "Onbekend"
        scan_cls = "chip-gray"
    elif mins <= 35:
        scan_str = f"{mins} min geleden"
        scan_cls = "chip-green"
    elif mins <= 90:
        scan_str = f"{mins} min geleden"
        scan_cls = "chip-yellow"
    else:
        scan_str = f"{mins} min geleden ⚠️"
        scan_cls = "chip-red"

    active_chip = (
        '<span class="top-status-chip chip-green">🟢 Bot ACTIEF</span>'
        if bot_active else
        '<span class="top-status-chip chip-red">🔴 Bot GESTOPT</span>'
    )

    st.markdown(f"""
    <div class="scanner-card">
        <div class="scanner-title">⚡ Scanner Status</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px;">
            {active_chip}
            <span class="top-status-chip {scan_cls}">🕐 Scan: {scan_str}</span>
            <span class="top-status-chip chip-blue">📋 Signals vandaag: {scanner['signals_today']}</span>
            <span class="top-status-chip chip-green">✅ Uitgevoerd: {scanner['signals_executed']}</span>
            <span class="top-status-chip chip-orange">⏳ Pending: {scanner['signals_pending']}</span>
        </div>
    </div>
    """, unsafe_allow_html=True)


# ============================================================
# DAGBUDGET WIDGET
# ============================================================
def render_dagbudget_widget(real_df: pd.DataFrame) -> None:
    """Toont dagbudget status — hoeveel van de €5 is al gebruikt."""
    budget = get_dagbudget_status(real_df)
    pct    = budget["budget_pct"]
    fill_cls = "budget-fill-safe" if pct < 60 else "budget-fill-warn"

    pnl = budget["pnl_today"]
    pnl_cls  = "chip-green" if pnl >= 0 else "chip-red"
    pnl_sign = "+" if pnl >= 0 else ""

    trades_pct = min((budget["trades_today"] / max(budget["max_trades"], 1)) * 100, 100)

    st.markdown(f"""
    <div class="metric-card orange-accent">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
            <div style="color:#94a3b8;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;">Dagbudget</div>
            <span class="top-status-chip {pnl_cls}">{pnl_sign}€{abs(pnl):.2f} vandaag</span>
        </div>
        <div style="color:#ffffff;font-size:18px;font-weight:900;margin-bottom:2px;">
            {budget['trades_today']}/{budget['max_trades']} trades
        </div>
        <div style="color:#94a3b8;font-size:11px;margin-bottom:6px;">
            Verliesruimte: €{budget['verlies_abs']:.2f} / €{budget['budget_max']:.2f} gebruikt
        </div>
        <div class="budget-bar">
            <div class="budget-fill-safe" style="width:{min(trades_pct,100):.0f}%;background:linear-gradient(90deg,#ff8c00,#fbbf24);"></div>
        </div>
        <div style="color:#555;font-size:10px;margin-top:3px;">{trades_pct:.0f}% daglimiet gebruikt</div>
        <div class="budget-bar" style="margin-top:4px;">
            <div class="{fill_cls}" style="width:{pct:.0f}%;"></div>
        </div>
        <div style="color:#555;font-size:10px;margin-top:3px;">{pct:.0f}% verliesbudget gebruikt — nog €{budget['ruimte']:.2f} ruimte</div>
    </div>
    """, unsafe_allow_html=True)


# ============================================================
# FLOATING P&L PAGINA
# ============================================================
def render_open_positions_page() -> None:
    """Open posities met live floating P&L van Bitvavo."""
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">📂 Open Posities — Live Floating P&L</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-subtitle">'
        'Alle open live trades met actuele prijs van Bitvavo. '
        'Floating P&L = (huidige prijs - entry) × hoeveelheid. '
        'Ververst elke 30 seconden.'
        '</div>',
        unsafe_allow_html=True,
    )

    positions = get_floating_pnl()

    if not positions:
        st.info("Geen open posities. Bot wacht op nieuwe signalen van de scanner.")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    # Totale floating P&L
    total_float = sum(p["float_pnl"] for p in positions)
    total_invested = sum(p["amount_eur"] for p in positions)
    total_cls = "chip-green" if total_float >= 0 else "chip-red"
    sign = "+" if total_float >= 0 else ""

    m1, m2, m3 = st.columns(3, gap="small")
    with m1:
        st.markdown(metric_card("Totale Float P&L", f"{sign}€{abs(total_float):.4f}", f"{len(positions)} posities", "green" if total_float >= 0 else "red"), unsafe_allow_html=True)
    with m2:
        st.markdown(metric_card("Geïnvesteerd", f"€{total_invested:.2f}", "totaal open", "blue"), unsafe_allow_html=True)
    with m3:
        float_pct = (total_float / max(total_invested, 0.001)) * 100
        st.markdown(metric_card("Float %", f"{float_pct:+.2f}%", "rendement op geïnvesteerd", "orange" if abs(float_pct) < 2 else "green" if float_pct >= 0 else "red"), unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    for pos in positions:
        sym       = safe_str(pos["symbol"])
        entry     = safe_float(pos["entry"])
        current   = safe_float(pos["current"])
        float_pnl = safe_float(pos["float_pnl"])
        float_r   = safe_float(pos["float_r"])
        hold_min  = safe_float(pos["hold_min"])
        rr        = safe_float(pos["rr"])
        setup     = safe_str(pos["setup"])
        stop      = safe_float(pos["stop"])
        target    = safe_float(pos["target"])
        amount    = safe_float(pos["amount_eur"])

        is_profit = float_pnl >= 0
        card_cls  = "tc-win" if is_profit else "tc-loss"
        pnl_sign  = "+" if is_profit else ""
        pnl_color = "#34d399" if is_profit else "#fb7185"
        r_color   = "#34d399" if float_r >= 0 else "#fb7185"
        hold_str  = f"{int(hold_min//60)}h {int(hold_min%60)}m" if hold_min > 60 else f"{int(hold_min)}m"

        pnl_pct = (float_pnl / max(amount, 0.001)) * 100

        st.markdown(f"""
        <div class="tc {card_cls}">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
                <div>
                    <span style="color:#ffffff;font-size:14px;font-weight:900;">{sym}</span>
                    <span class="trade-chip" style="margin-left:8px;">{setup}</span>
                </div>
                <div style="text-align:right;">
                    <div style="color:{pnl_color};font-size:16px;font-weight:900;">{pnl_sign}€{abs(float_pnl):.4f}</div>
                    <div style="color:{r_color};font-size:12px;font-weight:700;">{float_r:+.2f} R | {pnl_pct:+.1f}%</div>
                </div>
            </div>
            <div style="display:flex;gap:16px;flex-wrap:wrap;">
                <div><span style="color:#555;font-size:10px;">ENTRY</span><br><span style="color:#fff;font-size:12px;font-weight:700;">{format_price(entry)}</span></div>
                <div><span style="color:#555;font-size:10px;">NU</span><br><span style="color:{pnl_color};font-size:12px;font-weight:700;">{format_price(current) if current > 0 else '?'}</span></div>
                <div><span style="color:#555;font-size:10px;">STOP</span><br><span style="color:#fb7185;font-size:12px;font-weight:700;">{format_price(stop)}</span></div>
                <div><span style="color:#555;font-size:10px;">TARGET</span><br><span style="color:#34d399;font-size:12px;font-weight:700;">{format_price(target)}</span></div>
                <div><span style="color:#555;font-size:10px;">R/R</span><br><span style="color:#60a5fa;font-size:12px;font-weight:700;">1:{rr:.1f}</span></div>
                <div><span style="color:#555;font-size:10px;">OPEN</span><br><span style="color:#94a3b8;font-size:12px;font-weight:700;">{hold_str}</span></div>
                <div><span style="color:#555;font-size:10px;">INZET</span><br><span style="color:#94a3b8;font-size:12px;font-weight:700;">€{amount:.2f}</span></div>
            </div>
        </div>
        """, unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.caption("💡 Floating P&L is niet gerealiseerd. Prijs ophalen mislukt als Bitvavo API niet beschikbaar is.")
    st.markdown("</div>", unsafe_allow_html=True)


# ============================================================
# ANALYSE PAGINA — drawdown, rolling WR, recovery, streaks
# ============================================================
def render_analyse_page(history_df: pd.DataFrame, real_df: pd.DataFrame) -> None:
    """Diepgaande analyse pagina met alle geavanceerde metrics."""
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">📉 Diepgaande Analyse</div>', unsafe_allow_html=True)

    # Recovery Factor
    rf_all  = get_recovery_factor(history_df)
    rf_real = get_recovery_factor(real_df)
    s_all   = perf_summary(history_df)
    s_real  = perf_summary(real_df)

    m1, m2, m3, m4, m5 = st.columns(5, gap="small")
    with m1:
        rf_acc = "green" if rf_all >= 2.0 else "orange" if rf_all >= 1.0 else "red"
        st.markdown(metric_card("Recovery Factor (all)", f"{rf_all:.2f}", "doel: >2.0", rf_acc), unsafe_allow_html=True)
    with m2:
        rf_acc2 = "green" if rf_real >= 2.0 else "orange" if rf_real >= 1.0 else "red"
        st.markdown(metric_card("Recovery Factor (live)", f"{rf_real:.2f}", "doel: >2.0", rf_acc2), unsafe_allow_html=True)
    with m3:
        st.markdown(metric_card("Max Drawdown (all)", f"{s_all['max_dd']:.2f} R", "", "red"), unsafe_allow_html=True)
    with m4:
        st.markdown(metric_card("Max Drawdown (live)", f"{s_real['max_dd']:.2f} R", "", "red"), unsafe_allow_html=True)
    with m5:
        st.markdown(metric_card("Expectancy", f"{s_all['expectancy']:.2f} R", "per trade gemiddeld", "purple"), unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    # Tabs
    tab_dd, tab_rolling, tab_streak, tab_hold, tab_hist, tab_freq = st.tabs([
        "📉 Drawdown", "📈 Rolling WR", "🔥 Streaks",
        "⏱️ Houdtijd", "📊 P&L Verdeling", "📅 Frequentie"
    ])

    with tab_dd:
        dc1, dc2 = st.columns(2, gap="small")
        with dc1:
            st.plotly_chart(chart_drawdown(history_df, "Drawdown Alle Trades"), use_container_width=True, config={"displayModeBar":False}, key="dd_all")
        with dc2:
            st.plotly_chart(chart_drawdown(real_df, "Drawdown Live Trades"), use_container_width=True, config={"displayModeBar":False}, key="dd_real")
        st.markdown("""
        <div class="trade-note">
            <b>Drawdown uitleg:</b> Een drawdown is een daling van de equity peak naar een nieuw dieptepunt.
            Een diepe langdurige drawdown is een teken dat de strategie tijdelijk niet werkt.
            Recovery Factor = totale R winst / max drawdown. Hoe hoger, hoe beter het systeem herstelt.
        </div>
        """, unsafe_allow_html=True)

    with tab_rolling:
        w_opt = st.slider("Window (trades)", 5, 50, 20, key="rolling_window")
        rc1, rc2 = st.columns(2, gap="small")
        with rc1:
            st.plotly_chart(chart_rolling_winrate(history_df, w_opt), use_container_width=True, config={"displayModeBar":False}, key="roll_all")
        with rc2:
            st.plotly_chart(chart_rolling_winrate(real_df, w_opt), use_container_width=True, config={"displayModeBar":False}, key="roll_real")
        st.markdown("""
        <div class="trade-note">
            <b>Rolling Win Rate:</b> Win rate berekend over de laatste N trades.
            Als de lijn daalt, presteren de meest recente trades slechter.
            Dit is een vroeg signaal van edge decay voordat het in de totalen zichtbaar wordt.
        </div>
        """, unsafe_allow_html=True)

    with tab_streak:
        streaks = get_streak_history(history_df)
        if streaks:
            win_streaks  = [s for s in streaks if s["type"] == "WIN"]
            loss_streaks = [s for s in streaks if s["type"] == "LOSS"]
            max_win  = max((s["length"] for s in win_streaks),  default=0)
            max_loss = max((s["length"] for s in loss_streaks), default=0)
            cons_now = get_consecutive_losses(real_df)

            sc1, sc2, sc3 = st.columns(3, gap="small")
            with sc1:
                st.markdown(metric_card("Huidige Streak", f"{cons_now}x {'🔴' if cons_now > 0 else '—'}", "verliezen op rij", "red" if cons_now >= 3 else "blue"), unsafe_allow_html=True)
            with sc2:
                st.markdown(metric_card("Langste Win Streak", f"{max_win}x 🟢", "ooit", "green"), unsafe_allow_html=True)
            with sc3:
                st.markdown(metric_card("Langste Loss Streak", f"{max_loss}x 🔴", "ooit", "red"), unsafe_allow_html=True)

            st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
            st.markdown("**Streak Geschiedenis (meest recent)**")
            st.markdown('<div class="streak-row">', unsafe_allow_html=True)
            dots_html = ""
            for s in streaks[-60:]:
                cls = "streak-dot-w" if s["type"] == "WIN" else "streak-dot-l"
                for _ in range(min(s["length"], 10)):
                    dots_html += f'<span class="{cls}"></span>'
            st.markdown(f'<div class="streak-row">{dots_html}</div>', unsafe_allow_html=True)
            st.markdown("</div>", unsafe_allow_html=True)
            st.caption("Groen = win, Rood = loss. Max 10 dots per streak getoond.")

            # Top 10 langste streaks
            top_streaks = sorted(streaks, key=lambda x: x["length"], reverse=True)[:10]
            st.markdown("**Top 10 langste streaks:**")
            for s in top_streaks:
                emoji = "🟢" if s["type"] == "WIN" else "🔴"
                st.markdown(
                    f'<div class="score-row">'
                    f'<div class="score-left">{emoji} {s["type"]} — {s["length"]} trades op rij</div>'
                    f'<div class="score-right">{format_dt(s["start"])} → {format_dt(s["end"])}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        else:
            st.info("Nog geen streak data beschikbaar.")

    with tab_hold:
        hc1, hc2 = st.columns(2, gap="small")
        with hc1:
            st.plotly_chart(chart_hold_time_bar(history_df), use_container_width=True, config={"displayModeBar":False}, key="hold_all")
        with hc2:
            hold_df = get_avg_hold_time(history_df)
            if not hold_df.empty:
                for _, row in hold_df.iterrows():
                    setup = safe_str(row.get("setup_type"))
                    hrs   = safe_float(row.get("avg_hold")) / 60
                    n     = safe_int(row.get("n"))
                    wr    = safe_float(row.get("win_rate"))
                    st.markdown(
                        f'<div class="score-row">'
                        f'<div class="score-left">{setup}</div>'
                        f'<div class="score-right">{hrs:.1f}u gem. | {wr:.1f}% WR | {n} trades</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

    with tab_hist:
        hh1, hh2 = st.columns(2, gap="small")
        with hh1:
            st.plotly_chart(chart_pnl_histogram(history_df), use_container_width=True, config={"displayModeBar":False}, key="hist_all")
        with hh2:
            st.plotly_chart(chart_pnl_histogram(real_df), use_container_width=True, config={"displayModeBar":False}, key="hist_real")

    with tab_freq:
        st.plotly_chart(chart_trade_frequency(history_df), use_container_width=True, config={"displayModeBar":False}, key="freq_all")

    st.markdown("</div>", unsafe_allow_html=True)


# ============================================================
# COINS PAGINA — blacklist, cooldown, whitelist, fees, best/worst
# ============================================================
def render_coins_page(history_df: pd.DataFrame, real_df: pd.DataFrame) -> None:
    """Complete coin analyse pagina."""
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">🪙 Coin Analyse & Filters</div>', unsafe_allow_html=True)

    coins_data = get_blacklist_cooldown_coins()
    fees       = get_fee_stats(real_df)
    best, worst = get_best_worst_trades(history_df, 5)

    # Fee metrics
    f1, f2, f3, f4 = st.columns(4, gap="small")
    with f1:
        st.markdown(metric_card("Totale Fees Betaald", f"€{fees['total_fees']:.4f}", "Bitvavo 0.25% per kant", "yellow"), unsafe_allow_html=True)
    with f2:
        st.markdown(metric_card("Fee Impact", f"{fees['fee_impact_pct']:.1f}%", "van bruto winst", "yellow"), unsafe_allow_html=True)
    with f3:
        st.markdown(metric_card("Gem. Fee / Trade", f"€{fees['avg_fee_per_trade']:.4f}", "", "yellow"), unsafe_allow_html=True)
    with f4:
        st.markdown(metric_card("Bruto Winst", f"€{fees['gross_profit']:.4f}", "voor fees", "green"), unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    tab_bl, tab_cd, tab_wl, tab_best, tab_corr = st.tabs([
        "⚫ Blacklist", "⏳ Cooldown", "🌟 Whitelist",
        "🏅 Beste/Slechtste", "📊 BTC Correlatie"
    ])

    with tab_bl:
        st.markdown(
            '<div class="section-subtitle">'
            'Coins met win rate <30% na 20+ trades. '
            'Worden automatisch overgeslagen door de scanner (multi_coin_score.py).'
            '</div>',
            unsafe_allow_html=True,
        )
        blacklist = coins_data.get("blacklist", [])
        if blacklist:
            for coin in blacklist:
                name = safe_str(coin.get("coin"))
                n    = safe_int(coin.get("n"))
                wr   = safe_float(coin.get("win_rate"))
                wins = safe_int(coin.get("wins"))
                st.markdown(f"""
                <div class="coin-row coin-row-black">
                    <div>
                        <div class="coin-name">⚫ {name}</div>
                        <div class="coin-stats">Win rate: {wr:.1f}% | {wins} wins / {n-wins} losses / {n} trades</div>
                    </div>
                    <span class="top-status-chip chip-red">GEBLOKKEERD</span>
                </div>
                """, unsafe_allow_html=True)
        else:
            st.success("✅ Geen coins op de blacklist. Alle coins presteren acceptabel.")

    with tab_cd:
        st.markdown(
            '<div class="section-subtitle">'
            '24u cooldown na verlies. Worden tijdelijk overgeslagen door de scanner.'
            '</div>',
            unsafe_allow_html=True,
        )
        cooldown = coins_data.get("cooldown", [])
        if cooldown:
            for coin in cooldown:
                name  = safe_str(coin.get("coin"))
                hours = safe_float(coin.get("hours_since"))
                remaining = max(24.0 - hours, 0.0)
                st.markdown(f"""
                <div class="coin-row coin-row-cool">
                    <div>
                        <div class="coin-name">⏳ {name}</div>
                        <div class="coin-stats">Verlies {hours:.1f}u geleden — nog {remaining:.1f}u cooldown</div>
                    </div>
                    <span class="top-status-chip chip-yellow">COOLDOWN</span>
                </div>
                """, unsafe_allow_html=True)
        else:
            st.success("✅ Geen coins in cooldown momenteel.")

    with tab_wl:
        st.markdown(
            '<div class="section-subtitle">'
            'Coins met win rate >60% na 20+ trades. '
            'Hoge prioriteit in de scanner — hogere betrouwbaarheid.'
            '</div>',
            unsafe_allow_html=True,
        )
        whitelist = coins_data.get("whitelist", [])
        if whitelist:
            for coin in whitelist:
                name    = safe_str(coin.get("coin"))
                n       = safe_int(coin.get("n"))
                wr      = safe_float(coin.get("win_rate"))
                avg_pnl = safe_float(coin.get("avg_pnl"))
                st.markdown(f"""
                <div class="coin-row coin-row-white">
                    <div>
                        <div class="coin-name">🌟 {name}</div>
                        <div class="coin-stats">Win rate: {wr:.1f}% | {n} trades | Gem. R: {avg_pnl:+.3f}R</div>
                    </div>
                    <span class="top-status-chip chip-green">TOPPERFORMER</span>
                </div>
                """, unsafe_allow_html=True)
        else:
            st.info("Nog geen coins met >60% win rate na 20+ trades.")

    with tab_best:
        bb1, bb2 = st.columns(2, gap="small")
        with bb1:
            st.markdown("#### 🏆 Top 5 Beste Trades")
            if not best.empty:
                for _, row in best.iterrows():
                    sym   = safe_str(row.get("symbol"))
                    r     = safe_float(row.get("pnl_r"))
                    setup = safe_str(row.get("setup_type"))
                    dt    = safe_str(row.get("datetime"))
                    src   = safe_str(row.get("trade_type","")).upper()
                    is_real = src in ("REAL","LIVE")
                    eur   = safe_float(row.get("pnl_eur"))
                    eur_deel = f" / {format_money(eur)}" if is_real and abs(eur) > 0.0001 else ""
                    st.markdown(f"""
                    <div class="top-trade-card">
                        <div>
                            <div class="top-trade-left">{sym} — {setup}</div>
                            <div class="top-trade-sub">{dt} | {src}</div>
                        </div>
                        <div class="top-trade-right float-green">+{r:.2f} R{eur_deel}</div>
                    </div>
                    """, unsafe_allow_html=True)
            else:
                st.info("Geen data.")

        with bb2:
            st.markdown("#### 💀 Top 5 Slechtste Trades")
            if not worst.empty:
                for _, row in worst.iterrows():
                    sym   = safe_str(row.get("symbol"))
                    r     = safe_float(row.get("pnl_r"))
                    setup = safe_str(row.get("setup_type"))
                    dt    = safe_str(row.get("datetime"))
                    src   = safe_str(row.get("trade_type","")).upper()
                    is_real = src in ("REAL","LIVE")
                    eur   = safe_float(row.get("pnl_eur"))
                    eur_deel = f" / {format_money(eur)}" if is_real and abs(eur) > 0.0001 else ""
                    st.markdown(f"""
                    <div class="top-trade-card">
                        <div>
                            <div class="top-trade-left">{sym} — {setup}</div>
                            <div class="top-trade-sub">{dt} | {src}</div>
                        </div>
                        <div class="top-trade-right float-red">{r:.2f} R{eur_deel}</div>
                    </div>
                    """, unsafe_allow_html=True)
            else:
                st.info("Geen data.")

    with tab_corr:
        st.markdown(
            '<div class="section-subtitle">'
            'Win rate per marktregime. Laat zien hoe de bot presteert in verschillende marktomstandigheden.'
            '</div>',
            unsafe_allow_html=True,
        )
        corr_df = get_btc_correlation(history_df)
        cc1, cc2 = st.columns(2, gap="small")
        with cc1:
            st.plotly_chart(chart_btc_correlation_bar(history_df), use_container_width=True, config={"displayModeBar":False}, key="corr_chart")
        with cc2:
            if not corr_df.empty:
                color_map = {"BULL":"#34d399","BEAR":"#fb7185","RANGE":"#fbbf24"}
                for _, row in corr_df.iterrows():
                    regime  = safe_str(row.get("regime")).upper()
                    wr      = safe_float(row.get("win_rate"))
                    n       = safe_int(row.get("n"))
                    avg_r   = safe_float(row.get("avg_r"))
                    bar_w   = min(wr, 100)
                    bar_col = color_map.get(regime, "#60a5fa")
                    st.markdown(f"""
                    <div class="corr-row">
                        <div class="corr-label">{regime}</div>
                        <div class="corr-bar-wrap">
                            <div class="corr-bar-fill" style="width:{bar_w}%;background:{bar_col};"></div>
                        </div>
                        <div class="corr-pct">{wr:.1f}%</div>
                        <div class="corr-count">{n} trades / gem. {avg_r:+.2f}R</div>
                    </div>
                    """, unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)


# ============================================================
# KALENDER PAGINA — P&L heatmap per maand
# ============================================================
def render_kalender_page(history_df: pd.DataFrame) -> None:
    """Kalender P&L heatmap — visuele consistentie check."""
    render_alarm_banner()
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">📅 P&L Kalender</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-subtitle">'
        'Win/verlies per dag. Groen = winstdag, rood = verliesdag. '
        'Intensiteit = hoe groot de dag. Hover over een dag voor details.'
        '</div>',
        unsafe_allow_html=True,
    )

    import calendar as cal_module

    # ── MAAND SELECTIE ────────────────────────────────────────
    mc1, mc2, mc3 = st.columns([1, 1, 2], gap="small")
    now = now_utc()
    with mc1:
        sel_year  = st.selectbox("Jaar", list(range(2024, now.year + 2)),
                                 index=now.year - 2024, key="cal_year")
    with mc2:
        sel_month = st.selectbox("Maand", list(range(1, 13)), index=now.month - 1,
                                 format_func=lambda m: cal_module.month_name[m], key="cal_month")
    with mc3:
        # Alleen REAL trades voor kalender (omdat alleen die echt geld zijn)
        cal_filter = st.radio(
            "Trades",
            ["Alleen REAL", "Alles"],
            horizontal=True,
            key="cal_filter",
        )

    if cal_filter == "Alleen REAL" and not history_df.empty and "trade_type" in history_df.columns:
        cal_df = history_df[history_df["trade_type"].str.upper().isin(["REAL","LIVE"])]
    else:
        cal_df = history_df

    # P&L per dag op basis van R (altijd vergelijkbaar, ongeacht inzet)
    pnl_data_r: Dict[str, float]   = {}
    pnl_data_eur: Dict[str, float] = {}
    count_per_dag: Dict[str, int]  = {}
    wins_per_dag: Dict[str, int]   = {}

    if not cal_df.empty:
        ts_col = next((c for c in ["exit_time","closed_at","updated_at","created_at","datetime"] if c in cal_df.columns), None)
        if ts_col:
            tmp = cal_df.copy()
            tmp["_dag"] = pd.to_datetime(tmp[ts_col], errors="coerce").dt.strftime("%Y-%m-%d")
            tmp["_r"]   = pd.to_numeric(tmp.get("pnl_r", 0), errors="coerce").fillna(0)
            tmp["_eur"] = pd.to_numeric(tmp.get("pnl_eur", 0), errors="coerce").fillna(0)
            tmp["_win"] = tmp.get("outcome","").str.upper().isin(["WIN","1"])
            grp = tmp.dropna(subset=["_dag"]).groupby("_dag")
            pnl_data_r   = grp["_r"].sum().to_dict()
            pnl_data_eur = grp["_eur"].sum().to_dict()
            count_per_dag = grp["_r"].count().to_dict()
            wins_per_dag  = grp["_win"].sum().to_dict()

    pnl_data = pnl_data_r  # Kalender toont altijd R

    # ── KALENDER GRID ─────────────────────────────────────────
    days_in_month = cal_module.monthrange(sel_year, sel_month)[1]
    first_weekday = cal_module.monthrange(sel_year, sel_month)[0]

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    dag_namen = ["Ma", "Di", "Wo", "Do", "Vr", "Za", "Zo"]
    header_html = "".join(f'<div class="cal-header">{d}</div>' for d in dag_namen)
    st.markdown(f'<div class="cal-grid">{header_html}</div>', unsafe_allow_html=True)

    cells = []
    for _ in range(first_weekday):
        cells.append('<div class="cal-day cal-empty"></div>')

    month_wins   = 0
    month_losses = 0
    month_r      = 0.0
    month_eur    = 0.0
    month_trades = 0

    for day_num in range(1, days_in_month + 1):
        dag_str   = f"{sel_year}-{sel_month:02d}-{day_num:02d}"
        pnl_val   = pnl_data.get(dag_str)
        eur_val   = pnl_data_eur.get(dag_str, 0)
        n_trades  = int(count_per_dag.get(dag_str, 0))
        n_wins    = int(wins_per_dag.get(dag_str, 0))
        is_vandaag = dag_str == now.strftime("%Y-%m-%d")
        vandaag_border = "border:2px solid #f39c12;" if is_vandaag else ""

        if pnl_val is None or n_trades == 0:
            cells.append(f'<div class="cal-day cal-flat" style="{vandaag_border}">{day_num}</div>')
        elif pnl_val > 0:
            intensity = min(int(abs(pnl_val) * 30 + 15), 75)
            tooltip   = f"+{pnl_val:.2f}R | {n_wins}/{n_trades} wins"
            cells.append(
                f'<div class="cal-day cal-win" '
                f'style="background:rgba(52,211,153,0.{intensity:02d});{vandaag_border}" '
                f'title="{tooltip}">'
                f'{day_num}'
                f'<div style="font-size:9px;color:rgba(52,211,153,0.9);">+{pnl_val:.1f}R</div>'
                f'</div>'
            )
            month_wins += 1
            month_r    += pnl_val
            month_eur  += eur_val
            month_trades += n_trades
        else:
            intensity = min(int(abs(pnl_val) * 30 + 15), 75)
            tooltip   = f"{pnl_val:.2f}R | {n_wins}/{n_trades} wins"
            cells.append(
                f'<div class="cal-day cal-loss" '
                f'style="background:rgba(239,68,68,0.{intensity:02d});{vandaag_border}" '
                f'title="{tooltip}">'
                f'{day_num}'
                f'<div style="font-size:9px;color:rgba(239,68,68,0.9);">{pnl_val:.1f}R</div>'
                f'</div>'
            )
            month_losses += 1
            month_r      += pnl_val
            month_eur    += eur_val
            month_trades += n_trades

    grid_html = '<div class="cal-grid">' + "".join(cells) + '</div>'
    st.markdown(grid_html, unsafe_allow_html=True)

    # ── MAAND SAMENVATTING ────────────────────────────────────
    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    active_days  = month_wins + month_losses
    win_day_pct  = (month_wins / max(active_days, 1)) * 100
    r_teken      = "+" if month_r >= 0 else ""

    ms1, ms2, ms3, ms4, ms5 = st.columns(5, gap="small")
    with ms1:
        st.markdown(metric_card("Winstdagen", str(month_wins),
                                f"{win_day_pct:.1f}% van aktieve dagen", "green"), unsafe_allow_html=True)
    with ms2:
        st.markdown(metric_card("Verliessdagen", str(month_losses),
                                f"{100-win_day_pct:.1f}%", "red"), unsafe_allow_html=True)
    with ms3:
        st.markdown(metric_card("Maand R", f"{r_teken}{month_r:.2f}R",
                                "", "green" if month_r >= 0 else "red"), unsafe_allow_html=True)
    with ms4:
        # EUR alleen tonen als er echte trades zijn (filter = REAL)
        if cal_filter == "Alleen REAL" and abs(month_eur) > 0.0001:
            eur_teken = "+" if month_eur >= 0 else ""
            st.markdown(metric_card("Maand EUR", f"{eur_teken}€{month_eur:.4f}",
                                    "echt geld", "green" if month_eur >= 0 else "red"), unsafe_allow_html=True)
        else:
            st.markdown(metric_card("Actieve Dagen", str(active_days),
                                    f"van {days_in_month} dagen", "blue"), unsafe_allow_html=True)
    with ms5:
        st.markdown(metric_card("Trades", str(month_trades),
                                f"gem. {month_trades/max(active_days,1):.1f}/dag", "blue"), unsafe_allow_html=True)

    # ── 30-DAAGSE R TREND ─────────────────────────────────────
    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown("#### 📊 Dagelijkse R — 30 dagen")
    st.plotly_chart(chart_daily_r(cal_df, "Dagelijkse R (alle types)"),
                    use_container_width=True, config={"displayModeBar":False}, key="cal_daily_r")

    # ── BESTE EN SLECHTSTE DAGEN ──────────────────────────────
    if pnl_data_r:
        sorted_dagen = sorted(pnl_data_r.items(), key=lambda x: x[1], reverse=True)
        top3  = [(d, r) for d, r in sorted_dagen if r > 0][:3]
        bot3  = [(d, r) for d, r in sorted_dagen if r < 0][-3:]

        if top3 or bot3:
            bc1, bc2 = st.columns(2, gap="small")
            with bc1:
                st.markdown("**🏆 Beste dagen deze maand**")
                for dag, r in top3:
                    n = int(count_per_dag.get(dag, 0))
                    st.markdown(
                        f'<div style="padding:4px 0;border-bottom:1px solid #222;font-size:13px;">'
                        f'<b style="color:#2ecc71;">{dag}</b> — '
                        f'<span style="color:#f39c12;">+{r:.2f}R</span> | {n} trades'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
            with bc2:
                st.markdown("**💀 Slechtste dagen deze maand**")
                for dag, r in reversed(bot3):
                    n = int(count_per_dag.get(dag, 0))
                    st.markdown(
                        f'<div style="padding:4px 0;border-bottom:1px solid #222;font-size:13px;">'
                        f'<b style="color:#e74c3c;">{dag}</b> — '
                        f'<span style="color:#e74c3c;">{r:.2f}R</span> | {n} trades'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

    st.markdown("</div>", unsafe_allow_html=True)


# ============================================================
# CORRELATIE PAGINA — BTC correlatie + markt analyse
# ============================================================
def render_correlatie_page(history_df: pd.DataFrame, real_df: pd.DataFrame) -> None:
    """BTC Correlatie + edge decay + markt analyse."""
    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">📊 BTC Correlatie & Edge Decay</div>', unsafe_allow_html=True)

    # Edge decay
    s_all  = perf_summary(history_df)
    s_real = perf_summary(real_df)
    sim_wr  = safe_float(perf_summary(history_df[history_df["trade_type"] == "SIM"] if not history_df.empty else pd.DataFrame()).get("winrate"))
    live_wr = safe_float(s_real.get("winrate"))
    diff    = sim_wr - live_wr

    ed1, ed2, ed3, ed4 = st.columns(4, gap="small")
    with ed1:
        st.markdown(metric_card("Sim Win Rate", f"{sim_wr:.1f}%", "referentie (geen fees)", "purple"), unsafe_allow_html=True)
    with ed2:
        st.markdown(metric_card("Live Win Rate", f"{live_wr:.1f}%", f"{diff:+.1f}% vs sim", "orange" if abs(diff) < 5 else "red" if diff > 10 else "yellow"), unsafe_allow_html=True)
    with ed3:
        st.markdown(metric_card("Edge Decay", f"{diff:.1f}%", "grens: 10%", "green" if diff <= 5 else "yellow" if diff <= 10 else "red"), unsafe_allow_html=True)
    with ed4:
        rf = get_recovery_factor(real_df)
        st.markdown(metric_card("Recovery Factor", f"{rf:.2f}", "doel: >2.0", "green" if rf >= 2.0 else "orange" if rf >= 1.0 else "red"), unsafe_allow_html=True)

    if diff > 10:
        st.warning(
            f"⚠️ **Edge decay gedetecteerd** — verschil {diff:.1f}%\n\n"
            f"**Mogelijke oorzaken:** Marktverandering | Fees niet in sim | Score drempel te laag\n\n"
            f"**Actie:** Verhoog MIN_SCORE_TO_TRADE | Stuur HEALTH via WhatsApp"
        )
    elif diff > 5:
        st.warning(f"⚠️ Lichte edge decay ({diff:.1f}%) — blijf monitoren.")
    else:
        st.success(f"✅ Geen significante edge decay — strategie presteert consistent live.")

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    # BTC correlatie grafieken
    cc1, cc2 = st.columns(2, gap="small")
    with cc1:
        st.plotly_chart(chart_btc_correlation_bar(history_df), use_container_width=True, config={"displayModeBar":False}, key="corr_all_2")
        st.markdown("""
        <div class="trade-note">
            <b>BTC Correlatie uitleg:</b> Als BULL regime veel hogere win rate heeft dan BEAR,
            is de bot sterk afhankelijk van BTC richting. Dit is normaal — maar betekent dat je
            in BEAR markt minder of geen trades moet doen (vandaar BTC_SKIP_BEAR=True).
        </div>
        """, unsafe_allow_html=True)

    with cc2:
        corr_df = get_btc_correlation(history_df)
        if not corr_df.empty:
            color_map = {"BULL":"#34d399","BEAR":"#fb7185","RANGE":"#fbbf24"}
            st.markdown('<div class="section-title" style="font-size:15px;">Win Rate per Regime</div>', unsafe_allow_html=True)
            for _, row in corr_df.iterrows():
                regime = safe_str(row.get("regime")).upper()
                wr     = safe_float(row.get("win_rate"))
                n      = safe_int(row.get("n"))
                avg_r  = safe_float(row.get("avg_r"))
                bar_w  = min(wr, 100)
                bar_c  = color_map.get(regime, "#60a5fa")
                emoji  = {"BULL":"🟢","BEAR":"🔴","RANGE":"🟡"}.get(regime,"⚪")
                st.markdown(f"""
                <div class="corr-row">
                    <div class="corr-label">{emoji} {regime}</div>
                    <div class="corr-bar-wrap">
                        <div class="corr-bar-fill" style="width:{bar_w}%;background:{bar_c};"></div>
                    </div>
                    <div class="corr-pct" style="color:{bar_c};">{wr:.1f}%</div>
                    <div class="corr-count">{n} trades / {avg_r:+.2f}R</div>
                </div>
                """, unsafe_allow_html=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    # Rolling win rate vergelijking
    rc1, rc2 = st.columns(2, gap="small")
    with rc1:
        st.plotly_chart(chart_rolling_winrate(history_df, 20), use_container_width=True, config={"displayModeBar":False}, key="corr_roll_all")
    with rc2:
        st.plotly_chart(chart_rolling_winrate(real_df, 10), use_container_width=True, config={"displayModeBar":False}, key="corr_roll_real")

    # Claude correlatie analyse
    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    claude_btn(
        "Claude: edge decay analyse",
        f"""
Je bent een crypto edge decay specialist.
Analyseer in 4 zinnen Nederlands.

DATA:
- Sim win rate:   {sim_wr:.1f}%
- Live win rate:  {live_wr:.1f}%
- Verschil:       {diff:.1f}%
- Recovery Factor: {get_recovery_factor(real_df):.2f}

1. Is er edge decay?
2. Meest waarschijnlijke oorzaken?
3. Aanbevolen actie?
4. Urgentie?
""", 250, "corr_claude"
    )

    st.markdown("</div>", unsafe_allow_html=True)


# ============================================================
# AI COACH CHAT — live conversatie met de bot coach
# ============================================================
def get_live_context(history_df: pd.DataFrame, real_df: pd.DataFrame) -> Dict[str, Any]:
    """Bouwt live data context op voor de AI Coach chat."""
    try:
        s_all  = perf_summary(history_df)
        s_real = perf_summary(real_df)
        btc    = get_btc_regime()
        bl_status, _, _ = get_bot_status()
        pnl_dag = get_daily_pnl_today(real_df)
        cons    = get_consecutive_losses(real_df)
        pf30    = get_profit_factor_30d(real_df)

        laatste_trades = []
        if not real_df.empty:
            work = real_df[real_df["outcome"].isin(["WIN","LOSS"])].head(5)
            for _, r in work.iterrows():
                laatste_trades.append(
                    f"{r.get('outcome','?')} {r.get('symbol','?')} "
                    f"{format_r(r.get('pnl_r',0))} "
                    f"({r.get('setup_type','?')} / {r.get('regime','?')})"
                )

        return {
            "bot_status":       bl_status,
            "btc_regime":       safe_str(btc.get("regime"), "UNKNOWN"),
            "btc_strength":     safe_float(btc.get("strength")),
            "btc_prijs":        safe_float(btc.get("close")),
            "pnl_vandaag":      round(pnl_dag, 4),
            "cons_losses":      cons,
            "profit_factor_30": pf30,
            "all_trades":       safe_int(s_all.get("count")),
            "all_winrate":      round(safe_float(s_all.get("winrate")), 1),
            "all_total_r":      round(safe_float(s_all.get("total_r")), 2),
            "real_trades":      safe_int(s_real.get("count")),
            "real_winrate":     round(safe_float(s_real.get("winrate")), 1),
            "real_pnl_eur":     round(safe_float(s_real.get("total_eur")), 4),
            "real_expectancy":  round(safe_float(s_real.get("expectancy")), 3),
            "real_max_dd":      round(safe_float(s_real.get("max_dd")), 2),
            "laatste_trades":   laatste_trades,
            "min_score":        MIN_SCORE_TO_TRADE,
            "max_per_trade":    MAX_PER_TRADE_EUR,
            "trading_uren":     f"{TRADING_HOURS_START}:00-{TRADING_HOURS_END}:00 UTC",
            "atr_multiplier":   ATR_MULTIPLIER,
        }
    except Exception as e:
        log_debug(f"get_live_context fout: {e}")
        return {}


def coach_antwoord(vraag: str, context: Dict[str, Any], history: List[Dict]) -> str:
    """Stuurt vraag + live context naar Claude en geeft antwoord terug."""
    if not ANTHROPIC_API_KEY:
        return "❌ ANTHROPIC_API_KEY niet ingesteld in Render Environment Variables."

    laatste_trades_txt = "\n".join(
        f"  • {t}" for t in context.get("laatste_trades", [])
    ) or "  Geen recente trades"

    system_prompt = f"""Je bent de AI Coach van een automatische cryptocurrency trading bot.
Je hebt LIVE toegang tot alle bot data en kan vragen beantwoorden en concrete adviezen geven.

ACTUELE BOT DATA (nu geladen):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Bot status:        {context.get('bot_status', '?')}
BTC regime:        {context.get('btc_regime', '?')} ({context.get('btc_strength', 0):.0f}% sterkte)
BTC prijs:         €{context.get('btc_prijs', 0):,.0f}

PERFORMANCE:
Win rate (live):   {context.get('real_winrate', 0):.1f}% ({context.get('real_trades', 0)} trades)
Win rate (alles):  {context.get('all_winrate', 0):.1f}% ({context.get('all_trades', 0)} trades)
PnL vandaag:       €{context.get('pnl_vandaag', 0):.4f}
PnL totaal live:   €{context.get('real_pnl_eur', 0):.4f}
Profit Factor 30d: {context.get('profit_factor_30', 0):.2f}
Expectancy:        {context.get('real_expectancy', 0):.3f} R per trade
Max drawdown:      {context.get('real_max_dd', 0):.2f} R
Verlies streak:    {context.get('cons_losses', 0)}x op rij

LAATSTE 5 TRADES:
{laatste_trades_txt}

HUIDIGE INSTELLINGEN:
Min score:         {context.get('min_score', 85)}
Max per trade:     €{context.get('max_per_trade', 0.50):.2f}
Trading uren:      {context.get('trading_uren', '08:00-22:00 UTC')}
ATR multiplier:    {context.get('atr_multiplier', 2.0)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INSTRUCTIES:
- Spreek altijd in het Nederlands
- Wees direct en concreet
- Geef altijd een concrete ACTIE aanbeveling
- Als er een probleem is zeg PRECIES wat aanpassen inclusief de nieuwe waarde
- Gebruik de live data hierboven bij elk antwoord
- Schrijf parameter namen exact: MIN_SCORE_TO_TRADE, ATR_MULTIPLIER, etc.""".strip()

    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in history[-10:]
        if m.get("role") in ("user", "assistant")
    ]
    messages.append({"role": "user", "content": vraag})

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": 600,
                "system":     system_prompt,
                "messages":   messages,
            },
            timeout=30,
        )
        if resp.status_code == 200:
            content = resp.json().get("content", [])
            if content:
                return content[0]["text"].strip()
        return f"❌ API fout {resp.status_code} — probeer opnieuw."
    except requests.exceptions.Timeout:
        return "⏱️ Timeout — probeer een kortere vraag."
    except Exception as e:
        return f"❌ Fout: {type(e).__name__}"


def render_coach_chat_page(history_df: pd.DataFrame, real_df: pd.DataFrame) -> None:
    """AI Coach Chat pagina — live conversatie met volledige bot context."""
    import html as html_lib
    import re

    st.markdown('<div class="panel">', unsafe_allow_html=True)
    st.markdown('<div class="section-title">🤖 AI Coach Chat</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="section-subtitle">'
        'Direct chatten met je AI Coach. Hij heeft live toegang tot alle bot data '
        'en geeft concrete adviezen en aanpassingen.'
        '</div>',
        unsafe_allow_html=True,
    )

    if not ANTHROPIC_API_KEY:
        st.error(
            "❌ ANTHROPIC_API_KEY niet ingesteld.\n\n"
            "Ga naar: Render Dashboard → crypto-ai-dashboard → "
            "Environment → voeg ANTHROPIC_API_KEY toe."
        )
        st.markdown("</div>", unsafe_allow_html=True)
        return

    # Live context laden
    context = get_live_context(history_df, real_df)
    st.session_state.coach_context = context

    # Context samenvatting bovenaan
    pnl_kleur  = "#34d399" if context.get("pnl_vandaag", 0) >= 0 else "#fb7185"
    pnl_sign   = "+" if context.get("pnl_vandaag", 0) >= 0 else ""
    pf_kleur   = "#34d399" if context.get("profit_factor_30", 0) >= 1.5 else "#fb7185"
    str_kleur  = "#fb7185" if context.get("cons_losses", 0) >= 3 else "#ffffff"

    st.markdown(f"""
    <div class="chat-context-card">
        <div class="chat-context-title">⚡ Live Bot Data — automatisch geladen</div>
        <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:12px;">
            <span>Bot: <b style="color:#fff">{context.get('bot_status','?')}</b></span>
            <span>BTC: <b style="color:#fff">{context.get('btc_regime','?')}</b></span>
            <span>Win rate: <b style="color:#34d399">{context.get('real_winrate',0):.1f}%</b> live</span>
            <span>PnL vandaag: <b style="color:{pnl_kleur}">{pnl_sign}€{abs(context.get('pnl_vandaag',0)):.4f}</b></span>
            <span>PF 30d: <b style="color:{pf_kleur}">{context.get('profit_factor_30',0):.2f}</b></span>
            <span>Streak: <b style="color:{str_kleur}">{context.get('cons_losses',0)}x verlies</b></span>
            <span>Trades: <b style="color:#fff">{context.get('real_trades',0)}</b> live</span>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Suggesties bij leeg gesprek
    if not st.session_state.coach_messages:
        st.markdown(
            '<div style="color:#555;font-size:11px;margin-bottom:6px;">'
            '💡 Klik een vraag of typ hieronder:'
            '</div>',
            unsafe_allow_html=True,
        )
        suggesties = [
            "Hoe gaat het met de bot?",
            "Wat is mijn slechtste setup?",
            "Waarom verlies ik geld?",
            "Welke parameters aanpassen?",
            "Analyseer mijn laatste trades",
            "Is mijn profit factor goed?",
            "Wat moet ik verbeteren?",
            "Hoe win rate verhogen?",
        ]
        sc1, sc2, sc3, sc4 = st.columns(4, gap="small")
        for i, sug in enumerate(suggesties):
            col = [sc1, sc2, sc3, sc4][i % 4]
            with col:
                st.markdown('<div class="tiny-button">', unsafe_allow_html=True)
                if st.button(sug, key=f"sug_{i}", use_container_width=True):
                    st.session_state.coach_messages.append({
                        "role": "user", "content": sug,
                        "tijd": now_utc().strftime("%H:%M"),
                    })
                    with st.spinner("🤖 Coach analyseert..."):
                        ant = coach_antwoord(sug, context, [])
                    st.session_state.coach_messages.append({
                        "role": "assistant", "content": ant,
                        "tijd": now_utc().strftime("%H:%M"),
                    })
                    st.rerun()
                st.markdown("</div>", unsafe_allow_html=True)

    # Chat berichten weergeven
    if st.session_state.coach_messages:
        chat_html = '<div class="chat-container">'
        for msg in st.session_state.coach_messages:
            role    = msg.get("role", "user")
            content = html_lib.escape(msg.get("content", "")).replace("\n", "<br>")
            tijd    = msg.get("tijd", "")
            if role == "user":
                chat_html += f"""
                <div class="chat-msg-user">
                    <div>
                        <div class="chat-bubble-user">{content}</div>
                        <div class="chat-time">{tijd}</div>
                    </div>
                </div>"""
            else:
                chat_html += f"""
                <div class="chat-msg-coach">
                    <div class="chat-avatar-coach">🤖</div>
                    <div>
                        <div class="chat-bubble-coach">{content}</div>
                        <div class="chat-time">{tijd}</div>
                    </div>
                </div>"""
        chat_html += "</div>"
        st.markdown(chat_html, unsafe_allow_html=True)
    else:
        st.markdown("""
        <div class="chat-container">
            <div class="chat-empty">
                🤖 AI Coach staat klaar<br>
                <span style="font-size:11px;color:#444;">Typ je vraag hieronder of klik een suggestie.</span>
            </div>
        </div>""", unsafe_allow_html=True)

    # Chat input
    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    inp_col, btn_col, wis_col = st.columns([4, 0.8, 0.8], gap="small")
    with inp_col:
        vraag = st.text_input(
            "Vraag", placeholder="Stel een vraag aan de AI Coach...",
            label_visibility="collapsed", key="coach_input",
        )
    with btn_col:
        stuur = st.button("📤 Stuur", use_container_width=True, key="coach_stuur")
    with wis_col:
        if st.button("🗑️ Wis", use_container_width=True, key="coach_wis"):
            st.session_state.coach_messages = []
            st.rerun()

    # Verwerk vraag
    if stuur and vraag and vraag.strip():
        st.session_state.coach_messages.append({
            "role": "user", "content": vraag.strip(),
            "tijd": now_utc().strftime("%H:%M"),
        })
        with st.spinner("🤖 Coach analyseert jouw data..."):
            hist = [
                {"role": m["role"], "content": m["content"]}
                for m in st.session_state.coach_messages[:-1]
                if m.get("role") in ("user", "assistant")
            ]
            ant = coach_antwoord(vraag.strip(), context, hist)
        st.session_state.coach_messages.append({
            "role": "assistant", "content": ant,
            "tijd": now_utc().strftime("%H:%M"),
        })
        st.rerun()

    # Aanpassingen toepassen knop detectie
    if st.session_state.coach_messages:
        laatste = st.session_state.coach_messages[-1]
        if laatste.get("role") == "assistant":
            content_low = laatste.get("content", "").lower()
            aanpassingen = []

            score_m = re.search(r'min_score_to_trade.*?(\d{2,3})', content_low)
            if score_m:
                ns = safe_int(score_m.group(1))
                if 70 <= ns <= 100 and ns != MIN_SCORE_TO_TRADE:
                    aanpassingen.append({"label": f"✅ MIN_SCORE_TO_TRADE: {MIN_SCORE_TO_TRADE} → {ns}", "key": "min_score_to_trade", "value": str(ns)})

            atr_m = re.search(r'atr_multiplier.*?(\d+\.?\d*)', content_low)
            if atr_m:
                na = safe_float(atr_m.group(1))
                if 1.0 <= na <= 5.0 and abs(na - ATR_MULTIPLIER) > 0.05:
                    aanpassingen.append({"label": f"✅ ATR_MULTIPLIER: {ATR_MULTIPLIER} → {na}", "key": "atr_multiplier", "value": str(na)})

            if aanpassingen:
                st.markdown('<div class="chat-action-card">🔧 Coach adviseert — klik om direct toe te passen:</div>', unsafe_allow_html=True)
                for aanp in aanpassingen:
                    if st.button(aanp["label"], key=f"apply_{aanp['key']}", use_container_width=True):
                        conn = get_db_conn()
                        if conn:
                            try:
                                with conn.cursor() as cur:
                                    cur.execute(
                                        f"INSERT INTO public.bot_state(key,value,updated_at) VALUES(%s,%s,NOW()) "
                                        f"ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=NOW()",
                                        (aanp["key"], aanp["value"])
                                    )
                                conn.commit()
                                conn.close()
                                st.success(f"✅ Opgeslagen! Bot gebruikt dit bij volgende scan.")
                                st.session_state.coach_messages.append({
                                    "role": "assistant",
                                    "content": f"✅ Aanpassing doorgevoerd: {aanp['key']} = {aanp['value']}\nBot gebruikt dit bij de volgende scan.",
                                    "tijd": now_utc().strftime("%H:%M"),
                                })
                                st.rerun()
                            except Exception as e:
                                st.error(f"❌ Opslaan mislukt: {e}")

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
    st.markdown(
        '<div style="color:#444;font-size:11px;">💡 De coach heeft alle live data — je hoeft niets uit te leggen. '
        'Aanpassingen via knoppen werken direct. Permanente aanpassingen: Render → Environment Variables.</div>',
        unsafe_allow_html=True,
    )
    st.markdown("</div>", unsafe_allow_html=True)



# ============================================================
# HOOFD LAYOUT — lazy loading per pagina
# ============================================================
# Oorzaak 502: alle 57.000 trades werden bij elke page refresh
# tegelijk geladen → geheugen overflow → Render kill → 502.
#
# Fix: alleen de data laden die de huidige pagina nodig heeft.
# Elke pagina laadt zijn eigen data via gecachte functies.
# De top balk gebruikt alleen lichtgewicht queries.
# ============================================================

st.markdown('<div class="shell">', unsafe_allow_html=True)

# ── TOP BALK — alleen lichtgewicht data ───────────────────────
# Geen zware trade queries hier — alleen status en BTC regime.
bot_label, bot_emoji, bot_type = get_bot_status()
btc_regime_data = get_btc_regime()
source_mode     = "DB" if db_ready() else "DEMO"

# Dagelijks PnL via lichte query — beperkt tot vandaag
@st.cache_data(ttl=30, show_spinner=False)
def _get_pnl_today_light() -> float:
    if not db_ready():
        return 0.0
    result = run_scalar("""
    SELECT COALESCE(SUM(
        CASE WHEN UPPER(outcome)='WIN'  THEN  ABS(COALESCE(pnl_eur,0))
             WHEN UPPER(outcome)='LOSS' THEN -ABS(COALESCE(pnl_eur,0))
             ELSE 0 END
    ),0)
    FROM public.experience_trades
    WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
      AND DATE(COALESCE(exit_time,updated_at) AT TIME ZONE 'UTC') = CURRENT_DATE
    """, default=0.0)
    return safe_float(result)

@st.cache_data(ttl=30, show_spinner=False)
def _get_pf30_light() -> float:
    if not db_ready():
        return 0.0
    df = run_query("""
    SELECT COALESCE(SUM(CASE WHEN UPPER(outcome)='WIN' THEN ABS(COALESCE(pnl_eur,0)) ELSE 0 END),0) AS w,
           COALESCE(SUM(CASE WHEN UPPER(outcome)='LOSS' THEN ABS(COALESCE(pnl_eur,0)) ELSE 0 END),0.001) AS l
    FROM public.experience_trades
    WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
      AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
      AND COALESCE(exit_time,updated_at) >= NOW() - INTERVAL '30 days'
    """)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return 0.0
    w = safe_float(df.iloc[0].get("w", 0))
    l = max(safe_float(df.iloc[0].get("l", 0.001)), 0.001)
    return round(w / l, 2)

@st.cache_data(ttl=30, show_spinner=False)
def _get_cons_light() -> int:
    if not db_ready():
        return 0
    df = run_query("""
    SELECT outcome FROM public.experience_trades
    WHERE UPPER(COALESCE(source,'')) IN ('REAL','LIVE')
      AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
    ORDER BY COALESCE(exit_time,updated_at) DESC LIMIT 10
    """)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return 0
    count = 0
    for _, row in df.iterrows():
        if safe_str(row.get("outcome")).upper() == "LOSS":
            count += 1
        else:
            break
    return count

@st.cache_data(ttl=30, show_spinner=False)
def _get_open_count_light() -> int:
    try:
        state, _ = safe_json(LIVE_STATE_PATH)
        return len((state or {}).get("positions", {}))
    except Exception:
        return 0

pnl_today   = _get_pnl_today_light()
pf30d       = _get_pf30_light()
cons_losses = _get_cons_light()
open_count  = _get_open_count_light()

render_top_bar(
    bot_label, bot_emoji, bot_type,
    btc_regime_data,
    pnl_today, pf30d, cons_losses, open_count,
    source_mode,
)

# Status notice
if st.session_state.status_notice:
    st.info(st.session_state.status_notice)
    st.session_state.status_notice = ""

# ── PAGINA ROUTING — lazy loading ─────────────────────────────
page = st.session_state.page

if page == "dashboard":
    dash_left, dash_content = st.columns([0.72, 3.28], gap="small")
    with dash_left:
        render_sidebar()
    with dash_content:
        with st.spinner("Data laden..."):
            real_df, sim_df, shadow_df, history_df, source_mode = get_all_trade_data()
        render_dashboard(history_df, real_df, sim_df, shadow_df, source_mode)

else:
    side_col, content_col = st.columns([0.72, 3.28], gap="small")
    with side_col:
        render_sidebar()

    with content_col:

        if page == "coach":
            # Coach chat — laadt alleen lichte real_df (laatste 100)
            @st.cache_data(ttl=30, show_spinner=False)
            def _coach_data():
                sql = build_trades_sql("REAL", 100)
                return normalize_trade_df(run_query(sql)) if sql else empty_trade_df()
            @st.cache_data(ttl=120, show_spinner=False)
            def _coach_history():
                sql = build_trades_sql("ALL", 500)
                return normalize_trade_df(run_query(sql)) if sql else empty_trade_df()
            render_alarm_banner()
            render_coach_chat_page(_coach_history(), _coach_data())

        elif page == "monitor":
            render_coach_monitor_page()

        elif page == "health":
            render_health_page()

        elif page == "controls":
            render_controls_page()

        elif page == "live":
            with st.spinner("Live trades laden..."):
                real_df = load_real_trades()
            render_trade_page("live", real_df, "💶 Live Performance",
                "Alleen echte REAL trades met echt geld op Bitvavo.")

        elif page == "sim":
            with st.spinner("Simulator laden..."):
                sim_df = load_sim_trades()
            render_trade_page("sim", sim_df, "🔮 Simulator",
                "Alleen SIM trades — historische signalen van history_simulator.py.")

        elif page == "shadow":
            with st.spinner("Shadow trades laden..."):
                shadow_df = load_shadow_trades()
            render_trade_page("shadow", shadow_df, "🎭 Shadow Review",
                "Meekijkende trades zonder echt geld — leerdata voor het scoreboard.")

        elif page == "positions":
            render_open_positions_page()

        elif page == "analyse":
            with st.spinner("Analyse data laden..."):
                real_df   = load_real_trades()
                history_df = load_all_trades()
            render_alarm_banner()
            render_analyse_page(history_df, real_df)
            _render_extra_analyse_tabs(history_df, real_df)

        elif page == "coins":
            with st.spinner("Coin data laden..."):
                real_df    = load_real_trades()
                history_df = load_all_trades()
            render_coins_page(history_df, real_df)

        elif page == "kalender":
            with st.spinner("Kalender laden..."):
                history_df = load_all_trades()
            render_kalender_page(history_df)

        elif page == "correlatie":
            with st.spinner("Correlatie data laden..."):
                real_df    = load_real_trades()
                history_df = load_all_trades()
            render_correlatie_page(history_df, real_df)

        elif page == "portfolio":
            with st.spinner("Portfolio laden..."):
                snapshot  = read_snapshot()[0]
                assets_df = prepare_assets_df(snapshot)
                snap_mode = read_snapshot()[1]
            render_portfolio_page(snapshot, assets_df, snap_mode)

        elif page == "signals":
            with st.spinner("Signals laden..."):
                pending_df = load_pending_signals()
            render_signals_page(pending_df)

        elif page == "scoreboard":
            with st.spinner("Scoreboard laden..."):
                scoreboard_df = load_scoreboard()
            render_scoreboard_page(scoreboard_df)

        elif page == "regime":
            render_regime_page()

        elif page == "settings":
            render_settings_page()

        elif page == "help":
            with st.spinner("Data laden..."):
                real_df    = load_real_trades()
                sim_df     = load_sim_trades()
                shadow_df  = load_shadow_trades()
                history_df = load_all_trades()
            render_help_page(history_df, real_df, sim_df, shadow_df, source_mode,
                             read_snapshot()[1])

        else:
            st.error(f"Onbekende pagina: {page}")

# Sluiting
st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
st.caption(
    f"Crypto AI Terminal v3.0 | "
    f"DB: {'✅' if db_ready() else '❌ DEMO'} | "
    f"Pagina: {page} | "
    f"{now_utc().strftime('%Y-%m-%d %H:%M UTC')}"
)
st.markdown("</div>", unsafe_allow_html=True)
