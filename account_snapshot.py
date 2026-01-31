import os
import json
import time
import hmac
import hashlib
import requests

BITVAVO_API_KEY = os.getenv("BITVAVO_API_KEY")
BITVAVO_API_SECRET = os.getenv("BITVAVO_API_SECRET")

BASE_URL = "https://api.bitvavo.com/v2"
SNAPSHOT_PATH = os.path.join("data", "account_snapshot.json")
TIMEOUT = 10


def _auth_headers(method: str, path: str, body: str = "") -> dict:
    timestamp = str(int(time.time() * 1000))
    msg = timestamp + method + path + body
    signature = hmac.new(
        BITVAVO_API_SECRET.encode(),
        msg.encode(),
        hashlib.sha256
    ).hexdigest()

    return {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": signature,
        "Bitvavo-Access-Timestamp": timestamp,
        "Content-Type": "application/json"
    }


def get_balances() -> dict:
    path = "/balance"
    url = BASE_URL + path
    headers = _auth_headers("GET", path)

    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()

    balances = {}
    eur_available = 0.0

    for item in r.json():
        symbol = item["symbol"]
        available = float(item["available"])
        in_order = float(item["inOrder"])

        if symbol == "EUR":
            eur_available = available

        balances[symbol] = {
            "available": available,
            "inOrder": in_order
        }

    return eur_available, balances


def get_open_orders_count() -> int:
    path = "/ordersOpen"
    url = BASE_URL + path
    headers = _auth_headers("GET", path)

    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()

    return len(r.json())


def write_snapshot():
    os.makedirs("data", exist_ok=True)

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


if __name__ == "__main__":
    write_snapshot()

    if os.getenv("OPENAI_API_KEY"):
        pass  # AI-analyse komt later

