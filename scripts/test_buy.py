# scripts/test_buy.py
# Test: approval -> paper_trader buy_eur
# Run altijd vanuit project-root:
# cd C:\Users\heint\crypto_ai
# python .\scripts\test_buy.py

from __future__ import annotations

import os
import sys
import time
import json

# ✅ Zorg dat project-root in sys.path staat (zodat "trading.paper_trader" altijd werkt)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from trading.paper_trader import buy_eur  # noqa: E402


PENDING_FILE = os.path.join(PROJECT_ROOT, "data", "pending_approvals.json")


def load_pending() -> list:
    if not os.path.isfile(PENDING_FILE):
        return []
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            x = json.load(f)
        return x if isinstance(x, list) else []
    except Exception:
        return []


def main() -> None:
    print("🚀 TEST BUY START")

    pending = load_pending()
    if not pending:
        print("⛔ Geen approvals in pending_approvals.json")
        print(f"Open: {PENDING_FILE}")
        return

    # Pak de eerste APPROVED die nog niet CONSUMED is
    approved = None
    for item in pending:
        if item.get("status") == "APPROVED":
            approved = item
            break

    if not approved:
        print("⛔ Geen APPROVED item gevonden (status moet 'APPROVED' zijn).")
        return

    symbol = approved.get("coin", "BTCUSDT")
    amount = float(approved.get("approved_amount", 10.0))
    prebuy_id = approved.get("id", f"PREBUY-TEST-{int(time.time())}")

    # test-waarden (mag, want paper trade)
    stop_loss = 1.0
    target = 999999999.0

    print(f"✅ BUY OK | {symbol} €{amount:.2f} | prebuy_id={prebuy_id}")

    result = buy_eur(
        symbol=symbol,
        price=None,              # live prijs ophalen
        amount_eur=amount,       # jouw YES bedrag
        stop_loss=stop_loss,
        target=target,
        prebuy_id=prebuy_id,
    )

    print("✅ RESULT:", result)


if __name__ == "__main__":
    main()

