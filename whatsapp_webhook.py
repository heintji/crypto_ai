from __future__ import annotations

from flask import Flask, request, Response
import os
import sys
import json
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

# =========================================
# PROJECT ROOT (Render + lokaal IDENTIEK)
# =========================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from trading.paper_trader import buy_eur, get_price  # noqa: E402

app = Flask(__name__)

# =========================================
# PATHS  ✅ DIT IS DE FIX
# =========================================
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
PENDING_PATH = os.path.join(DATA_DIR, "pending_approvals.json")

# =========================================
# SETTINGS
# =========================================
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

STOP_PCT = 0.02
RR_TARGET = 2.0

STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_CONSUMED = "CONSUMED"
STATUS_REJECTED = "REJECTED"
STATUS_ERROR = "ERROR"

# =========================================
# FILE HELPERS
# =========================================
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

# =========================================
# TWIML
# =========================================
def twiml(msg: str) -> Response:
    msg = str(msg).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Message>{msg}</Message>
</Response>"""
    return Response(xml, mimetype="application/xml")

# =========================================
# HELPERS
# =========================================
def is_expired(expires_at: Any) -> bool:
    try:
        return int(expires_at) < int(time.time())
    except Exception:
        return False

def find_latest_pending(pending: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for p in reversed(pending):
        if p.get("status") == STATUS_PENDING and not is_expired(p.get("expires_at")):
            return p
    return None

def find_by_id(pending: List[Dict[str, Any]], pid: str) -> Optional[Dict[str, Any]]:
    for p in pending:
        if str(p.get("id")) == str(pid):
            return p
    return None

def parse_yes(body: str) -> Tuple[Optional[int], Optional[str]]:
    parts = body.split()
    if len(parts) >= 2 and parts[0].upper() == "YES" and parts[1].isdigit():
        return int(parts[1]), parts[2] if len(parts) >= 3 else None
    return None, None

def parse_no(body: str) -> Optional[str]:
    parts = body.split()
    if parts and parts[0].upper() == "NO":
        return parts[1] if len(parts) >= 2 else None
    return None

def compute_stop_target(entry: float) -> Tuple[float, float]:
    stop = entry * (1 - STOP_PCT)
    target = entry + (entry - stop) * RR_TARGET
    return stop, target

# =========================================
# ROUTES
# =========================================
@app.get("/")
def health():
    return "OK - whatsapp_webhook running", 200

@app.post("/whatsapp")
def whatsapp():
    try:
        body = (request.values.get("Body") or "").strip()
        pending = load_pending()

        if body.upper() == "HELP":
            return twiml(
                "Crypto_AI — Commands:\n"
                "LIST\n"
                "YES 10 [PREBUY-ID]\n"
                "NO [PREBUY-ID]"
            )

        if body.upper() == "LIST":
            items = [p for p in pending if p.get("status") == STATUS_PENDING]
            if not items:
                return twiml("Geen PENDING Pre-BUY’s gevonden.")
            return twiml("\n".join(f"- {p['id']} | {p['coin']}" for p in items))

        pid_no = parse_no(body)
        if pid_no is not None:
            item = find_by_id(pending, pid_no) if pid_no else find_latest_pending(pending)
            if not item:
                return twiml("Geen pending Pre-BUY gevonden.")
            item["status"] = STATUS_REJECTED
            save_pending(pending)
            return twiml(f"❌ Afgewezen: {item['id']}")

        amount, pid_yes = parse_yes(body)
        if amount is None:
            return twiml("Onbekend commando. Stuur HELP.")

        item = find_by_id(pending, pid_yes) if pid_yes else find_latest_pending(pending)
        if not item:
            return twiml("❌ Geen PENDING Pre-BUY gevonden.")

        item["status"] = STATUS_APPROVED
        save_pending(pending)

        entry = float(get_price(item["coin"]))
        stop, target = compute_stop_target(entry)

        buy_eur(
            symbol=item["coin"],
            price=entry,
            amount_eur=amount,
            stop_loss=stop,
            target=target,
            prebuy_id=item["id"],
        )

        item["status"] = STATUS_CONSUMED
        save_pending(pending)

        return twiml(f"✅ BUY uitgevoerd: {item['coin']} €{amount}")

    except Exception:
        traceback.print_exc()
        return twiml("⚠️ Interne fout")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
