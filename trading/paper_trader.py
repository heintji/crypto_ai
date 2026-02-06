import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
import hmac
import hashlib

# =========================================
# RENDER DISK PATHS (belangrijk!)
# =========================================
# Op Render is /data de enige plek die blijft bestaan.
STATE_PATH = os.getenv("LIVE_STATE_PATH", "/data/live_state.json")
APPROVALS_PATH = os.getenv("PENDING_PATH", "/data/pending_approvals.json")
LOG_PATH = os.getenv("LIVE_TRADES_CSV", "/data/live_trades.csv")

HTTP_TIMEOUT = 20

# =========================================
# Bitvavo settings
# =========================================
BITVAVO_API_KEY = os.getenv("BITVAVO_API_KEY", "")
BITVAVO_API_SECRET = os.getenv("BITVAVO_API_SECRET", "")
BITVAVO_BASE_URL = "https://api.bitvavo.com"

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
    """
    Stuurt WhatsApp bericht via Twilio. Als env vars ontbreken: silent skip + print.
    """
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
# Helpers
# =========================================
def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def symbol_to_bitvavo_market(symbol: str) -> str:
    """
    Jouw bot gebruikt BTCUSDT (Binance-style).
    Bitvavo werkt typisch met BTC-EUR.
    """
    s = symbol.upper().strip()
    if s.endswith("USDT"):
        base = s.replace("USDT", "")
        return f"{base}-EUR"
    # fallback: als je ooit al BTC-EUR doorgeeft
    if "-" in s:
        return s
    return f"{s}-EUR"


# =========================================
# Bitvavo signing + request
# =========================================
def _require_bitvavo_keys():
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        raise RuntimeError("BITVAVO_API_KEY / BITVAVO_API_SECRET ontbreken in env vars.")


def _bitvavo_sign(ts_ms: str, method: str, path: str, body: str = "") -> str:
    msg = f"{ts_ms}{method.upper()}{path}{body}"
    return hmac.new(
        BITVAVO_API_SECRET.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def _bitvavo_headers(method: str, path: str, body: str = "") -> Dict[str, str]:
    _require_bitvavo_keys()
    ts = str(int(time.time() * 1000))
    sig = _bitvavo_sign(ts, method, path, body)
    return {
        "Bitvavo-Access-Key": BITVAVO_API_KEY,
        "Bitvavo-Access-Signature": sig,
        "Bitvavo-Access-Timestamp": ts,
        "Content-Type": "application/json",
    }


def bitvavo_request(method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{BITVAVO_BASE_URL}{path}"
    body = ""
    data = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":"))
        data = body

    r = requests.request(method, url, headers=_bitvavo_headers(method, path, body), data=data, timeout=HTTP_TIMEOUT)

    if r.status_code >= 300:
        raise RuntimeError(f"Bitvavo {r.status_code} {path}: {r.text}")

    if not r.text:
        return None

    try:
        return r.json()
    except Exception:
        return {"raw": r.text}


def bitvavo_market_buy_eur(market: str, amount_eur: float) -> Dict[str, Any]:
    """
    ECHTE market BUY op Bitvavo: kopen voor EUR bedrag (quote).
    """
    payload = {
        "market": market,
        "side": "buy",
        "orderType": "market",
        "amountQuote": f"{float(amount_eur):.2f}",
    }
    return bitvavo_request("POST", "/v2/order", payload)


def bitvavo_market_sell_base(market: str, amount_base: float) -> Dict[str, Any]:
    """
    ECHTE market SELL op Bitvavo: verkopen coin hoeveelheid (base).
    """
    payload = {
        "market": market,
        "side": "sell",
        "orderType": "market",
        "amount": str(amount_base),
    }
    return bitvavo_request("POST", "/v2/order", payload)


def extract_qty_and_avg_price(order_resp: Dict[str, Any]) -> Tuple[float, float]:
    """
    Probeert robust base_qty + avg_price uit response te halen.
    Als Bitvavo response afwijkt, blijven we netjes fallbacken.
    """
    base_qty = 0.0
    avg_price = 0.0

    fills = order_resp.get("fills")
    if isinstance(fills, list) and fills:
        total_qty = 0.0
        total_quote = 0.0
        for f in fills:
            q = float(f.get("amount") or f.get("quantity") or f.get("size") or 0.0)
            p = float(f.get("price") or 0.0)
            total_qty += q
            total_quote += q * p
        if total_qty > 0:
            base_qty = total_qty
            avg_price = total_quote / total_qty

    if base_qty == 0.0:
        # alternatieve keys
        for k in ["filledAmount", "amount", "executedAmount"]:
            try:
                base_qty = float(order_resp.get(k) or 0.0)
                if base_qty:
                    break
            except Exception:
                pass

    if avg_price == 0.0:
        for k in ["averagePrice", "price", "avgPrice"]:
            try:
                avg_price = float(order_resp.get(k) or 0.0)
                if avg_price:
                    break
            except Exception:
                pass

    return float(base_qty), float(avg_price)


# =========================================
# LIVE STATE (Render Disk)
# =========================================
def _default_state() -> Dict[str, Any]:
    return {"open_trades": {}}


def load_state() -> Dict[str, Any]:
    if not os.path.isfile(STATE_PATH):
        return _default_state()
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        return _default_state()

    s.setdefault("open_trades", {})
    return s


def save_state(state: Dict[str, Any]) -> None:
    ensure_parent_dir(STATE_PATH)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def has_open_trade(prebuy_id: str) -> bool:
    s = load_state()
    return prebuy_id in (s.get("open_trades") or {})


# =========================================
# APPROVAL HANDLING (supports list OR dict)
# =========================================
def load_approvals_any() -> Any:
    if not os.path.isfile(APPROVALS_PATH):
        return None
    try:
        with open(APPROVALS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_approvals_any(data: Any) -> None:
    ensure_parent_dir(APPROVALS_PATH)
    tmp = APPROVALS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, APPROVALS_PATH)


def consume_approval(prebuy_id: str) -> Dict[str, Any]:
    """
    Verbruikt approval definitief na LIVE BUY.
    Ondersteunt 2 formaten:
    1) list: [{id, status, expires_at, ...}, ...]
    2) dict: {"pending": { "<id>": {...}, ... } }
    Retourneert: {"ok": bool, "item": dict|None, "reason": str}
    """
    data = load_approvals_any()
    now = int(time.time())

    if data is None:
        return {"ok": False, "item": None, "reason": "NO_APPROVALS_FILE"}

    # Format A: list
    if isinstance(data, list):
        new_list: List[Dict[str, Any]] = []
        found_item = None

        for a in data:
            if a.get("id") == prebuy_id:
                found_item = a
                # checks
                if a.get("status") != "APPROVED":
                    return {"ok": False, "item": None, "reason": "NOT_APPROVED"}
                if int(a.get("expires_at", 0)) < now:
                    return {"ok": False, "item": None, "reason": "EXPIRED"}
                continue  # consume -> remove
            new_list.append(a)

        if not found_item:
            return {"ok": False, "item": None, "reason": "ID_NOT_FOUND"}

        save_approvals_any(new_list)
        return {"ok": True, "item": found_item, "reason": "OK"}

    # Format B: dict with "pending"
    if isinstance(data, dict):
        pending = data.get("pending")
        if not isinstance(pending, dict):
            return {"ok": False, "item": None, "reason": "INVALID_PENDING_FORMAT"}

        item = pending.get(prebuy_id)
        if not item:
            return {"ok": False, "item": None, "reason": "ID_NOT_FOUND"}

        # checks (best effort)
        status = (item.get("status") or "").upper()
        expires_at = int(item.get("expires_at") or item.get("expiresAt") or 0)

        if status and status not in ["APPROVED", "PENDING", "NEW"]:
            # als jouw flow 'APPROVED' gebruikt, dan houd je het strak.
            # Als je flow geen status gebruikt, laten we het door.
            pass

        if expires_at and expires_at < now:
            return {"ok": False, "item": None, "reason": "EXPIRED"}

        # consume: markeer consumed
        item["status"] = "CONSUMED"
        item["consumed_at"] = now
        pending[prebuy_id] = item
        data["pending"] = pending
        save_approvals_any(data)

        return {"ok": True, "item": item, "reason": "OK"}

    return {"ok": False, "item": None, "reason": "UNKNOWN_APPROVALS_FORMAT"}


# =========================================
# LOGGING (CSV)
# =========================================
def log_trade(
    market: str,
    side: str,
    qty: float,
    amount_eur: float,
    avg_price: float,
    order_id: str,
    meta: str = ""
) -> None:
    ensure_parent_dir(LOG_PATH)
    exists = os.path.isfile(LOG_PATH)

    with open(LOG_PATH, "a", encoding="utf-8") as f:
        if not exists:
            f.write("datetime,market,side,qty,amount_eur,avg_price,order_id,meta\n")
        f.write(
            f"{_now_iso()},"
            f"{market},{side},{qty:.12f},{amount_eur:.2f},{avg_price:.8f},{order_id},{meta}\n"
        )


# =========================================
# LIVE BUY (ONLY VIA APPROVAL)
# =========================================
def buy_eur(
    symbol: str,
    price: Optional[float],  # blijft voor compat, maar we gebruiken Bitvavo fills
    amount_eur: float,
    stop_loss: float,
    target: float,
    prebuy_id: Optional[str] = None
) -> Dict[str, Any]:

    if not prebuy_id:
        return {"ok": False, "reason": "MISSING_PREBUY_ID"}

    if has_open_trade(prebuy_id):
        return {"ok": False, "reason": "ALREADY_OPEN_FOR_PREBUY_ID"}

    # consume approval (must succeed)
    c = consume_approval(prebuy_id)
    if not c["ok"]:
        return {"ok": False, "reason": f"APPROVAL_INVALID: {c['reason']}"}

    market = symbol_to_bitvavo_market(symbol)

    try:
        order = bitvavo_market_buy_eur(market, float(amount_eur))
        base_qty, avg_price = extract_qty_and_avg_price(order)
        order_id = str(order.get("orderId") or order.get("id") or "unknown")
    except Exception as e:
        # als buy faalt: zet approval niet terug (want we hebben al CONSUMED gemarkeerd in dict-mode)
        # -> daarom wil je eigenlijk dict-mode met status update zodat je het kunt zien.
        err = str(e)
        print(f"❌ LIVE BUY FAILED: {err}", flush=True)
        send_whatsapp(f"❌ LIVE BUY mislukt\nMarket: {market}\nInzet: €{amount_eur:.2f}\nFout: {err}\nID: {prebuy_id}")
        return {"ok": False, "reason": "BITVAVO_BUY_FAILED", "error": err}

    # state opslaan zodat trade_monitor kan SELL'en
    state = load_state()
    state.setdefault("open_trades", {})
    state["open_trades"][prebuy_id] = {
        "status": "OPEN",
        "prebuy_id": prebuy_id,
        "symbol": symbol,
        "market": market,
        "amount_eur": float(amount_eur),
        "base_qty": float(base_qty),
        "avg_entry_price": float(avg_price),
        "stop_loss": float(stop_loss),
        "target": float(target),
        "opened_at": int(time.time()),
        "buy_order": order,
    }
    save_state(state)

    log_trade(market, "BUY", base_qty, float(amount_eur), avg_price, order_id, meta=f"prebuy={prebuy_id}")

    msg = (
        f"✅ LIVE BUY uitgevoerd (Bitvavo)\n"
        f"Market: {market}\n"
        f"Inzet: €{amount_eur:.2f}\n"
        f"Qty: {base_qty:.12f}\n"
        f"Avg entry: {avg_price:.8f}\n"
        f"Stop: {stop_loss:.6f}\n"
        f"Target: {target:.6f}\n"
        f"OrderId: {order_id}\n"
        f"Pre-BUY ID: {prebuy_id}"
    )
    send_whatsapp(msg)

    return {
        "ok": True,
        "prebuy_id": prebuy_id,
        "market": market,
        "order_id": order_id,
        "qty": base_qty,
        "avg_price": avg_price
    }


# =========================================
# LIVE SELL (AANGESTUURD DOOR trade_monitor)
# =========================================
def sell(prebuy_id: str, fraction: float = 1.0) -> Dict[str, Any]:
    """
    Live SELL op basis van prebuy_id (zodat we exact weten welke trade).
    fraction: 1.0 = 100%, 0.4 = 40% etc.
    """
    state = load_state()
    open_trades = state.get("open_trades", {}) or {}

    trade = open_trades.get(prebuy_id)
    if not trade:
        return {"ok": False, "reason": "NO_OPEN_TRADE_FOR_ID"}

    market = trade["market"]
    qty_total = float(trade.get("base_qty") or 0.0)
    if qty_total <= 0:
        return {"ok": False, "reason": "INVALID_QTY"}

    fraction = max(0.0, min(1.0, fraction))
    if fraction <= 0:
        return {"ok": False, "reason": "INVALID_FRACTION"}

    qty_sell = qty_total * fraction

    try:
        order = bitvavo_market_sell_base(market, qty_sell)
        sold_qty, avg_price = extract_qty_and_avg_price(order)
        order_id = str(order.get("orderId") or order.get("id") or "unknown")
    except Exception as e:
        err = str(e)
        print(f"❌ LIVE SELL FAILED: {err}", flush=True)
        send_whatsapp(f"❌ LIVE SELL mislukt\nMarket: {market}\nQty: {qty_sell:.12f}\nFout: {err}\nID: {prebuy_id}")
        return {"ok": False, "reason": "BITVAVO_SELL_FAILED", "error": err}

    # update state: reduce qty or close
    qty_left = max(0.0, qty_total - qty_sell)
    trade["base_qty"] = qty_left
    trade.setdefault("sell_orders", [])
    trade["sell_orders"].append(order)

    if qty_left <= 1e-12:
        trade["status"] = "CLOSED"
        trade["closed_at"] = int(time.time())

    open_trades[prebuy_id] = trade
    state["open_trades"] = open_trades
    save_state(state)

    log_trade(market, "SELL", sold_qty or qty_sell, 0.0, avg_price, order_id, meta=f"fraction={fraction};prebuy={prebuy_id}")

    msg = (
        f"💰 LIVE SELL uitgevoerd (Bitvavo)\n"
        f"Market: {market}\n"
        f"Fraction: {fraction*100:.0f}%\n"
        f"Qty sold: {(sold_qty or qty_sell):.12f}\n"
        f"Avg price: {avg_price:.8f}\n"
        f"Qty left: {qty_left:.12f}\n"
        f"OrderId: {order_id}\n"
        f"Pre-BUY ID: {prebuy_id}"
    )
    send_whatsapp(msg)

    return {
        "ok": True,
        "prebuy_id": prebuy_id,
        "market": market,
        "order_id": order_id,
        "qty_sold": (sold_qty or qty_sell),
        "qty_left": qty_left,
        "avg_price": avg_price
    }


if __name__ == "__main__":
    print("✅ LIVE trader (in paper_trader.py) klaar", flush=True)
    print(f"STATE_PATH: {STATE_PATH}", flush=True)
    print(f"LOG_PATH:   {LOG_PATH}", flush=True)
    print(f"APPROVALS:  {APPROVALS_PATH}", flush=True)
