import os
import json
import time
import hmac
import hashlib
from datetime import datetime, timezone

import requests
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt


# =========================
# Config
# =========================
API_KEY = (os.getenv("BITVAVO_API_KEY", "") or "").strip().strip('"').strip("'")
API_SECRET = (os.getenv("BITVAVO_API_SECRET", "") or "").strip().strip('"').strip("'")

DATA_DIR = os.getenv("DATA_DIR", "/data")

SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", f"{DATA_DIR}/account_snapshot.json")
PENDING_PATH = os.getenv("PENDING_PATH", f"{DATA_DIR}/pending_approvals.json")
PAPER_STATE_PATH = os.getenv("PAPER_STATE_PATH", f"{DATA_DIR}/paper_state.json")

PAPER_TRADES_CSV = os.getenv("PAPER_TRADES_CSV", f"{DATA_DIR}/paper_trades.csv")
AI_USAGE_PATH = os.getenv("AI_USAGE_PATH", f"{DATA_DIR}/ai_usage.json")
PREBUY_STATE_PATH = os.getenv("PREBUY_STATE_PATH", f"{DATA_DIR}/prebuy_state.json")
PREBUY_PAYLOAD_PATH = os.getenv("PREBUY_PAYLOAD_PATH", f"{DATA_DIR}/prebuy_payload.json")

PORTFOLIO_HISTORY_CSV = os.getenv("PORTFOLIO_HISTORY_CSV", f"{DATA_DIR}/portfolio_history.csv")
BENCHMARK_HISTORY_CSV = os.getenv("BENCHMARK_HISTORY_CSV", f"{DATA_DIR}/benchmark_btc_eur_history.csv")

BASE_URL = "https://api.bitvavo.com"
ACCESS_WINDOW_MS = "10000"
BITVAVO_TICKER_PRICE_URL = "https://api.bitvavo.com/v2/ticker/price"
BITVAVO_TICKER_24H_URL = "https://api.bitvavo.com/v2/ticker/24h"

BTC_BENCHMARK_MARKET = "BTC-EUR"


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

def get_public_prices():
    """market -> price (float)"""
    r = requests.get(BITVAVO_TICKER_PRICE_URL, timeout=15)
    r.raise_for_status()
    data = r.json()
    prices = {}
    for item in data:
        try:
            prices[item["market"]] = float(item["price"])
        except Exception:
            continue
    return prices

def get_public_24h():
    """list of 24h stats"""
    r = requests.get(BITVAVO_TICKER_24H_URL, timeout=15)
    r.raise_for_status()
    return r.json()

def asset_value_eur(symbol: str, amount: float, prices: dict) -> float:
    if symbol == "EUR":
        return float(amount)
    market = f"{symbol}-EUR"
    price = float(prices.get(market, 0.0))
    return float(amount) * price

def bitvavo_request(method: str, path: str, body: str = ""):
    """
    signature = HMAC_SHA256(secret, timestamp + METHOD + path + body)
    path moet exact incl. /v2 zijn (bijv /v2/balance)
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

def build_snapshot():
    balances = bitvavo_request("GET", "/v2/balance")

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
            assets.append({
                "symbol": symbol,
                "available": available,
                "inOrder": in_order,
                "total": total
            })

    snapshot = {
        "status": "OK",
        "ts": now_iso(),
        "eur_available": eur_available,
        "assets": sorted(assets, key=lambda x: (x["symbol"] != "EUR", x["symbol"])),
    }

    ensure_parent_dir(SNAPSHOT_PATH)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    return snapshot

def append_history(path: str, row: dict):
    ensure_parent_dir(path)
    if os.path.exists(path):
        df = pd.read_csv(path)
    else:
        df = pd.DataFrame(columns=row.keys())
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(path, index=False)

def filter_history(df: pd.DataFrame, mode: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    d = df.copy()
    d["_ts"] = pd.to_datetime(d["timestamp"], utc=True, errors="coerce")
    d = d.dropna(subset=["_ts"]).sort_values("_ts")
    latest = d["_ts"].max()
    if mode == "24u":
        return d[d["_ts"] >= latest - pd.Timedelta(hours=24)]
    if mode == "7d":
        return d[d["_ts"] >= latest - pd.Timedelta(days=7)]
    if mode == "30d":
        return d[d["_ts"] >= latest - pd.Timedelta(days=30)]
    if mode == "90d":
        return d[d["_ts"] >= latest - pd.Timedelta(days=90)]
    return d

def pct_change(df: pd.DataFrame, hours: int) -> float:
    if df is None or df.empty:
        return 0.0
    try:
        d = df.copy()
        d["_ts"] = pd.to_datetime(d["timestamp"], utc=True, errors="coerce")
        d = d.dropna(subset=["_ts"]).sort_values("_ts")

        latest_ts = d["_ts"].max()
        cutoff = latest_ts - pd.Timedelta(hours=hours)

        old = d[d["_ts"] <= cutoff]
        if old.empty:
            return 0.0

        old_val = float(old.iloc[-1]["total_portfolio"])
        new_val = float(d.iloc[-1]["total_portfolio"])
        if old_val <= 0:
            return 0.0

        return ((new_val - old_val) / old_val) * 100.0
    except Exception:
        return 0.0

def all_time_pct(df: pd.DataFrame) -> float:
    if df is None or df.empty:
        return 0.0
    try:
        d = df.copy()
        d["_ts"] = pd.to_datetime(d["timestamp"], utc=True, errors="coerce")
        d = d.dropna(subset=["_ts"]).sort_values("_ts")
        first = float(d.iloc[0]["total_portfolio"])
        last = float(d.iloc[-1]["total_portfolio"])
        if first <= 0:
            return 0.0
        return ((last - first) / first) * 100.0
    except Exception:
        return 0.0

def max_drawdown(df: pd.DataFrame) -> tuple:
    """
    Return (max_dd_abs, max_dd_pct)
    """
    if df is None or df.empty:
        return (0.0, 0.0)
    try:
        d = df.copy()
        d["_ts"] = pd.to_datetime(d["timestamp"], utc=True, errors="coerce")
        d = d.dropna(subset=["_ts"]).sort_values("_ts")
        series = pd.to_numeric(d["total_portfolio"], errors="coerce").fillna(0)
        if series.empty:
            return (0.0, 0.0)
        roll_max = series.cummax()
        dd = series - roll_max
        dd_pct = (dd / roll_max.replace(0, pd.NA)) * 100.0
        max_dd_abs = float(dd.min())
        max_dd_pct = float(dd_pct.min()) if dd_pct.notna().any() else 0.0
        return (max_dd_abs, max_dd_pct)
    except Exception:
        return (0.0, 0.0)

def volatility(df: pd.DataFrame) -> float:
    """
    Simpele volatiliteit op basis van returns van portfolio history
    """
    if df is None or df.empty:
        return 0.0
    try:
        d = df.copy()
        d["_ts"] = pd.to_datetime(d["timestamp"], utc=True, errors="coerce")
        d = d.dropna(subset=["_ts"]).sort_values("_ts")
        v = pd.to_numeric(d["total_portfolio"], errors="coerce").fillna(0)
        if len(v) < 3:
            return 0.0
        rets = v.pct_change().dropna()
        return float(rets.std() * 100.0)  # %
    except Exception:
        return 0.0

def load_trades():
    df, err = safe_read_csv(PAPER_TRADES_CSV)
    if err:
        return None, err
    return df, None


# =========================
# UI (pro look)
# =========================
st.set_page_config(page_title="Crypto AI Dashboard", layout="wide")
st.title("📊 Crypto AI Dashboard (PRO)")
st.caption("Bitvavo-stijl portfolio, benchmark, drawdown, volatiliteit, trades overlay en bot health — alles persistent op /data")

tabs = st.tabs(["💼 Portfolio", "📈 Trades", "🧠 Bot Health"])


# =========================
# TAB 1: Portfolio
# =========================
with tabs[0]:
    headerL, headerR = st.columns([2, 1])

    with headerL:
        st.subheader("Snapshot & opslag")
        st.code(SNAPSHOT_PATH, language="text")
        meta = file_meta(SNAPSHOT_PATH)
        if meta["exists"]:
            st.success(f"Snapshot gevonden ✅ | Laatst aangepast: {meta['modified']} | {meta['size_kb']} KB")
        else:
            st.warning("Nog geen snapshot gevonden. Maak eerst een snapshot.")

    with headerR:
        st.subheader("Acties")
        if st.button("🔄 Snapshot verversen", use_container_width=True):
            try:
                snap = build_snapshot()
                st.success("Snapshot aangemaakt ✅")
                st.json(snap)
            except Exception as e:
                st.error(f"Snapshot fout: {e}")
                st.caption(f"API_KEY len: {len(API_KEY)} | API_SECRET len: {len(API_SECRET)}")

    snap, err = safe_read_json(SNAPSHOT_PATH)
    st.divider()

    if err:
        st.warning(err)
    elif not snap or snap.get("status") != "OK":
        st.warning("Snapshot bestaat, maar status is niet OK.")
    else:
        # Public prices & 24h stats
        prices = {}
        stats24h = []
        p_err = None
        s_err = None
        try:
            prices = get_public_prices()
        except Exception as e:
            p_err = str(e)

        try:
            stats24h = get_public_24h()
        except Exception as e:
            s_err = str(e)

        eur_available = float(snap.get("eur_available", 0.0))
        assets = snap.get("assets", [])

        enriched = []
        crypto_assets_eur = 0.0

        for a in assets:
            symbol = a.get("symbol")
            total = float(a.get("total", 0.0) or 0.0)
            if total <= 0:
                continue

            value_eur = asset_value_eur(symbol, total, prices) if prices else (total if symbol == "EUR" else 0.0)
            if symbol != "EUR":
                crypto_assets_eur += value_eur

            enriched.append({
                "symbol": symbol,
                "total": total,
                "value_eur": round(value_eur, 2),
                "available": float(a.get("available", 0.0) or 0.0),
                "inOrder": float(a.get("inOrder", 0.0) or 0.0),
            })

        total_portfolio = eur_available + crypto_assets_eur

        # Save portfolio history point
        append_history(PORTFOLIO_HISTORY_CSV, {
            "timestamp": now_iso(),
            "eur_balance": eur_available,
            "crypto_assets": crypto_assets_eur,
            "total_portfolio": total_portfolio,
        })

        # Benchmark BTC-EUR history point (public price)
        btc_price = float(prices.get(BTC_BENCHMARK_MARKET, 0.0))
        if btc_price > 0:
            append_history(BENCHMARK_HISTORY_CSV, {
                "timestamp": now_iso(),
                "btc_eur": btc_price
            })

        # Load history
        hist_df, hist_err = safe_read_csv(PORTFOLIO_HISTORY_CSV)
        bench_df, bench_err = safe_read_csv(BENCHMARK_HISTORY_CSV)

        if hist_err:
            hist_df = None
        if bench_err:
            bench_df = None

        # Metrics
        day_pct = pct_change(hist_df, 24)
        week_pct = pct_change(hist_df, 24 * 7)
        m30_pct = pct_change(hist_df, 24 * 30)
        all_pct = all_time_pct(hist_df)
        dd_abs, dd_pct = max_drawdown(hist_df)
        vol = volatility(hist_df)

        # KPI grid
        st.success("✅ Portfolio berekend (Bitvavo-stijl)")
        k1, k2, k3 = st.columns(3)
        k1.metric("💶 Beschikbaar saldo (EUR)", f"€ {eur_available:.2f}")
        k2.metric("🪙 Crypto assets (EUR)", f"€ {crypto_assets_eur:.2f}")
        k3.metric("📈 Totale waarde portfolio", f"€ {total_portfolio:.2f}")

        k4, k5, k6, k7 = st.columns(4)
        k4.metric("📅 24u", f"{day_pct:+.2f}%")
        k5.metric("🗓️ 7d", f"{week_pct:+.2f}%")
        k6.metric("📆 30d", f"{m30_pct:+.2f}%")
        k7.metric("🏁 All-time", f"{all_pct:+.2f}%")

        k8, k9 = st.columns(2)
        k8.metric("📉 Max Drawdown", f"{dd_abs:.2f} (€) / {dd_pct:.2f}%")
        k9.metric("🌪️ Volatiliteit", f"{vol:.2f}%")

        if p_err:
            st.info(f"Prijzen (public) ophalen gaf een melding: {p_err}")
        if s_err:
            st.info(f"24h stats ophalen gaf een melding: {s_err}")

        st.divider()

        # Time filter
        period = st.selectbox("Portfolio periode", ["24u", "7d", "30d", "90d", "All"], index=2)
        hist_plot = filter_history(hist_df, period) if hist_df is not None else None
        bench_plot = filter_history(bench_df.rename(columns={"btc_eur": "btc_eur"}), period) if bench_df is not None else None

        st.subheader("📉 Portfolio koersgrafiek + BTC benchmark + trades overlay")

        if hist_plot is None or hist_plot.empty:
            st.info("Nog geen historie. Laat snapshots lopen (cron) of klik vaker op refresh.")
        else:
            # Prepare portfolio series
            hp = hist_plot.copy()
            hp["_ts"] = pd.to_datetime(hp["timestamp"], utc=True, errors="coerce")
            hp = hp.dropna(subset=["_ts"]).sort_values("_ts")
            hp["_ts_local"] = hp["_ts"].dt.tz_convert(None)

            # Prepare benchmark as normalized
            bp = None
            if bench_plot is not None and not bench_plot.empty:
                bp = bench_plot.copy()
                bp["_ts"] = pd.to_datetime(bp["timestamp"], utc=True, errors="coerce")
                bp = bp.dropna(subset=["_ts"]).sort_values("_ts")
                bp["_ts_local"] = bp["_ts"].dt.tz_convert(None)
                # normalize benchmark to portfolio start
                try:
                    p0 = float(hp["total_portfolio"].iloc[0])
                    b0 = float(bp["btc_eur"].iloc[0])
                    if p0 > 0 and b0 > 0:
                        bp["btc_norm"] = (bp["btc_eur"] / b0) * p0
                    else:
                        bp["btc_norm"] = None
                except Exception:
                    bp = None

            # Trades markers
            trades_df, t_err = load_trades()
            buy_points, sell_points = [], []
            if t_err is None and trades_df is not None and not trades_df.empty:
                if "datetime" in trades_df.columns and "side" in trades_df.columns:
                    trades_df["_dt"] = pd.to_datetime(trades_df["datetime"], errors="coerce")
                    trades_df = trades_df.dropna(subset=["_dt"])

                    start_ts = hp["_ts_local"].min()
                    end_ts = hp["_ts_local"].max()
                    trades_df = trades_df[(trades_df["_dt"] >= start_ts) & (trades_df["_dt"] <= end_ts)]

                    series_total = pd.Series(hp["total_portfolio"].values, index=hp["_ts_local"])

                    for _, r in trades_df.iterrows():
                        side = str(r.get("side", "")).upper().strip()
                        dt = r["_dt"]
                        try:
                            y = float(series_total.iloc[series_total.index.get_indexer([dt], method="nearest")[0]])
                        except Exception:
                            y = float(series_total.iloc[-1])

                        if side == "BUY":
                            buy_points.append((dt, y))
                        elif side == "SELL":
                            sell_points.append((dt, y))

            # Plot
            fig, ax = plt.subplots()
            ax.plot(hp["_ts_local"], hp["total_portfolio"], label="Portfolio")

            if bp is not None and "btc_norm" in bp.columns:
                ax.plot(bp["_ts_local"], bp["btc_norm"], label="BTC benchmark (genormaliseerd)")

            if buy_points:
                ax.scatter([p[0] for p in buy_points], [p[1] for p in buy_points], marker="^", label="BUY")
            if sell_points:
                ax.scatter([p[0] for p in sell_points], [p[1] for p in sell_points], marker="v", label="SELL")

            ax.set_xlabel("Tijd")
            ax.set_ylabel("Waarde (€)")
            ax.legend(loc="best")
            st.pyplot(fig)

        st.divider()

        # Assets
        st.subheader("🧩 Asset verdeling (Top 10 + OVERIG)")
        df_assets = pd.DataFrame(enriched)
        if df_assets.empty:
            st.info("Geen assets.")
        else:
            df_assets = df_assets[df_assets["value_eur"] > 0].sort_values("value_eur", ascending=False)

            top = df_assets.head(10).copy()
            rest_sum = float(df_assets.iloc[10:]["value_eur"].sum()) if len(df_assets) > 10 else 0.0

            labels = list(top["symbol"])
            values = list(top["value_eur"])

            if rest_sum > 0:
                labels.append("OVERIG")
                values.append(rest_sum)

            fig2, ax2 = plt.subplots()
            ax2.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
            ax2.axis("equal")
            st.pyplot(fig2)

        st.subheader("🪙 Assets overzicht (EUR waarde)")
        if not df_assets.empty:
            show = df_assets[["symbol", "total", "value_eur", "available", "inOrder"]].copy()
            st.dataframe(show, use_container_width=True, hide_index=True)

        st.divider()

        # Top movers (24h)
        st.subheader("🚀 Top movers (24h) — Bitvavo public")
        if isinstance(stats24h, list) and stats24h:
            # alleen EUR pairs, anders is het chaos
            rows = []
            for it in stats24h:
                m = str(it.get("market", ""))
                if not m.endswith("-EUR"):
                    continue
                try:
                    change = float(it.get("priceChangePercentage", 0) or 0)
                    last = float(it.get("last", 0) or 0)
                    vol_quote = float(it.get("volumeQuote", 0) or 0)
                except Exception:
                    continue
                rows.append({
                    "market": m,
                    "change_24h_%": change,
                    "last": last,
                    "volumeQuote": vol_quote
                })

            movers = pd.DataFrame(rows)
            if movers.empty:
                st.info("Geen movers data.")
            else:
                movers = movers.sort_values("change_24h_%", ascending=False)

                cA, cB = st.columns(2)
                with cA:
                    st.caption("Top gainers")
                    st.dataframe(movers.head(10), use_container_width=True, hide_index=True)
                with cB:
                    st.caption("Top losers")
                    st.dataframe(movers.tail(10).sort_values("change_24h_%"), use_container_width=True, hide_index=True)
        else:
            st.info("24h movers niet beschikbaar (public endpoint).")

    st.info(
        "Let op: private Bitvavo endpoints werken alleen via code met headers/signature. "
        "In je browser geeft `https://api.bitvavo.com/v2/balance` altijd auth-error (normaal)."
    )


# =========================
# TAB 2: Trades
# =========================
with tabs[1]:
    st.subheader("📈 Trades & Performance (Paper)")

    df_trades, err = safe_read_csv(PAPER_TRADES_CSV)

    topL, topR = st.columns([2, 1])
    with topR:
        st.write("Bronbestand:")
        st.code(PAPER_TRADES_CSV, language="text")
        meta = file_meta(PAPER_TRADES_CSV)
        if meta["exists"]:
            st.success(f"Trades CSV gevonden ✅ | Laatst aangepast: {meta['modified']}")
        else:
            st.warning("Trades CSV bestaat nog niet. Zodra paper_trader trades logt, komt dit vanzelf.")

    if err:
        st.warning(err)
    elif df_trades is None or df_trades.empty:
        st.info("Nog geen trades. Eerst een BUY/SELL laten plaatsvinden.")
    else:
        st.dataframe(df_trades.tail(100), use_container_width=True)

        # Basic metrics
        cols = [c.lower() for c in df_trades.columns]
        colmap = {c.lower(): c for c in df_trades.columns}

        pnl_col = None
        for candidate in ["pnl", "profit", "pnl_eur", "pnl_usdt"]:
            if candidate in cols:
                pnl_col = colmap[candidate]
                break

        sym_col = colmap.get("symbol") if "symbol" in cols else None
        side_col = colmap.get("side") if "side" in cols else None
        dt_col = colmap.get("datetime") if "datetime" in cols else None

        if pnl_col:
            pnl = pd.to_numeric(df_trades[pnl_col], errors="coerce").fillna(0)
            total_pnl = float(pnl.sum())
            wins = int((pnl > 0).sum())
            losses = int((pnl < 0).sum())
            n = int(len(pnl))
            winrate = (wins / n * 100) if n else 0.0

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Totaal PnL", f"{total_pnl:.2f}")
            c2.metric("Trades", f"{n}")
            c3.metric("Winrate", f"{winrate:.1f}%")
            c4.metric("W/L", f"{wins}/{losses}")

            st.subheader("Equity curve (cumulatieve PnL)")
            st.line_chart(pnl.cumsum())

            # Daily PnL
            if dt_col:
                try:
                    tmp = df_trades.copy()
                    tmp["_dt"] = pd.to_datetime(tmp[dt_col], errors="coerce")
                    tmp = tmp.dropna(subset=["_dt"])
                    tmp["_date"] = tmp["_dt"].dt.date
                    tmp["_pnl"] = pd.to_numeric(tmp[pnl_col], errors="coerce").fillna(0)
                    daily = tmp.groupby("_date")["_pnl"].sum()
                    st.subheader("Daily PnL")
                    st.line_chart(daily)
                except Exception:
                    st.info("Daily PnL kon niet opgebouwd worden (datetime format wijkt af).")
        else:
            st.info("Geen herkenbare PnL-kolom gevonden. Tip: log `pnl` in paper_trader.")

        # Trades per coin
        if sym_col:
            st.subheader("Trades per coin")
            try:
                counts = df_trades.groupby(sym_col).size().sort_values(ascending=False)
                st.bar_chart(counts)
            except Exception:
                st.info("Trades-per-coin chart kon niet opgebouwd worden.")


# =========================
# TAB 3: Bot Health
# =========================
with tabs[2]:
    st.subheader("🧠 Bot Health (Render Disk /data)")

    paths = [
        ("Snapshot (Bitvavo)", SNAPSHOT_PATH),
        ("Portfolio history", PORTFOLIO_HISTORY_CSV),
        ("BTC benchmark history", BENCHMARK_HISTORY_CSV),
        ("Pending approvals", PENDING_PATH),
        ("Paper state", PAPER_STATE_PATH),
        ("Paper trades CSV", PAPER_TRADES_CSV),
        ("AI usage", AI_USAGE_PATH),
        ("Prebuy state", PREBUY_STATE_PATH),
        ("Prebuy payload", PREBUY_PAYLOAD_PATH),
    ]

    rows = []
    for name, p in paths:
        m = file_meta(p)
        rows.append({
            "item": name,
            "exists": m.get("exists", False),
            "path": p,
            "modified": m.get("modified", ""),
            "size_kb": m.get("size_kb", ""),
        })

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.divider()

    # Quick previews
    c1, c2 = st.columns(2)

    with c1:
        st.write("### Pending approvals (preview)")
        pending, e = safe_read_json(PENDING_PATH)
        if e:
            st.info(e)
        else:
            st.json(pending)

        st.write("### Paper state (preview)")
        pstate, e = safe_read_json(PAPER_STATE_PATH)
        if e:
            st.info(e)
        else:
            st.json(pstate)

    with c2:
        st.write("### AI usage (preview)")
        aiu, e = safe_read_json(AI_USAGE_PATH)
        if e:
            st.info(e)
        else:
            st.json(aiu)

        st.write("### Prebuy payload (preview)")
        pb, e = safe_read_json(PREBUY_PAYLOAD_PATH)
        if e:
            st.info(e)
        else:
            st.json(pb)

    st.success("✅ Dit tabblad is je controlepaneel: je ziet direct welke scripts data wegschrijven en welke nog leeg zijn.")
