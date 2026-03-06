# trading/live_trader.py
from __future__ import annotations

import os
import time
import hmac
import json
import hashlib
from typing import Any, Dict, Optional

import requests

# =========================
# ENV
# =========================
BITVAVO_BASE_URL = (os.getenv("BITVAVO_BASE_URL") or "https://api.bitvavo.com").rstrip("/")
BITVAVO_API_KEY = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()
BITVAVO_OPERATOR_ID = (os.getenv("BITVAVO_OPERATOR_ID") or "crypto_ai_bot").strip()

BITVAVO_ACCESS_WINDOW = (os.getenv("BITVAVO_ACCESS_WINDOW") or "10000").strip()
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT") or "20")

# live state
def _get_data_dir() -> str:
    d = (os.getenv("DATA_DIR") or "").strip()
    if d:
        return d
    return "/data" if os.path.isdir("/data") else "/tmp/data"


DATA_DIR = _get_data_dir()
LIVE_STATE_PATH = (os.getenv("LIVE_STATE_PATH") or os.path.join(DATA_DIR, "live_state.json")).strip()
LIVE_TRADES_CSV = (os.getenv("LIVE_TRADES_CSV") or os.path.join(DATA_DIR, "live_trades.csv")).strip()

# markets cache
_MARKETS_CACHE: Dict[str, Any] = {"ts": 0.0, "set": set()}
_MARKETS_TTL_SECONDS = 60 * 30  # 30 min


# =========================
# File helpers
# =========================
def _ensure_dir_for(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _safe_str(x: Any, default: str = "") -> str:
    if x is None:
        return default
    try:
        s = str(x).strip()
        return s if s else default
    except Exception:
        return default


def _meta_to_log_str(meta: Any) -> str:
    try:
        if isinstance(meta, dict):
            return json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
        return str(meta or "")
    except Exception:
        return str(meta or "")


def _load_state() -> Dict[str, Any]:
    _ensure_dir_for(LIVE_STATE_PATH)
    if not os.path.exists(LIVE_STATE_PATH):
        return {"positions": {}, "open_trades": []}

    try:
        with open(LIVE_STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}

    s.setdefault("positions", {})
    s.setdefault("open_trades", [])
    return s


def _save_state(state: Dict[str, Any]) -> None:
    _ensure_dir_for(LIVE_STATE_PATH)
    tmp = LIVE_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, LIVE_STATE_PATH)


def _log_row(symbol: str, side: str, price: float, qty: float, pnl: float = 0.0, meta: str = "") -> None:
    _ensure_dir_for(LIVE_TRADES_CSV)
    new = not os.path.exists(LIVE_TRADES_CSV)
    with open(LIVE_TRADES_CSV, "a", encoding="utf-8") as f:
        if new:
            f.write("datetime,symbol,side,price,qty,pnl,meta\n")
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())},{symbol},{side},{price},{qty},{pnl},{meta}\n")


def _remove_existing_open_trade(state: Dict[str, Any], symbol: str) -> None:
    open_trades = state.get("open_trades", []) or []
    state["open_trades"] = [t for t in open_trades if t.get("symbol") != symbol]


# =========================
# Helpers
# =========================
def _canonical_json(obj: Any) -> str:
    """
    JSON zonder spaties voor stabiele Bitvavo-signature.
    """
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _sign(timestamp_ms: str, method: str, path: str, body: str) -> str:
    """
    Bitvavo signing string = timestamp + method + path + body
    """
    msg = f"{timestamp_ms}{method.upper()}{path}{body}".encode("utf-8")
    secret = BITVAVO_API_SECRET.encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def _headers(timestamp_ms: str, signature: str) -> Dict[str, str]:
    return {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": signature,
        "Bitvavo-Access-Timestamp": timestamp_ms,
        "Bitvavo-Access-Window": BITVAVO_ACCESS_WINDOW,
        "Content-Type": "application/json",
    }


def _request_signed(method: str, path: str, body_obj: Optional[dict] = None) -> Dict[str, Any]:
    """
    Signed request naar Bitvavo.
    """
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY/SECRET ontbreken in env.")

    body = _canonical_json(body_obj) if body_obj else ""
    ts = str(int(time.time() * 1000))
    sig = _sign(ts, method, path, body)

    url = f"{BITVAVO_BASE_URL}{path}"
    response = requests.request(
        method=method.upper(),
        url=url,
        headers=_headers(ts, sig),
        data=body if body else None,
        timeout=HTTP_TIMEOUT,
    )

    try:
        data = response.json()
    except Exception:
        data = {
            "ok": False,
            "status": response.status_code,
            "text": response.text,
        }

    return {
        "ok": response.ok,
        "status": response.status_code,
        "data": data,
    }


# =========================
# Public markets (no signing)
# =========================
def _get_tradable_markets() -> set[str]:
    """
    Haalt actieve/tradable Bitvavo markets op en cachet ze 30 minuten.
    """
    now = time.time()
    if _MARKETS_CACHE["set"] and (now - _MARKETS_CACHE["ts"] < _MARKETS_TTL_SECONDS):
        return _MARKETS_CACHE["set"]

    url = f"{BITVAVO_BASE_URL}/v2/markets"
    response = requests.get(url, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    items = response.json()

    tradable = set()
    for market_info in items:
        market = (market_info.get("market") or "").strip()
        status = (market_info.get("status") or "").strip().lower()
        if market and status == "trading":
            tradable.add(market)

    _MARKETS_CACHE["ts"] = now
    _MARKETS_CACHE["set"] = tradable
    return tradable


def symbol_usdt_to_bitvavo_market(symbol_usdt: str) -> str:
    """
    Zet Binance-style symbool om naar Bitvavo market.

    Voorbeeld:
    ETHFIUSDT -> ETHFI-EUR
    """
    symbol = (symbol_usdt or "").upper().strip()
    if "-" in symbol:
        return symbol
    if not symbol.endswith("USDT"):
        raise ValueError(f"symbol_usdt moet eindigen op USDT, ontvangen: {symbol_usdt}")

    base = symbol[:-4].strip()
    if not base:
        raise ValueError(f"Ongeldig symbol_usdt ontvangen: {symbol_usdt}")

    return f"{base}-EUR"


def ensure_market_tradable(symbol_usdt: str) -> str:
    """
    Controleert of de coin ook echt tradable is op Bitvavo.
    """
    market = symbol_usdt_to_bitvavo_market(symbol_usdt)
    tradable_markets = _get_tradable_markets()

    if market not in tradable_markets:
        raise ValueError(f"UNSUPPORTED_MARKET: {market} (niet tradable/geen listing op Bitvavo).")

    return market


# =========================
# Bitvavo response parsing
# =========================
def _extract_order_price(order: Dict[str, Any]) -> float:
    """
    Probeert een nuttige prijs uit order/fills te halen.
    """
    price = _safe_float(order.get("price"), 0.0)
    if price > 0:
        return price

    fills = order.get("fills")
    if isinstance(fills, list) and fills:
        fill_price = _safe_float(fills[0].get("price"), 0.0)
        if fill_price > 0:
            return fill_price

    return 0.0


def _extract_filled_base_amount(order: Dict[str, Any]) -> float:
    """
    Probeert de gekochte/verkochte base amount te bepalen.
    """
    for key in ("filledAmount", "amount", "filled"):
        val = _safe_float(order.get(key), 0.0)
        if val > 0:
            return val

    fills = order.get("fills")
    if isinstance(fills, list) and fills:
        total = 0.0
        for fill in fills:
            amount = _safe_float(fill.get("amount"), 0.0)
            if amount > 0:
                total += amount
        if total > 0:
            return total

    return 0.0


# =========================
# Trading
# =========================
def place_market_buy_eur(symbol_usdt: str, amount_eur: float, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Market BUY met EUR-bedrag via amountQuote.
    Slaat bij succes ook live_state.json bij zodat trade_monitor kan monitoren.
    """
    meta = meta or {}

    if amount_eur <= 0:
        raise ValueError("amount_eur moet > 0 zijn")

    market = ensure_market_tradable(symbol_usdt)

    body = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        "amountQuote": str(float(amount_eur)),
        "operatorId": BITVAVO_OPERATOR_ID,
    }

    result = _request_signed("POST", "/v2/order", body)

    if not result["ok"]:
        return {
            "ok": False,
            "status": result.get("status"),
            "error": result.get("data"),
            "market": market,
            "sent": body,
        }

    order = result.get("data") or {}
    price = _extract_order_price(order)
    base_amount = _extract_filled_base_amount(order)
    opened_at = int(time.time())

    prebuy_id = _safe_str(meta.get("prebuy_id"))
    stop_loss = _safe_float(meta.get("stop_loss") or meta.get("stop"), 0.0)
    target = _safe_float(meta.get("target"), 0.0)

    timeframe = _safe_str(meta.get("timeframe"), "4h")
    setup_type = _safe_str(meta.get("setup_type"), "TREND_PULLBACK")
    regime = _safe_str(meta.get("regime"), "UNKNOWN")

    score = _safe_int(meta.get("score"), 0)
    raw_score = _safe_int(meta.get("raw_score"), score)
    chance = _safe_int(meta.get("chance"), 0)
    confidence = _safe_int(meta.get("confidence"), chance)

    label = _safe_str(meta.get("label"), "GO")
    why_tag = _safe_str(meta.get("why_tag"))
    market_condition = _safe_str(meta.get("market_condition"), "NORMAAL")
    overextended_pct = _safe_float(meta.get("overextended_pct"), 0.0)

    user_decision = _safe_str(meta.get("user_decision"), "YES")
    bot_decision = _safe_str(meta.get("bot_decision"), label)

    state = _load_state()

    state["positions"][symbol_usdt] = {
        "market": market,
        "qty": base_amount,
        "entry": price,
        "stop_loss": stop_loss,
        "target": target,
        "opened_at": opened_at,
        "prebuy_id": prebuy_id,
        "amount_eur": amount_eur,
        "position_size": base_amount,
        "base_amount": base_amount,
        "timeframe": timeframe,
        "setup_type": setup_type,
        "regime": regime,
        "score": score,
        "raw_score": raw_score,
        "chance": chance,
        "confidence": confidence,
        "label": label,
        "why_tag": why_tag,
        "market_condition": market_condition,
        "overextended_pct": overextended_pct,
        "user_decision": user_decision,
        "bot_decision": bot_decision,
        "live": True,
    }

    _remove_existing_open_trade(state, symbol_usdt)

    state["open_trades"].append(
        {
            "symbol": symbol_usdt,
            "market": market,
            "entry": price,
            "stop_loss": stop_loss,
            "stop": stop_loss,
            "target": target,
            "prebuy_id": prebuy_id,
            "status": "OPEN",
            "opened_at": opened_at,
            "monitor_started_at": opened_at,
            "mode": "NORMAL",
            "live": True,

            # trade sizing
            "amount_eur": amount_eur,
            "position_size": base_amount,
            "base_amount": base_amount,

            # leer/context velden
            "timeframe": timeframe,
            "setup_type": setup_type,
            "regime": regime,
            "score": score,
            "raw_score": raw_score,
            "chance": chance,
            "confidence": confidence,
            "label": label,
            "why_tag": why_tag,
            "market_condition": market_condition,
            "overextended_pct": overextended_pct,
            "user_decision": user_decision,
            "bot_decision": bot_decision,

            # monitor leerwaarden
            "max_r": 0.0,
            "mfe_r": 0.0,
            "mae_r": 0.0,
            "max_price_seen": price,
            "min_price_seen": price,
            "had_over_1r": False,
            "partial_sold_40": False,
            "below_1r_count": 0,
            "target_reached_notified": False,
            "last_check": opened_at,
        }
    )

    _save_state(state)
    _log_row(
        symbol_usdt,
        "BUY",
        price,
        base_amount,
        meta=_meta_to_log_str({
            "prebuy_id": prebuy_id,
            "market": market,
            "amount_eur": amount_eur,
            "setup_type": setup_type,
            "regime": regime,
            "score": score,
            "chance": chance,
            "label": label,
        }),
    )

    return {
        "ok": True,
        "status": result.get("status"),
        "order": order,
        "market": market,
        "price": price,
        "base_amount": base_amount,
        "live": True,
    }


def place_market_sell_base(market: str, amount_base: float) -> Dict[str, Any]:
    """
    Market SELL met hoeveelheid van de base asset.
    Werkt ook live_state.json bij.
    """
    if amount_base <= 0:
        raise ValueError("amount_base moet > 0 zijn")

    body = {
        "market": market,
        "side": "sell",
        "orderType": "market",
        "amount": str(float(amount_base)),
        "operatorId": BITVAVO_OPERATOR_ID,
    }

    result = _request_signed("POST", "/v2/order", body)

    if not result["ok"]:
        return {
            "ok": False,
            "status": result.get("status"),
            "error": result.get("data"),
            "market": market,
            "sent": body,
        }

    order = result.get("data") or {}
    price = _extract_order_price(order)
    filled_amount = _extract_filled_base_amount(order)

    # state update
    state = _load_state()

    symbol_key = None
    for sym, pos in (state.get("positions") or {}).items():
        pos_market = _safe_str(pos.get("market"))
        if pos_market == market or symbol_usdt_to_bitvavo_market(sym) == market:
            symbol_key = sym
            break

    pnl = 0.0

    if symbol_key:
        pos = state["positions"].get(symbol_key) or {}
        entry = _safe_float(pos.get("entry"), 0.0)
        qty_total = _safe_float(pos.get("qty") or pos.get("base_amount") or pos.get("position_size"), 0.0)
        sell_qty = filled_amount if filled_amount > 0 else amount_base

        pnl = (price - entry) * sell_qty if entry > 0 and sell_qty > 0 else 0.0

        if sell_qty >= qty_total or qty_total <= 0:
            del state["positions"][symbol_key]
        else:
            pos["qty"] = max(0.0, qty_total - sell_qty)
            pos["base_amount"] = pos["qty"]
            pos["position_size"] = pos["qty"]
            pos["last_sell_price"] = price
            pos["last_sell_at"] = int(time.time())
            state["positions"][symbol_key] = pos

        _save_state(state)
        _log_row(
            symbol_key,
            "SELL",
            price,
            sell_qty,
            pnl=pnl,
            meta=_meta_to_log_str({"market": market}),
        )

    return {
        "ok": True,
        "status": result.get("status"),
        "order": order,
        "market": market,
        "price": price,
        "qty": filled_amount if filled_amount > 0 else amount_base,
        "pnl": pnl,
        "live": True,
    }


# =========================
# Compatibility aliases
# =========================
def buy_eur(symbol_usdt: str, amount_eur: float, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Compatibiliteitsfunctie voor code die buy_eur(...) verwacht.
    """
    return place_market_buy_eur(symbol_usdt, amount_eur, meta=meta)


def sell(symbol: str, fraction: float = 1.0, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Compatibele sell voor trade_monitor:
    - symbol is meestal BTCUSDT / ETHUSDT etc.
    - fraction 1.0 = alles
    """
    meta = meta or {}
    market = ensure_market_tradable(symbol)

    state = _load_state()
    pos = state.get("positions", {}).get(symbol)
    if not pos:
        return {"ok": False, "reason": "NO_POSITION", "market": market}

    qty_total = _safe_float(pos.get("qty") or pos.get("base_amount") or pos.get("position_size"), 0.0)
    if qty_total <= 0:
        return {"ok": False, "reason": "QTY_INVALID", "market": market}

    fraction = float(fraction)
    if fraction <= 0:
        return {"ok": False, "reason": "FRACTION_INVALID", "market": market}

    amount_base = qty_total if fraction >= 1.0 else qty_total * fraction
    return place_market_sell_base(market, amount_base)


def sell_base(market: str, amount_base: float) -> Dict[str, Any]:
    """
    Compatibiliteitsfunctie voor code die sell_base(...) verwacht.
    """
    return place_market_sell_base(market, amount_base)


# =========================
# Debug helpers
# =========================
def get_tradable_markets_cached() -> Dict[str, Any]:
    """
    Handig voor debug / filtering.
    """
    markets = _get_tradable_markets()
    return {
        "count": len(markets),
        "sample": sorted(list(markets))[:20],
    }
