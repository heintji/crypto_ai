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
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()

# ==========================================================
# PATHS (Render-proof: altijd naar /data schrijven als die bestaat)
# ==========================================================
PERSIST_DIR = os.getenv("PERSIST_DIR", "/data").strip() or "/data"
if not os.path.isdir(PERSIST_DIR):
    # fallback lokaal (zonder Render Disk)
    PERSIST_DIR = os.path.join(PROJECT_ROOT, "data")

PENDING_PATH = os.path.join(PERSIST_DIR, "pending_approvals.json")

# ==========================================================
# SETTINGS
# ==========================================================
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

STOP_PCT = float(os.getenv("STOP_PCT", "0.02"))      # 2% stop
RR_TARGET = float(os.getenv("RR_TARGET", "2.0"))     # 2R target

STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_CONSUMED = "CONSUMED"
STATUS_REJECTED = "REJECTED"
STATUS_ERROR = "ERROR"

# ==========================================================
# FILE HELPERS
# ==========================================================
def ensure_file() -> None:
    os.makedirs(os.path.dirname(PENDING_PATH), exist_ok=True)
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
# HELPERS (expiry + formatting)
# ==========================================================
def _to_int_seconds(ts: Any) -> int:
    try:
        x = int(ts)
    except Exception:
        return 0
    # ms support
    if x > 10**12:
        x = int(x / 1000)
    return x

def is_expired(expires_at: Any) -> bool:
    now_s = int(time.time())
    ex = _to_int_seconds(expires_at)
    if ex <= 0:
        return False
    return ex < now_s

def mins_left_text(expires_at: Any) -> str:
    ex = _to_int_seconds(expires_at)
    if ex <= 0:
        return "onbekend"
    now_s = int(time.time())
    delta = ex - now_s
    if delta <= 0:
        return "0 min"
    mins = int(delta // 60)
    if mins < 60:
        return f"{mins} min"
    h = mins // 60
    m = mins % 60
    return f"{h}u {m}m"

def fmt_price(x: Any, decimals: int = 6) -> str:
    try:
        v = float(x)
        return f"{v:.{decimals}f}"
    except Exception:
        return "?"

def fmt_int(x: Any) -> str:
    try:
        return str(int(float(x)))
    except Exception:
        return "?"

def find_latest_pending(pending: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    # laatste geldige PENDING
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
# MESSAGE BUILDERS (DIT IS JOUW FORMAT)
# ==========================================================
def build_prebuy_push(p: Dict[str, Any]) -> str:
    coin = str(p.get("coin", "?"))
    score = fmt_int(p.get("score", "?"))
    kans = str(p.get("kans", "") or "")
    entry = fmt_price(p.get("entry", p.get("details", {}).get("last_price", 0.0)))
    stop = fmt_price(p.get("stop_loss", 0.0))
    target = fmt_price(p.get("target", 0.0))
    expires = mins_left_text(p.get("expires_at", 0))
    pid = str(p.get("id", "?"))

    return (
        "📊 PRE-BUY GEVONDEN\n"
        f"Coin: {coin}\n"
        f"Score: {score}{f' ({kans})' if kans else ''}\n"
        f"Entry: {entry}\n"
        f"Stop: {stop}\n"
        f"Target: {target}\n\n"
        f"⏳ Geldig: nog {expires}\n"
        f"ID: {pid}\n\n"
        "Bevestig: YES <bedrag> <ID>\n"
        f"Voorbeeld: YES 10 {pid}"
    )

def build_list(pending_items: List[Dict[str, Any]]) -> str:
    lines = [f"📋 BESCHIKBARE PRE-BUY'S ({len(pending_items)})", ""]
    for idx, p in enumerate(pending_items, start=1):
        coin = str(p.get("coin", "?"))
        score = fmt_int(p.get("score", "?"))
        expires = mins_left_text(p.get("expires_at", 0))
        pid = str(p.get("id", "?"))
        lines.append(f"{idx}) {coin} | score {score}")
        lines.append(f"⏳ nog {expires}")
        lines.append(f"ID: {pid}")
        lines.append("")
    lines.append("Bevestig: YES <bedrag> <ID>")
    lines.append("Weiger: NO <ID>")
    return "\n".join(lines).strip()

def build_top(pending_items: List[Dict[str, Any]]) -> str:
    lines = ["🏆 TOP 3 BESTE PRE-BUY'S", ""]
    for idx, p in enumerate(pending_items, start=1):
        coin = str(p.get("coin", "?"))
        score = fmt_int(p.get("score", "?"))
        expires = mins_left_text(p.get("expires_at", 0))
        pid = str(p.get("id", "?"))
        lines.append(f"{idx}) {coin} | score {score}")
        lines.append(f"⏳ nog {expires}")
        lines.append(f"ID: {pid}")
        lines.append("")
    lines.append("Bevestig: YES <bedrag> <ID>")
    return "\n".join(lines).strip()

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

        pid = str(data.get("id", "")).strip()
        coin = str(data.get("coin", "")).strip()
        status = str(data.get("status", STATUS_PENDING)).upper().strip()
        expires_at = data.get("expires_at", 0)

        if not pid or not coin:
            return jsonify({"ok": False, "error": "missing id/coin"}), 400

        if status not in {STATUS_PENDING, STATUS_APPROVED, STATUS_CONSUMED, STATUS_REJECTED, STATUS_ERROR}:
            status = STATUS_PENDING

        pending = load_pending()

        # voorkom duplicates (idempotent)
        if any(str(p.get("id", "")).strip() == pid for p in pending):
            log_event("INTERNAL_PREBUY_DUPLICATE", {"id": pid, "coin": coin})
            return jsonify({"ok": True, "duplicate": True}), 200

        # created_at
        if "created_at" not in data:
            data["created_at"] = int(time.time())

        # expiry: als missing => default 4 uur
        ex = _to_int_seconds(expires_at)
        if ex <= 0:
            ex = int(time.time()) + 4 * 60 * 60
        data["expires_at"] = ex

        # Zorg dat entry/stop/target bestaan (zodat WhatsApp en dashboard altijd compleet zijn)
        # Als multi_coin_score ze al meestuurt: laten we het staan.
        try:
            entry = float(data.get("entry") or data.get("details", {}).get("last_price") or 0.0)
        except Exception:
            entry = 0.0

        if entry > 0:
            if not data.get("entry"):
                data["entry"] = round(entry, 8)
            if not data.get("stop_loss") or float(data.get("stop_loss") or 0) <= 0:
                stop, target = compute_stop_target(entry)
                data["stop_loss"] = round(stop, 8)
                data["target"] = round(target, 8)

        data["status"] = status

        pending.append(data)
        save_pending(pending)

        log_event("INTERNAL_PREBUY_SAVED", {"id": pid, "coin": coin, "pending_file": PENDING_PATH})

        # ✅ Optioneel: push format in logs (handig debug)
        try:
            preview = build_prebuy_push(data)
            log_event("INTERNAL_PREBUY_PREVIEW", {"id": pid, "preview": preview[:400]})
        except Exception:
            pass

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
                "• TOP\n"
                "• YES 5|10|15|20|30|100 <PREBUY-ID>\n"
                "• NO <PREBUY-ID>\n\n"
                "Voorbeeld:\n"
                "YES 10 PB-BTCUSDT-123456\n\n"
                "Tip: gebruik LIST of TOP om een ID te kiezen."
            )

        # LIST (alle PENDING)
        if up == "LIST":
            items = [
                p for p in pending
                if str(p.get("status", "")).upper() == STATUS_PENDING and not is_expired(p.get("expires_at", 0))
            ]

            if not items:
                return twiml(f"Geen PENDING Pre-BUY’s gevonden.\n(ik lees: {PENDING_PATH})")

            # sorteer: nieuwste onderaan, maar we tonen maximaal 10
            items_sorted = sorted(items, key=lambda x: int(_to_int_seconds(x.get("created_at", 0))) or 0)
            show = items_sorted[-10:]
            return twiml(build_list(show))

        # TOP (top 3 beste)
        if up == "TOP":
            items = [
                p for p in pending
                if str(p.get("status", "")).upper() == STATUS_PENDING and not is_expired(p.get("expires_at", 0))
            ]
            if not items:
                return twiml("Geen PENDING Pre-BUY’s om te tonen. Stuur later opnieuw LIST.")

            def _score(p: Dict[str, Any]) -> int:
                try:
                    return int(float(p.get("score", 0) or 0))
                except Exception:
                    return 0

            # score desc, daarna expiry (meest tijd over), daarna created_at
            items_sorted = sorted(
                items,
                key=lambda p: (
                    -_score(p),
                    -(_to_int_seconds(p.get("expires_at", 0)) - int(time.time())),
                    -(_to_int_seconds(p.get("created_at", 0))),
                )
            )
            top3 = items_sorted[:3]
            return twiml(build_top(top3))

        # NO
        pid_no = parse_no(body)
        if pid_no is not None:
            if not pid_no:
                return twiml("Gebruik: NO <ID>\nTip: stuur LIST om ID’s te zien.")

            item = find_by_id(pending, pid_no)
            if not item:
                return twiml("❌ ID niet gevonden. Stuur LIST.")

            if str(item.get("status", "")).upper() != STATUS_PENDING:
                return twiml(f"⚠️ Deze Pre-BUY is niet meer PENDING ({item.get('status','?')}).")

            if is_expired(item.get("expires_at", 0)):
                item["status"] = STATUS_REJECTED
                item["rejected_at"] = int(time.time())
                save_pending(pending)
                return twiml("⚠️ Deze Pre-BUY was al verlopen en is nu afgesloten (REJECTED).")

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
            return twiml("Gebruik: YES <bedrag> <ID>\nTip: stuur TOP voor de beste 3 trades.")

        item = find_by_id(pending, pid_yes)
        if not item:
            return twiml("❌ ID niet gevonden. Stuur LIST of TOP.")

        status = str(item.get("status", "")).upper()

        if status == STATUS_CONSUMED:
            return twiml(f"⚠️ Deze Pre-BUY is al gebruikt (CONSUMED).\nID: {item.get('id','?')}")
        if status != STATUS_PENDING:
            return twiml(f"⚠️ Deze Pre-BUY is niet meer PENDING ({item.get('status','?')}).")

        if is_expired(item.get("expires_at", 0)):
            return twiml("❌ Niet mogelijk\nReden: Pre-BUY verlopen")

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
            "✅ BUY UITGEVOERD\n"
            f"Coin: {coin}\n"
            f"Inzet: €{amount}\n"
            f"Entry: {entry:.6f}\n"
            f"Stop: {stop:.6f}\n"
            f"Target: {target:.6f}\n\n"
            f"Trade ID: {item.get('trade_id','?')}\n"
            f"Saldo: €{float(buy_res.get('balance', 0.0)):.2f}\n"
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
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
