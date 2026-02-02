import os
import json
import time
import hmac
import hashlib
import requests
from datetime import datetime, timezone

# ======================
# CONFIG (kinderlijk simpel)
# ======================
# Deze 2 komen uit Render Environment Variables
# Strippen voorkomt signature issues door spaties/quotes
BITVAVO_API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip().strip('"').strip("'")
BITVAVO_API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip().strip('"').strip("'")

# Bitvavo base URL (ZONDER /v2)
BASE_URL = "https://api.bitvavo.com"

# ✅ Render Disk pad (dit is wat je dashboard gebruikt)
RENDER_DISK_PATH = "/data/account_snapshot.json"

# ✅ Fallback voor lokaal testen (repo/data/account_snapshot.json)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_DATA_DIR = os.path.join(BASE_DIR, "data")
LOCAL_SNAPSHOT_PATH = os.path.join(LOCAL_DATA_DIR, "account_snapshot.json")

TIMEOUT = 10


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _pick_snapshot_path() -> str:
    """
    Render: /data bestaat (disk mount) → schrijf daar.
    Lokaal: /data bestaat meestal niet → schrijf naar ./data in repo.
    """
    if os.path.isdir("/data"):
        return RENDER_DISK_PATH
    os.makedirs(LOCAL_DATA_DIR, exist_ok=True)
    return LOCAL_SNAPSHOT_PATH


# ======================
# AUTH (SIGNING)
# ======================
def _auth_headers(method: str, path: str, body: str = "") -> dict:
    """
    BELANGRIJK:
    - path moet '/v2/...' zijn (alleen path, geen hele URL)
    - signing message = timestamp + method + path + body
    - timestamp in milliseconden
    - body is "" bij GET
    """
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        raise RuntimeError("Missing BITVAVO_API_KEY or BITVAVO_API_SECRET in env vars (Render Environment Variables)")

    method = method.upper()
    timestamp = str(int(time.time() * 1000))

    message = timestamp + method + path + (body or "")

    signature = hmac.new(
        BITVAVO_API_SECRET.encode("utf-8"),
        message.encode("utf-8"),
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
    path = "/v2/balance"
    url = BASE_URL + path
    headers = _auth_headers("GET", path, body="")

    r = requests.get(url, headers=headers, timeout=TIMEOUT)

    # Bij fouten wil je de Bitvavo error zien
    if r.status_code != 200:
        raise RuntimeError(f"Bitvavo balance error {r.status_code}: {r.text}")

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
    """
    ordersOpen kan bij sommige accounts/instellingen anders reageren.
    Als dit faalt, willen we nog steeds wél je balance snapshot opslaan.
    """
    path = "/v2/ordersOpen"
    url = BASE_URL + path
    headers = _auth_headers("GET", path, body="")

    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    if r.status_code != 200:
        # niet hard falen; geef 0 terug maar log de fout in snapshot
        return 0, f"Bitvavo ordersOpen error {r.status_code}: {r.text}"

    orders = r.json()
    return (len(orders) if isinstance(orders, list) else 0), None


# ======================
# SNAPSHOT
# ======================
def write_snapshot():
    snapshot_path = _pick_snapshot_path()

    snapshot = {
        "ts": _now_iso(),
        "status": "OK",
        "eur_available": 0.0,
        "assets": {},
        "open_orders": 0,
        "meta": {
            "snapshot_path": snapshot_path,
            "using_render_disk": os.path.isdir("/data"),
        }
    }

    try:
        eur, assets = get_balances()
        snapshot["eur_available"] = eur
        snapshot["assets"] = assets

        open_orders, orders_err = get_open_orders_count()
        snapshot["open_orders"] = open_orders
        if orders_err:
            snapshot["meta"]["orders_warning"] = orders_err

    except Exception as e:
        snapshot["status"] = "ERROR"
        snapshot["error"] = str(e)
        snapshot["meta"]["api_key_len"] = len(BITVAVO_API_KEY)
        snapshot["meta"]["api_secret_len"] = len(BITVAVO_API_SECRET)

    # Zorg dat map bestaat
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)

    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    print("✅ account_snapshot geschreven:", snapshot_path)


# ======================
# MAIN
# ======================
if __name__ == "__main__":
    write_snapshot()
