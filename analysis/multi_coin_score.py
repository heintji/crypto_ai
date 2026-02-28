# analysis/multi_coin_score.py
from __future__ import annotations

import os
import sys
import time
import math
import json
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
import psycopg2
import psycopg2.extras


# =========================
# PATH / ROOT
# =========================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# local import (live_trader only for market list, not for trading!)
try:
    from trading.live_trader import (
        get_tradable_markets_cached,
        symbol_usdt_to_bitvavo_market,
    )
except Exception:
    get_tradable_markets_cached = None
    symbol_usdt_to_bitvavo_market = None


# =========================
# ENV
# =========================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

BINANCE_BASE_URL = (os.getenv("BINANCE_BASE_URL") or "https://api.binance.com").rstrip("/")
UNIVERSE_LIMIT = int(os.getenv("UNIVERSE_LIMIT") or "250")  # logs show 250
TF_MAIN = (os.getenv("TF_MAIN") or "4h").strip()
TF_CTX = (os.getenv("TF_CTX") or "1h").strip()

MAX_PREBUY_PER_DAY = int(os.getenv("MAX_PREBUY_PER_DAY") or "10")
MAX_ACTIVE_PREBUYS = int(os.getenv("MAX_ACTIVE_PREBUYS") or "10")
INCLUDE_WATCH_IN_PENDING = (os.getenv("INCLUDE_WATCH_IN_PENDING") or "0").strip() == "1"

PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))

MIN_SCORE_TO_GO = int(os.getenv("MIN_SCORE_TO_GO") or "80")
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE") or "70")

BITVAVO_FILTER_UNIVERSE = (os.getenv("BITVAVO_FILTER_UNIVERSE") or "1").strip() == "1"

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT") or "20")


# WhatsApp push via Twilio (optioneel)
TWILIO_ACCOUNT_SID = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
TWILIO_WHATSAPP_TO = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()


# =========================
# Models
# =========================
@dataclass
class Prebuy:
    prebuy_id: str
    symbol: str           # e.g. BARDUSDT
    timeframe: str        # e.g. 4h
    setup_type: str       # TREND_PULLBACK / BREAKOUT_RETEST
    regime: str           # BULL/BEAR/RANGE
    score: int            # normalized
    raw_score: int        # raw
    chance: int           # 0-100
    confidence: int       # 0-100
    entry: float
    stop: float
    target: float
    expires_at: datetime  # UTC
    bitvavo_market: Optional[str] = None
    label: str = "WATCH"  # GO/WATCH


# =========================
# DB
# =========================
def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def ensure_schema(cur):
    # pending_approvals minimal columns (robust)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS public.pending_approvals (
        id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        timeframe TEXT,
        setup_type TEXT,
        regime TEXT,
        score INTEGER,
        raw_score INTEGER,
        chance INTEGER,
        confidence INTEGER,
        entry DOUBLE PRECISION,
        stop DOUBLE PRECISION,
        target DOUBLE PRECISION,
        status TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        expires_at TIMESTAMPTZ,
        approved_at TIMESTAMPTZ,
        rejected_at TIMESTAMPTZ,
        consumed_at TIMESTAMPTZ,
        bitvavo_market TEXT
    );
    """)
    # add columns if missing
    cur.execute("ALTER TABLE public.pending_approvals ADD COLUMN IF NOT EXISTS raw_score INTEGER;")
    cur.execute("ALTER TABLE public.pending_approvals ADD COLUMN IF NOT EXISTS confidence INTEGER;")
    cur.execute("ALTER TABLE public.pending_approvals ADD COLUMN IF NOT EXISTS chance INTEGER;")
    cur.execute("ALTER TABLE public.pending_approvals ADD COLUMN IF NOT EXISTS timeframe TEXT;")
    cur.execute("ALTER TABLE public.pending_approvals ADD COLUMN IF NOT EXISTS bitvavo_market TEXT;")
    cur.execute("ALTER TABLE public.pending_approvals ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;")
    cur.execute("ALTER TABLE public.pending_approvals ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();")
    cur.execute("ALTER TABLE public.pending_approvals ADD COLUMN IF NOT EXISTS status TEXT;")

    # trade_fingerprints
    cur.execute("""
    CREATE TABLE IF NOT EXISTS public.trade_fingerprints (
        fingerprint TEXT UNIQUE,
        last_created_at TIMESTAMPTZ
    );
    """)
    cur.execute("ALTER TABLE public.trade_fingerprints ADD COLUMN IF NOT EXISTS fingerprint TEXT;")
    cur.execute("ALTER TABLE public.trade_fingerprints ADD COLUMN IF NOT EXISTS last_created_at TIMESTAMPTZ;")

    # prebuy_state
    cur.execute("""
    CREATE TABLE IF NOT EXISTS public.prebuy_state (
        day TEXT PRIMARY KEY,
        created_count INTEGER DEFAULT 0,
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """)
    cur.execute("ALTER TABLE public.prebuy_state ADD COLUMN IF NOT EXISTS created_count INTEGER DEFAULT 0;")
    cur.execute("ALTER TABLE public.prebuy_state ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();")


def utc_day_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_created_today(cur) -> int:
    day = utc_day_str()
    cur.execute("SELECT created_count FROM public.prebuy_state WHERE day=%s", (day,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO public.prebuy_state(day, created_count, updated_at) VALUES(%s, %s, NOW())", (day, 0))
        return 0
    return int(row[0] or 0)


def inc_created_today(cur, n: int = 1):
    day = utc_day_str()
    cur.execute("""
    INSERT INTO public.prebuy_state(day, created_count, updated_at)
    VALUES(%s, %s, NOW())
    ON CONFLICT(day) DO UPDATE SET created_count = public.prebuy_state.created_count + EXCLUDED.created_count,
                                 updated_at = NOW()
    """, (day, n))


def count_active_prebuys(cur) -> int:
    cur.execute("""
    SELECT COUNT(*) FROM public.pending_approvals
    WHERE COALESCE(status,'PENDING') IN ('PENDING','APPROVED')
      AND (expires_at IS NULL OR expires_at > NOW())
    """)
    return int(cur.fetchone()[0] or 0)


def fingerprint_for(pre: Prebuy) -> str:
    # fingerprint must be stable and unique for "same trade"
    payload = f"{pre.symbol}|{pre.timeframe}|{pre.setup_type}|{pre.entry:.8f}|{pre.target:.8f}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_duplicate(cur, fp: str) -> bool:
    cur.execute("SELECT 1 FROM public.trade_fingerprints WHERE fingerprint=%s", (fp,))
    return cur.fetchone() is not None


def remember_fingerprint(cur, fp: str):
    cur.execute("""
    INSERT INTO public.trade_fingerprints(fingerprint, last_created_at)
    VALUES(%s, NOW())
    ON CONFLICT (fingerprint) DO UPDATE SET last_created_at = NOW()
    """, (fp,))


def insert_pending(cur, pre: Prebuy):
    cur.execute("""
    INSERT INTO public.pending_approvals(
        id, symbol, timeframe, setup_type, regime, score, raw_score, chance, confidence,
        entry, stop, target, status, created_at, expires_at, bitvavo_market
    )
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING', NOW(), %s, %s)
    ON CONFLICT (id) DO NOTHING
    """, (
        pre.prebuy_id, pre.symbol, pre.timeframe, pre.setup_type, pre.regime,
        pre.score, pre.raw_score, pre.chance, pre.confidence,
        pre.entry, pre.stop, pre.target,
        pre.expires_at, pre.bitvavo_market
    ))


# =========================
# Binance Data
# =========================
def binance_get(url_path: str, params: dict) -> Any:
    url = f"{BINANCE_BASE_URL}{url_path}"
    r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_exchange_symbols_usdt(limit: int) -> List[str]:
    info = binance_get("/api/v3/exchangeInfo", {})
    syms = []
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        if s.get("quoteAsset") != "USDT":
            continue
        sym = s.get("symbol")
        if sym and sym.endswith("USDT"):
            syms.append(sym)
        if len(syms) >= limit:
            break
    return syms


def get_klines(symbol: str, interval: str, limit: int = 200) -> List[List[Any]]:
    return binance_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})


def closes_from_klines(klines: List[List[Any]]) -> List[float]:
    # close = index 4
    return [float(k[4]) for k in klines]


# =========================
# Indicators (simple)
# =========================
def sma(values: List[float], period: int) -> float:
    if len(values) < period:
        return float("nan")
    return sum(values[-period:]) / period


def rsi(values: List[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return float("nan")
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
    return 100.0 - (100.0 / (1.0 + rs))


def slope(values: List[float], period: int = 10) -> float:
    if len(values) < period:
        return 0.0
    # simple slope: last - first
    return values[-1] - values[-period]


def detect_regime(closes: List[float]) -> str:
    s50 = sma(closes, 50)
    s50_prev = sma(closes[:-10], 50) if len(closes) > 60 else s50
    if math.isnan(s50) or math.isnan(s50_prev):
        return "RANGE"
    if s50 > s50_prev and closes[-1] > s50:
        return "BULL"
    if s50 < s50_prev and closes[-1] < s50:
        return "BEAR"
    return "RANGE"


def choose_setup(closes: List[float]) -> str:
    # simpele keuze: als prijs boven SMA50 en pullback naar SMA20 => trend_pullback
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    if math.isnan(s20) or math.isnan(s50):
        return "TREND_PULLBACK"
    px = closes[-1]
    if px > s50 and abs(px - s20) / px < 0.02:
        return "TREND_PULLBACK"
    return "BREAKOUT_RETEST"


def score_trade(closes: List[float]) -> Tuple[int, int, int, int]:
    """
    returns (raw_score, norm_score, chance, confidence)
    """
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    r = rsi(closes, 14)
    sl = slope(closes, 10)

    if any(map(math.isnan, [s20, s50, r])):
        return 0, 0, 0, 0

    px = closes[-1]
    raw = 50

    # trend bias
    if s20 > s50:
        raw += 15
    else:
        raw -= 10

    # momentum / RSI
    if 45 <= r <= 65:
        raw += 10
    elif r > 75:
        raw -= 10
    elif r < 30:
        raw -= 5

    # slope
    if sl > 0:
        raw += 10
    else:
        raw -= 5

    # price above s20
    if px > s20:
        raw += 5
    else:
        raw -= 5

    raw = max(0, min(100, raw))
    norm = raw  # hier houden we het gelijk (jij hebt later scoreboard-normalisatie)

    chance = max(0, min(100, int(norm * 1.05)))  # kleine boost
    confidence = max(0, min(100, int((norm + chance) / 2)))

    return raw, norm, chance, confidence


def build_levels(entry: float) -> Tuple[float, float]:
    # default stop 2% onder entry, target 2R
    stop = entry * 0.98
    risk = entry - stop
    target = entry + (2.0 * risk)
    return stop, target


def make_prebuy(symbol: str, tf: str, setup: str, regime: str, raw: int, norm: int, chance: int, conf: int, entry: float) -> Prebuy:
    stop, target = build_levels(entry)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=PREBUY_VALID_SECONDS)
    uid = hashlib.md5(f"{symbol}-{time.time()}".encode("utf-8")).hexdigest()[:6]
    prebuy_id = f"PB-{symbol}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uid}"

    label = "GO" if norm >= MIN_SCORE_TO_GO else "WATCH"

    bitvavo_market = None
    if symbol_usdt_to_bitvavo_market:
        bitvavo_market = symbol_usdt_to_bitvavo_market(symbol)

    return Prebuy(
        prebuy_id=prebuy_id,
        symbol=symbol,
        timeframe=tf,
        setup_type=setup,
        regime=regime,
        score=norm,
        raw_score=raw,
        chance=chance,
        confidence=conf,
        entry=entry,
        stop=stop,
        target=target,
        expires_at=expires_at,
        bitvavo_market=bitvavo_market,
        label=label,
    )


# =========================
# WhatsApp push (optional)
# =========================
def twilio_enabled() -> bool:
    return all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO])


def log_twilio_status():
    if twilio_enabled():
        print("📲 WhatsApp push: ON")
    else:
        print("📲 WhatsApp push overgeslagen (Twilio env vars ontbreken).")


# =========================
# Bitvavo filter
# =========================
def filter_universe_by_bitvavo(universe: List[str]) -> List[str]:
    if not BITVAVO_FILTER_UNIVERSE:
        return universe
    if not get_tradable_markets_cached or not symbol_usdt_to_bitvavo_market:
        print("⚠️ Bitvavo filter: niet beschikbaar (import live_trader faalde). Universe blijft Binance-only.")
        return universe

    info = get_tradable_markets_cached()
    # haal echte set opnieuw (sample is niet genoeg) -> we gebruiken cached in live_trader
    from trading.live_trader import _get_tradable_markets  # type: ignore
    tradable = _get_tradable_markets()

    out = []
    for sym in universe:
        m = symbol_usdt_to_bitvavo_market(sym)
        if m in tradable:
            out.append(sym)

    print(f"✅ Bitvavo filter: {len(out)}/{len(universe)} symbols blijven over (tradable op Bitvavo).")
    return out


# =========================
# MAIN
# =========================
def main():
    start = time.time()

    # DB
    with db_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            ensure_schema(cur)

            created_today = get_created_today(cur)
            day = utc_day_str()
            print(f"multi_coin_score | day={day} | created_today={created_today}/{MAX_PREBUY_PER_DAY}")

            # active limit
            active_now = count_active_prebuys(cur)
            if active_now >= MAX_ACTIVE_PREBUYS:
                print(f"⛔ MAX_ACTIVE_PREBUYS bereikt ({active_now}/{MAX_ACTIVE_PREBUYS}). Geen nieuwe Pre-BUYs.")
                print(f"DONE created=0 skipped={UNIVERSE_LIMIT} seconds={time.time()-start:.1f}")
                return

            log_twilio_status()

            # Universe
            universe = get_exchange_symbols_usdt(UNIVERSE_LIMIT)
            print(f"universe={len(universe)} (limit={UNIVERSE_LIMIT}) tf_main={TF_MAIN} tf_ctx={TF_CTX}")

            universe = filter_universe_by_bitvavo(universe)

            created = 0
            skipped = 0
            candidates = 0

            for sym in universe:
                # daily cap
                created_today = get_created_today(cur)
                if created_today >= MAX_PREBUY_PER_DAY:
                    print(f"⛔ Daily cap bereikt ({created_today}/{MAX_PREBUY_PER_DAY}). Stop.")
                    break

                try:
                    kl = get_klines(sym, TF_MAIN, 200)
                    closes = closes_from_klines(kl)
                    if len(closes) < 60:
                        skipped += 1
                        continue

                    raw, norm, chance, conf = score_trade(closes)
                    if norm < WATCH_MIN_SCORE:
                        skipped += 1
                        continue

                    candidates += 1
                    regime = detect_regime(closes)
                    setup = choose_setup(closes)
                    entry = closes[-1]

                    pre = make_prebuy(sym, TF_MAIN, setup, regime, raw, norm, chance, conf, entry)

                    # label filter
                    if pre.label == "WATCH" and not INCLUDE_WATCH_IN_PENDING:
                        # je ziet 'm nog wel in logs (ervaring), maar niet in pending
                        print(f"🟡 {sym}: WATCH score={pre.score} chance={pre.chance} regime={pre.regime} (niet in pending)")
                        skipped += 1
                        continue

                    # fingerprint dedup
                    fp = fingerprint_for(pre)
                    if is_duplicate(cur, fp):
                        skipped += 1
                        continue

                    # insert pending + remember fp
                    insert_pending(cur, pre)
                    remember_fingerprint(cur, fp)
                    inc_created_today(cur, 1)

                    created += 1
                    print(f"🟢 {sym}: {pre.label} raw={pre.raw_score} norm={pre.score} chance={pre.chance} regime={pre.regime} id={pre.prebuy_id}")

                except Exception as e:
                    skipped += 1
                    # nooit crashen op 1 coin
                    print(f"⚠️ {sym} skip: {type(e).__name__}: {e}")

            dur = time.time() - start
            print(f"DONE created={created} candidates={candidates} scanned={len(universe)} seconds={dur:.1f}")


if __name__ == "__main__":
    main()
