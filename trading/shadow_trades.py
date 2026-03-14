from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras


# ==========================================================
# CONFIG
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

# Belangrijk:
# standaard naar /data i.p.v. "data"
# zodat alle Render services dezelfde persistente disk gebruiken
DATA_DIR = (os.getenv("DATA_DIR") or "/data").strip()
SHADOW_PATH = os.path.join(DATA_DIR, "shadow_trades.json")


# ==========================================================
# HELPERS
# ==========================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_ts() -> int:
    return int(time.time())


def dt_to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    try:
        return str(value)
    except Exception:
        return None


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def safe_str(x: Any, default: str = "") -> str:
    if x is None:
        return default
    try:
        s = str(x).strip()
        return s if s else default
    except Exception:
        return default


def db_ready() -> bool:
    return bool(DATABASE_URL)


def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def ensure_file() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.isfile(SHADOW_PATH):
        with open(SHADOW_PATH, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)


def load_shadows_file() -> List[Dict[str, Any]]:
    ensure_file()
    try:
        with open(SHADOW_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_shadows_file(shadows: List[Dict[str, Any]]) -> None:
    ensure_file()
    with open(SHADOW_PATH, "w", encoding="utf-8") as f:
        json.dump(shadows, f, indent=2, ensure_ascii=False)


# alias zodat trade_monitor.py gewoon load_shadows kan importeren
def load_shadows() -> List[Dict[str, Any]]:
    return load_shadows_file()


def calc_r_result(entry: float, stop: float, exit_price: float) -> float:
    risk = entry - stop
    if risk <= 0:
        return 0.0
    return (exit_price - entry) / risk


def calc_pnl_pct(entry: float, exit_price: float) -> float:
    if entry <= 0:
        return 0.0
    return ((exit_price - entry) / entry) * 100.0


def determine_shadow_outcome(entry: float, exit_price: float) -> str:
    """
    Definitieve regel voor shadow trades:
    - exit_price > entry  => WIN
    - exit_price <= entry => LOSS
    """
    entry = safe_float(entry, 0.0)
    exit_price = safe_float(exit_price, 0.0)

    if entry <= 0:
        return "LOSS"

    return "WIN" if exit_price > entry else "LOSS"


def infer_outcome(result_r: float, entry: float, exit_price: float) -> str:
    """
    Backward compatible helper.
    Geen FLAT/UNKNOWN meer als einduitkomst.
    """
    return determine_shadow_outcome(entry, exit_price)


def normalize_shadow_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Zorgt dat dashboard-consumptie netjes en consistent is.
    """
    out = dict(row)

    out["id"] = safe_str(out.get("id") or out.get("trade_key"))
    out["trade_key"] = safe_str(out.get("trade_key"), out["id"])
    out["exchange"] = safe_str(out.get("exchange"), "BINANCE")
    out["symbol"] = safe_str(out.get("symbol") or out.get("coin"))
    out["timeframe"] = safe_str(out.get("timeframe") or out.get("entry_timeframe"), "4h")
    out["setup_type"] = safe_str(out.get("setup_type"), "UNKNOWN")
    out["regime"] = safe_str(out.get("regime") or out.get("market_regime"), "UNKNOWN")
    out["label"] = safe_str(out.get("label") or out.get("grade"), "WATCH")
    out["status"] = safe_str(out.get("status"), "OPEN").upper()

    out["score"] = safe_float(out.get("score"))
    out["raw_score"] = safe_float(out.get("raw_score"))
    out["chance"] = safe_float(out.get("chance"))
    out["confidence"] = safe_float(out.get("confidence"))

    out["entry"] = safe_float(out.get("entry"))
    out["stop"] = safe_float(out.get("stop"))
    out["target"] = safe_float(out.get("target"))
    out["exit_price"] = safe_float(out.get("exit_price"), 0.0) if out.get("exit_price") is not None else None

    out["result_r"] = safe_float(out.get("result_r"), 0.0) if out.get("result_r") is not None else None
    out["pnl_pct"] = safe_float(out.get("pnl_pct"), 0.0) if out.get("pnl_pct") is not None else None

    raw_outcome = safe_str(out.get("outcome"), "OPEN").upper()
    if out["status"] == "OPEN":
        out["outcome"] = "OPEN"
    else:
        if raw_outcome in ("WIN", "LOSS"):
            out["outcome"] = raw_outcome
        elif out["exit_price"] is not None:
            out["outcome"] = determine_shadow_outcome(out["entry"], safe_float(out["exit_price"]))
        else:
            out["outcome"] = "LOSS"

    out["is_shadow"] = True

    out["created_at"] = dt_to_iso(out.get("created_at") or out.get("timestamp") or out.get("entry_time"))
    out["closed_at"] = dt_to_iso(out.get("closed_at") or out.get("exit_time"))

    return out


# ==========================================================
# DB SCHEMA
# ==========================================================
def ensure_schema() -> None:
    """
    Houdt experience_trades compatible met:
    - history_simulator
    - shadow trades
    - dashboard historie
    """
    if not db_ready():
        return

    with db_connect() as conn:
        with conn.cursor() as cur:
            # Basistabel als hij nog niet bestaat
            cur.execute("""
                CREATE TABLE IF NOT EXISTS public.experience_trades (
                    trade_key TEXT PRIMARY KEY
                );
            """)

            # Compat-kolommen voor bestaande simulator-structuur
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS id TEXT;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS source TEXT;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS timestamp TIMESTAMPTZ;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS entry_time TIMESTAMPTZ;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS exit_time TIMESTAMPTZ;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS coin TEXT;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS entry_timeframe TEXT;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS regime_timeframe TEXT;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS market_regime TEXT;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS grade TEXT;")

            # Shadow/dashboard kolommen
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS exchange TEXT;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS symbol TEXT;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS timeframe TEXT;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS setup_type TEXT;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS regime TEXT;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS label TEXT;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS score DOUBLE PRECISION;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS raw_score DOUBLE PRECISION;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS chance DOUBLE PRECISION;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS entry DOUBLE PRECISION;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS stop DOUBLE PRECISION;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS target DOUBLE PRECISION;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS exit_price DOUBLE PRECISION;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS pnl_pct DOUBLE PRECISION;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS result_r DOUBLE PRECISION;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS outcome TEXT;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'OPEN';")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS is_shadow BOOLEAN DEFAULT FALSE;")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();")
            cur.execute("ALTER TABLE public.experience_trades ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;")

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_experience_trades_shadow_status
                ON public.experience_trades(is_shadow, status);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_experience_trades_shadow_symbol
                ON public.experience_trades(symbol, is_shadow, status);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_experience_trades_shadow_created
                ON public.experience_trades(created_at DESC);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_experience_trades_shadow_symbol_created
                ON public.experience_trades(symbol, created_at DESC);
            """)
        conn.commit()


# ==========================================================
# CREATE SHADOW TRADE
# ==========================================================
def create_shadow(
    prebuy_id: str,
    symbol: str,
    entry: float,
    stop: float,
    target: float,
    *,
    timeframe: str = "4h",
    setup_type: str = "UNKNOWN",
    regime: str = "UNKNOWN",
    label: str = "WATCH",
    score: float = 0.0,
    raw_score: float = 0.0,
    chance: float = 0.0,
    confidence: float = 0.0,
    exchange: str = "BINANCE",
) -> Dict[str, Any]:
    """
    Maakt een shadow trade aan.
    Hoofdroute = Postgres (experience_trades, is_shadow=true).
    Fallback = JSON file.
    """

    trade_key = f"SH-{prebuy_id}"
    shadow_id = trade_key
    created_at = now_utc()

    payload = {
        "trade_key": trade_key,
        "id": shadow_id,
        "source": "SHADOW",
        "exchange": exchange,
        "symbol": symbol,
        "coin": symbol,
        "timeframe": timeframe,
        "entry_timeframe": timeframe,
        "setup_type": setup_type,
        "regime": regime,
        "market_regime": regime,
        "label": label,
        "grade": label,
        "score": safe_float(score),
        "raw_score": safe_float(raw_score),
        "chance": safe_float(chance),
        "confidence": safe_float(confidence),
        "entry": safe_float(entry),
        "stop": safe_float(stop),
        "target": safe_float(target),
        "exit_price": None,
        "pnl_pct": None,
        "result_r": None,
        "outcome": "OPEN",
        "status": "OPEN",
        "is_shadow": True,
        "timestamp": created_at,
        "entry_time": created_at,
        "created_at": created_at,
        "closed_at": None,
        "exit_time": None,
    }

    if db_ready():
        ensure_schema()
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO public.experience_trades(
                        trade_key,
                        id,
                        source,
                        exchange,
                        symbol,
                        coin,
                        timeframe,
                        entry_timeframe,
                        setup_type,
                        regime,
                        market_regime,
                        label,
                        grade,
                        score,
                        raw_score,
                        chance,
                        confidence,
                        entry,
                        stop,
                        target,
                        exit_price,
                        pnl_pct,
                        result_r,
                        outcome,
                        status,
                        is_shadow,
                        timestamp,
                        entry_time,
                        created_at,
                        closed_at,
                        exit_time
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (trade_key) DO NOTHING
                """, (
                    payload["trade_key"],
                    payload["id"],
                    payload["source"],
                    payload["exchange"],
                    payload["symbol"],
                    payload["coin"],
                    payload["timeframe"],
                    payload["entry_timeframe"],
                    payload["setup_type"],
                    payload["regime"],
                    payload["market_regime"],
                    payload["label"],
                    payload["grade"],
                    payload["score"],
                    payload["raw_score"],
                    payload["chance"],
                    payload["confidence"],
                    payload["entry"],
                    payload["stop"],
                    payload["target"],
                    payload["exit_price"],
                    payload["pnl_pct"],
                    payload["result_r"],
                    payload["outcome"],
                    payload["status"],
                    payload["is_shadow"],
                    payload["timestamp"],
                    payload["entry_time"],
                    payload["created_at"],
                    payload["closed_at"],
                    payload["exit_time"],
                ))
            conn.commit()
        return {"ok": True, "storage": "postgres", "id": shadow_id, "trade_key": trade_key}

    # fallback file
    shadows = load_shadows_file()
    shadows.append({
        "id": shadow_id,
        "trade_key": trade_key,
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "setup_type": setup_type,
        "regime": regime,
        "label": label,
        "score": safe_float(score),
        "raw_score": safe_float(raw_score),
        "chance": safe_float(chance),
        "confidence": safe_float(confidence),
        "entry": safe_float(entry),
        "stop": safe_float(stop),
        "target": safe_float(target),
        "exit_price": None,
        "pnl_pct": None,
        "result_r": None,
        "outcome": "OPEN",
        "status": "OPEN",
        "is_shadow": True,
        "created_at": dt_to_iso(created_at),
        "closed_at": None,
        "exit_time": None,
    })
    save_shadows_file(shadows)
    return {"ok": True, "storage": "file", "id": shadow_id, "trade_key": trade_key}


# ==========================================================
# GET OPEN SHADOWS
# ==========================================================
def get_open_shadows(symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Haalt open shadow trades op.
    Open = is_shadow=true en nog niet gesloten.
    """

    if db_ready():
        ensure_schema()
        where = "WHERE is_shadow = TRUE AND COALESCE(status,'OPEN') = 'OPEN' AND closed_at IS NULL"
        params: List[Any] = []

        if symbol:
            where += " AND COALESCE(symbol, coin) = %s"
            params.append(symbol)

        sql = f"""
            SELECT
                trade_key, id, exchange, symbol, coin, timeframe, entry_timeframe,
                setup_type, regime, market_regime, label, grade,
                score, raw_score, chance, confidence,
                entry, stop, target, exit_price, pnl_pct,
                result_r, outcome, status, is_shadow, timestamp, entry_time, created_at, closed_at, exit_time
            FROM public.experience_trades
            {where}
            ORDER BY created_at ASC NULLS LAST
        """

        with db_connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()
                return [normalize_shadow_row(dict(r)) for r in rows]

    # fallback file
    out = []
    for s in load_shadows_file():
        if _is_file_shadow_open(s) is False:
            continue
        if symbol and s.get("symbol") != symbol:
            continue
        out.append(normalize_shadow_row(s))
    return out


def _is_file_shadow_open(shadow: Dict[str, Any]) -> bool:
    status = safe_str(shadow.get("status"), "OPEN").upper()
    closed_at = shadow.get("closed_at")
    return status == "OPEN" and not closed_at


# ==========================================================
# CLOSE SHADOW TRADE
# ==========================================================
def close_shadow(
    shadow_id: str,
    exit_price: float,
    *,
    outcome: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Sluit shadow trade af en berekent pnl/result_r.
    Einduitkomst mag alleen WIN of LOSS zijn.
    """

    exit_price = safe_float(exit_price)

    if db_ready():
        ensure_schema()
        with db_connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT trade_key, id, symbol, coin, entry, stop, target, result_r, outcome, is_shadow, status
                    FROM public.experience_trades
                    WHERE (id = %s OR trade_key = %s) AND is_shadow = TRUE
                    LIMIT 1
                """, (shadow_id, shadow_id))
                row = cur.fetchone()

                if not row:
                    return {"ok": False, "reason": "SHADOW_NOT_FOUND"}

                entry = safe_float(row["entry"])
                stop = safe_float(row["stop"])

                result_r = calc_r_result(entry, stop, exit_price)
                pnl_pct = calc_pnl_pct(entry, exit_price)

                forced_outcome = safe_str(outcome, "").upper()
                if forced_outcome in ("WIN", "LOSS"):
                    final_outcome = forced_outcome
                else:
                    final_outcome = determine_shadow_outcome(entry, exit_price)

                cur.execute("""
                    UPDATE public.experience_trades
                    SET exit_price = %s,
                        pnl_pct = %s,
                        result_r = %s,
                        outcome = %s,
                        status = 'CLOSED',
                        closed_at = NOW(),
                        exit_time = NOW()
                    WHERE (id = %s OR trade_key = %s) AND is_shadow = TRUE
                """, (
                    exit_price,
                    pnl_pct,
                    result_r,
                    final_outcome,
                    shadow_id,
                    shadow_id,
                ))
            conn.commit()

        return {
            "ok": True,
            "storage": "postgres",
            "id": shadow_id,
            "exit_price": exit_price,
            "pnl_pct": pnl_pct,
            "result_r": result_r,
            "outcome": final_outcome,
            "status": "CLOSED",
        }

    # fallback file
    shadows = load_shadows_file()
    found = False
    result_r = 0.0
    pnl_pct = 0.0
    final_outcome = "LOSS"

    for s in shadows:
        if s.get("id") != shadow_id and s.get("trade_key") != shadow_id:
            continue

        found = True
        entry = safe_float(s.get("entry"))
        stop = safe_float(s.get("stop"))
        result_r = calc_r_result(entry, stop, exit_price)
        pnl_pct = calc_pnl_pct(entry, exit_price)

        forced_outcome = safe_str(outcome, "").upper()
        if forced_outcome in ("WIN", "LOSS"):
            final_outcome = forced_outcome
        else:
            final_outcome = determine_shadow_outcome(entry, exit_price)

        s["status"] = "CLOSED"
        s["exit_price"] = exit_price
        s["pnl_pct"] = pnl_pct
        s["result_r"] = result_r
        s["outcome"] = final_outcome
        s["closed_at"] = dt_to_iso(now_utc())
        s["exit_time"] = dt_to_iso(now_utc())
        break

    if not found:
        return {"ok": False, "reason": "SHADOW_NOT_FOUND"}

    save_shadows_file(shadows)
    return {
        "ok": True,
        "storage": "file",
        "id": shadow_id,
        "exit_price": exit_price,
        "pnl_pct": pnl_pct,
        "result_r": result_r,
        "outcome": final_outcome,
        "status": "CLOSED",
    }


# ==========================================================
# AUTO EVALUATE OPEN SHADOWS
# ==========================================================
def evaluate_shadow_price(shadow: Dict[str, Any], current_price: float) -> Optional[Dict[str, Any]]:
    """
    Kijkt of shadow trade target of stop heeft geraakt.
    Returnt close-result als trade sluit, anders None.
    """
    stop = safe_float(shadow.get("stop"))
    target = safe_float(shadow.get("target"))
    current_price = safe_float(current_price)
    shadow_id = safe_str(shadow.get("id") or shadow.get("trade_key"))

    if not shadow_id:
        return None

    # long logica
    if target > 0 and current_price >= target:
        return close_shadow(shadow_id, target, outcome="WIN")

    if stop > 0 and current_price <= stop:
        return close_shadow(shadow_id, stop, outcome="LOSS")

    return None


def evaluate_open_shadows_for_symbol(symbol: str, current_price: float) -> List[Dict[str, Any]]:
    """
    Check alle open shadow trades van 1 coin tegen current_price.
    """
    results: List[Dict[str, Any]] = []
    opens = get_open_shadows(symbol=symbol)
    for sh in opens:
        res = evaluate_shadow_price(sh, current_price)
        if res:
            results.append(res)
    return results


# ==========================================================
# LIST / STATS
# ==========================================================
def list_recent_shadows(limit: int = 100, include_open: bool = True) -> List[Dict[str, Any]]:
    if db_ready():
        ensure_schema()
        where = "WHERE is_shadow = TRUE"
        if not include_open:
            where += " AND closed_at IS NOT NULL"

        sql = f"""
            SELECT
                trade_key, id, exchange, symbol, coin, timeframe, entry_timeframe,
                setup_type, regime, market_regime, label, grade,
                score, raw_score, chance, confidence,
                entry, stop, target, exit_price, pnl_pct,
                result_r, outcome, status, is_shadow, timestamp, entry_time, created_at, closed_at, exit_time
            FROM public.experience_trades
            {where}
            ORDER BY created_at DESC NULLS LAST
            LIMIT {safe_int(limit, 100)}
        """
        with db_connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                return [normalize_shadow_row(dict(r)) for r in cur.fetchall()]

    shadows = load_shadows_file()
    if not include_open:
        shadows = [s for s in shadows if safe_str(s.get("status")).upper() == "CLOSED"]
    return [normalize_shadow_row(s) for s in list(reversed(shadows))[:limit]]


def shadow_stats() -> Dict[str, Any]:
    rows = list_recent_shadows(limit=5000, include_open=True)

    total = len(rows)
    open_count = 0
    closed_count = 0
    wins = 0
    losses = 0
    sum_r = 0.0
    sum_pnl_pct = 0.0

    for r in rows:
        outcome = safe_str(r.get("outcome"), "").upper()
        closed_at = r.get("closed_at")

        if not closed_at or outcome == "OPEN":
            open_count += 1
            continue

        closed_count += 1
        rr = safe_float(r.get("result_r"), 0.0)
        pnl_pct = safe_float(r.get("pnl_pct"), 0.0)
        sum_r += rr
        sum_pnl_pct += pnl_pct

        if outcome == "WIN":
            wins += 1
        else:
            losses += 1

    winrate = (wins / closed_count * 100.0) if closed_count else 0.0
    avg_r = (sum_r / closed_count) if closed_count else 0.0
    avg_pnl_pct = (sum_pnl_pct / closed_count) if closed_count else 0.0

    return {
        "total": total,
        "open": open_count,
        "closed": closed_count,
        "wins": wins,
        "losses": losses,
        "winrate": round(winrate, 2),
        "avg_r": round(avg_r, 4),
        "avg_pnl_pct": round(avg_pnl_pct, 4),
        "storage_path": SHADOW_PATH,
    }


# ==========================================================
# MAIN TEST
# ==========================================================
if __name__ == "__main__":
    ensure_schema()
    print("DB ready:", db_ready())
    print("DATA_DIR:", DATA_DIR)
    print("SHADOW_PATH:", SHADOW_PATH)
    print("Shadow stats:", shadow_stats())
