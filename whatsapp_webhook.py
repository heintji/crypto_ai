from __future__ import annotations

import os
import sys
import json
import time
import math
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests


# ============================================================
# PROJECT ROOT
# analysis/multi_coin_score.py -> project-root is 1 map omhoog
# ============================================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ============================================================
# PATHS (1 waarheid voor hele bot)
# ============================================================
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")

PENDING_FILE = os.path.join(DATA_DIR, "pending_approvals.json")

# Logs (audit-proof)
PREBUY_PAYLOAD_FILE = os.path.join(LOGS_DIR, "prebuy_payload.json")
PREBUY_STATE_FILE = os.path.join(LOGS_DIR, "prebuy_state.json")
EVENTS_LOG_FILE = os.path.join(LOGS_DIR, "events_log.csv")


# ============================================================
# SETTINGS (veilig + voorspelbaar)
# ============================================================
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

# Coins
COINS = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "SOLUSDT",
]

# Timeframes
TF_MAIN = "4h"     # hoofdscore / trend
TF_CONTEXT = "1h"  # context / bevestiging
LIMIT_MAIN = 200
LIMIT_CONTEXT = 200

# Pre-BUY geldigheid (zoals jij wilde)
PREBUY_TTL_SECONDS = 4 * 60 * 60

# Anti-spam / controle
MAX_PREBUY_PER_DAY = 5

# Score thresholds (pas dit later aan als je wil)
THRESHOLD_STRONG = 90
THRESHOLD_GOOD = 80
THRESHOLD_OK = 70

# Test mode (Render env)
# FORCE_TEST_PREBUY=1 -> maakt 1 test-prebuy (max 1 tegelijk)
FORCE_TEST_PREBUY = os.getenv("FORCE_TEST_PREBUY", "0") == "1"


# ============================================================
# UTIL
# ============================================================
def now_s() -> int:
    return int(time.time())

def utc_iso(ts: Optional[int] = None) -> str:
    t = ts if ts is not None else now_s()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

def ensure_dirs_and_files() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)

    if not os.path.isfile(PENDING_FILE):
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)

    # events_log header als file niet bestaat
    if not os.path.isfile(EVENTS_LOG_FILE):
        with open(EVENTS_LOG_FILE, "w", encoding="utf-8") as f:
            f.write("ts_iso,event,details\n")

def append_event(event: str, details: Dict[str, Any]) -> None:
    ensure_dirs_and_files()
    line = f"{utc_iso()},{event},{json.dumps(details, ensure_ascii=False)}\n"
    with open(EVENTS_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)

def safe_json_load(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def safe_json_save(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def is_expired(expires_at: Any) -> bool:
    try:
        x = int(expires_at)
    except Exception:
        return False
    # ms -> s
    if x > 10**12:
        x = int(x / 1000)
    return x < now_s()

def day_key_utc() -> str:
    # UTC dag-sleutel, zodat cron niet afhankelijk is van timezone
    return time.strftime("%Y-%m-%d", time.gmtime())

def stable_hash(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ============================================================
# PENDING FILE (Pre-BUY buffer)
# ============================================================
def load_pending() -> List[Dict[str, Any]]:
    ensure_dirs_and_files()
    data = safe_json_load(PENDING_FILE, [])
    return data if isinstance(data, list) else []

def save_pending(pending: List[Dict[str, Any]]) -> None:
    ensure_dirs_and_files()
    safe_json_save(PENDING_FILE, pending)

def pending_has_active_for_coin(pending: List[Dict[str, Any]], coin: str) -> bool:
    for p in pending:
        if str(p.get("coin", "")).upper() == coin.upper():
            st = str(p.get("status", "")).upper()
            if st == "PENDING" and not is_expired(p.get("expires_at", 0)):
                return True
    return False

def pending_has_active_test(pending: List[Dict[str, Any]]) -> bool:
    for p in pending:
        if str(p.get("setup", "")).upper() == "TEST" and str(p.get("status", "")).upper() == "PENDING":
            if not is_expired(p.get("expires_at", 0)):
                return True
    return False


# ============================================================
# STATE (daglimiet, counters, audit)
# ============================================================
def load_prebuy_state() -> Dict[str, Any]:
    ensure_dirs_and_files()
    st = safe_json_load(PREBUY_STATE_FILE, {})
    if not isinstance(st, dict):
        st = {}
    # defaults
    st.setdefault("day", day_key_utc())
    st.setdefault("count_today", 0)
    st.setdefault("last_run_utc", None)
    return st

def save_prebuy_state(st: Dict[str, Any]) -> None:
    ensure_dirs_and_files()
    safe_json_save(PREBUY_STATE_FILE, st)

def reset_state_if_new_day(st: Dict[str, Any]) -> Dict[str, Any]:
    today = day_key_utc()
    if st.get("day") != today:
        st["day"] = today
        st["count_today"] = 0
    st["last_run_utc"] = utc_iso()
    return st


# ============================================================
# MARKET DATA
# ============================================================
def fetch_klines(symbol: str, interval: str, limit: int) -> Optional[List[List[Any]]]:
    try:
        r = requests.get(
            BINANCE_KLINES_URL,
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or len(data) < 30:
            return None
        return data
    except Exception as e:
        append_event("FETCH_FAIL", {"coin": symbol, "tf": interval, "err": str(e)[:160]})
        return None

def closes_from_klines(klines: List[List[Any]]) -> List[float]:
    # kline: [open_time, open, high, low, close, volume, ...]
    out: List[float] = []
    for row in klines:
        try:
            out.append(float(row[4]))
        except Exception:
            continue
    return out


# ============================================================
# INDICATORS (geen externe libs: minder foutkans)
# ============================================================
def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / float(period)

def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None

    gains = 0.0
    losses = 0.0

    # Wilder style init (simpel)
    for i in range(-period, 0):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += abs(diff)

    avg_gain = gains / period
    avg_loss = losses / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ============================================================
# SCORING & LABELS
# ============================================================
@dataclass
class ScoreResult:
    coin: str
    score: float
    label: str
    entry: float
    tf_main: str
    tf_ctx: str
    trend_main: str
    trend_ctx: str
    rsi_main: float
    rsi_ctx: float
    sma20_main: float
    sma50_main: float
    sma20_ctx: float
    sma50_ctx: float

def trend_from_smas(sma20: float, sma50: float, tol: float = 1e-9) -> str:
    if sma20 > sma50 + tol:
        return "UP"
    if sma20 < sma50 - tol:
        return "DOWN"
    return "FLAT"

def score_coin(symbol: str) -> Optional[ScoreResult]:
    k_main = fetch_klines(symbol, TF_MAIN, LIMIT_MAIN)
    k_ctx = fetch_klines(symbol, TF_CONTEXT, LIMIT_CONTEXT)
    if not k_main or not k_ctx:
        return None

    c_main = closes_from_klines(k_main)
    c_ctx = closes_from_klines(k_ctx)

    entry = float(c_main[-1])

    sma20_m = sma(c_main, 20)
    sma50_m = sma(c_main, 50)
    sma20_c = sma(c_ctx, 20)
    sma50_c = sma(c_ctx, 50)

    rsi_m = rsi(c_main, 14)
    rsi_c = rsi(c_ctx, 14)

    if None in (sma20_m, sma50_m, sma20_c, sma50_c, rsi_m, rsi_c):
        return None

    sma20_m = float(sma20_m); sma50_m = float(sma50_m)
    sma20_c = float(sma20_c); sma50_c = float(sma50_c)
    rsi_m = float(rsi_m); rsi_c = float(rsi_c)

    tr_m = trend_from_smas(sma20_m, sma50_m)
    tr_c = trend_from_smas(sma20_c, sma50_c)

    # ---------- SCORE (0-100) ----------
    score = 50.0

    # Trend alignment
    if tr_m == "UP":
        score += 15
    elif tr_m == "DOWN":
        score -= 15

    if tr_c == "UP":
        score += 10
    elif tr_c == "DOWN":
        score -= 10

    # RSI sanity (geen “overbought” push)
    # mild: 45-60 is vaak gezond in uptrend
    if 45 <= rsi_m <= 60:
        score += 10
    elif rsi_m < 35:
        score += 5   # mogelijk mean reversion / goedkoop
    elif rsi_m > 70:
        score -= 10  # te heet

    if 45 <= rsi_c <= 60:
        score += 5
    elif rsi_c > 70:
        score -= 5

    # Price relative to SMA20 (momentum check)
    if entry > sma20_m:
        score += 5
    else:
        score -= 5

    # Clamp
    score = max(0.0, min(100.0, score))

    # label buckets
    if score >= THRESHOLD_STRONG:
        label = "kans groot"
    elif score >= THRESHOLD_GOOD:
        label = "kans boven gemiddeld"
    elif score >= THRESHOLD_OK:
        label = "kans gemiddeld"
    else:
        label = "laag"

    return ScoreResult(
        coin=symbol,
        score=score,
        label=label,
        entry=entry,
        tf_main=TF_MAIN,
        tf_ctx=TF_CONTEXT,
        trend_main=tr_m,
        trend_ctx=tr_c,
        rsi_main=rsi_m,
        rsi_ctx=rsi_c,
        sma20_main=sma20_m,
        sma50_main=sma50_m,
        sma20_ctx=sma20_c,
        sma50_ctx=sma50_c,
    )


# ============================================================
# PREBUY OBJECT CREATION
# ============================================================
def build_prebuy(sr: ScoreResult, setup: str = "LIVE") -> Dict[str, Any]:
    # ID: uniek + traceerbaar
    prebuy_id = f"PB-{setup}-{sr.coin}-{now_s()}"

    expires_at = now_s() + PREBUY_TTL_SECONDS

    payload = {
        "id": prebuy_id,
        "coin": sr.coin,
        "setup": setup,  # LIVE / TEST
        "status": "PENDING",
        "score": round(sr.score, 2),
        "label": sr.label,
        "entry": round(sr.entry, 8),
        # stop_loss/target worden pas bij BUY berekend in whatsapp_webhook (jouw architectuur)
        "stop_loss": None,
        "target": None,
        "created_at": utc_iso(),
        "expires_at": expires_at,
        "timeframes": {
            "main": sr.tf_main,
            "context": sr.tf_ctx,
        },
        "trend": {
            "main": sr.trend_main,
            "context": sr.trend_ctx,
        },
        "indicators": {
            "rsi14_main": round(sr.rsi_main, 2),
            "rsi14_context": round(sr.rsi_ctx, 2),
            "sma20_main": round(sr.sma20_main, 8),
            "sma50_main": round(sr.sma50_main, 8),
            "sma20_context": round(sr.sma20_ctx, 8),
            "sma50_context": round(sr.sma50_ctx, 8),
        },
    }

    # fingerprint voor idempotency/debug
    payload["fingerprint"] = stable_hash({
        "coin": payload["coin"],
        "setup": payload["setup"],
        "score": payload["score"],
        "entry": payload["entry"],
        "expires_at": payload["expires_at"],
    })

    return payload

def log_prebuy_payload(prebuy: Dict[str, Any]) -> None:
    ensure_dirs_and_files()
    safe_json_save(PREBUY_PAYLOAD_FILE, prebuy)

def can_create_more_today(st: Dict[str, Any]) -> bool:
    return int(st.get("count_today", 0)) < MAX_PREBUY_PER_DAY


# ============================================================
# FORCE TEST PREBUY (veilig)
# ============================================================
def force_test_prebuy() -> Tuple[bool, str]:
    pending = load_pending()

    if pending_has_active_test(pending):
        return False, "TEST prebuy bestaat al (PENDING)."

    # test met vaste entry, zodat flow altijd werkt
    sr = ScoreResult(
        coin="BTCUSDT",
        score=99.0,
        label="test",
        entry=100.0,
        tf_main=TF_MAIN,
        tf_ctx=TF_CONTEXT,
        trend_main="UP",
        trend_ctx="UP",
        rsi_main=50.0,
        rsi_ctx=50.0,
        sma20_main=99.0,
        sma50_main=98.0,
        sma20_ctx=99.0,
        sma50_ctx=98.0,
    )

    prebuy = build_prebuy(sr, setup="TEST")

    pending.insert(0, prebuy)
    save_pending(pending)

    log_prebuy_payload(prebuy)
    append_event("TEST_PREBUY_CREATED", {"id": prebuy["id"], "coin": prebuy["coin"], "expires_at": prebuy["expires_at"]})
    return True, prebuy["id"]


# ============================================================
# MAIN RUN
# ============================================================
def main() -> None:
    ensure_dirs_and_files()

    append_event("SCAN_START", {"mode": "prebuy_only", "force_test": FORCE_TEST_PREBUY})

    st = load_prebuy_state()
    st = reset_state_if_new_day(st)

    pending = load_pending()

    # 0) Force test (als env aan staat) -> maak 1 test-prebuy en stop daarna
    if FORCE_TEST_PREBUY:
        ok, msg = force_test_prebuy()
        append_event("SCAN_END", {"new_prebuys": 1 if ok else 0, "note": msg})
        print(f"🧪 FORCE_TEST_PREBUY=1 -> {msg}")
        print(f"📌 Pending file: {PENDING_FILE}")
        save_prebuy_state(st)
        return

    # 1) Daglimiet check
    if not can_create_more_today(st):
        append_event("SCAN_END", {"new_prebuys": 0, "note": "MAX_PREBUY_PER_DAY bereikt"})
        print("🟡 Daglimiet bereikt. Geen nieuwe Pre-BUY.")
        save_prebuy_state(st)
        return

    new_prebuys = 0

    # 2) Scan coins
    for coin in COINS:
        if not can_create_more_today(st):
            break

        # skip als al pending bestaat voor coin (voorkomt spam)
        if pending_has_active_for_coin(pending, coin):
            continue

        sr = score_coin(coin)
        if sr is None:
            continue

        # Alleen prebuy maken boven threshold (anders niets doen)
        if sr.score < THRESHOLD_OK:
            continue

        prebuy = build_prebuy(sr, setup="LIVE")

        # extra guard: geen duplicate fingerprint binnen actieve pending
        fp = prebuy.get("fingerprint")
        duplicate = False
        for p in pending:
            if p.get("fingerprint") == fp and str(p.get("status", "")).upper() == "PENDING" and not is_expired(p.get("expires_at", 0)):
                duplicate = True
                break
        if duplicate:
            continue

        pending.insert(0, prebuy)
        save_pending(pending)

        log_prebuy_payload(prebuy)

        st["count_today"] = int(st.get("count_today", 0)) + 1
        new_prebuys += 1

        append_event("PREBUY_CREATED", {
            "id": prebuy["id"],
            "coin": prebuy["coin"],
            "score": prebuy["score"],
            "label": prebuy["label"],
            "expires_at": prebuy["expires_at"],
        })

    save_prebuy_state(st)

    append_event("SCAN_END", {"new_prebuys": new_prebuys, "count_today": st.get("count_today", 0)})

    print(f"✅ Pre-BUY run klaar — {new_prebuys} nieuwe Pre-BUY(s).")
    print(f"📌 Pending file: {PENDING_FILE}")
    print(f"🧾 Prebuy payload log: {PREBUY_PAYLOAD_FILE}")
    print(f"🧾 State file: {PREBUY_STATE_FILE}")


if __name__ == "__main__":
    main()

