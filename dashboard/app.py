import os
import json
import time
from datetime import datetime, timezone

import requests
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt


# =========================
# Config
# =========================
API_KEY = os.getenv("BITVAVO_API_KEY")
API_SECRET = os.getenv("BITVAVO_API_SECRET")

DATA_DIR = os.getenv("DATA_DIR", "/data")

SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", f"{DATA_DIR}/account_snapshot.json")

# Paper trading files (jouw bot)
PAPER_TRADES_PATH = os.getenv("PAPER_TRADES_PATH", f"{DATA_DIR}/paper_trades.csv")
PAPER_STATE_PATH = os.getenv("PAPER_STATE_PATH", f"{DATA_DIR}/paper_state.json")

# Pre-buy / approvals (jouw bot)
PENDING_APPROVALS_PATH = os.getenv("PENDING_APPROVALS_PATH", f"{DATA_DIR}/pending_approvals.json")
PREBUY_LOG_PATH = os.getenv("PREBUY_LOG_PATH", f"{DATA_DIR}/prebuy_log.csv")

# System / events (optioneel later)
TRADE_EVENTS_PATH = os.getenv("TRADE_EVENTS_PATH", f"{DATA_DIR}/trade_events.csv")

# Kleine history voor grafiek
BALANCE_HISTORY_PATH = os.getenv("BALANCE_HISTORY_PATH", f"{DATA_DIR}/balance_history.csv")

BASE_URL = "https://api.bitvavo.com/v2"


# =========================
# Helpers
# =========================
def safe_read_json(path: str):
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def safe_read_csv(path: str):
    try:
        if not os.path.exists(path):
            return None
        return pd.read_csv(path)
    except Exception:
        return None


def safe_append_balance_history(eur_available: float):
    """
    Schrijf een simpele saldo-historie weg (tijd + eur_available).
    Zo kan je meteen een grafiek tonen, ook al heb je nog geen volledige equity engine.
    """
    try:
        now = datetime.now(timezone.utc).isoformat()
        row = pd.DataFrame([{"ts": now, "eur_available": float(eur_available)}])

        if os.path.exists(BALANCE_HISTORY_PATH):
            df = pd.read_csv(BALANCE_HISTORY_PATH)
            df = pd.concat([df, row], ignore_index=True)
        else:
            df = row

        # klein houden
        df = df.tail(2000)
        df.to_csv(BALANCE_HISTORY_PATH, index=False)
    except Exception:
        pass


# =========================
# Bitvavo signed request (zoals je al had, maar stabiel)
# =========================
def bitvavo_request(path: str):
    """
    Minimalistische signed call:
    - timestamp in ms
    - message = timestamp + "GET" + "/v2/..." path
    - signature = HMAC_SHA256(secret, message)
    """
    if not API_KEY or not API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY/SECRET ontbreken in Render Environment.")

    timestamp = str(int(time.time() * 1000))
    method = "GET"
    url_path = f"/v2{path}"
    message = timestamp + method + url_path

    import hmac
    import hashlib

    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "bitvavo-access-key": API_KEY,
        "bitvavo-access-signature": signature,
        "bitvavo-access-timestamp": timestamp,
        "bitvavo-access-window": "10000",
    }

    r = requests.get(BASE_URL + path, headers=headers, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"Bitvavo error {r.status_code}: {r.text}")
    return r.json()


def fetch_live_balance():
    """
    Haalt live balance op en geeft:
    - eur_available
    - assets_df (alleen >0)
    """
    balances = bitvavo_request("/balance")

    rows = []
    eur_available = 0.0

    for b in balances:
        symbol = b.get("symbol")
        available = float(b.get("available", 0) or 0)
        in_order = float(b.get("inOrder", 0) or 0)
        total = available + in_order

        if symbol == "EUR":
            eur_available = available

        if total > 0:
            rows.append({
                "symbol": symbol,
                "available": available,
                "inOrder": in_order,
                "total": total
            })

    df = pd.DataFrame(rows).sort_values("symbol") if rows else pd.DataFrame(columns=["symbol", "available", "inOrder", "total"])
    return eur_available, df


# =========================
# UI
# =========================
st.set_page_config(page_title="Crypto AI Dashboard", layout="wide")
st.title("📊 Crypto AI Dashboard")
st.caption("Master dashboard: saldo + trades + pre-buys + logs + grafieken")

tabs = st.tabs(["🏦 Account", "📈 Trades", "🧠 Pre-BUY", "⚙️ Systeem"])


# -------------------------
# TAB 1: ACCOUNT
# -------------------------
with tabs[0]:
    c1, c2 = st.columns([1, 2])

    with c1:
        if st.button("🔄 Saldo nu verversen"):
            try:
                eur_available, assets_df = fetch_live_balance()
                st.success("Bitvavo saldo opgehaald")
                safe_append_balance_history(eur_available)

                # schrijf ook een snapshot (handig voor andere services)
                snap = {
                    "status": "OK",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "eur_available": eur_available,
                    "assets": assets_df.to_dict(orient="records"),
                }
                os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
                with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
                    json.dump(snap, f, indent=2)

            except Exception as e:
                st.error(f"Verversen mislukt: {e}")

        st.write("Snapshot pad")
        st.code(SNAPSHOT_PATH)

    # probeer snapshot te tonen (als cron hem maakt)
    snap = safe_read_json(SNAPSHOT_PATH)

    if snap and snap.get("status") == "OK":
        st.success("Snapshot gevonden")
        st.subheader("EUR beschikbaar")
        st.markdown(f"## € {float(snap.get('eur_available', 0.0)):.2f}")

        st.subheader("Assets (alleen > 0)")
        assets = snap.get("assets", [])
        st.dataframe(pd.DataFrame(assets), use_container_width=True)
    else:
        st.info("Geen (geldige) snapshot gevonden. Klik op 'Saldo nu verversen' of laat account_snapshot.py cron draaien.")

    # saldo-historie grafiek
    hist = safe_read_csv(BALANCE_HISTORY_PATH)
    if hist is not None and len(hist) > 2:
        st.subheader("Saldo historie (EUR beschikbaar)")
        hist["ts"] = pd.to_datetime(hist["ts"], errors="coerce")
        hist = hist.dropna(subset=["ts"]).sort_values("ts")

        fig = plt.figure()
        plt.plot(hist["ts"], hist["eur_available"])
        plt.xlabel("Tijd")
        plt.ylabel("EUR beschikbaar")
        st.pyplot(fig, clear_figure=True)
    else:
        st.caption("Nog geen saldo-historie. Klik een paar keer op verversen (of laat cron lopen).")


# -------------------------
# TAB 2: TRADES
# -------------------------
with tabs[1]:
    st.subheader("Paper trades / performance")

    trades = safe_read_csv(PAPER_TRADES_PATH)
    state = safe_read_json(PAPER_STATE_PATH)

    if trades is None:
        st.warning(f"Geen trades gevonden op: {PAPER_TRADES_PATH}")
        st.caption("Fix: laat paper_trader.py naar /data/paper_trades.csv schrijven.")
    else:
        st.success(f"Trades geladen: {len(trades)}")
        st.dataframe(trades.tail(200), use_container_width=True)

        # simpele PnL samenvatting (werkt als je kolom 'pnl' hebt)
        if "pnl" in trades.columns:
            total_pnl = float(trades["pnl"].fillna(0).sum())
            st.metric("Totaal PnL", f"{total_pnl:.2f}")
        else:
            st.caption("Tip: voeg kolom 'pnl' toe in paper_trades.csv voor snelle PnL stats.")

        # equity curve (als je balance/equity kolom hebt)
        equity_col = None
        for candidate in ["balance", "equity", "account_balance"]:
            if candidate in trades.columns:
                equity_col = candidate
                break

        if equity_col:
            st.subheader("Equity curve")
            t = trades.copy()
            if "ts" in t.columns:
                t["ts"] = pd.to_datetime(t["ts"], errors="coerce")
                t = t.dropna(subset=["ts"]).sort_values("ts")

                fig = plt.figure()
                plt.plot(t["ts"], t[equity_col].astype(float))
                plt.xlabel("Tijd")
                plt.ylabel(equity_col)
                st.pyplot(fig, clear_figure=True)
            else:
                st.caption("Voor een echte curve: zet een 'ts' kolom in je trades log.")
        else:
            st.caption("Nog geen equity curve: voeg 'balance' of 'equity' + 'ts' toe aan paper_trades.csv.")

    if state:
        st.subheader("Paper state (laatste status)")
        st.json(state)
    else:
        st.caption(f"Geen paper_state.json gevonden op: {PAPER_STATE_PATH}")


# -------------------------
# TAB 3: PRE-BUY
# -------------------------
with tabs[2]:
    st.subheader("Pre-BUY & approvals")

    pending = safe_read_json(PENDING_APPROVALS_PATH)
    if pending:
        st.success("pending_approvals.json gevonden")
        st.json(pending)
    else:
        st.warning(f"Geen pending approvals gevonden op: {PENDING_APPROVALS_PATH}")
        st.caption("Fix: multi_coin_score.py moet pending_approvals.json naar /data schrijven.")

    prebuy_log = safe_read_csv(PREBUY_LOG_PATH)
    if prebuy_log is not None:
        st.subheader("Pre-BUY log")
        st.dataframe(prebuy_log.tail(300), use_container_width=True)

        # simpele score-visual
        if "score" in prebuy_log.columns:
            st.subheader("Scores (laatste 200)")
            d = prebuy_log.tail(200).copy()
            fig = plt.figure()
            plt.plot(d["score"].astype(float).values)
            plt.xlabel("Laatste signals")
            plt.ylabel("Score")
            st.pyplot(fig, clear_figure=True)
    else:
        st.caption(f"Nog geen prebuy_log.csv op: {PREBUY_LOG_PATH} (optioneel maar sterk aangeraden).")


# -------------------------
# TAB 4: SYSTEM
# -------------------------
with tabs[3]:
    st.subheader("Systeem & events")

    events = safe_read_csv(TRADE_EVENTS_PATH)
    if events is not None:
        st.success("trade_events.csv gevonden")
        st.dataframe(events.tail(300), use_container_width=True)
    else:
        st.caption(f"Nog geen event log op: {TRADE_EVENTS_PATH}")
        st.caption("Later: trade_monitor.py kan elke actie wegschrijven naar trade_events.csv (status/errors/exits).")

    st.divider()
    st.caption("Tip: als je deze tab gevuld wilt, laat elk script 1 regel per run loggen naar /data/*.csv.")
