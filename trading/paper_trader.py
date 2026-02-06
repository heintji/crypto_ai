# trading/paper_trader.py
import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import hmac
import hashlib
import requests

# =========================================
# RENDER DISK PATHS (belangrijk!)
# =========================================
STATE_PATH = os.getenv("PAPER_STATE_PATH", "/data/paper_state.json")
APPROVALS_PATH = os.getenv("PENDING_PATH", "/data/pending_approvals.json")
LOG_PATH = os.getenv("PAPER_TRADES_CSV", "/data/paper_trades.csv")

HTTP_TIMEOUT = 15

# =========================================
# LIVE TRADING SWITCH (jij wil altijd LIVE)
# =========================================
# Jij wilt geen paper trades meer. Dus dit staat hard op LIVE.
LIVE_TRADING = True

# =========================================
# Bitvavo API
# =========================================
BITVAVO_API_KEY = os.getenv("BITVAVO_API_KEY", "").strip()
BITVAVO_API_SECRET = os.getenv("BITVAVO_API_SECRET", "").strip()
BITVAVO_BASE_URL = os.getenv("BITVAVO_BASE_URL", "https://api.bitvavo.com").strip()

# Binance (alleen voor prijs/R-berekening in trade_monitor)
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"

# In jouw bot gebruik je symbols als BTCUSDT (Binance).
# Bitvavo werkt met markets zoals BTC-EUR.
QUOTE_CURRENCY = os.getenv("QUOTE_CURRENCY", "EUR").strip().upper()


# =========================================
# Helpers
# =========================================
def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def _now_ts() -> int:
    return int(time.time())


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def binance_symbol_to_bitvavo_market(symbol: str) -> str:
    """
    BTCUSDT -> BTC-EUR
    ETHUSDT -> ETH-EUR
    (We nemen aan: quote is EUR bij Bitvavo.)
    """
    s = (symbol or "").upper().strip()
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}-{QUOTE_CURRENCY}"
    if s.endswith("EUR"):
        base = s[:-3]
        return f"{base}-{QUOTE_CURRENCY}"
    # fallback: probeer eerste 3/4 letters als base (niet perfect)
    # beter: jij blijft gewoon BTCUSDT/ETHUSDT etc gebruiken.
    base = s.replace("-", "").replace("/", "")
    if len(base) >= 3:
        return f"{base[:3]}-{QUOTE_CURRENCY}"
    return f"{s}-{QUOTE_CURRENCY}"


# =========================================
# WhatsApp (Twilio)
# =========================================
def twilio_ready() -> bool:
    return all([
        os.getenv("TWILIO_ACCOUNT_SID"),
        os.getenv("TWILIO_AUTH_TOKEN"),
        os.getenv("TWILIO_WHATSAPP_FROM"),
        os.getenv("TWILIO_WHATSAPP_TO"),
    ])


def send_whatsapp(message: str) -> bool:
    if not twilio_ready():
        print("📭 WhatsApp melding overgeslagen (Twilio env vars ontbreken).", flush=True)
        return False

    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    wa_from = os.getenv("TWILIO_WHATSAPP_FROM")
    wa_to = os.getenv("TWILIO_WHATSAPP_TO")

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = {"From": wa_from, "To": wa_to, "Body": message}

    try:
        r = requests.post(url, data=data, auth=(sid, token), timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            print(f"⚠️ Twilio send failed ({r.status_code}): {r.text[:200]}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"⚠️ Twilio send exception: {e}", flush=True)
        return False


# =========================================
# PRICE (Binance) — nodig voor trade_monitor R-calcs
# =========================================
def get_price(symbol: str) -> float:
    """
    Binance spot price. trade_monitor gebruikt dit voor R/targets.
    (Heeft niks met live buy/sell te maken.)
    """
    r = requests.get(BINANCE_TICKER_URL, params={"symbol": symbol}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return float(r.json()["price"])


# =========================================
# Bitvavo signing + request
# =========================================
def _bitvavo_headers(method: str, path: str, body: str = "") -> Dict[str, str]:
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY/SECRET ontbreken (Render env vars).")

    ts = str(int(time.time() * 1000))
    payload = ts + method.upper() + path + body
    sig = hmac.new(
        BITVAVO_API_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    return {
        "bitvavo-access-key": BITVAVO_API_KEY,
        "bitvavo-access-signature": sig,
        "bitvavo-access-timestamp": ts,
        "bitvavo-access-window": "10000",
        "Content-Type": "application/json",
    }


def _bitvavo_request(method: str, path: str, params: Optional[dict] = None, data: Optional[dict] = None) -> Any:
    url = BITVAVO_BASE_URL + path
    body = json.dumps(data) if data else ""
    headers = _bitvavo_headers(method, path, body)

    r = requests.request(
        method=method.upper(),
        url=url,
        params=params,
        data=body if body else None,
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )

    if r.status_code >= 400:
        # Bitvavo geeft vaak json error body, maar soms plain text
        try:
            err = r.json()
        except Exception:
            err = r.text
        raise RuntimeError(f"Bitvavo API error {r.status_code} on {path}: {err}")

    # meestal json
    try:
        return r.json()
    except Exception:
        return r.text


def bitvavo_get_balances() -> List[Dict[str, Any]]:
    # /v2/balance
    return _bitvavo_request("GET", "/v2/balance")


def bitvavo_get_open_orders(market: Optional[str] = None) -> List[Dict[str, Any]]:
    # /v2/ordersOpen
    params = {}
    if market:
        params["market"] = market
    return _bitvavo_request("GET", "/v2/ordersOpen", params=params)


def bitvavo_place_order(
    market: str,
    side: str,
    order_type: str,
    amount: Optional[str] = None,
    amount_quote: Optional[str] = None
) -> Dict[str, Any]:
    """
    Plaats market order.
    - SELL: amount = base amount (qty)
    - BUY: bij voorkeur amountQuote (EUR)
    """
    payload: Dict[str, Any] = {
        "market": market,
        "side": side.upper(),
        "orderType": order_type,
    }
    if amount_quote is not None:
        payload["amountQuote"] = str(amount_quote)
    if amount is not None:
        payload["amount"] = str(amount)

    # /v2/order
    return _bitvavo_request("POST", "/v2/order", data=payload)


def _balance_lookup(balances: List[Dict[str, Any]], symbol: str) -> Tuple[float, float]:
    """
    Returns (available, inOrder)
    """
    for b in balances:
        if str(b.get("symbol", "")).upper() == symbol.upper():
            return _safe_float(b.get("available", 0.0)), _safe_float(b.get("inOrder", 0.0))
    return 0.0, 0.0


def get_eur_available_live() -> float:
    balances = bitvavo_get_balances()
    available, _ = _balance_lookup(balances, "EUR")
    return available


def get_asset_available_live(asset: str) -> float:
    balances = bitvavo_get_balances()
    available, _ = _balance_lookup(balances, asset)
    return available


# =========================================
# STATE (we houden dit bij voor trade_monitor)
# =========================================
def _default_state() -> Dict[str, Any]:
    return {
        "balance": 0.0,  # live EUR available
        "positions": {},  # symbol -> qty (Binance symbol key), plus "{symbol}_entry"
        "open_trades": []
    }


def load_state() -> Dict[str, Any]:
    if not os.path.isfile(STATE_PATH):
        return _default_state()

    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        return _default_state()

    base = _default_state()
    s.setdefault("balance", base["balance"])
    s.setdefault("positions", base["positions"])
    s.setdefault("open_trades", base["open_trades"])
    return s


def save_state(state: Dict[str, Any]) -> None:
    ensure_parent_dir(STATE_PATH)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def has_position(symbol: str) -> bool:
    s = load_state()
    return float(s.get("positions", {}).get(symbol, 0.0)) > 0


# =========================================
# APPROVAL HANDLING (CRUCIAAL)
# =========================================
def load_approvals() -> List[Dict[str, Any]]:
    if not os.path.isfile(APPROVALS_PATH):
        return []
    try:
        with open(APPROVALS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_approvals(data: List[Dict[str, Any]]) -> None:
    ensure_parent_dir(APPROVALS_PATH)
    tmp = APPROVALS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, APPROVALS_PATH)


def consume_approval(prebuy_id: str) -> bool:
    approvals = load_approvals()
    now = int(time.time())

    new_list: List[Dict[str, Any]] = []
    consumed = False

    for a in approvals:
        if a.get("id") == prebuy_id:
            if a.get("status") != "APPROVED":
                return False
            if int(a.get("expires_at", 0)) < now:
                return False
            consumed = True
            continue
        new_list.append(a)

    if consumed:
        save_approvals(new_list)

    return consumed


# =========================================
# LOGGING (CSV)
# =========================================
def log_trade(
    symbol: str,
    side: str,
    price: float,
    qty: float,
    balance: float,
    pnl: float = 0.0,
    meta: str = ""
) -> None:
    ensure_parent_dir(LOG_PATH)
    exists = os.path.isfile(LOG_PATH)

    with open(LOG_PATH, "a", encoding="utf-8") as f:
        if not exists:
            f.write("datetime,symbol,side,price,qty,balance,pnl,meta\n")
        f.write(
            f"{_iso_now()},"
            f"{symbol},{side},{price:.8f},{qty:.12f},{balance:.2f},{pnl:.2f},{meta}\n"
        )


# =========================================
# LIVE BUY (Bitvavo) — ALLEEN VIA APPROVAL
# =========================================
def buy_eur(
    symbol: str,                  # Binance symbol, bv BTCUSDT
    price: Optional[float],       # entry price (kan None)
    amount_eur: float,            # inzet
    stop_loss: float,
    target: float,
    prebuy_id: Optional[str] = None
) -> Dict[str, Any]:

    if not prebuy_id:
        return {"ok": False, "reason": "MISSING_PREBUY_ID"}

    # 1) approval consumeren (nooit dubbel)
    if not consume_approval(prebuy_id):
        return {"ok": False, "reason": "APPROVAL_INVALID_OR_USED"}

    if not LIVE_TRADING:
        return {"ok": False, "reason": "LIVE_TRADING_DISABLED"}  # zou bij jou nooit gebeuren

    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        return {"ok": False, "reason": "BITVAVO_KEYS_MISSING"}

    # 2) al in positie? (volgens eigen state)
    if has_position(symbol):
        return {"ok": False, "reason": "ALREADY_IN_POSITION"}

    # 3) check live eur beschikbaar
    try:
        eur_avail = get_eur_available_live()
    except Exception as e:
        return {"ok": False, "reason": f"BITVAVO_BALANCE_ERROR: {e}"}

    if amount_eur > eur_avail:
        return {"ok": False, "reason": "INSUFFICIENT_EUR_LIVE", "eur_available": eur_avail}

    # 4) market bepalen
    market = binance_symbol_to_bitvavo_market(symbol)
    base_asset = market.split("-")[0]

    # 5) entry prijs voor logging/R (liefst Binance, anders Bitvavo ticker)
    if price is None:
        try:
            price = get_price(symbol)
        except Exception:
            price = 0.0

    # 6) LIVE market BUY op Bitvavo (voorkeur amountQuote)
    order_res: Optional[Dict[str, Any]] = None
    try:
        order_res = bitvavo_place_order(
            market=market,
            side="buy",
            order_type="market",
            amount_quote=str(round(float(amount_eur), 2)),
            amount=None
        )
    except Exception as e1:
        # fallback: als amountQuote niet geaccepteerd wordt, bereken qty via prijs
        if price and price > 0:
            qty_guess = float(amount_eur) / float(price)
            try:
                order_res = bitvavo_place_order(
                    market=market,
                    side="buy",
                    order_type="market",
                    amount=str(qty_guess),
                    amount_quote=None
                )
            except Exception as e2:
                return {"ok": False, "reason": f"BITVAVO_BUY_FAILED: {e2}"}
        else:
            return {"ok": False, "reason": f"BITVAVO_BUY_FAILED: {e1}"}

    # 7) na order: haal live balans/asset op
    try:
        eur_after = get_eur_available_live()
        asset_after = get_asset_available_live(base_asset)
    except Exception as e:
        # order is waarschijnlijk wel gedaan, maar snapshot faalt
        eur_after = 0.0
        asset_after = 0.0
        print(f"⚠️ Balance refresh error after BUY: {e}", flush=True)

    # 8) qty bepalen (beste poging): asset available delta is lastig zonder 'before'
    # We gebruiken asset_after als indicatie (niet perfect als je al assets had).
    # Voor trade_monitor hebben we vooral 'qty' nodig om te kunnen SELL'en.
    qty = asset_after

    # 9) state opslaan (voor monitor)
    state = load_state()
    state["balance"] = eur_after
    state["positions"][symbol] = qty
    state["positions"][f"{symbol}_entry"] = float(price) if price else 0.0

    trade_id = f"{symbol}-{_now_ts()}"
    state["open_trades"].append({
        "trade_id": trade_id,
        "symbol": symbol,
        "entry": float(price) if price else 0.0,
        "stop_loss": float(stop_loss),
        "target": float(target),
        "qty": qty,
        "side": "LONG",
        "prebuy_id": prebuy_id,
        "opened_at": _now_ts(),
        "bitvavo_market": market,
        "bitvavo_order": order_res,
    })

    save_state(state)

    log_trade(symbol, "BUY", float(price) if price else 0.0, qty, eur_after, meta=f"LIVE bitvavo market={market} prebuy={prebuy_id}")

    print(f"✅ LIVE BUY OK | {market} | €{amount_eur:.2f}", flush=True)

    msg = (
        f"✅ LIVE BUY uitgevoerd (Bitvavo)\n"
        f"Market: {market}\n"
        f"Inzet: €{amount_eur:.2f}\n"
        f"Entry (binance): {float(price) if price else 0.0:.6f}\n"
        f"Stop: {float(stop_loss):.6f}\n"
        f"Target: {float(target):.6f}\n"
        f"Trade ID: {trade_id}\n"
        f"Pre-BUY ID: {prebuy_id}\n"
        f"EUR (avail): €{eur_after:.2f}\n"
        f"{base_asset} (avail): {asset_after:.8f}"
    )
    send_whatsapp(msg)

    return {
        "ok": True,
        "trade_id": trade_id,
        "symbol": symbol,
        "bitvavo_market": market,
        "price": float(price) if price else 0.0,
        "qty": qty,
        "eur_available": eur_after,
        "order": order_res,
    }


# =========================================
# LIVE SELL (Bitvavo) — aangestuurd door trade_monitor
# =========================================
def sell(symbol: str, fraction: float = 1.0) -> Dict[str, Any]:
    if not LIVE_TRADING:
        return {"ok": False, "reason": "LIVE_TRADING_DISABLED"}

    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        return {"ok": False, "reason": "BITVAVO_KEYS_MISSING"}

    state = load_state()
    positions = state.get("positions", {})
    open_trades = state.get("open_trades", [])

    qty_total = float(positions.get(symbol, 0.0))
    if qty_total <= 0:
        return {"ok": False, "reason": "NO_POSITION_IN_STATE"}

    fraction = max(0.0, min(1.0, float(fraction)))
    if fraction <= 0:
        return {"ok": False, "reason": "INVALID_FRACTION"}

    market = None
    for t in open_trades:
        if t.get("symbol") == symbol and t.get("bitvavo_market"):
            market = t.get("bitvavo_market")
            break
    if not market:
        market = binance_symbol_to_bitvavo_market(symbol)

    base_asset = market.split("-")[0]

    qty_sell = qty_total * fraction
    if qty_sell <= 0:
        return {"ok": False, "reason": "SELL_QTY_ZERO"}

    # prijs voor pnl logging (binance)
    try:
        price_now = get_price(symbol)
    except Exception:
        price_now = 0.0

    entry = float(positions.get(f"{symbol}_entry", 0.0)) or 0.0

    # LIVE market SELL
    try:
        order_res = bitvavo_place_order(
            market=market,
            side="sell",
            order_type="market",
            amount=str(qty_sell),
            amount_quote=None
        )
    except Exception as e:
        return {"ok": False, "reason": f"BITVAVO_SELL_FAILED: {e}"}

    # refresh balances
    try:
        eur_after = get_eur_available_live()
        asset_after = get_asset_available_live(base_asset)
    except Exception as e:
        eur_after = float(state.get("balance", 0.0))
        asset_after = 0.0
        print(f"⚠️ Balance refresh error after SELL: {e}", flush=True)

    pnl = 0.0
    if entry > 0 and price_now > 0:
        pnl = (price_now - entry) * qty_sell

    # state aanpassen
    qty_left = qty_total - qty_sell
    state["balance"] = eur_after

    if qty_left <= 1e-12:
        positions.pop(symbol, None)
        positions.pop(f"{symbol}_entry", None)
        state["open_trades"] = [t for t in open_trades if t.get("symbol") != symbol]
    else:
        positions[symbol] = qty_left
        for t in state["open_trades"]:
            if t.get("symbol") == symbol:
                t["qty"] = qty_left

    save_state(state)

    log_trade(symbol, "SELL", float(price_now) if price_now else 0.0, qty_sell, eur_after, pnl=pnl, meta=f"LIVE bitvavo market={market} fraction={fraction}")

    print(f"💰 LIVE SELL | {market} {fraction*100:.0f}% | pnl={pnl:.2f}", flush=True)

    return {
        "ok": True,
        "symbol": symbol,
        "bitvavo_market": market,
        "price": float(price_now) if price_now else 0.0,
        "qty_sold": qty_sell,
        "qty_left": max(0.0, qty_left),
        "pnl": pnl,
        "eur_available": eur_after,
        "asset_available": asset_after,
        "order": order_res,
    }


if __name__ == "__main__":
    print("✅ LIVE paper_trader (Bitvavo) klaar", flush=True)
    print(f"STATE_PATH: {STATE_PATH}", flush=True)
    print(f"LOG_PATH:   {LOG_PATH}", flush=True)
    print(f"LIVE_TRADING: {LIVE_TRADING}", flush=True)
