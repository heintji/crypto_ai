import time
import hmac
import hashlib
import requests
import os
from decimal import Decimal, InvalidOperation
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("BITVAVO_API_KEY")
API_SECRET = os.getenv("BITVAVO_API_SECRET")

BASE_URL = "https://api.bitvavo.com/v2"


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

    response = requests.get(
        BASE_URL + path,
        headers=headers,
        timeout=15
    )

    return response.json()


if __name__ == "__main__":
    balances = get_balance()

    if isinstance(balances, dict) and balances.get("error"):
        print("ERROR:", balances)
        raise SystemExit(1)

    if not isinstance(balances, list):
        print("Onverwachte response:", balances)
        raise SystemExit(1)

    print("=== BALANCES (alleen > 0) ===")

    for item in balances:
        symbol = item.get("symbol")
        available = d(item.get("available", 0))
        in_order = d(item.get("inOrder", 0))
        total = available + in_order

        if total > 0:
            print(f"{symbol}: available={available} | inOrder={in_order} | total={total}")
