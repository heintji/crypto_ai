# whatsapp_webhook.py
from __future__ import annotations

import os
import re
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

# ==========================================================
# CONFIG
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

# Jij gebruikt deze (SET) ✅
WHATSAPP_FROM = (os.getenv("WHATSAPP_FROM") or "").strip()
WHATSAPP_TO = (os.getenv("WHATSAPP_TO") or "").strip()

# Soms staan ze zo in andere modules — supporten we ook
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
TWILIO_WHATSAPP_TO = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()

ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

DEFAULT_TOP_N = 5
MAX_LIST_N = 12  # zodat je whatsapp niet vol spam loopt

app = Flask(__name__)

# ==========================================================
# DB HELPERS
# ==========================================================
def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL ontbreekt (Render Environment).")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def ensure_tables():
    """
    Zorgt dat tabellen + kolommen bestaan.
    Dit voorkomt precies de errors die jij nu ziet (missing columns).
    """
    conn = db_conn()
    cur = conn.cursor()

    # pending_approvals: PENDING -> APPROVED/REJECTED/EXPIRED/CONSUMED
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_approvals (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            setup_type TEXT,
            label TEXT,
            score INTEGER,
            grade TEXT,
            entry DOUBLE PRECISION,
            stop_loss DOUBLE PRECISION,
            target DOUBLE PRECISION,
            regime TEXT,
            confidence INTEGER,
            expires_at TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'PENDING',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            approved_at TIMESTAMPTZ,
            rejected_at TIMESTAMPTZ,
            consumed_at TIMESTAMPTZ,
            stake_eur INTEGER
        );
        """
    )

    # Add columns if someone created an older schema
    cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS confidence INTEGER;")
    cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();")
    cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;")
    cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'PENDING';")
    cur.execute("ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS stake_eur INTEGER;")

    conn.commit()
    cur.close()
    conn.close()


@dataclass
class PendingItem:
    id: str
    symbol: str
    label: Optional[str]
    score: Optional[int]
    setup_type: Optional[str]
    entry: Optional[float]
    stop_loss: Optional[float]
    target: Optional[float]
    regime: Optional[str]
    confidence: Optional[int]
    expires_at: Optional[str]


def fetch_pending(limit: int, order: str = "best") -> List[PendingItem]:
    """
    order='best' -> confidence desc, score desc, created_at desc
    order='new'  -> created_at desc
    """
    conn = db_conn()
    cur = conn.cursor()

    # Expiry check: timestamptz vergelijken met NOW() ✅
    base_where = "WHERE status='PENDING' AND (expires_at IS NULL OR expires_at > NOW())"

    if order == "new":
        order_by = "ORDER BY created_at DESC"
    else:
        order_by = """
        ORDER BY
          confidence DESC NULLS LAST,
          score DESC NULLS LAST,
          created_at DESC
        """

    q = f"""
    SELECT
      id, symbol, label, score, setup_type,
      entry, stop_loss, target, regime,
      confidence,
      COALESCE(expires_at::text,'')
    FROM pending_approvals
    {base_where}
    {order_by}
    LIMIT %s;
    """

    cur.execute(q, (limit,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    items: List[PendingItem] = []
    for r in rows:
        items.append(
            PendingItem(
                id=r[0],
                symbol=r[1],
                label=r[2],
                score=r[3],
                setup_type=r[4],
                entry=r[5],
                stop_loss=r[6],
                target=r[7],
                regime=r[8],
                confidence=r[9],
                expires_at=r[10] or None,
            )
        )
    return items


def mark_status(item_id: str, status: str, stake_eur: Optional[int] = None):
    conn = db_conn()
    cur = conn.cursor()

    if status == "APPROVED":
        cur.execute(
            """
            UPDATE pending_approvals
            SET status='APPROVED',
                approved_at=NOW(),
                stake_eur=%s
            WHERE id=%s;
            """,
            (stake_eur, item_id),
        )
    elif status == "REJECTED":
        cur.execute(
            """
            UPDATE pending_approvals
            SET status='REJECTED',
                rejected_at=NOW()
            WHERE id=%s;
            """,
            (item_id,),
        )
    elif status == "CONSUMED":
        cur.execute(
            """
            UPDATE pending_approvals
            SET status='CONSUMED',
                consumed_at=NOW()
            WHERE id=%s;
            """,
            (item_id,),
        )
    else:
        cur.execute("UPDATE pending_approvals SET status=%s WHERE id=%s;", (status, item_id))

    conn.commit()
    cur.close()
    conn.close()


# ==========================================================
# FORMATTERS
# ==========================================================
def fmt_float(x: Optional[float]) -> str:
    if x is None:
        return "-"
    try:
        return f"{x:.5f}".rstrip("0").rstrip(".")
    except Exception:
        return str(x)


def format_list(items: List[PendingItem], title: str) -> str:
    if not items:
        return "Geen PENDING Pre-BUYs gevonden (of alles is verlopen)."

    lines = [f"📌 {title}:"]
    for i, it in enumerate(items, 1):
        conf = f"{it.confidence}" if it.confidence is not None else "-"
        lines.append(
            f"{i}) {it.symbol} | {it.label or '-'} | score {it.score if it.score is not None else '-'} | "
            f"{it.setup_type or '-'} | entry {fmt_float(it.entry)} | SL {fmt_float(it.stop_loss)} | "
            f"TG {fmt_float(it.target)} | regime {it.regime or '-'} | conf {conf}\n"
            f"   ID: {it.id}"
        )

    lines.append("")
    lines.append("✅ Goedkeuren: YES 10 [ID]")
    lines.append("❌ Afwijzen: NO [ID]")
    lines.append("🏆 Top 5: TOP")
    lines.append("ℹ️ Help: HELP")
    return "\n".join(lines)


def help_text() -> str:
    return (
        "📣 Commando’s:\n"
        "• LIST → toon openstaande Pre-BUYs\n"
        "• TOP → top 5 beste kans van slagen (op confidence/score)\n"
        "• YES <bedrag> [ID] → approve + BUY (bedrag: 5/10/15/20/30/100)\n"
        "• NO [ID] → afwijzen\n"
        "• HELP → dit overzicht\n\n"
        "Voorbeeld: YES 20 PB-XRPUSDT-1700000000"
    )


# ==========================================================
# BUY EXECUTION (paper_trader)
# ==========================================================
def execute_buy(symbol: str, stake_eur: int, entry: Optional[float], stop_loss: Optional[float], target: Optional[float]) -> str:
    """
    Past bij jouw architectuur: webhook = human gate → start BUY.
    We proberen buy_eur() te gebruiken, anders buy().
    """
    try:
        from trading.paper_trader import buy_eur  # type: ignore

        buy_eur(symbol=symbol, eur_amount=stake_eur, entry_price=entry, stop_loss=stop_loss, target=target)
        return "OK"
    except Exception:
        try:
            from trading.paper_trader import buy  # type: ignore

            buy(symbol=symbol, eur_amount=stake_eur, entry_price=entry, stop_loss=stop_loss, target=target)
            return "OK"
        except Exception as e:
            return f"ERROR: BUY failed: {e}"


# ==========================================================
# COMMAND PARSER
# ==========================================================
YES_RE = re.compile(r"^YES\s+(\d+)(?:\s+(.+))?$", re.IGNORECASE)
NO_RE = re.compile(r"^NO(?:\s+(.+))?$", re.IGNORECASE)

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    resp = MessagingResponse()

    try:
        ensure_tables()

        body = (request.form.get("Body") or "").strip()
        msg = body.upper().strip()

        if not msg:
            resp.message(help_text())
            return str(resp)

        # HELP
        if msg in {"HELP", "H"}:
            resp.message(help_text())
            return str(resp)

        # LIST
        if msg == "LIST":
            items = fetch_pending(limit=MAX_LIST_N, order="new")
            resp.message(format_list(items, "PENDING Pre-BUYs"))
            return str(resp)

        # TOP (top 5 best chance)
        if msg.startswith("TOP"):
            items = fetch_pending(limit=DEFAULT_TOP_N, order="best")
            resp.message(format_list(items, f"TOP {DEFAULT_TOP_N} (beste kans van slagen)"))
            return str(resp)

        # YES amount [id]
        m = YES_RE.match(body.strip())
        if m:
            amount = int(m.group(1))
            if amount not in ALLOWED_AMOUNTS:
                resp.message(f"❌ Bedrag niet toegestaan. Kies uit: {sorted(ALLOWED_AMOUNTS)}")
                return str(resp)

            raw_id = (m.group(2) or "").strip()
            if raw_id:
                pick_id = raw_id
                items = fetch_pending(limit=MAX_LIST_N, order="best")
                chosen = next((x for x in items if x.id == pick_id), None)
                if not chosen:
                    resp.message("❌ ID niet gevonden of niet meer PENDING.\n\n" + help_text())
                    return str(resp)
            else:
                # geen ID: pak de beste
                items = fetch_pending(limit=1, order="best")
                if not items:
                    resp.message("Geen PENDING Pre-BUYs om goed te keuren.")
                    return str(resp)
                chosen = items[0]

            # Mark approved + execute buy
            mark_status(chosen.id, "APPROVED", stake_eur=amount)
            buy_result = execute_buy(chosen.symbol, amount, chosen.entry, chosen.stop_loss, chosen.target)

            if buy_result == "OK":
                resp.message(
                    f"✅ GOEDGEKEURD + BUY gestart\n"
                    f"{chosen.symbol} | {chosen.setup_type or '-'} | {chosen.label or '-'}\n"
                    f"€{amount} | entry {fmt_float(chosen.entry)} | SL {fmt_float(chosen.stop_loss)} | TG {fmt_float(chosen.target)}\n"
                    f"ID: {chosen.id}"
                )
            else:
                resp.message(
                    f"⚠️ GOEDGEKEURD, maar BUY faalde.\n"
                    f"{buy_result}\n"
                    f"ID: {chosen.id}"
                )
            return str(resp)

        # NO [id]
        m = NO_RE.match(body.strip())
        if m:
            raw_id = (m.group(1) or "").strip()
            if not raw_id:
                resp.message("❌ Geef een ID mee.\nVoorbeeld: NO PB-XRPUSDT-1700000000")
                return str(resp)

            mark_status(raw_id, "REJECTED")
            resp.message(f"❌ Afgewezen: {raw_id}")
            return str(resp)

        # Unknown
        resp.message("Onbekend commando.\n\n" + help_text())
        return str(resp)

    except Exception as e:
        # Twilio moet altijd antwoord krijgen (anders retrypogingen)
        resp.message("❌ Interne fout in webhook. Check Render logs (Traceback).")
        print("ERROR:", e)
        traceback.print_exc()
        return str(resp)
