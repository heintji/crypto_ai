import os
import json
import time
import hmac
import hashlib
from datetime import datetime, timezone

import requests
import pandas as pd
import streamlit as st
import plotly.graph_objects as go


# =========================
# Config
# =========================
API_KEY = (os.getenv("BITVAVO_API_KEY", "") or "").strip().strip('"').strip("'")
API_SECRET = (os.getenv("BITVAVO_API_SECRET", "") or "").strip().strip('"').strip("'")

SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", "/data/account_snapshot.json")
PORTFOLIO_HISTORY_CSV = os.getenv("PORTFOLIO_HISTORY_CSV", "/data/portfolio_history.csv")

BASE_URL = "https://api.bitvavo.com"
ACCESS_WINDOW_MS = "10000"

PENDING_PATH = os.getenv("PENDING_PATH", "/data/pending_approvals.json")
PAPER_STATE_PATH = os.getenv("PAPER_STATE_PATH", "/data/paper_state.json")
PAPER_TRADES_CSV = os.getenv("PAPER_TRADES_CSV", "/data/paper_trades.csv")
AI_USAGE_PATH = os.getenv("AI_USAGE_PATH", "/data/ai_usage.json")
PREBUY_STATE_PATH = os.getenv("PREBUY_STATE_PATH", "/data/prebuy_state.json")
PREBUY_PAYLOAD_PATH = os.getenv("PREBUY_PAYLOAD_PATH", "/data/prebuy_payload.json")


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

def fmt_eur(x: float) -> str:
    try:
        return f"€ {float(x):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "€ 0,00"


# =========================
# Bitvavo signing (private)
# =========================
def bitvavo_request(method: str, path: str, body: str = ""):
    """
    signature = HMAC_SHA256(secret, timestamp + METHOD + path + body)
    path moet exact zijn incl /v2, bv: /v2/balance
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
# Bitvavo public price helpers
# =========================
_price_cache = {}

def public_ticker_price(market: str):
    """
    Public endpoint: /v2/ticker/price?market=BTC-EUR
    Geeft dict terug met 'price' of None
    """
    if market in _price_cache:
        return _price_cache[market]

    try:
        url = f"{BASE_URL}/v2/ticker/price"
        r = requests.get(url, params={"market": market}, timeout=12)
        if r.status_code >= 400:
            _price_cache[market] = None
            return None
        data = r.json()
        # soms is het dict, soms list (afhankelijk endpoint response) -> maak robuust
        if isinstance(data, list) and len(data) > 0:
            data = data[0]
        price = float(data.get("price")) if isinstance(data, dict) and data.get("price") is not None else None
        _price_cache[market] = price
        return price
    except Exception:
        _price_cache[market] = None
        return None


def get_price_eur(symbol: str):
    """
    Probeert EUR-prijs te krijgen:
    1) SYMBOL-EUR
    2) SYMBOL-USDT + USDT-EUR
    3) SYMBOL-BTC  + BTC-EUR
    """
    if symbol == "EUR":
        return 1.0, "EUR"

    # 1) direct EUR
    m1 = f"{symbol}-EUR"
    p1 = public_ticker_price(m1)
    if p1 is not None:
        return p1, m1

    # 2) via USDT
    m2a = f"{symbol}-USDT"
    m2b = "USDT-EUR"
    p2a = public_ticker_price(m2a)
    p2b = public_ticker_price(m2b)
    if p2a is not None and p2b is not None:
        return p2a * p2b, f"{m2a} * {m2b}"

    # 3) via BTC
    m3a = f"{symbol}-BTC"
    m3b = "BTC-EUR"
    p3a = public_ticker_price(m3a)
    p3b = public_ticker_price(m3b)
    if p3a is not None and p3b is not None:
        return p3a * p3b, f"{m3a} * {m3b}"

    return None, None


# =========================
# Snapshot builder (+ EUR waarden + history)
# =========================
def build_snapshot():
    """
    Haal balans op, verrijk met EUR-waarden, schrijf snapshot + portfolio history.
    """
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

    # Verrijk met EUR prijs/waarde
    crypto_assets_eur = 0.0
    enriched = []
    for a in assets:
        sym = a["symbol"]
        price_eur, route = get_price_eur(sym)
        eur_value = 0.0
        if sym == "EUR":
            eur_value = float(a["total"])
        else:
            if price_eur is not None:
                eur_value = float(a["total"]) * float(price_eur)
                crypto_assets_eur += eur_value

        a2 = dict(a)
        a2["price_eur"] = None if price_eur is None or sym == "EUR" else float(price_eur)
        a2["eur_value"] = float(eur_value) if eur_value is not None else 0.0
        a2["price_route"] = route
        enriched.append(a2)

    total_portfolio_eur = eur_available + crypto_assets_eur

    snapshot = {
        "status": "OK",
        "ts": now_iso(),
        "eur_available": float(eur_available),
        "crypto_assets_eur": float(crypto_assets_eur),
        "total_portfolio_eur": float(total_portfolio_eur),
        "assets": sorted(enriched, key=lambda x: (x["symbol"] != "EUR", x["symbol"])),
    }

    ensure_parent_dir(SNAPSHOT_PATH)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    # Portfolio history append
    ensure_parent_dir(PORTFOLIO_HISTORY_CSV)
    row = {
        "ts": snapshot["ts"],
        "eur_available": snapshot["eur_available"],
        "crypto_assets_eur": snapshot["crypto_assets_eur"],
        "total_portfolio_eur": snapshot["total_portfolio_eur"],
    }
    try:
        if os.path.exists(PORTFOLIO_HISTORY_CSV):
            df = pd.read_csv(PORTFOLIO_HISTORY_CSV)
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        else:
            df = pd.DataFrame([row])
        # keep last 10k rows
        df = df.tail(10000)
        df.to_csv(PORTFOLIO_HISTORY_CSV, index=False)
    except Exception:
        # history is nice-to-have; snapshot is must-have
        pass

    return snapshot


# =========================
# UI
# =========================
st.set_page_config(page_title="Crypto AI Dashboard", layout="wide")
st.title("📊 Crypto AI Dashboard")
st.caption("Render-proof: Bitvavo saldo + portfolio waarde + koersgrafiek + trades/monitoring (audit-proof)")

tabs = st.tabs(["💶 Saldo & Portfolio", "📉 Koers & Trades", "📈 Trades & Performance", "🧠 Bot Status / Masterlijst"])


# =========================
# TAB 1: Saldo & Portfolio
# =========================
with tabs[0]:
    colL, colR = st.columns([2, 1], vertical_alignment="top")

    with colL:
        meta = file_meta(SNAPSHOT_PATH)
        if meta["exists"]:
            st.success(f"Snapshot gevonden ✅  | Laatst aangepast: {meta['modified']} | Grootte: {meta['size_kb']} KB")
        else:
            st.warning("Nog geen snapshot gevonden. Klik rechts op **Snapshot verversen**.")

        st.write("Snapshot pad (Render Disk):")
        st.code(SNAPSHOT_PATH, language="text")

        st.write("Portfolio history pad (Render Disk):")
        st.code(PORTFOLIO_HISTORY_CSV, language="text")

    with colR:
        st.subheader("Acties")
        if st.button("🔄 Snapshot verversen (saldo + EUR waardes)", use_container_width=True):
            try:
                _price_cache.clear()
                snap = build_snapshot()
                st.success("Snapshot aangemaakt/ververst ✅")
                st.caption("Tip: als je net een coin hebt gekocht/verkocht, klik nog 1x om alles te syncen.")
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
            eur_available = float(snap.get("eur_available", 0) or 0)
            crypto_assets_eur = float(snap.get("crypto_assets_eur", 0) or 0)
            total_portfolio_eur = float(snap.get("total_portfolio_eur", eur_available + crypto_assets_eur) or 0)

            c1, c2, c3 = st.columns(3)
            c1.metric("Beschikbaar saldo (EUR)", fmt_eur(eur_available))
            c2.metric("Crypto assets", fmt_eur(crypto_assets_eur))
            c3.metric("Totale waarde portfolio", fmt_eur(total_portfolio_eur))

            assets = snap.get("assets", [])
            df_assets = pd.DataFrame(assets) if assets else pd.DataFrame(columns=["symbol", "available", "inOrder", "total", "price_eur", "eur_value", "price_route"])

            st.subheader("Assets (alleen > 0) — inclusief waarde in €")
            # reindex voorkomt KeyError als velden ontbreken
            df_assets = df_assets.reindex(columns=["symbol", "available", "inOrder", "total", "price_eur", "eur_value", "price_route"], fill_value=None)
            st.dataframe(df_assets, use_container_width=True, hide_index=True)

            # Pie chart asset verdeling (op EUR waarde)
            df_pie = df_assets.copy()
            if "eur_value" in df_pie.columns:
                df_pie["eur_value"] = pd.to_numeric(df_pie["eur_value"], errors="coerce").fillna(0.0)
                df_pie = df_pie[df_pie["eur_value"] > 0]
                if len(df_pie) > 0:
                    st.subheader("Asset-verdeling (op € waarde)")
                    fig_pie = go.Figure(data=[go.Pie(labels=df_pie["symbol"], values=df_pie["eur_value"], hole=0.35)])
                    fig_pie.update_layout(margin=dict(l=10, r=10, t=10, b=10))
                    st.plotly_chart(fig_pie, use_container_width=True)
                else:
                    st.info("Nog geen EUR-waardes voor coins gevonden (markets ontbreken of prijzen zijn nog niet beschikbaar).")

        else:
            st.warning("Snapshot bestaat, maar status is niet OK.")

    st.info(
        "Let op: als je in je browser naar `https://api.bitvavo.com/v2/balance` gaat, krijg je altijd een auth-fout. "
        "Dat is normaal — private endpoints werken alleen met headers/signature vanuit je code."
    )


# =========================
# TAB 2: Koers & Trades (portfolio grafiek + % changes + trade markers)
# =========================
with tabs[1]:
    st.subheader("Portfolio koersgrafiek (Bitvavo-stijl)")

    df_hist, herr = safe_read_csv(PORTFOLIO_HISTORY_CSV)
    if herr:
        st.warning(herr)
        st.info("Klik in tab **Saldo & Portfolio** op **Snapshot verversen** om automatisch history op te bouwen.")
    else:
        df_hist["ts"] = pd.to_datetime(df_hist["ts"], errors="coerce", utc=True)
        df_hist = df_hist.dropna(subset=["ts"]).sort_values("ts")
        for col in ["eur_available", "crypto_assets_eur", "total_portfolio_eur"]:
            df_hist[col] = pd.to_numeric(df_hist.get(col), errors="coerce").fillna(0.0)

        # % verandering dag/week (op total_portfolio_eur)
        latest = df_hist.iloc[-1]
        latest_val = float(latest["total_portfolio_eur"])

        def pct_change(lookback_hours: int):
            target_time = latest["ts"] - pd.Timedelta(hours=lookback_hours)
            past = df_hist[df_hist["ts"] <= target_time]
            if len(past) == 0:
                return None
            past_val = float(past.iloc[-1]["total_portfolio_eur"])
            if past_val == 0:
                return None
            return (latest_val - past_val) / past_val * 100.0

        day_pct = pct_change(24)
        week_pct = pct_change(24 * 7)

        c1, c2, c3 = st.columns(3)
        c1.metric("Totale waarde (laatst)", fmt_eur(latest_val))
        c2.metric("% verandering (24h)", "N/A" if day_pct is None else f"{day_pct:.2f}%")
        c3.metric("% verandering (7d)", "N/A" if week_pct is None else f"{week_pct:.2f}%")

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=df_hist["ts"], y=df_hist["total_portfolio_eur"],
            mode="lines", name="Portfolio (€)"
        ))
        fig.update_layout(
            height=420,
            margin=dict(l=10, r=10, t=10, b=10),
            xaxis_title="Tijd",
            yaxis_title="Waarde (€)",
        )

        # Trades markers (als timestamps bestaan)
        df_trades, terr = safe_read_csv(PAPER_TRADES_CSV)
        if not terr and df_trades is not None and len(df_trades) > 0:
            cols = {c.lower(): c for c in df_trades.columns}
            ts_col = None
            for candidate in ["timestamp", "time", "ts", "date", "datetime"]:
                if candidate in cols:
                    ts_col = cols[candidate]
                    break

            side_col = None
            for candidate in ["side", "action", "type"]:
                if candidate in cols:
                    side_col = cols[candidate]
                    break

            if ts_col:
                df_trades["_ts"] = pd.to_datetime(df_trades[ts_col], errors="coerce", utc=True)
                df_trades = df_trades.dropna(subset=["_ts"])
                # alleen markers als ze in tijd-range vallen
                df_trades = df_trades[(df_trades["_ts"] >= df_hist["ts"].min()) & (df_trades["_ts"] <= df_hist["ts"].max())]

                if len(df_trades) > 0:
                    # y-waarde: neem portfolio waarde "asof" tijd
                    hist_idx = df_hist.set_index("ts")["total_portfolio_eur"].sort_index()

                    ys = []
                    for t in df_trades["_ts"]:
                        # asof
                        s = hist_idx[hist_idx.index <= t]
                        ys.append(float(s.iloc[-1]) if len(s) else float(hist_idx.iloc[0]))

                    labels = []
                    if side_col:
                        labels = df_trades[side_col].astype(str).tolist()
                    else:
                        labels = ["TRADE"] * len(df_trades)

                    fig.add_trace(go.Scatter(
                        x=df_trades["_ts"], y=ys,
                        mode="markers", name="Trades",
                        text=labels,
                        hovertemplate="Trade: %{text}<br>%{x}<br>€ %{y:.2f}<extra></extra>"
                    ))

        st.plotly_chart(fig, use_container_width=True)

        st.caption("Tip: hoe vaker je snapshot ververst (bijv. elke 15-30 min met cron), hoe strakker je koersgrafiek.")


# =========================
# TAB 3: Trades & Performance
# =========================
with tabs[2]:
    st.subheader("Trades & Performance (Paper)")

    df_trades, err = safe_read_csv(PAPER_TRADES_CSV)

    left, right = st.columns([2, 1], vertical_alignment="top")

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
        for candidate in ["pnl", "profit", "pnl_eur", "profit_eur", "pnl_usdt", "profit_usdt"]:
            if candidate in cols:
                pnl_col = colmap[candidate]
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
        else:
            st.info(
                "Ik kan nog geen PnL grafieken maken, omdat je CSV geen herkenbare PnL kolom heeft. "
                "Tip: voeg een kolom toe zoals `pnl` of `profit` in `paper_trades.csv`."
            )


# =========================
# TAB 4: Bot Status / Masterlijst
# =========================
with tabs[3]:
    st.subheader("Bot Status / Masterlijst (wat we willen zien + of het al werkt)")

    st.markdown("""
**Dit is jouw masterlijst (dashboard moet dit uiteindelijk allemaal kunnen tonen):**
- ✅ **Saldo & Assets** (Bitvavo snapshot + EUR waardes)
- ✅ **Portfolio koersgrafiek** (history csv)
- ✅ **% verandering (dag/week)** (op basis van history)
- ✅ **Pending approvals** (Pre-BUY wachtrij + expiry + gekozen bedrag)
- ✅ **Open posities** (paper_state / live later)
- ✅ **Trades history** (paper_trades.csv)
- ✅ **Performance metrics** (PnL, winrate, equity, drawdown, streaks)
- ✅ **Bot health** (laatste run per service/script + errors)
- ✅ **AI usage** (calls per dag/maand + kosten-guardrails)
- ✅ **Signal kwaliteit** (scores, filters, hitrate per score-band)
- ✅ **R-metrics** (R bij exit, partials 40/60, STRUCTUUR-MODE activaties)
- ✅ **Shadow trades** (later: alles loggen ook als je ‘NO’ zegt)
- ✅ **Weekly reporting** (later: weekoverzicht + learnings)
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
