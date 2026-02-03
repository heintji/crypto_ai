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


# =========================================================
# Config (Render-proof)
# =========================================================
API_KEY = (os.getenv("BITVAVO_API_KEY", "") or "").strip().strip('"').strip("'")
API_SECRET = (os.getenv("BITVAVO_API_SECRET", "") or "").strip().strip('"').strip("'")

BASE_URL_PRIVATE = "https://api.bitvavo.com"   # private endpoints
BASE_URL_PUBLIC = "https://api.bitvavo.com"    # public endpoints
ACCESS_WINDOW_MS = "10000"

# Render Disk paths (persistent)
SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", "/data/account_snapshot.json")
HISTORY_PATH = os.getenv("PORTFOLIO_HISTORY_PATH", "/data/portfolio_history.csv")

PENDING_PATH = os.getenv("PENDING_PATH", "/data/pending_approvals.json")
PAPER_STATE_PATH = os.getenv("PAPER_STATE_PATH", "/data/paper_state.json")
PAPER_TRADES_CSV = os.getenv("PAPER_TRADES_CSV", "/data/paper_trades.csv")

AI_USAGE_PATH = os.getenv("AI_USAGE_PATH", "/data/ai_usage.json")
PREBUY_STATE_PATH = os.getenv("PREBUY_STATE_PATH", "/data/prebuy_state.json")
PREBUY_PAYLOAD_PATH = os.getenv("PREBUY_PAYLOAD_PATH", "/data/prebuy_payload.json")


# =========================================================
# Helpers
# =========================================================
def now_iso() -> str:
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

def to_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


# =========================================================
# Bitvavo Private Request (SIGNING)
# =========================================================
def bitvavo_request(method: str, path: str, body: str = ""):
    """
    signature = HMAC_SHA256(secret, timestamp + METHOD + path + body)
    path moet exact zijn incl /v2 (bijv "/v2/balance")
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

    url = f"{BASE_URL_PRIVATE}{path}"

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


# =========================================================
# Bitvavo Public Helpers (prijzen + candles)
# =========================================================
@st.cache_data(ttl=60)
def get_price_eur(symbol: str) -> float:
    """
    Haalt live prijs op via public endpoint:
    /v2/ticker/price?market=BTC-EUR
    """
    if symbol == "EUR":
        return 1.0

    market = f"{symbol}-EUR"
    url = f"{BASE_URL_PUBLIC}/v2/ticker/price"
    r = requests.get(url, params={"market": market}, timeout=15)

    if r.status_code >= 400:
        return 0.0

    try:
        data = r.json()
        # verwacht: {"market":"BTC-EUR","price":"..."}
        return to_float(data.get("price", 0.0), 0.0)
    except Exception:
        return 0.0


@st.cache_data(ttl=120)
def get_candles_eur(symbol: str, interval: str = "1h", limit: int = 240) -> pd.DataFrame:
    """
    Bitvavo public candles:
    /v2/BTC-EUR/candles?interval=1h&limit=240
    Return: [ [timestamp, open, high, low, close, volume], ... ]
    """
    market = f"{symbol}-EUR"
    url = f"{BASE_URL_PUBLIC}/v2/{market}/candles"
    r = requests.get(url, params={"interval": interval, "limit": limit}, timeout=20)
    if r.status_code >= 400:
        return pd.DataFrame()

    try:
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["ts", "open", "high", "low", "close"])
        return df.sort_values("ts")
    except Exception:
        return pd.DataFrame()


# =========================================================
# Snapshot builder (balance -> enriched assets -> totals)
# =========================================================
def build_snapshot():
    balances = bitvavo_request("GET", "/v2/balance")

    assets = []
    eur_available = 0.0

    for row in balances:
        symbol = row.get("symbol")
        available = to_float(row.get("available", 0) or 0)
        in_order = to_float(row.get("inOrder", 0) or 0)
        total = available + in_order

        if symbol == "EUR":
            eur_available = available

        if total > 0:
            assets.append({
                "symbol": symbol,
                "available": available,
                "inOrder": in_order,
                "total": total
            })

    # Verrijk: price_eur + eur_value per asset
    enriched = []
    crypto_assets_value = 0.0

    for a in assets:
        sym = a["symbol"]
        price = get_price_eur(sym)
        eur_value = (a["total"] * price) if sym != "EUR" else a["total"]
        row = {**a, "price_eur": price, "eur_value": eur_value}
        enriched.append(row)

        if sym != "EUR":
            crypto_assets_value += eur_value

    portfolio_total = eur_available + crypto_assets_value

    snapshot = {
        "status": "OK",
        "ts": now_iso(),
        "eur_available": eur_available,
        "crypto_assets_value": crypto_assets_value,
        "portfolio_total": portfolio_total,
        "assets": sorted(enriched, key=lambda x: (x["symbol"] != "EUR", x["symbol"])),
    }

    ensure_parent_dir(SNAPSHOT_PATH)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    # History wegschrijven (voor portfolio grafiek & % changes)
    append_portfolio_history(snapshot)

    return snapshot


def append_portfolio_history(snapshot: dict):
    ensure_parent_dir(HISTORY_PATH)
    ts = snapshot.get("ts")
    eur_available = snapshot.get("eur_available", 0.0)
    crypto_assets_value = snapshot.get("crypto_assets_value", 0.0)
    portfolio_total = snapshot.get("portfolio_total", 0.0)

    row = pd.DataFrame([{
        "ts": ts,
        "eur_available": eur_available,
        "crypto_assets_value": crypto_assets_value,
        "portfolio_total": portfolio_total
    }])

    if os.path.exists(HISTORY_PATH):
        try:
            existing = pd.read_csv(HISTORY_PATH)
            # voorkom dubbele timestamps
            if "ts" in existing.columns and ts in set(existing["ts"].astype(str).tolist()):
                return
        except Exception:
            pass
        row.to_csv(HISTORY_PATH, mode="a", header=False, index=False)
    else:
        row.to_csv(HISTORY_PATH, index=False)


def load_portfolio_history() -> pd.DataFrame:
    if not os.path.exists(HISTORY_PATH):
        return pd.DataFrame()
    try:
        df = pd.read_csv(HISTORY_PATH)
        df["ts"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
        df = df.dropna(subset=["ts"]).sort_values("ts")
        for c in ["eur_available", "crypto_assets_value", "portfolio_total"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        return df
    except Exception:
        return pd.DataFrame()


def pct_change_from_history(hist: pd.DataFrame, days_back: int) -> float | None:
    """
    pctl = (now - value(days_back)) / value(days_back) * 100
    """
    if hist.empty or "portfolio_total" not in hist.columns:
        return None

    now_ts = hist["ts"].max()
    target_ts = now_ts - timedelta(days=days_back)

    # zoek dichtstbijzijnde punt <= target_ts
    past = hist[hist["ts"] <= target_ts].tail(1)
    if past.empty:
        return None

    past_val = float(past["portfolio_total"].iloc[0])
    if past_val <= 0:
        return None

    now_val = float(hist[hist["portfolio_total"].notna()]["portfolio_total"].iloc[-1])
    return (now_val - past_val) / past_val * 100.0


# =========================================================
# Trades helpers (markers op chart)
# =========================================================
def normalize_trades_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Probeer trades kolommen te herkennen zonder dat jij nu alles hoeft te hernoemen.
    We zoeken o.a.: ts/timestamp/date, side/buy_sell, symbol/coin/market, price, amount/qty.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    cols_lower = {c.lower(): c for c in df.columns}

    def pick(*names):
        for n in names:
            if n in cols_lower:
                return cols_lower[n]
        return None

    col_ts = pick("timestamp", "time", "ts", "date", "datetime")
    col_side = pick("side", "type", "action", "buy_sell")
    col_symbol = pick("symbol", "coin", "market", "pair")
    col_price = pick("price", "entry", "fill_price", "avg_price")
    col_qty = pick("amount", "qty", "size", "volume")

    out = df.copy()

    if col_ts:
        out["_ts"] = pd.to_datetime(out[col_ts], errors="coerce", utc=True)
    else:
        out["_ts"] = pd.NaT

    out["_side"] = out[col_side].astype(str).str.upper() if col_side else ""
    out["_symbol"] = out[col_symbol].astype(str).str.upper() if col_symbol else ""
    out["_price"] = pd.to_numeric(out[col_price], errors="coerce") if col_price else None
    out["_qty"] = pd.to_numeric(out[col_qty], errors="coerce") if col_qty else None

    out = out.dropna(subset=["_ts"])
    return out


def filter_trades_for_symbol(trades: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    We matchen grof:
    - als _symbol exact "BTC" of "BTC-EUR" of "BTCUSDT" etc bevat.
    """
    if trades is None or trades.empty:
        return pd.DataFrame()
    s = symbol.upper()
    mask = trades["_symbol"].str.contains(s, na=False)
    return trades[mask].copy()


# =========================================================
# UI
# =========================================================
st.set_page_config(page_title="Crypto AI Dashboard", layout="wide")
st.title("📊 Crypto AI Dashboard")
st.caption("Render-proof dashboard: Bitvavo saldo + portfolio waarde + grafieken + trades/monitoring.")

tabs = st.tabs(["💶 Saldo & Portfolio", "📉 Koers & Trades", "📈 Trades & Performance", "🧠 Bot Status / Masterlijst"])


# =========================================================
# TAB 1: Saldo & Portfolio
# =========================================================
with tabs[0]:
    colL, colR = st.columns([2, 1])

    with colL:
        st.subheader("Snapshot (Bitvavo)")
        st.write("Snapshot pad (Render Disk):")
        st.code(SNAPSHOT_PATH, language="text")

        meta = file_meta(SNAPSHOT_PATH)
        if meta["exists"]:
            st.success(f"Snapshot gevonden ✅  | Laatst aangepast: {meta['modified']} | Grootte: {meta['size_kb']} KB")
        else:
            st.warning("Nog geen snapshot gevonden. Klik op **Snapshot verversen**.")

    with colR:
        st.subheader("Acties")
        if st.button("🔄 Snapshot verversen", use_container_width=True):
            try:
                snap = build_snapshot()
                st.success("Snapshot aangemaakt/ververst ✅")
                st.caption("Tip: dit bouwt ook automatisch je portfolio-historie op voor grafieken & %.")
                st.json(snap)
            except Exception as e:
                st.error(f"Snapshot fout: {e}")
                st.caption(f"API_KEY len: {len(API_KEY)} | API_SECRET len: {len(API_SECRET)}")

    snap, err = safe_read_json(SNAPSHOT_PATH)
    st.divider()

    if err:
        st.warning(err)
    else:
        if snap and snap.get("status") == "OK":
            eur_available = float(snap.get("eur_available", 0.0) or 0.0)
            crypto_assets_value = float(snap.get("crypto_assets_value", 0.0) or 0.0)
            portfolio_total = float(snap.get("portfolio_total", 0.0) or 0.0)

            # ✅ Dit zijn precies jouw Bitvavo waarden die je terug wil zien
            c1, c2, c3 = st.columns(3)
            c1.metric("Beschikbaar saldo (EUR)", f"€ {eur_available:.2f}")
            c2.metric("Crypto assets", f"€ {crypto_assets_value:.2f}")
            c3.metric("Totale waarde portfolio", f"€ {portfolio_total:.2f}")

            assets = snap.get("assets", [])
            df_assets = pd.DataFrame(assets) if assets else pd.DataFrame()

            if not df_assets.empty:
                # nette volgorde + rounding
                for c in ["available", "inOrder", "total", "price_eur", "eur_value"]:
                    if c in df_assets.columns:
                        df_assets[c] = pd.to_numeric(df_assets[c], errors="coerce").fillna(0)

                st.subheader("Assets (alleen > 0) — inclusief waarde in €")
                show_cols = [c for c in ["symbol", "available", "inOrder", "total", "price_eur", "eur_value"] if c in df_assets.columns]
                st.dataframe(df_assets[show_cols], use_container_width=True, hide_index=True)

                # Asset verdeling (pie chart) op eur_value (ex EUR)
                df_pie = df_assets[df_assets["symbol"] != "EUR"].copy()
                df_pie = df_pie[df_pie["eur_value"] > 0]
                if not df_pie.empty:
                    st.subheader("Asset-verdeling (op basis van € waarde)")
                    fig_pie = px.pie(df_pie, names="symbol", values="eur_value", hole=0.45)
                    st.plotly_chart(fig_pie, use_container_width=True)
                else:
                    st.info("Geen crypto assets waarde beschikbaar voor pie chart (mogelijk geen EUR-paren of alles = 0).")
            else:
                st.info("Geen assets gevonden (of alles is 0).")

            # Portfolio koersgrafiek (historie)
            hist = load_portfolio_history()
            if not hist.empty and "portfolio_total" in hist.columns:
                st.subheader("Portfolio koersgrafiek (Bitvavo-stijl)")

                pct_1d = pct_change_from_history(hist, 1)
                pct_7d = pct_change_from_history(hist, 7)

                p1, p2 = st.columns(2)
                p1.metric("% verandering (dag)", "-" if pct_1d is None else f"{pct_1d:+.2f}%")
                p2.metric("% verandering (week)", "-" if pct_7d is None else f"{pct_7d:+.2f}%")

                fig_line = go.Figure()
                fig_line.add_trace(go.Scatter(
                    x=hist["ts"], y=hist["portfolio_total"],
                    mode="lines", name="Portfolio total (€)"
                ))
                fig_line.update_layout(
                    height=360,
                    margin=dict(l=10, r=10, t=10, b=10),
                    xaxis_title="Tijd",
                    yaxis_title="€",
                    legend=dict(orientation="h")
                )
                st.plotly_chart(fig_line, use_container_width=True)
                st.caption(f"Historie bestand: {HISTORY_PATH}")
            else:
                st.info("Nog geen portfolio-historie. Klik een paar keer op **Snapshot verversen** (bv. verspreid over tijd).")

        else:
            st.warning("Snapshot bestaat, maar status is niet OK.")

    st.info(
        "Let op: als je in je browser naar `https://api.bitvavo.com/v2/balance` gaat, krijg je altijd auth-fout. "
        "Dat is normaal — private endpoints werken alleen met headers/signature vanuit je code."
    )


# =========================================================
# TAB 2: Koers & Trades (coin chart + markers)
# =========================================================
with tabs[1]:
    st.subheader("Koers & Trades")

    snap, _ = safe_read_json(SNAPSHOT_PATH)
    assets = (snap or {}).get("assets", [])
    df_assets = pd.DataFrame(assets) if assets else pd.DataFrame()

    # coin selector
    coins = ["BTC", "ETH", "ADA", "SOL", "XRP", "BNB", "DOGE"]
    if not df_assets.empty and "symbol" in df_assets.columns:
        # zet coins uit je echte holdings bovenaan
        holdings = [s for s in df_assets["symbol"].astype(str).tolist() if s and s != "EUR"]
        coins = sorted(list(dict.fromkeys(holdings + coins)))

    sel = st.selectbox("Kies coin voor koersgrafiek (EUR)", coins, index=0)
    interval = st.selectbox("Interval", ["15m", "1h", "4h", "1d"], index=1)
    limit = st.slider("Aantal candles", 60, 500, 240, 20)

    df_c = get_candles_eur(sel, interval=interval, limit=limit)
    if df_c.empty:
        st.warning("Kon geen candles ophalen voor deze coin. (Bestaat het EUR-market op Bitvavo?)")
    else:
        fig = go.Figure(data=[go.Candlestick(
            x=df_c["ts"],
            open=df_c["open"],
            high=df_c["high"],
            low=df_c["low"],
            close=df_c["close"],
            name=f"{sel}-EUR"
        )])

        # trades markers (paper_trades.csv)
        df_trades, terr = safe_read_csv(PAPER_TRADES_CSV)
        if terr:
            st.caption("Trades markers: paper_trades.csv nog niet gevonden of leeg.")
        else:
            tnorm = normalize_trades_df(df_trades)
            tsel = filter_trades_for_symbol(tnorm, sel)
            if not tsel.empty:
                # alleen markers als we price hebben
                has_price = tsel["_price"] is not None and tsel["_price"].notna().any()
                if has_price:
                    buys = tsel[tsel["_side"].str.contains("BUY", na=False)].copy()
                    sells = tsel[tsel["_side"].str.contains("SELL", na=False)].copy()

                    if not buys.empty:
                        fig.add_trace(go.Scatter(
                            x=buys["_ts"], y=buys["_price"],
                            mode="markers", name="BUY",
                            marker=dict(symbol="triangle-up", size=10)
                        ))
                    if not sells.empty:
                        fig.add_trace(go.Scatter(
                            x=sells["_ts"], y=sells["_price"],
                            mode="markers", name="SELL",
                            marker=dict(symbol="triangle-down", size=10)
                        ))
                else:
                    st.caption("Trades markers: ik zie geen herkenbare price-kolom in paper_trades.csv.")
            else:
                st.caption("Trades markers: geen trades gevonden voor deze coin (op basis van symbol/market match).")

        fig.update_layout(
            height=520,
            margin=dict(l=10, r=10, t=10, b=10),
            xaxis_title="Tijd",
            yaxis_title="Prijs (EUR)",
            legend=dict(orientation="h")
        )
        st.plotly_chart(fig, use_container_width=True)

        st.caption("Tip: als je wilt dat markers 100% werken, zorg dat paper_trades.csv kolommen heeft zoals: timestamp, side (BUY/SELL), symbol/coin/market, price.")


# =========================================================
# TAB 3: Trades & Performance (Paper)
# =========================================================
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
                "Tip: voeg een kolom toe zoals `pnl` of `profit` in paper_trades.csv."
            )


# =========================================================
# TAB 4: Bot Status / Masterlijst
# =========================================================
with tabs[3]:
    st.subheader("Bot Status / Masterlijst")

    st.markdown("""
**Controlepaneel — dit is wat jouw bot/stack uiteindelijk moet kunnen tonen:**
- ✅ **Saldo & Assets** (Bitvavo snapshot + € waardes)
- ✅ **Portfolio total + % dag/week + grafiek**
- ✅ **Asset-verdeling (pie)**
- ✅ **Koersgrafiek + trades markers**
- ✅ **Pending approvals** (Pre-BUY wachtrij + expiry + bedrag)
- ✅ **Open posities** (paper_state / later live)
- ✅ **Trades history** (paper_trades.csv)
- ✅ **Performance metrics** (PnL, winrate, equity, daily PnL)
- ✅ **Bot health** (files/laatste updates)
- ✅ **AI usage** (calls + kosten-guardrails)
- ✅ **Signal kwaliteit / R-metrics / STRUCTUUR-MODE / shadow trades** (volgende fases)
""")

    st.write("### Bestandsstatus (Render Disk)")
    paths = [
        ("Snapshot (Bitvavo)", SNAPSHOT_PATH),
        ("Portfolio history", HISTORY_PATH),
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
    if err:
        st.info(err)
    else:
        st.json(pending)

    st.write("### Paper state (preview)")
    pstate, err = safe_read_json(PAPER_STATE_PATH)
    if err:
        st.info(err)
    else:
        st.json(pstate)

    st.write("### AI usage (preview)")
    aiu, err = safe_read_json(AI_USAGE_PATH)
    if err:
        st.info(err)
    else:
        st.json(aiu)

    st.success("✅ Dit dashboard is nu stabiel: geen matplotlib, geen KeyErrors, en alles draait Render-proof via /data.")
