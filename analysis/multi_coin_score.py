from __future__ import annotations

import os
import sys
import json
import time
import csv
import hashlib
import traceback
from typing import Any, Dict, List, Optional, Tuple

import requests

# ==========================================================
# PROJECT ROOT
# ==========================================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ==========================================================
# ENV
# ==========================================================
WEBHOOK_BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or "").strip().rstrip("/")
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()

FORCE_TEST_PREBUY = (os.getenv("FORCE_TEST_PREBUY") or "0").strip() == "1"

PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))
MIN_SCORE_TO_PREBUY = int(os.getenv("MIN_SCORE_TO_PREBUY") or "80")

# WATCH drempel: trade wél melden + opslaan als ervaring, maar label als WATCH
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "70")

# cooldown in seconden (bijv. 6 uur)
TRADE_COOLDOWN_SECONDS = int(os.getenv("TRADE_COOLDOWN_SECONDS") or str(6 * 60 * 60))

# ✅ Nieuw: Postgres (optioneel)
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

# ==========================================================
# DATA PATHS (ALTIJD via ENV)
# - Default /data (Render Disk). Als service geen disk heeft -> zet DATA_DIR env of hij valt terug naar /tmp/data
# ==========================================================
DATA_DIR = (os.getenv("DATA_DIR") or "/data").rstrip("/")
LOGS_DIR = (os.getenv("LOGS_DIR") or os.path.join(DATA_DIR, "logs")).rstrip("/")

FINGERPRINT_PATH = os.getenv("FINGERPRINT_PATH", os.path.join(DATA_DIR, "fingerprints.json"))

# Experience log (CSV) – GO én WATCH worden opgeslagen
EXPERIENCE_CSV = os.getenv("EXPERIENCE_CSV", os.path.join(DATA_DIR, "experience.csv"))

# ✅ NIEUW: Scoreboard (gemaakt door history_simulator.py)
SCOREBOARD_PATH = os.getenv("SCOREBOARD_PATH", os.path.join(DATA_DIR, "scoreboard.json"))

# ==========================================================
# MARKET SETTINGS
# ==========================================================
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

COINS = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "SOLUSDT",
]

INTERVAL = "1h"
LIMIT = 200

# ==========================================================
# HELPERS
# ==========================================================
def log(msg: str) -> None:
    print(msg, flush=True)

def now_utc() -> int:
    return int(time.time())

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def ensure_data_file(path: str, default: Any) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)

def load_json(path: str, default: Any) -> Any:
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def safe_prepare_storage() -> None:
    """
    ✅ Belangrijk: sommige Render services hebben GEEN Disk mount.
    Dan is /data niet schrijfbaar. We vallen dan automatisch terug naar /tmp/data.
    """
    global DATA_DIR, LOGS_DIR, FINGERPRINT_PATH, EXPERIENCE_CSV, SCOREBOARD_PATH

    try:
        ensure_dir(DATA_DIR)
        ensure_dir(LOGS_DIR)
    except PermissionError:
        fallback = "/tmp/data"
        log(f"⚠️ Geen write access op {DATA_DIR}. Fallback naar {fallback}")
        DATA_DIR = fallback
        LOGS_DIR = os.path.join(DATA_DIR, "logs")
        FINGERPRINT_PATH = os.path.join(DATA_DIR, "fingerprints.json")
        EXPERIENCE_CSV = os.path.join(DATA_DIR, "experience.csv")
        SCOREBOARD_PATH = os.path.join(DATA_DIR, "scoreboard.json")
        ensure_dir(DATA_DIR)
        ensure_dir(LOGS_DIR)

# ==========================================================
# ✅ POSTGRES HELPERS (optioneel, maar nu toegevoegd)
# ==========================================================
def db_available() -> bool:
    if not DATABASE_URL:
        return False
    try:
        import psycopg2  # type: ignore
        _ = psycopg2
        return True
    except Exception:
        return False

def db_upsert_prebuy(prebuy: Dict[str, Any]) -> bool:
    """
    Slaat Pre-BUY op in Postgres (pending_approvals).
    Werkt alleen als DATABASE_URL bestaat én psycopg2 geïnstalleerd is.
    """
    if not db_available():
        return False

    try:
        import psycopg2  # type: ignore
        from psycopg2.extras import Json  # type: ignore

        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # Let op: jouw tabel moet minimaal id/symbol/status/created_at hebben.
        # We stoppen de volledige prebuy in payload zodat je later niks kwijt bent.
        cur.execute(
            """
            INSERT INTO pending_approvals (
                id, symbol, setup_type, regime, score, label,
                entry, stop, target,
                status, created_at, expires_at, updated_at, payload
            )
            VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, NOW(), to_timestamp(%s), NOW(), %s
            )
            ON CONFLICT (id) DO UPDATE SET
                symbol = EXCLUDED.symbol,
                setup_type = EXCLUDED.setup_type,
                regime = EXCLUDED.regime,
                score = EXCLUDED.score,
                label = EXCLUDED.label,
                entry = EXCLUDED.entry,
                stop = EXCLUDED.stop,
                target = EXCLUDED.target,
                status = EXCLUDED.status,
                expires_at = EXCLUDED.expires_at,
                updated_at = NOW(),
                payload = EXCLUDED.payload
            """,
            (
                prebuy["id"],
                prebuy["coin"],
                prebuy.get("setup_type", ""),
                prebuy.get("market_regime", ""),
                int(prebuy.get("score", 0) or 0),
                prebuy.get("grade", ""),
                float(prebuy.get("entry", 0.0) or 0.0),
                float(prebuy.get("stop_loss", 0.0) or 0.0),
                float(prebuy.get("target", 0.0) or 0.0),
                prebuy.get("status", "PENDING"),
                int(prebuy.get("expires_at", now_utc()) or now_utc()),
                Json(prebuy),
            ),
        )

        conn.commit()
        cur.close()
        conn.close()
        return True

    except Exception:
        traceback.print_exc()
        return False

# ==========================================================
# BINANCE DATA
# ==========================================================
def get_klines(symbol: str, interval: str, limit: int) -> List[List[Any]]:
    r = requests.get(
        BINANCE_KLINES,
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()

def closes_from_klines(klines: List[List[Any]]) -> List[float]:
    return [float(k[4]) for k in klines if k and len(k) > 4]

# ==========================================================
# INDICATORS
# ==========================================================
def sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses += abs(delta)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))

def slope_sma(values: List[float], period: int, lookback: int = 6) -> Optional[float]:
    """Simpel slope signaal: SMA nu - SMA lookback candles geleden."""
    if len(values) < period + lookback:
        return None
    now_val = sum(values[-period:]) / period
    prev_val = sum(values[-period - lookback : -lookback]) / period
    return now_val - prev_val

# ==========================================================
# REGIME / SETUP & SCORING
# ==========================================================
def determine_market_regime(s20: float, s50: float, s20_slope: float) -> str:
    # simpel maar bruikbaar: bull/bear/range
    if s20 > s50 and s20_slope > 0:
        return "BULL"
    if s20 < s50 and s20_slope < 0:
        return "BEAR"
    return "RANGE"

def determine_setup(s20: float, s50: float, r14: float) -> str:
    if s20 > s50 and r14 >= 55:
        return "TREND_PULLBACK"
    if s20 < s50 and r14 <= 45:
        return "BREAKOUT_RETEST"  # (houd je 2 setups aan; BEAR_RALLY niet gebruiken)
    return "BREAKOUT_RETEST"

def score_symbol(closes: List[float]) -> Tuple[int, Dict[str, Any]]:
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    r14 = rsi(closes, 14)
    s20_sl = slope_sma(closes, 20, lookback=6)

    if s20 is None or s50 is None or r14 is None or s20_sl is None:
        return 0, {}

    score = 50
    if s20 > s50:
        score += 20
    else:
        score -= 10

    if r14 >= 60:
        score += 20
    elif r14 <= 40:
        score -= 10

    setup = determine_setup(s20, s50, r14)
    regime = determine_market_regime(s20, s50, s20_sl)

    return max(0, min(100, score)), {
        "sma20": s20,
        "sma50": s50,
        "rsi14": r14,
        "setup_type": setup,
        "market_regime": regime,
        "sma20_slope": s20_sl,
    }

# ==========================================================
# ✅ SCOREBOARD READ + CONTEXT
# ==========================================================
def load_scoreboard() -> Dict[str, Any]:
    # scoreboard.json wordt gemaakt door history_simulator.py
    return load_json(SCOREBOARD_PATH, {})

def sb_key(setup_type: str, market_regime: str) -> str:
    return f"{setup_type}|{market_regime}"

def scoreboard_context(sb: Dict[str, Any], coin: str, setup_type: str, market_regime: str) -> Dict[str, Any]:
    """
    Verwacht grofweg structuur zoals:
    sb["global"][ "TREND_PULLBACK|BULL" ] = {"trades":..., "winrate":..., "avg_r":..., ...}
    sb["by_coin"]["XRPUSDT"][ "TREND_PULLBACK|BEAR" ] = {...}
    Werkt ook als die structuur nog niet perfect is -> dan geeft hij gewoon {} terug.
    """
    key = sb_key(setup_type, market_regime)
    out: Dict[str, Any] = {"key": key}

    # coin-level (als aanwezig)
    by_coin = sb.get("by_coin", {}) if isinstance(sb, dict) else {}
    coin_bucket = by_coin.get(coin, {}) if isinstance(by_coin, dict) else {}
    coin_stats = coin_bucket.get(key, {}) if isinstance(coin_bucket, dict) else {}

    # global-level (als aanwezig)
    global_bucket = sb.get("global", {}) if isinstance(sb, dict) else {}
    global_stats = global_bucket.get(key, {}) if isinstance(global_bucket, dict) else {}

    # kies coin_stats als die voldoende sample heeft, anders global
    def trades(x: Any) -> int:
        try:
            return int(x.get("trades", 0))
        except Exception:
            return 0

    chosen = coin_stats if trades(coin_stats) >= 30 else global_stats
    if not isinstance(chosen, dict):
        chosen = {}

    out["stats"] = {
        "source": "coin" if chosen is coin_stats else "global",
        "trades": trades(chosen),
        "winrate": float(chosen.get("winrate", 0.0) or 0.0),
        "avg_r": float(chosen.get("avg_r", 0.0) or 0.0),
    }

    # eenvoudige interpretatie (geen hard skippen; alleen context + confidence tweak)
    wr = out["stats"]["winrate"]
    n = out["stats"]["trades"]

    if n < 30:
        out["verdict"] = "LOW_SAMPLE"
        out["confidence_adj"] = -5
        out["note"] = "Weinig historie voor deze combinatie."
    else:
        if wr >= 0.55:
            out["verdict"] = "STRONG"
            out["confidence_adj"] = +8
            out["note"] = "Historisch bovengemiddeld voor deze combinatie."
        elif wr <= 0.45:
            out["verdict"] = "WEAK"
            out["confidence_adj"] = -10
            out["note"] = "Historisch ondergemiddeld voor deze combinatie."
        else:
            out["verdict"] = "NEUTRAL"
            out["confidence_adj"] = 0
            out["note"] = "Historisch gemiddeld voor deze combinatie."

    return out

# ==========================================================
# FINGERPRINT LOGIC (ANTI-DUPLICATE)
# ==========================================================
def make_fingerprint(coin: str, setup: str, entry: float, target: float) -> str:
    raw = f"{coin}|{setup}|{round(entry,3)}|{round(target,3)}"
    return hashlib.sha256(raw.encode()).hexdigest()

def is_duplicate(fp: str, fingerprints: Dict[str, Any]) -> bool:
    rec = fingerprints.get(fp)
    if not rec:
        return False
    return (now_utc() - rec["ts"]) < TRADE_COOLDOWN_SECONDS

def store_fingerprint(fp: str, coin: str, setup: str, grade: str) -> None:
    fingerprints = load_json(FINGERPRINT_PATH, {})
    fingerprints[fp] = {
        "coin": coin,
        "setup": setup,
        "grade": grade,
        "ts": now_utc(),
    }
    save_json(FINGERPRINT_PATH, fingerprints)

# ==========================================================
# EXPERIENCE CSV (GO + WATCH opslaan)
# ==========================================================
EXPERIENCE_HEADERS = [
    "timestamp",
    "coin",
    "setup_type",
    "market_regime",
    "grade",             # GO / WATCH
    "entry",
    "stop",
    "target",
    "decision",          # user decision later (BUY/NO/...) -> nu leeg
    "outcome",           # later invullen door simulatie/monitor
    "mfe",               # later
    "mae",               # later
    "time_minutes",      # later
    "why",
    "market_condition",
    "bot_confidence",
    "overextended",
]

def ensure_experience_csv() -> None:
    ensure_dir(os.path.dirname(EXPERIENCE_CSV) or ".")
    if not os.path.exists(EXPERIENCE_CSV):
        with open(EXPERIENCE_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(EXPERIENCE_HEADERS)

def append_experience_row(row: Dict[str, Any]) -> None:
    ensure_experience_csv()
    with open(EXPERIENCE_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([row.get(h, "") for h in EXPERIENCE_HEADERS])

def calc_overextended(price: float, sma20_val: float) -> float:
    if sma20_val <= 0:
        return 0.0
    return round(((price - sma20_val) / sma20_val) * 100.0, 3)  # % boven SMA20

# ==========================================================
# PREBUY / WEBHOOK (optioneel)
# ==========================================================
def build_prebuy(
    symbol: str,
    score: int,
    details: Dict[str, Any],
    price: float,
    sb_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    created = now_utc()
    stop = price * 0.98
    target = price + (price - stop) * 2

    # basis label
    grade = "GO" if score >= MIN_SCORE_TO_PREBUY else "WATCH"

    # ✅ scoreboard influence: geen hard skip, alleen label strakker + confidence aanpassen
    bot_conf = score
    sb_note = ""
    sb_verdict = ""
    if sb_ctx:
        adj = int(sb_ctx.get("confidence_adj", 0) or 0)
        bot_conf = max(0, min(100, score + adj))
        sb_verdict = str(sb_ctx.get("verdict", "") or "")
        sb_note = str(sb_ctx.get("note", "") or "")

        # als historie WEAK is -> degrade naar WATCH (zelfs als score hoog is)
        if sb_verdict == "WEAK":
            grade = "WATCH"
        # als historie STRONG is en score nét onder GO -> promote naar GO
        if sb_verdict == "STRONG" and score >= (MIN_SCORE_TO_PREBUY - 5):
            grade = "GO"

    return {
        "id": f"PB-{symbol}-{created}",
        "coin": symbol,
        "setup_type": details.get("setup_type", "UNKNOWN"),
        "market_regime": details.get("market_regime", ""),
        "score": score,
        "bot_confidence": bot_conf,
        "grade": grade,
        "status": "PENDING",
        "created_at": created,
        "expires_at": created + PREBUY_VALID_SECONDS,
        "entry": round(price, 8),
        "stop_loss": round(stop, 8),
        "target": round(target, 8),
        "details": details,
        "scoreboard": {
            "path": SCOREBOARD_PATH,
            "verdict": sb_verdict,
            "note": sb_note,
            "stats": (sb_ctx or {}).get("stats", {}),
            "key": (sb_ctx or {}).get("key", ""),
        },
    }

def send_to_webservice(prebuy: Dict[str, Any]) -> bool:
    if not WEBHOOK_BASE_URL or not INTERNAL_TOKEN:
        return False
    try:
        r = requests.post(
            f"{WEBHOOK_BASE_URL}/internal/prebuy",
            json=prebuy,
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=20,
        )
        return r.status_code < 300
    except Exception:
        return False

# ==========================================================
# MAIN
# ==========================================================
def main() -> None:
    log("🚀 multi_coin_score gestart")

    # ✅ storage voorbereiden (met fallback)
    safe_prepare_storage()

    ensure_data_file(FINGERPRINT_PATH, {})
    ensure_experience_csv()

    fingerprints = load_json(FINGERPRINT_PATH, {})

    # ✅ scoreboard laden (als hij bestaat)
    sb = load_scoreboard()
    if sb:
        log(f"📘 Scoreboard geladen: {SCOREBOARD_PATH}")
    else:
        log(f"📘 Scoreboard niet gevonden/leeg: {SCOREBOARD_PATH} (gaat alsnog door)")

    # DB status log
    if db_available():
        log("🗄️ Postgres: AAN (DATABASE_URL gevonden + psycopg2 aanwezig)")
    else:
        log("🗄️ Postgres: UIT (geen DATABASE_URL of psycopg2 ontbreekt)")

    # Force test: maakt 1 fake prebuy + schrijft ervaring weg
    if FORCE_TEST_PREBUY:
        fake_details = {
            "setup_type": "TREND_PULLBACK",
            "market_regime": "RANGE",
            "sma20": 100.0,
            "sma50": 100.0,
            "rsi14": 55.0,
            "sma20_slope": 0.0,
        }
        sb_ctx = scoreboard_context(sb, "BTCUSDT", "TREND_PULLBACK", "RANGE") if sb else None
        prebuy = build_prebuy("BTCUSDT", 79, fake_details, 100.0, sb_ctx=sb_ctx)

        append_experience_row({
            "timestamp": prebuy["created_at"],
            "coin": prebuy["coin"],
            "setup_type": prebuy["setup_type"],
            "market_regime": prebuy["market_regime"],
            "grade": prebuy["grade"],
            "entry": prebuy["entry"],
            "stop": prebuy["stop_loss"],
            "target": prebuy["target"],
            "decision": "",
            "outcome": "",
            "mfe": "",
            "mae": "",
            "time_minutes": "",
            "why": f"SCOREBOARD:{(sb_ctx or {}).get('note','FORCE_TEST_PREBUY')}",
            "market_condition": "test",
            "bot_confidence": prebuy.get("bot_confidence", prebuy["score"]),
            "overextended": 0.0,
        })

        # ✅ Nieuw: ook naar DB proberen
        db_ok = db_upsert_prebuy(prebuy)
        sent = send_to_webservice(prebuy)

        log(f"✅ FORCE_TEST_PREBUY: experience opgeslagen | db={'OK' if db_ok else 'NO'} | webhook={'OK' if sent else 'NO'}")
        return

    for symbol in COINS:
        try:
            klines = get_klines(symbol, INTERVAL, LIMIT)
            closes = closes_from_klines(klines)
            if len(closes) < 60:
                continue

            score, details = score_symbol(closes)
            if not details:
                continue

            # Alleen loggen/melden als score minimaal WATCH niveau haalt
            if score < WATCH_MIN_SCORE:
                continue

            price = closes[-1]

            setup_type = str(details.get("setup_type", "UNKNOWN"))
            market_regime = str(details.get("market_regime", ""))

            # ✅ scoreboard context ophalen
            sb_ctx = scoreboard_context(sb, symbol, setup_type, market_regime) if sb else None

            prebuy = build_prebuy(symbol, score, details, price, sb_ctx=sb_ctx)

            fp = make_fingerprint(symbol, prebuy["setup_type"], prebuy["entry"], prebuy["target"])
            if is_duplicate(fp, fingerprints):
                log(f"⏭️ Skip duplicate {symbol} {prebuy['setup_type']} ({prebuy['grade']})")
                continue

            # 1) Opslaan als ervaring (GO én WATCH)
            overext = calc_overextended(price, float(details["sma20"]))
            why_note = ""
            if sb_ctx:
                why_note = f"SCOREBOARD:{sb_ctx.get('verdict','')} | {sb_ctx.get('note','')}"
            else:
                why_note = prebuy["setup_type"]

            append_experience_row({
                "timestamp": prebuy["created_at"],
                "coin": prebuy["coin"],
                "setup_type": prebuy["setup_type"],
                "market_regime": prebuy["market_regime"],
                "grade": prebuy["grade"],
                "entry": prebuy["entry"],
                "stop": prebuy["stop_loss"],
                "target": prebuy["target"],
                "decision": "",
                "outcome": "",
                "mfe": "",
                "mae": "",
                "time_minutes": "",
                "why": why_note,
                "market_condition": "normal",
                "bot_confidence": prebuy.get("bot_confidence", score),
                "overextended": overext,
            })

            # 2) ✅ Nieuw: ook in Postgres zetten (centrale pending)
            db_ok = db_upsert_prebuy(prebuy)

            # 3) Optioneel naar WhatsApp-webservice sturen (mag blijven)
            sent = send_to_webservice(prebuy)

            # 4) Fingerprint opslaan (anti-duplicate)
            store_fingerprint(fp, symbol, prebuy["setup_type"], prebuy["grade"])

            log(
                f"✅ Pre-BUY ({prebuy['grade']}): {symbol} {prebuy['setup_type']} "
                f"score={score} conf={prebuy.get('bot_confidence',score)} "
                f"db={'OK' if db_ok else 'NO'} webhook={'OK' if sent else 'NO'} sb={prebuy['scoreboard'].get('verdict','')}"
            )

        except Exception:
            traceback.print_exc()

    log("✅ Run klaar")

if __name__ == "__main__":
    main()
