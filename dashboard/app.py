import os
import time
import json
import hmac
import hashlib
import requests
import streamlit as st

# =========================
# Config
# =========================
API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip()
API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()

BASE_URL = "https://api.bitvavo.com"  # LET OP: zonder /v2
TIME_WINDOW = "10000"  # ms

st.set_page_config(page_title="Crypto AI Dashboard", layout="wide")
st.title("📊 Crypto AI Dashboard")
st.caption("Live overzicht van saldo, assets en bot-status")

# =========================
# Bitvavo signing helper
# =========================
def bitvavo_request(method: str, path: str, body: str = ""):
    """
    Bitvavo signature = HMAC_SHA256(secret, timestamp + method + path + body) as HEX.
    Belangrijk: path moet '/v2/...' bevatten.
    """
    if not API_KEY or not API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY of BITVAVO_API_SECRET ontbreekt in Render Environment Variables.")

    method = method.upper()
    timestamp = str(int(time.time() * 1000))

    # message EXACT zoals Bitvavo verwacht
    message = f"{timestamp}{method}{path}{body}"

    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()  # ✅ HEX (niet base64)

    headers = {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Signature": signature,
        "Bitvavo-Access-Timestamp": timestamp,
        "Bitvavo-Access-Window": TIME_WINDOW,
        "Content-Type": "application/json",
    }

    url = f"{BASE_URL}{path}"
    r = requests.request(method, url, headers=headers, data=body, timeout=15)

    if r.status_code >= 400:
        try:
            err = r.json()
        except Exception:
            err = {"raw": r.text}
        raise RuntimeError(f"Bitvavo error {r.status_code}: {err}")

    return r.json()

# =========================
# UI
# =========================
col1, col2 = st.columns([1, 3])

with col1:
    refresh = st.button("🔄 Saldo nu verversen")

with col2:
    st.info(
        "We halen saldo LIVE op via Bitvavo.\n"
        "Krijg je nog 'signature invalid'? Dan is 99% zeker je API_SECRET verkeerd (spatie/enter of mismatch key/secret)."
    )

# Auto-load 1x
if "loaded" not in st.session_state:
    st.session_state["loaded"] = True
    refresh = True

if refresh:
    try:
        balances = bitvavo_request("GET", "/v2/balance")  # ✅ PATH MET /v2

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
        st.error(f"❌ Kan Bitvavo saldo niet ophalen: {e}")

st.divider()
st.caption("Als dit werkt, koppelen we hierna je bot-logs en trades eronder.")
