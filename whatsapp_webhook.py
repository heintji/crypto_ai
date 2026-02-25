# whatsapp_webhook.py
from __future__ import annotations

import inspect
import os
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
from flask import Flask, request

try:
    from twilio.twiml.messaging_response import MessagingResponse
except Exception:  # pragma: no cover
    MessagingResponse = None  # type: ignore


# ============================================================
# APP
# ============================================================
app = Flask(__name__)

# ============================================================
# ENV
# ============================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
TRADER_MODE = (os.getenv("TRADER_MODE") or "auto").strip().lower()  # paper|live|auto
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 100}

# DB connect timeout (voorkomt hangen -> 502)
DB_CONNECT_TIMEOUT_SECONDS = int(os.getenv("DB_CONNECT_TIMEOUT_SECONDS") or "5")

# Optioneel: extra debug logging
DEBUG_WEBHOOK = (os.getenv("DEBUG_WEBHOOK") or "0").strip() in {"1", "true", "yes", "on"}


# ============================================================
# HELPERS
# ============================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(msg, flush=True)


def twiml(text: str):
    """
    Altijd iets teruggeven aan Twilio.
    Als Twilio lib ontbreekt -> plain text.
    """
    if MessagingResponse is None:
        return (text, 200, {"Content-Type": "text/plain; charset=utf-8"})
    resp = MessagingResponse()
    resp.message(text)
    return (str(resp), 200, {"Content-Type": "application/xml"})


def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def as_aware_utc(dt: Any) -> Optional[datetime]:
    if not dt or not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def db_ready() -> bool:
    return bool(DATABASE_URL)


def db_connect():
    """
    connect_timeout voorkomt dat je webhook "hangt"
    en daardoor 502's veroorzaakt.
    """
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
        connect_timeout=DB_CONNECT_TIMEOUT_SECONDS,
    )


# ============================================================
# ROUTES (BELANGRIJK VOOR RENDER)
# ============================================================
@app.get("/")
def root():
    # Render/uptime checks komen hier vaak op uit.
    return "OK", 200


@app.get("/healthz")
def healthz():
    return "OK", 200


# ============================================================
# DB QUERIES
# ============================================================
def fetch_pending(conn, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Toon zowel PENDING als APPROVED zodat failed-buys terug te zien zijn.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
              id, symbol, setup_type, timeframe, regime,
              score, chance, confidence,
              entry, stop, target,
              status, created_at, expires_at
            FROM public.pending_approvals
            WHERE COALESCE(status,'PENDING') IN ('PENDING','APPROVED')
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY
              CASE WHEN COALESCE(status,'PENDING')='PENDING' THEN 0 ELSE 1 END,
              COALESCE(chance,0) DESC,
              created_at ASC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_pending_by_id(conn, prebuy_id: str) -> Optional[Dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
              id, symbol, setup_type, timeframe, regime,
              score, chance, confidence,
              entry, stop, target,
              status, created_at, expires_at
            FROM public.pending_approvals
            WHERE id = %s
            """,
            (prebuy_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_top_pending(conn) -> Optional[Dict[str, Any]]:
    rows = fetch_pending(conn, limit=1)
    return rows[0] if rows else None


def mark_rejected(conn, prebuy_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.pending_approvals
            SET status='REJECTED', rejected_at=NOW()
            WHERE id=%s
            """,
            (prebuy_id,),
        )


def mark_approved(conn, prebuy_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.pending_approvals
            SET status='APPROVED', approved_at=NOW()
            WHERE id=%s
            """,
            (prebuy_id,),
        )


def mark_consumed(conn, prebuy_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.pending_approvals
            SET status='CONSUMED', consumed_at=NOW()
            WHERE id=%s
            """,
            (prebuy_id,),
        )


# ============================================================
# TRADER LOADER (compat)
# ============================================================
def _call_buy_compat(buy_fn, symbol: str, amount_eur: float, meta: Dict[str, Any]) -> Any:
    """
    Ondersteunt meerdere buy_eur signatures (meta/prebuy_id/entry/stop/target).
    """
    try:
        sig = inspect.signature(buy_fn)
        params = sig.parameters
    except Exception:
        return buy_fn(symbol, amount_eur)

    args = [symbol, amount_eur]
    kwargs: Dict[str, Any] = {}

    if "meta" in params:
        kwargs["meta"] = meta
    if "prebuy_id" in params:
        kwargs["prebuy_id"] = meta.get("prebuy_id")
    if "entry" in params:
        kwargs["entry"] = meta.get("entry")
    if "stop" in params:
        kwargs["stop"] = meta.get("stop")
    if "stop_loss" in params:
        kwargs["stop_loss"] = meta.get("stop")
    if "target" in params:
        kwargs["target"] = meta.get("target")

    return buy_fn(*args, **kwargs)


def _get_buy_fn(module_path: str):
    try:
        mod = __import__(module_path, fromlist=["buy_eur"])
        fn = getattr(mod, "buy_eur", None)
        if not callable(fn):
            raise AttributeError(f"{module_path}.buy_eur ontbreekt")
        return fn, None
    except Exception as e:
        return None, e


def execute_buy(prebuy: Dict[str, Any], amount_eur: int) -> Tuple[bool, str]:
    """
    TRADER_MODE:
      - paper: altijd paper_trader
      - live : altijd live_trader
      - auto : probeert paper, als dat faalt -> live
    """
    symbol = (prebuy.get("symbol") or "").strip()
    entry = float(prebuy.get("entry") or 0.0)
    stop = float(prebuy.get("stop") or 0.0)
    target = float(prebuy.get("target") or 0.0)
    prebuy_id = str(prebuy.get("id") or "").strip()

    if not symbol:
        return False, "BUY faalde: symbol ontbreekt."

    meta = {"prebuy_id": prebuy_id, "entry": entry, "stop": stop, "target": target}
    attempts: List[Tuple[str, str]] = []

    def try_one(mode: str) -> Tuple[bool, str]:
        module_path = "trading.paper_trader" if mode == "paper" else "trading.live_trader"
        buy_fn, err = _get_buy_fn(module_path)
        if err or buy_fn is None:
            attempts.append((mode, f"{type(err).__name__}: {err}"))
            return False, f"{mode} trader niet beschikbaar: {type(err).__name__}: {err}"

        try:
            res = _call_buy_compat(buy_fn, symbol, float(amount_eur), meta)
            if isinstance(res, dict):
                if res.get("ok") is True:
                    return True, f"BUY uitgevoerd ({mode}) {symbol} €{amount_eur}"
                return False, f"{mode} BUY faalde: {res}"
            # Als trader geen dict returned, nemen we aan: ok
            return True, f"BUY uitgevoerd ({mode}) {symbol} €{amount_eur} (no-return)"
        except Exception as e:
            attempts.append((mode, f"{type(e).__name__}: {e}"))
            return False, f"{mode} buy error: {type(e).__name__}: {e}"

    if TRADER_MODE == "paper":
        return try_one("paper")
    if TRADER_MODE == "live":
        return try_one("live")

    # auto
    ok, msg = try_one("paper")
    if ok:
        return True, msg

    ok2, msg2 = try_one("live")
    if ok2:
        return True, msg2

    detail = "\n".join([f"{m}_err={e}" for (m, e) in attempts]) if attempts else "geen details"
    return False, "BUY NIET uitgevoerd.\n" + detail


# ============================================================
# COMMANDS
# ============================================================
def parse_command(text: str) -> Tuple[str, List[str]]:
    t = (text or "").strip()
    if not t:
        return "HELP", []
    parts = t.split()
    return parts[0].upper(), parts[1:]


def fmt_prebuy_row(p: Dict[str, Any]) -> str:
    st = (p.get("status") or "PENDING").upper()
    return (
        f"{p.get('id')} | {p.get('symbol')} | status={st} | chance={p.get('chance')} "
        f"| score={p.get('score')} | {p.get('setup_type')} | entry={p.get('entry')}"
    )


HELP_TEXT = (
    "Commands:\n"
    "HELP\n"
    "LIST (laat pending/approved zien)\n"
    "TOP (top 5 hoogste chance)\n"
    "YES <bedrag> [ID]  (zonder ID = pakt TOP 1)\n"
    "NO <ID>\n\n"
    "Bedragen: 5/10/15/20/30/100\n"
    "TRADER_MODE=paper|live|auto"
)


# ============================================================
# MAIN WHATSAPP WEBHOOK
# ============================================================
@app.post("/whatsapp")
def whatsapp():
    start = time.time()
    try:
        body = (request.values.get("Body") or "").strip()
        cmd, args = parse_command(body)

        if DEBUG_WEBHOOK:
            log(f"📩 /whatsapp cmd={cmd} args={args} raw='{body}'")

        if not db_ready():
            return twiml("DATABASE_URL ontbreekt in Render Environment.")

        with db_connect() as conn:
            # Zorg dat elke query safe is
            try:
                if cmd in ("HELP", "?"):
                    return twiml(HELP_TEXT)

                if cmd == "LIST":
                    pending = fetch_pending(conn, limit=10)
                    if not pending:
                        return twiml("Geen pending/approved Pre-BUYs.")
                    lines = ["Pre-BUYs (PENDING/APPROVED):"]
                    lines += [fmt_prebuy_row(p) for p in pending]
                    lines.append("\nGebruik: YES <bedrag> [ID]  of  NO <ID>")
                    return twiml("\n".join(lines))

                if cmd == "TOP":
                    pending = fetch_pending(conn, limit=5)
                    if not pending:
                        return twiml("Geen pending/approved Pre-BUYs.")
                    lines = ["TOP 5 (chance):"]
                    lines += [fmt_prebuy_row(p) for p in pending]
                    lines.append("\nGebruik: YES <bedrag> [ID]")
                    return twiml("\n".join(lines))

                if cmd == "NO":
                    if not args:
                        return twiml("Gebruik: NO <ID>")
                    prebuy_id = args[0].strip()
                    p = get_pending_by_id(conn, prebuy_id)
                    if not p:
                        return twiml("ID niet gevonden.")
                    mark_rejected(conn, prebuy_id)
                    conn.commit()
                    return twiml(f"Afgewezen: {prebuy_id}")

                if cmd == "YES":
                    if len(args) < 1:
                        return twiml("Gebruik: YES <bedrag> [ID]")

                    amount = safe_int(args[0], 0)
                    if amount not in ALLOWED_AMOUNTS:
                        return twiml("Bedrag ongeldig. Toegestaan: 5/10/15/20/30/100")

                    # Met ID
                    if len(args) >= 2:
                        prebuy_id = args[1].strip()
                        p = get_pending_by_id(conn, prebuy_id)
                        if not p:
                            return twiml("ID niet gevonden.")
                    else:
                        # Zonder ID -> top 1
                        p = get_top_pending(conn)
                        if not p:
                            return twiml("Geen pending/approved Pre-BUYs om te keuren.")
                        prebuy_id = str(p.get("id"))

                    status = (p.get("status") or "PENDING").upper()
                    if status not in ("PENDING", "APPROVED"):
                        return twiml(f"Kan niet YES doen: status is {status}")

                    exp = as_aware_utc(p.get("expires_at"))
                    if exp and exp <= now_utc():
                        mark_rejected(conn, prebuy_id)
                        conn.commit()
                        return twiml("Deze Pre-BUY is verlopen en is nu afgewezen.")

                    # Mark approved (ook bij retry)
                    mark_approved(conn, prebuy_id)
                    conn.commit()

                    ok, msg = execute_buy(p, amount)

                    if ok:
                        # BUY gelukt -> consumed
                        mark_consumed(conn, prebuy_id)
                        conn.commit()
                        return twiml(f"GOEDGEKEURD ✅\n{msg}\nID: {prebuy_id}")

                    # BUY faalde -> blijft APPROVED zodat je opnieuw YES kunt doen
                    return twiml(
                        f"GOEDGEKEURD ✅\nMaar BUY faalde:\n{msg}\nID: {prebuy_id}\n\n"
                        f"TIP: probeer nogmaals YES <bedrag> {prebuy_id}"
                    )

                return twiml("Onbekend command.\n\n" + HELP_TEXT)

            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise e

    except Exception as e:
        log("❌ ERROR in /whatsapp")
        log(str(e))
        log(traceback.format_exc())
        return twiml("Interne fout. Check Render logs.")

    finally:
        if DEBUG_WEBHOOK:
            ms = int((time.time() - start) * 1000)
            log(f"⏱️ webhook done in {ms}ms")


if __name__ == "__main__":
    # Render geeft PORT mee
    port = int(os.getenv("PORT") or "10000")
    app.run(host="0.0.0.0", port=port)
