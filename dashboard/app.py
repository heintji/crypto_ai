import os
import json
import time
import hmac
import hashlib
import requests
import streamlit as st

# =========================
# Config
# =========================
API_KEY = os.getenv("BITVAVO_API_KEY")
API_SECRET = os.getenv("BITVAVO_API_SECRET")

# Render disk mount (jij hebt disk op /data gezet)
SNAPSHOT_PATH = os.getenv("SNAPSHOT_PATH", "/data/account_snapshot.json")

BASE_URL = "https://api.bitvavo.com/v2"


# =========================
# Helpers
# =========================
def bitvavo_request(path: str, method: str = "GET", body: str = ""):
    """
    Signed request naar Bitvavo private endpoints.
    """
    if not API_KEY or not API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY of BITVAVO_API_SECRET ontbreekt (Environment Variables).")

    ts = str(int(time.time() * 1000))
    message = ts + method.upper() + path + body

    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "bitvavo-access-key": API_KEY,
        "bitvavo-access-signature": signature,
        "bitvavo-access-timestamp": ts,
        "bitvavo-access-window": "10000",
        "Content-Type": "application/json",
    }

    url = BASE_URL + path
    r = requests.request(method.upper(), url, headers=headers, data=body, timeout=20)

    if r.status_code >= 400:
        # Probeer wat extra info te geven
        try:
            err = r.json()
        except Exception:
            err = r.text
        raise RuntimeError(f"Bitvavo error {r.status_code}: {err}")

    return r.json()


def create_snapshot():
    """
    Maakt een nieuwe snapshot en schrijft hem weg.
    """
    balances = bitvavo_request("/balance")

    snapshot = {
        "status": "OK",
        "timestamp": int(time.time()),
        "balances": balances,
    }

    # Zorg dat map bestaat (bij /data is dat normaal al zo, maar veilig)
    folder = os.path.dirname(SNAPSHOT_PATH) or "."
    os.makedirs(folder, exist_ok=True)

    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)

    return snapshot


def load_snapshot():
    """
    Leest snapshot. Bestaat hij niet? Dan maken we hem eerst aan.
    """
    if not os.path.exists(SNAPSHOT_PATH):
        return create_snapshot()

    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def eur_available_from_balances(balances):
    """
    Probeert EUR free/available uit Bitvavo balances te halen.
    Bitvavo geeft velden zoals: symbol, available, inOrder
    """
    for b in balances:
        if str(b.get("symbol", "")).upper() == "EUR":
            # available kan string zijn
            try:
                return float(b.get("available", 0))
            except Exception:
                return b.get("available", 0)
    return 0.0


def nonzero_assets(balances, min_amount=0.00000001):
    """
    Filtert assets met available+inOrder > 0
    """
    out = []
    for b in balances:
        sym = str(b.get("symbol", "")).upper()
        try:
            available = float(b.get("available", 0))
        except Exception:
            available = 0.0
        try:
            in_order = float(b.get("inOrder", 0))
        except Exception:
            in_order = 0.0

        total = available + in_order
        if sym and total > min_amount:
            out.append({
                "symbol": sym,
                "available": available,
                "inOrder": in_order,
                "total": total
            })
    # Sort op grootste total
    out.sort(key=lambda x: x["total"], reverse=True)
    return out


# =========================
# UI
# =========================
st.set_page_config(page_title="Crypto AI Dashboard", layout="wide")

st.title("📊 Crypto AI Dashboard")
st.caption("Live overzicht van saldo, assets en bot-status")

colA, colB, colC = st.columns([1, 1, 2])

with colA:
    st.write("**Snapshot pad**")
    st.code(SNAPSHOT_PATH)

with colB:
    if st.button("🔄 Snapshot nu verversen"):
        try:
            snap = create_snapshot()
            st.success("Snapshot vernieuwd ✅")
        except Exception as e:
            st.error(f"Verversen mislukt: {e}")
            st.stop()

with colC:
    st.info("Tip: Als je ooit weer 'snapshot niet gevonden' ziet, betekent dat meestal dat er nog nooit een snapshot is aangemaakt. Dit script maakt hem nu automatisch aan.")


# Snapshot laden (of aanmaken)
try:
    snapshot = load_snapshot()
except Exception as e:
    st.error(f"Kan snapshot niet laden/maken: {e}")
    st.stop()

# Als snapshot status ERROR is (als jij dat later zo opslaat), laten we het zien
if snapshot.get("status") and snapshot.get("status") != "OK":
    st.warning(f"Snapshot status: {snapshot.get('status')}")
    if snapshot.get("error"):
        st.code(snapshot.get("error"))

balances = snapshot.get("balances", [])
if not balances:
    st.error("Geen balances gevonden in snapshot.")
    st.stop()

# KPI's
eur_av = eur_available_from_balances(balances)
assets = nonzero_assets(balances)

k1, k2, k3 = st.columns(3)
k1.metric("EUR beschikbaar", f"{eur_av:,.2f}")
k2.metric("Aantal assets > 0", f"{len(assets)}")
ts = snapshot.get("timestamp", 0)
if ts:
    readable = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
else:
    readable = "onbekend"
k3.metric("Laatste snapshot", readable)

st.divider()

# Assets tabel
st.subheader("Assets")
if len(assets) == 0:
    st.write("Geen assets (behalve EUR) gevonden.")
else:
    st.dataframe(assets, use_container_width=True)

st.divider()

# Debug sectie (uitklapbaar)
with st.expander("🔍 Debug (handig bij fouten)"):
    st.write("**Environment checks**")
    st.write(f"API_KEY gezet: {'JA' if bool(API_KEY) else 'NEE'}")
    st.write(f"API_SECRET gezet: {'JA' if bool(API_SECRET) else 'NEE'}")
    st.write("**Ruwe snapshot JSON**")
    st.json(snapshot)
