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
# Bitvavo public prices (ONE CALL)
# =========================
def fetch_all_market_prices():
    """
    Public endpoint (geen key):
    GET /v2/ticker/price  -> lijst met dicts: {"market":"BTC-EUR","price":"12345"}
    We doen 1 call en bouwen een map: prices["BTC-EUR"] = 12345.0
    """
    url = f"{BASE_URL}/v2/ticker/price"
    headers = {
        # user-agent helpt soms bij hosting/proxies
        "User-Agent": "crypto-ai-dashboard/1.0"
    }
    r = requests.get(url, headers=headers, timeout=20)

    if r.status_code >= 400:
        raise RuntimeError(f"Public ticker error {r.status_code}: {r.text[:200]}")

    data = r.json()
    if not isinstance(data, list):
        # onverwacht response-type
        raise RuntimeError(f"Public ticker onverwachte response: {type(data)}")

    prices = {}
    for row in data:
        try:
            m = row.get("market")
            p = row.get("price")
            if m and p is not None:
                prices[m] = float(p)
        except Exception:
            continue

    return prices


def resolve_price_eur(symbol: str, prices: dict):
    """
    EUR prijs resolutie:
    1) SYMBOL-EUR
    2) SYMBOL-USDT * USDT-EUR
    3) SYMBOL-BTC  * BTC-EUR
    """
    if symbol == "EUR":
        return 1.0, "EUR"

    m1 = f"{symbol}-EUR"
    if m1 in prices:
        return prices[m1], m1

    m2a = f"{symbol}-USDT"
    m2b = "USDT-EUR"
    if m2a in prices and m2b in prices:
        return prices[m2a] * prices[m2b], f"{m2a} * {m2b}"

    m3a = f"{symbol}-BTC"
    m3b = "BTC-EUR"
    if m3a in prices and m3b in prices:
        return prices[m3a] * prices[m3b], f"{m3a} * {m3b}"

    return None, None


# =========================
# Snapshot builder (+ EUR waarden + history)
# =========================
def build_snapshot():
    """
    Haal balans op, haal public prijzen op (1 call),
    verrijk assets met EUR value, schrijf snapshot + history.
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
            assets.append({
                "symbol": symbol,
                "available": available,
                "inOrder": in_order,
                "total": total
            })

    crypto_assets_eur = 0.0
    enriched = []

    for a in assets:
        sym = a["symbol"]
        price_eur, route = resolve_price_eur(sym, prices)

        if sym == "EUR":
            eur_value = float(a["total"])
        else:
            if price_eur is None:
                eur_value = 0.0
            else:
                eur_value = float(a["total"]) * float(price_eur)
                crypto_assets_eur += eur_value

        a2 = dict(a)
        a2["price_eur"] = None if (price_eur is None or sym == "EUR") else float(price_eur)
        a2["eur_value"] = float(eur_value)
        a2["price_route"] = route
        enriched.append(a2)

    total_portfolio_eur = float(eur_available) + float(crypto_assets_eur)

    snapshot = {
        "status": "OK",
        "ts": now_iso(),
        "eur_available": float(eur_available),
        "crypto_assets_eur": float(crypto_assets_eur),
        "total_portfolio_eur": float(total_portfolio_eur),
        "assets": sorted(enriched, key=lambda x: (x["symbol"] != "EUR", x["symbol"])),
        "prices_loaded": int(len(prices)),
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

        df = df.tail(10000)
        df.to_csv(PORTFOLIO_HISTORY_CSV, index=False)
    except Exception:
        pass

    return snapshot


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
                snap = build_snapshot()
                st.success("Snapshot aangemaakt/ververst ✅")
                st.caption(f"Public prices loaded: {snap.get('prices_loaded', 0)} markets")
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
            df_assets = df_assets.reindex(columns=["symbol", "available", "inOrder", "total", "price_eur", "eur_value", "price_route"], fill_value=None)
            st.dataframe(df_assets, use_container_width=True, hide_index=True)

            # Pie chart asset verdeling (op EUR waarde)
            df_pie = df_assets.copy()
            df_pie["eur_value"] = pd.to_numeric(df_pie.get("eur_value"), errors="coerce").fillna(0.0)
            df_pie = df_pie[df_pie["eur_value"] > 0]

            if len(df_pie) > 0:
                st.subheader("Asset-verdeling (op € waarde)")
                fig_pie = go.Figure(data=[go.Pie(labels=df_pie["symbol"], values=df_pie["eur_value"], hole=0.35)])
                fig_pie.update_layout(margin=dict(l=10, r=10, t=10, b=10))
                st.plotly_chart(fig_pie, use_container_width=True)
            else:
                st.warning("Nog geen EUR-waardes berekend. Klik nogmaals op Snapshot verversen (of er zijn geen markets gevonden).")

            with st.expander("🔎 Debug (prijzen / routes)", expanded=False):
                st.write(f"Prices loaded (markets): {snap.get('prices_loaded', 0)}")
                # toon alleen rows waar price_route None is (handig)
                if len(df_assets) > 0:
                    missing = df_assets[df_assets["price_route"].isna() & (df_assets["symbol"] != "EUR")]
                    st.write("Coins zonder route/prijs:")
                    st.dataframe(missing[["symbol", "total", "price_route", "price_eur", "eur_value"]], use_container_width=True, hide_index=True)

        else:
            st.warning("Snapshot bestaat, maar status is niet OK.")

    st.info(
        "Let op: `https://api.bitvavo.com/v2/balance` geeft in je browser een auth-fout. "
        "Dat is normaal — private endpoints werken alleen met headers/signature vanuit je code."
    )


# =========================
# TAB 2: Koers & Trades
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
        st.plotly_chart(fig, use_container_width=True)

        st.caption("Tip: hoe vaker je snapshot ververst (of later via cron), hoe strakker je koersgrafiek.")


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
            st.info("Nog geen herkenbare PnL kolom gevonden in je CSV. (tip: `pnl` of `profit`).")


# =========================
# TAB 4: Bot Status / Masterlijst
# =========================
with tabs[3]:
    st.subheader("Bot Status / Masterlijst")

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
    st.json(pending if not err else {"info": err})

    st.write("### Paper state (preview)")
    pstate, err = safe_read_json(PAPER_STATE_PATH)
    st.json(pstate if not err else {"info": err})

    st.write("### AI usage (preview)")
    aiu, err = safe_read_json(AI_USAGE_PATH)
    st.json(aiu if not err else {"info": err})

    st.success("✅ Controlepaneel klaar: je ziet direct welke data goed wegschrijft en wat nog mist.")
