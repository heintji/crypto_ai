# whatsapp_webhook.py
from __future__ import annotations

import os
import re
import json
import time
import traceback
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, request, Response

# =========================
# ENV
# =========================
USE_DB_APPROVALS = (os.getenv("USE_DB_APPROVALS") or "1").strip().lower() in {"1", "true", "yes", "on"}
DATABASE_URL = (os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL".lower()) or "").strip()
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()

DATA_DIR = (os.getenv("DATA_DIR") or "").strip() or ("/data" if os.path.isdir("/data") else "/tmp/data")
PENDING_JSON_PATH = (os.getenv("PENDING_PATH") or os.path.join(DATA_DIR, "pending_approvals.json")).strip()

ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

# =========================
# Optional: DB
# =========================
_conn = None
def db_ready() -> bool:
    return USE_DB_APPROVALS and bool(DATABASE_URL)

def db_conn():
    global _conn
    if _conn is not None:
        return _conn
    import psycopg2
    _conn = psycopg2.connect(DATABASE_URL)
    _conn.autocommit = False
    return _conn

def db_init() -> None:
    if not db_ready():
        return
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_approvals (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            coin TEXT,
            setup_type TEXT,
            label TEXT,
            score INTEGER,
            grade TEXT,
            entry DOUBLE PRECISION,
            stop_loss DOUBLE PRECISION,
            target DOUBLE PRECISION,
            bot_confidence INTEGER,
            market_regime TEXT,
            regime TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            payload_json JSONB
        );
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_status_exp ON pending_approvals(status, expires_at);")
    conn.commit()
    cur.close()

# =========================
# Trading execution
# =========================
# Let op: jouw huidige paper_trader.py is LIVE Bitvavo file, maar functie heet nog buy_eur(...)
from trading.paper_trader import buy_eur  # noqa

# =========================
# Flask
# =========================
app = Flask(__name__)

def respond(msg: str) -> Response:
    # Twilio WhatsApp verwacht TwiML, maar plain text werkt vaak ook.
    # We sturen text/plain zodat je altijd ziet wat er gebeurt.
    return Response(msg, mimetype="text/plain")

def _clean(s: str) -> str:
    return (s or "").strip()

def _upper(s: str) -> str:
    return _clean(s).upper()

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

# =========================
# JSON fallback (optioneel)
# =========================
def _load_pending_json() -> List[Dict[str, Any]]:
    if not os.path.exists(PENDING_JSON_PATH):
        return []
    try:
        with open(PENDING_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or []
    except Exception:
        return []

def _save_pending_json(rows: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(PENDING_JSON_PATH), exist_ok=True)
    with open(PENDING_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)

# =========================
# DB helpers
# =========================
def db_list_pending(limit: int = 10) -> List[Dict[str, Any]]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, symbol, label, score, entry, stop_loss, target,
               EXTRACT(EPOCH FROM (expires_at - NOW()))::bigint AS seconds_left
        FROM pending_approvals
        WHERE status='PENDING' AND expires_at > NOW()
        ORDER BY score DESC, created_at DESC
        LIMIT %s;
        """,
        (limit,)
    )
    rows = []
    for r in cur.fetchall():
        rows.append({
            "id": r[0],
            "symbol": r[1],
            "label": r[2],
            "score": r[3],
            "entry": r[4],
            "stop_loss": r[5],
            "target": r[6],
            "seconds_left": int(r[7] or 0),
        })
    cur.close()
    conn.commit()
    return rows

def db_get_by_id(prebuy_id: str) -> Optional[Dict[str, Any]]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, symbol, label, score, entry, stop_loss, target, status
        FROM pending_approvals
        WHERE id=%s
        """,
        (prebuy_id,)
    )
    r = cur.fetchone()
    cur.close()
    conn.commit()
    if not r:
        return None
    return {
        "id": r[0],
        "symbol": r[1],
        "label": r[2],
        "score": r[3],
        "entry": float(r[4] or 0),
        "stop_loss": float(r[5] or 0),
        "target": float(r[6] or 0),
        "status": r[7],
    }

def db_approve(prebuy_id: str) -> Tuple[bool, str]:
    conn = db_conn()
    cur = conn.cursor()
    # Alleen goedkeuren als nog pending en niet verlopen
    cur.execute(
        """
        UPDATE pending_approvals
        SET status='APPROVED'
        WHERE id=%s AND status='PENDING' AND expires_at > NOW()
        """,
        (prebuy_id,)
    )
    ok = cur.rowcount == 1
    cur.close()
    conn.commit()
    return ok, ("OK" if ok else "NOT_FOUND_OR_EXPIRED")

def db_reject(prebuy_id: str) -> Tuple[bool, str]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE pending_approvals
        SET status='REJECTED'
        WHERE id=%s AND status IN ('PENDING','APPROVED')
        """,
        (prebuy_id,)
    )
    ok = cur.rowcount == 1
    cur.close()
    conn.commit()
    return ok, ("OK" if ok else "NOT_FOUND")

# =========================
# Command parsing
# =========================
YES_RE = re.compile(r"^\s*YES\s+(\d+)\s+([A-Za-z0-9\-_]+)\s*$", re.IGNORECASE)
NO_RE  = re.compile(r"^\s*NO\s+([A-Za-z0-9\-_]+)\s*$", re.IGNORECASE)

HELP_TEXT = (
    "Commands:\n"
    "HELP\n"
    "LIST (laatste PENDING pre-buys)\n"
    "TOP (beste 5 op score)\n"
    "YES <bedrag> <ID>\n"
    "NO <ID>\n\n"
    "Voorbeeld: YES 10 PB-BTCUSDT-1771100252"
)

@app.route("/whatsapp", methods=["POST"])
def whatsapp() -> Response:
    try:
        if db_ready():
            db_init()

        body = _clean(request.form.get("Body", ""))
        msg = _upper(body)

        if not msg:
            return respond(HELP_TEXT)

        if msg in {"HELP", "H"}:
            return respond(HELP_TEXT)

        if msg in {"LIST", "L"}:
            if db_ready():
                rows = db_list_pending(limit=10)
                if not rows:
                    return respond("Geen PENDING Pre-BUYs gevonden. (of alles is verlopen)")
                out = []
                for r in rows:
                    mins = max(0, int(r["seconds_left"] // 60))
                    out.append(
                        f"{r['id']} | {r['symbol']} | {r.get('label','')} | score={r.get('score')} | "
                        f"entry={r.get('entry')} | stop={r.get('stop_loss')} | target={r.get('target')} | exp {mins}m"
                    )
                out.append("\nBevestig: YES <bedrag> <ID>")
                return respond("\n".join(out))
            else:
                rows = _load_pending_json()
                now = int(time.time())
                rows = [r for r in rows if r.get("status") == "PENDING" and int(r.get("expires_at", 0)) > now]
                if not rows:
                    return respond("Geen PENDING Pre-BUYs gevonden. (of alles is verlopen)")
                rows = sorted(rows, key=lambda x: (int(x.get("score", 0)), int(x.get("created_at", 0))), reverse=True)[:10]
                out = []
                for r in rows:
                    mins = max(0, int((int(r.get("expires_at", 0)) - now) // 60))
                    out.append(
                        f"{r['id']} | {r['symbol']} | {r.get('label','')} | score={r.get('score')} | "
                        f"entry={r.get('entry')} | stop={r.get('stop_loss')} | target={r.get('target')} | exp {mins}m"
                    )
                out.append("\nBevestig: YES <bedrag> <ID>")
                return respond("\n".join(out))

        if msg in {"TOP", "T"}:
            if db_ready():
                rows = db_list_pending(limit=5)
                if not rows:
                    return respond("Geen PENDING Pre-BUYs gevonden. (of alles is verlopen)")
                out = ["TOP 5 (PENDING):"]
                for r in rows:
                    mins = max(0, int(r["seconds_left"] // 60))
                    out.append(
                        f"{r['id']} | {r['symbol']} | {r.get('label','')} | score={r.get('score')} | exp {mins}m"
                    )
                out.append("\nBevestig: YES <bedrag> <ID>")
                return respond("\n".join(out))
            else:
                return respond("TOP werkt alleen met DB in deze setup. Gebruik LIST.")

        m = YES_RE.match(body)
        if m:
            amount = int(m.group(1))
            prebuy_id = m.group(2).strip()

            if amount not in ALLOWED_AMOUNTS:
                return respond(f"❌ Bedrag niet toegestaan. Kies uit: {sorted(ALLOWED_AMOUNTS)}")

            if db_ready():
                ok, reason = db_approve(prebuy_id)
                if not ok:
                    return respond(f"❌ Kan niet goedkeuren (niet gevonden/verlopen): {prebuy_id}")

                row = db_get_by_id(prebuy_id)
                if not row:
                    return respond(f"❌ Pre-BUY niet gevonden: {prebuy_id}")

                # Feedback + BUY
                # Let op: jouw buy_eur signature is (symbol, amount_eur, stop_loss, target, prebuy_id)
                try:
                    res = buy_eur(
                        symbol=row["symbol"],
                        amount_eur=float(amount),
                        stop_loss=float(row["stop_loss"]),
                        target=float(row["target"]),
                        prebuy_id=row["id"],
                    )
                    if isinstance(res, dict) and res.get("ok"):
                        return respond(
                            "✅ GOEDKEURING ONTVANGEN\n"
                            f"ID: {row['id']}\n"
                            f"Inzet: €{amount}\n"
                            f"{row['symbol']} | {row.get('label','')} | score={row.get('score')}\n"
                            f"entry={row.get('entry')} stop={row.get('stop_loss')} target={row.get('target')}\n"
                            "🟢 BUY uitgevoerd."
                        )
                    return respond(
                        "✅ GOEDKEURING ONTVANGEN\n"
                        f"ID: {row['id']}\n"
                        f"Inzet: €{amount}\n"
                        f"{row['symbol']} | {row.get('label','')} | score={row.get('score')}\n"
                        f"entry={row.get('entry')} stop={row.get('stop_loss')} target={row.get('target')}\n"
                        f"⚠️ BUY niet uitgevoerd: {res}"
                    )
                except Exception as e:
                    return respond(
                        "✅ GOEDKEURING ONTVANGEN\n"
                        f"ID: {row['id']}\n"
                        f"Inzet: €{amount}\n"
                        f"{row['symbol']} | {row.get('label','')} | score={row.get('score')}\n"
                        f"entry={row.get('entry')} stop={row.get('stop_loss')} target={row.get('target')}\n"
                        f"⚠️ BUY niet uitgevoerd: {str(e)}"
                    )
            else:
                return respond("❌ DB staat uit. Zet USE_DB_APPROVALS=1 en DATABASE_URL.")

        m = NO_RE.match(body)
        if m:
            prebuy_id = m.group(1).strip()
            if db_ready():
                ok, _ = db_reject(prebuy_id)
                return respond("🟥 Afgewezen: " + prebuy_id if ok else "❌ ID niet gevonden: " + prebuy_id)
            return respond("❌ DB staat uit. NO werkt hier alleen met DB.")

        return respond(HELP_TEXT)

    except Exception:
        # Dit is exact je “Interne fout in webhook”
        tb = traceback.format_exc()
        print(tb, flush=True)
        return respond("❌ Interne fout in webhook. Check Render logs (Traceback).")

if __name__ == "__main__":
    # Render: bind op PORT
    port = int(os.getenv("PORT") or "10000")
    app.run(host="0.0.0.0", port=port)
