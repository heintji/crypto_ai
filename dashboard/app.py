import os
import json
import time
import hmac
import hashlib
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go


# =========================
# Config
# =========================
API_KEY = (os.getenv("BITVAVO_API_KEY", "") or "").strip().strip('"').strip("'")
API_SECRET = (os.getenv("BITVAVO_API_SECRET", "") or "").strip().strip('"').strip("'")

# Disk paths (Render)
SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", "/data/account_snapshot.json")
PORTFOLIO_HISTORY_CSV = os.getenv("PORTFOLIO_HISTORY_CSV", "/data/portfolio_history.csv")

PENDING_PATH = os.getenv("PENDING_PATH", "/data/pending_approvals.json")
PAPER_STATE_PATH = os.getenv("PAPER_STATE_PATH", "/data/paper_state.json")
PAPER_TRADES_CSV = os.getenv("PAPER_TRADES_CSV", "/data/paper_trades.csv")
AI_USAGE_PATH = os.getenv("AI_USAGE_PATH", "/data/ai_usage.json")
PREBUY_STATE_PATH = os.getenv("PREBUY_STATE_PATH", "/data/prebuy_state.json")
PREBUY_PAYLOAD_PATH = os.getenv("PREBUY_PAYLOAD_PATH", "/data/prebuy_payload.json")

# Bitvavo endpoints
PRIVATE_BASE_URL = "https://api.bitvavo.com"
PUBLIC_BASE_URL_V2 = "https://api.bitvavo.com/v2"
ACCESS_WINDOW_MS = "10000"


# =========================
# Helpers
# =========================
def now_iso():
    return datetime.now(timezone.utc).isoformat()

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
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }

def http_get_json(url: str, params: dict | None = None, timeout: int = 20):
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


# =========================
# Bitvavo Private Request (SIGNING)
# =========================
def bitvavo_private_request(method: str, path: str, body: str = ""):
    """
    signature = HMAC_SHA256(secret, timestamp + METHOD + path + body)
    path moet exact zijn inclusief /v2 (bijv: /v2/balance)
    """
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

    url = f"{PRIVATE_BASE_URL}{path}"

    if method_u == "GET":
        r = requests.get(url, headers=headers, timeout=20)
    elif method_u == "POST":
        r = requests.post(url, headers=headers, data=body, timeout=20)
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
# Public prices (no signature)
# =========================
@st.cache_data(ttl=30, show_spinner=False)
def get_all_market_prices():
    """
    Haalt ALLE prijzen op van Bitvavo public endpoint:
    GET /v2/ticker/price  -> list met {market, price}
    """
    data = http_get_json(f"{PUBLIC_BASE_URL_V2}/ticker/price")
    # map: "BTC-EUR" -> float(price)
    prices = {}
    for row in data:
        m = row.get("market")
        p = row.get("price")
        try:
            prices[m] = float(p)
        except Exception:
            continue
    return prices

def price_in_eur(symbol: str, prices: dict) -> tuple[float | None, str]:
    """
    Geeft (price_eur, route) terug.
    Route:
      1) SYMBOL-EUR
      2) SYMBOL-USDT * USDT-EUR
      3) SYMBOL-BTC  * BTC-EUR
    """
    if symbol == "EUR":
        return 1.0, "EUR"

    m1 = f"{symbol}-EUR"
    if m1 in prices:
        return prices[m1], m1

    # via USDT (meest voorkomend)
    m2a = f"{symbol}-USDT"
    m2b = "USDT-EUR"
    if m2a in prices and m2b in prices:
        return prices[m2a] * prices[m2b], f"{m2a} * {m2b}"

    # via BTC (fallback)
    m3a = f"{symbol}-BTC"
    m3b = "BTC-EUR"
    if m3a in prices and m3b in prices:
        return prices[m3a] * prices[m3b], f"{m3a} * {m3b}"

    return None, "NO_ROUTE"


# =========================
# Snapshot builder (+ EUR waardes)
# =========================
def append_portfolio_history(ts_iso: str, eur_available: float, crypto_assets_eur: float, total_portfolio_eur: float):
    ensure_parent_dir(PORTFOLIO_HISTORY_CSV)

    row = {
        "ts": ts_iso,
        "eur_available": eur_available,
        "crypto_assets_eur": crypto_assets_eur,
        "total_portfolio_eur": total_portfolio_eur,
    }

    if not os.path.exists(PORTFOLIO_HISTORY_CSV):
        pd.DataFrame([row]).to_csv(PORTFOLIO_HISTORY_CSV, index=False)
        return

    df = pd.read_csv(PORTFOLIO_HISTORY_CSV)
    # voorkom dubbele timestamps
    if "ts" in df.columns and (df["ts"] == ts_iso).any():
        return

    df2 = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    # sort op tijd
    try:
        df2["_t"] = pd.to_datetime(df2["ts"], utc=True, errors="coerce")
        df2 = df2.sort_values("_t").drop(columns=["_t"])
    except Exception:
        pass
    df2.to_csv(PORTFOLIO_HISTORY_CSV, index=False)

def build_snapshot():
    """
    1) private balance
    2) public prijzen
    3) bereken crypto assets € + total portfolio €
    4) schrijf snapshot + append history
    """
    balances = bitvavo_private_request("GET", "/v2/balance")
    prices = get_all_market_prices()

    assets = []
    eur_available = 0.0
    crypto_assets_eur = 0.0

    for row in balances:
        symbol = row.get("symbol")
        available = float(row.get("available", 0) or 0)
        in_order = float(row.get("inOrder", 0) or 0)
        total = available + in_order

        if symbol == "EUR":
            eur_available = available

        if total <= 0:
            continue

        p_eur, route = price_in_eur(symbol, prices)
        if p_eur is None:
            eur_value = 0.0
        else:
            eur_value = float(total) * float(p_eur)

        if symbol != "EUR":
            crypto_assets_eur += eur_value

        assets.append({
            "symbol": symbol,
            "available": available,
            "inOrder": in_order,
            "total": total,
            "price_eur": p_eur,           # kan None zijn
            "eur_value": eur_value,       # altijd aanwezig (float)
            "price_route": route,         # debug route
        })

    total_portfolio_eur = eur_available + crypto_assets_eur
    ts_iso = now_iso()

    snapshot = {
        "status": "OK",
        "ts": ts_iso,
        "eur_available": eur_available,
        "crypto_assets_eur": crypto_assets_eur,
        "total_portfolio_eur": total_portfolio_eur,
        "assets": sorted(assets, key=lambda x: x["eur_value"], reverse=True),
    }

    ensure_parent_dir(SNAPSHOT_PATH)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    append_portfolio_history(ts_iso, eur_available, crypto_assets_eur, total_portfolio_eur)
    return snapshot


# =========================
# Portfolio analytics
# =========================
def load_portfolio_history():
    df, err = safe_read_csv(PORTFOLIO_HISTORY_CSV)
    if err:
        return None, err
    if df is None or df.empty:
        return None, "Portfolio history is leeg."
    if "ts" not in df.columns:
        return None, "Portfolio history mist kolom 'ts'."

    df = df.copy()
    df["ts_dt"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts_dt"]).sort_values("ts_dt")
    return df, None

def compute_change_pct(df_hist: pd.DataFrame, days: int):
    """
    % verandering over laatste X dagen op total_portfolio_eur
    """
    if df_hist is None or df_hist.empty:
        return None

    end = df_hist["ts_dt"].max()
    start = end - timedelta(days=days)

    dfw = df_hist[df_hist["ts_dt"] >= start]
    if len(dfw) < 2:
        return None

    first = float(dfw["total_portfolio_eur"].iloc[0])
    last = float(dfw["total_portfolio_eur"].iloc[-1])
    if first == 0:
        return None

    return (last - first) / first * 100.0

def filter_history(df_hist: pd.DataFrame, window: str):
    if df_hist is None or df_hist.empty:
        return df_hist

    end = df_hist["ts_dt"].max()
    if window == "1D":
        start = end - timedelta(days=1)
    elif window == "7D":
        start = end - timedelta(days=7)
    elif window == "30D":
        start = end - timedelta(days=30)
    else:
        return df_hist

    return df_hist[df_hist["ts_dt"] >= start]


# =========================
# UI
# =========================
st.set_page_config(page_title="Crypto AI Dashboard", layout="wide")
st.title("📊 Crypto AI Dashboard")
st.caption("Render-proof dashboard: Bitvavo saldo + portfolio waarde + grafieken + trades/monitoring.")


tabs = st.tabs(["💶 Saldo & Portfolio", "📉 Koers & Trades", "📈 Trades & Performance", "🧠 Bot Status / Masterlijst"])


# =========================
# TAB 1: Saldo & Portfolio
# =========================
with tabs[0]:
    colL, colR = st.columns([2, 1], gap="large")

    with colL:
        meta = file_meta(SNAPSHOT_PATH)
        if meta["exists"]:
            st.success(f"Snapshot gevonden ✅  | Laatst aangepast: {meta['modified']} | Grootte: {meta['size_kb']} KB")
        else:
            st.warning("Nog geen snapshot gevonden. Klik op **Snapshot verversen**.")

        st.write("Snapshot pad (Render Disk):")
        st.code(SNAPSHOT_PATH, language="text")

        st.write("Portfolio history pad (Render Disk):")
        st.code(PORTFOLIO_HISTORY_CSV, language="text")

    with colR:
        st.subheader("Acties")
        if st.button("🔄 Snapshot verversen (saldo + EUR waardes)", use_container_width=True):
            try:
                snap = build_snapshot()
                st.success("Snapshot aangemaakt/ververst ✅")
                st.caption("Tip: refresh de pagina (F5) als je direct alles wilt zien.")
                st.json({
                    "eur_available": snap.get("eur_available"),
                    "crypto_assets_eur": snap.get("crypto_assets_eur"),
                    "total_portfolio_eur": snap.get("total_portfolio_eur"),
                    "ts": snap.get("ts")
                })
            except Exception as e:
                st.error(f"Snapshot fout: {e}")
                st.caption(f"API_KEY len: {len(API_KEY)} | API_SECRET len: {len(API_SECRET)}")

    snap, err = safe_read_json(SNAPSHOT_PATH)
    st.divider()

    if err:
        st.warning(err)
    else:
        if snap and snap.get("status") == "OK":
            eur_available = float(snap.get("eur_available", 0) or 0)
            crypto_assets_eur = float(snap.get("crypto_assets_eur", 0) or 0)
            total_portfolio_eur = float(snap.get("total_portfolio_eur", 0) or 0)

            # % changes (op basis van history)
            df_hist, herr = load_portfolio_history()
            d1 = compute_change_pct(df_hist, 1) if herr is None else None
            d7 = compute_change_pct(df_hist, 7) if herr is None else None

            c1, c2, c3 = st.columns(3)
            c1.metric("Beschikbaar saldo (EUR)", f"€ {eur_available:.2f}")
            c2.metric("Crypto assets", f"€ {crypto_assets_eur:.2f}", delta=(f"{d1:.2f}% (1D)" if d1 is not None else None))
            c3.metric("Totale waarde portfolio", f"€ {total_portfolio_eur:.2f}", delta=(f"{d7:.2f}% (7D)" if d7 is not None else None))

            st.subheader("Assets (alleen > 0) — inclusief waarde in €")
            assets = snap.get("assets", []) or []
            df_assets = pd.DataFrame(assets)

            # Zorg dat kolommen altijd bestaan (nooit KeyError)
            for col in ["symbol", "available", "inOrder", "total", "price_eur", "eur_value", "price_route"]:
                if col not in df_assets.columns:
                    df_assets[col] = None

            # nette formatting
            df_assets["price_eur"] = pd.to_numeric(df_assets["price_eur"], errors="coerce")
            df_assets["eur_value"] = pd.to_numeric(df_assets["eur_value"], errors="coerce").fillna(0.0)

            # toon tabel
            st.dataframe(
                df_assets[["symbol", "available", "inOrder", "total", "price_eur", "eur_value", "price_route"]],
                use_container_width=True,
                hide_index=True
            )

            # pie chart verdeling (excl EUR)
            df_pie = df_assets[(df_assets["symbol"] != "EUR") & (df_assets["eur_value"] > 0)].copy()
            if not df_pie.empty:
                st.subheader("Asset-verdeling (EUR waarde)")
                fig_pie = px.pie(df_pie, names="symbol", values="eur_value")
                st.plotly_chart(fig_pie, use_container_width=True)
            else:
                st.info("Nog geen EUR-waardes voor coins gevonden (of alles is 0).")

            # portfolio koersgrafiek (Bitvavo-style)
            st.subheader("Portfolio koersgrafiek (history)")
            if herr:
                st.info(herr)
            else:
                window = st.radio("Periode", ["1D", "7D", "30D", "ALL"], horizontal=True)
                dfw = filter_history(df_hist, window)

                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=dfw["ts_dt"],
                    y=dfw["total_portfolio_eur"],
                    mode="lines",
                    name="Total portfolio (€)"
                ))
                fig.update_layout(
                    height=420,
                    margin=dict(l=10, r=10, t=10, b=10),
                    yaxis_title="€",
                    xaxis_title=""
                )
                st.plotly_chart(fig, use_container_width=True)

        else:
            st.warning("Snapshot bestaat, maar status is niet OK.")

    st.info(
        "Let op: als je in je browser naar `https://api.bitvavo.com/v2/balance` gaat, krijg je altijd een auth-fout. "
        "Dat is normaal — private endpoints werken alleen met headers/signature vanuit je code."
    )


# =========================
# TAB 2: Koers & Trades
# =========================
with tabs[1]:
    st.subheader("Koers & Trades (portfolio + trade markers)")

    df_hist, herr = load_portfolio_history()
    if herr:
        st.info(herr)
    else:
        window = st.radio("Periode (koers)", ["1D", "7D", "30D", "ALL"], horizontal=True, key="window_koers")
        dfw = filter_history(df_hist, window)

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=dfw["ts_dt"],
            y=dfw["total_portfolio_eur"],
            mode="lines",
            name="Portfolio (€)"
        ))

        # trades markers (als paper_trades.csv bestaat en timestamp kolom herkend wordt)
        df_trades, terr = safe_read_csv(PAPER_TRADES_CSV)
        if terr is None and df_trades is not None and not df_trades.empty:
            cols = [c.lower() for c in df_trades.columns]
            colmap = {c.lower(): c for c in df_trades.columns}

            ts_col = None
            for candidate in ["timestamp", "time", "ts", "date", "datetime"]:
                if candidate in cols:
                    ts_col = colmap[candidate]
                    break

            side_col = None
            for candidate in ["side", "action", "type"]:
                if candidate in cols:
                    side_col = colmap[candidate]
                    break

            if ts_col:
                tmp = df_trades.copy()
                tmp["_ts"] = pd.to_datetime(tmp[ts_col], errors="coerce", utc=True)
                tmp = tmp.dropna(subset=["_ts"])

                # plaats markers op de portfolio lijn (y = nearest portfolio value)
                # simpel: y = laatste bekende portfolio waarde vóór trade tijd
                df_port = df_hist[["ts_dt", "total_portfolio_eur"]].copy()

                def nearest_port_value(t):
                    before = df_port[df_port["ts_dt"] <= t]
                    if before.empty:
                        return None
                    return float(before["total_portfolio_eur"].iloc[-1])

                tmp["_y"] = tmp["_ts"].apply(nearest_port_value)
                tmp = tmp.dropna(subset=["_y"])

                label = tmp[side_col].astype(str) if side_col else pd.Series(["trade"] * len(tmp))
                fig.add_trace(go.Scatter(
                    x=tmp["_ts"],
                    y=tmp["_y"],
                    mode="markers",
                    name="Trades",
                    text=label,
                    hovertemplate="Trade: %{text}<br>%{x}<br>€ %{y:.2f}<extra></extra>"
                ))

        fig.update_layout(
            height=460,
            margin=dict(l=10, r=10, t=10, b=10),
            yaxis_title="€",
            xaxis_title=""
        )
        st.plotly_chart(fig, use_container_width=True)

    st.caption("Tip: wil je markers per coin (BUY/SELL) exact? Zet dan in paper_trades.csv altijd: timestamp, symbol, side, price, amount, pnl.")


# =========================
# TAB 3: Trades & Performance
# =========================
with tabs[2]:
    st.subheader("Trades & Performance (Paper)")
    df_trades, err = safe_read_csv(PAPER_TRADES_CSV)

    left, right = st.columns([2, 1])

    with right:
        st.write("Bronbestand:")
        st.code(PAPER_TRADES_CSV, language="text")
        meta = file_meta(PAPER_TRADES_CSV)
        if meta["exists"]:
            st.success(f"Trades CSV gevonden ✅ | Laatst aangepast: {meta['modified']}")
        else:
            st.warning("Trades CSV bestaat nog niet. Zodra paper_trader trades logt, komt dit vanzelf.")

    if err:
        st.warning(err)
    else:
        st.dataframe(df_trades.tail(50), use_container_width=True)

        cols = [c.lower() for c in df_trades.columns]
        colmap = {c.lower(): c for c in df_trades.columns}

        pnl_col = None
        for candidate in ["pnl", "profit", "pnl_eur", "pnl_usdt"]:
            if candidate in cols:
                pnl_col = colmap[candidate]
                break

        ts_col = None
        for candidate in ["timestamp", "time", "ts", "date", "datetime"]:
            if candidate in cols:
                ts_col = colmap[candidate]
                break

        if pnl_col:
            pnl_series = pd.to_numeric(df_trades[pnl_col], errors="coerce").fillna(0)
            total_pnl = float(pnl_series.sum())
            wins = int((pnl_series > 0).sum())
            losses = int((pnl_series < 0).sum())
            n = int(len(pnl_series))
            winrate = (wins / n * 100) if n else 0.0

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Totaal PnL", f"{total_pnl:.2f}")
            c2.metric("Trades", f"{n}")
            c3.metric("Winrate", f"{winrate:.1f}%")
            c4.metric("W/L", f"{wins}/{losses}")

            equity = pnl_series.cumsum()
            st.subheader("Equity curve (cumulatieve PnL)")
            st.line_chart(equity)

            if ts_col:
                try:
                    tmp = df_trades.copy()
                    tmp["_ts"] = pd.to_datetime(tmp[ts_col], errors="coerce", utc=True)
                    tmp = tmp.dropna(subset=["_ts"])
                    tmp["_date"] = tmp["_ts"].dt.date
                    daily = tmp.groupby("_date")[pnl_col].apply(
                        lambda s: pd.to_numeric(s, errors="coerce").fillna(0).sum()
                    )
                    st.subheader("Daily PnL")
                    st.line_chart(daily)
                except Exception:
                    st.info("Daily PnL kon niet opgebouwd worden (timestamp formaat wijkt af).")
        else:
            st.info(
                "Ik kan nog geen PnL grafieken maken, omdat je CSV geen herkenbare PnL kolom heeft. "
                "Tip: zorg dat `paper_trades.csv` een kolom heeft zoals `pnl` of `profit`."
            )


# =========================
# TAB 4: Bot Status / Masterlijst
# =========================
with tabs[3]:
    st.subheader("Bot Status / Masterlijst (wat we willen zien + of het al werkt)")

    st.markdown("""
**Dit is de masterlijst die jij wilde (dashboard moet dit uiteindelijk allemaal kunnen tonen):**
- ✅ **Saldo & Assets** (Bitvavo snapshot + EUR-waardes)
- ✅ **Portfolio history** (koersgrafiek + % verandering)
- ✅ **Pending approvals** (Pre-BUY wachtrij + expiry + gekozen bedrag)
- ✅ **Open posities** (paper_state / live later)
- ✅ **Trades history** (paper_trades.csv)
- ✅ **Performance metrics** (PnL, winrate, equity, drawdown, streaks)
- ✅ **Bot health** (laatste run per service/script + errors)
- ✅ **AI usage** (calls per dag/maand + kosten-guardrails)
- ✅ **Signal kwaliteit** (scores, welke methode/filters, hitrate per score-band)
- ✅ **R-metrics** (R bij exit, partials 40/60, STRUCTUUR-MODE activaties)
- ✅ **Shadow trades** (later stap: alles loggen ook als je ‘NO’ zegt)
- ✅ **Weekly reporting** (later stap: weekoverzicht + learnings)
""")

    st.write("### Bestandsstatus (Render Disk)")
    paths = [
        ("Snapshot (Bitvavo)", SNAPSHOT_PATH),
        ("Portfolio history", PORTFOLIO_HISTORY_CSV),
        ("Pending approvals", PENDING_PATH),
        ("Paper state", PAPER_STATE_PATH),
        ("Paper trades CSV", PAPER_TRADES_CSV),
        ("AI usage", AI_USAGE_PATH),
        ("Prebuy state", PREBUY_STATE_PATH),
        ("Prebuy payload", PREBUY_PAYLOAD_PATH),
    ]

    status_rows = []
    for name, p in paths:
        m = file_meta(p)
        status_rows.append({
            "item": name,
            "exists": m.get("exists", False),
            "path": p,
            "modified": m.get("modified", ""),
            "size_kb": m.get("size_kb", ""),
        })
    st.dataframe(pd.DataFrame(status_rows), use_container_width=True, hide_index=True)

    st.divider()

    st.write("### Pending approvals (preview)")
    pending, err = safe_read_json(PENDING_PATH)
    st.json(pending if err is None else {"info": err})

    st.write("### Paper state (preview)")
    pstate, err = safe_read_json(PAPER_STATE_PATH)
    st.json(pstate if err is None else {"info": err})

    st.write("### AI usage (preview)")
    aiu, err = safe_read_json(AI_USAGE_PATH)
    st.json(aiu if err is None else {"info": err})

    st.success("👉 Dit tabblad is jouw controlepaneel: je ziet direct welke onderdelen al data wegschrijven en welke nog leeg zijn.")
