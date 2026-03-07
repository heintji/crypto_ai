# analysis/multi_coin_score.py
from __future__ import annotations

import os
import sys
import time
import math
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
import psycopg2


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

# shadow trades
try:
    from trading.shadow_trades import create_shadow
except Exception:
    create_shadow = None


# =========================
# ENV
# =========================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

BINANCE_BASE_URL = (os.getenv("BINANCE_BASE_URL") or "https://api.binance.com").rstrip("/")
UNIVERSE_LIMIT = int(os.getenv("UNIVERSE_LIMIT") or "250")
TF_MAIN = (os.getenv("TF_MAIN") or "4h").strip()
TF_CTX = (os.getenv("TF_CTX") or "1h").strip()

# BELANGRIJK:
# MAX_PREBUY_PER_DAY = vanaf nu ALLEEN auto-push limiet per dag
# dus NIET meer limiet op pending_approvals / LIST / TOP / BUY
MAX_PREBUY_PER_DAY = int(os.getenv("MAX_PREBUY_PER_DAY") or "50")

# Deze laten we bestaan voor compatibiliteit, maar blokkeren pending niet meer.
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
    symbol: str
    timeframe: str
    setup_type: str
    regime: str
    score: int
    raw_score: int
    chance: int
    confidence: int
    entry: float
    stop: float
    target: float
    expires_at: datetime
    bitvavo_market: Optional[str] = None
    label: str = "WATCH"
    why_tag: str = ""
    market_condition: str = "NORMAAL"
    overextended_pct: float = 0.0


# =========================
# DB
# =========================
def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def ensure_schema(cur):
    # pending_approvals: alles wat jij met LIST/TOP moet kunnen zien
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
    cur.execute("ALTER TABLE public.pending_approvals ADD COLUMN IF NOT EXISTS raw_score INTEGER;")
    cur.execute("ALTER TABLE public.pending_approvals ADD COLUMN IF NOT EXISTS confidence INTEGER;")
    cur.execute("ALTER TABLE public.pending_approvals ADD COLUMN IF NOT EXISTS chance INTEGER;")
    cur.execute("ALTER TABLE public.pending_approvals ADD COLUMN IF NOT EXISTS timeframe TEXT;")
    cur.execute("ALTER TABLE public.pending_approvals ADD COLUMN IF NOT EXISTS bitvavo_market TEXT;")
    cur.execute("ALTER TABLE public.pending_approvals ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;")
    cur.execute("ALTER TABLE public.pending_approvals ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();")
    cur.execute("ALTER TABLE public.pending_approvals ADD COLUMN IF NOT EXISTS status TEXT;")

    # trade_fingerprints: deduplicatie van dezelfde trade
    cur.execute("""
    CREATE TABLE IF NOT EXISTS public.trade_fingerprints (
        fingerprint TEXT UNIQUE,
        last_created_at TIMESTAMPTZ
    );
    """)
    cur.execute("ALTER TABLE public.trade_fingerprints ADD COLUMN IF NOT EXISTS fingerprint TEXT;")
    cur.execute("ALTER TABLE public.trade_fingerprints ADD COLUMN IF NOT EXISTS last_created_at TIMESTAMPTZ;")

    # push_state: telt ALLEEN auto-push meldingen per dag
    cur.execute("""
    CREATE TABLE IF NOT EXISTS public.prebuy_state (
        day TEXT PRIMARY KEY,
        created_count INTEGER DEFAULT 0,
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """)
    cur.execute("ALTER TABLE public.prebuy_state ADD COLUMN IF NOT EXISTS created_count INTEGER DEFAULT 0;")
    cur.execute("ALTER TABLE public.prebuy_state ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();")

    # experience / leerlaag
    cur.execute("""
    CREATE TABLE IF NOT EXISTS public.prebuy_experience (
        prebuy_id TEXT PRIMARY KEY,
        fingerprint TEXT,
        symbol TEXT NOT NULL,
        timeframe TEXT,
        setup_type TEXT,
        regime TEXT,
        score INTEGER,
        raw_score INTEGER,
        chance INTEGER,
        confidence INTEGER,
        label TEXT,
        entry DOUBLE PRECISION,
        stop DOUBLE PRECISION,
        target DOUBLE PRECISION,
        bitvavo_market TEXT,
        why_tag TEXT,
        market_condition TEXT,
        overextended_pct DOUBLE PRECISION,
        notified BOOLEAN DEFAULT FALSE,
        notification_reason TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        expires_at TIMESTAMPTZ
    );
    """)
    cur.execute("ALTER TABLE public.prebuy_experience ADD COLUMN IF NOT EXISTS fingerprint TEXT;")
    cur.execute("ALTER TABLE public.prebuy_experience ADD COLUMN IF NOT EXISTS why_tag TEXT;")
    cur.execute("ALTER TABLE public.prebuy_experience ADD COLUMN IF NOT EXISTS market_condition TEXT;")
    cur.execute("ALTER TABLE public.prebuy_experience ADD COLUMN IF NOT EXISTS overextended_pct DOUBLE PRECISION;")
    cur.execute("ALTER TABLE public.prebuy_experience ADD COLUMN IF NOT EXISTS notified BOOLEAN DEFAULT FALSE;")
    cur.execute("ALTER TABLE public.prebuy_experience ADD COLUMN IF NOT EXISTS notification_reason TEXT;")
    cur.execute("ALTER TABLE public.prebuy_experience ADD COLUMN IF NOT EXISTS label TEXT;")
    cur.execute("ALTER TABLE public.prebuy_experience ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();")
    cur.execute("ALTER TABLE public.prebuy_experience ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;")

    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_prebuy_experience_symbol_created_at
    ON public.prebuy_experience(symbol, created_at DESC);
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_prebuy_experience_fingerprint
    ON public.prebuy_experience(fingerprint);
    """)


def utc_day_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_pushes_today(cur) -> int:
    day = utc_day_str()
    cur.execute("SELECT created_count FROM public.prebuy_state WHERE day=%s", (day,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO public.prebuy_state(day, created_count, updated_at) VALUES(%s, %s, NOW())",
            (day, 0),
        )
        return 0
    return int(row[0] or 0)


def inc_pushes_today(cur, n: int = 1):
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
        pre.prebuy_id,
        pre.symbol,
        pre.timeframe,
        pre.setup_type,
        pre.regime,
        pre.score,
        pre.raw_score,
        pre.chance,
        pre.confidence,
        pre.entry,
        pre.stop,
        pre.target,
        pre.expires_at,
        pre.bitvavo_market,
    ))


def insert_experience(cur, pre: Prebuy, fp: str, notified: bool, notification_reason: str):
    cur.execute("""
    INSERT INTO public.prebuy_experience(
        prebuy_id, fingerprint, symbol, timeframe, setup_type, regime, score, raw_score,
        chance, confidence, label, entry, stop, target, bitvavo_market, why_tag,
        market_condition, overextended_pct, notified, notification_reason, created_at, expires_at
    )
    VALUES (
        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s
    )
    ON CONFLICT (prebuy_id) DO NOTHING
    """, (
        pre.prebuy_id,
        fp,
        pre.symbol,
        pre.timeframe,
        pre.setup_type,
        pre.regime,
        pre.score,
        pre.raw_score,
        pre.chance,
        pre.confidence,
        pre.label,
        pre.entry,
        pre.stop,
        pre.target,
        pre.bitvavo_market,
        pre.why_tag,
        pre.market_condition,
        pre.overextended_pct,
        notified,
        notification_reason,
        pre.expires_at,
    ))


# =========================
# Binance Data
# =========================
def binance_get(url_path: str, params: dict) -> Any:
    url = f"{BINANCE_BASE_URL}{url_path}"
    response = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_exchange_symbols_usdt(limit: int) -> List[str]:
    info = binance_get("/api/v3/exchangeInfo", {})
    symbols: List[str] = []

    for symbol_info in info.get("symbols", []):
        if symbol_info.get("status") != "TRADING":
            continue
        if symbol_info.get("quoteAsset") != "USDT":
            continue

        symbol = symbol_info.get("symbol")
        if symbol and symbol.endswith("USDT"):
            symbols.append(symbol)

        if len(symbols) >= limit:
            break

    return symbols


def get_klines(symbol: str, interval: str, limit: int = 200) -> List[List[Any]]:
    return binance_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})


def closes_from_klines(klines: List[List[Any]]) -> List[float]:
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
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)

    if math.isnan(s20) or math.isnan(s50):
        return "TREND_PULLBACK"

    px = closes[-1]
    if px > s50 and abs(px - s20) / px < 0.02:
        return "TREND_PULLBACK"

    return "BREAKOUT_RETEST"


def detect_market_condition(closes: List[float], period: int = 20) -> str:
    if len(closes) < period + 1:
        return "NORMAAL"

    returns: List[float] = []
    recent = closes[-(period + 1):]

    for i in range(1, len(recent)):
        prev_close = recent[i - 1]
        curr_close = recent[i]
        if prev_close == 0:
            continue
        returns.append(abs((curr_close - prev_close) / prev_close))

    if not returns:
        return "NORMAAL"

    avg_move = sum(returns) / len(returns)

    if avg_move < 0.008:
        return "RUSTIG"
    if avg_move < 0.02:
        return "NORMAAL"
    return "VOLATIEL"


def calc_overextended_pct(closes: List[float]) -> float:
    s20 = sma(closes, 20)
    px = closes[-1]

    if math.isnan(s20) or s20 == 0:
        return 0.0

    return round(((px - s20) / s20) * 100.0, 2)


def build_why_tag(closes: List[float], setup: str, regime: str) -> str:
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    r = rsi(closes, 14)
    px = closes[-1]

    parts: List[str] = [setup, regime]

    if not math.isnan(s20) and not math.isnan(s50):
        if s20 > s50:
            parts.append("trend_up")
        elif s20 < s50:
            parts.append("trend_down")
        else:
            parts.append("trend_flat")

    if not math.isnan(r):
        if r > 75:
            parts.append("rsi_hot")
        elif r < 30:
            parts.append("rsi_oversold")
        elif 45 <= r <= 65:
            parts.append("rsi_healthy")

    if not math.isnan(s20):
        if px >= s20:
            parts.append("above_sma20")
        else:
            parts.append("below_sma20")

    return "|".join(parts[:5])


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

    if s20 > s50:
        raw += 15
    else:
        raw -= 10

    if 45 <= r <= 65:
        raw += 10
    elif r > 75:
        raw -= 10
    elif r < 30:
        raw -= 5

    if sl > 0:
        raw += 10
    else:
        raw -= 5

    if px > s20:
        raw += 5
    else:
        raw -= 5

    raw = max(0, min(100, raw))
    norm = raw

    chance = max(0, min(100, int(norm * 1.05)))
    confidence = max(0, min(100, int((norm + chance) / 2)))

    return raw, norm, chance, confidence


def build_levels(entry: float) -> Tuple[float, float]:
    stop = entry * 0.98
    risk = entry - stop
    target = entry + (2.0 * risk)
    return stop, target


def make_prebuy(
    symbol: str,
    tf: str,
    setup: str,
    regime: str,
    raw: int,
    norm: int,
    chance: int,
    conf: int,
    entry: float,
    why_tag: str,
    market_condition: str,
    overextended_pct: float,
) -> Prebuy:
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
        why_tag=why_tag,
        market_condition=market_condition,
        overextended_pct=overextended_pct,
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


def can_auto_push(cur) -> bool:
    pushes_today = get_pushes_today(cur)
    return pushes_today < MAX_PREBUY_PER_DAY


# =========================
# Bitvavo filter
# =========================
def filter_universe_by_bitvavo(universe: List[str]) -> List[str]:
    if not BITVAVO_FILTER_UNIVERSE:
        return universe

    if not get_tradable_markets_cached or not symbol_usdt_to_bitvavo_market:
        print("⚠️ Bitvavo filter: niet beschikbaar (import live_trader faalde). Universe blijft Binance-only.")
        return universe

    get_tradable_markets_cached()

    from trading.live_trader import _get_tradable_markets  # type: ignore

    tradable = _get_tradable_markets()
    filtered: List[str] = []

    for sym in universe:
        market = symbol_usdt_to_bitvavo_market(sym)
        if market in tradable:
            filtered.append(sym)

    print(f"✅ Bitvavo filter: {len(filtered)}/{len(universe)} symbols blijven over (tradable op Bitvavo).")
    return filtered


# =========================
# SHADOW HELPER
# =========================
def create_shadow_safe(pre: Prebuy) -> Tuple[bool, str]:
    """
    Maakt een shadow trade aan voor deze unieke setup.
    Faalt shadow aanmaak, dan mag de scan niet crashen.
    """
    if create_shadow is None:
        return False, "SHADOW_MODULE_NOT_AVAILABLE"

    try:
        result = create_shadow(
            prebuy_id=pre.prebuy_id,
            symbol=pre.symbol,
            entry=pre.entry,
            stop=pre.stop,
            target=pre.target,
            timeframe=pre.timeframe,
            setup_type=pre.setup_type,
            regime=pre.regime,
            label=pre.label,
            score=pre.score,
            raw_score=pre.raw_score,
            chance=pre.chance,
            confidence=pre.confidence,
            exchange="BINANCE",
        )

        if isinstance(result, dict) and result.get("ok"):
            return True, safe_str(result.get("storage"), "OK")

        return False, "SHADOW_CREATE_RETURNED_NOT_OK"

    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# =========================
# MAIN
# =========================
def main():
    start = time.time()

    with db_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            ensure_schema(cur)

            push_count_today = get_pushes_today(cur)
            day = utc_day_str()
            print(f"multi_coin_score | day={day} | auto_push_today={push_count_today}/{MAX_PREBUY_PER_DAY}")

            active_now = count_active_prebuys(cur)
            print(f"pending_active_now={active_now} | MAX_ACTIVE_PREBUYS={MAX_ACTIVE_PREBUYS} (alleen info, blokkeert niets)")

            log_twilio_status()

            universe = get_exchange_symbols_usdt(UNIVERSE_LIMIT)
            print(f"universe={len(universe)} (limit={UNIVERSE_LIMIT}) tf_main={TF_MAIN} tf_ctx={TF_CTX}")

            universe = filter_universe_by_bitvavo(universe)

            created_pending = 0
            pushed_count = 0
            skipped = 0
            candidates = 0
            experienced = 0
            shadow_created = 0
            shadow_failed = 0

            for sym in universe:
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

                    why_tag = build_why_tag(closes, setup, regime)
                    market_condition = detect_market_condition(closes)
                    overextended_pct = calc_overextended_pct(closes)

                    pre = make_prebuy(
                        symbol=sym,
                        tf=TF_MAIN,
                        setup=setup,
                        regime=regime,
                        raw=raw,
                        norm=norm,
                        chance=chance,
                        conf=conf,
                        entry=entry,
                        why_tag=why_tag,
                        market_condition=market_condition,
                        overextended_pct=overextended_pct,
                    )

                    fp = fingerprint_for(pre)

                    # standaard: alleen leren
                    notified = False
                    notification_reason = "LEARNING_ONLY"

                    # 1) eerst dedup check
                    if is_duplicate(cur, fp):
                        notification_reason = "DUPLICATE_BLOCKED"
                        insert_experience(cur, pre, fp, notified, notification_reason)
                        experienced += 1
                        print(
                            f"📘 {sym}: duplicate geblokkeerd | label={pre.label} "
                            f"score={pre.score} chance={pre.chance} reason={notification_reason}"
                        )
                        continue

                    # 2) fingerprint onthouden zodat exact dezelfde trade niet opnieuw binnenkomt
                    remember_fingerprint(cur, fp)

                    # 3) shadow trade aanmaken voor iedere unieke setup
                    shadow_ok, shadow_reason = create_shadow_safe(pre)
                    if shadow_ok:
                        shadow_created += 1
                    else:
                        shadow_failed += 1
                        print(f"⚠️ {sym}: shadow create failed | reason={shadow_reason}")

                    # 4) altijd experience opslaan
                    should_insert_pending = True

                    # WATCH niet in pending tenzij jij dit expliciet aanzet
                    if pre.label == "WATCH" and not INCLUDE_WATCH_IN_PENDING:
                        should_insert_pending = False
                        notification_reason = "WATCH_ONLY_NOT_PENDING"

                    # 5) pending maken = zichtbaar in LIST/TOP en dus BUY mogelijk
                    if should_insert_pending:
                        insert_pending(cur, pre)
                        created_pending += 1

                        # 6) auto-push limiet geldt ALLEEN nog voor melding, niet voor pending
                        if can_auto_push(cur):
                            inc_pushes_today(cur, 1)
                            pushed_count += 1
                            notified = True
                            notification_reason = "AUTO_PUSH_ALLOWED"
                        else:
                            notification_reason = "AUTO_PUSH_LIMIT_REACHED_PENDING_KEPT"

                        print(
                            f"🟢 {sym}: pending aangemaakt | label={pre.label} raw={pre.raw_score} "
                            f"norm={pre.score} chance={pre.chance} regime={pre.regime} "
                            f"why={pre.why_tag} push={notification_reason} shadow={shadow_reason} id={pre.prebuy_id}"
                        )
                    else:
                        print(
                            f"📘 {sym}: alleen leren | label={pre.label} score={pre.score} "
                            f"chance={pre.chance} regime={pre.regime} why={pre.why_tag} "
                            f"reason={notification_reason} shadow={shadow_reason}"
                        )

                    insert_experience(cur, pre, fp, notified, notification_reason)
                    experienced += 1

                except Exception as e:
                    skipped += 1
                    print(f"⚠️ {sym} skip: {type(e).__name__}: {e}")

            dur = time.time() - start
            print(
                f"DONE pending_created={created_pending} auto_push_sent={pushed_count} "
                f"experienced={experienced} shadow_created={shadow_created} shadow_failed={shadow_failed} "
                f"candidates={candidates} scanned={len(universe)} seconds={dur:.1f}"
            )


if __name__ == "__main__":
    main()
