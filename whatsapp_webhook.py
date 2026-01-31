from __future__ import annotations

from flask import Flask, request, Response
import os
import sys
import json
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

# ============================================================
# PROJECT ROOT (Render + lokaal IDENTIEK)
# whatsapp_webhook.py staat in de repo-root: crypto_ai/
# -> /opt/render/project/src (Render)
# ============================================================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from trading.paper_trader import buy_eur, get_price  # noqa: E402

app = Flask(__name__)

# ============================================================
# PATHS (ALTijd dezelfde file als multi_coin_score)
# ============================================================
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
PENDING_PATH = os.path.join(DATA_DIR, "pending_approvals.json")

# ============================================================
# SETTINGS
# ============================================================
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}
STOP_PCT = 0.02   # 2% onder entry
RR_TARGET = 2.0   # target = 2R

STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_CONSUMED = "CONSUMED"
STATUS_REJECTED = "REJECTED"
STATUS_ERROR = "ERROR"

# ============================================================
# FILE HELPERS
# ============================================================
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

# ============================================================
# TWIML (Twilio response)
# ============================================================
def twiml(msg: str) -> Response:
    msg = str(msg).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Message>{msg}</Message>
</Response>"""
    return Response(xml, mimetype="application/xml")

# ============================================================
# HELPERS
# ============================================================
def now_s() -> int:
    return int(time.time())

def is_expired(expires_at: Any) -> bool:
    """supports seconds or ms"""
    try:
        x = int(expires_at)
    except Exception:
        return False
    if x > 10**12:  # ms -> sec
        x = int(x / 1000)
    return x < now_s()

def normalize_status(x: Any) -> str:
    return str(x or "").strip().upper()

def find_latest_pending(pending: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for p in reversed(pending):
        if normalize_status(p.get("status")) == STATUS_PENDING and not is_expired(p.get("expires_at", 0)):
            return p
    return None

def find_by_id(pending: List[Dict[str, Any]], pid: str) -> Optional[Dict[str, Any]]:
    pid = str(pid or "").strip()
    if not pid:
        return None
    for p in pending:
        if str(p.get("id", "")).strip() == pid:
            return p
    return None

def parse_yes(body: str) -> Tuple[Optional[int], Optional[str]]:
    """
    YES 10
    YES 10 PB-TEST-123
    """
    parts = body.strip().split()
    if len(parts) >= 2 and parts[0].upper() == "YES" and parts[1].isdigit():
        amount = int(parts[1])
        pid = parts[2].strip() if len(parts) >= 3 else None
        return amount, pid
    return None, None

def parse_no(body: str) -> Optional[str]:
    """
    NO
    NO PB-TEST-123
    """
    parts = body.strip().split()
    if parts and parts[0].upper() == "NO":
        return parts[1].strip() if len(parts) >= 2 else None
    return None

def compute_stop_target(entry: float) -> Tuple[float, float]:
    stop = entry * (1.0 - STOP_PCT)
    r = entry - stop
    target = entry + (RR_TARGET * r)
    return float(stop), float(target)

# ============================================================
# ROUTES
# ============================================================
@app.get("/")
def health():
    return "OK - whatsapp_webhook running", 200

@app.post("/whatsapp")
def whatsapp():
    try:
        body = (request.values.get("Body") or "").strip()
        sender = (request.values.get("From") or "").strip()

        if not body:
            return twiml("Leeg bericht. Stuur HELP.")

        pending = load_pending()
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

        # LIST
        if up == "LIST":
            items = [
                p for p in pending
                if normalize_status(p.get("status")) == STATUS_PENDING and not is_expired(p.get("expires_at", 0))
            ]
            if not items:
                return twiml("Geen PENDING Pre-BUY’s gevonden.")

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
                return twiml("❌ Geen PENDING Pre-BUY gevonden om af te wijzen.")
            if normalize_status(item.get("status")) != STATUS_PENDING:
                return twiml(f"⚠️ Niet meer PENDING ({item.get('status','?')}).")

            item["status"] = STATUS_REJECTED
            item["rejected_at"] = now_s()
            item["rejected_by"] = sender
            save_pending(pending)
            return twiml(f"❌ Afgewezen: {item.get('coin','?')} ({item.get('id','?')}).")

        # YES
        amount, pid_yes = parse_yes(body)
        if amount is None:
            return twiml("Onbekend commando. Stuur HELP.")

        if amount not in ALLOWED_AMOUNTS:
            return twiml("⛔ Ongeldig bedrag. Gebruik: 5,10,15,20,30,100.")

        item = find_by_id(pending, pid_yes) if pid_yes else find_latest_pending(pending)
        if not item:
            return twiml("❌ Geen PENDING Pre-BUY gevonden. Stuur eerst LIST.")

        status = normalize_status(item.get("status"))
        if status == STATUS_CONSUMED:
            return twiml(f"⚠️ Al gebruikt (CONSUMED). ID: {item.get('id','?')}")
        if status != STATUS_PENDING:
            return twiml(f"⚠️ Niet meer PENDING ({item.get('status','?')}).")
        if is_expired(item.get("expires_at", 0)):
            return twiml("⚠️ Deze Pre-BUY is verlopen. Wacht op een nieuwe.")

        coin = item.get("coin")
        if not coin:
            return twiml("⚠️ Pre-BUY mist 'coin'. Check pending_approvals.json.")

        # 1) APPROVED (audit-proof)
        item["status"] = STATUS_APPROVED
        item["approved_amount"] = float(amount)
        item["approved_at"] = now_s()
        item["approved_by"] = sender
        save_pending(pending)

        # 2) BUY (paper)
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
            item["error_at"] = now_s()
            save_pending(pending)
            return twiml(f"⛔ BUY mislukt: {item.get('error_reason','UNKNOWN')}")

        # 3) CONSUMED (idempotency)
        item["status"] = STATUS_CONSUMED
        item["consumed_at"] = now_s()
        item["entry"] = round(entry, 8)
        item["stop_loss"] = round(stop, 8)
        item["target"] = round(target, 8)
        item["qty"] = round(float(buy_res.get("qty", 0.0)), 10)
        item["trade_id"] = buy_res.get("trade_id")
        save_pending(pending)

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
        return twiml(f"⚠️ Interne fout in webhook: {e}")

if __name__ == "__main__":
    # Render gebruikt PORT environment variable:
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
