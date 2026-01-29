import requests
from decimal import Decimal, InvalidOperation
from bitvavo_balance import get_balance

BASE_URL = "https://api.bitvavo.com/v2"


def d(x) -> Decimal:
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError):
        return Decimal("0")


def get_price_eur(symbol: str):
    """
    Haalt prijs op voor bijv. SHIB-EUR.
    Bestaat de markt niet? -> return None
    """
    market = f"{symbol}-EUR"
    r = requests.get(f"{BASE_URL}/ticker/price", params={"market": market}, timeout=10)

    # Als Bitvavo even hapert of rate-limit geeft
    r.raise_for_status()

    data = r.json()

    if isinstance(data, dict):
        price_str = data.get("price") or data.get("last")
        if price_str:
            return d(price_str)

    return None


if __name__ == "__main__":
    balances = get_balance()

    # Als get_balance een fout teruggeeft
    if isinstance(balances, dict) and balances.get("error"):
        print("ERROR:", balances)
        raise SystemExit(1)

    if not isinstance(balances, list):
        print("Onverwachte response:", balances)
        raise SystemExit(1)

    total_eur = Decimal("0")
    print("=== BALANCES IN EUR ===")

    for item in balances:
        symbol = item.get("symbol")
        available = d(item.get("available", 0))
        in_order = d(item.get("inOrder", 0))
        total_coin = available + in_order

        if total_coin <= 0:
            continue

        if symbol == "EUR":
            print(f"EUR: €{total_coin:.2f}")
            total_eur += total_coin
            continue

        price = get_price_eur(symbol)

        if price is None:
            # Zichtbaar maken (handig bij debuggen)
            print(f"{symbol}: {total_coin} (geen {symbol}-EUR markt)")
            continue

        value = total_coin * price
        total_eur += value
        print(f"{symbol}: {total_coin} ≈ €{value:.2f}")

    print("-----------------------")
    print(f"TOTAAL: €{total_eur:.2f}")
