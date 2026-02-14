from __future__ import annotations

import os
import sys
import time
import json
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import requests

# ==========================================================
# PROJECT ROOT (zodat imports altijd werken)
# ==========================================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ==========================================================
# ENV
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()
WEBHOOK_BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or "").strip().rstrip("/")

# scoring / cadence
COINS = (os.getenv("COINS") or "BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT,ADAUSDT,SOLUSDT").split(",")
COINS = [c.strip().upper() for c in COINS if c.strip()]

INTERVAL = (os.getenv("ANALYSIS_INTERVAL") or "1h").strip()
LIMIT = int(os.getenv("ANALYSIS_LIMIT") or "240")

PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))
MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "80")
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "70")

DEFAULT_STOP_PCT = float(os.getenv("DEFAULT_STOP_PCT") or "0.02")  # 2%
DEFAULT_R_MULTIPLE = float(os.getenv("DEFAULT_R_MULTIPLE") or "2.0")  # 2R

# Force test signal
FORCE_TEST_PREBUY = (os.getenv("FORCE_TEST_PREBUY") or "0").strip() == "1"

# ==========================================================
# BINANCE
# ==========================================================
KLINES_URL = "https://api.binance.com/api/v3/klines"

def get_klines(symbol: str, interval: str, limit: int) -> List[List[Any]]:
    r = requests.get(KLINES_URL, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=20)
    r.raise_for_status()
    return r.json()

def last_close(klines: List[List[Any]]) -> float:
    # kline: [openTime, open, high, low, close, volume, closeTime, ...]
    return float(klines[-1][4])

# ==========================================================
# DB
# ==========================================================
def db_available() -> bool:
    return bool(DATABASE_URL)

def db_conn():
    return psycopg2.connect(DATABASE_URL)

def ensure_tables() -> None:
    if not db_available():
        return
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_approvals (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            setup_type TEXT,
            market_regime TEXT,
            score INT,
            grade TEXT,
            entry NUMERIC,
            stop_loss NUMERIC,
            target NUMERIC,
            created_at BIGINT NOT NULL,
            expires_at BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            payload JSONB
        );
        """
    )
    conn.commit()
    cur.close()
    conn.close()

def insert_pending(pb: Dict[str, Any]) -> None:
    ensure_tables()
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO pending_approvals (
            id, symbol, setup_type, market_regime, score, grade,
            entry, stop_loss, target, created_at, expires_at, status, payload
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s::jsonb)
        ON CONFLICT (id) DO NOTHING;
        """,
        (
            pb["id"],
            pb["symbol"],
            pb.get("setup_type"),
            pb.get("market_regime"),
            pb.get("score"),
            pb.get("grade"),
            pb.get("entry"),
            pb.get("stop_loss"),
            pb.get("target"),
            pb.get("created_at"),
            pb.get("expires_at"),
            json.dumps(pb),
        ),
    )
    conn.commit()
    cur.close()
    conn.close()

# ==========================================================
# LOGIC (placeholder scoring / regime)
# ==========================================================
def score_symbol(symbol: str, entry: float) -> Tuple[int, str, str, str]:
    """
    Return: (score, grade, setup_type, market_regime)
    Dit is een simpele placeholder. Jij gaat dit later vervangen door jouw echte scoring + setups.
    """
    if FORCE_TEST_PREBUY:
        return 90, "GO", "TREND_PULLBACK", "RANGE"

    # simpele demo: altijd WATCH tenzij je zelf later echte score toevoegt
    score = 75
    grade = "WATCH" if score >= WATCH_MIN_SCORE else "SKIP"
    setup_type = "TREND_PULLBACK"
    market_regime = "RANGE"

    # grade regels
    if score >= MIN_SCORE_TO_PREBUY:
        grade = "GO"
    elif score >= WATCH_MIN_SCORE:
        grade = "WATCH"
    else:
        grade = "SKIP"

    return score, grade, setup_type, market_regime

def compute_stop_target(entry: float) -> Tuple[float, float]:
    stop = entry * (1.0 - DEFAULT_STOP_PCT)
    r = max(1e-9, entry - stop)
    target = entry + (DEFAULT_R_MULTIPLE * r)
    return float(stop), float(target)

def build_prebuy(symbol: str, entry: float) -> Dict[str, Any]:
    now = int(time.time())
    score, grade, setup_type, market_regime = score_symbol(symbol, entry)
    stop, target = compute_stop_target(entry)

    pb_id = f"PB-{symbol}-{now}"
    return {
        "id": pb_id,
        "symbol": symbol,
        "setup_type": setup_type,
        "market_regime": market_regime,
        "score": score,
        "grade": grade,
        "entry": float(entry),
        "stop_loss": float(stop),
        "target": float(target),
        "created_at": now,
        "expires_at": now + PREBUY_VALID_SECONDS,
    }

def main() -> None:
    print(f"multi_coin_score: coins={len(COINS)} interval={INTERVAL} limit={LIMIT} db={'YES' if db_available() else 'NO'}")
    if not db_available():
        print("ERROR: DATABASE_URL ontbreekt. Stop.")
        return

    ensure_tables()

    created = 0
    for sym in COINS:
        try:
            kl = get_klines(sym, INTERVAL, LIMIT)
            entry = last_close(kl)
            pb = build_prebuy(sym, entry)

            if pb["grade"] == "SKIP":
                continue

            insert_pending(pb)
            created += 1
            print(f"PREBUY saved: {pb['id']} {pb['symbol']} grade={pb['grade']} score={pb['score']} entry={pb['entry']} stop={pb['stop_loss']} target={pb['target']}")
        except Exception as e:
            print(f"ERROR {sym}: {type(e).__name__}: {e}")

    print(f"done. created={created}")

if __name__ == "__main__":
    main()
