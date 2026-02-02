import os
import json
import time
import hmac
import hashlib
from datetime import datetime, timezone

import requests
import pandas as pd
import streamlit as st


# =========================
# Config
# =========================
# Strip voorkomt fouten door spaties/quotes in Render env vars
API_KEY = (os.getenv("BITVAVO_API_KEY", "") or "").strip().strip('"').strip("'")
API_SECRET = (os.getenv("BITVAVO_API_SECRET", "") or "").strip().strip('"').strip("'")

# ✅ Render Disk snapshot path
SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", "/data/account_snapshot.json")

# ✅ Bitvavo base URL ZONDER /v2
BASE_URL = "https://api.bitvavo.com"
ACCESS_WINDOW_MS = "10000"  # Bitvavo access window

# ✅ Files (Render disk)
PENDING_PATH = os.getenv("PENDING_PATH", "/data/pending_approvals.json")
PAPER_STATE_PATH = os.getenv("PAPER_STATE_PATH", "/data/paper_state.json")

# 🚨 FIX: trades/logs ook op /data (persistent)
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


# =========================
# Bitvavo Private Request (SIGNING)
# =========================
def bitvavo_request(method: str, path: str, body: str = ""):
    """
    Bitvavo private request:
    signature = HMAC_SHA256(secret, timestamp + METHOD + path + body)

    ✅ path moet exact zijn, inclusief /v2
    Voorbeeld: path = "/v2/balance"
    """
    if not API_KEY or not API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY of BITVAVO_API_SECRET ontbreekt in Render Environment Variables.")

    method_u = method.upper()
    timestamp = str(int(time.time() * 1000))

    # GET -> body altijd "" (leeg)
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
    """
    Haal balans op en schrijf snapshot naar Render Disk (/data).
    """
    # 🚨 FIX: path moet /v2/balance zijn (ook voor signature!)
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


# =========================
# UI
# =========================
st.set_page_config(page_title="Crypto AI Dashboard", layout="wide")
st.title("📊 Crypto AI Dashboard")
st.caption("Live overzicht van saldo, assets, trades, performance en bot-status (Render-proof)")

tabs = st.tabs(["💶 Saldo & Assets", "📈 Trades & Performance", "🧠 Bot Status / Masterlijst"])


# =========================
# TAB 1: Saldo & Assets
# =========================
with tabs[0]:
    colL, colR = st.columns([2, 1])

    with colL:
        st.subheader("Snapshot instellingen")
        st.write("Snapshot pad (Render Disk):")
        st.code(SNAPSHOT_PATH, language="text")

        meta = file_meta(SNAPSHOT_PATH)
        if meta["exists"]:
            st.success(f"Snapshot gevonden ✅  | Laatst aangepast: {meta['modified']} | Grootte: {meta['size_kb']} KB")
        else:
            st.warning("Nog geen snapshot gevonden. Klik op **Snapshot (saldo) verversen**.")

    with colR:
        st.subheader("Actie")
        if st.button("🔄 Snapshot (saldo) verversen", use_container_width=True):
            try:
                snap = build_snapshot()
                st.success("Snapshot aangemaakt/ververst ✅")
                st.json(snap)
            except Exception as e:
                st.error(f"Snapshot fout: {e}")
                # Extra debug info (helpt bij env var issues)
                st.caption(f"API_KEY len: {len(API_KEY)} | API_SECRET len: {len(API_SECRET)}")

    snap, err = safe_read_json(SNAPSHOT_PATH)
    st.divider()

    if err:
        st.warning(err)
    else:
        if snap and snap.get("status") == "OK":
            st.success("Bitvavo saldo opgehaald ✅")
            st.metric("EUR beschikbaar", f"€ {snap.get('eur_available', 0):.2f}")

            assets = snap.get("assets", [])
            if assets:
                df_assets = pd.DataFrame(assets)
                st.subheader("Assets (alleen > 0)")
                st.dataframe(df_assets, use_container_width=True, hide_index=True)
            else:
                st.info("Geen assets gevonden (of alles is 0).")
        else:
            st.warning("Snapshot bestaat, maar status is niet OK.")

    st.info(
        "Let op: als je in je browser naar `https://api.bitvavo.com/v2/balance` gaat, krijg je altijd een auth-fout. "
        "Dat is normaal — private endpoints werken alleen met headers/signature vanuit je code."
    )


# =========================
# TAB 2: Trades & Performance
# =========================
with tabs[1]:
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
# TAB 3: Bot Status / Masterlijst
# =========================
with tabs[2]:
    st.subheader("Bot Status / Masterlijst (wat we willen zien + of het al werkt)")

    st.markdown("""
**Dit is de masterlijst die jij wilde (dashboard moet dit uiteindelijk allemaal kunnen tonen):**
- ✅ **Saldo & Assets** (Bitvavo snapshot)
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
