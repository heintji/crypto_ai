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
import plotly.graph_objects as go


# =========================
# Config
# =========================
API_KEY = (os.getenv("BITVAVO_API_KEY", "") or "").strip().strip('"').strip("'")
API_SECRET = (os.getenv("BITVAVO_API_SECRET", "") or "").strip().strip('"').strip("'")

# Render Disk paths (persistent)
SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", "/data/account_snapshot.json")
SNAPSHOT_HISTORY_PATH = os.getenv("SNAPSHOT_HISTORY_PATH", "/data/account_snapshot_history.jsonl")

PENDING_PATH = os.getenv("PENDING_PATH", "/data/pending_approvals.json")
PAPER_STATE_PATH = os.getenv("PAPER_STATE_PATH", "/data/paper_state.json")

PAPER_TRADES_CSV = os.getenv("PAPER_TRADES_CSV", "/data/paper_trades.csv")
AI_USAGE_PATH = os.getenv("AI_USAGE_PATH", "/data/ai_usage.json")
PREBUY_STATE_PATH = os.getenv("PREBUY_STATE_PATH", "/data/prebuy_state.json")
PREBUY_PAYLOAD_PATH = os.getenv("PREBUY_PAYLOAD_PATH", "/data/prebuy_payload.json")

BASE_URL = "https://api.bitvavo.com"
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

def append_jsonl(path: str, obj: dict):
    ensure_parent_dir(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def read_jsonl(path: str, max_rows: int = 5000):
    if not os.path.exists(path):
        return pd.DataFrame()
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= max_rows:
                    break
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()


# =========================
# Bitvavo Private Request (SIGNING)
# =========================
def bitvavo_request(method: str, path: str, body: str = ""):
    """
    Bitvavo private request:
    signature = HMAC_SHA256(secret, timestamp + METHOD + path + body)

    ✅ path moet exact zijn inclusief /v2
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

    url = f"{BASE_URL}{path}"

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
# Bitvavo Public Helpers (geen signing)
# =========================
def public_get(path: str, params: dict | None = None):
    url = f"{BASE_URL}{path}"
    r = requests.get(url, params=params or {}, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Public Bitvavo error {r.status_code}: {r.text}")
    return r.json()

def get_price_eur(symbol: str):
    """
    Probeer market SYMBOL-EUR te prijzen via /v2/ticker/price.
    Als market niet bestaat -> None.
    """
    if symbol == "EUR":
        return 1.0
    market = f"{symbol}-EUR"
    try:
        data = public_get("/v2/ticker/price", params={"market": market})
        # response is dict met market/price of list; we vangen beide af
        if isinstance(data, list) and len(data) > 0:
            price = float(data[0].get("price"))
        elif isinstance(data, dict):
            price = float(data.get("price"))
        else:
            return None
        return price
    except Exception:
        return None

def get_ohlc(market: str, interval: str = "1h", limit: int = 240):
    """
    Candles: /v2/candles?market=BTC-EUR&interval=1h&limit=240
    Return DataFrame with columns: ts, open, high, low, close, volume
    """
    data = public_get("/v2/candles", params={"market": market, "interval": interval, "limit": limit})
    # Bitvavo candles zijn arrays: [timestamp, open, high, low, close, volume]
    rows = []
    for c in data:
        rows.append({
            "ts": pd.to_datetime(int(c[0]), unit="ms", utc=True),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
        })
    df = pd.DataFrame(rows).sort_values("ts")
    return df


# =========================
# Snapshot builder + portfolio waardering
# =========================
def build_snapshot():
    """
    Haal balans op (private) + prijs EUR per asset (public) en schrijf:
    - /data/account_snapshot.json
    - append naar /data/account_snapshot_history.jsonl (voor koersgrafiek + % dag/week)
    """
    balances = bitvavo_request("GET", "/v2/balance")

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

        if total > 0:
            price_eur = get_price_eur(symbol)
            eur_value = float(total * price_eur) if price_eur is not None else None

            if symbol != "EUR" and eur_value is not None:
                crypto_assets_eur += eur_value

            assets.append({
                "symbol": symbol,
                "available": available,
                "inOrder": in_order,
                "total": total,
                "price_eur": price_eur,
                "eur_value": eur_value,
            })

    total_portfolio_eur = eur_available + (crypto_assets_eur or 0.0)

    snapshot = {
        "status": "OK",
        "ts": now_iso(),
        "eur_available": eur_available,
        "crypto_assets_eur": crypto_assets_eur,
        "total_portfolio_eur": total_portfolio_eur,
        "assets": sorted(assets, key=lambda x: (x["symbol"] != "EUR", x["symbol"])),
    }

    ensure_parent_dir(SNAPSHOT_PATH)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    # append history (voor grafiek & % changes)
    append_jsonl(SNAPSHOT_HISTORY_PATH, {
        "ts": snapshot["ts"],
        "eur_available": eur_available,
        "crypto_assets_eur": crypto_assets_eur,
        "total_portfolio_eur": total_portfolio_eur,
    })

    return snapshot


# =========================
# UI
# =========================
st.set_page_config(page_title="Crypto AI Dashboard", layout="wide")
st.title("📊 Crypto AI Dashboard")
st.caption("Render-proof dashboard: Bitvavo saldo + portfolio waarde + grafieken + trades/monitoring.")


tabs = st.tabs([
    "💶 Saldo & Portfolio",
    "📈 Koers & Trades",
    "🧾 Trades & Performance",
    "🧠 Bot Status / Masterlijst",
])


# =========================
# TAB 1: Saldo & Portfolio (Bitvavo-stijl)
# =========================
with tabs[0]:
    topL, topR = st.columns([2, 1])

    with topL:
        st.subheader("Snapshot & Data paths")
        st.write("Snapshot pad:")
        st.code(SNAPSHOT_PATH, language="text")
        st.write("Snapshot historie (voor koersgrafiek / % dag/week):")
        st.code(SNAPSHOT_HISTORY_PATH, language="text")

        meta = file_meta(SNAPSHOT_PATH)
        if meta["exists"]:
            st.success(f"Snapshot gevonden ✅ | Laatst aangepast: {meta['modified']} | Grootte: {meta['size_kb']} KB")
        else:
            st.warning("Nog geen snapshot gevonden. Klik rechts op **Snapshot verversen**.")

    with topR:
        st.subheader("Actie")
        if st.button("🔄 Snapshot verversen (Bitvavo)", use_container_width=True):
            try:
                snap_new = build_snapshot()
                st.success("Snapshot aangemaakt/ververst ✅")
                st.json(snap_new)
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
            total_portfolio_eur = float(snap.get("total_portfolio_eur", eur_available + crypto_assets_eur) or 0)

            c1, c2, c3 = st.columns(3)
            c1.metric("Beschikbaar saldo (EUR)", f"€ {eur_available:.2f}")
            c2.metric("Crypto assets", f"€ {crypto_assets_eur:.2f}")
            c3.metric("Totale waarde portfolio", f"€ {total_portfolio_eur:.2f}")

            # Assets tabel
            assets = snap.get("assets", [])
            if assets:
                df_assets = pd.DataFrame(assets)
                st.subheader("Assets (alleen > 0)")
                st.dataframe(
                    df_assets[["symbol", "available", "inOrder", "total", "price_eur", "eur_value"]],
                    use_container_width=True,
                    hide_index=True
                )

                # Pie chart asset verdeling op EUR value (zonder EUR)
                df_pie = df_assets.copy()
                df_pie = df_pie[df_pie["symbol"] != "EUR"].copy()
                df_pie = df_pie[df_pie["eur_value"].notna()].copy()
                if len(df_pie) > 0 and df_pie["eur_value"].sum() > 0:
                    st.subheader("Asset-verdeling (EUR waarde)")
                    fig_pie = px.pie(df_pie, names="symbol", values="eur_value", hole=0.45)
                    st.plotly_chart(fig_pie, use_container_width=True)
                else:
                    st.info("Asset-verdeling kan nog niet: er zijn (nog) geen EUR-prijzen gevonden voor je assets.")
            else:
                st.info("Geen assets gevonden (of alles is 0).")

            # Portfolio koersgrafiek o.b.v. historie
            hist = read_jsonl(SNAPSHOT_HISTORY_PATH)
            if not hist.empty and "total_portfolio_eur" in hist.columns:
                hist["ts"] = pd.to_datetime(hist["ts"], utc=True, errors="coerce")
                hist = hist.dropna(subset=["ts"]).sort_values("ts")

                st.subheader("Portfolio koersgrafiek (op basis van snapshots)")
                fig_line = px.line(hist, x="ts", y="total_portfolio_eur")
                fig_line.update_layout(yaxis_title="EUR", xaxis_title="Tijd")
                st.plotly_chart(fig_line, use_container_width=True)

                # % verandering dag/week (beste effort)
                latest = hist.iloc[-1]
                latest_val = float(latest["total_portfolio_eur"])

                def pct_change_from_ago(hours_ago: int):
                    cutoff = latest["ts"] - pd.Timedelta(hours=hours_ago)
                    past = hist[hist["ts"] <= cutoff]
                    if past.empty:
                        return None
                    past_val = float(past.iloc[-1]["total_portfolio_eur"])
                    if past_val == 0:
                        return None
                    return (latest_val - past_val) / past_val * 100.0

                d1 = pct_change_from_ago(24)
                w1 = pct_change_from_ago(24 * 7)

                pc1, pc2 = st.columns(2)
                pc1.metric("% verandering (24h)", "—" if d1 is None else f"{d1:.2f}%")
                pc2.metric("% verandering (7d)", "—" if w1 is None else f"{w1:.2f}%")

                if d1 is None or w1 is None:
                    st.caption("Tip: % dag/week komt vanzelf zodra je genoeg snapshot-historie hebt (minimaal 24 uur / 7 dagen).")
            else:
                st.info("Nog geen snapshot-historie voor portfolio grafiek. Klik eerst een paar keer op ‘Snapshot verversen’ (of laat cron draaien).")

        else:
            st.warning("Snapshot bestaat, maar status is niet OK.")

    st.info(
        "Let op: als je in je browser naar `https://api.bitvavo.com/v2/balance` gaat, krijg je altijd een auth-fout. "
        "Dat is normaal — private endpoints werken alleen met headers/signature vanuit je code."
    )


# =========================
# TAB 2: Koers & Trades (candles + markers)
# =========================
with tabs[1]:
    st.subheader("Koersgrafiek + Trades overlay (Bitvavo-stijl)")

    # Bepaal markets op basis van assets (indien snapshot aanwezig)
    snap, _ = safe_read_json(SNAPSHOT_PATH)
    markets = ["BTC-EUR", "ETH-EUR"]
    if snap and snap.get("assets"):
        syms = [a.get("symbol") for a in snap["assets"] if a.get("symbol") and a.get("symbol") != "EUR"]
        # unique + map naar EUR markets
        candidates = []
        for s in syms:
            candidates.append(f"{s}-EUR")
        # houd het netjes
        markets = sorted(list(dict.fromkeys(candidates)))[:50] or markets

    colA, colB, colC = st.columns([2, 1, 1])
    with colA:
        market = st.selectbox("Market", markets, index=0)
    with colB:
        interval = st.selectbox("Interval", ["15m", "30m", "1h", "4h", "1d"], index=2)
    with colC:
        limit = st.selectbox("Aantal candles", [120, 240, 480, 720], index=1)

    # Load candles
    try:
        dfc = get_ohlc(market=market, interval=interval, limit=int(limit))
        if dfc.empty:
            st.warning("Geen candles gevonden voor deze market.")
        else:
            fig = go.Figure(data=[
                go.Candlestick(
                    x=dfc["ts"],
                    open=dfc["open"],
                    high=dfc["high"],
                    low=dfc["low"],
                    close=dfc["close"],
                    name=market
                )
            ])

            # Trades overlay (beste effort: alleen als paper_trades.csv kolommen herkenbaar zijn)
            df_trades, terr = safe_read_csv(PAPER_TRADES_CSV)
            if terr is None and df_trades is not None and not df_trades.empty:
                cols = {c.lower(): c for c in df_trades.columns}

                # probeer timestamp
                ts_key = None
                for k in ["timestamp", "time", "ts", "date", "datetime"]:
                    if k in cols:
                        ts_key = cols[k]
                        break

                # probeer side
                side_key = None
                for k in ["side", "action", "type"]:
                    if k in cols:
                        side_key = cols[k]
                        break

                # probeer price
                price_key = None
                for k in ["price", "entry_price", "fill_price", "avg_price"]:
                    if k in cols:
                        price_key = cols[k]
                        break

                # probeer market/symbol
                sym_key = None
                for k in ["market", "symbol", "pair", "coin"]:
                    if k in cols:
                        sym_key = cols[k]
                        break

                if ts_key and side_key and price_key:
                    tmp = df_trades.copy()
                    tmp["_ts"] = pd.to_datetime(tmp[ts_key], errors="coerce", utc=True)

                    # filter op market indien mogelijk
                    if sym_key:
                        # normaliseer
                        tmp["_sym"] = tmp[sym_key].astype(str).str.upper()
                        m1 = market.upper()
                        # accept both BTC-EUR and BTCEUR / BTC_EUR
                        tmp = tmp[tmp["_sym"].str.replace("_", "-").str.replace("/", "-").str.contains(m1.split("-")[0], na=False)]

                    tmp = tmp.dropna(subset=["_ts"])
                    tmp["_side"] = tmp[side_key].astype(str).str.upper()
                    tmp["_price"] = pd.to_numeric(tmp[price_key], errors="coerce")
                    tmp = tmp.dropna(subset=["_price"])

                    buys = tmp[tmp["_side"].str.contains("BUY")]
                    sells = tmp[tmp["_side"].str.contains("SELL")]

                    if not buys.empty:
                        fig.add_trace(go.Scatter(
                            x=buys["_ts"],
                            y=buys["_price"],
                            mode="markers",
                            name="BUY",
                            marker=dict(size=10, symbol="triangle-up")
                        ))
                    if not sells.empty:
                        fig.add_trace(go.Scatter(
                            x=sells["_ts"],
                            y=sells["_price"],
                            mode="markers",
                            name="SELL",
                            marker=dict(size=10, symbol="triangle-down")
                        ))

            fig.update_layout(
                height=650,
                xaxis_title="Tijd",
                yaxis_title="Prijs (EUR)",
                legend_orientation="h",
                legend_y=1.02,
                margin=dict(l=10, r=10, t=40, b=10),
            )

            st.plotly_chart(fig, use_container_width=True)

    except Exception as e:
        st.error(f"Koersgrafiek fout: {e}")
        st.caption("Tip: sommige assets hebben geen EUR market op Bitvavo. Kies dan een andere market.")


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
        st.dataframe(df_trades.tail(100), use_container_width=True)

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
    st.subheader("Bot Status / Masterlijst (control panel)")

    st.markdown("""
**Masterlijst (dashboard moet dit uiteindelijk allemaal kunnen tonen):**
- ✅ **Saldo & Portfolio** (EUR + Crypto assets € + Totaal)
- ✅ **Portfolio koersgrafiek** (op basis van snapshot-historie)
- ✅ **% verandering** (24h / 7d) zodra historie aanwezig is
- ✅ **Asset verdeling** (pie chart) o.b.v. EUR-waardes
- ✅ **Koersgrafiek per market** (candles)
- ✅ **Trades overlay** (BUY/SELL markers) als CSV kolommen herkenbaar zijn
- ✅ **Pending approvals** (Pre-BUY wachtrij)
- ✅ **Open posities** (paper_state)
- ✅ **Trades history** (paper_trades.csv)
- ✅ **AI usage** (ai_usage.json)
""")

    st.write("### Bestandsstatus (Render Disk)")
    paths = [
        ("Snapshot (Bitvavo)", SNAPSHOT_PATH),
        ("Snapshot historie", SNAPSHOT_HISTORY_PATH),
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
    pending, perr = safe_read_json(PENDING_PATH)
    if perr:
        st.info(perr)
    else:
        st.json(pending)

    st.write("### Paper state (preview)")
    pstate, serr = safe_read_json(PAPER_STATE_PATH)
    if serr:
        st.info(serr)
    else:
        st.json(pstate)

    st.write("### AI usage (preview)")
    aiu, aerr = safe_read_json(AI_USAGE_PATH)
    if aerr:
        st.info(aerr)
    else:
        st.json(aiu)

    st.success("✅ Dit tabblad is jouw controlepaneel: je ziet direct welke onderdelen data wegschrijven en waar het nog leeg is.")
