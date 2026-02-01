import os
import json
import time
import hmac
import hashlib
import requests
import streamlit as st
import pandas as pd
from datetime import datetime

# ============================================================
# CONFIG (via Render Environment Variables)
# ============================================================
BITVAVO_API_KEY = os.getenv("BITVAVO_API_KEY")
BITVAVO_API_SECRET = os.getenv("BITVAVO_API_SECRET")

SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", "/data/account_snapshot.json")

# Optioneel (als je dit later koppelt)
PAPER_TRADES_PATH = os.getenv("PAPER_TRADES_PATH", "/data/paper_trades.csv")
PAPER_STATE_PATH = os.getenv("PAPER_STATE_PATH", "/data/paper_state.json")
PENDING_APPROVALS_PATH = os.getenv("PENDING_APPROVALS_PATH", "/data/pending_approvals.json")
BOT_LOG_PATH = os.getenv("BOT_LOG_PATH", "/data/bot_log.json")

BASE_URL = "https://api.bitvavo.com/v2"

# ============================================================
# STREAMLIT PAGE
# ============================================================
st.set_page_config(page_title="Crypto AI Dashboard", layout="wide")
st.title("📊 Crypto AI Dashboard")
st.caption("Live overzicht van saldo, assets, trades en bot-status (Render-proof)")

# ============================================================
# HELPERS
# ============================================================
def now_ts():
    return int(time.time())

def fmt_dt(ts: int):
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"

def safe_read_json(path: str):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return None
    return None

def safe_read_csv(path: str):
    try:
        if os.path.exists(path):
            return pd.read_csv(path)
    except Exception:
        return None
    return None

def bitvavo_request(path: str):
    """
    GET-request naar Bitvavo v2 met HMAC signature.
    """
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        raise Exception("BITVAVO_API_KEY / BITVAVO_API_SECRET ontbreken in Render env.")

    timestamp = str(int(time.time() * 1000))
    message = timestamp + "GET" + path

    signature = hmac.new(
        BITVAVO_API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": signature,
        "Bitvavo-Access-Timestamp": timestamp,
        "Bitvavo-Access-Window": "10000",
    }

    r = requests.get(BASE_URL + path, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()

def write_snapshot(path: str, balances):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    snapshot = {
        "status": "OK",
        "timestamp": now_ts(),
        "balances": balances
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)

def compute_drawdown(equity_series: pd.Series):
    """
    equity_series: reeks met equity/saldo waarden
    return: max drawdown in %
    """
    if equity_series is None or len(equity_series) < 2:
        return None
    peak = equity_series.cummax()
    dd = (equity_series - peak) / peak
    return float(dd.min() * 100)

# ============================================================
# LAYOUT: 3 tabs
# ============================================================
tab1, tab2, tab3 = st.tabs(["💶 Saldo & Assets", "📈 Trades & Performance", "🤖 Bot Status / Masterlijst"])

# ============================================================
# TAB 1: SALDO & ASSETS
# ============================================================
with tab1:
    colA, colB = st.columns([2, 1])

    with colA:
        st.subheader("Snapshot instellingen")
        st.write("Snapshot pad (Render Disk):")
        st.code(SNAPSHOT_PATH)

    with colB:
        st.subheader("Actie")
        if st.button("🔄 Snapshot (saldo) verversen"):
            try:
                balances = bitvavo_request("/balance")
                write_snapshot(SNAPSHOT_PATH, balances)
                st.success("Snapshot bijgewerkt ✅")
            except Exception as e:
                st.error(f"Snapshot fout: {e}")

    snapshot = safe_read_json(SNAPSHOT_PATH)

    if not snapshot:
        st.warning("Nog geen snapshot gevonden. Klik op **Snapshot (saldo) verversen**.")
        st.stop()

    if snapshot.get("status") != "OK":
        st.error(f"Snapshot status is niet OK: {snapshot}")
        st.stop()

    ts = snapshot.get("timestamp", 0)
    balances = snapshot.get("balances", [])

    st.success(f"✅ Snapshot geladen (laatste update: {fmt_dt(ts)})")

    # balances -> dataframe
    rows = []
    for b in balances:
        try:
            available = float(b.get("available", 0))
            in_order = float(b.get("inOrder", 0))
            total = available + in_order
            if total > 0:
                rows.append({
                    "symbol": b.get("symbol", "-"),
                    "available": available,
                    "inOrder": in_order,
                    "total": total
                })
        except Exception:
            continue

    df_assets = pd.DataFrame(rows)

    # EUR metric
    eur_total = 0.0
    if not df_assets.empty and "symbol" in df_assets.columns:
        eur_row = df_assets[df_assets["symbol"] == "EUR"]
        if not eur_row.empty:
            eur_total = float(eur_row["total"].iloc[0])

    st.metric("💶 EUR beschikbaar", f"€ {eur_total:,.2f}")

    st.subheader("Assets (alleen > 0)")
    st.dataframe(df_assets, use_container_width=True)

    st.subheader("📊 Assetverdeling (%)")
    if not df_assets.empty:
        df_assets["percentage"] = (df_assets["total"] / df_assets["total"].sum()) * 100
        chart_df = df_assets.set_index("symbol")["percentage"]
        st.bar_chart(chart_df)
    else:
        st.info("Geen assets gevonden (>0).")

# ============================================================
# TAB 2: TRADES & PERFORMANCE
# ============================================================
with tab2:
    st.subheader("Trade history (paper trades)")

    st.write("Pad paper_trades.csv:")
    st.code(PAPER_TRADES_PATH)

    trades_df = safe_read_csv(PAPER_TRADES_PATH)

    if trades_df is None:
        st.warning("paper_trades.csv niet gevonden. (Nog geen trades gelogd of pad klopt niet.)")
        st.info("Tip: Zorg dat paper_trader.py trades naar /data/paper_trades.csv schrijft (Render Disk).")
    else:
        st.success(f"✅ Trades geladen: {len(trades_df)} rijen")

        st.dataframe(trades_df, use_container_width=True)

        # ---------- Basic normalisatie (werkt met verschillende kolomnamen) ----------
        # We proberen generiek te zijn. Als je kolommen anders heten, werkt het alsnog zo goed mogelijk.
        # Verwachte kolommen (voorbeeld): timestamp, symbol, side, price, qty, pnl, r_multiple, balance
        # Maar we checken veilig.

        # Timestamp -> datetime
        if "timestamp" in trades_df.columns:
            try:
                trades_df["dt"] = pd.to_datetime(trades_df["timestamp"], unit="s", errors="coerce")
            except Exception:
                trades_df["dt"] = pd.to_datetime(trades_df["timestamp"], errors="coerce")
        elif "time" in trades_df.columns:
            trades_df["dt"] = pd.to_datetime(trades_df["time"], errors="coerce")
        else:
            trades_df["dt"] = pd.NaT

        # KPI’s: PnL kolom zoeken
        pnl_col = None
        for c in ["pnl", "PnL", "profit", "profit_eur"]:
            if c in trades_df.columns:
                pnl_col = c
                break

        # Balance kolom zoeken
        balance_col = None
        for c in ["balance", "Balance", "equity", "eur_balance"]:
            if c in trades_df.columns:
                balance_col = c
                break

        # R multiple kolom zoeken
        r_col = None
        for c in ["r_multiple", "R", "r", "r_mult"]:
            if c in trades_df.columns:
                r_col = c
                break

        # Side kolom zoeken
        side_col = None
        for c in ["side", "action", "type"]:
            if c in trades_df.columns:
                side_col = c
                break

        # ---------- Performance blok ----------
        st.subheader("📌 Performance (wat kan op basis van je CSV)")

        total_trades = len(trades_df)
        total_pnl = None
        winrate = None
        avg_r = None
        max_dd = None

        if pnl_col:
            try:
                pnl_series = pd.to_numeric(trades_df[pnl_col], errors="coerce").fillna(0.0)
                total_pnl = float(pnl_series.sum())
                wins = (pnl_series > 0).sum()
                losses = (pnl_series <= 0).sum()
                if (wins + losses) > 0:
                    winrate = (wins / (wins + losses)) * 100
            except Exception:
                pass

        if r_col:
            try:
                r_series = pd.to_numeric(trades_df[r_col], errors="coerce").dropna()
                if len(r_series) > 0:
                    avg_r = float(r_series.mean())
            except Exception:
                pass

        if balance_col:
            try:
                eq = pd.to_numeric(trades_df[balance_col], errors="coerce").dropna()
                if len(eq) > 1:
                    max_dd = compute_drawdown(eq)
            except Exception:
                pass

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Trades", f"{total_trades}")
        c2.metric("Totaal PnL", "-" if total_pnl is None else f"{total_pnl:,.2f}")
        c3.metric("Winrate", "-" if winrate is None else f"{winrate:.1f}%")
        c4.metric("Max drawdown", "-" if max_dd is None else f"{max_dd:.1f}%")

        # ---------- Grafieken ----------
        st.subheader("📈 Grafieken")

        if balance_col:
            try:
                eq_df = trades_df[[balance_col]].copy()
                eq_df[balance_col] = pd.to_numeric(eq_df[balance_col], errors="coerce")
                eq_df = eq_df.dropna()
                if len(eq_df) > 1:
                    st.write("Equity curve (saldo over tijd)")
                    st.line_chart(eq_df[balance_col])
            except Exception:
                st.info("Equity curve kan niet worden gemaakt (balance kolom niet bruikbaar).")
        else:
            st.info("Geen balance/equity kolom gevonden in je CSV → equity curve kan nog niet.")

        if pnl_col:
            try:
                pnl_series = pd.to_numeric(trades_df[pnl_col], errors="coerce").fillna(0.0)
                st.write("PnL per trade")
                st.bar_chart(pnl_series)
            except Exception:
                st.info("PnL chart kan niet worden gemaakt.")
        else:
            st.info("Geen pnl kolom gevonden → PnL grafiek kan nog niet.")

# ============================================================
# TAB 3: BOT STATUS / MASTERLIJST
# ============================================================
with tab3:
    st.subheader("🤖 Bot status (health & bestanden)")

    # Snel overzicht: bestaan bestanden?
    checks = [
        ("Snapshot", SNAPSHOT_PATH),
        ("Paper trades", PAPER_TRADES_PATH),
        ("Paper state", PAPER_STATE_PATH),
        ("Pending approvals", PENDING_APPROVALS_PATH),
        ("Bot log", BOT_LOG_PATH),
    ]

    check_rows = []
    for name, path in checks:
        check_rows.append({
            "item": name,
            "path": path,
            "exists": "✅" if os.path.exists(path) else "❌"
        })

    st.dataframe(pd.DataFrame(check_rows), use_container_width=True)

    st.subheader("📌 Masterlijst (wat we willen meten & tonen)")
    st.write("""
Hier is jouw **masterlijst** die we stap voor stap in dit dashboard gaan vullen (grafieken + tabellen):

### A) Account & Portfolio
- EUR beschikbaar + total account value
- Asset verdeling (%) + ontwikkeling over tijd
- Open orders (als je die later toevoegt)
- Fees betaald (per week/maand)

### B) Signals (Pre-BUY)
- Aantal Pre-BUY’s per dag/week
- Score verdeling (70-79 / 80-89 / 90+)
- Welke coins komen het meest terug
- Conversion rate: Pre-BUY → YES → BUY (in %)

### C) Trades (paper/real)
- Open trades vs gesloten trades
- Entry/stop/target per trade
- R-multiple per trade (R-resultaat)
- Partial sells (40% / 60% / 100%) status

### D) Performance KPI’s
- Winrate (%)
- Profit factor
- Average R
- Max drawdown
- Equity curve
- Best/worst coin
- Best/worst timeframe (4h setups)

### E) Execution & Monitoring
- Bot uptime/status (OK/ERROR)
- Laatste run multi_coin_score
- Laatste run trade_monitor
- Laatste WhatsApp approval binnen
- Error log (laatste 20 errors)

### F) Learning layer (jouw roadmap)
- WHY-tags per trade
- Decision vs system comparison
- MFE/MAE tracking
- Timing metrics
- Context labels
- Weekly report snapshot
""")

    st.info("Zodra de bestanden/CSV’s bestaan, kunnen we elk blok 1 voor 1 aanzetten en visualiseren.")
