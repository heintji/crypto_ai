# whatsapp_webhook.py
# Web Service (Render) - Twilio WhatsApp inbound
#
# Functies:
# - LIST  -> toont openstaande Pre-BUYs (PENDING en niet verlopen)
# - YES <bedrag> [PREBUY_ID] -> approve + BUY (paper/live via paper_trader.buy_eur)
# - NO [PREBUY_ID] -> reject (zet status REJECTED)
# - HELP -> commando’s
#
# Opslag:
# - Postgres (DATABASE_URL) voor pending_approvals, trade_fingerprints, prebuy_state
# - /data alleen voor paper_state.json/paper_trades.csv etc. (paper_trader/trade_monitor)
#
# Belangrijk:
# - Ondersteunt zowel WHATSAPP_FROM/WHATSAPP_TO als TWILIO_WHATSAPP_FROM/TWILIO_WHATSAPP_TO
# - Returnt altijd TwiML (MessagingResponse), geen “stil” 204 gedrag.

from __future__ import annotations

import os
import re
import time
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse


# =========================================================
# ENV
# =========================================================
# Twilio credentials (verplicht)
TWILIO_ACCOUNT_SID = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()

# WhatsApp routing (ondersteun beide varianten)
WHATSAPP_FROM = (
    (os.getenv("WHATSAPP_FROM") or "").strip()
    or (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
)
WHATSAPP_TO = (
    (os.getenv("WHATSAPP_TO") or "").strip()
    or (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()
)

# Postgres
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

# Optional: simpele beveiliging, als je later een interne endpoint maakt
INTERNAL_TOKEN = (os.getenv("INTERNAL_TOKEN") or "").strip()

# BUY settings
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}
DEFAULT_STOP_PCT = float(os.getenv("DEFAULT_STOP_PCT") or "0.02")  # 2% onder entry
DEFAULT_TARGET_R = float(os.getenv("DEFAULT_TARGET_R") or "2.0")   # 2R target
MAX_LIST = int(os.getenv("MAX_LIST") or "10")

# Logging
DEBUG = (os.getenv("DEBUG") or "0").strip() == "1"

# =========================================================
# Imports (execution)
# =========================================================
# Let op: jouw paper_trader.buy_eur signature verwacht:
# buy_eur(symbol, eur_amount, stop_loss, target, prebuy_id)
from trading.paper_trader import buy_eur  # noqa: E402


app = Flask(__name__)


# =========================================================
# DB helpers
# =========================================================
def _db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt in env.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def _db_init() -> None:
    """
    Maak tabellen aan als ze nog niet bestaan.
    (Je had dit al gedaan in shell, maar dit houdt het “self-healing”.)
    """
    q_pending = """
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
        regime TEXT,
        market_condition TEXT,
        confidence INTEGER,
        overextended DOUBLE PRECISION,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING',
        approved_amount_eur INTEGER,
        approved_at TIMESTAMPTZ,
        rejected_at TIMESTAMPTZ,
        consumed_at TIMESTAMPTZ
    );
    """

    # fingerprint table: alleen fingerprint + last_created_at nodig
    q_fp = """
    CREATE TABLE IF NOT EXISTS trade_fingerprints (
        fingerprint TEXT PRIMARY KEY,
        last_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """

    # prebuy_state: daily counter (optioneel)
    q_state = """
    CREATE TABLE IF NOT EXISTS prebuy_state (
        day DATE PRIMARY KEY,
        created_count INTEGER NOT NULL DEFAULT 0
    );
    """

    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q_pending)
            cur.execute(q_fp)
            cur.execute(q_state)
        conn.commit()


# =========================================================
# Domain helpers
# =========================================================
@dataclass
class PendingPrebuy:
    id: str
    symbol: str
    setup_type: Optional[str]
    label: Optional[str]
    score: Optional[int]
    entry: Optional[float]
    stop_loss: Optional[float]
    target: Optional[float]
    expires_at: str  # keep as ISO string for display
    regime: Optional[str]


def _twiml(text: str) -> Response:
    resp = MessagingResponse()
    resp.message(text)
    return Response(str(resp), mimetype="application/xml")


def _norm_msg(body: str) -> str:
    return (body or "").strip()


def _parse_yes(msg: str) -> Optional[Tuple[int, Optional[str]]]:
    """
    YES 10
    YES 20 PB-...
    YES 100 PB-...
    """
    m = re.match(r"^\s*YES\s+(\d+)(?:\s+([A-Za-z0-9\-_\.]+))?\s*$", msg, flags=re.I)
    if not m:
        return None
    amt = int(m.group(1))
    pid = m.group(2)
    return amt, pid


def _parse_no(msg: str) -> Optional[Optional[str]]:
    """
    NO
    NO PB-...
    """
    m = re.match(r"^\s*NO(?:\s+([A-Za-z0-9\-_\.]+))?\s*$", msg, flags=re.I)
    if not m:
        return None
    pid = m.group(1)
    return pid


def _fetch_pending(limit: int = 10) -> List[PendingPrebuy]:
    q = """
    SELECT id, symbol, setup_type, label, score, entry, stop_loss, target,
           expires_at, regime
    FROM pending_approvals
    WHERE status = 'PENDING'
      AND expires_at > NOW()
    ORDER BY created_at ASC
    LIMIT %s;
    """
    with _db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(q, (limit,))
            rows = cur.fetchall()

    out: List[PendingPrebuy] = []
    for r in rows:
        out.append(
            PendingPrebuy(
                id=r["id"],
                symbol=r["symbol"],
                setup_type=r["setup_type"],
                label=r["label"],
                score=r["score"],
                entry=r["entry"],
                stop_loss=r["stop_loss"],
                target=r["target"],
                expires_at=str(r["expires_at"]),
                regime=r["regime"],
            )
        )
    return out


def _get_pending_by_id(prebuy_id: str) -> Optional[PendingPrebuy]:
    q = """
    SELECT id, symbol, setup_type, label, score, entry, stop_loss, target,
           expires_at, regime
    FROM pending_approvals
    WHERE id = %s
    LIMIT 1;
    """
    with _db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(q, (prebuy_id,))
            r = cur.fetchone()

    if not r:
        return None

    return PendingPrebuy(
        id=r["id"],
        symbol=r["symbol"],
        setup_type=r["setup_type"],
        label=r["label"],
        score=r["score"],
        entry=r["entry"],
        stop_loss=r["stop_loss"],
        target=r["target"],
        expires_at=str(r["expires_at"]),
        regime=r["regime"],
    )


def _mark_rejected(prebuy_id: str) -> bool:
    q = """
    UPDATE pending_approvals
    SET status='REJECTED', rejected_at=NOW()
    WHERE id=%s AND status='PENDING';
    """
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (prebuy_id,))
            updated = cur.rowcount
        conn.commit()
    return updated > 0


def _mark_approved(prebuy_id: str, amount_eur: int) -> bool:
    q = """
    UPDATE pending_approvals
    SET status='APPROVED', approved_at=NOW(), approved_amount_eur=%s
    WHERE id=%s AND status='PENDING' AND expires_at > NOW();
    """
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (amount_eur, prebuy_id))
            updated = cur.rowcount
        conn.commit()
    return updated > 0


def _mark_consumed(prebuy_id: str) -> None:
    q = """
    UPDATE pending_approvals
    SET status='CONSUMED', consumed_at=NOW()
    WHERE id=%s AND status='APPROVED';
    """
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (prebuy_id,))
        conn.commit()


def _calc_defaults(entry: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    """
    Als stop/target niet meegeleverd zijn, bereken:
    stop = entry*(1-DEFAULT_STOP_PCT)
    target = entry + DEFAULT_TARGET_R*(entry-stop)
    """
    if entry is None:
        return None, None
    stop = entry * (1.0 - DEFAULT_STOP_PCT)
    risk = entry - stop
    target = entry + DEFAULT_TARGET_R * risk
    return stop, target


def _format_list(items: List[PendingPrebuy]) -> str:
    if not items:
        return "Geen PENDING Pre-BUYs gevonden (of alles is verlopen)."

    lines = ["📌 PENDING Pre-BUYs:"]
    for i, p in enumerate(items, start=1):
        label = p.label or "-"
        st = p.setup_type or "-"
        sc = p.score if p.score is not None else "-"
        entry = f"{p.entry:.6g}" if p.entry is not None else "?"
        stop = f"{p.stop_loss:.6g}" if p.stop_loss is not None else "auto"
        tgt = f"{p.target:.6g}" if p.target is not None else "auto"
        reg = p.regime or "-"
        lines.append(
            f"{i}) {p.symbol} | {label} | score {sc} | {st} | entry {entry} | SL {stop} | TG {tgt} | regime {reg}\n"
            f"    ID: {p.id}"
        )
    lines.append("")
    lines.append("✅ Goedkeuren: YES 10 [ID]")
    lines.append("❌ Afwijzen: NO [ID]")
    lines.append("ℹ️ Help: HELP")
    return "\n".join(lines)


def _format_help() -> str:
    return (
        "📣 Commando’s:\n"
        "• LIST  → toon openstaande Pre-BUYs\n"
        "• YES <bedrag> [ID] → approve + BUY (bedrag: 5/10/15/20/30/100)\n"
        "• NO [ID] → afwijzen\n"
        "• HELP → dit overzicht\n\n"
        "Voorbeeld: YES 20 PB-XRPUSDT-1700000000"
    )


# =========================================================
# Routes
# =========================================================
@app.get("/")
def health():
    return {"ok": True, "service": "whatsapp_webhook"}


@app.post("/whatsapp")
def whatsapp_webhook():
    """
    Twilio webhook endpoint.
    """
    body = _norm_msg(request.form.get("Body", ""))
    from_number = (request.form.get("From", "") or "").strip()

    if DEBUG:
        print(f"[WHATSAPP_IN] from={from_number} body={body}")

    # Always init DB (safe)
    try:
        _db_init()
    except Exception as e:
        if DEBUG:
            print(f"[DB_INIT_ERROR] {e}")
        return _twiml("❌ DB fout: DATABASE_URL of schema probleem. Check Render logs.")

    if not body:
        return _twiml(_format_help())

    up = body.strip().upper()

    # HELP
    if up in {"HELP", "H", "?"}:
        return _twiml(_format_help())

    # LIST
    if up in {"LIST", "LS", "PENDING"}:
        try:
            items = _fetch_pending(limit=MAX_LIST)
            return _twiml(_format_list(items))
        except Exception as e:
            if DEBUG:
                print(f"[LIST_ERROR] {e}")
            return _twiml("❌ Kon LIST niet ophalen. Check Render logs (DB).")

    # NO
    no_pid = _parse_no(body)
    if no_pid is not None:
        try:
            if no_pid:
                target_id = no_pid
            else:
                # reject oldest pending
                items = _fetch_pending(limit=1)
                if not items:
                    return _twiml("Geen PENDING Pre-BUYs om af te wijzen.")
                target_id = items[0].id

            ok = _mark_rejected(target_id)
            if not ok:
                return _twiml("❌ Niet gelukt: ID bestaat niet of is niet meer PENDING.")
            return _twiml(f"✅ Afgewezen: {target_id}")
        except Exception as e:
            if DEBUG:
                print(f"[NO_ERROR] {e}")
            return _twiml("❌ Fout bij NO. Check Render logs.")

    # YES
    parsed = _parse_yes(body)
    if parsed:
        amount_eur, maybe_id = parsed

        if amount_eur not in ALLOWED_AMOUNTS:
            return _twiml(
                f"❌ Bedrag niet toegestaan. Kies uit: {sorted(ALLOWED_AMOUNTS)}\n"
                "Voorbeeld: YES 20 [ID]"
            )

        try:
            if maybe_id:
                p = _get_pending_by_id(maybe_id)
                if not p:
                    return _twiml("❌ ID niet gevonden.")
                target_id = p.id
            else:
                items = _fetch_pending(limit=1)
                if not items:
                    return _twiml("Geen PENDING Pre-BUYs om goed te keuren.")
                p = items[0]
                target_id = p.id

            # check expire
            # (db update doet ook expires_at > NOW() check)
            approved = _mark_approved(target_id, amount_eur)
            if not approved:
                return _twiml("❌ Niet gelukt: trade is verlopen of niet meer PENDING.")

            # defaults for stop/target if missing
            stop = p.stop_loss
            target = p.target
            if stop is None or target is None:
                d_stop, d_target = _calc_defaults(p.entry)
                stop = stop if stop is not None else d_stop
                target = target if target is not None else d_target

            if stop is None or target is None:
                return _twiml("❌ Kan geen stop/target bepalen (entry ontbreekt).")

            # Execute BUY (paper)
            # buy_eur(symbol, eur_amount, stop_loss, target, prebuy_id)
            buy_eur(p.symbol, amount_eur, stop, target, target_id)

            # mark consumed after execution
            _mark_consumed(target_id)

            msg = (
                "✅ BUY uitgevoerd (paper)\n"
                f"• {p.symbol}\n"
                f"• Inzet: €{amount_eur}\n"
                f"• Stop-loss: {stop:.6g}\n"
                f"• Target: {target:.6g}\n"
                f"• PreBUY-ID: {target_id}"
            )
            return _twiml(msg)

        except Exception as e:
            if DEBUG:
                print(f"[YES_ERROR] {e}")
            # rollback approve if buy fails (best-effort)
            try:
                with _db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE pending_approvals SET status='PENDING', approved_at=NULL, approved_amount_eur=NULL "
                            "WHERE id=%s AND status='APPROVED';",
                            (target_id,),
                        )
                    conn.commit()
            except Exception:
                pass

            return _twiml(f"❌ BUY niet uitgevoerd: {str(e)}")

    # Default fallback
    return _twiml(
        "Onbekend commando.\n\n"
        + _format_help()
    )


# =========================================================
# Local run
# =========================================================
if __name__ == "__main__":
    # Render gebruikt meestal gunicorn, maar dit is prima voor lokaal testen
    port = int(os.getenv("PORT") or "10000")
    app.run(host="0.0.0.0", port=port, debug=False)
