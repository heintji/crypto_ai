from __future__ import annotations

from flask import Flask, request, Response, jsonify
import os
import sys
import json
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

# ==========================================================
# PROJECT ROOT (whatsapp_webhook.py staat in project-root)
# ==========================================================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Paper trader alleen hier gebruiken voor BUY (jouw structuur)
from trading.paper_trader import buy_eur, get_price  # noqa: E402

app = Flask(__name__)

# ==========================================================
# ENV
# ==========================================================
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "").strip()  # moet op Render gezet worden (Web Service)

# ==========================================================
# PATHS (alleen binnen deze Web Service container)
# ==========================================================
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
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

# ==========================================================
# FILE HELPERS
# ==========================================================
def ensure_file() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.isfile(PENDING_PATH):
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)

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
# TWIML
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
        print(f"\n[{now}] {event}: {json.dumps(details, ensure_ascii=False)}")
    except Exception:
        print(f"\n[{time.time()}] {event}: (log fail)")

# ==========================================================
# HELPERS
# ==========================================================
def is_expired(expires_at: Any) -> bool:
    now_s = int(time.time())
    try:
        x = int(expires_at)
    except Exception:
        return False
    # ms support
    if x > 10**12:
        x = int(x / 1000)
    return x < now_s

def find_latest_pending(pending: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for p in reversed(pending):
        if str(p.get("status", "")).upper() == STATUS_PENDING and not is_expired(p.get("expires_at", 0)):
            return p
    return None

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

# ==========================================================
# INTERNAL ENDPOINT (hier komt multi_coin_score binnen)
# ==========================================================
@app.post("/internal/prebuy")
def internal_prebuy():
    """
    multi_coin_score.py -> POST /internal/prebuy
    Header: X-Internal-Token: <INTERNAL_TOKEN>
    JSON body: prebuy dict
    """
    try:
        token = (request.headers.get("X-Internal-Token") or "").strip()
        if not INTERNAL_TOKEN:
            return jsonify({"ok": False, "error": "INTERNAL_TOKEN not set on web service"}), 500
        if token != INTERNAL_TOKEN:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "invalid json"}), 400

        # basisvalidatie
        pid = str(data.get("id", "")).strip()
        coin = str(data.get("coin", "")).strip()
        status = str(data.get("status", "PENDING")).upper().strip()
        expires_at = data.get("expires_at", 0)

        if not pid or not coin:
            return jsonify({"ok": False, "error": "missing id/coin"}), 400

        if status not in {STATUS_PENDING, STATUS_APPROVED, STATUS_CONSUMED, STATUS_REJECTED, STATUS_ERROR}:
            status = STATUS_PENDING

        pending = load_pending()

        # voorkom duplicates (idempotent)
        exists = any(str(p.get("id", "")).strip() == pid for p in pending)
        if exists:
            log_event("INTERNAL_PREBUY_DUPLICATE", {"id": pid, "coin": coin})
            return jsonify({"ok": True, "duplicate": True}), 200

        # force velden netjes
        data["status"] = status
        if "created_at" not in data:
            data["created_at"] = int(time.time())

        # expiry check: als missing, geef default 4 uur
        try:
            ex = int(expires_at)
        except Exception:
            ex = int(time.time()) + 4 * 60 * 60
        data["expires_at"] = ex

        pending.append(data)
        save_pending(pending)

        log_event("INTERNAL_PREBUY_SAVED", {"id": pid, "coin": coin, "pending_file": PENDING_PATH})
        return jsonify({"ok": True}), 200

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

        # HELP
        if up == "HELP":
            return twiml(
                "Crypto_AI — Commands:\n"
                "• LIST\n"
                "• YES 5|10|15|20|30|100 [PREBUY-ID]\n"
                "• NO [PREBUY-ID]\n\n"
                "Voorbeeld:\n"
                "YES 10 PB-TEST-001"
            )

        # LIST
        if up == "LIST":
            items = [
                p for p in pending
                if str(p.get("status", "")).upper() == STATUS_PENDING and not is_expired(p.get("expires_at", 0))
            ]
            if not items:
                return twiml(f"Geen PENDING Pre-BUY’s gevonden.\n(ik lees: {PENDING_PATH})")

            last = items[-10:]
            lines = ["PENDING Pre-BUY’s (laatste 10):"]
            for p in last:
                lines.append(f"- {p.get('id','?')} | {p.get('coin','?')} | score={p.get('score','?')}")
            return twiml("\n".join(lines))

        # NO
        pid_no = parse_no(body)
        if pid_no is not None:
            item = find_by_id(pending, pid_no) if pid_no else find_latest_pending(pending)
            if not item:
                return twiml("❌ Geen PENDING Pre-BUY gevonden om af te wijzen. Stuur LIST.")

            if str(item.get("status", "")).upper() != STATUS_PENDING:
                return twiml(f"⚠️ Deze Pre-BUY is niet meer PENDING ({item.get('status','?')}).")

            item["status"] = STATUS_REJECTED
            item["rejected_at"] = int(time.time())
            save_pending(pending)

            log_event("PREBUY_REJECTED", {"id": item.get("id"), "coin": item.get("coin"), "from": sender})
            return twiml(f"❌ Afgewezen: {item.get('coin','?')} ({item.get('id','?')}).")

        # YES
        amount, pid_yes = parse_yes(body)
        if amount is None:
            return twiml("Onbekend bericht. Stuur HELP voor commands.")

        if amount not in ALLOWED_AMOUNTS:
            return twiml("⛔ Ongeldig bedrag. Gebruik: 5,10,15,20,30,100.")

        item = find_by_id(pending, pid_yes) if pid_yes else find_latest_pending(pending)
        if not item:
            return twiml("❌ Geen PENDING Pre-BUY gevonden. Stuur eerst LIST.")

        status = str(item.get("status", "")).upper()

        if status == STATUS_CONSUMED:
            return twiml(f"⚠️ Deze Pre-BUY is al gebruikt (CONSUMED). ID: {item.get('id','?')}")
        if status != STATUS_PENDING:
            return twiml(f"⚠️ Deze Pre-BUY is niet meer PENDING ({item.get('status','?')}).")

        if is_expired(item.get("expires_at", 0)):
            return twiml("⚠️ Deze Pre-BUY is verlopen. Wacht op een nieuwe.")

        coin = str(item.get("coin") or "").strip()
        if not coin:
            return twiml("⚠️ Pre-BUY mist 'coin'. Check storage.")

        # 1) APPROVED opslaan (audit-proof)
        item["status"] = STATUS_APPROVED
        item["approved_amount"] = float(amount)
        item["approved_at"] = int(time.time())
        save_pending(pending)

        # 2) BUY uitvoeren (paper)
        entry = float(get_price(coin))
        stop, target = compute_stop_target(entry)

        buy_res = buy_eur(
            symbol=coin,
            price=entry,
            amount_eur=float(amount),
            stop_loss=stop,
            target=target,
            prebuy_id=item.get("id"),
        )

        if not isinstance(buy_res, dict) or not buy_res.get("ok"):
            item["status"] = STATUS_ERROR
            item["error_reason"] = (buy_res or {}).get("reason", "UNKNOWN")
            item["error_at"] = int(time.time())
            save_pending(pending)

            log_event("BUY_ERROR", {"id": item.get("id"), "coin": coin, "reason": item.get("error_reason"), "from": sender})
            return twiml(f"⛔ BUY mislukt: {item.get('error_reason','UNKNOWN')}")

        # 3) CONSUMED
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
            f"✅ BUY uitgevoerd ({coin})\n"
            f"Inzet: €{amount}\n"
            f"Entry: {entry:.6f}\n"
            f"Stop: {stop:.6f}\n"
            f"Target: {target:.6f}\n"
            f"ID: {item.get('id','?')}"
        )

    except Exception as e:
        traceback.print_exc()
        log_event("WEBHOOK_FATAL", {"error": str(e)})
        return twiml("⚠️ Interne fout in webhook. Check Render logs.")

# ==========================================================
# TEST ONDERAAN LATEN STAAN (zoals jij wil)
# ==========================================================
if __name__ == "__main__":
    # In Render draait dit via start command (gunicorn/uvicorn of python)
    # Lokaal kan dit prima:
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
