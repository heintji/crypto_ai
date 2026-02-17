from __future__ import annotations

import os
import re
import traceback
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

# ==========================================================
# CONFIG / ENV
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or os.getenv("RENDER_DATABASE_URL") or "").strip()
SSL_MODE = (os.getenv("DB_SSLMODE") or "require").strip()

# Twilio / WhatsApp
TWILIO_ACCOUNT_SID = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()

# Jij hebt deze set (SET in je shell)
WHATSAPP_FROM = (os.getenv("WHATSAPP_FROM") or "").strip()
WHATSAPP_TO = (os.getenv("WHATSAPP_TO") or "").strip()

# Optional alternatieve namen (sommige scripts gebruiken dit)
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
TWILIO_WHATSAPP_TO = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()

ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}
DEFAULT_TOP_N = 5

TRADER_MODE = (os.getenv("TRADER_MODE") or "paper").strip().lower()  # paper | live
PORT = int(os.getenv("PORT") or "10000")

# ==========================================================
# APP
# ==========================================================
app = Flask(__name__)


# ==========================================================
# DB HELPERS
# ==========================================================
def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt in env.")
    return psycopg2.connect(DATABASE_URL, sslmode=SSL_MODE)


def _has_column(cur, table: str, col: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table, col),
    )
    return cur.fetchone() is not None


def _safe_cols(cur) -> Dict[str, bool]:
    # pending_approvals kolommen die soms ontbreken tijdens schema-migraties
    cols = ["chance", "confidence", "expires_at", "created_at", "setup_type", "status"]
    return {c: _has_column(cur, "pending_approvals", c) for c in cols}


def fetch_pending(cur, limit: int = 50, order: str = "created_desc") -> List[Dict[str, Any]]:
    cols = _safe_cols(cur)

    # Basisvelden (moeten er zijn)
    fields = ["id", "symbol", "score", "entry", "stop_loss", "target", "regime", "setup_type", "status", "created_at", "expires_at"]
    # Voeg optioneel toe
    if cols.get("confidence"):
        fields.append("confidence")
    if cols.get("chance"):
        fields.append("chance")

    select_sql = "SELECT " + ", ".join(fields) + " FROM pending_approvals"

    # Alleen pending en niet verlopen
    # expires_at is TIMESTAMPTZ; als expires_at NULL is, behandelen we als "niet verlopen"
    where_sql = "WHERE status = 'PENDING' AND (expires_at IS NULL OR expires_at > NOW())"

    if order == "chance_desc" and cols.get("chance"):
        order_sql = "ORDER BY chance DESC NULLS LAST, score DESC NULLS LAST, created_at DESC NULLS LAST"
    elif order == "score_desc":
        order_sql = "ORDER BY score DESC NULLS LAST, created_at DESC NULLS LAST"
    else:
        order_sql = "ORDER BY created_at DESC NULLS LAST"

    cur.execute(f"{select_sql} {where_sql} {order_sql} LIMIT %s", (limit,))
    return list(cur.fetchall())


def get_pending_by_id(cur, prebuy_id: str) -> Optional[Dict[str, Any]]:
    cols = _safe_cols(cur)

    fields = ["id", "symbol", "score", "entry", "stop_loss", "target", "regime", "setup_type", "status", "created_at", "expires_at"]
    if cols.get("confidence"):
        fields.append("confidence")
    if cols.get("chance"):
        fields.append("chance")

    cur.execute(
        f"""
        SELECT {", ".join(fields)}
        FROM pending_approvals
        WHERE id = %s
        """,
        (prebuy_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def mark_rejected(cur, prebuy_id: str):
    # rejected_at bestaat mogelijk, maar we kunnen altijd status zetten
    cur.execute(
        """
        UPDATE pending_approvals
        SET status='REJECTED', rejected_at = COALESCE(rejected_at, NOW())
        WHERE id = %s
        """,
        (prebuy_id,),
    )


def mark_approved(cur, prebuy_id: str, amount: int):
    cur.execute(
        """
        UPDATE pending_approvals
        SET status='APPROVED',
            approved_at = COALESCE(approved_at, NOW()),
            approved_amount = %s
        WHERE id = %s
        """,
        (amount, prebuy_id),
    )


def mark_consumed(cur, prebuy_id: str):
    cur.execute(
        """
        UPDATE pending_approvals
        SET status='CONSUMED', consumed_at = COALESCE(consumed_at, NOW())
        WHERE id = %s
        """,
        (prebuy_id,),
    )


# ==========================================================
# TRADER (BUY execution)
# ==========================================================
def execute_buy(symbol: str, eur_amount: int, entry: float, stop_loss: float, target: float, meta: Dict[str, Any]) -> str:
    """
    Voert de BUY uit via trader-module.
    - paper: trading.paper_trader
    - live : trading.live_trader (Bitvavo implementatie verwacht)
    """
    if TRADER_MODE == "live":
        try:
            from trading.live_trader import buy_eur  # type: ignore
        except Exception:
            raise RuntimeError("TRADER_MODE=live maar trading/live_trader.py (buy_eur) ontbreekt.")
        return buy_eur(symbol=symbol, eur_amount=eur_amount, entry=entry, stop_loss=stop_loss, target=target, meta=meta)

    # default paper
    try:
        from trading.paper_trader import buy_eur  # type: ignore
    except Exception:
        # fallback naam (sommige versies noemen het buy())
        try:
            from trading.paper_trader import buy as buy_eur  # type: ignore
        except Exception:
            raise RuntimeError("paper_trader mist buy_eur() of buy().")
    return buy_eur(symbol=symbol, eur_amount=eur_amount, entry=entry, stop_loss=stop_loss, target=target, meta=meta)


# ==========================================================
# MESSAGE FORMATTING
# ==========================================================
def fmt_item(i: int, row: Dict[str, Any]) -> str:
    parts = []
    parts.append(f"{i}) {row.get('symbol')} | {row.get('setup_type')} | score {row.get('score')}")
    if "confidence" in row and row.get("confidence") is not None:
        parts.append(f"conf {row.get('confidence')}")
    if "chance" in row and row.get("chance") is not None:
        parts.append(f"chance {row.get('chance')}")
    parts.append(f"entry {row.get('entry')}")
    parts.append(f"SL {row.get('stop_loss')}")
    parts.append(f"TG {row.get('target')}")
    if row.get("regime"):
        parts.append(f"regime {row.get('regime')}")
    parts.append(f"ID: {row.get('id')}")
    return " | ".join(parts)


def help_text() -> str:
    return (
        "📣 Commando’s:\n"
        "• LIST  → toon openstaande Pre-BUY’s\n"
        "• TOP   → toon TOP 5 beste kans van slagen (chance desc)\n"
        "• YES <bedrag> [ID] → approve + BUY (bedrag: 5/10/15/20/30/100)\n"
        "• NO [ID] → afwijzen\n"
        "• HELP → dit overzicht\n\n"
        "Voorbeeld: YES 20 PB-XRPUSDT-1700000000"
    )


# ==========================================================
# COMMAND PARSING
# ==========================================================
YES_RE = re.compile(r"^YES\s+(\d+)(?:\s+([A-Z0-9\-_]+))?$", re.IGNORECASE)
NO_RE = re.compile(r"^NO(?:\s+([A-Z0-9\-_]+))?$", re.IGNORECASE)


@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    msg = (request.form.get("Body") or "").strip()
    cmd = msg.strip().upper()

    resp = MessagingResponse()

    try:
        # DB connect per request
        with db_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:

                if cmd in ("HELP", "?"):
                    resp.message(help_text())
                    return str(resp)

                if cmd == "LIST":
                    items = fetch_pending(cur, limit=20, order="created_desc")
                    if not items:
                        resp.message("Geen PENDING Pre-BUY’s gevonden (of alles is verlopen).")
                        return str(resp)

                    lines = ["📌 PENDING Pre-BUY’s:"]
                    for idx, row in enumerate(items, start=1):
                        lines.append(fmt_item(idx, dict(row)))

                    lines.append("")
                    lines.append("✅ Goedkeuren: YES 10 [ID]")
                    lines.append("❌ Afwijzen: NO [ID]")
                    lines.append("🔥 Beste 5: TOP")
                    lines.append("ℹ️ Help: HELP")
                    resp.message("\n".join(lines))
                    return str(resp)

                if cmd == "TOP":
                    items = fetch_pending(cur, limit=DEFAULT_TOP_N, order="chance_desc")
                    if not items:
                        resp.message("Geen PENDING Pre-BUY’s gevonden (of alles is verlopen).")
                        return str(resp)

                    lines = ["🔥 TOP 5 (beste kans van slagen):"]
                    for idx, row in enumerate(items, start=1):
                        lines.append(fmt_item(idx, dict(row)))

                    lines.append("")
                    lines.append("✅ Goedkeuren: YES 10 [ID]")
                    lines.append("❌ Afwijzen: NO [ID]")
                    resp.message("\n".join(lines))
                    return str(resp)

                m_yes = YES_RE.match(msg)
                if m_yes:
                    amount = int(m_yes.group(1))
                    prebuy_id = (m_yes.group(2) or "").strip() or None

                    if amount not in ALLOWED_AMOUNTS:
                        resp.message(f"❌ Ongeldig bedrag. Toegestaan: {sorted(ALLOWED_AMOUNTS)}")
                        return str(resp)

                    # Als geen ID: pak de meest recente pending
                    if not prebuy_id:
                        items = fetch_pending(cur, limit=1, order="created_desc")
                        if not items:
                            resp.message("Geen PENDING Pre-BUY’s om goed te keuren.")
                            return str(resp)
                        prebuy_id = items[0]["id"]

                    row = get_pending_by_id(cur, prebuy_id)
                    if not row:
                        resp.message(f"❌ ID niet gevonden: {prebuy_id}")
                        return str(resp)

                    # Verlopen check (extra zekerheid)
                    # (als expires_at NULL is: ok)
                    # row['expires_at'] is datetime from DB
                    if row.get("status") != "PENDING":
                        resp.message(f"⚠️ Deze Pre-BUY is niet meer PENDING (status={row.get('status')}).")
                        return str(resp)

                    # Mark approved, execute buy, then consumed
                    mark_approved(cur, prebuy_id, amount)
                    conn.commit()

                    # Execute buy via trader
                    symbol = row["symbol"]
                    entry = float(row["entry"])
                    stop_loss = float(row["stop_loss"])
                    target = float(row["target"])
                    meta = {
                        "prebuy_id": prebuy_id,
                        "setup_type": row.get("setup_type"),
                        "regime": row.get("regime"),
                        "score": row.get("score"),
                        "confidence": row.get("confidence") if "confidence" in row else None,
                        "chance": row.get("chance") if "chance" in row else None,
                    }

                    result = execute_buy(
                        symbol=symbol,
                        eur_amount=amount,
                        entry=entry,
                        stop_loss=stop_loss,
                        target=target,
                        meta=meta,
                    )

                    # Consume after successful buy
                    mark_consumed(cur, prebuy_id)
                    conn.commit()

                    resp.message(
                        "✅ GOEDGEKEURD + BUY uitgevoerd\n"
                        f"• ID: {prebuy_id}\n"
                        f"• {symbol}\n"
                        f"• Bedrag: €{amount}\n"
                        f"• Entry: {entry}\n"
                        f"• SL: {stop_loss}\n"
                        f"• TG: {target}\n"
                        f"• Trader: {TRADER_MODE}\n"
                        f"• Result: {result}"
                    )
                    return str(resp)

                m_no = NO_RE.match(msg)
                if m_no:
                    prebuy_id = (m_no.group(1) or "").strip() or None

                    if not prebuy_id:
                        # Als geen ID: wijs meest recente pending af
                        items = fetch_pending(cur, limit=1, order="created_desc")
                        if not items:
                            resp.message("Geen PENDING Pre-BUY’s om af te wijzen.")
                            return str(resp)
                        prebuy_id = items[0]["id"]

                    row = get_pending_by_id(cur, prebuy_id)
                    if not row:
                        resp.message(f"❌ ID niet gevonden: {prebuy_id}")
                        return str(resp)

                    mark_rejected(cur, prebuy_id)
                    conn.commit()

                    resp.message(f"❌ Afgewezen: {prebuy_id}")
                    return str(resp)

                # Onbekend
                resp.message("Onbekend commando.\n\n" + help_text())
                return str(resp)

    except Exception as e:
        # Log in Render logs + terug naar WhatsApp
        traceback.print_exc()
        resp.message(f"❌ Interne fout in webhook: {e}\nCheck Render logs (Traceback).")
        return str(resp)


@app.route("/", methods=["GET"])
def root():
    return "OK", 200


if __name__ == "__main__":
    # Render Web Service: altijd binden op PORT
    app.run(host="0.0.0.0", port=PORT)
