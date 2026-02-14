from __future__ import annotations

import os
import re
import json
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, request, Response

# Twilio is optioneel geïnstalleerd in sommige setups.
# Als je Twilio lib niet hebt, werkt het alsnog met plain text response.
try:
    from twilio.twiml.messaging_response import MessagingResponse  # type: ignore
except Exception:
    MessagingResponse = None  # type: ignore

# psycopg2 is nodig voor Postgres
try:
    import psycopg2  # type: ignore
except Exception:
    psycopg2 = None  # type: ignore


app = Flask(__name__)

# ==========================
# ENV
# ==========================
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()

DATA_DIR = (os.getenv("DATA_DIR") or "/data").rstrip("/")
PENDING_PATH = os.getenv("PENDING_PATH") or os.path.join(DATA_DIR, "pending_approvals.json")

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

# Allowed stake amounts
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

# ==========================
# HELPERS
# ==========================
def log(msg: str) -> None:
    print(msg, flush=True)


def twilio_reply(text: str) -> Response:
    """
    Stuurt een Twilio-compatibele response terug.
    Werkt met Twilio lib of plain text fallback.
    """
    if MessagingResponse is None:
        return Response(text, mimetype="text/plain")
    resp = MessagingResponse()
    resp.message(text)
    return Response(str(resp), mimetype="application/xml")


def normalize_msg(body: str) -> str:
    return (body or "").strip()


def read_pending_json() -> List[Dict[str, Any]]:
    try:
        if not os.path.exists(PENDING_PATH):
            return []
        with open(PENDING_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        # support {"items":[...]}
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data["items"]
        return []
    except Exception:
        return []


def write_pending_json(items: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(PENDING_PATH), exist_ok=True)
    tmp = PENDING_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    os.replace(tmp, PENDING_PATH)


# ==========================
# POSTGRES LAYER (A: alleen pending_approvals)
# ==========================
def db_available() -> bool:
    return bool(DATABASE_URL) and (psycopg2 is not None)


def db_conn():
    # autocommit zodat updates direct doorpakken
    conn = psycopg2.connect(DATABASE_URL)  # type: ignore
    conn.autocommit = True
    return conn


def db_list_pending(limit: int = 10) -> List[Dict[str, Any]]:
    """
    Haalt PENDING prebuys op uit Postgres.
    """
    if not db_available():
        return []

    q = """
    SELECT
      id,
      symbol,
      setup_type,
      regime,
      score,
      label,
      entry,
      stop,
      target,
      created_at
    FROM pending_approvals
    WHERE status = 'PENDING'
    ORDER BY created_at ASC
    LIMIT %s
    """
    rows: List[Dict[str, Any]] = []
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (limit,))
            for r in cur.fetchall():
                rows.append({
                    "id": r[0],
                    "coin": r[1],
                    "setup_type": r[2],
                    "market_regime": r[3],
                    "score": r[4],
                    "grade": r[5],
                    "entry": float(r[6]) if r[6] is not None else None,
                    "stop_loss": float(r[7]) if r[7] is not None else None,
                    "target": float(r[8]) if r[8] is not None else None,
                    "created_at": str(r[9]),
                })
    return rows


def db_find_pending_by_id(prebuy_id: str) -> Optional[Dict[str, Any]]:
    if not db_available():
        return None

    q = """
    SELECT
      id,
      symbol,
      setup_type,
      regime,
      score,
      label,
      entry,
      stop,
      target,
      created_at
    FROM pending_approvals
    WHERE id = %s AND status = 'PENDING'
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (prebuy_id,))
            r = cur.fetchone()
            if not r:
                return None
            return {
                "id": r[0],
                "coin": r[1],
                "setup_type": r[2],
                "market_regime": r[3],
                "score": r[4],
                "grade": r[5],
                "entry": float(r[6]) if r[6] is not None else None,
                "stop_loss": float(r[7]) if r[7] is not None else None,
                "target": float(r[8]) if r[8] is not None else None,
                "created_at": str(r[9]),
            }


def db_claim_oldest_pending() -> Optional[Dict[str, Any]]:
    """
    Pakt de oudste PENDING en zet hem op APPROVED (zodat hij niet dubbel gepakt wordt).
    """
    if not db_available():
        return None

    # Gebruik een transaction met row lock
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("BEGIN;")
            cur.execute(
                """
                SELECT id, symbol, setup_type, regime, score, label, entry, stop, target
                FROM pending_approvals
                WHERE status = 'PENDING'
                ORDER BY created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            )
            r = cur.fetchone()
            if not r:
                cur.execute("COMMIT;")
                return None

            prebuy_id = r[0]
            cur.execute(
                "UPDATE pending_approvals SET status = 'APPROVED' WHERE id = %s",
                (prebuy_id,),
            )
            cur.execute("COMMIT;")

            return {
                "id": r[0],
                "coin": r[1],
                "setup_type": r[2],
                "market_regime": r[3],
                "score": r[4],
                "grade": r[5],
                "entry": float(r[6]) if r[6] is not None else None,
                "stop_loss": float(r[7]) if r[7] is not None else None,
                "target": float(r[8]) if r[8] is not None else None,
            }


def db_update_status(prebuy_id: str, new_status: str) -> None:
    if not db_available():
        return
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pending_approvals SET status = %s WHERE id = %s",
                (new_status, prebuy_id),
            )


# ==========================
# COMMAND PARSER
# ==========================
def parse_yes(msg: str) -> Tuple[Optional[int], Optional[str]]:
    """
    YES 10
    YES 10 PB-BTCUSDT-123
    """
    m = re.match(r"(?i)^\s*YES\s+(\d+)(?:\s+([A-Za-z0-9\-\_]+))?\s*$", msg)
    if not m:
        return None, None
    amount = int(m.group(1))
    pid = m.group(2)
    return amount, pid


def parse_no(msg: str) -> Optional[str]:
    """
    NO
    NO PB-...
    """
    m = re.match(r"(?i)^\s*NO(?:\s+([A-Za-z0-9\-\_]+))?\s*$", msg)
    if not m:
        return None
    return m.group(1)


# ==========================
# ROUTES
# ==========================
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook() -> Response:
    body = normalize_msg(request.form.get("Body", ""))
    from_number = (request.form.get("From", "") or "").strip()

    log(f"WHATSAPP_IN: from={from_number} body={body} db={'YES' if db_available() else 'NO'} pending_file={PENDING_PATH}")

    try:
        msg_up = body.strip()

        # 1) LIST
        if re.match(r"(?i)^\s*LIST\s*$", msg_up):
            items = db_list_pending(limit=10) if db_available() else read_pending_json()

            if not items:
                return twilio_reply("Geen open Pre-BUY’s (PENDING).")

            lines = ["Open Pre-BUY’s:"]
            for it in items[:10]:
                lines.append(
                    f"- {it.get('id')} | {it.get('coin')} | {it.get('setup_type')} | "
                    f"{it.get('grade')} | score={it.get('score')} | entry={it.get('entry')} | target={it.get('target')}"
                )
            lines.append("")
            lines.append("Bevestig: YES <bedrag> <ID>")
            lines.append("Voorbeeld: YES 10 PB-BTCUSDT-1770831004")
            return twilio_reply("\n".join(lines))

        # 2) YES
        amount, pid = parse_yes(msg_up)
        if amount is not None:
            if amount not in ALLOWED_AMOUNTS:
                return twilio_reply(f"Bedrag niet toegestaan. Kies uit: {sorted(ALLOWED_AMOUNTS)}")

            # Als je een ID meegeeft: pak die. Anders: pak de oudste.
            if db_available():
                if pid:
                    item = db_find_pending_by_id(pid)
                    if not item:
                        return twilio_reply("ID niet gevonden of niet meer PENDING.")
                    # claim via status update zodat hij niet dubbel kan
                    db_update_status(pid, "APPROVED")
                    item["id"] = pid
                else:
                    item = db_claim_oldest_pending()
                    if not item:
                        return twilio_reply("Geen open Pre-BUY’s om goed te keuren.")
            else:
                # fallback JSON (oude gedrag)
                items = read_pending_json()
                if not items:
                    return twilio_reply("Geen open Pre-BUY’s om goed te keuren.")
                item = None
                if pid:
                    for x in items:
                        if str(x.get("id")) == pid and str(x.get("status", "PENDING")).upper() == "PENDING":
                            item = x
                            break
                    if not item:
                        return twilio_reply("ID niet gevonden of niet meer PENDING.")
                else:
                    item = items[0]

                # markeer als approved in JSON
                item["status"] = "APPROVED"
                write_pending_json(items)

            # Hier kun jij later je BUY trigger doen (paper_trader) – nu alleen bevestigen.
            return twilio_reply(
                f"✅ Goedgekeurd: {item.get('id')} | {item.get('coin')} | inzet €{amount}\n"
                f"entry={item.get('entry')} stop={item.get('stop_loss')} target={item.get('target')}"
            )

        # 3) NO
        pid2 = parse_no(msg_up)
        if pid2 is not None:
            if db_available():
                if pid2:
                    db_update_status(pid2, "REJECTED")
                    return twilio_reply(f"❌ Afgewezen: {pid2}")
                else:
                    # reject oudste PENDING
                    item = db_claim_oldest_pending()
                    if not item:
                        return twilio_reply("Geen open Pre-BUY’s om af te wijzen.")
                    db_update_status(item["id"], "REJECTED")
                    return twilio_reply(f"❌ Afgewezen: {item['id']}")
            else:
                items = read_pending_json()
                if not items:
                    return twilio_reply("Geen open Pre-BUY’s om af te wijzen.")
                if pid2:
                    found = False
                    for x in items:
                        if str(x.get("id")) == pid2 and str(x.get("status", "PENDING")).upper() == "PENDING":
                            x["status"] = "REJECTED"
                            found = True
                            break
                    if not found:
                        return twilio_reply("ID niet gevonden of niet meer PENDING.")
                    write_pending_json(items)
                    return twilio_reply(f"❌ Afgewezen: {pid2}")
                else:
                    items[0]["status"] = "REJECTED"
                    write_pending_json(items)
                    return twilio_reply(f"❌ Afgewezen: {items[0].get('id')}")

        # 4) HELP / fallback
        return twilio_reply(
            "Commando’s:\n"
            "- LIST\n"
            "- YES <bedrag> <ID>\n"
            "- NO <ID>\n"
            "Voorbeeld: YES 10 PB-BTCUSDT-1770831004"
        )

    except Exception as e:
        traceback.print_exc()
        return twilio_reply(f"⚠️ Error in webhook: {type(e).__name__}")


@app.route("/", methods=["GET"])
def health() -> str:
    return "OK"


if __name__ == "__main__":
    # Render draait vaak via gunicorn; lokaal kan je dit gebruiken:
    port = int(os.getenv("PORT") or "10000")
    app.run(host="0.0.0.0", port=port, debug=False)
