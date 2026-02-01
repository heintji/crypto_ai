import os
import json
import time
import hmac
import hashlib
import base64
import requests
import streamlit as st

# =========================
# Config
# =========================
API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip()
API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()

BASE_URL = "https://api.bitvavo.com/v2"
TIME_WINDOW = "10000"  # ms, standaard prima

st.set_page_config(page_title="Crypto AI Dashboard", layout="wide")
st.title("📊 Crypto AI Dashboard")
st.caption("Live overzicht van saldo, assets en bot-status")

# =========================
# Bitvavo signing helper
# =========================
def bitvavo_request(method: str, path: str, body: str = ""):
    """
    method: 'GET' / 'POST' etc.
    path: '/balance' etc (MOET beginnen met '/')
    body: string JSON (bij GET meestal '')
    """
    if not API_KEY or not API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY of BITVAVO_API_SECRET ontbreekt in Environment Variables.")

    timestamp = str(int(time.time() * 1000))
    method = method.upper()

    # Bitvavo signing: timestamp + method + path + body
    message = f"{timestamp}{method}{path}{body}"

    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).digest()
    signature_b64 = base64.b64encode(signature).decode("utf-8")

    headers = {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Signature": signature_b64,
        "Bitvavo-Access-Timestamp": timestamp,
        "Bitvavo-Access-Window": TIME_WINDOW,
        "Content-Type": "application/json",
    }

    url = f"{BASE_URL}{path}"
    r = requests.request(method, url, headers=headers, data=body, timeout=15)

    if r.status_code >= 400:
        # geef Bitvavo error terug zodat jij exact ziet wat er misgaat
        try:
            err_json = r.json()
        except Exception:
            err_json = {"raw": r.text}
        raise RuntimeError(f"Bitvavo error {r.status_code}: {err_json}")

    return r.json()


# =========================
# UI
# =========================
col1, col2 = st.columns([1, 2])

with col1:
    if st.button("🔄 Saldo nu verversen"):
        st.session_state["refresh"] = True

with col2:
    st.info(
        "Tip: Als je eerder 'snapshot niet gevonden' zag, kwam dat doordat je dashboard een file zocht.\n"
        "We doen nu LIVE Bitvavo ophalen (betrouwbaarder op Render)."
    )

# automatisch refresh 1x bij load
if "refresh" not in st.session_state:
    st.session_state["refresh"] = True

if st.session_state["refresh"]:
    st.session_state["refresh"] = False

    try:
        balances = bitvavo_request("GET", "/balance")

        # Maak overzicht
        eur_available = 0.0
        assets = []

        for b in balances:
            symbol = b.get("symbol")
            available = float(b.get("available", "0") or "0")
            in_order = float(b.get("inOrder", "0") or "0")
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

        st.success("✅ Bitvavo saldo opgehaald")

        st.metric("EUR beschikbaar", f"€ {eur_available:,.2f}")

        st.subheader("Assets (alleen > 0)")
        if assets:
            st.dataframe(assets, use_container_width=True)
        else:
            st.write("Geen assets met saldo > 0 gevonden.")

    except Exception as e:
        st.error(f"Kan Bitvavo saldo niet ophalen: {e}")

st.divider()
st.caption("Volgende stap: als dit werkt, koppelen we je monitoring/trade logs eronder.")
