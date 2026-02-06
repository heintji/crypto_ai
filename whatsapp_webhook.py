from __future__ import annotations

from flask import Flask, request, Response, jsonify
import os
import sys
import json
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import requests

# ==========================================================
# PROJECT ROOT (whatsapp_webhook.py staat in project-root)
# ==========================================================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Paper trader alleen hier gebruiken voor BUY (jouw structuur)
# LET OP: we gebruiken buy_eur, maar maken de call "compatibel" met meerdere signatures.
from trading.paper_trader import buy_eur  # noqa: E402

app = Flask(__name__)

# ==========================================================
# ENV
# ==========================================================
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "").strip()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
WHATSAPP_FROM = os.getenv("WHATSAPP_FROM", "").strip()  # bv: "whatsapp:+14155238886"
WHATSAPP_TO = os.getenv("WHATSAPP_TO", "").strip()      # bv: "whatsapp:+316..."

# Render Disk pad (BELANGRIJK: /data)
DATA_DIR = (os.getenv("DATA_DIR") or "/data").strip()
PENDING_PATH = os.path.join(DATA_DIR, "pending_approvals.json")

# ==========================================================
# SETTINGS
# ==========================================================
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

STOP_PCT = 0.02
RR_TARGET = 2.0

STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_CONSUMED = "CONSUMED"
STATUS_REJECTED = "REJECTED"
STATUS_ERROR = "ERROR"

# Binance price (voor entry/stop/target berekening in webhook)
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
HTTP_TIMEOUT = 15

# ==========================================================
# FILE HELPERS
# ==========================================================
def ensure_file() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.isfile(PENDING_PATH):
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2, ensure_ascii=False)

def load_pending() -> List[Dict[str, Any]]:
    ensure_file()
    try:
        with open(PENDING_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def save_pending(data: List[Dict[str, Any]]) -> None:
    ensure_file()
    tmp = PENDING_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, PENDING_PATH)

# ==========================================================
# TWIML (reply naar Twilio inbound)
# ==========================================================
def twiml(msg: str) -> Response:
    msg = str(msg).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Message>{msg}</Message>
</Response>"""
    return Response(xml, mimetype="application/xml")

# ==========================================================
# LOGGING
# ==========================================================
def log_event(event: str, details: dict) -> None:
    try:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{now}] {event}: {json.dumps(details, ensure_ascii=False)}", flush=True)
    except Exception:
        print(f"\n[{time.time()}] {event}: (log fail)", flush=True)

# ==========================================================
# TIME HELPERS
# ==========================================================
def _to_int_seconds(ts: Any) -> Optional[int]:
    try:
        x = int(ts)
    except Exception:
        return None
    # ms support
    if x > 10**12:
        x = int(x / 1000)
    return x

def is_expired(expires_at: Any) -> bool:
    now_s = int(time.time())
    x = _to_int_seconds(expires_at)
    if x is None:
        return False
    return x < now_s

def remaining_text(expires_at: Any) -> str:
    now_s = int(time.time())
    x = _to_int_seconds(expires_at)
    if x is None:
        return "onbekend"
    left = x - now_s
    if left <= 0:
        return "verlopen"
    mins = left // 60
    if mins < 60:
        return f"nog {mins} min"
    hours = mins // 60
    rem_m = mins % 60
    if rem_m == 0:
        return f"nog {hours}u"
    return f"nog {hours}u {rem_m}m"

# ==========================================================
# PENDING HELPERS
# ==========================================================
def find_by_id(pending: List[Dict[str, Any]], pid: str) -> Optional[Dict[str, Any]]:
    pid = str(pid or "").strip()
    for p in pending:
        if str(p.get("id", "")).strip() == pid:
            return p
    return None

def parse_yes(body: str) -> Tuple[Optional[int], Optional[str]]:
    parts = body.strip().split()
    if len(parts) >= 2 and parts[0].upper() == "YES" and parts[1].isdigit():
        amount = int(parts[1])
        pid = parts[2].strip() if len(parts) >= 3 else None
        return amount, pid
    return None, None

def parse_no(body: str) -> Optional[str]:
    parts = body.strip().split()
    if parts and parts[0].upper() == "NO":
        return parts[1].strip() if len(parts) >= 2 else None
    return None

def compute_stop_target(entry: float) -> Tuple[float, float]:
    stop = entry * (1.0 - STOP_PCT)
    r = entry - stop
    target = entry + (RR_TARGET * r)
    return float(stop), float(target)

def fetch_price(symbol: str) -> float:
    r = requests.get(BINANCE_TICKER_URL, params={"symbol": symbol}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return float(r.json()["price"])

# ==========================================================
# TWILIO OUTBOUND (Pre-BUY push)
# ==========================================================
def twilio_ready() -> bool:
    return all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, WHATSAPP_FROM, WHATSAPP_TO])

def send_whatsapp_outbound(message: str) -> bool:
    if not twilio_ready():
        log_event("TWILIO_NOT_READY", {
            "has_sid": bool(TWILIO_ACCOUNT_SID),
            "has_token": bool(TWILIO_AUTH_TOKEN),
            "has_from": bool(WHATSAPP_FROM),
            "has_to": bool(WHATSAPP_TO),
        })
        return False

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    try:
        r = requests.post(
            url,
            data={"From": WHATSAPP_FROM, "To": WHATSAPP_TO, "Body": message},
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            timeout=20,
        )
        ok = 200 <= r.status_code < 300
        if not ok:
            log_event("TWILIO_SEND_FAIL", {"status": r.status_code, "text": r.text[:250]})
        return ok
    except Exception as e:
        log_event("TWILIO_SEND_ERROR", {"error": str(e)})
        return False

def format_prebuy_push(p: Dict[str, Any]) -> str:
    coin = p.get("coin", "?")
    score = p.get("score", "?")
    kans = p.get("kans", "")
    entry = p.get("entry", 0)
    stop = p.get("stop_loss", 0)
    target = p.get("target", 0)
    pid = p.get("id", "?")
    exp = remaining_text(p.get("expires_at", 0))

    try:
        entry_f = f"{float(entry):.6f}"
    except Exception:
        entry_f = str(entry)
    try:
        stop_f = f"{float(stop):.6f}"
    except Exception:
        stop_f = str(stop)
    try:
        target_f = f"{float(target):.6f}"
    except Exception:
        target_f = str(target)

    kans_txt = f" ({kans})" if kans else ""
    return (
        "📊 PRE-BUY GEVONDEN\n"
        f"Coin: {coin}\n"
        f"Score: {score}{kans_txt}\n"
        f"Entry: {entry_f}\n"
        f"Stop: {stop_f}\n"
        f"Target: {target_f}\n\n"
        f"⏳ Geldig: {exp}\n"
        f"ID: {pid}\n\n"
        "Bevestig: YES <bedrag> <ID>\n"
        f"Voorbeeld: YES 10 {pid}"
    )

# ==========================================================
# INTERNAL ENDPOINT (multi_coin_score -> POST /internal/prebuy)
# ==========================================================
@app.post("/internal/prebuy")
def internal_prebuy():
    try:
        token = (request.headers.get("X-Internal-Token") or "").strip()
        if not INTERNAL_TOKEN:
            return jsonify({"ok": False, "error": "INTERNAL_TOKEN not set on web service"}), 500
        if token != INTERNAL_TOKEN:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "invalid json"}), 400

        pid = str(data.get("id", "")).strip()
        coin = str(data.get("coin", "")).strip()
        status = str(data.get("status", "PENDING")).upper().strip()
        expires_at = data.get("expires_at", 0)

        if not pid or not coin:
            return jsonify({"ok": False, "error": "missing id/coin"}), 400

        if status not in {STATUS_PENDING, STATUS_APPROVED, STATUS_CONSUMED, STATUS_REJECTED, STATUS_ERROR}:
            status = STATUS_PENDING

        pending = load_pending()

        # idempotent: geen duplicates
        exists = any(str(p.get("id", "")).strip() == pid for p in pending)
        if exists:
            log_event("INTERNAL_PREBUY_DUPLICATE", {"id": pid, "coin": coin})
            return jsonify({"ok": True, "duplicate": True}), 200

        if "created_at" not in data:
            data["created_at"] = int(time.time())

        # expiry default 4 uur
        ex = _to_int_seconds(expires_at)
        if ex is None:
            ex = int(time.time()) + 4 * 60 * 60
        data["expires_at"] = ex
        data["status"] = status

        pending.append(data)
        save_pending(pending)

        log_event("INTERNAL_PREBUY_SAVED", {"id": pid, "coin": coin, "pending_file": PENDING_PATH})

        pushed = False
        if status == STATUS_PENDING and not is_expired(ex):
            msg = format_prebuy_push(data)
            pushed = send_whatsapp_outbound(msg)
            log_event("INTERNAL_PREBUY_PUSH", {"id": pid, "ok": pushed})

        return jsonify({"ok": True, "pushed": pushed}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

# ==========================================================
# HEALTH
# ==========================================================
@app.get("/")
def health():
    return "OK - whatsapp_webhook running", 200

# ==========================================================
# COMPAT BUY CALL (WATERDICHT)
# ==========================================================
def execute_buy_compat(
    coin: str,
    amount_eur: float,
    entry: float,
    stop: float,
    target: float,
    prebuy_id: str,
) -> Dict[str, Any]:
    """
    Maakt BUY-call compatibel met meerdere buy_eur signatures.
    - Eerst proberen met keywords (incl. price)
    - Als dat faalt: fallback naar positional call
    """
    # 1) keyword poging (nieuwere/uitgebreidere signature)
    try:
        return buy_eur(
            symbol=coin,
            price=entry,              # <-- dit gaf bij jou de crash, dus vangen we op
            amount_eur=float(amount_eur),
            stop_loss=float(stop),
            target=float(target),
            prebuy_id=prebuy_id,
        )
    except TypeError as te:
        log_event("BUY_CALL_FALLBACK", {"coin": coin, "error": str(te), "mode": "positional"})
        # 2) fallback: positional (oudere signature)
        # Verwachte volgorde: (symbol, price, amount_eur, stop_loss, target, prebuy_id)
        return buy_eur(coin, entry, float(amount_eur), float(stop), float(target), prebuy_id)

# ==========================================================
# WHATSAPP WEBHOOK (Twilio -> POST /whatsapp)
# ==========================================================
@app.post("/whatsapp")
def whatsapp():
    try:
        body = (request.values.get("Body") or "").strip()
        sender = (request.values.get("From") or "").strip()

        log_event("WHATSAPP_IN", {"from": sender, "body": body, "pending_file": PENDING_PATH})

        if not body:
            return twiml("Leeg bericht. Stuur HELP.")

        up = body.upper().strip()
        pending = load_pending()

        def active_pending() -> List[Dict[str, Any]]:
            return [
                p for p in pending
                if str(p.get("status", "")).upper() == STATUS_PENDING and not is_expired(p.get("expires_at", 0))
            ]

        # HELP
        if up == "HELP":
            return twiml(
                "Crypto_AI — Commands:\n"
                "• LIST\n"
                "• TOP\n"
                "• YES 5|10|15|20|30|100 <PREBUY-ID>\n"
                "• NO <PREBUY-ID>\n\n"
                "Voorbeeld:\n"
                "YES 10 PB-BTCUSDT-123"
            )

        # LIST
        if up == "LIST":
            items = active_pending()
            if not items:
                return twiml(f"Geen PENDING Pre-BUY’s gevonden.\n(ik lees: {PENDING_PATH})")

            items = items[-10:]
            lines = [f"📋 BESCHIKBARE PRE-BUY'S ({len(items)})", ""]
            for i, p in enumerate(items, start=1):
                coin = p.get("coin", "?")
                score = p.get("score", "?")
                pid = p.get("id", "?")
                exp = remaining_text(p.get("expires_at", 0))
                lines.append(f"{i}) {coin} | score {score}")
                lines.append(f"⏳ {exp}")
                lines.append(f"ID: {pid}")
                lines.append("")
            lines.append("Bevestig: YES <bedrag> <ID>")
            lines.append("Weiger: NO <ID>")
            return twiml("\n".join(lines).strip())

        # TOP
        if up == "TOP":
            items = active_pending()
            if not items:
                return twiml("Geen PENDING Pre-BUY’s gevonden. Stuur LIST.")

            def score_val(p: Dict[str, Any]) -> float:
                try:
                    return float(p.get("score", 0))
                except Exception:
                    return 0.0

            items_sorted = sorted(items, key=score_val, reverse=True)[:3]
            lines = ["🏆 TOP 3 BESTE PRE-BUY'S", ""]
            for i, p in enumerate(items_sorted, start=1):
                coin = p.get("coin", "?")
                score = p.get("score", "?")
                pid = p.get("id", "?")
                exp = remaining_text(p.get("expires_at", 0))
                lines.append(f"{i}) {coin} | score {score}")
                lines.append(f"⏳ {exp}")
                lines.append(f"ID: {pid}")
                lines.append("")
            lines.append("Bevestig: YES <bedrag> <ID>")
            return twiml("\n".join(lines).strip())

        # NO
        pid_no = parse_no(body)
        if pid_no is not None:
            if not pid_no:
                return twiml("⚠️ Geef een ID mee.\nVoorbeeld: NO PB-BTCUSDT-123\nTip: stuur LIST.")
            item = find_by_id(pending, pid_no)
            if not item:
                return twiml("❌ ID niet gevonden. Stuur LIST.")

            st = str(item.get("status", "")).upper()
            if st == STATUS_CONSUMED:
                return twiml("⚠️ Deze Pre-BUY is al gebruikt (CONSUMED).")
            if st == STATUS_REJECTED:
                return twiml("⚠️ Deze Pre-BUY was al afgewezen (REJECTED).")

            item["status"] = STATUS_REJECTED
            item["rejected_at"] = int(time.time())
            save_pending(pending)

            log_event("PREBUY_REJECTED", {"id": item.get("id"), "coin": item.get("coin"), "from": sender})
            return twiml(f"❌ Pre-BUY afgewezen\nID: {item.get('id','?')}")

        # YES
        amount, pid_yes = parse_yes(body)
        if amount is None:
            return twiml("Onbekend bericht. Stuur HELP voor commands.")

        if amount not in ALLOWED_AMOUNTS:
            return twiml("⛔ Ongeldig bedrag. Gebruik: 5,10,15,20,30,100.")

        if not pid_yes:
            return twiml("⚠️ Geef een ID mee.\nVoorbeeld: YES 10 PB-BTCUSDT-123\nTip: stuur LIST of TOP.")

        item = find_by_id(pending, pid_yes)
        if not item:
            return twiml("❌ ID niet gevonden. Stuur LIST.")

        status = str(item.get("status", "")).upper()

        # ✅ Belangrijk: maak APPROVED/ERROR retrybaar, zodat je niet vastloopt.
        if status == STATUS_CONSUMED:
            return twiml(f"⚠️ Deze Pre-BUY is al gebruikt (CONSUMED).\nID: {item.get('id','?')}")
        if status == STATUS_REJECTED:
            return twiml(f"⚠️ Deze Pre-BUY is afgewezen (REJECTED).\nID: {item.get('id','?')}")

        if is_expired(item.get("expires_at", 0)):
            return twiml("⚠️ Deze Pre-BUY is verlopen. Wacht op een nieuwe.")

        coin = str(item.get("coin") or "").strip()
        if not coin:
            return twiml("⚠️ Pre-BUY mist 'coin'. Check storage.")

        # 1) Zet naar APPROVED (audit-proof) - óók als hij al APPROVED was, updaten we bedrag.
        item["status"] = STATUS_APPROVED
        item["approved_amount"] = float(amount)
        item["approved_at"] = int(time.time())
        item["last_attempt_at"] = int(time.time())
        item.pop("error_reason", None)
        item.pop("error_at", None)
        save_pending(pending)

        # 2) BUY uitvoeren
        try:
            entry = float(fetch_price(coin))
        except Exception as e:
            item["status"] = STATUS_ERROR
            item["error_reason"] = f"PRICE_FETCH_FAIL: {e}"
            item["error_at"] = int(time.time())
            save_pending(pending)
            log_event("BUY_ERROR_PRICE", {"id": item.get("id"), "coin": coin, "error": str(e), "from": sender})
            return twiml("⛔ Kan prijs niet ophalen (Binance). Probeer later opnieuw.")

        stop, target = compute_stop_target(entry)

        buy_res = execute_buy_compat(
            coin=coin,
            amount_eur=float(amount),
            entry=entry,
            stop=stop,
            target=target,
            prebuy_id=str(item.get("id")),
        )

        if not isinstance(buy_res, dict) or not buy_res.get("ok"):
            # ✅ Zet naar ERROR maar laat het RETRYBAAR: gebruiker kan opnieuw YES sturen.
            item["status"] = STATUS_ERROR
            item["error_reason"] = (buy_res or {}).get("reason", "UNKNOWN")
            item["error_at"] = int(time.time())
            item["entry"] = round(entry, 8)
            item["stop_loss"] = round(stop, 8)
            item["target"] = round(target, 8)
            save_pending(pending)

            log_event("BUY_ERROR", {"id": item.get("id"), "coin": coin, "reason": item.get("error_reason"), "from": sender})
            return twiml(
                f"⛔ BUY mislukt: {item.get('error_reason','UNKNOWN')}\n"
                f"Je mag opnieuw proberen met:\n"
                f"YES {amount} {item.get('id','?')}"
            )

        # 3) CONSUMED (definitief)
        item["status"] = STATUS_CONSUMED
        item["consumed_at"] = int(time.time())
        item["entry"] = round(entry, 8)
        item["stop_loss"] = round(stop, 8)
        item["target"] = round(target, 8)
        item["qty"] = round(float(buy_res.get("qty", 0.0)), 10)
        item["trade_id"] = buy_res.get("trade_id")
        save_pending(pending)

        log_event("BUY_OK", {"id": item.get("id"), "coin": coin, "amount": amount, "trade_id": item.get("trade_id"), "from": sender})

        return twiml(
            f"✅ BUY UITGEVOERD\n"
            f"Coin: {coin}\n"
            f"Inzet: €{amount}\n"
            f"Entry: {entry:.6f}\n"
            f"Stop: {stop:.6f}\n"
            f"Target: {target:.6f}\n\n"
            f"ID: {item.get('id','?')}"
        )

    except Exception as e:
        traceback.print_exc()
        log_event("WEBHOOK_FATAL", {"error": str(e)})
        return twiml("⚠️ Interne fout in webhook. Check Render logs.")

# ==========================================================
# LOCAL TEST
# ==========================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
