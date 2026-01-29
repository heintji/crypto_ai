import csv
import os
from datetime import datetime, timezone

import config


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _ensure_log_dir() -> None:
    os.makedirs(config.LOG_DIR, exist_ok=True)


def _file_path(filename: str) -> str:
    return os.path.join(config.LOG_DIR, filename)


def log_event(event_type: str, symbol: str, message: str, extra: dict | None = None) -> None:
    """
    Log algemene events:
    - PREBUY_CANDIDATE
    - BUY_SIGNAL
    - BUY_APPROVED (human)
    - BUY_REJECTED (human)
    - SELL_EXECUTED
    - STOP_TRIGGERED (daily/weekly)
    """
    _ensure_log_dir()
    path = _file_path(config.EVENTS_LOG_FILE)

    row = {
        "ts_utc": _utc_now_str(),
        "event_type": event_type,
        "symbol": symbol,
        "message": message,
        "extra": "" if not extra else str(extra),
    }

    file_exists = os.path.isfile(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def log_trade(
    trade_id: str,
    symbol: str,
    side: str,
    action: str,
    price: float,
    qty: float,
    reason: str,
    confidence: float | None = None,
    pnl_pct: float | None = None,
    extra: dict | None = None,
) -> None:
    """
    Log trade acties:
    - action kan zijn: ENTRY, WS1, WS2, TARGET, STOPLOSS, FULL_EXIT
    - side is meestal LONG (voor nu)
    """
    _ensure_log_dir()
    path = _file_path(config.TRADES_LOG_FILE)

    row = {
        "ts_utc": _utc_now_str(),
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "action": action,
        "price": float(price),
        "qty": float(qty),
        "reason": reason,
        "confidence": "" if confidence is None else float(confidence),
        "pnl_pct": "" if pnl_pct is None else float(pnl_pct),
        "extra": "" if not extra else str(extra),
    }

    file_exists = os.path.isfile(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def read_last_events(limit: int = 20) -> list[dict]:
    """Handig voor debug: lees de laatste events."""
    path = _file_path(config.EVENTS_LOG_FILE)
    if not os.path.isfile(path):
        return []

    with open(path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[-limit:]


def read_last_trades(limit: int = 20) -> list[dict]:
    """Handig voor debug: lees de laatste trades."""
    path = _file_path(config.TRADES_LOG_FILE)
    if not os.path.isfile(path):
        return []

    with open(path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[-limit:]
