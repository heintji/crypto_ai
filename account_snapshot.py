import os
import json
import time
import hmac
import hashlib
from typing import Dict, Any, Tuple
import requests

# (optioneel) lokaal .env laden (op Render niet nodig, daar staan env vars al)
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# =========================
# Settings
# =========================
BITVAVO_API_KEY = os.getenv("BITVAVO_API_KEY")
BITVAVO_API_SECRET = os.getenv("BITVAVO_API_SECRET")

BASE_URL = "https://api.bitvavo.com/v2"
TIMEOUT = 15
ACCESS_WINDOW_MS = "10000"  # Bitvavo window (ms)

SNAPSHOT_PATH = os.path.join("data", "account_snapshot.json")


# =========================
# Helpers
# =========================
def _ensure_dirs() -> None:
    os.makedirs("data", exist_ok=True)
    os.makedirs("logs", exist_ok=True)


def _auth_headers(method: str, path: str, body: str = "") -> Dict[str, str]:
    """
    Bitvavo signing:
    signature = HMAC_SHA256(secret, timestamp + method + path + body)
    """
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        raise RuntimeError("Missing BITVAVO_API_KEY / BITVAVO_API_SECRET in environment variables")

    timestamp = str(int(time.time() * 1000))
    msg = timestamp + method.upper() + path + body

    signature = hmac.new(
        BITVAVO_API_SECRET.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": signature,
        "Bitvavo-Access-Timestamp": timestamp,
        "Bitvavo-Access-Window": ACCESS_WINDOW_MS,
        "Content-Type": "application/json",
    }


def _bitvavo_get(path: str, params: Dict[str, Any] | None = None) -> Any:
    url = BASE_URL + path
    headers = _auth_headers("GET", path)
    r = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_balances() -> Tuple[float, Dict[str, float]]:
    """
    Returns:
      eur_available (float),
      assets dict (symbol -> total amount)
    """
    data = _bitvavo_get("/balance")  # GET /v2/balance

    eur_available = 0.0
    assets: Dict[str, float] = {}

    for item in data:
        symbol = item.get("symbol")
        available = float(item.get("available", 0.0))
        in_order = float(item.get("inOrder", 0.0))
        total = available + in_order

        if symbol == "EUR":
            eur_available = available

        # Alleen coins tonen die > 0 zijn
        if symbol and symbol != "EUR" and total > 0:
            assets[symbol] = round(total, 12)

    return eur_available, assets


def get_open_orders_count() -> int:
    """
    Bitvavo: GET /v2/ordersOpen
    """
    data = _bitvavo_get("/ordersOpen")
    if isinstance(data, list):
        return len(data)
    return 0


def write_snapshot() -> Dict[str, Any]:
    _ensure_dirs()

    snapshot: Dict[str, Any] = {
        "timestamp": int(time.time()),
        "status": "OK",
        "eur_available": 0.0,
        "assets": {},
        "open_orders": 0,
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
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    # Kleine log voor Render logs
    print("✅ account_snapshot geschreven:", SNAPSHOT_PATH, flush=True)
    print(f"Status: {snapshot.get('status')} | EUR beschikbaar: {snapshot.get('eur_available')} | assets: {len(snapshot.get('assets', {}))} | open_orders: {snapshot.get('open_orders')}", flush=True)

    return snapshot


# =========================
# Main
# =========================
if __name__ == "__main__":
    write_snapshot()
