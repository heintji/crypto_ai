# research/history_simulator.py
from __future__ import annotations

import os
import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# ==========================================================
# ENV / PATHS
# ==========================================================
DATA_DIR = (os.getenv("DATA_DIR") or "/tmp/data").rstrip("/")
HISTORY_DIR = os.path.join(DATA_DIR, "history")
OUT_EXPERIENCE = os.getenv("EXPERIENCE_PATH", os.path.join(DATA_DIR, "experience.csv"))
OUT_SCOREBOARD = os.getenv("SCOREBOARD_PATH", os.path.join(DATA_DIR, "scoreboard.json"))

# Welke coins wil je simuleren?
COINS = (os.getenv("HIST_COINS") or "BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT,ADAUSDT,SOLUSDT,DOGEUSDT").split(",")
COINS = [c.strip().upper() for c in COINS if c.strip()]

# Timeframes
TF_ENTRY = os.getenv("SIM_ENTRY_INTERVAL", "1h").strip() or "1h"
TF_REGIME = os.getenv("SIM_REGIME_INTERVAL", "4h").strip() or "4h"

# Simulatie regels (basis)
STOP_PCT = float(os.getenv("SIM_STOP_PCT", "0.02"))     # 2% stop
RR_TARGET = float(os.getenv("SIM_RR", "2.0"))           # target = 2R
MAX_HOLD_CANDLES = int(os.getenv("SIM_MAX_HOLD", "72")) # max 72 candles (72 uur op 1h)

# Grade regels (GO/WATCH) -> geen filtering, alleen labelen
GO_MIN_SCORE = int(os.getenv("GO_MIN_SCORE", "80"))     # >=80 = GO
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE", "60"))  # 60-79 = WATCH

# Setup keuze
SETUPS = (os.getenv("SIM_SETUPS", "TREND_PULLBACK,BREAKOUT_RETEST").split(","))
SETUPS = [s.strip().upper() for s in SETUPS if s.strip()]

# ==========================================================
# DATA STRUCTURES
# ==========================================================
@dataclass
class Candle:
    ts_ms: int
    open: float
    high: float
    low: float
    close: float

def _read_csv_candles(path: str) -> List[Candle]:
    """
    Expected CSV header from history_fetcher.py:
    open_time_ms, open_time_utc, open, high, low, close, volume, close_time_ms, trades
    """
    out: List[Candle] = []
    if not os.path.exists(path):
        return out

    with open(path, "r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                ts = int(row["open_time_ms"])
                out.append(Candle(
                    ts_ms=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                ))
            except Exception:
                continue

    out.sort(key=lambda c: c.ts_ms)
    return out

def _sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def _rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        d = values[i] - values[i - 1]
        if d >= 0:
            gains += d
        else:
            losses += abs(d)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))

def _slope(values: List[float], lookback: int = 5) -> Optional[float]:
    # simpele slope: laatste - (n candles terug)
    if len(values) < lookback + 1:
        return None
    return values[-1] - values[-1 - lookback]

def _utc_iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()

# ==========================================================
# REGIME LABELING (4h)
# ==========================================================
def _compute_regime_map(c4h: List[Candle]) -> Dict[int, str]:
    """
    regime = BULL / BEAR / RANGE
    Simpel & sterk:
    - BULL: SMA20 > SMA50 én slope(SMA20) positief
    - BEAR: SMA20 < SMA50 én slope(SMA20) negatief
    - anders RANGE
    Map key: candle ts_ms -> regime
    """
    closes: List[float] = []
    sma20_list: List[float] = []
    sma50_list: List[float] = []

    regime_map: Dict[int, str] = {}

    for c in c4h:
        closes.append(c.close)
        s20 = _sma(closes, 20)
        s50 = _sma(closes, 50)

        if s20 is None or s50 is None:
            regime_map[c.ts_ms] = "UNKNOWN"
            continue

        sma20_list.append(s20)
        sma50_list.append(s50)

        sl = _slope(sma20_list, lookback=5)
        if sl is None:
            regime_map[c.ts_ms] = "UNKNOWN"
            continue

        if s20 > s50 and sl > 0:
            regime_map[c.ts_ms] = "BULL"
        elif s20 < s50 and sl < 0:
            regime_map[c.ts_ms] = "BEAR"
        else:
            regime_map[c.ts_ms] = "RANGE"

    return regime_map

def _regime_at_ts(regime_map: Dict[int, str], c4h: List[Candle], ts_ms: int) -> str:
    """
    Pak laatste 4h candle die <= ts_ms is.
    """
    # binary search kan later; nu simpel
    last = "UNKNOWN"
    for c in c4h:
        if c.ts_ms <= ts_ms:
            last = regime_map.get(c.ts_ms, "UNKNOWN")
        else:
            break
    return last

# ==========================================================
# SETUP DETECTION (1h)
# ==========================================================
def _score_and_setup_1h(closes: List[float]) -> Tuple[int, str, Dict[str, float]]:
    """
    Maak score 0-100 + setup_type.
    Basis: SMA20/SMA50 + RSI14.
    """
    s20 = _sma(closes, 20)
    s50 = _sma(closes, 50)
    r14 = _rsi(closes, 14)

    if s20 is None or s50 is None or r14 is None:
        return 0, "NONE", {}

    score = 50
    score += 20 if s20 > s50 else -10

    if r14 >= 60:
        score += 20
    elif r14 <= 40:
        score -= 10

    score = max(0, min(100, score))

    # setup classificatie
    if s20 > s50 and r14 >= 55:
        setup = "TREND_PULLBACK"
    elif s20 <= s50 and r14 >= 55:
        # dit kan breakout zijn, maar we checken later met structurele regel
        setup = "BREAKOUT_RETEST"
    else:
        setup = "NONE"

    return score, setup, {"sma20": s20, "sma50": s50, "rsi14": r14}

def _is_breakout_retest(c1h: List[Candle], idx: int, lookback: int = 48) -> bool:
    """
    Heel simpele breakout-retest detector:
    - koers breekt boven de high van de afgelopen lookback candles
    - daarna komt hij terug richting dat niveau en sluit er weer boven
    (basisversie: 2-fase check met laatste 8 candles)
    """
    if idx < lookback + 10:
        return False

    # niveau = hoogste high van lookback periode (tot idx-9)
    past = c1h[idx - lookback: idx - 9]
    lvl = max(x.high for x in past)

    # fase 1: breakout in (idx-8 .. idx-5)
    breakout = any(c1h[j].close > lvl for j in range(idx - 8, idx - 4))
    if not breakout:
        return False

    # fase 2: retest: low raakt lvl (of eronder) en close weer boven lvl (idx-4 .. idx)
    for j in range(idx - 4, idx + 1):
        if c1h[j].low <= lvl * 1.002 and c1h[j].close > lvl:
            return True
    return False

def _grade_from_score(score: int) -> str:
    if score >= GO_MIN_SCORE:
        return "GO"
    if score >= WATCH_MIN_SCORE:
        return "WATCH"
    # ook dit bewaren als ervaring? jij zei GO & WATCH, dus onder WATCH slaan we niet op
    return "IGNORE"

def _market_condition_from_atr_like(c1h: List[Candle], idx: int, period: int = 14) -> str:
    """
    Simpele volatiliteit label: rustig/normaal/volatiel
    We doen: gemiddelde range% over laatste N candles.
    """
    if idx < period:
        return "unknown"
    ranges = []
    for j in range(idx - period + 1, idx + 1):
        c = c1h[j]
        if c.close <= 0:
            continue
        ranges.append((c.high - c.low) / c.close)
    if not ranges:
        return "unknown"
    avg = sum(ranges) / len(ranges)
    if avg < 0.006:
        return "rustig"
    if avg < 0.015:
        return "normaal"
    return "volatiel"

def _overextended_pct(c1h: List[Candle], idx: int, ma_period: int = 20) -> float:
    """
    Hoe ver prijs boven/onder SMA20 is (%).
    """
    if idx < ma_period:
        return 0.0
    closes = [c.close for c in c1h[idx - ma_period + 1: idx + 1]]
    ma = sum(closes) / len(closes)
    if ma <= 0:
        return 0.0
    return (c1h[idx].close - ma) / ma * 100.0

# ==========================================================
# FORWARD SIMULATION (stop/target/timeout)
# ==========================================================
def _simulate_forward(c1h: List[Candle], entry_idx: int, entry: float, stop: float, target: float) -> Dict[str, object]:
    """
    Candle-by-candle vooruit:
    - Als low <= stop -> loss
    - Als high >= target -> win
    - Anders timeout na MAX_HOLD_CANDLES
    Meet:
    - MFE (max favorable excursion in R)
    - MAE (max adverse excursion in R)
    - time_minutes (tot outcome)
    """
    risk = entry - stop
    if risk <= 0:
        return {"outcome": "invalid", "mfe": 0.0, "mae": 0.0, "time_minutes": 0}

    mfe_r = 0.0
    mae_r = 0.0

    start_ts = c1h[entry_idx].ts_ms
    last_idx = min(len(c1h) - 1, entry_idx + MAX_HOLD_CANDLES)

    for i in range(entry_idx + 1, last_idx + 1):
        c = c1h[i]

        # update MFE/MAE intrabar
        best = (c.high - entry) / risk
        worst = (entry - c.low) / risk
        mfe_r = max(mfe_r, best)
        mae_r = max(mae_r, worst)

        # check exits
        if c.low <= stop:
            tmin = int((c.ts_ms - start_ts) / 60000)
            return {"outcome": "loss", "mfe": round(mfe_r, 4), "mae": round(mae_r, 4), "time_minutes": tmin}
        if c.high >= target:
            tmin = int((c.ts_ms - start_ts) / 60000)
            return {"outcome": "win", "mfe": round(mfe_r, 4), "mae": round(mae_r, 4), "time_minutes": tmin}

    # timeout
    tmin = int((c1h[last_idx].ts_ms - start_ts) / 60000)
    return {"outcome": "timeout", "mfe": round(mfe_r, 4), "mae": round(mae_r, 4), "time_minutes": tmin}

# ==========================================================
# EXPERIENCE WRITER
# ==========================================================
EXP_HEADERS = [
    "timestamp",
    "coin",
    "setup_type",
    "market_regime",
    "grade",               # GO / WATCH
    "entry",
    "stop",
    "target",
    "decision",            # LIVE later: YES/NO; simulator: "SIM"
    "outcome",             # win/loss/timeout
    "mfe",
    "mae",
    "time_minutes",
    "why",                 # korte reden (placeholder)
    "market_condition",    # rustig/normaal/volatiel
    "bot_confidence",      # 0-100
    "overextended",        # % van SMA
]

def _ensure_parent(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)

def _append_experience_row(row: Dict[str, object]) -> None:
    _ensure_parent(OUT_EXPERIENCE)
    file_exists = os.path.exists(OUT_EXPERIENCE)

    with open(OUT_EXPERIENCE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EXP_HEADERS)
        if not file_exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in EXP_HEADERS})

# ==========================================================
# SCOREBOARD
# ==========================================================
def _update_scoreboard(sb: Dict[str, object], row: Dict[str, object]) -> None:
    """
    Stats per (setup_type, regime, grade)
    """
    setup = str(row["setup_type"])
    regime = str(row["market_regime"])
    grade = str(row["grade"])
    outcome = str(row["outcome"])
    mfe = float(row["mfe"])
    mae = float(row["mae"])
    tmin = int(row["time_minutes"])

    key = f"{setup}|{regime}|{grade}"
    rec = sb.get(key, {
        "setup_type": setup,
        "market_regime": regime,
        "grade": grade,
        "n": 0,
        "wins": 0,
        "losses": 0,
        "timeouts": 0,
        "avg_mfe": 0.0,
        "avg_mae": 0.0,
        "avg_time_minutes": 0.0,
    })

    n0 = int(rec["n"])
    n1 = n0 + 1
    rec["n"] = n1

    if outcome == "win":
        rec["wins"] = int(rec["wins"]) + 1
    elif outcome == "loss":
        rec["losses"] = int(rec["losses"]) + 1
    else:
        rec["timeouts"] = int(rec["timeouts"]) + 1

    # running averages
    rec["avg_mfe"] = (float(rec["avg_mfe"]) * n0 + mfe) / n1
    rec["avg_mae"] = (float(rec["avg_mae"]) * n0 + mae) / n1
    rec["avg_time_minutes"] = (float(rec["avg_time_minutes"]) * n0 + tmin) / n1

    sb[key] = rec

def _save_scoreboard(sb: Dict[str, object]) -> None:
    _ensure_parent(OUT_SCOREBOARD)
    # nette output als list
    items = list(sb.values())
    items.sort(key=lambda x: (x["setup_type"], x["market_regime"], x["grade"]))
    with open(OUT_SCOREBOARD, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "items": items}, f, indent=2)

# ==========================================================
# MAIN SIMULATION
# ==========================================================
def _history_path(symbol: str, interval: str) -> str:
    return os.path.join(HISTORY_DIR, f"{symbol}_{interval}.csv")

def run_for_symbol(symbol: str) -> Tuple[int, Dict[str, object]]:
    """
    Returns: (rows_written, scoreboard_dict)
    """
    p1 = _history_path(symbol, TF_ENTRY)
    p4 = _history_path(symbol, TF_REGIME)

    c1h = _read_csv_candles(p1)
    c4h = _read_csv_candles(p4)

    if not c1h or not c4h:
        print(f"⚠️ Missing history for {symbol}: {p1} or {p4}")
        return 0, {}

    regime_map = _compute_regime_map(c4h)

    closes_1h: List[float] = []
    sb: Dict[str, object] = {}
    written = 0

    for i in range(len(c1h)):
        closes_1h.append(c1h[i].close)

        # genoeg data nodig voor indicators
        if i < 60:
            continue

        score, base_setup, ind = _score_and_setup_1h(closes_1h)
        grade = _grade_from_score(score)
        if grade == "IGNORE":
            continue  # alleen GO & WATCH opslaan als ervaring

        # setup check (2 setups)
        setup_type = "NONE"

        if "TREND_PULLBACK" in SETUPS and base_setup == "TREND_PULLBACK":
            setup_type = "TREND_PULLBACK"

        if "BREAKOUT_RETEST" in SETUPS:
            if _is_breakout_retest(c1h, i):
                setup_type = "BREAKOUT_RETEST"

        if setup_type == "NONE":
            continue

        # regime op dit moment
        regime = _regime_at_ts(regime_map, c4h, c1h[i].ts_ms)
        if regime == "UNKNOWN":
            continue

        entry = c1h[i].close
        stop = entry * (1.0 - STOP_PCT)
        target = entry + (entry - stop) * RR_TARGET

        sim = _simulate_forward(c1h, i, entry, stop, target)

        market_condition = _market_condition_from_atr_like(c1h, i)
        overext = _overextended_pct(c1h, i)

        # “why” (kort, later verfijnen)
        why = f"SMA20/50 + RSI ({round(ind.get('rsi14', 0.0),1)})"

        row = {
            "timestamp": _utc_iso(c1h[i].ts_ms),
            "coin": symbol,
            "setup_type": setup_type,
            "market_regime": regime,
            "grade": grade,
            "entry": round(entry, 8),
            "stop": round(stop, 8),
            "target": round(target, 8),
            "decision": "SIM",
            "outcome": sim["outcome"],
            "mfe": sim["mfe"],
            "mae": sim["mae"],
            "time_minutes": sim["time_minutes"],
            "why": why,
            "market_condition": market_condition,
            "bot_confidence": score,  # voorlopig = score
            "overextended": round(overext, 4),
        }

        _append_experience_row(row)
        _update_scoreboard(sb, row)
        written += 1

        if written % 500 == 0:
            print(f"✅ {symbol}: rows_written={written} (running)")

    return written, sb

def main() -> None:
    os.makedirs(HISTORY_DIR, exist_ok=True)
    print("🚀 history_simulator START")
    print(f"DATA_DIR: {DATA_DIR}")
    print(f"HISTORY_DIR: {HISTORY_DIR}")
    print(f"OUT_EXPERIENCE: {OUT_EXPERIENCE}")
    print(f"OUT_SCOREBOARD: {OUT_SCOREBOARD}")
    print(f"COINS: {COINS}")
    print(f"SETUPS: {SETUPS}")
    print("")

    # optioneel: start opnieuw (clean) zodat je niet dupliceert
    reset = (os.getenv("SIM_RESET", "1").strip() == "1")
    if reset and os.path.exists(OUT_EXPERIENCE):
        os.remove(OUT_EXPERIENCE)
        print("🧹 experience.csv verwijderd (SIM_RESET=1)")

    combined_sb: Dict[str, object] = {}
    total = 0

    for coin in COINS:
        w, sb = run_for_symbol(coin)
        total += w
        # merge scoreboards
        for k, v in sb.items():
            if k not in combined_sb:
                combined_sb[k] = v
            else:
                # merge: simpel opnieuw optellen via “fake rows” is omslachtig.
                # hier doen we basis merge op n en avg via weighted.
                a = combined_sb[k]
                b = v
                nA = int(a["n"])
                nB = int(b["n"])
                nT = nA + nB
                if nT == 0:
                    continue

                def wavg(xa, xb):
                    return (float(xa) * nA + float(xb) * nB) / nT

                a["n"] = nT
                a["wins"] = int(a["wins"]) + int(b["wins"])
                a["losses"] = int(a["losses"]) + int(b["losses"])
                a["timeouts"] = int(a["timeouts"]) + int(b["timeouts"])
                a["avg_mfe"] = wavg(a["avg_mfe"], b["avg_mfe"])
                a["avg_mae"] = wavg(a["avg_mae"], b["avg_mae"])
                a["avg_time_minutes"] = wavg(a["avg_time_minutes"], b["avg_time_minutes"])

    _save_scoreboard(combined_sb)

    print("")
    print(f"✅ history_simulator DONE | total_rows={total}")
    print(f"📄 experience: {OUT_EXPERIENCE}")
    print(f"📊 scoreboard: {OUT_SCOREBOARD}")

if __name__ == "__main__":
    main()
