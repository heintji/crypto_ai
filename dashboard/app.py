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

BASE_URL = "https://api.bitvavo.com"
ACCESS_WINDOW_MS = "10000"
BITVAVO_TICKER_PRICE_URL = "https://api.bitvavo.com/v2/ticker/price"


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

def get_public_prices_eur():
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

def append_portfolio_history(ts_iso: str, eur_balance: float, crypto_assets: float, total_portfolio: float):
    ensure_parent_dir(PORTFOLIO_HISTORY_CSV)

    row = {
        "timestamp": ts_iso,
        "eur_balance": eur_balance,
        "crypto_assets": crypto_assets,
        "total_portfolio": total_portfolio,
    }

    if os.path.exists(PORTFOLIO_HISTORY_CSV):
        df = pd.read_csv(PORTFOLIO_HISTORY_CSV)
    else:
        df = pd.DataFrame(columns=row.keys())

    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(PORTFOLIO_HISTORY_CSV, index=False)

def compute_pct_change(history_df: pd.DataFrame, hours: int) -> float:
    if history_df is None or history_df.empty:
        return 0.0
    try:
        df = history_df.copy()
        df["_ts"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        df = df.dropna(subset=["_ts"]).sort_values("_ts")

        latest_ts = df["_ts"].max()
        cutoff = latest_ts - pd.Timedelta(hours=hours)

        old = df[df["_ts"] <= cutoff]
        if old.empty:
            return 0.0

        old_val = float(old.iloc[-1]["total_portfolio"])
        new_val = float(df.iloc[-1]["total_portfolio"])
        if old_val <= 0:
            return 0.0

        return ((new_val - old_val) / old_val) * 100.0
    except Exception:
        return 0.0

def filter_history(df: pd.DataFrame, mode: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    d = df.copy()
    d["_ts"] = pd.to_datetime(d["timestamp"], utc=True, errors="coerce")
    d = d.dropna(subset=["_ts"]).sort_values("_ts")
    if mode == "24u":
        cutoff = d["_ts"].max() - pd.Timedelta(hours=24)
        return d[d["_ts"] >= cutoff]
    if mode == "7d":
        cutoff = d["_ts"].max() - pd.Timedelta(days=7)
        return d[d["_ts"] >= cutoff]
    if mode == "30d":
        cutoff = d["_ts"].max() - pd.Timedelta(days=30)
        return d[d["_ts"] >= cutoff]
    return d


# =========================
# UI
# =========================
st.set_page_config(page_title="Crypto AI Dashboard", layout="wide")
st.title("📊 Crypto AI Dashboard")
st.caption("Portfolio (Bitvavo-stijl) + trades + performance + bot status — alles Render-proof op /data")

tabs = st.tabs(["💶 Saldo & Assets", "📈 Trades & Performance", "🧠 Bot Status"])


# =========================
# TAB 1: Saldo & Assets
# =========================
with tabs[0]:
    topL, topR = st.columns([2, 1])

    with topL:
        st.subheader("Snapshot instellingen")
        st.code(SNAPSHOT_PATH, language="text")

        meta = file_meta(SNAPSHOT_PATH)
        if meta["exists"]:
            st.success(f"Snapshot gevonden ✅ | Laatst aangepast: {meta['modified']} | {meta['size_kb']} KB")
        else:
            st.warning("Nog geen snapshot gevonden. Klik op **Snapshot verversen**.")

    with topR:
        st.subheader("Acties")
        if st.button("🔄 Snapshot verversen (saldo)", use_container_width=True):
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
    else:
        if not snap or snap.get("status") != "OK":
            st.warning("Snapshot bestaat, maar status is niet OK.")
        else:
            # Prices
            prices = {}
            prices_err = None
            try:
                prices = get_public_prices_eur()
            except Exception as e:
                prices_err = str(e)

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

            # Write history
            append_portfolio_history(
                ts_iso=now_iso(),
                eur_balance=eur_available,
                crypto_assets=crypto_assets_eur,
                total_portfolio=total_portfolio
            )

            # Load history
            hist_df, hist_err = safe_read_csv(PORTFOLIO_HISTORY_CSV)
            if hist_err:
                hist_df = None

            day_pct = compute_pct_change(hist_df, 24)
            week_pct = compute_pct_change(hist_df, 24 * 7)
            m30_pct = compute_pct_change(hist_df, 24 * 30)

            # KPI cards
            st.success("✅ Portfolio berekend (Bitvavo-stijl)")
            c1, c2, c3 = st.columns(3)
            c1.metric("💶 Beschikbaar saldo (EUR)", f"€ {eur_available:.2f}")
            c2.metric("🪙 Crypto assets (EUR)", f"€ {crypto_assets_eur:.2f}")
            c3.metric("📈 Totale waarde portfolio", f"€ {total_portfolio:.2f}")

            c4, c5, c6 = st.columns(3)
            c4.metric("📅 24u", f"{day_pct:+.2f}%")
            c5.metric("🗓️ 7d", f"{week_pct:+.2f}%")
            c6.metric("📆 30d", f"{m30_pct:+.2f}%")

            if prices_err:
                st.info(f"Prijzen ophalen (public) gaf een melding: {prices_err}")

            st.divider()

            # Time filter
            sel = st.selectbox("Portfolio periode", ["24u", "7d", "30d", "All"], index=1)
            hist_plot = filter_history(hist_df, sel) if hist_df is not None else None

            st.subheader("📉 Portfolio koersgrafiek")
            if hist_plot is None or hist_plot.empty:
                st.info("Nog geen portfolio historie. Dit vult zich na meerdere snapshots.")
            else:
                hist_plot = hist_plot.copy()
                hist_plot["_ts"] = pd.to_datetime(hist_plot["timestamp"], utc=True, errors="coerce")
                hist_plot = hist_plot.dropna(subset=["_ts"]).sort_values("_ts")

                # Trades markers
                trades_df, trades_err = safe_read_csv(PAPER_TRADES_CSV)
                buy_points, sell_points = [], []

                if trades_err is None and trades_df is not None and not trades_df.empty:
                    if "datetime" in trades_df.columns and "side" in trades_df.columns:
                        trades_df["_dt"] = pd.to_datetime(trades_df["datetime"], errors="coerce")
                        trades_df = trades_df.dropna(subset=["_dt"])

                        # alleen trades binnen periode (als mogelijk)
                        start_ts = hist_plot["_ts"].min().to_pydatetime().replace(tzinfo=None)
                        end_ts = hist_plot["_ts"].max().to_pydatetime().replace(tzinfo=None)

                        trades_df = trades_df[(trades_df["_dt"] >= start_ts) & (trades_df["_dt"] <= end_ts)]

                        # map trade dt -> nearest portfolio value
                        tmp_idx = pd.to_datetime(hist_plot["_ts"].dt.tz_convert(None))
                        series_total = pd.Series(hist_plot["total_portfolio"].values, index=tmp_idx)

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

                fig, ax = plt.subplots()
                ax.plot(hist_plot["_ts"], hist_plot["total_portfolio"])

                if buy_points:
                    ax.scatter([p[0] for p in buy_points], [p[1] for p in buy_points], marker="^", label="BUY")
                if sell_points:
                    ax.scatter([p[0] for p in sell_points], [p[1] for p in sell_points], marker="v", label="SELL")

                ax.set_xlabel("Tijd")
                ax.set_ylabel("Portfolio (€)")
                ax.legend(loc="best")
                st.pyplot(fig)

            st.divider()

            # Asset pie: top 10 + OVERIG
            st.subheader("🧩 Asset verdeling")
            df_assets = pd.DataFrame(enriched)
            if df_assets.empty:
                st.info("Geen assets gevonden.")
            else:
                df_assets = df_assets[df_assets["value_eur"] > 0].copy()
                df_assets = df_assets.sort_values("value_eur", ascending=False)

                top = df_assets.head(10).copy()
                rest_sum = float(df_assets.iloc[10:]["value_eur"].sum()) if len(df_assets) > 10 else 0.0

                pie_labels = list(top["symbol"])
                pie_values = list(top["value_eur"])

                if rest_sum > 0:
                    pie_labels.append("OVERIG")
                    pie_values.append(rest_sum)

                fig2, ax2 = plt.subplots()
                ax2.pie(pie_values, labels=pie_labels, autopct="%1.1f%%", startangle=90)
                ax2.axis("equal")
                st.pyplot(fig2)

            st.divider()

            # Assets table (sorted)
            st.subheader("🪙 Assets overzicht (EUR waarde)")
            if not df_assets.empty:
                show = df_assets[["symbol", "total", "value_eur", "available", "inOrder"]].copy()
                show = show.sort_values("value_eur", ascending=False)
                st.dataframe(show, use_container_width=True, hide_index=True)

    st.info(
        "Let op: private Bitvavo endpoints werken alleen via code met headers/signature. "
        "In je browser geeft `https://api.bitvavo.com/v2/balance` altijd auth-error (normaal)."
    )


# =========================
# TAB 2: Trades & Performance
# =========================
with tabs[1]:
    st.subheader("Trades & Performance (Paper)")

    df_trades, err = safe_read_csv(PAPER_TRADES_CSV)

    r1, r2 = st.columns([2, 1])
    with r2:
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
        else:
            st.info("Geen herkenbare PnL-kolom gevonden in CSV. Tip: voeg kolom `pnl` toe in paper_trader log.")


# =========================
# TAB 3: Bot Status
# =========================
with tabs[2]:
    st.subheader("Bot Status (Render Disk)")

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

    st.write("### AI usage (preview)")
    aiu, e = safe_read_json(AI_USAGE_PATH)
    if e:
        st.info(e)
    else:
        st.json(aiu)

    st.success("✅ Status OK: hier zie je direct welke onderdelen data wegschrijven en welke nog leeg zijn.")
