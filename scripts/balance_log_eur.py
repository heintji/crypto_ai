import time
import hmac
import hashlib
import requests
import os
import json
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("BITVAVO_API_KEY")
API_SECRET = os.getenv("BITVAVO_API_SECRET")

BASE_URL = "https://api.bitvavo.com/v2"
LOG_PATH = os.path.join("logs", "balance_log.csv")


def d(x) -> Decimal:
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError):
        return Decimal("0")


def get_balance():
    if not API_KEY or not API_SECRET:
        return {"error": "API_KEY of API_SECRET ontbreekt. Check je .env."}

    timestamp = int(time.time() * 1000)
    method = "GET"
    path = "/balance"

    signing_path = "/v2" + path
    message = f"{timestamp}{method}{signing_path}"

    signature = hmac.new(
        API_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Signature": signature,
        "Bitvavo-Access-Timestamp": str(timestamp),
        "Content-Type": "application/json"
    }

    r = requests.get(BASE_URL + path, headers=headers, timeout=15)
    return r.json()


def get_price_eur(symbol: str):
    market = f"{symbol}-EUR"
    url = f"{BASE_URL}/ticker/price"
    r = requests.get(url, params={"market": market}, timeout=15)
    data = r.json()
    if isinstance(data, dict) and "price" in data:
        return d(data["price"])
    return None


def ensure_logs_folder():
    os.makedirs("logs", exist_ok=True)


def append_csv_line(dt_iso: str, total_eur: Decimal, positions: dict):
    ensure_logs_folder()
    file_exists = os.path.isfile(LOG_PATH)

    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow(["datetime", "total_eur", "positions_json"])
        w.writerow([dt_iso, f"{total_eur:.2f}", json.dumps(positions, ensure_ascii=False)])


if __name__ == "__main__":
    balances = get_balance()

    if isinstance(balances, dict) and balances.get("error"):
        print("ERROR:", balances)
        raise SystemExit(1)

    if not isinstance(balances, list):
        print("Onverwachte response:", balances)
        raise SystemExit(1)

    dt_iso = datetime.now().isoformat(timespec="seconds")
    positions = {}
    total_eur = Decimal("0")

    for item in balances:
        symbol = item.get("symbol")
        available = d(item.get("available", 0))
        in_order = d(item.get("inOrder", 0))
        amount = available + in_order

        if amount <= 0:
            continue

        if symbol == "EUR":
            eur_value = amount
            positions[symbol] = {"amount": str(amount), "eur": str(eur_value)}
            total_eur += eur_value
            continue

        price = get_price_eur(symbol)
        if price is None:
            positions[symbol] = {"amount": str(amount), "eur": None, "note": "no EUR market"}
            continue

        eur_value = amount * price
        positions[symbol] = {"amount": str(amount), "eur": str(eur_value)}
        total_eur += eur_value

    append_csv_line(dt_iso, total_eur, positions)

    print("✅ Gelogd naar:", LOG_PATH)
    print("Tijd:", dt_iso)
    print(f"Totaal (EUR): €{total_eur:.2f}")
