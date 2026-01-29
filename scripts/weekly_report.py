# scripts/weekly_report.py
import os
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections import defaultdict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")
TRADES_CSV = os.path.join(LOGS_DIR, "paper_trades.csv")
REPORTS_DIR = os.path.join(LOGS_DIR, "reports")

# Handig voor NL-Excel: ; i.p.v. ,
EXCEL_DELIM = ";"


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    # accepteer: 2026-01-28T00:59:05  of  2026-01-28T00:59:05+00:00
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _to_float(x, default=0.0) -> float:
    try:
        if x is None:
            return float(default)
        s = str(x).strip().replace(",", ".")
        return float(s) if s else float(default)
    except Exception:
        return float(default)


@dataclass
class TradeRow:
    dt: datetime
    symbol: str
    side: str
    price: float
    qty: float
    balance: float
    pnl: float
    meta: str


def _normalize_row(d: dict) -> TradeRow | None:
    # Support meerdere oudere headers:
    # - datetime,symbol,side,price,qty,balance,pnl,meta
    # - datetime,symbol,side,price,amount,balance
    dt = _parse_dt(d.get("datetime") or d.get("date") or d.get("time") or "")
    if not dt:
        return None

    symbol = (d.get("symbol") or "").strip().upper()
    side = (d.get("side") or "").strip().upper()

    price = _to_float(d.get("price"))
    balance = _to_float(d.get("balance"))
    pnl = _to_float(d.get("pnl"))

    # qty kan heten qty of amount (oude variant)
    qty = _to_float(d.get("qty") if d.get("qty") is not None else d.get("amount"))

    meta = (d.get("meta") or "").strip()

    return TradeRow(dt=dt, symbol=symbol, side=side, price=price, qty=qty, balance=balance, pnl=pnl, meta=meta)


def read_trades() -> list[TradeRow]:
    if not os.path.isfile(TRADES_CSV):
        return []

    rows: list[TradeRow] = []
    with open(TRADES_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for d in reader:
            r = _normalize_row(d)
            if r:
                rows.append(r)
    rows.sort(key=lambda x: x.dt)
    return rows


def start_of_week_utc(dt: datetime) -> datetime:
    # ISO week start = maandag
    dt = dt.astimezone(timezone.utc)
    monday = dt - timedelta(days=dt.isoweekday() - 1)
    return datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)


def filter_week(rows: list[TradeRow], year: int, week: int) -> list[TradeRow]:
    # ISO week filter
    out = []
    for r in rows:
        y, w, _ = r.dt.isocalendar()
        if y == year and w == week:
            out.append(r)
    return out


def summarize(rows: list[TradeRow]) -> dict:
    # We beoordelen PnL op SELL-rows (BUY heeft pnl 0)
    sells = [r for r in rows if r.side == "SELL"]
    buys = [r for r in rows if r.side == "BUY"]

    total_pnl = sum(r.pnl for r in sells)
    win_sells = [r for r in sells if r.pnl > 0]
    loss_sells = [r for r in sells if r.pnl < 0]
    winrate = (len(win_sells) / len(sells) * 100.0) if sells else 0.0

    start_balance = rows[0].balance if rows else 0.0
    end_balance = rows[-1].balance if rows else 0.0

    best = max(sells, key=lambda r: r.pnl, default=None)
    worst = min(sells, key=lambda r: r.pnl, default=None)

    # per coin
    coin = defaultdict(lambda: {"sells": 0, "pnl": 0.0, "wins": 0, "losses": 0})
    for r in sells:
        coin[r.symbol]["sells"] += 1
        coin[r.symbol]["pnl"] += r.pnl
        if r.pnl > 0:
            coin[r.symbol]["wins"] += 1
        elif r.pnl < 0:
            coin[r.symbol]["losses"] += 1

    return {
        "count_rows": len(rows),
        "count_buys": len(buys),
        "count_sells": len(sells),
        "total_pnl": total_pnl,
        "winrate": winrate,
        "start_balance": start_balance,
        "end_balance": end_balance,
        "best": best,
        "worst": worst,
        "per_coin": coin,
    }


def write_reports(year: int, week: int, rows: list[TradeRow], stats: dict):
    os.makedirs(REPORTS_DIR, exist_ok=True)

    # bestandsnaam
    md_path = os.path.join(REPORTS_DIR, f"weekly_report_{year}-W{week:02d}.md")
    csv_path = os.path.join(REPORTS_DIR, f"weekly_report_{year}-W{week:02d}.csv")

    # Markdown rapport
    best = stats["best"]
    worst = stats["worst"]

    def fmt_trade(t: TradeRow | None) -> str:
        if not t:
            return "-"
        return f"{t.dt.isoformat(timespec='seconds')} | {t.symbol} | {t.side} | pnl={t.pnl:.2f} | price={t.price:.2f} | qty={t.qty:.8f} | bal={t.balance:.2f}"

    lines = []
    lines.append(f"# Weekly report — {year}-W{week:02d}")
    lines.append("")
    lines.append("## Overview")
    lines.append(f"- Rows this week: {stats['count_rows']}")
    lines.append(f"- BUY count: {stats['count_buys']}")
    lines.append(f"- SELL count: {stats['count_sells']}")
    lines.append(f"- Total PnL (SELL): {stats['total_pnl']:.2f}")
    lines.append(f"- Winrate (SELL): {stats['winrate']:.1f}%")
    lines.append(f"- Start balance: {stats['start_balance']:.2f}")
    lines.append(f"- End balance: {stats['end_balance']:.2f}")
    lines.append("")
    lines.append("## Best / Worst SELL")
    lines.append(f"- Best:  {fmt_trade(best)}")
    lines.append(f"- Worst: {fmt_trade(worst)}")
    lines.append("")
    lines.append("## Per coin (SELL only)")
    lines.append("| Symbol | Sells | Wins | Losses | PnL | Winrate |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for sym, d in sorted(stats["per_coin"].items(), key=lambda kv: kv[1]["pnl"], reverse=True):
        sells = d["sells"]
        wr = (d["wins"] / sells * 100.0) if sells else 0.0
        lines.append(f"| {sym} | {sells} | {d['wins']} | {d['losses']} | {d['pnl']:.2f} | {wr:.1f}% |")
    lines.append("")
    lines.append("## Raw sells")
    lines.append("| datetime | symbol | pnl | price | qty | balance | meta |")
    lines.append("|---|---|---:|---:|---:|---:|---|")
    for r in [x for x in rows if x.side == "SELL"]:
        lines.append(f"| {r.dt.isoformat(timespec='seconds')} | {r.symbol} | {r.pnl:.2f} | {r.price:.2f} | {r.qty:.8f} | {r.balance:.2f} | {r.meta} |")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # CSV rapport (Excel NL-proof met ;)
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=EXCEL_DELIM)
        w.writerow(["datetime", "symbol", "side", "price", "qty", "balance", "pnl", "meta"])
        for r in rows:
            w.writerow([
                r.dt.isoformat(timespec="seconds"),
                r.symbol,
                r.side,
                f"{r.price:.8f}",
                f"{r.qty:.12f}",
                f"{r.balance:.2f}",
                f"{r.pnl:.2f}",
                r.meta
            ])

    return md_path, csv_path


def main():
    rows = read_trades()
    if not rows:
        print("⛔ Geen trades gevonden in logs/paper_trades.csv")
        return

    now = datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()

    week_rows = filter_week(rows, year, week)
    if not week_rows:
        print(f"ℹ️ Geen trades in huidige week {year}-W{week:02d}.")
        return

    stats = summarize(week_rows)
    md_path, csv_path = write_reports(year, week, week_rows, stats)

    print("✅ Weekly report gemaakt:")
    print(f"- {md_path}")
    print(f"- {csv_path}")
    print(f"PnL: {stats['total_pnl']:.2f} | Winrate: {stats['winrate']:.1f}% | End balance: {stats['end_balance']:.2f}")


if __name__ == "__main__":
    main()
