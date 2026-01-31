from __future__ import annotations

from flask import Flask, request, Response
import os
import sys
import json
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

# =========================================================
# PROJECT ROOT (Render + lokaal IDENTIEK)
# - whatsapp_webhook.py staat in de ROOT van je repo.
# - dus ROOT = map waar dit bestand in staat.
# =========================================================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# =========================================================
# IMPORTS (blijven werken door sys.path fix hierboven)
# =========================================================
from trading.paper_trader import buy_eur, get_price  # noqa: E402

app = Flask(__name__)

# =========================================================
# PATHS (BELANGRIJK)
# 1) Standaard: <project_root>/data/pending_approvals.json
# 2) Override mogelijk via ENV: PENDING_FILE
#    - voorbeeld Render env var: PENDING_FILE=data/pending_approvals.json
# =========================================================
DEFAULT_PENDING = os.path.join(PROJECT_ROOT, "data", "pending_approvals.json")
PENDING_PATH = os.getenv("PENDING_FILE", DEFAULT_PENDING)

DATA_DIR = os.path.dirname(PENDING_PATH)

# =========================================================
# SETTINGS
# =========================================================
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

STOP_PCT = float(os.getenv("STOP_PCT", "0.02"))       # 2% onder entry
RR_TARGET = float(os.getenv("RR_TARGET", "2.0"))      # target = 2R

STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_CONSUMED = "CONSUMED"
STATUS_REJECTED = "REJECTED"
STATUS_ERROR = "ERROR"


# =========================================================
# FILE HELPERS
# =========================================================
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


# =========================================================
# TWIML (Twilio response)
# =========================================================
def twiml(msg: str) -> Response:
    msg = str(msg).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Message>{msg}</Message>
</Response>"""
    return Response(xml, mimetype="application/xml")


# =========================================================
# LOGGING (console)
# =========================================================
def log_event(event: str, details: Dict[str, Any]) -> None:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        print(f"\n[{now}] {event}: {json.dumps(details, ensure_ascii=False)}")
    except Exception:
        print(f"\n[{now}] {event}: (log fail)")


# =========================================================
# PENDING HELPERS
# =========================================================
def _to_seconds(ts: Any) -> Optional[int]:
    try:
        x = int(ts)
        # ms -> sec
        if x > 10**12:
            x = int(x / 1000)
        return x
    except Exception:
        return None


def is_expired(expires_at: Any) -> bool:
    now_s = int(time.time())
    x = _to_seconds(expires_at)
    if x is None:
        return False
    return x < now_s


def find_latest_pending(pending_list: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for item in reversed(pending_list):
        if str(item.get("status", "")).upper() == STATUS_PENDING and not is_expired(item.get("expires_at", 0)):
            return item
    return None


def find_by_id(pending_list: List[Dict[str, Any]], prebuy_id: str) -> Optional[Dict[str, Any]]:
    pid = str(prebuy_id or "").strip()
    for item in pending_list:
        if str(item.get("id", "")).strip() == pid:
            return item
    return None


# =========================================================
# INPUT PARSERS
# =========================================================
def parse_yes(body: str) -> Tuple[Optional[int], Optional[str]]:
    """
    Accept:
      YES 10
      YES 10 PREBUY-XXX
    """
    parts = body.strip().split()
    if len(parts) >= 2 and parts[0].upper() == "YES" and parts[1].isdigit():
        amount = int(parts[1])
        prebuy_id = parts[2].strip() if len(parts) >= 3 else None
        return amount, prebuy_id
    return None, None


def parse_no(body: str) -> Optional[str]:
    """
    Accept:
      NO
      NO PREBUY-XXX
    """
    parts = body.strip().split()
    if parts and parts[0].upper() == "NO":
        return parts[1].strip() if len(parts) >= 2 else None
    return None


# =========================================================
# RISK
# =========================================================
def compute_stop_target(entry_price: float) -> Tuple[float, float]:
    stop = entry_price * (1.0 - STOP_PCT)
    r = entry_price - stop
    target = entry_price + (RR_TARGET * r)
    return float(stop), float(target)


# =========================================================
# ROUTES
# =========================================================
@app.get("/")
def health():
    return "OK - whatsapp_webhook running", 200


@app.post("/whatsapp")
def whatsapp():
    try:
        body = (request.values.get("Body") or "").strip()
        sender = (request.values.get("From") or "").strip()

        pending = load_pending()

        log_event("WHATSAPP_IN", {"from": sender, "body": body, "pending_path": PENDING_PATH})

        if not body:
            return twiml("Leeg bericht. Stuur HELP.")

        up = body.upper().strip()

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

        # LIST (alleen niet-verlopen PENDING)
        if up == "LIST":
            pending_items = [
                p for p in pending
                if str(p.get("status", "")).upper() == STATUS_PENDING and not is_expired(p.get("expires_at", 0))
            ]
            if not pending_items:
                return twiml(f"Geen PENDING Pre-BUY’s gevonden.\n(ik lees: {PENDING_PATH})")

            last = pending_items[-10:]
            lines = ["PENDING Pre-BUY’s (laatste 10):"]
            for p in last:
                lines.append(f"- {p.get('id','?')} | {p.get('coin','?')} | score={p.get('score','?')}")
            return twiml("\n".join(lines))

        # NO flow
        prebuy_id_no = parse_no(body)
        if prebuy_id_no is not None:
            item = find_by_id(pending, prebuy_id_no) if prebuy_id_no else find_latest_pending(pending)

            if not item:
                return twiml("❌ Geen pending Pre-BUY gevonden om af te wijzen.")

            if str(item.get("status", "")).upper() != STATUS_PENDING:
                return twiml(f"⚠️ Deze Pre-BUY is niet meer PENDING ({item.get('status','?')}).")

            item["status"] = STATUS_REJECTED
            item["rejected_at"] = int(time.time())
            save_pending(pending)

            log_event("PREBUY_REJECTED", {"id": item.get("id"), "coin": item.get("coin"), "from": sender})
            return twiml(f"❌ Afgewezen: {item.get('coin','?')} ({item.get('id','?')}).")

        # YES flow
        amount, prebuy_id = parse_yes(body)
        if amount is None:
            return twiml("Onbekend bericht. Stuur HELP voor commands.")

        if amount not in ALLOWED_AMOUNTS:
            return twiml("⛔ Ongeldig bedrag. Gebruik: 5,10,15,20,30,100.")

        item = find_by_id(pending, prebuy_id) if prebuy_id else find_latest_pending(pending)
        if not item:
            return twiml("❌ Geen PENDING Pre-BUY gevonden. Stuur eerst LIST.")

        status = str(item.get("status", "")).upper()

        if status == STATUS_CONSUMED:
            return twiml(f"⚠️ Deze Pre-BUY is al gebruikt (CONSUMED). ID: {item.get('id','?')}")
        if status != STATUS_PENDING:
            return twiml(f"⚠️ Deze Pre-BUY is niet meer PENDING ({item.get('status','?')}).")

        if is_expired(item.get("expires_at", 0)):
            return twiml("⚠️ Deze Pre-BUY is verlopen (EXPIRED). Wacht op een nieuwe.")

        coin = item.get("coin")
        if not coin:
            return twiml("⚠️ Pre-BUY mist 'coin' veld. Check pending_approvals.json")

        # 1) APPROVED (audit-proof)
        item["status"] = STATUS_APPROVED
        item["approved_amount"] = float(amount)
        item["approved_at"] = int(time.time())
        save_pending(pending)

        # 2) BUY uitvoeren (paper)
        entry_price = float(get_price(coin))
        stop, target = compute_stop_target(entry_price)

        buy_res = buy_eur(
            symbol=coin,
            price=entry_price,
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

        # 3) CONSUMED (idempotency)
        item["status"] = STATUS_CONSUMED
        item["consumed_at"] = int(time.time())
        item["entry"] = round(entry_price, 8)
        item["stop_loss"] = round(stop, 8)
        item["target"] = round(target, 8)
        item["qty"] = round(float(buy_res.get("qty", 0.0)), 10)
        item["trade_id"] = buy_res.get("trade_id")

        save_pending(pending)

        log_event("BUY_OK", {"id": item.get("id"), "coin": coin, "amount": amount, "trade_id": item.get("trade_id"), "from": sender})

        return twiml(
            f"✅ BUY uitgevoerd ({coin})\n"
            f"Inzet: €{amount}\n"
            f"Entry: {entry_price:.6f}\n"
            f"Stop: {stop:.6f}\n"
            f"Target: {target:.6f}\n"
            f"ID: {item.get('id','?')}"
        )

    except Exception as e:
        traceback.print_exc()
        log_event("WEBHOOK_FATAL", {"error": str(e)})
        return twiml("⚠️ Interne fout in webhook. Check je Render logs.")


if __name__ == "__main__":
    # Render gebruikt PORT env var. Lokaal valt hij terug op 5000.
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
