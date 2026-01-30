# analysis/multi_coin_score.py
# ✅ PRE-BUY ONLY (geen BUY/SELL)
# - Analyse candles
# - Maakt Pre-BUY candidates
# - Schrijft PENDING approvals naar data/pending_approvals.json
# - Logt AI-advice naar logs/ai_advice.csv
# - Optioneel: force TEST PreBUY om WhatsApp flow te testen

import os
import sys
import json
import time
import uuid
import csv
import re
from datetime import datetime, timezone

import requests

# =========================
# PATH FIX (project root)
# =========================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# =========================
# SETTINGS
# =========================
BASE_URL = "https://api.binance.com/api/v3/klines"

COINS = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "SOLUSDT",
]

INTERVAL_1H = "1h"
INTERVAL_4H = "4h"

LIMIT_1H = 180
LIMIT_4H = 180

# Score drempel om Pre-BUY te maken
MIN_SCORE_PCT = 65

# ✅ Belangrijk: multi_coin_score is PRE-BUY ONLY
PREBUY_ONLY = True

# ✅ Test modus: maakt altijd 1 PENDING prebuy aan (zodat WhatsApp YES werkt)
FORCE_TEST_PREBUY = False
TEST_PREBUY_ID = "PB-TEST-001"

# Bestanden
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")

PENDING_FILE = os.path.join(DATA_DIR, "pending_approvals.json")
AI_ADVICE_LOG = os.path.join(LOGS_DIR, "ai_advice.csv")

# OpenAI (optioneel – als je dit nog niet gebruikt, zet USE_AI=False)
USE_AI = True
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Als je geen AI wilt: zet False
# Dan blijft alles werken (alleen minder tekst/uitleg)
# USE_AI = False

# =========================
# HELPERS
# =========================
def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def get_candles(symbol: str, interval: str, limit: int):
    """Return list of candles: [open_time, open, high, low, close, volume, ...]"""
    r = requests.get(BASE_URL, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=15)
    r.raise_for_status()
    return r.json()

def closes(candles):
    return [safe_float(c[4]) for c in candles]

def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n

def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += abs(diff)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - (100 / (1 + rs))

def trend_label(sma_fast, sma_slow):
    if sma_fast is None or sma_slow is None:
        return "unknown"
    if sma_fast > sma_slow:
        return "up"
    if sma_fast < sma_slow:
        return "down"
    return "flat"

def score_setup(close_1h, close_4h):
    """
    Simpele score:
    - 4h trend (SMA20 vs SMA50)
    - 1h pullback (RSI)
    - momentum (laatste close boven SMA20 1h)
    """
    c1 = close_1h
    c4 = close_4h

    sma20_1h = sma(c1, 20)
    sma50_1h = sma(c1, 50)
    rsi14_1h = rsi(c1, 14)

    sma20_4h = sma(c4, 20)
    sma50_4h = sma(c4, 50)
    rsi14_4h = rsi(c4, 14)

    t4 = trend_label(sma20_4h, sma50_4h)
    t1 = trend_label(sma20_1h, sma50_1h)

    score = 50  # basis

    # 4h trend weegt zwaar
    if t4 == "up":
        score += 20
    elif t4 == "down":
        score -= 20

    # 1h trend helpt timing
    if t1 == "up":
        score += 10
    elif t1 == "down":
        score -= 10

    # RSI pullback: ideaal ~35-55 in uptrend
    if rsi14_1h is not None:
        if 35 <= rsi14_1h <= 55:
            score += 10
        elif rsi14_1h < 30:
            score += 5  # oversold kan ok zijn, maar riskanter
        elif rsi14_1h > 70:
            score -= 10

    # Momentum: laatste close boven SMA20 1h
    if sma20_1h is not None and c1[-1] > sma20_1h:
        score += 5
    else:
        score -= 5

    # Clamp
    score = max(0, min(100, score))

    setup_type = "trend_continuation" if t4 == "up" else "counter_trend" if t4 == "down" else "neutral"
    return {
        "score_pct": int(round(score)),
        "setup_type": setup_type,
        "trend_4h": t4,
        "trend_1h": t1,
        "rsi_1h": None if rsi14_1h is None else round(rsi14_1h, 2),
        "rsi_4h": None if rsi14_4h is None else round(rsi14_4h, 2),
        "sma20_1h": None if sma20_1h is None else round(sma20_1h, 4),
        "sma50_1h": None if sma50_1h is None else round(sma50_1h, 4),
        "sma20_4h": None if sma20_4h is None else round(sma20_4h, 4),
        "sma50_4h": None if sma50_4h is None else round(sma50_4h, 4),
        "last_close": round(c1[-1], 4),
    }

def load_pending():
    ensure_dirs()
    if not os.path.exists(PENDING_FILE):
        return {"pending": []}
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"pending": []}

def save_pending(data):
    ensure_dirs()
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def add_pending_prebuy(prebuy_obj):
    """
    prebuy_obj minimaal:
    {
      prebuy_id, utc_time, coin, score_pct, setup_type, notes
    }
    """
    pending = load_pending()
    pending_list = pending.get("pending", [])

    # voorkom dubbele IDs
    existing_ids = {p.get("prebuy_id") for p in pending_list}
    if prebuy_obj.get("prebuy_id") in existing_ids:
        return False

    pending_list.append(prebuy_obj)
    pending["pending"] = pending_list
    save_pending(pending)
    return True

def log_ai_advice_row(row: dict):
    """
    CSV kolommen:
    utc_time, coin, score_pct, setup_type, verdict, confidence_pct, market_regime, notes
    """
    ensure_dirs()
    file_exists = os.path.exists(AI_ADVICE_LOG)

    headers = ["utc_time", "coin", "score_pct", "setup_type", "verdict", "confidence_pct", "market_regime", "notes"]

    with open(AI_ADVICE_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        if not file_exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in headers})

def extract_json_object(text: str):
    """
    Probeert JSON object uit tekst te trekken.
    Werkt ook als AI extra tekst ervoor/erna plakt.
    """
    if not text:
        return None
    # zoek eerste { ... } blok (simpel maar effectief)
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        return None
    candidate = m.group(0).strip()
    try:
        return json.loads(candidate)
    except Exception:
        return None

def ai_advice(prebuy_candidate: dict):
    """
    Verwacht JSON terug.
    Als AI faalt: return fallback (altijd geldig).
    """
    fallback = {
        "verdict": "B",
        "confidence_pct": 55,
        "market_regime": "unknown",
        "notes": "AI disabled/failed, fallback used."
    }

    if not USE_AI or not OPENAI_API_KEY:
        return fallback

    # Minimalistische prompt: AI móet JSON teruggeven.
    prompt = f"""
Geef ALLEEN een geldig JSON object terug. Geen tekst eromheen.

Schema:
{{
  "verdict": "A" | "B" | "C",
  "confidence_pct": 0-100,
  "market_regime": "up"|"down"|"range"|"transition"|"unknown",
  "notes": "korte uitleg"
}}

Context:
Coin: {prebuy_candidate.get("coin")}
Score: {prebuy_candidate.get("score_pct")}
Setup: {prebuy_candidate.get("setup_type")}
Trend 4h: {prebuy_candidate.get("trend_4h")}
Trend 1h: {prebuy_candidate.get("trend_1h")}
RSI 1h: {prebuy_candidate.get("rsi_1h")}
RSI 4h: {prebuy_candidate.get("rsi_4h")}
Last close: {prebuy_candidate.get("last_close")}
""".strip()

    try:
        # ✅ Simpele call (je kunt dit later vervangen door officiële OpenAI client)
        # Dit is bewust basic: liever stabiel dan fancy.
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": "You output only valid JSON and nothing else."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        text = data["choices"][0]["message"]["content"]

        obj = None
        try:
            obj = json.loads(text)
        except Exception:
            obj = extract_json_object(text)

        if not obj:
            return {**fallback, "notes": "AI returned invalid JSON (fallback).", "raw": text[:250]}

        # valideren
        verdict = obj.get("verdict", "B")
        conf = obj.get("confidence_pct", 55)
        regime = obj.get("market_regime", "unknown")
        notes = obj.get("notes", "")

        if verdict not in ["A", "B", "C"]:
            verdict = "B"
        try:
            conf = int(conf)
        except Exception:
            conf = 55
        conf = max(0, min(100, conf))

        if regime not in ["up", "down", "range", "transition", "unknown"]:
            regime = "unknown"

        return {
            "verdict": verdict,
            "confidence_pct": conf,
            "market_regime": regime,
            "notes": str(notes)[:300],
        }

    except Exception as e:
        return {**fallback, "notes": f"AI error fallback: {e}"}


# =========================
# MAIN
# =========================
def main():
    ensure_dirs()
    print("🚀 multi_coin_score gestart (Pre-BUY only)")
    print(f"UTC time: {utc_now_iso()}")

    new_prebuys = 0

    # ✅ Force test prebuy (voor WhatsApp flow)
    if FORCE_TEST_PREBUY:
        test_prebuy = {
            "prebuy_id": TEST_PREBUY_ID,
            "utc_time": utc_now_iso(),
            "coin": "BTCUSDT",
            "score_pct": 78,
            "setup_type": "trend_continuation",
            "trend_4h": "up",
            "trend_1h": "up",
            "rsi_1h": 45.0,
            "rsi_4h": 52.0,
            "last_close": 0,
            "notes": "FORCED TEST PREBUY",
        }

        # AI advice + log
        advice = ai_advice(test_prebuy)
        log_ai_advice_row({
            "utc_time": test_prebuy["utc_time"],
            "coin": test_prebuy["coin"],
            "score_pct": test_prebuy["score_pct"],
            "setup_type": test_prebuy["setup_type"],
            "verdict": advice.get("verdict"),
            "confidence_pct": advice.get("confidence_pct"),
            "market_regime": advice.get("market_regime"),
            "notes": advice.get("notes"),
        })

        ok = add_pending_prebuy(test_prebuy)
        if ok:
            new_prebuys += 1
            print(f"✅ TEST Pre-BUY toegevoegd: {TEST_PREBUY_ID}")
        else:
            print(f"ℹ️ TEST Pre-BUY bestond al: {TEST_PREBUY_ID}")

        print(f"✅ Pre-BUY run klaar — {new_prebuys} nieuwe Pre-BUY(s).")
        return

    # ✅ Normale run
    for symbol in COINS:
        try:
            c1 = get_candles(symbol, INTERVAL_1H, LIMIT_1H)
            c4 = get_candles(symbol, INTERVAL_4H, LIMIT_4H)

            close_1h = closes(c1)
            close_4h = closes(c4)

            setup = score_setup(close_1h, close_4h)

            score_pct = setup["score_pct"]
            if score_pct < MIN_SCORE_PCT:
                continue

            prebuy_id = f"PB-{datetime.now().strftime('%Y%m%d')}-{symbol}-{uuid.uuid4().hex[:6].upper()}"

            prebuy = {
                "prebuy_id": prebuy_id,
                "utc_time": utc_now_iso(),
                "coin": symbol,
                "score_pct": score_pct,
                "setup_type": setup["setup_type"],
                "trend_4h": setup["trend_4h"],
                "trend_1h": setup["trend_1h"],
                "rsi_1h": setup["rsi_1h"],
                "rsi_4h": setup["rsi_4h"],
                "last_close": setup["last_close"],
                "notes": "",
            }

            advice = ai_advice(prebuy)
            prebuy["notes"] = advice.get("notes", "")

            # log AI advice
            log_ai_advice_row({
                "utc_time": prebuy["utc_time"],
                "coin": prebuy["coin"],
                "score_pct": prebuy["score_pct"],
                "setup_type": prebuy["setup_type"],
                "verdict": advice.get("verdict"),
                "confidence_pct": advice.get("confidence_pct"),
                "market_regime": advice.get("market_regime"),
                "notes": advice.get("notes"),
            })

            # ✅ PENDING toevoegen (dit is wat WhatsApp nodig heeft)
            ok = add_pending_prebuy(prebuy)
            if ok:
                new_prebuys += 1
                print(f"✅ Pre-BUY toegevoegd: {prebuy_id} ({symbol}) score={score_pct}")
            else:
                print(f"ℹ️ Bestond al: {prebuy_id}")

            # kleine pauze (rate limiting vriendelijk)
            time.sleep(0.2)

        except Exception as e:
            print(f"⚠️ {symbol} fout: {e}")

    print(f"✅ Pre-BUY run klaar — {new_prebuys} nieuwe Pre-BUY(s).")
    print(f"📌 Pending file: {PENDING_FILE}")
    print(f"🧾 AI log: {AI_ADVICE_LOG}")


if __name__ == "__main__":
    main()


import os
import time
import json

def force_test_prebuy():
    # 1 simpele, veilige test-prebuy (alleen om de flow te testen)
    test = {
        "id": f"PB-TEST-{int(time.time())}",
        "coin": "BTCUSDT",
        "setup": "TEST",
        "score": 99.0,
        "kans": "test",
        "entry": 100.0,
        "stop_loss": 95.0,
        "target": 110.0,
        "status": "PENDING",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "expires_at": int(time.time()) + 4 * 60 * 60
    }

    path = "data/pending_approvals.json"

    # file lezen
    try:
        with open(path, "r", encoding="utf-8") as f:
            arr = json.load(f)
            if not isinstance(arr, list):
                arr = []
    except FileNotFoundError:
        arr = []

    # voorkom spam: max 1 test prebuy tegelijk
    if any(x.get("setup") == "TEST" and x.get("status") == "PENDING" for x in arr):
        print("🟡 TEST prebuy bestaat al (PENDING) — geen nieuwe gemaakt.")
        return

    arr.insert(0, test)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(arr, f, indent=2)

    print(f"✅ TEST PRE-BUY gemaakt: {test['id']} (PENDING)")

if __name__ == "__main__":
    # jouw normale run blijft eerst draaien
    # (dus je bestaande logic blijft bestaan)

    # 🔥 Force-test alleen als env var aan staat
    if os.getenv("FORCE_TEST_PREBUY", "0") == "1":
        force_test_prebuy()
