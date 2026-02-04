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
PAPER_TRADES_CSV = os.getenv("PAPER_TRADES_CSV", "/data/paper_trades.csv")
AI_USAGE_PATH = os.getenv("AI_USAGE_PATH", "/data/ai_usage.json")
PREBUY_STATE_PATH = os.getenv("PREBUY_STATE_PATH", "/data/prebuy_state.json")
PREBUY_PAYLOAD_PATH = os.getenv("PREBUY_PAYLOAD_PATH", "/data/prebuy_payload.json")

# Auto-refresh snapshot als hij ouder is dan X minuten
AUTO_REFRESH_MINUTES = int(os.getenv("AUTO_REFRESH_MINUTES", "15"))

# Timeouts
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


# =========================
# Bitvavo Private Request (SIGNING)
# =========================
def bitvavo_request(method: str, path: str, body: str = ""):
    """
    signature = HMAC_SHA256(secret, timestamp + METHOD + path + body)
    path moet exact incl. /v2, bv. /v2/balance
    """
    if not API_KEY or not API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY of BITVAVO_API_SECRET ontbreekt in Render Environment Variables.")

    method_u = method.upper()
    timestamp = str(int(time.time() * 1000))
    body = body or ""  # GET body altijd leeg

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
# Public prices (EUR waardes)
# =========================
@st.cache_data(ttl=30)
def fetch_all_market_prices():
    """
    Haal alle prijzen op (public endpoint).
    Retourneert dict: { "BTC-EUR": 42000.0, ... }
    """
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
    """
    Probeert een EUR-prijs te bepalen via:
    1) SYMBOL-EUR
    2) SYMBOL-USDT * USDT-EUR
    3) SYMBOL-BTC * BTC-EUR
    Geeft (price_eur, route) terug of (None, "NO_ROUTE").
    """
    if symbol == "EUR":
        return 1.0, "EUR"

    direct = f"{symbol}-EUR"
    if direct in prices:
        return prices[direct], direct

    # USDT route
    a = f"{symbol}-USDT"
    b = "USDT-EUR"
    if a in prices and b in prices:
        return prices[a] * prices[b], f"{a} * {b}"

    # BTC route
    a = f"{symbol}-BTC"
    b = "BTC-EUR"
    if a in prices and b in prices:
        return prices[a] * prices[b], f"{a} * {b}"

    return None, "NO_ROUTE"


# =========================
# Snapshot builder (balance + EUR waardes + portfolio history)
# =========================
def build_snapshot_with_eur_values():
    """
    1) Haal private balance op
    2) Haal public prijzen op
    3) Bereken EUR waarde per asset
    4) Schrijf snapshot naar /data/account_snapshot.json
    5) Append portfolio total naar /data/portfolio_history.csv
    """
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
                "price_eur": p_eur,        # kan None zijn
                "eur_value": eur_value,    # kan None zijn
                "price_route": route,
            })

    # Crypto assets in € = som eur_value van non-EUR
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

    # Portfolio history (voor koersgrafiek + % dag/week)
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

    # Dedup op ts (voor safety)
    hist = hist.drop_duplicates(subset=["ts"], keep="last")
    hist.to_csv(PORTFOLIO_HISTORY_CSV, index=False)

    return snapshot


def maybe_auto_refresh_snapshot():
    """
    Als snapshot niet bestaat OF ouder dan AUTO_REFRESH_MINUTES → rebuild.
    """
    meta = file_meta(SNAPSHOT_PATH)
    if not meta["exists"]:
        return build_snapshot_with_eur_values(), "created"

    mins = age_minutes(meta["modified_epoch"])
    if mins >= AUTO_REFRESH_MINUTES:
        return build_snapshot_with_eur_values(), f"refreshed ({mins:.0f} min old)"
    return None, f"kept (age {mins:.0f} min)"


# =========================
# UI
# =========================
st.set_page_config(page_title="Crypto AI Dashboard", layout="wide")
st.title("📊 Crypto AI Dashboard")
st.caption("Render-proof: Bitvavo saldo + portfolio waarde + koersgrafiek + trades/monitoring")

tabs = st.tabs(["💶 Saldo & Portfolio", "📉 Koers & Trades", "📈 Trades & Performance", "🧠 Bot Status / Masterlijst"])


# =========================
# TAB 1: Saldo & Portfolio
# =========================
with tabs[0]:
    # Auto refresh bij laden (geen gedoe meer met 'oude' snapshots)
    try:
        _, refresh_state = maybe_auto_refresh_snapshot()
    except Exception as e:
        refresh_state = f"auto-refresh faalde: {e}"

    colL, colR = st.columns([2, 1])

    with colL:
        meta = file_meta(SNAPSHOT_PATH)
        if meta["exists"]:
            mins = age_minutes(meta["modified_epoch"])
            st.success(f"Snapshot gevonden ✅  | Laatst aangepast: {meta['modified']} | Leeftijd: {mins:.0f} min | Grootte: {meta['size_kb']} KB")
        else:
            st.warning("Nog geen snapshot gevonden. Klik op **Snapshot verversen**.")

        st.write("Snapshot pad (Render Disk):")
        st.code(SNAPSHOT_PATH, language="text")

        st.write("Portfolio history pad (Render Disk):")
        st.code(PORTFOLIO_HISTORY_CSV, language="text")

        st.caption(f"Auto-refresh: elke {AUTO_REFRESH_MINUTES} min (status: {refresh_state})")

    with colR:
        st.subheader("Acties")
        if st.button("🔄 Snapshot verversen (saldo + EUR waardes)", use_container_width=True):
            try:
                snap = build_snapshot_with_eur_values()
                st.success("Snapshot aangemaakt/ververst ✅")
                st.json(snap)
            except Exception as e:
                st.error(f"Snapshot fout: {e}")
                st.caption(f"API_KEY len: {len(API_KEY)} | API_SECRET len: {len(API_SECRET)}")

    st.divider()

    snap, err = safe_read_json(SNAPSHOT_PATH)
    if err:
        st.warning(err)
    else:
        if snap and snap.get("status") == "OK":
            eur_available = float(snap.get("eur_available", 0) or 0)
            crypto_assets_eur = float(snap.get("crypto_assets_eur", 0) or 0)
            total_portfolio_eur = float(snap.get("total_portfolio_eur", 0) or 0)

            c1, c2, c3 = st.columns(3)
            c1.metric("Beschikbaar saldo (EUR)", f"€ {eur_available:,.2f}")
            c2.metric("Crypto assets", f"€ {crypto_assets_eur:,.2f}")
            c3.metric("Totale waarde portfolio", f"€ {total_portfolio_eur:,.2f}")

            assets = snap.get("assets", []) or []
            df_assets = pd.DataFrame(assets)

            # Zorg dat kolommen bestaan (geen KeyError meer)
            for col in ["price_eur", "eur_value", "price_route"]:
                if col not in df_assets.columns:
                    df_assets[col] = None

            st.subheader("Assets (alleen > 0) — inclusief waarde in €")
            st.dataframe(
                df_assets[["symbol", "available", "inOrder", "total", "price_eur", "eur_value", "price_route"]],
                use_container_width=True,
                hide_index=True
            )

            # Asset verdeling (pie) op basis van eur_value
            df_pie = df_assets.copy()
            df_pie = df_pie[df_pie["symbol"] != "EUR"]
            df_pie["eur_value"] = pd.to_numeric(df_pie["eur_value"], errors="coerce")
            df_pie = df_pie[df_pie["eur_value"].notna() & (df_pie["eur_value"] > 0)]

            if len(df_pie) > 0:
                st.subheader("Asset-verdeling (op basis van € waarde)")
                fig = px.pie(df_pie, names="symbol", values="eur_value")
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("Nog geen EUR-waardes voor coins gevonden. (Dit gebeurt als er geen route is via EUR/USDT/BTC-markten.)")

        else:
            st.warning("Snapshot bestaat, maar status is niet OK.")

    st.info(
        "Let op: als je in je browser naar `https://api.bitvavo.com/v2/balance` gaat, krijg je altijd een auth-fout. "
        "Dat is normaal — private endpoints werken alleen met headers/signature vanuit je code."
    )


# =========================
# TAB 2: Koers & Trades (portfolio history + % verandering + trades overlay)
# =========================
with tabs[1]:
    st.subheader("Portfolio koersgrafiek (history) + % verandering")

    df_hist, err = safe_read_csv(PORTFOLIO_HISTORY_CSV)
    if err:
        st.warning(err)
    else:
        df_hist["ts"] = pd.to_datetime(df_hist["ts"], errors="coerce", utc=True)
        df_hist = df_hist.dropna(subset=["ts"]).sort_values("ts")

        if len(df_hist) >= 2:
            # % change dag/week op basis van laatste punt vs punt ~24h/7d geleden (closest)
            latest = df_hist.iloc[-1]
            latest_val = float(latest["total_portfolio_eur"])

            def pct_change_from_ago(hours: int):
                target = latest["ts"] - pd.Timedelta(hours=hours)
                tmp = df_hist.copy()
                tmp["diff"] = (tmp["ts"] - target).abs()
                past = tmp.sort_values("diff").iloc[0]
                past_val = float(past["total_portfolio_eur"])
                if past_val == 0:
                    return None
                return (latest_val - past_val) / past_val * 100.0

            d1 = pct_change_from_ago(24)
            w1 = pct_change_from_ago(24 * 7)

            c1, c2, c3 = st.columns(3)
            c1.metric("Totale waarde (laatst)", f"€ {latest_val:,.2f}")
            c2.metric("% verandering (dag)", "-" if d1 is None else f"{d1:.2f}%")
            c3.metric("% verandering (week)", "-" if w1 is None else f"{w1:.2f}%")

        st.subheader("Koersgrafiek: totale portfolio (€)")
        fig = px.line(df_hist, x="ts", y="total_portfolio_eur")
        st.plotly_chart(fig, use_container_width=True)

        st.write("Laatste 50 regels portfolio history:")
        st.dataframe(df_hist.tail(50), use_container_width=True, hide_index=True)

    st.divider()

    st.subheader("Trades op grafiek (optioneel)")
    df_trades, err_t = safe_read_csv(PAPER_TRADES_CSV)
    if err_t:
        st.info("Nog geen paper_trades.csv gevonden of niet leesbaar. Zodra trades gelogd worden, kunnen we ze hier plotten.")
    else:
        # Probeer timestamps + side te vinden
        cols = [c.lower() for c in df_trades.columns]
        colmap = {c.lower(): c for c in df_trades.columns}

        ts_col = None
        for cand in ["timestamp", "time", "ts", "date"]:
            if cand in cols:
                ts_col = colmap[cand]
                break

        side_col = None
        for cand in ["side", "action", "type"]:
            if cand in cols:
                side_col = colmap[cand]
                break

        if ts_col:
            df_trades["_ts"] = pd.to_datetime(df_trades[ts_col], errors="coerce", utc=True)
            df_trades = df_trades.dropna(subset=["_ts"]).sort_values("_ts")
            st.dataframe(df_trades.tail(50), use_container_width=True, hide_index=True)
            st.caption("Trades zijn zichtbaar; overlay op de portfolio-grafiek kan zodra je trade kolommen stabiel zijn (BUY/SELL + waarde).")
        else:
            st.info("Trades CSV heeft geen herkenbare timestamp kolom; voeg bv. `timestamp` toe in paper_trader logs.")


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
        for candidate in ["timestamp", "time", "ts", "date"]:
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
**Masterlijst (dashboard kan dit allemaal tonen):**
- ✅ **Saldo & Assets** (Bitvavo snapshot + EUR waardes)
- ✅ **Portfolio history** (koersgrafiek + % dag/week)
- ✅ **Pending approvals** (Pre-BUY wachtrij + expiry + gekozen bedrag)
- ✅ **Open posities** (paper_state / live later)
- ✅ **Trades history** (paper_trades.csv)
- ✅ **Performance metrics** (PnL, winrate, equity)
- ✅ **Bot health** (bestanden + timestamps)
- ✅ **AI usage** (calls per dag/maand)
- ✅ **R-metrics / STRUCTUUR-MODE** (later uit trade logs afleiden)
- ✅ **Shadow trades** (later)
- ✅ **Weekly reporting** (later)
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

    st.success("👉 Dit tabblad is jouw controlepaneel: je ziet direct welke onderdelen al data wegschrijven en welke nog leeg zijn.")
