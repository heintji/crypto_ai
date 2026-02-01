import os
import json
import time
import hmac
import hashlib
import requests

# ======================
# CONFIG (kinderlijk simpel)
# ======================
# Deze 2 komen uit Render Environment Variables
BITVAVO_API_KEY = os.getenv("BITVAVO_API_KEY")
BITVAVO_API_SECRET = os.getenv("BITVAVO_API_SECRET")

# Bitvavo base URL (ZONDER /v2)
BASE_URL = "https://api.bitvavo.com"

# Opslaan in ./data/account_snapshot.json naast dit bestand
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
SNAPSHOT_PATH = os.path.join(DATA_DIR, "account_snapshot.json")

TIMEOUT = 10


# ======================
# AUTH (SIGNING)
# ======================
def _auth_headers(method: str, path: str, body: str = "") -> dict:
    """
    BELANGRIJK:
    - path moet '/v2/...' zijn
    - signing message = timestamp + method + path + body
    """
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        raise RuntimeError("Missing BITVAVO_API_KEY or BITVAVO_API_SECRET in env vars")

    method = method.upper()
    timestamp = str(int(time.time() * 1000))

    message = timestamp + method + path + body

    signature = hmac.new(
        BITVAVO_API_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()

    return {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": signature,
        "Bitvavo-Access-Timestamp": timestamp,
        "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json",
    }


# ======================
# API CALLS
# ======================
def get_balances():
    # ✅ Hier zat jouw fout eerder: '/v2' moet in de path staan
    path = "/v2/balance"
    url = BASE_URL + path
    headers = _auth_headers("GET", path)

    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()

    data = r.json()

    eur_available = 0.0
    assets = {}

    for item in data:
        symbol = item.get("symbol")
        available = float(item.get("available", "0") or 0)

        if symbol == "EUR":
            eur_available = available
        elif available > 0:
            assets[symbol] = available

    return eur_available, assets


def get_open_orders_count():
    path = "/v2/ordersOpen"
    url = BASE_URL + path
    headers = _auth_headers("GET", path)

    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()

    orders = r.json()
    return len(orders) if isinstance(orders, list) else 0


# ======================
# SNAPSHOT
# ======================
def write_snapshot():
    os.makedirs(DATA_DIR, exist_ok=True)

    snapshot = {
        "timestamp": int(time.time()),
        "status": "OK",
        "eur_available": 0.0,
        "assets": {},
        "open_orders": 0
    }

    try:
        eur, assets = get_balances()
        snapshot["eur_available"] = eur
        snapshot["assets"] = assets
        snapshot["open_orders"] = get_open_orders_count()

    except Exception as e:
        snapshot["status"] = "ERROR"
        snapshot["error"] = str(e)

    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)

    print("✅ account_snapshot geschreven:", SNAPSHOT_PATH)


# ======================
# MAIN
# ======================
if __name__ == "__main__":
    write_snapshot()
