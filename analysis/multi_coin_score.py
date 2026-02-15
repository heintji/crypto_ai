# analysis/multi_coin_score.py
from __future__ import annotations

import os
import sys
import time
import json
import math
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

# =========================
# Project root fix
# =========================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# =========================
# ENV
# =========================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
USE_DB_APPROVALS = (os.getenv("USE_DB_APPROVALS") or "1").strip().lower() in {"1", "true", "yes", "on"}
USE_DB_FINGERPRINTS = (os.getenv("USE_DB_FINGERPRINTS") or "1").strip().lower() in {"1", "true", "yes", "on"}

PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))
MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "80")
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "70")

MAX_PREBUY_PER_RUN = int(os.getenv("MAX_PREBUY_PER_RUN") or "5")
TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS") or str(6 * 60 * 60))

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT") or "15")

COINS = [c.strip().upper() for c in (os.getenv("COINS") or "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,ADAUSDT,DOGEUSDT").split(",") if c.strip()]

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"

# =========================
# Optional WhatsApp notify (Twilio)
# =========================
def twilio_ready() -> bool:
    return all([
        os.getenv("TWILIO_ACCOUNT_SID"),
        os.getenv("TWILIO_AUTH_TOKEN"),
        os.getenv("TWILIO_WHATSAPP_FROM") or os.getenv("WHATSAPP_FROM"),
        os.getenv("TWILIO_WHATSAPP_TO") or os.getenv("WHATSAPP_TO"),
    ])

def send_whatsapp(message: str) -> bool:
    if not twilio_ready():
        return False
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    wa_from = os.getenv("TWILIO_WHATSAPP_FROM") or os.getenv("WHATSAPP_FROM")
    wa_to = os.getenv("TWILIO_WHATSAPP_TO") or os.getenv("WHATSAPP_TO")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = {"From": wa_from, "To": wa_to, "Body": message}
    try:
        r = requests.post(url, data=data, auth=(sid, token), timeout=HTTP_TIMEOUT)
        return r.status_code < 400
    except Exception:
        return False

# =========================
# DB
# =========================
_conn = None

def db_ready() -> bool:
    return bool(DATABASE_URL) and USE_DB_APPROVALS

def db_conn():
    global _conn
    if _conn is not None:
        return _conn
    import psycopg2
    _conn = psycopg2.connect(DATABASE_URL)
    _conn.autocommit = False
    return _conn

def db_init() -> None:
    if not db_ready():
        raise RuntimeError("DATABASE_URL ontbreekt of USE_DB_APPROVALS=0")

    conn = db_conn()
    cur = conn.cursor()

    # pending approvals
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_approvals (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            coin TEXT,
            setup_type TEXT,
            label TEXT,
            score INTEGER,
            grade TEXT,
            entry DOUBLE PRECISION,
            stop_loss DOUBLE PRECISION,
            target DOUBLE PRECISION,
            bot_confidence INTEGER,
            market_regime TEXT,
            regime TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            payload_json JSONB
        );
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_status_exp ON pending_approvals(status, expires_at);")

    # fingerprints for dedupe
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS prebuy_fingerprints (
            fp TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    conn.commit()
    cur.close()

def db_fp_allowed(fp: str) -> bool:
    """
    True = toegestaan (nieuw of cooldown voorbij)
    """
    if not USE_DB_FINGERPRINTS:
        return True

    conn = db_conn()
    cur = conn.cursor()

    # Bestaan?
    cur.execute("SELECT last_seen_at FROM prebuy_fingerprints WHERE fp=%s", (fp,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO prebuy_fingerprints(fp) VALUES (%s)", (fp,))
        conn.commit()
        cur.close()
        return True

    last_seen: datetime = row[0]
    now = datetime.now(timezone.utc)

    # cooldown check
    if (now - last_seen).total_seconds() >= TRADE_COOLDOWN_SECONDS:
        cur.execute("UPDATE prebuy_fingerprints SET last_seen_at=NOW() WHERE fp=%s", (fp,))
        conn.commit()
        cur.close()
        return True

    cur.close()
    conn.commit()
    return False

def db_insert_prebuy(row: Dict[str, Any]) -> bool:
    conn = db_conn()
    cur = conn.cursor()

    # expires_at = NOW() + interval
    expires_seconds = int(row["valid_seconds"])
    cur.execute(
        """
        INSERT INTO pending_approvals (
            id, symbol, coin, setup_type, label, score, grade, entry, stop_loss, target,
            bot_confidence, market_regime, regime, status, expires_at, payload_json
        )
        VALUES (
            %(id)s, %(symbol)s, %(coin)s, %(setup_type)s, %(label)s, %(score)s, %(grade)s, %(entry)s, %(stop_loss)s, %(target)s,
            %(bot_confidence)s, %(market_regime)s, %(regime)s, 'PENDING',
            NOW() + (%(expires_seconds)s || ' seconds')::interval,
            %(payload_json)s::jsonb
        )
        ON CONFLICT (id) DO NOTHING;
        """,
        {
            "id": row["id"],
            "symbol": row["symbol"],
            "coin": row.get("coin") or row["symbol"],
            "setup_type": row.get("setup_type") or "TREND_PULLBACK",
            "label": row["label"],
            "score": int(row["score"]),
            "grade": row.get("grade") or "",
            "entry": float(row["entry"]),
            "stop_loss": float(row["stop_loss"]),
            "target": float(row["target"]),
            "bot_confidence": int(row.get("bot_confidence") or 0),
            "market_regime": row.get("market_regime") or "",
            "regime": row.get("regime") or "",
            "expires_seconds": expires_seconds,
            "payload_json": json.dumps(row.get("payload_json") or {}, ensure_ascii=False),
        }
    )

    created = cur.rowcount == 1
    conn.commit()
    cur.close()
    return created

# =========================
# Indicators
# =========================
def get_price(symbol: str) -> float:
    r = requests.get(BINANCE_TICKER_URL, params={"symbol": symbol}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return float(r.json()["price"])

def get_klines(symbol: str, interval: str, limit: int) -> List[List[Any]]:
    r = requests.get(
        BINANCE_KLINES_URL,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    return r.json()

def closes_from_klines(klines: List[List[Any]]) -> List[float]:
    return [float(k[4]) for k in klines]  # close

def sma(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n

def rsi(values: List[float], n: int = 14) -> Optional[float]:
    if len(values) < n + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-n, 0):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))

def slope(values: List[float], n: int = 10) -> float:
    if len(values) < n:
        return 0.0
    # simpele slope: laatste - eerste over n
    return (values[-1] - values[-n]) / max(1e-9, float(n))

# =========================
# Scoring (simpel maar stabiel)
# =========================
def score_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    # 4h voor regime/trend, 1h voor entry
    k4 = get_klines(symbol, "4h", 120)
    k1 = get_klines(symbol, "1h", 120)

    c4 = closes_from_klines(k4)
    c1 = closes_from_klines(k1)

    sma20_4 = sma(c4, 20)
    sma50_4 = sma(c4, 50)
    rsi14_1 = rsi(c1, 14)
    sma20_1 = sma(c1, 20)

    if sma20_4 is None or sma50_4 is None or rsi14_1 is None or sma20_1 is None:
        return None

    price = c1[-1]
    s_sma = slope(c4, 10)

    # Regime
    if sma20_4 > sma50_4 and s_sma > 0:
        regime = "BULL"
    elif sma20_4 < sma50_4 and s_sma < 0:
        regime = "BEAR"
    else:
        regime = "RANGE"

    # Score
    score = 0
    # Trend bias
    score += 35 if sma20_4 > sma50_4 else 15
    # RSI sweet spot
    if 45 <= rsi14_1 <= 65:
        score += 30
    elif 35 <= rsi14_1 < 45 or 65 < rsi14_1 <= 75:
        score += 20
    else:
        score += 10
    # Price above SMA20 1h
    score += 25 if price >= sma20_1 else 10

    score = int(min(100, max(0, score)))

    label = "GO" if score >= MIN_SCORE_TO_PREBUY else ("WATCH" if score >= WATCH_MIN_SCORE else "SKIP")

    # Basic stop/target (2% stop, 2R target)
    entry = float(price)
    stop_loss = entry * 0.98
    risk = entry - stop_loss
    target = entry + 2.0 * risk

    # confidence (0-100)
    bot_conf = score

    return {
        "symbol": symbol,
        "score": score,
        "label": label,
        "entry": entry,
        "stop_loss": stop_loss,
        "target": target,
        "regime": regime,
        "market_regime": regime,
        "bot_confidence": bot_conf,
        "setup_type": "TREND_PULLBACK",
        "grade": "A" if score >= 90 else ("B" if score >= 80 else "C"),
    }

def make_id(symbol: str) -> str:
    # jouw style: PB-COIN-<timestamp>
    return f"PB-{symbol}-{int(time.time())}"

def make_fp(symbol: str, entry: float, target: float, setup_type: str) -> str:
    # jouw eis: geen dubbele trades (zelfde coin + entry + target + setup_type)
    return f"{symbol}|{setup_type}|{entry:.6f}|{target:.6f}"

# =========================
# Main
# =========================
def main() -> None:
    if not db_ready():
        raise RuntimeError("❌ DATABASE_URL ontbreekt of USE_DB_APPROVALS=0 (maar jij wil Postgres)")

    db_init()

    created = 0
    candidates: List[Dict[str, Any]] = []

    for sym in COINS:
        try:
            r = score_symbol(sym)
            if not r:
                continue
            if r["label"] == "SKIP":
                continue
            candidates.append(r)
        except Exception as e:
            print(f"ERROR {sym}: {e}", flush=True)

    # hoogste score eerst
    candidates.sort(key=lambda x: int(x["score"]), reverse=True)

    for r in candidates[:MAX_PREBUY_PER_RUN]:
        prebuy_id = make_id(r["symbol"])
        fp = make_fp(r["symbol"], float(r["entry"]), float(r["target"]), str(r["setup_type"]))

        if not db_fp_allowed(fp):
            # duplicate binnen cooldown → skip
            continue

        payload = {
            "id": prebuy_id,
            "symbol": r["symbol"],
            "coin": r["symbol"],
            "setup_type": r["setup_type"],
            "label": r["label"],
            "score": int(r["score"]),
            "grade": r["grade"],
            "entry": float(r["entry"]),
            "stop_loss": float(r["stop_loss"]),
            "target": float(r["target"]),
            "bot_confidence": int(r["bot_confidence"]),
            "market_regime": r["market_regime"],
            "regime": r["regime"],
            "valid_seconds": PREBUY_VALID_SECONDS,
            "payload_json": {
                "note": "Pre-BUY opgeslagen in Postgres (pending_approvals).",
                "fp": fp,
            },
        }

        if db_insert_prebuy(payload):
            created += 1

            # WhatsApp notify (optioneel)
            if twilio_ready():
                exp_mins = int(PREBUY_VALID_SECONDS // 60)
                msg = (
                    f"{payload['id']} | {payload['symbol']} | {payload['label']} | score={payload['score']} | "
                    f"entry={payload['entry']:.6f} | stop={payload['stop_loss']:.6f} | target={payload['target']:.6f} | exp {exp_mins}m\n\n"
                    "Bevestig: YES <bedrag> <ID>"
                )
                send_whatsapp(msg)

    print(f"done. created={created}", flush=True)

if __name__ == "__main__":
    main()
