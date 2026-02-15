# whatsapp_webhook.py
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import psycopg2
import psycopg2.extras
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

# ==========================================================
# CONFIG
# ==========================================================
app = Flask(__name__)

DEBUG = (os.getenv("DEBUG") or "0").strip() == "1"

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL ontbreekt (Render Postgres).")

# Twilio creds (voor outbound WhatsApp berichten)
TWILIO_ACCOUNT_SID = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()

# Support beide naamvarianten (jij gebruikt WHATSAPP_FROM/TO)
WHATSAPP_FROM = (os.getenv("WHATSAPP_FROM") or os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
WHATSAPP_TO = (os.getenv("WHATSAPP_TO") or os.getenv("TWILIO_WHATSAPP_TO") or "").strip()

ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

DEFAULT_TOP_LIMIT = int(os.getenv("DEFAULT_TOP_LIMIT") or "5")
MAX_TOP_LIMIT = int(os.getenv("MAX_TOP_LIMIT") or "15")

# Kans-van-slagen weging
# confidence 0-100 (als je die al logt), score (0-100), regime bonus
WEIGHT_CONFIDENCE = float(os.getenv("WEIGHT_CONFIDENCE") or "0.65")
WEIGHT_SCORE = float(os.getenv("WEIGHT_SCORE") or "0.35")

REGIME_BONUS_BULL = float(os.getenv("REGIME_BONUS_BULL") or "6")     # bull = iets meer kans
REGIME_BONUS_RANGE = float(os.getenv("REGIME_BONUS_RANGE") or "0")   # range = neutraal
REGIME_BONUS_BEAR = float(os.getenv("REGIME_BONUS_BEAR") or "-8")    # bear = lagere kans

# ==========================================================
# DATA MODELS
# ==========================================================
@dataclass
class PendingPrebuy:
    id: str
    symbol: str
    setup_type: str
    label: str
    score: int
    entry: float
    stop_loss: float
    target: float
    regime: Optional[str]
    expires_at: str
    confidence: Optional[int] = None


# ==========================================================
# DB HELPERS
# ==========================================================
def _db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def _ensure_tables() -> None:
    """
    Zorg dat minimaal de tabel(s) bestaan die webhook gebruikt.
    """
    q1 = """
    CREATE TABLE IF NOT EXISTS pending_approvals (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'PENDING',
        symbol TEXT NOT NULL,
        setup_type TEXT,
        label TEXT,
        score INTEGER,
        entry DOUBLE PRECISION,
        stop_loss DOUBLE PRECISION,
        target DOUBLE PRECISION,
        regime TEXT,
        confidence INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at TIMESTAMPTZ NOT NULL
    );
    """
    q2 = "CREATE INDEX IF NOT EXISTS idx_pending_status_expires ON pending_approvals(status, expires_at);"

    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q1)
            cur.execute(q2)
        conn.commit()


def _twiml(text: str):
    resp = MessagingResponse()
    resp.message(text)
    return str(resp)


def _parse_top_limit(msg: str) -> int:
    """
    TOP -> default limit
    TOP 10 -> limit=10
    """
    m = re.search(r"\bTOP\s+(\d+)\b", msg.upper())
    if not m:
        return DEFAULT_TOP_LIMIT
    n = int(m.group(1))
    if n < 1:
        return DEFAULT_TOP_LIMIT
    return min(n, MAX_TOP_LIMIT)


def _derive_confidence(score: Optional[int]) -> int:
    """
    Als confidence niet aanwezig is: schat grof op basis van score.
    """
    s = int(score or 0)
    # eenvoudige mapping (kan later beter met scoreboard)
    if s >= 90:
        return 85
    if s >= 85:
        return 78
    if s >= 80:
        return 72
    if s >= 75:
        return 65
    if s >= 70:
        return 58
    return 45


def _regime_bonus(regime: Optional[str]) -> float:
    r = (regime or "").strip().upper()
    if r == "BULL":
        return REGIME_BONUS_BULL
    if r in ("RANGE", "SIDEWAYS", "ZIJWAARTS"):
        return REGIME_BONUS_RANGE
    if r == "BEAR":
        return REGIME_BONUS_BEAR
    return 0.0


def _chance_score(p: PendingPrebuy) -> float:
    """
    Dit is “beste kans van slagen”.
    Higher = better.
    """
    conf = p.confidence if p.confidence is not None else _derive_confidence(p.score)
    base = (WEIGHT_CONFIDENCE * conf) + (WEIGHT_SCORE * float(p.score or 0))
    return base + _regime_bonus(p.regime)


def _fetch_pending() -> List[PendingPrebuy]:
    q = """
    SELECT id, symbol, setup_type, label, score, entry, stop_loss, target, regime,
           expires_at, confidence
    FROM pending_approvals
    WHERE status = 'PENDING'
      AND expires_at > NOW()
    ORDER BY created_at DESC;
    """
    with _db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(q)
            rows = cur.fetchall()

    out: List[PendingPrebuy] = []
    for r in rows:
        out.append(
            PendingPrebuy(
                id=r["id"],
                symbol=r["symbol"],
                setup_type=(r["setup_type"] or "UNKNOWN"),
                label=(r["label"] or ""),
                score=int(r["score"] or 0),
                entry=float(r["entry"] or 0.0),
                stop_loss=float(r["stop_loss"] or 0.0),
                target=float(r["target"] or 0.0),
                regime=r["regime"],
                expires_at=str(r["expires_at"]),
                confidence=(int(r["confidence"]) if r["confidence"] is not None else None),
            )
        )
    return out


def _fetch_top(limit: int) -> List[PendingPrebuy]:
    # We halen pending op en sorteren in Python zodat kansformule flexibel blijft.
    items = _fetch_pending()
    items.sort(key=_chance_score, reverse=True)
    return items[:limit]


def _find_pending_by_id(prebuy_id: str) -> Optional[PendingPrebuy]:
    q = """
    SELECT id, symbol, setup_type, label, score, entry, stop_loss, target, regime,
           expires_at, confidence
    FROM pending_approvals
    WHERE id = %s
      AND status = 'PENDING'
      AND expires_at > NOW()
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
        setup_type=(r["setup_type"] or "UNKNOWN"),
        label=(r["label"] or ""),
        score=int(r["score"] or 0),
        entry=float(r["entry"] or 0.0),
        stop_loss=float(r["stop_loss"] or 0.0),
        target=float(r["target"] or 0.0),
        regime=r["regime"],
        expires_at=str(r["expires_at"]),
        confidence=(int(r["confidence"]) if r["confidence"] is not None else None),
    )


def _pick_oldest_pending() -> Optional[PendingPrebuy]:
    q = """
    SELECT id, symbol, setup_type, label, score, entry, stop_loss, target, regime,
           expires_at, confidence
    FROM pending_approvals
    WHERE status = 'PENDING'
      AND expires_at > NOW()
    ORDER BY created_at ASC
    LIMIT 1;
    """
    with _db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(q)
            r = cur.fetchone()

    if not r:
        return None

    return PendingPrebuy(
        id=r["id"],
        symbol=r["symbol"],
        setup_type=(r["setup_type"] or "UNKNOWN"),
        label=(r["label"] or ""),
        score=int(r["score"] or 0),
        entry=float(r["entry"] or 0.0),
        stop_loss=float(r["stop_loss"] or 0.0),
        target=float(r["target"] or 0.0),
        regime=r["regime"],
        expires_at=str(r["expires_at"]),
        confidence=(int(r["confidence"]) if r["confidence"] is not None else None),
    )


def _mark_status(prebuy_id: str, new_status: str) -> None:
    q = "UPDATE pending_approvals SET status = %s WHERE id = %s;"
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, (new_status, prebuy_id))
        conn.commit()


# ==========================================================
# (OPTIONEEL) EXECUTION HOOK
# ==========================================================
def _execute_buy(prebuy: PendingPrebuy, amount_eur: int) -> Tuple[bool, str]:
    """
    Koppel hier je paper_trader of live execution.
    Voor nu: placeholder zodat flow klopt.
    """
    # TODO: import jouw paper_trader buy functie indien je die hier al gebruikt.
    # from trading.paper_trader import buy_eur
    # ok, msg = buy_eur(symbol=prebuy.symbol, eur_amount=amount_eur, entry=prebuy.entry, stop=prebuy.stop_loss, target=prebuy.target)
    # return ok, msg

    return True, f"BUY gestart (mock). {prebuy.symbol} €{amount_eur}"


# ==========================================================
# COMMAND PARSING
# ==========================================================
YES_RE = re.compile(r"^\s*YES\s+(\d+)(?:\s+([A-Z0-9\-\_]+))?\s*$", re.IGNORECASE)
NO_RE = re.compile(r"^\s*NO(?:\s+([A-Z0-9\-\_]+))?\s*$", re.IGNORECASE)

HELP_TEXT = (
    "📌 Commando’s:\n"
    "• LIST → toon openstaande Pre-BUYs\n"
    "• TOP [n] → beste kans van slagen (default 5). Voorbeeld: TOP 10\n"
    "• YES <bedrag> [ID] → approve + BUY (bedrag: 5/10/15/20/30/100)\n"
    "• NO [ID] → afwijzen\n"
    "• HELP → dit overzicht\n\n"
    "Voorbeeld: YES 20 PB-XRPUSDT-1700000000"
)


# ==========================================================
# ROUTE
# ==========================================================
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    _ensure_tables()

    body = (request.form.get("Body") or "").strip()
    up = body.upper().strip()

    if DEBUG:
        print(f"[IN] {body}")

    # HELP
    if up in {"HELP", "H", "?"}:
        return _twiml(HELP_TEXT)

    # LIST
    if up in {"LIST", "LS", "PENDING"}:
        items = _fetch_pending()
        if not items:
            return _twiml("Geen PENDING Pre-BUYs gevonden (of alles is verlopen).")

        lines = ["📌 PENDING Pre-BUYs:"]
        for i, p in enumerate(items, start=1):
            entry = f"{p.entry:.6g}" if p.entry else "?"
            sl = f"{p.stop_loss:.6g}" if p.stop_loss else "auto"
            tg = f"{p.target:.6g}" if p.target else "auto"
            regime = (p.regime or "UNKNOWN").upper()
            conf = p.confidence if p.confidence is not None else _derive_confidence(p.score)
            chance = _chance_score(p)

            lines.append(
                f"{i}) {p.symbol} | {p.label} | score {p.score} | conf {conf} | chance {chance:.1f}\n"
                f"   {p.setup_type} | entry {entry} | SL {sl} | TG {tg} | regime {regime}\n"
                f"   ID: {p.id}"
            )

        lines.append("")
        lines.append("✅ Goedkeuren: YES 10 [ID]")
        lines.append("❌ Afwijzen: NO [ID]")
        lines.append("🔥 Beste 5: TOP")
        return _twiml("\n".join(lines))

    # TOP (beste kans van slagen)
    if up.startswith("TOP") or up in {"BEST", "TOP5"}:
        limit = _parse_top_limit(body)
        items = _fetch_top(limit=limit)

        if not items:
            return _twiml("Geen TOP trades gevonden (geen PENDING of alles verlopen).")

        lines = [f"🔥 TOP {len(items)} (beste kans van slagen):"]
        for i, p in enumerate(items, start=1):
            entry = f"{p.entry:.6g}" if p.entry else "?"
            sl = f"{p.stop_loss:.6g}" if p.stop_loss else "auto"
            tg = f"{p.target:.6g}" if p.target else "auto"
            regime = (p.regime or "UNKNOWN").upper()
            conf = p.confidence if p.confidence is not None else _derive_confidence(p.score)
            chance = _chance_score(p)

            lines.append(
                f"{i}) {p.symbol} | chance {chance:.1f} | conf {conf} | score {p.score}\n"
                f"   {p.setup_type} | entry {entry} | SL {sl} | TG {tg} | regime {regime}\n"
                f"   ID: {p.id}"
            )

        lines.append("")
        lines.append("Goedkeuren: YES 10 [ID]")
        return _twiml("\n".join(lines))

    # YES <amount> [ID]
    m = YES_RE.match(body)
    if m:
        amount = int(m.group(1))
        if amount not in ALLOWED_AMOUNTS:
            return _twiml(f"❌ Bedrag niet toegestaan. Kies uit: {sorted(ALLOWED_AMOUNTS)}")

        prebuy_id = (m.group(2) or "").strip()
        prebuy = _find_pending_by_id(prebuy_id) if prebuy_id else _pick_oldest_pending()

        if not prebuy:
            return _twiml("Geen geldige PENDING Pre-BUY gevonden (misschien verlopen).")

        # Mark as APPROVED (optional status)
        _mark_status(prebuy.id, "APPROVED")

        ok, msg = _execute_buy(prebuy, amount)
        if ok:
            _mark_status(prebuy.id, "CONSUMED")
            return _twiml(f"✅ Goedgekeurd + BUY gestart\n{msg}\nID: {prebuy.id}")
        else:
            _mark_status(prebuy.id, "PENDING")  # rollback
            return _twiml(f"❌ BUY mislukt: {msg}\nID: {prebuy.id}")

    # NO [ID]
    m = NO_RE.match(body)
    if m:
        prebuy_id = (m.group(1) or "").strip()
        prebuy = _find_pending_by_id(prebuy_id) if prebuy_id else _pick_oldest_pending()

        if not prebuy:
            return _twiml("Geen geldige PENDING Pre-BUY gevonden om af te wijzen.")

        _mark_status(prebuy.id, "REJECTED")
        return _twiml(f"❌ Afgewezen.\nID: {prebuy.id}")

    # Unknown
    return _twiml("Onbekend commando.\n\n" + HELP_TEXT)


# ==========================================================
# MAIN
# ==========================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT") or "10000")
    app.run(host="0.0.0.0", port=port)
