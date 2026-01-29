import os
import csv
from datetime import datetime
from collections import defaultdict

LOG_PATH = os.path.join("logs", "paper_trades.csv")


def parse_dt(s: str):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def main():
    if not os.path.isfile(LOG_PATH):
        print(f"❌ Geen bestand gevonden: {LOG_PATH}")
        print("Tip: run eerst een paar paper trades.")
        return

    sells = []

    with open(LOG_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            side = (row.get("side") or "").upper()
            if side != "SELL":
                continue

            dt = parse_dt((row.get("datetime") or "").strip())
            pnl = safe_float(row.get("pnl"), 0.0)
            symbol = (row.get("symbol") or "?").upper()

            sells.append((dt, symbol, pnl))

    if not sells:
        print("⚠️ Nog geen SELL-trades gevonden.")
        return

    total = len(sells)
    wins = sum(1 for _, _, p in sells if p > 0)
    losses = sum(1 for _, _, p in sells if p < 0)
    breakeven = total - wins - losses
    total_pnl = sum(p for _, _, p in sells)
    winrate = (wins / total) * 100 if total else 0.0

    print("\n=== Crypto_AI – Success Metrics ===")
    print(f"Gesloten trades : {total}")
    print(f"Wins            : {wins}")
    print(f"Losses          : {losses}")
    print(f"Break-even      : {breakeven}")
    print(f"Winrate         : {winrate:.1f}%")
    print(f"Totaal PnL      : €{total_pnl:.2f}")

    # Per coin
    by_coin = defaultdict(lambda: {"closed": 0, "wins": 0, "pnl": 0.0})
    for _, sym, pnl in sells:
        by_coin[sym]["closed"] += 1
        by_coin[sym]["wins"] += 1 if pnl > 0 else 0
        by_coin[sym]["pnl"] += pnl

    print("\n--- Per coin ---")
    for sym, s in sorted(by_coin.items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr = (s["wins"] / s["closed"]) * 100 if s["closed"] else 0
        print(f"{sym:8} | closed={s['closed']:3} | winrate={wr:5.1f}% | pnl=€{s['pnl']:.2f}")

    # Per week
    by_week = defaultdict(lambda: {"closed": 0, "wins": 0, "pnl": 0.0})
    for dt, _, pnl in sells:
        if not dt:
            continue
        year, week, _ = dt.isocalendar()
        key = f"{year}-W{week:02d}"
        by_week[key]["closed"] += 1
        by_week[key]["wins"] += 1 if pnl > 0 else 0
        by_week[key]["pnl"] += pnl

    print("\n--- Per week ---")
    for wk, s in sorted(by_week.items()):
        wr = (s["wins"] / s["closed"]) * 100 if s["closed"] else 0
        print(f"{wk} | closed={s['closed']:3} | winrate={wr:5.1f}% | pnl=€{s['pnl']:.2f}")

    print("\n✅ Klaar.")


if __name__ == "__main__":
    main()

