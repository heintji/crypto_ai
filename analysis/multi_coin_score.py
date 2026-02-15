# analysis/multi_coin_score.py
from __future__ import annotations

import os
import sys
import json
import time
import math
from typing import Any, Dict, List, Optional, Tuple

import requests
import psycopg2
import psycopg2.extras

# =========================
# Project-root fix
# =========================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# =========================
# ENV
# =========================
USE_DB_APPROVALS = (os.getenv("USE_DB_APPROVALS", "1").strip().lower() in {"1", "true", "yes", "on"})
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

DATA_DIR = (os.getenv("DATA_DIR") or "/data").strip()
LOGS_DIR = (os.getenv("LOGS_DIR") or f"{DATA_DIR}/logs").strip()

PENDING_FILE = (os.getenv("PENDING_PATH") or f"{DATA_DIR}/pending_approvals.json").strip()
FINGERPRINT_PATH = (os.getenv("FINGERPRINT_PATH") or f"{DATA_DIR}/fingerprints.json").strip()

FORCE_TEST_PREBUY = (os.getenv("FORCE_TEST_PREBUY", "0").strip() == "1")

PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))
MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "80")
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "70")

TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS") or str(6 * 60 * 60))
MAX_PREBUY_PER_RUN = int(os.getenv("MAX_PREBUY_PER_RUN") or "5")

# Binance
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
HTTP_TIMEOUT = 15

COINS = (os.getenv("COINS") or "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT").split(",")
COINS = [c.strip().upper() for c in COINS if c.strip()]

# =========================
# UTIL
# =========================
def _ts() -> int:
    return int(time.time())

def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)

def _load_json(path: str, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(path: str, data) -> None:
    _ensure_dir(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def _connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt (Render env).")
    return psycopg2.connect(DATABASE_URL)

def _ensure_db_schema() -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_approvals (
                    id TEXT PRIMARY KEY,
                    symbol TEXT,
                    coin TEXT,
                    status TEXT,
                    label TEXT,
                    score INTEGER,
                    grade TEXT,
                    setup_type TEXT,
                    regime TEXT,
                    market_regime TEXT,
                    entry DOUBLE PRECISION,
                    stop DOUBLE PRECISION,
                    stop_loss DOUBLE PRECISION,
                    target DOUBLE PRECISION,
                    bot_confidence INTEGER,
                    payload TEXT,
                    payload_json JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    expires_at TIMESTAMPTZ
                );
                """
            )
            conn.commit()

def _db_insert_pending(row: Dict[str, Any]) -> bool:
    """
    created_at = NOW()
    expires_at = NOW() + interval 'X seconds'
    """
    _ensure_db_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pending_approvals (
                    id, symbol, coin, status, label, score, grade, setup_type,
                    regime, market_regime,
                    entry, stop, stop_loss, target,
                    bot_confidence, payload, payload_json,
                    created_at, expires_at
                )
                VALUES (
                    %(id)s, %(symbol)s, %(coin)s, 'PENDING', %(label)s, %(score)s, %(grade)s, %(setup_type)s,
                    %(regime)s, %(market_regime)s,
                    %(entry)s, %(stop)s, %(stop_loss)s, %(target)s,
                    %(bot_confidence)s, %(payload)s, %(payload_json)s,
                    NOW(),
                    NOW() + (%(valid_seconds)s || ' seconds')::interval
                )
                ON CONFLICT (id) DO NOTHING;
                """,
                {
                    **row,
                    "valid_seconds": int(PREBUY_VALID_SECONDS),
                },
            )
            conn.commit()
            return cur.rowcount == 1

def _file_append_pending(row: Dict[str, Any]) -> bool:
    items = _load_json(PENDING_FILE, [])
    # keep as epoch in file-mode (maar jij gaat DB gebruiken)
    row2 = dict(row)
    row2["status"] = "PENDING"
    row2["created_at"] = _ts()
    row2["expires_at"] = _ts() + int(PREBUY_VALID_SECONDS)
    items.append(row2)
    _save_json(PENDING_FILE, items)
    return True

def _fingerprint(symbol: str, entry: float, target: float, setup_type: str) -> str:
    return f"{symbol}|{setup_type}|{round(entry, 8)}|{round(target, 8)}"

def _dedupe_allowed(fp: str) -> bool:
    data = _load_json(FINGERPRINT_PATH, {})
    now = _ts()
    last = int(data.get(fp, 0) or 0)
    if last and (now - last) < TRADE_COOLDOWN_SECONDS:
        return False
    data[fp] = now
    _save_json(FINGERPRINT_PATH, data)
    return True

# =========================
# MARKET DATA
# =========================
def get_price(symbol: str) -> float:
    r = requests.get(BINANCE_TICKER_URL, params={"symbol": symbol}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return float(r.json()["price"])

def get_klines(symbol: str, interval: str, limit: int) -> List[List[Any]]:
    r = requests.get(
        BINANCE_KLINES_URL,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()

def sma(values: List[float], n: int) -> float:
    if len(values) < n:
        return float("nan")
    return sum(values[-n:]) / n

def rsi(values: List[float], n: int = 14) -> float:
    if len(values) < n + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(-n, 0):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def regime_from_4h(closes_4h: List[float]) -> str:
    s20 = sma(closes_4h, 20)
    s50 = sma(closes_4h, 50)
    if math.isnan(s20) or math.isnan(s50):
        return "RANGE"
    if s20 > s50:
        return "BULL"
    if s20 < s50:
        return "BEAR"
    return "RANGE"

# =========================
# SCORING (simpel maar stabiel)
# =========================
def score_symbol(symbol: str) -> Dict[str, Any]:
    # 1h voor entry, 4h voor regime
    kl_1h = get_klines(symbol, "1h", 120)
    kl_4h = get_klines(symbol, "4h", 120)

    closes_1h = [float(k[4]) for k in kl_1h]
    closes_4h = [float(k[4]) for k in kl_4h]

    price = closes_1h[-1]
    s20 = sma(closes_1h, 20)
    s50 = sma(closes_1h, 50)
    rsi14 = rsi(closes_1h, 14)
    reg = regime_from_4h(closes_4h)

    score = 50

    # trend bias
    if not math.isnan(s20) and not math.isnan(s50):
        if s20 > s50:
            score += 20
        else:
            score -= 10

    # RSI sweet spot
    if 45 <= rsi14 <= 65:
        score += 15
    elif rsi14 < 35:
        score += 5
    elif rsi14 > 75:
        score -= 15

    # price above s20 bonus
    if not math.isnan(s20) and price > s20:
        score += 10

    score = max(0, min(100, int(score)))

    label = "GO" if score >= MIN_SCORE_TO_PREBUY else ("WATCH" if score >= WATCH_MIN_SCORE else "SKIP")
    grade = "A" if score >= 90 else ("B" if score >= 80 else ("C" if score >= 70 else "D"))

    # setup_type placeholder (later: TREND_PULLBACK/BREAKOUT_RETEST)
    setup_type = "TREND_PULLBACK" if reg in {"BULL", "RANGE"} else "BREAKOUT_RETEST"

    # stop/target defaults (2% stop, 2R target)
    entry = float(price)
    stop_loss = entry * 0.98
    risk = max(entry - stop_loss, entry * 0.02)
    target = entry + 2.0 * risk

    return {
        "symbol": symbol,
        "coin": symbol,
        "score": score,
        "label": label,
        "grade": grade,
        "setup_type": setup_type,
        "regime": reg,
        "market_regime": reg,
        "entry": entry,
        "stop": stop_loss,
        "stop_loss": stop_loss,
        "target": target,
        "bot_confidence": score,
    }

# =========================
# MAIN
# =========================
def main():
    created = 0
    results: List[Dict[str, Any]] = []

    if FORCE_TEST_PREBUY:
        # test: maak “fake” score hoog zodat je flow altijd iets maakt
        symbol = COINS[0] if COINS else "BTCUSDT"
        row = score_symbol(symbol)
        row["score"] = 90
        row["label"] = "GO"
        results.append(row)
    else:
        for c in COINS:
            try:
                results.append(score_symbol(c))
            except Exception as e:
                print(f"ERROR {c}: {e}", flush=True)

    # sort op score
    results.sort(key=lambda x: x.get("score", 0), reverse=True)

    for row in results:
        if created >= MAX_PREBUY_PER_RUN:
            break
        if row["label"] not in {"GO", "WATCH"}:
            continue

        # dedupe “zelfde trade niet opnieuw”
        fp = _fingerprint(row["symbol"], row["entry"], row["target"], row["setup_type"])
        if not _dedupe_allowed(fp):
            continue

        prebuy_id = f"PB-{row['symbol']}-{int(time.time())}"
        payload = {
            "id": prebuy_id,
            **row,
        }

        payload["payload_json"] = payload
        payload["payload"] = json.dumps(payload, ensure_ascii=False)

        if USE_DB_APPROVALS:
            try:
                ok = _db_insert_pending(payload)
                if ok:
                    created += 1
            except Exception as e:
                print(f"DB INSERT ERROR {row['symbol']}: {e}", flush=True)
        else:
            _file_append_pending(payload)
            created += 1

        print(
            f"CREATED {prebuy_id} {row['symbol']} {row['label']} score={row['score']} entry={row['entry']}",
            flush=True,
        )

    print(f"done. created={created}", flush=True)


if __name__ == "__main__":
    main()
