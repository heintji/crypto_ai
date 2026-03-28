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
# NAVIGATIE HELPERS
# ============================================================
PAGE_NAMES = {
    "dashboard":  "◉ Dashboard",
    "live":       "◉ Live Performance",
    "sim":        "◉ Simulator",
    "shadow":     "◉ Shadow Review",
    "portfolio":  "◉ Portfolio",
    "signals":    "◉ Pre-BUY Signals",
    "scoreboard": "◉ Scoreboard",
    "regime":     "◉ BTC Regime",
    "settings":   "◉ Instellingen",
    "help":       "◉ Help & Debug",
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

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    # Nav knoppen
    for page_key, label in PAGE_NAMES.items():
        nav_btn(label, page_key)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    # Auto refresh
    auto = st.checkbox("⚡ Auto-refresh", value=st.session_state.auto_refresh)
    if auto != st.session_state.auto_refresh:
        st.session_state.auto_refresh = auto
    if auto:
        st.caption(f"Ververst elke {DASHBOARD_REFRESH}s")
        time.sleep(DASHBOARD_REFRESH)
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

    outcome  = safe_str(row.get("outcome"))
    pnl_r    = safe_float(row.get("pnl_r"))
    pnl_eur  = safe_float(row.get("pnl_eur"))
    entry    = safe_float(row.get("entry"))
    stop     = safe_float(row.get("stop"))
    target   = safe_float(row.get("target"))
    rr       = abs(target - entry) / max(abs(entry - stop), 0.0001) if entry > 0 and stop > 0 and target > 0 else 0.0

    outcome_color = "#34d399" if outcome == "WIN" else "#fb7185" if outcome == "LOSS" else "#94a3b8"

    st.markdown(f"""
    <div class="trade-chip-row">
        <div class="trade-chip" style="color:{outcome_color}">{outcome}</div>
        <div class="trade-chip">{safe_str(row.get("symbol"))}</div>
        <div class="trade-chip">{safe_str(row.get("trade_type"))}</div>
        <div class="trade-chip">{safe_str(row.get("setup_type"))}</div>
        <div class="trade-chip">{safe_str(row.get("timeframe"))}</div>
        <div class="trade-chip">{safe_str(row.get("regime"))}</div>
    </div>
    """, unsafe_allow_html=True)

    c1, c2 = st.columns(2, gap="small")
    with c1:
        for label, value in [
            ("Trade ID",   safe_str(row.get("trade_id"))),
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
        for label, value in [
            ("P&L R",       format_r(pnl_r)),
            ("P&L EUR",     format_money(pnl_eur)),
            ("Open",        format_dt(row.get("created_at"))),
            ("Close",       format_dt(row.get("closed_at"))),
            ("Duur",        dur_str),
            ("Source",      safe_str(row.get("source"))),
            ("Label",       safe_str(row.get("label"), "-")),
            ("MFE",         format_price(row.get("mfe")) if safe_float(row.get("mfe")) > 0 else "-"),
        ]:
            st.markdown(f'<div class="list-row"><div class="list-left">{label}</div><div class="list-right">{value}</div></div>', unsafe_allow_html=True)

    st.plotly_chart(chart_trade_detail(row), use_container_width=True,
                    config={"displayModeBar":False},
                    key=f"td_{safe_str(row.get('trade_id','x'))}")

    # Trade analyse tekst
    sym    = safe_str(row.get("symbol"))
    setup  = safe_str(row.get("setup_type"))
    regime = safe_str(row.get("regime"))
    ttype  = safe_str(row.get("trade_type"))
    analyse = (
        f"{'✅ WIN' if outcome=='WIN' else '❌ LOSS'} op {sym} — setup '{setup}' in regime '{regime}' via {ttype}. "
        f"Entry: {format_price(entry)}, Stop: {format_price(stop)}, Target: {format_price(target)}. "
        f"R/R = 1:{rr:.2f}. Resultaat: {format_r(pnl_r)} ({format_money(pnl_eur)})."
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

    # Hero metrics rij
    summary_all  = perf_summary(history_df)
    summary_real = perf_summary(real_df)

    c1, c2, c3, c4, c5, c6 = st.columns(6, gap="small")
    with c1:
        st.markdown(metric_card("Trades (totaal)", str(int(summary_all["count"])), "", "blue"), unsafe_allow_html=True)
    with c2:
        wr_all = format_pct(summary_all["winrate"])
        st.markdown(metric_card("Win Rate (all)", wr_all, f"R:{format_r(summary_all['total_r'])}", "green"), unsafe_allow_html=True)
    with c3:
        wr_real = format_pct(summary_real["winrate"])
        st.markdown(metric_card("Win Rate (live)", wr_real, f"€{summary_real['total_eur']:.2f}", "orange"), unsafe_allow_html=True)
    with c4:
        pf = summary_real["profit_factor"]
        pf_color = "green" if pf >= 1.5 else "red"
        st.markdown(metric_card("Profit Factor 30d", f"{pf:.2f}", "✅ OK" if pf >= 1.5 else "⚠️ Laag", pf_color), unsafe_allow_html=True)
    with c5:
        st.markdown(metric_card("Expectancy", f"{summary_all['expectancy']:.2f} R", "Per trade", "purple"), unsafe_allow_html=True)
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
            ("Trades",         str(int(summary_real["count"])),      ""),
            ("Wins",           str(int(summary_real["count"] * summary_real["winrate"] / 100)), f'{summary_real["winrate"]:.1f}%'),
            ("Losses",         str(int(summary_real["count"] * (100 - summary_real["winrate"]) / 100)), f'{100-summary_real["winrate"]:.1f}%'),
            ("Totale Winst",   format_money(summary_real["gross_profit"]), ""),
            ("Totale Verlies", format_money(summary_real["gross_loss"]),   ""),
            ("Netto",          format_money(summary_real["total_eur"]),    ""),
            ("Profit Factor",  f'{summary_real["profit_factor"]:.2f}',     "doel: >1.5"),
            ("Expectancy",     f'{summary_real["expectancy"]:.2f} R',      "per trade"),
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
    st.markdown('<div class="panel">', unsafe_allow_html=True)

    st.markdown(f'<div class="section-title">{title}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="section-subtitle">{subtitle}</div>', unsafe_allow_html=True)

    filtered = render_filters(df, include_trade_type=False)
    summary  = perf_summary(filtered)

    m1, m2, m3, m4, m5 = st.columns(5, gap="small")
    with m1: st.markdown(metric_card("Trades", str(int(summary["count"])), "", "blue"), unsafe_allow_html=True)
    with m2: st.markdown(metric_card("Win Rate", format_pct(summary["winrate"]), "", "green"), unsafe_allow_html=True)
    with m3: st.markdown(metric_card("Totale R", format_r(summary["total_r"]), "", "orange"), unsafe_allow_html=True)
    with m4: st.markdown(metric_card("Profit Factor", f'{summary["profit_factor"]:.2f}', "doel: >1.5", "purple" if summary["profit_factor"] >= 1.5 else "red"), unsafe_allow_html=True)
    with m5: st.markdown(metric_card("Max Drawdown", f'{summary["max_dd"]:.2f} R', "", "red"), unsafe_allow_html=True)

    c1, c2 = st.columns([1.4, 1.0], gap="small")
    with c1:
        st.plotly_chart(chart_equity_curve(filtered, f"{title} Equity Curve"), use_container_width=True, config={"displayModeBar":False}, key=f"{page_name}_equity")
    with c2:
        st.plotly_chart(chart_win_loss_bar(filtered, "Win / Loss"), use_container_width=True, config={"displayModeBar":False}, key=f"{page_name}_wl")

    c3, c4 = st.columns([1.4, 1.0], gap="small")
    with c3:
        st.plotly_chart(chart_setup_perf(filtered, "Setup Performance"), use_container_width=True, config={"displayModeBar":False}, key=f"{page_name}_setup")
    with c4:
        st.plotly_chart(chart_daily_r(filtered, "Dagresultaten"), use_container_width=True, config={"displayModeBar":False}, key=f"{page_name}_daily")

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    tl, tr = st.columns([1.2, 0.95], gap="small")
    with tl:
        render_trade_list(filtered, f"{title} — Trade Lijst")
    with tr:
        selected = get_selected_trade(filtered)
        render_trade_detail(selected)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    display_cols = [c for c in ["trade_id","symbol","setup_type","regime","timeframe","outcome","pnl_r","pnl_eur","score","datetime"] if c in filtered.columns]
    if display_cols:
        st.dataframe(filtered[display_cols].head(100), use_container_width=True, hide_index=True)

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
# DATA LADEN
# ============================================================
real_df, sim_df, shadow_df, history_df, source_mode = get_all_trade_data()
snapshot, snap_mode   = read_snapshot()
assets_df             = prepare_assets_df(snapshot)
pending_df            = load_pending_signals()
scoreboard_df         = load_scoreboard()
btc_regime_data       = get_btc_regime()

# Key metrics voor top balk
bot_label, bot_emoji, bot_type = get_bot_status()
pnl_today  = get_daily_pnl_today(real_df)
pf30d      = get_profit_factor_30d(real_df)
cons_losses = get_consecutive_losses(real_df)
real_summary = perf_summary(real_df)

# Open trades tellen
try:
    _live_state, _ = safe_json(LIVE_STATE_PATH)
    open_count = len((_live_state or {}).get("positions", {})) if _live_state else 0
except Exception:
    open_count = 0


# ============================================================
# HOOFD LAYOUT
# ============================================================
st.markdown('<div class="shell">', unsafe_allow_html=True)

# Top balk
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

# Hoofd kolommen: sidebar + content
if st.session_state.page == "dashboard":
    # Dashboard gebruikt volle breedte
    dash_left, dash_content = st.columns([0.72, 3.28], gap="small")
    with dash_left:
        render_sidebar()
    with dash_content:
        render_dashboard(history_df, real_df, sim_df, shadow_df, source_mode)

else:
    # Alle andere pagina's
    side_col, content_col = st.columns([0.72, 3.28], gap="small")

    with side_col:
        render_sidebar()

    with content_col:
        page = st.session_state.page

        if page == "live":
            render_trade_page(
                "live", real_df,
                "💶 Live Performance",
                "Alleen echte REAL trades met echt geld op Bitvavo. "
                "Bewaakt door trade_monitor.py. Geschreven door live_trader.py.",
            )

        elif page == "sim":
            render_trade_page(
                "sim", sim_df,
                "🔮 Simulator",
                "Alleen SIM trades. Historische en hypothetische signalen. "
                "Gebouwd door history_simulator.py op historische candle data.",
            )

        elif page == "shadow":
            render_trade_page(
                "shadow", shadow_df,
                "🎭 Shadow Review",
                "Alleen SHADOW trades. Meekijkende trades zonder echt geld. "
                "Parallel aan live trades — identieke exit logica, geen limieten. "
                "Leerdata voor het experience_scoreboard.",
            )

        elif page == "portfolio":
            render_portfolio_page(snapshot, assets_df, snap_mode)

        elif page == "signals":
            render_signals_page(pending_df)

        elif page == "scoreboard":
            render_scoreboard_page(scoreboard_df)

        elif page == "regime":
            render_regime_page()

        elif page == "settings":
            render_settings_page()

        elif page == "help":
            render_help_page(history_df, real_df, sim_df, shadow_df, source_mode, snap_mode)

        else:
            st.error(f"Onbekende pagina: {page}")

# Sluiting
st.markdown('<div class="divider"></div>', unsafe_allow_html=True)
st.caption(
    f"Crypto AI Terminal v3.0 | "
    f"Mode: {source_mode} | "
    f"DB: {'✅' if db_ready() else '❌ DEMO'} | "
    f"History: {len(history_df)} | "
    f"Live: {len(real_df)} | "
    f"SIM: {len(sim_df)} | "
    f"Shadow: {len(shadow_df)} | "
    f"{now_utc().strftime('%Y-%m-%d %H:%M UTC')}"
)

st.markdown("</div>", unsafe_allow_html=True)
