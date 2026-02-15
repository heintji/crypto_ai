# whatsapp_webhook.py
from __future__ import annotations

import os
import re
import json
import time
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
from flask import Flask, request, Response

# ========= CONFIG =========
USE_DB_APPROVALS = (os.getenv("USE_DB_APPROVALS", "1").strip().lower() in {"1", "true", "yes", "on"})
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()  # optioneel
DATA_DIR = (os.getenv("DATA_DIR") or "/data").strip()
PENDING_FILE = (os.getenv("PENDING_PATH") or f"{DATA_DIR}/pending_approvals.json").strip()

ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

# ===== BUY KOPPELING =====
# Let op: jouw trading/paper_trader.py is nu LIVE Bitvavo (maar function name is buy_eur)
from trading.paper_trader import buy_eur  # noqa: E402

app = Flask(__name__)


# ========= HELPERS =========
def _now_ts() -> int:
    return int(time.time())


def _resp(text: str, status: int = 200) -> Response:
    # Twilio accepteert plain text prima (geen TwiML nodig als je Messaging Webhook gebruikt)
    return Response(text, status=status, mimetype="text/plain")


def _connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt (Render env).")
    return psycopg2.connect(DATABASE_URL)


def _ensure_db_schema() -> None:
    # pending_approvals bestaat bij jou al, maar we zorgen dat extra kolommen bestaan
    with _connect() as conn:
        with conn.cursor() as cur:
            # basis tabel (safe: IF NOT EXISTS)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_approvals (
                    id TEXT PRIMARY KEY,
                    symbol TEXT,
                    coin TEXT,
                    status TEXT,
                    label TEXT,
                    score INTEGER,
                    grade TEXT,
                    setup_type TEXT,
                    regime TEXT,
                    market_regime TEXT,
                    entry DOUBLE PRECISION,
                    stop DOUBLE PRECISION,
                    stop_loss DOUBLE PRECISION,
                    target DOUBLE PRECISION,
                    bot_confidence INTEGER,
                    payload TEXT,
                    payload_json JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    expires_at TIMESTAMPTZ
                );
                """
            )

            # extra velden voor approvals
            cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS approved_amount DOUBLE PRECISION;")
            cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;")
            cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS approved_by TEXT;")

            conn.commit()


def _db_mark_expired() -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pending_approvals
                SET status='EXPIRED'
                WHERE status='PENDING' AND expires_at IS NOT NULL AND expires_at <= NOW();
                """
            )
            conn.commit()


def _db_fetch_pending(order_by: str, limit: int = 5) -> List[Dict[str, Any]]:
    _db_mark_expired()
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # order_by moet hard-coded zijn (geen user input)
            cur.execute(
                f"""
                SELECT *
                FROM pending_approvals
                WHERE status='PENDING' AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY {order_by}
                LIMIT %s;
                """,
                (limit,),
            )
            rows = cur.fetchall() or []
            return [dict(r) for r in rows]


def _db_get_pending_by_id(prebuy_id: str) -> Optional[Dict[str, Any]]:
    _db_mark_expired()
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM pending_approvals
                WHERE id=%s AND status='PENDING' AND (expires_at IS NULL OR expires_at > NOW())
                LIMIT 1;
                """,
                (prebuy_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def _db_set_approved(prebuy_id: str, amount: float, approved_by: str) -> bool:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pending_approvals
                SET status='APPROVED', approved_amount=%s, approved_at=NOW(), approved_by=%s
                WHERE id=%s AND status='PENDING' AND (expires_at IS NULL OR expires_at > NOW());
                """,
                (float(amount), approved_by, prebuy_id),
            )
            ok = (cur.rowcount == 1)
            conn.commit()
            return ok


def _db_set_consumed(prebuy_id: str) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pending_approvals
                SET status='CONSUMED'
                WHERE id=%s AND status IN ('APPROVED','PENDING');
                """,
                (prebuy_id,),
            )
            conn.commit()


def _load_file_pending() -> List[Dict[str, Any]]:
    if not os.path.exists(PENDING_FILE):
        return []
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or []
    except Exception:
        return []


def _save_file_pending(items: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)


def _file_mark_expired(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    now = _now_ts()
    out = []
    for x in items:
        exp = int(x.get("expires_at", 0) or 0)
        if x.get("status") == "PENDING" and exp and exp <= now:
            x["status"] = "EXPIRED"
        out.append(x)
    return out


def _file_fetch_pending(order_by_key: str, limit: int = 5) -> List[Dict[str, Any]]:
    items = _file_mark_expired(_load_file_pending())
    _save_file_pending(items)
    pending = [x for x in items if x.get("status") == "PENDING" and (int(x.get("expires_at", 0) or 0) > _now_ts())]
    pending.sort(key=lambda r: r.get(order_by_key, 0), reverse=True)
    return pending[:limit]


def _file_get_pending_by_id(prebuy_id: str) -> Optional[Dict[str, Any]]:
    items = _file_mark_expired(_load_file_pending())
    _save_file_pending(items)
    now = _now_ts()
    for x in items:
        if x.get("id") == prebuy_id and x.get("status") == "PENDING" and int(x.get("expires_at", 0) or 0) > now:
            return x
    return None


def _file_set_approved(prebuy_id: str, amount: float) -> bool:
    items = _load_file_pending()
    now = _now_ts()
    changed = False
    for x in items:
        if x.get("id") == prebuy_id and x.get("status") == "PENDING" and int(x.get("expires_at", 0) or 0) > now:
            x["status"] = "APPROVED"
            x["approved_amount"] = float(amount)
            x["approved_at"] = now
            changed = True
            break
    if changed:
        _save_file_pending(items)
    return changed


def _file_set_consumed(prebuy_id: str) -> None:
    items = _load_file_pending()
    for x in items:
        if x.get("id") == prebuy_id and x.get("status") in {"PENDING", "APPROVED"}:
            x["status"] = "CONSUMED"
    _save_file_pending(items)


def _format_rows(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "Geen PENDING Pre-BUYs gevonden (of alles is verlopen)."

    lines = []
    for r in rows:
        symbol = (r.get("symbol") or r.get("coin") or "").strip()
        pid = r.get("id")
        label = r.get("label") or ""
        score = r.get("score")
        entry = r.get("entry")
        sl = r.get("stop_loss") or r.get("stop")
        target = r.get("target")
        lines.append(
            f"{pid} | {symbol} | {label} | score={score} | entry={entry} | stop={sl} | target={target}"
        )

    lines.append("")
    lines.append("Bevestig: YES <bedrag> <ID>")
    lines.append("Voorbeeld: YES 10 PB-ADAUSDT-1234567890")
    return "\n".join(lines)


def _parse_yes(msg: str) -> Optional[Tuple[float, str]]:
    # YES 10 PB-XXX
    m = re.match(r"^\s*YES\s+(\d+)\s+([A-Za-z0-9\-_]+)\s*$", msg, flags=re.IGNORECASE)
    if not m:
        return None
    amount = float(m.group(1))
    pid = m.group(2).strip()
    return amount, pid


# ========= ROUTE =========
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    try:
        _ensure_db_schema()

        body = (request.form.get("Body") or "").strip()
        msg = body.upper().strip()
        from_number = (request.form.get("From") or "").strip()

        # (optioneel) beveiliging met INTERNAL_TOKEN via querystring/header
        # als je dit wilt afdwingen: zet REQUIRE_INTERNAL_TOKEN=1 en check token
        # (nu niet hard afdwingen om je flow niet te slopen)

        if msg in {"HELP", "H"}:
            return _resp(
                "Commands:\n"
                "HELP\n"
                "LIST (laatste PENDING pre-buys)\n"
                "TOP (beste 5 op score)\n"
                "YES <bedrag> <ID>\n"
                "NO <ID>\n"
            )

        if msg.startswith("NO "):
            prebuy_id = body.split(" ", 1)[1].strip()
            if USE_DB_APPROVALS:
                with _connect() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE pending_approvals SET status='REJECTED' WHERE id=%s AND status='PENDING';",
                            (prebuy_id,),
                        )
                        conn.commit()
                return _resp(f"❌ Afgewezen: {prebuy_id}")
            else:
                items = _load_file_pending()
                for x in items:
                    if x.get("id") == prebuy_id and x.get("status") == "PENDING":
                        x["status"] = "REJECTED"
                _save_file_pending(items)
                return _resp(f"❌ Afgewezen: {prebuy_id}")

        if msg == "LIST":
            if USE_DB_APPROVALS:
                rows = _db_fetch_pending(order_by="created_at DESC", limit=5)
                return _resp(_format_rows(rows))
            rows = _file_fetch_pending(order_by_key="created_at", limit=5)
            return _resp(_format_rows(rows))

        if msg == "TOP":
            if USE_DB_APPROVALS:
                rows = _db_fetch_pending(order_by="score DESC, created_at DESC", limit=5)
                return _resp(_format_rows(rows))
            rows = _file_fetch_pending(order_by_key="score", limit=5)
            return _resp(_format_rows(rows))

        yes = _parse_yes(body)
        if yes:
            amount, prebuy_id = yes
            if int(amount) not in ALLOWED_AMOUNTS:
                return _resp(f"⚠️ Ongeldig bedrag. Toegestaan: {sorted(ALLOWED_AMOUNTS)}")

            if USE_DB_APPROVALS:
                row = _db_get_pending_by_id(prebuy_id)
                if not row:
                    return _resp("Geen PENDING Pre-BUY gevonden (met die ID). Doe eerst LIST.")

                # mark approved
                ok = _db_set_approved(prebuy_id, amount, approved_by=from_number or "unknown")
                if not ok:
                    return _resp("⚠️ Kon niet goedkeuren (misschien verlopen of al verwerkt).")

                # ----- bereken stop/target defaults als ze ontbreken -----
                symbol = (row.get("symbol") or row.get("coin") or "").strip()
                entry = float(row.get("entry") or 0.0)
                stop_loss = float(row.get("stop_loss") or row.get("stop") or 0.0)
                target = float(row.get("target") or 0.0)

                if entry <= 0:
                    return _resp("⚠️ Pre-BUY mist entry. Kan BUY niet uitvoeren.")

                if stop_loss <= 0:
                    stop_loss = entry * 0.98  # default 2% risk

                if target <= 0:
                    risk = max(entry - stop_loss, entry * 0.02)
                    target = entry + 2.0 * risk  # 2R default

                # ✅ JUISTE buy_eur SIGNATURE
                # buy_eur(symbol, amount_eur, stop_loss, target, prebuy_id)
                try:
                    res = buy_eur(
                        symbol=symbol,
                        amount_eur=float(amount),
                        stop_loss=float(stop_loss),
                        target=float(target),
                        prebuy_id=prebuy_id,
                    )
                except Exception as e:
                    return _resp(f"⚠️ BUY niet uitgevoerd: {e}")

                if not isinstance(res, dict) or not res.get("ok"):
                    return _resp(f"⚠️ BUY niet uitgevoerd: {res}")

                _db_set_consumed(prebuy_id)

                return _resp(
                    "🟢 LIVE BUY uitgevoerd (Bitvavo)\n"
                    f"ID: {prebuy_id}\n"
                    f"{symbol} | €{int(amount)}\n"
                    f"Entry: {res.get('price')}\n"
                    f"Qty: {res.get('qty')}\n"
                    f"Stop: {stop_loss}\n"
                    f"Target: {target}\n"
                    "Status: CONSUMED"
                )

            # ====== FILE MODE ======
            row = _file_get_pending_by_id(prebuy_id)
            if not row:
                return _resp("Geen PENDING Pre-BUY gevonden (met die ID). Doe eerst LIST.")

            ok = _file_set_approved(prebuy_id, amount)
            if not ok:
                return _resp("⚠️ Kon niet goedkeuren (misschien verlopen of al verwerkt).")

            symbol = (row.get("symbol") or row.get("coin") or "").strip()
            entry = float(row.get("entry") or 0.0)
            stop_loss = float(row.get("stop_loss") or row.get("stop") or 0.0)
            target = float(row.get("target") or 0.0)

            if entry <= 0:
                return _resp("⚠️ Pre-BUY mist entry. Kan BUY niet uitvoeren.")

            if stop_loss <= 0:
                stop_loss = entry * 0.98

            if target <= 0:
                risk = max(entry - stop_loss, entry * 0.02)
                target = entry + 2.0 * risk

            try:
                res = buy_eur(
                    symbol=symbol,
                    amount_eur=float(amount),
                    stop_loss=float(stop_loss),
                    target=float(target),
                    prebuy_id=prebuy_id,
                )
            except Exception as e:
                return _resp(f"⚠️ BUY niet uitgevoerd: {e}")

            if not isinstance(res, dict) or not res.get("ok"):
                return _resp(f"⚠️ BUY niet uitgevoerd: {res}")

            _file_set_consumed(prebuy_id)

            return _resp(
                "🟢 LIVE BUY uitgevoerd (Bitvavo)\n"
                f"ID: {prebuy_id}\n"
                f"{symbol} | €{int(amount)}\n"
                f"Entry: {res.get('price')}\n"
                f"Qty: {res.get('qty')}\n"
                f"Stop: {stop_loss}\n"
                f"Target: {target}\n"
                "Status: CONSUMED"
            )

        # unknown
        return _resp("Onbekend commando. Stuur HELP.")

    except Exception as e:
        # dit is je “Interne fout in webhook”
        print("WEBHOOK_ERROR:", e, flush=True)
        traceback.print_exc()
        return _resp("❌ Interne fout in webhook. Check Render logs (Traceback).", status=200)


if __name__ == "__main__":
    # Render bindt PORT automatisch
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
