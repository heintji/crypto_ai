# whatsapp_webhook.py
from __future__ import annotations

import os
import re
import json
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple, List

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, Response

# Twilio (TwiML response)
from twilio.twiml.messaging_response import MessagingResponse

# ==========================================================
# ENV
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL".lower()) or "").strip()
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()

# Optional (voor logging/links)
WEBHOOK_BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or "").strip().rstrip("/")

# Allowed stake amounts
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

# Validity (seconds)
PREBUY_VALID_SECONDS = int(os.getenv("PREBUY_VALID_SECONDS") or str(4 * 60 * 60))

# Live trading toggle (alleen als je live buy echt hebt gebouwd)
LIVE_TRADING = (os.getenv("LIVE_TRADING") or "0").strip() == "1"

# ==========================================================
# APP
# ==========================================================
app = Flask(__name__)


# ==========================================================
# Helpers
# ==========================================================
def now_epoch() -> int:
    return int(time.time())


def dt_to_epoch(v: Any) -> Optional[int]:
    """Convert DB datetime/timestamp/epoch to int epoch safely."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return int(v.timestamp())
    # string?
    try:
        # try parse ISO
        vv = str(v).strip()
        if not vv:
            return None
        # If already numeric
        if vv.isdigit():
            return int(vv)
        # ISO datetime
        dt = datetime.fromisoformat(vv.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def twiml(text: str) -> Response:
    resp = MessagingResponse()
    resp.message(text)
    return Response(str(resp), mimetype="application/xml")


def log(msg: str) -> None:
    print(msg, flush=True)


def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt in env.")
    return psycopg2.connect(DATABASE_URL)


def ensure_schema() -> None:
    sql = """
    CREATE TABLE IF NOT EXISTS pending_approvals (
        id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        setup_type TEXT,
        market_regime TEXT,
        score INT,
        label TEXT,
        entry NUMERIC,
        stop NUMERIC,
        target NUMERIC,
        status TEXT NOT NULL DEFAULT 'PENDING',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at BIGINT,
        payload_json JSONB,
        bot_confidence INT
    );

    CREATE INDEX IF NOT EXISTS idx_pending_status_created
        ON pending_approvals(status, created_at DESC);

    CREATE INDEX IF NOT EXISTS idx_pending_score
        ON pending_approvals(score DESC);
    """
    conn = db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
    finally:
        conn.close()


def fetch_pending(limit: int = 10) -> List[Dict[str, Any]]:
    conn = db_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # created_at houden we als datetime -> later veilig converteren
            cur.execute(
                """
                SELECT id, symbol, setup_type, market_regime, score, label, entry, stop, target,
                       status, created_at, expires_at
                FROM pending_approvals
                WHERE status='PENDING'
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall() or []
            return [dict(r) for r in rows]
    finally:
        conn.close()


def fetch_top(limit: int = 5) -> List[Dict[str, Any]]:
    conn = db_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, symbol, setup_type, market_regime, score, label, entry, stop, target,
                       status, created_at, expires_at
                FROM pending_approvals
                WHERE status='PENDING'
                ORDER BY score DESC NULLS LAST, created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall() or []
            return [dict(r) for r in rows]
    finally:
        conn.close()


def find_pending_by_id(prebuy_id: str) -> Optional[Dict[str, Any]]:
    conn = db_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, symbol, setup_type, market_regime, score, label, entry, stop, target,
                       status, created_at, expires_at, payload_json
                FROM pending_approvals
                WHERE id=%s
                """,
                (prebuy_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def fetch_latest_pending() -> Optional[Dict[str, Any]]:
    rows = fetch_pending(limit=1)
    return rows[0] if rows else None


def mark_approved(prebuy_id: str, stake_eur: int) -> None:
    conn = db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE pending_approvals
                    SET status='APPROVED'
                    WHERE id=%s AND status='PENDING'
                    """,
                    (prebuy_id,),
                )
    finally:
        conn.close()


def mark_rejected(prebuy_id: str) -> None:
    conn = db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE pending_approvals
                    SET status='REJECTED'
                    WHERE id=%s AND status='PENDING'
                    """,
                    (prebuy_id,),
                )
    finally:
        conn.close()


def is_expired(row: Dict[str, Any]) -> bool:
    exp = row.get("expires_at")
    exp_epoch = dt_to_epoch(exp)
    if exp_epoch is None:
        # fallback: created_at + PREBUY_VALID_SECONDS
        created = dt_to_epoch(row.get("created_at")) or now_epoch()
        exp_epoch = created + PREBUY_VALID_SECONDS
    return now_epoch() > int(exp_epoch)


def try_execute_buy(row: Dict[str, Any], stake_eur: int) -> Tuple[bool, str]:
    """
    Probeert BUY uit te voeren.
    - Als LIVE_TRADING=1 en je hebt een live trader module: voer echte buy uit.
    - Anders: paper buy (of skip met duidelijke melding).
    """
    symbol = row.get("symbol") or ""
    entry = float(row.get("entry") or 0.0)
    stop = float(row.get("stop") or 0.0)
    target = float(row.get("target") or 0.0)

    # 1) Live trading (alleen als jij het echt hebt)
    if LIVE_TRADING:
        try:
            # pas dit import-pad aan naar jouw echte live executor als die bestaat
            from trading.bitvavo_trader import buy_eur as live_buy_eur  # type: ignore
            ok, info = live_buy_eur(symbol=symbol, eur_amount=stake_eur)
            return bool(ok), f"LIVE BUY: {info}"
        except Exception as e:
            return False, f"LIVE BUY faalde: {e}"

    # 2) Paper trading (werkt in elk geval als module bestaat)
    try:
        from trading.paper_trader import buy_eur  # type: ignore

        ok, info = buy_eur(symbol=symbol, eur_amount=stake_eur, entry_hint=entry, stop_loss=stop, target=target)
        return bool(ok), f"PAPER BUY: {info}"
    except Exception as e:
        return False, f"BUY niet uitgevoerd (geen paper/live koppeling): {e}"


# ==========================================================
# Routes
# ==========================================================
@app.get("/")
def root():
    # health checks moeten nooit 502 geven
    return "OK", 200


@app.post("/internal/prebuy")
def internal_prebuy():
    """
    Optioneel endpoint zodat multi_coin_score.py prebuys kan posten:
    Headers:
      X-Internal-Token: <INTERNAL_TOKEN>
    Body JSON: {id, symbol, score, label, entry, stop, target, setup_type, market_regime, expires_at, payload_json, bot_confidence}
    """
    try:
        token = (request.headers.get("X-Internal-Token") or "").strip()
        if not INTERNAL_TOKEN or token != INTERNAL_TOKEN:
            return ("Forbidden", 403)

        data = request.get_json(force=True) or {}
        prebuy_id = str(data.get("id") or "").strip()
        symbol = str(data.get("symbol") or data.get("coin") or "").strip().upper()
        if not prebuy_id or not symbol:
            return ("Missing id/symbol", 400)

        ensure_schema()

        conn = db_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO pending_approvals
                        (id, symbol, setup_type, market_regime, score, label, entry, stop, target, status, expires_at, payload_json, bot_confidence)
                        VALUES
                        (%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s,%s,%s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (
                            prebuy_id,
                            symbol,
                            data.get("setup_type"),
                            data.get("market_regime"),
                            data.get("score"),
                            data.get("label") or data.get("grade"),
                            data.get("entry"),
                            data.get("stop") or data.get("stop_loss"),
                            data.get("target"),
                            int(data.get("expires_at")) if str(data.get("expires_at") or "").isdigit() else None,
                            json.dumps(data.get("payload_json")) if isinstance(data.get("payload_json"), dict) else data.get("payload_json"),
                            data.get("bot_confidence"),
                        ),
                    )
            return ("OK", 200)
        finally:
            conn.close()

    except Exception:
        log("INTERNAL_PREBUY_ERROR\n" + traceback.format_exc())
        return ("ERROR", 500)


@app.post("/whatsapp")
def whatsapp():
    """
    Commands:
      HELP
      LIST  -> laatste PENDING prebuys
      TOP   -> beste 5 op score (PENDING)
      YES <bedrag> <ID>  (ID optioneel -> pakt laatste)
      NO <ID>            (ID optioneel -> pakt laatste)
    """
    try:
        ensure_schema()

        body_raw = (request.form.get("Body") or "").strip()
        body = body_raw.upper().strip()
        from_number = (request.form.get("From") or "").strip()

        log(f"WHATSAPP_IN: from={from_number} body={body_raw}")

        if not body:
            return twiml("Stuur HELP voor commando’s.")

        if body in ("HELP", "H"):
            return twiml(
                "Commands:\n"
                "HELP\n"
                "LIST (laatste PENDING pre-buys)\n"
                "TOP (beste 5 op score)\n"
                "YES <bedrag> <ID>\n"
                "NO <ID>\n\n"
                "Voorbeeld: YES 10 PB-BTCUSDT-1771100252"
            )

        if body.startswith("LIST"):
            rows = fetch_pending(limit=10)
            if not rows:
                return twiml("Geen PENDING Pre-BUYs gevonden.")
            lines = []
            for r in rows:
                created = dt_to_epoch(r.get("created_at")) or now_epoch()
                expires_at = dt_to_epoch(r.get("expires_at")) or (created + PREBUY_VALID_SECONDS)
                mins_left = max(0, int((expires_at - now_epoch()) / 60))
                lines.append(
                    f"{r.get('id')} | {r.get('symbol')} | {r.get('label') or '-'} | score={r.get('score')} | "
                    f"entry={r.get('entry')} | stop={r.get('stop')} | target={r.get('target')} | exp {mins_left}m"
                )
            return twiml("\n".join(lines) + "\n\nBevestig: YES <bedrag> <ID>")

        if body.startswith("TOP"):
            rows = fetch_top(limit=5)
            if not rows:
                return twiml("Geen PENDING Pre-BUYs gevonden.")
            lines = []
            for r in rows:
                created = dt_to_epoch(r.get("created_at")) or now_epoch()
                expires_at = dt_to_epoch(r.get("expires_at")) or (created + PREBUY_VALID_SECONDS)
                mins_left = max(0, int((expires_at - now_epoch()) / 60))
                lines.append(
                    f"{r.get('id')} | {r.get('symbol')} | {r.get('label') or '-'} | score={r.get('score')} | "
                    f"entry={r.get('entry')} | stop={r.get('stop')} | target={r.get('target')} | exp {mins_left}m"
                )
            return twiml("TOP 5:\n" + "\n".join(lines) + "\n\nBevestig: YES <bedrag> <ID>")

        # YES <amount> <id?>
        m_yes = re.match(r"^YES\s+(\d+)(?:\s+(.+))?$", body, re.IGNORECASE)
        if m_yes:
            amount = int(m_yes.group(1))
            if amount not in ALLOWED_AMOUNTS:
                return twiml(f"Onjuist bedrag. Toegestaan: {sorted(ALLOWED_AMOUNTS)}")

            prebuy_id = (m_yes.group(2) or "").strip()
            row = find_pending_by_id(prebuy_id) if prebuy_id else fetch_latest_pending()

            if not row:
                return twiml("Geen PENDING Pre-BUY gevonden. Doe eerst LIST of TOP.")

            if row.get("status") != "PENDING":
                return twiml(f"Deze Pre-BUY is niet meer PENDING (status={row.get('status')}).")

            if is_expired(row):
                return twiml("Deze Pre-BUY is verlopen. Doe opnieuw LIST/TOP.")

            mark_approved(row["id"], amount)

            # Probeer BUY uit te voeren (paper of live)
            ok, info = try_execute_buy(row, amount)

            msg = (
                "✅ GOEDKEURING ONTVANGEN\n"
                f"ID: {row.get('id')}\n"
                f"Inzet: €{amount}\n"
                f"{row.get('symbol')} | {row.get('label') or '-'} | score={row.get('score')}\n"
                f"entry={row.get('entry')} stop={row.get('stop')} target={row.get('target')}\n"
                f"{'✅' if ok else '⚠️'} {info}"
            )
            return twiml(msg)

        # NO <id?>
        m_no = re.match(r"^NO(?:\s+(.+))?$", body, re.IGNORECASE)
        if m_no:
            prebuy_id = (m_no.group(1) or "").strip()
            row = find_pending_by_id(prebuy_id) if prebuy_id else fetch_latest_pending()
            if not row:
                return twiml("Geen PENDING Pre-BUY gevonden om te weigeren. Doe eerst LIST/TOP.")
            if row.get("status") != "PENDING":
                return twiml(f"Deze Pre-BUY is niet meer PENDING (status={row.get('status')}).")
            mark_rejected(row["id"])
            return twiml(f"❌ Afgewezen: {row.get('id')} ({row.get('symbol')})")

        return twiml("Onbekend commando. Stuur HELP.")

    except Exception as e:
        # NOOIT crashen richting Twilio: altijd nette response
        log("WHATSAPP_WEBHOOK_ERROR\n" + traceback.format_exc())
        return twiml(f"❌ Interne fout in webhook. Check Render logs. ({type(e).__name__}: {e})")


# ==========================================================
# Main
# ==========================================================
if __name__ == "__main__":
    ensure_schema()
    port = int(os.getenv("PORT") or "10000")
    app.run(host="0.0.0.0", port=port, debug=False)
