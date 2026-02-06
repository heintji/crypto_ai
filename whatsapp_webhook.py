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
# PATHS (Render Disk!)
# Belangrijk: /data bestaat ALLEEN als je Web Service een Disk mount heeft.
# ==========================================================
ENV_DATA_DIR = (os.getenv("DATA_DIR") or "").strip()
DATA_DIR = ENV_DATA_DIR if ENV_DATA_DIR else "/data"   # default: Render Disk mount path
PENDING_PATH = os.path.join(DATA_DIR, "pending_approvals.json")

# fallback (alleen als /data echt niet kan) -> container data (niet persistent)
FALLBACK_DATA_DIR = os.path.join(PROJECT_ROOT, "data")
FALLBACK_PENDING_PATH = os.path.join(FALLBACK_DATA_DIR, "pending_approvals.json")

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

MAX_LIST_ITEMS = 10
TOP_N = 3

# ==========================================================
# FILE HELPERS
# ==========================================================
def _is_writable_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        testfile = os.path.join(path, ".write_test")
        with open(testfile, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(testfile)
        return True
    except Exception:
        return False

def effective_paths() -> Tuple[str, str]:
    """
    Returns (data_dir, pending_path) that is writable.
    Prefers /data (Render Disk). Falls back to PROJECT_ROOT/data if needed.
    """
    if _is_writable_dir(DATA_DIR):
        return DATA_DIR, PENDING_PATH
    # fallback
    os.makedirs(FALLBACK_DATA_DIR, exist_ok=True)
    return FALLBACK_DATA_DIR, FALLBACK_PENDING_PATH

def ensure_file() -> Tuple[str, str]:
    data_dir, pending_path = effective_paths()
    os.makedirs(data_dir, exist_ok=True)
    if not os.path.isfile(pending_path):
        with open(pending_path, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)
    return data_dir, pending_path

def load_pending() -> Tuple[List[Dict[str, Any]], str]:
    _, pending_path = ensure_file()
    try:
        with open(pending_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return (data if isinstance(data, list) else []), pending_path
    except Exception:
        return [], pending_path

def save_pending(data: List[Dict[str, Any]]) -> str:
    _, pending_path = ensure_file()
    tmp = pending_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, pending_path)
    return pending_path

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
        print(f"\n[{now}] {event}: {json.dumps(details, ensure_ascii=False)}", flush=True)
    except Exception:
        print(f"\n[{time.time()}] {event}: (log fail)", flush=True)

# ==========================================================
# HELPERS
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

def remaining_seconds(expires_at: Any) -> Optional[int]:
    now_s = int(time.time())
    x = _to_int_seconds(expires_at)
    if x is None:
        return None
    return max(0, x - now_s)

def remaining_text(expires_at: Any) -> str:
    rem = remaining_seconds(expires_at)
    if rem is None:
        return "onbekend"
    if rem <= 0:
        return "verlopen"
    mins = rem // 60
    hrs = mins // 60
    mins_left = mins % 60
    if hrs >= 1:
        return f"nog {hrs}u {mins_left}m"
    return f"nog {mins} min"

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
    # YES 10 PB-XXX
    parts = body.strip().split()
    if len(parts) >= 2 and parts[0].upper() == "YES" and parts[1].isdigit():
        amount = int(parts[1])
        pid = parts[2].strip() if len(parts) >= 3 else None
        return amount, pid
    return None, None

def parse_no(body: str) -> Optional[str]:
    # NO PB-XXX
    parts = body.strip().split()
    if parts and parts[0].upper() == "NO":
        return parts[1].strip() if len(parts) >= 2 else None
    return None

def compute_stop_target(entry: float) -> Tuple[float, float]:
    stop = entry * (1.0 - STOP_PCT)
    r = entry - stop
    target = entry + (RR_TARGET * r)
    return float(stop), float(target)

def format_prebuy_push(p: Dict[str, Any]) -> str:
    coin = p.get("coin", "?")
    score = p.get("score", "?")
    kans = p.get("kans", "")
    entry = p.get("entry", "?")
    stop = p.get("stop_loss", "?")
    target = p.get("target", "?")
    pid = p.get("id", "?")
    exp = remaining_text(p.get("expires_at", 0))

    kans_txt = f" ({kans})" if kans else ""
    return (
        "📊 PRE-BUY GEVONDEN\n"
        f"Coin: {coin}\n"
        f"Score: {score}{kans_txt}\n"
        f"Entry: {entry}\n"
        f"Stop: {stop}\n"
        f"Target: {target}\n\n"
        f"⏳ Geldig: {exp}\n"
        f"ID: {pid}\n\n"
        "Bevestig: YES <bedrag> <ID>\n"
        f"Voorbeeld: YES 10 {pid}"
    )

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

        pending, pending_path = load_pending()

        # voorkom duplicates (idempotent)
        exists = any(str(p.get("id", "")).strip() == pid for p in pending)
        if exists:
            log_event("INTERNAL_PREBUY_DUPLICATE", {"id": pid, "coin": coin, "pending_file": pending_path})
            return jsonify({"ok": True, "duplicate": True}), 200

        # force velden netjes
        data["status"] = status
        if "created_at" not in data:
            data["created_at"] = int(time.time())

        # expiry check: als missing, geef default 4 uur
        ex = _to_int_seconds(expires_at)
        if ex is None:
            ex = int(time.time()) + 4 * 60 * 60
        data["expires_at"] = ex

        pending.append(data)
        pending_path = save_pending(pending)

        # log + preview (handig voor debug)
        preview = format_prebuy_push(data)

        log_event("INTERNAL_PREBUY_SAVED", {"id": pid, "coin": coin, "pending_file": pending_path})
        log_event("INTERNAL_PREBUY_PREVIEW", {"id": pid, "preview": preview, "pending_file": pending_path})

        return jsonify({"ok": True}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

# ==========================================================
# HEALTH
# ==========================================================
@app.get("/")
def health():
    data_dir, pending_path = effective_paths()
    return f"OK - whatsapp_webhook running | data_dir={data_dir} | pending={pending_path}", 200

# ==========================================================
# WHATSAPP WEBHOOK (Twilio -> POST /whatsapp)
# ==========================================================
@app.post("/whatsapp")
def whatsapp():
    try:
        body = (request.values.get("Body") or "").strip()
        sender = (request.values.get("From") or "").strip()

        pending, pending_path = load_pending()
        log_event("WHATSAPP_IN", {"from": sender, "body": body, "pending_file": pending_path})

        if not body:
            return twiml("Leeg bericht. Stuur HELP.")

        up = body.upper().strip()

        # HELP
        if up == "HELP":
            return twiml(
                "Crypto_AI — Commands:\n"
                "• LIST\n"
                "• TOP\n"
                "• YES 5|10|15|20|30|100 <PREBUY-ID>\n"
                "• NO <PREBUY-ID>\n\n"
                "Voorbeeld:\n"
                "YES 10 PB-BTCUSDT-1707372001"
            )

        # verzamel actieve pendings
        active = [
            p for p in pending
            if str(p.get("status", "")).upper() == STATUS_PENDING and not is_expired(p.get("expires_at", 0))
        ]

        # LIST (laatste 10)
        if up == "LIST":
            if not active:
                return twiml(f"Geen PENDING Pre-BUY’s gevonden.\n(ik lees: {pending_path})")

            last = active[-MAX_LIST_ITEMS:]
            lines = [f"📋 BESCHIKBARE PRE-BUY'S ({len(last)})"]
            lines.append("")
            for idx, p in enumerate(last, start=1):
                coin = p.get("coin", "?")
                score = p.get("score", "?")
                pid = p.get("id", "?")
                exp = remaining_text(p.get("expires_at", 0))
                lines.append(f"{idx}) {coin} | score {score}")
                lines.append(f"⏳ {exp}")
                lines.append(f"ID: {pid}")
                lines.append("")

            lines.append("Bevestig: YES <bedrag> <ID>")
            lines.append("Weiger: NO <ID>")
            return twiml("\n".join(lines).strip())

        # TOP (beste 3 op score)
        if up == "TOP":
            if not active:
                return twiml(f"Geen PENDING Pre-BUY’s gevonden.\n(ik lees: {pending_path})")

            # sorteer op score (hoog -> laag), als score ontbreekt -> 0
            sorted_items = sorted(active, key=lambda x: int(x.get("score") or 0), reverse=True)
            top = sorted_items[:TOP_N]

            lines = ["🏆 TOP 3 BESTE PRE-BUY'S", ""]
            for idx, p in enumerate(top, start=1):
                coin = p.get("coin", "?")
                score = p.get("score", "?")
                pid = p.get("id", "?")
                exp = remaining_text(p.get("expires_at", 0))
                lines.append(f"{idx}) {coin} | score {score}")
                lines.append(f"⏳ {exp}")
                lines.append(f"ID: {pid}")
                lines.append("")

            lines.append("Bevestig: YES <bedrag> <ID>")
            return twiml("\n".join(lines).strip())

        # NO
        pid_no = parse_no(body)
        if pid_no is not None:
            if not pid_no:
                return twiml("❌ Gebruik: NO <ID>. Tip: stuur LIST om ID’s te zien.")
            item = find_by_id(pending, pid_no)
            if not item:
                return twiml("❌ ID niet gevonden. Stuur LIST.")

            if str(item.get("status", "")).upper() != STATUS_PENDING:
                return twiml(f"⚠️ Deze Pre-BUY is niet meer PENDING ({item.get('status','?')}).")

            if is_expired(item.get("expires_at", 0)):
                item["status"] = STATUS_REJECTED
                item["rejected_at"] = int(time.time())
                pending_path = save_pending(pending)
                log_event("PREBUY_REJECTED_EXPIRED", {"id": item.get("id"), "coin": item.get("coin"), "from": sender, "pending_file": pending_path})
                return twiml(f"⚠️ Pre-BUY was al verlopen. ID: {item.get('id','?')}")

            item["status"] = STATUS_REJECTED
            item["rejected_at"] = int(time.time())
            pending_path = save_pending(pending)

            log_event("PREBUY_REJECTED", {"id": item.get("id"), "coin": item.get("coin"), "from": sender, "pending_file": pending_path})
            return twiml(f"❌ Pre-BUY afgewezen\nID: {item.get('id','?')}")

        # YES
        amount, pid_yes = parse_yes(body)
        if amount is None:
            return twiml("Onbekend bericht. Stuur HELP voor commands.")

        if amount not in ALLOWED_AMOUNTS:
            return twiml("⛔ Ongeldig bedrag. Gebruik: 5,10,15,20,30,100.")

        if not pid_yes:
            return twiml("⚠️ Gebruik: YES <bedrag> <ID>\nTip: stuur LIST of TOP om een ID te kiezen.")

        item = find_by_id(pending, pid_yes)
        if not item:
            return twiml("❌ ID niet gevonden. Stuur LIST.")

        status = str(item.get("status", "")).upper()

        if status == STATUS_CONSUMED:
            return twiml(f"⚠️ Deze Pre-BUY is al gebruikt (CONSUMED).\nID: {item.get('id','?')}")
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
        pending_path = save_pending(pending)

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
            pending_path = save_pending(pending)

            log_event("BUY_ERROR", {"id": item.get("id"), "coin": coin, "reason": item.get("error_reason"), "from": sender, "pending_file": pending_path})
            return twiml(f"⛔ BUY mislukt: {item.get('error_reason','UNKNOWN')}")

        # 3) CONSUMED
        item["status"] = STATUS_CONSUMED
        item["consumed_at"] = int(time.time())
        item["entry"] = round(entry, 8)
        item["stop_loss"] = round(stop, 8)
        item["target"] = round(target, 8)
        item["qty"] = round(float(buy_res.get("qty", 0.0)), 10)
        item["trade_id"] = buy_res.get("trade_id")
        pending_path = save_pending(pending)

        log_event("BUY_OK", {"id": item.get("id"), "coin": coin, "amount": amount, "trade_id": item.get("trade_id"), "from": sender, "pending_file": pending_path})

        return twiml(
            f"✅ BUY UITGEVOERD\n"
            f"Coin: {coin}\n"
            f"Inzet: €{amount}\n"
            f"Entry: {entry:.6f}\n"
            f"Stop: {stop:.6f}\n"
            f"Target: {target:.6f}\n\n"
            f"PreBUY-ID: {item.get('id','?')}\n"
            f"Trade ID: {item.get('trade_id','?')}"
        )

    except Exception as e:
        traceback.print_exc()
        log_event("WEBHOOK_FATAL", {"error": str(e)})
        return twiml("⚠️ Interne fout in webhook. Check Render logs.")

# ==========================================================
# TEST ONDERAAN LATEN STAAN (zoals jij wil)
# ==========================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
