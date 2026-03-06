import os
import json
import time
import hmac
import hashlib
from datetime import datetime, timezone

import requests
import pandas as pd
import streamlit as st
import plotly.express as px


# =========================
# Config
# =========================
API_KEY = (os.getenv("BITVAVO_API_KEY", "") or "").strip().strip('"').strip("'")
API_SECRET = (os.getenv("BITVAVO_API_SECRET", "") or "").strip().strip('"').strip("'")

BASE_URL = "https://api.bitvavo.com"
ACCESS_WINDOW_MS = os.getenv("BITVAVO_ACCESS_WINDOW_MS", "10000")

SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", "/data/account_snapshot.json")
PORTFOLIO_HISTORY_CSV = os.getenv("PORTFOLIO_HISTORY_CSV", "/data/portfolio_history.csv")

PENDING_PATH = os.getenv("PENDING_PATH", "/data/pending_approvals.json")
PAPER_STATE_PATH = os.getenv("PAPER_STATE_PATH", "/data/paper_state.json")
LIVE_STATE_PATH = os.getenv("LIVE_STATE_PATH", "/data/live_state.json")
PAPER_TRADES_CSV = os.getenv("PAPER_TRADES_CSV", "/data/paper_trades.csv")
LIVE_TRADES_CSV = os.getenv("LIVE_TRADES_CSV", "/data/live_trades.csv")
AI_USAGE_PATH = os.getenv("AI_USAGE_PATH", "/data/ai_usage.json")
PREBUY_STATE_PATH = os.getenv("PREBUY_STATE_PATH", "/data/prebuy_state.json")
PREBUY_PAYLOAD_PATH = os.getenv("PREBUY_PAYLOAD_PATH", "/data/prebuy_payload.json")

AUTO_REFRESH_MINUTES = int(os.getenv("AUTO_REFRESH_MINUTES", "15"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))


# =========================
# Helpers
# =========================
def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat()


def ensure_parent_dir(path: str):
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def safe_read_json(path: str):
    try:
        if not os.path.exists(path):
            return None, f"Bestand niet gevonden: {path}"
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        return None, f"JSON leesfout ({path}): {e}"


def safe_read_csv(path: str):
    try:
        if not os.path.exists(path):
            return None, f"CSV niet gevonden: {path}"
        df = pd.read_csv(path)
        return df, None
    except Exception as e:
        return None, f"CSV leesfout ({path}): {e}"


def file_meta(path: str):
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


def age_minutes(epoch_seconds: float) -> float:
    return (time.time() - float(epoch_seconds)) / 60.0


def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default


def parse_dt(x):
    try:
        return pd.to_datetime(x, utc=True, errors="coerce")
    except Exception:
        return pd.NaT


def format_dt_short(x):
    dt = parse_dt(x)
    if pd.isna(dt):
        return "-"
    return dt.strftime("%Y.%m.%d %H:%M:%S")


def format_money(x):
    v = safe_float(x, 0.0)
    return f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_pnl_compact(x):
    v = safe_float(x, 0.0)
    sign = "+" if v > 0 else ""
    return f"{sign}€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def side_color(side: str) -> str:
    s = str(side or "").strip().lower()
    if s == "buy":
        return "#2ecc71"
    if s == "sell":
        return "#ff5a5f"
    return "#e5e7eb"


def pnl_color(pnl: float) -> str:
    return "#5aa2ff" if pnl >= 0 else "#ff5a5f"


def read_json_meta_field(meta_value, field_name, default=None):
    if meta_value is None:
        return default
    try:
        if isinstance(meta_value, dict):
            return meta_value.get(field_name, default)
        parsed = json.loads(str(meta_value))
        if isinstance(parsed, dict):
            return parsed.get(field_name, default)
    except Exception:
        pass
    return default


# =========================
# Bitvavo Private Request
# =========================
def bitvavo_request(method: str, path: str, body: str = ""):
    if not API_KEY or not API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY of BITVAVO_API_SECRET ontbreekt in Render Environment Variables.")

    method_u = method.upper()
    timestamp = str(int(time.time() * 1000))
    body = body or ""

    message = f"{timestamp}{method_u}{path}{body}"
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
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
        raise ValueError("Alleen GET/POST geïmplementeerd.")

    if r.status_code >= 400:
        try:
            err = r.json()
        except Exception:
            err = {"error": r.text}
        raise RuntimeError(f"Bitvavo error {r.status_code}: {err}")

    return r.json()


# =========================
# Public prices
# =========================
@st.cache_data(ttl=30)
def fetch_all_market_prices():
    url = f"{BASE_URL}/v2/ticker/price"
    r = requests.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    prices = {}
    for row in data:
        market = row.get("market")
        price = row.get("price")
        if market and price is not None:
            try:
                prices[market] = float(price)
            except Exception:
                pass
    return prices


def price_in_eur(symbol: str, prices: dict):
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


# =========================
# Snapshot builder
# =========================
def build_snapshot_with_eur_values():
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

    ensure_parent_dir(PORTFOLIO_HISTORY_CSV)
    row = {
        "ts": snapshot["ts"],
        "eur_available": snapshot["eur_available"],
        "crypto_assets_eur": snapshot["crypto_assets_eur"],
        "total_portfolio_eur": snapshot["total_portfolio_eur"],
    }

    if os.path.exists(PORTFOLIO_HISTORY_CSV):
        hist = pd.read_csv(PORTFOLIO_HISTORY_CSV)
        hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
    else:
        hist = pd.DataFrame([row])

    hist = hist.drop_duplicates(subset=["ts"], keep="last")
    hist.to_csv(PORTFOLIO_HISTORY_CSV, index=False)

    return snapshot


def maybe_auto_refresh_snapshot():
    meta = file_meta(SNAPSHOT_PATH)
    if not meta["exists"]:
        return build_snapshot_with_eur_values(), "created"

    mins = age_minutes(meta["modified_epoch"])
    if mins >= AUTO_REFRESH_MINUTES:
        return build_snapshot_with_eur_values(), f"refreshed ({mins:.0f} min old)"
    return None, f"kept (age {mins:.0f} min)"


# =========================
# Load dashboard data
# =========================
def load_state_positions():
    items = []

    for source_name, path in [("paper", PAPER_STATE_PATH), ("live", LIVE_STATE_PATH)]:
        data, err = safe_read_json(path)
        if err or not data:
            continue

        positions = data.get("positions", {}) or {}
        for symbol, pos in positions.items():
            items.append({
                "source": source_name,
                "symbol": symbol,
                "qty": safe_float(pos.get("qty") or pos.get("base_amount") or pos.get("position_size"), 0.0),
                "entry": safe_float(pos.get("entry"), 0.0),
                "stop_loss": safe_float(pos.get("stop_loss"), 0.0),
                "target": safe_float(pos.get("target"), 0.0),
                "opened_at": pos.get("opened_at"),
                "prebuy_id": pos.get("prebuy_id"),
                "setup_type": pos.get("setup_type"),
                "regime": pos.get("regime"),
                "label": pos.get("label"),
                "chance": pos.get("chance"),
                "score": pos.get("score"),
                "live": bool(pos.get("live", source_name == "live")),
            })

    return pd.DataFrame(items)


def load_pending_orders():
    data, err = safe_read_json(PENDING_PATH)
    if err or not data:
        return pd.DataFrame([])

    rows = []
    if isinstance(data, list):
        iterable = data
    elif isinstance(data, dict):
        iterable = list(data.values()) if all(isinstance(v, dict) for v in data.values()) else [data]
    else:
        iterable = []

    for row in iterable:
        rows.append({
            "id": row.get("id"),
            "symbol": row.get("symbol"),
            "status": row.get("status", "PENDING"),
            "setup_type": row.get("setup_type"),
            "regime": row.get("regime"),
            "score": row.get("score"),
            "chance": row.get("chance"),
            "entry": row.get("entry"),
            "stop": row.get("stop"),
            "target": row.get("target"),
            "expires_at": row.get("expires_at"),
        })

    return pd.DataFrame(rows)


def load_trade_history():
    all_rows = []

    for source_name, path in [("paper", PAPER_TRADES_CSV), ("live", LIVE_TRADES_CSV)]:
        df, err = safe_read_csv(path)
        if err or df is None or df.empty:
            continue

        lower = {c.lower(): c for c in df.columns}

        dt_col = lower.get("datetime") or lower.get("timestamp") or lower.get("time") or lower.get("ts")
        symbol_col = lower.get("symbol")
        side_col = lower.get("side")
        price_col = lower.get("price")
        qty_col = lower.get("qty")
        pnl_col = lower.get("pnl")
        meta_col = lower.get("meta")

        for _, row in df.iterrows():
            meta_val = row[meta_col] if meta_col else None

            entry_from_meta = read_json_meta_field(meta_val, "entry")
            exit_from_meta = read_json_meta_field(meta_val, "exit")
            setup_from_meta = read_json_meta_field(meta_val, "setup_type")
            regime_from_meta = read_json_meta_field(meta_val, "regime")
            label_from_meta = read_json_meta_field(meta_val, "label")
            amount_eur_from_meta = read_json_meta_field(meta_val, "amount_eur")
            prebuy_id_from_meta = read_json_meta_field(meta_val, "prebuy_id")

            side = str(row[side_col] if side_col else "").strip().lower()
            symbol = str(row[symbol_col] if symbol_col else "").strip()
            price = safe_float(row[price_col] if price_col else 0.0, 0.0)
            qty = safe_float(row[qty_col] if qty_col else 0.0, 0.0)
            pnl = safe_float(row[pnl_col] if pnl_col else 0.0, 0.0)
            dt_val = row[dt_col] if dt_col else None

            entry_price = safe_float(entry_from_meta, 0.0)
            exit_price = safe_float(exit_from_meta, 0.0)

            if side == "buy":
                if entry_price <= 0:
                    entry_price = price
                if exit_price <= 0:
                    exit_price = price
            else:
                if exit_price <= 0:
                    exit_price = price
                if entry_price <= 0:
                    entry_price = price

            all_rows.append({
                "source": source_name,
                "datetime": format_dt_short(dt_val),
                "datetime_raw": parse_dt(dt_val),
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "price": price,
                "pnl": pnl,
                "setup_type": setup_from_meta,
                "regime": regime_from_meta,
                "label": label_from_meta,
                "amount_eur": amount_eur_from_meta,
                "prebuy_id": prebuy_id_from_meta,
            })

    df_all = pd.DataFrame(all_rows)
    if not df_all.empty and "datetime_raw" in df_all.columns:
        df_all = df_all.sort_values("datetime_raw", ascending=False, na_position="last").reset_index(drop=True)
    return df_all


# =========================
# UI styling
# =========================
st.set_page_config(page_title="Crypto AI Terminal", layout="wide")

st.markdown("""
<style>
    .stApp {
        background-color: #05070b;
        color: #f5f7fb;
    }
    .block-container {
        padding-top: 1rem;
        padding-bottom: 6rem;
        max-width: 1100px;
    }

    /* Metrics */
    div[data-testid="stMetric"] {
        background: #0d1118;
        border: 1px solid #1f2937;
        padding: 10px 14px;
        border-radius: 16px;
    }
    div[data-testid="stMetric"] label {
        color: #9aa4b2 !important;
    }
    div[data-testid="stMetricValue"] {
        color: #ffffff !important;
    }

    /* Bovenste navigatieknoppen zichtbaar maken */
    div[data-testid="stHorizontalBlock"] button {
        color: #000000 !important;
        font-weight: 700 !important;
    }

    /* Tabs tekst beter leesbaar */
    button[role="tab"] {
        color: #e5e7eb !important;
        font-weight: 600 !important;
    }

    .terminal-card {
        background: #06080d;
        border: 1px solid #111827;
        border-radius: 24px;
        padding: 14px 14px 6px 14px;
        box-shadow: 0 8px 24px rgba(0,0,0,0.35);
    }
    .section-title {
        color: #e5e7eb;
        font-size: 16px;
        font-weight: 700;
        margin-bottom: 8px;
    }
    .deal-row {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        border-bottom: 1px solid rgba(255,255,255,0.04);
        padding: 14px 4px;
    }
    .deal-left {
        width: 70%;
    }
    .deal-main {
        font-size: 20px;
        font-weight: 700;
        color: #f3f4f6;
        line-height: 1.2;
    }
    .deal-sub {
        font-size: 15px;
        color: #cbd5e1;
        margin-top: 4px;
    }
    .deal-right {
        width: 30%;
        text-align: right;
    }
    .deal-dt {
        color: #cbd5e1;
        font-size: 13px;
        margin-bottom: 10px;
        white-space: nowrap;
    }
    .deal-pnl {
        font-size: 30px;
        font-weight: 800;
        line-height: 1;
    }
    .pill-wrap {
        display: flex;
        gap: 8px;
        margin-bottom: 12px;
        flex-wrap: wrap;
    }
    .pill {
        background: #161b23;
        color: #ffffff;
        border-radius: 999px;
        padding: 8px 14px;
        font-size: 14px;
        font-weight: 600;
        display: inline-block;
        border: 1px solid #222938;
    }
    .mini-row {
        display: flex;
        justify-content: space-between;
        border-bottom: 1px solid rgba(255,255,255,0.04);
        padding: 10px 2px;
    }
    .mini-left {
        color: #f3f4f6;
        font-weight: 600;
    }
    .mini-right {
        color: #93c5fd;
        font-weight: 700;
    }
    .warn-box {
        background: #0f172a;
        border: 1px solid #1e293b;
        color: #cbd5e1;
        border-radius: 16px;
        padding: 12px 14px;
        font-size: 14px;
    }
    .nav-hint {
        color: #94a3b8;
        font-size: 12px;
        text-align: center;
        margin-top: 4px;
    }
</style>
""", unsafe_allow_html=True)


# =========================
# Refresh snapshot
# =========================
try:
    _, refresh_state = maybe_auto_refresh_snapshot()
except Exception as e:
    refresh_state = f"auto-refresh faalde: {e}"


# =========================
# Load data
# =========================
snapshot, snapshot_err = safe_read_json(SNAPSHOT_PATH)
portfolio_hist, portfolio_hist_err = safe_read_csv(PORTFOLIO_HISTORY_CSV)
positions_df = load_state_positions()
orders_df = load_pending_orders()
deals_df = load_trade_history()

eur_available = safe_float((snapshot or {}).get("eur_available"), 0.0)
crypto_assets_eur = safe_float((snapshot or {}).get("crypto_assets_eur"), 0.0)
total_portfolio_eur = safe_float((snapshot or {}).get("total_portfolio_eur"), 0.0)

total_pnl = 0.0
if not deals_df.empty and "pnl" in deals_df.columns:
    total_pnl = float(pd.to_numeric(deals_df["pnl"], errors="coerce").fillna(0).sum())

open_trades_count = len(positions_df) if not positions_df.empty else 0

if "nav" not in st.session_state:
    st.session_state["nav"] = "History"

nav_cols = st.columns(5)
nav_items = ["Quotes", "Chart", "Trade", "History", "Settings"]

for idx, item in enumerate(nav_items):
    selected = st.session_state["nav"] == item
    label = f"● {item}" if selected else item
    if nav_cols[idx].button(label, use_container_width=True):
        st.session_state["nav"] = item

st.markdown('<div class="nav-hint">App-stijl navigatie zoals je screenshot</div>', unsafe_allow_html=True)

st.markdown("### Crypto AI Terminal")
st.caption(f"Snapshot status: {refresh_state}")

m1, m2, m3, m4 = st.columns(4)
m1.metric("Balance", format_money(eur_available))
m2.metric("Crypto", format_money(crypto_assets_eur))
m3.metric("Total", format_money(total_portfolio_eur))
m4.metric("Profit", format_pnl_compact(total_pnl))

st.markdown('<div class="terminal-card">', unsafe_allow_html=True)

top_tabs = st.tabs(["Positions", "Orders", "Deals"])

with top_tabs[0]:
    st.markdown('<div class="section-title">Open Positions</div>', unsafe_allow_html=True)
    if positions_df.empty:
        st.markdown('<div class="warn-box">Geen open positions gevonden.</div>', unsafe_allow_html=True)
    else:
        positions_show = positions_df.copy()
        positions_show["opened_at"] = positions_show["opened_at"].apply(
            lambda x: datetime.utcfromtimestamp(int(x)).strftime("%Y.%m.%d %H:%M:%S") if safe_int(x, 0) > 0 else "-"
        )
        st.dataframe(
            positions_show[[
                "symbol", "source", "qty", "entry", "stop_loss", "target",
                "chance", "score", "label", "regime", "opened_at"
            ]],
            use_container_width=True,
            hide_index=True
        )

with top_tabs[1]:
    st.markdown('<div class="section-title">Orders / Pending approvals</div>', unsafe_allow_html=True)
    if orders_df.empty:
        st.markdown('<div class="warn-box">Geen pending orders gevonden. Gebruik LIST/TOP in WhatsApp of check je pending bron.</div>', unsafe_allow_html=True)
    else:
        st.dataframe(orders_df, use_container_width=True, hide_index=True)

with top_tabs[2]:
    st.markdown('<div class="section-title">Deals / History</div>', unsafe_allow_html=True)
    if deals_df.empty:
        st.markdown('<div class="warn-box">Nog geen deals gevonden in paper/live trades CSV.</div>', unsafe_allow_html=True)
    else:
        for _, row in deals_df.head(50).iterrows():
            sym = row.get("symbol") or "-"
            side = str(row.get("side") or "").lower()
            qty = safe_float(row.get("qty"), 0.0)
            entry_price = safe_float(row.get("entry_price"), 0.0)
            exit_price = safe_float(row.get("exit_price"), 0.0)
            pnl = safe_float(row.get("pnl"), 0.0)
            dt_txt = row.get("datetime") or "-"
            side_col = side_color(side)
            pnl_col = pnl_color(pnl)

            if entry_price > 0 and exit_price > 0:
                price_line = f"{entry_price:.6f} → {exit_price:.6f}"
            else:
                price_line = f"{safe_float(row.get('price'), 0.0):.6f}"

            st.markdown(
                f"""
                <div class="deal-row">
                    <div class="deal-left">
                        <div class="deal-main">
                            {sym}
                            <span style="color:{side_col};font-weight:800;"> {side}</span>
                            <span style="color:{side_col};font-weight:800;"> {qty:g}</span>
                        </div>
                        <div class="deal-sub">{price_line}</div>
                    </div>
                    <div class="deal-right">
                        <div class="deal-dt">{dt_txt}</div>
                        <div class="deal-pnl" style="color:{pnl_col};">{format_pnl_compact(pnl)}</div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

st.markdown("</div>", unsafe_allow_html=True)


# =========================
# Bottom nav content
# =========================
nav = st.session_state["nav"]

if nav == "Quotes":
    st.subheader("Quotes")
    if snapshot_err:
        st.info(snapshot_err)
    elif snapshot and snapshot.get("assets"):
        assets_df = pd.DataFrame(snapshot.get("assets") or [])
        if not assets_df.empty:
            for _, row in assets_df.iterrows():
                symbol = row.get("symbol", "-")
                price_eur = row.get("price_eur")
                eur_val = row.get("eur_value")
                st.markdown(
                    f"""
                    <div class="mini-row">
                        <div class="mini-left">{symbol}</div>
                        <div class="mini-right">
                            prijs: {('-' if price_eur is None else format_money(price_eur))} |
                            waarde: {('-' if eur_val is None else format_money(eur_val))}
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
        else:
            st.info("Geen quote data gevonden.")
    else:
        st.info("Geen quote data gevonden.")

elif nav == "Chart":
    st.subheader("Chart")
    if portfolio_hist_err or portfolio_hist is None or portfolio_hist.empty:
        st.info("Nog geen portfolio history beschikbaar.")
    else:
        portfolio_hist["ts"] = pd.to_datetime(portfolio_hist["ts"], errors="coerce", utc=True)
        portfolio_hist = portfolio_hist.dropna(subset=["ts"]).sort_values("ts")
        if portfolio_hist.empty:
            st.info("Nog geen grafiekdata.")
        else:
            fig = px.line(portfolio_hist, x="ts", y="total_portfolio_eur")
            fig.update_layout(
                template="plotly_dark",
                paper_bgcolor="#05070b",
                plot_bgcolor="#05070b",
                margin=dict(l=10, r=10, t=10, b=10),
                height=420,
            )
            st.plotly_chart(fig, use_container_width=True)

elif nav == "Trade":
    st.subheader("Trade overzicht")
    c1, c2, c3 = st.columns(3)
    c1.metric("Open trades", str(open_trades_count))
    c2.metric("Pending orders", str(len(orders_df) if not orders_df.empty else 0))
    c3.metric("Deals", str(len(deals_df) if not deals_df.empty else 0))

    st.markdown('<div class="warn-box">Deze tab is voor overzicht. BUY/SELL blijft voorlopig via WhatsApp of je bot-logica lopen. Dat is veiliger.</div>', unsafe_allow_html=True)

elif nav == "History":
    st.subheader("History")
    if deals_df.empty:
        st.info("Nog geen history beschikbaar.")
    else:
        history_df = deals_df.copy()
        history_df["PnL €"] = history_df["pnl"].apply(format_pnl_compact)
        history_df["Entry → Exit"] = history_df.apply(
            lambda r: f"{safe_float(r.get('entry_price'), 0.0):.6f} → {safe_float(r.get('exit_price'), 0.0):.6f}",
            axis=1
        )
        st.dataframe(
            history_df[["datetime", "symbol", "side", "qty", "Entry → Exit", "PnL €", "source"]],
            use_container_width=True,
            hide_index=True
        )

elif nav == "Settings":
    st.subheader("Settings / Controle")
    rows = []
    for name, path in [
        ("Snapshot", SNAPSHOT_PATH),
        ("Portfolio history", PORTFOLIO_HISTORY_CSV),
        ("Paper state", PAPER_STATE_PATH),
        ("Live state", LIVE_STATE_PATH),
        ("Paper trades", PAPER_TRADES_CSV),
        ("Live trades", LIVE_TRADES_CSV),
        ("Pending", PENDING_PATH),
        ("AI usage", AI_USAGE_PATH),
        ("Prebuy state", PREBUY_STATE_PATH),
        ("Prebuy payload", PREBUY_PAYLOAD_PATH),
    ]:
        meta = file_meta(path)
        rows.append({
            "item": name,
            "exists": meta.get("exists", False),
            "modified": meta.get("modified", ""),
            "size_kb": meta.get("size_kb", ""),
            "path": path,
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.markdown(
        '<div class="warn-box">Pro tip: hou deze app als overzicht/dashboard. Laat BUY/SELL logica bij je bot-bestanden. Dan blijft het stabiel.</div>',
        unsafe_allow_html=True
    )
