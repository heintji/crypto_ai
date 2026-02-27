# whatsapp_webhook.py
from __future__ import annotations

import inspect
import json
import os
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

# ==========================================================
# APP
# ==========================================================
app = Flask(__name__)

# ==========================================================
# ENV / CONFIG
# ==========================================================
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

# ✅ nieuwe bedragen
ALLOWED_AMOUNTS = {5, 10, 15, 20, 30, 40, 50}

# paper | live | auto
TRADER_MODE = (os.getenv("TRADER_MODE") or "auto").strip().lower()

# Max trades tegelijk (open posities)
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES") or "10")

# Waar state staat (paper_state.json gebruikt als open_trades teller)
# (ook bij live gebruiken we dit als “limiet” zodat je bot niet doorschiet)
DATA_DIR = (os.getenv("DATA_DIR") or "/data").strip()
PAPER_STATE_PATH = (os.getenv("PAPER_STATE_PATH") or os.path.join(DATA_DIR, "paper_state.json")).strip()

# WhatsApp output limieten
LIST_LIMIT = int(os.getenv("LIST_LIMIT") or "10")     # LIST
TOP_LIMIT = int(os.getenv("TOP_LIMIT") or "10")       # TOP = top 10

# ==========================================================
# BASIC HELPERS
# ==========================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(msg, flush=True)


def twiml(text: str):
    """
    Twilio TwiML response, of plain text als Twilio lib ontbreekt.
    """
    if MessagingResponse is None:
        return (text, 200, {"Content-Type": "text/plain; charset=utf-8"})
    resp = MessagingResponse()
    resp.message(text)
    return (str(resp), 200, {"Content-Type": "application/xml"})


def db_connect():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def as_aware_utc(dt: Any) -> Optional[datetime]:
    if not dt or not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_prebuy_id(text: str) -> bool:
    t = (text or "").strip().upper()
    return t.startswith("PB-")


# ==========================================================
# OPEN TRADES LIMIT (anti-ontploffing)
# ==========================================================
def _load_state_for_open_trades() -> Dict[str, Any]:
    """
    Leest PAPER_STATE_PATH en pakt open_trades.
    Als file niet bestaat: 0 open.
    """
    try:
        if not PAPER_STATE_PATH or not os.path.isfile(PAPER_STATE_PATH):
            return {"open_trades": []}
        with open(PAPER_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if not isinstance(data, dict):
            return {"open_trades": []}
        data.setdefault("open_trades", [])
        return data
    except Exception:
        return {"open_trades": []}


def count_open_trades() -> int:
    st = _load_state_for_open_trades()
    ot = st.get("open_trades") or []
    return len(ot) if isinstance(ot, list) else 0


def can_open_new_trade() -> Tuple[bool, str]:
    n = count_open_trades()
    if n >= MAX_OPEN_TRADES:
        return False, f"MAX OPEN TRADES bereikt ({n}/{MAX_OPEN_TRADES}). Eerst trades sluiten."
    return True, f"Open trades: {n}/{MAX_OPEN_TRADES}"


# ==========================================================
# KANS TEKST (zoals jij wilt)
# ==========================================================
def chance_label(chance: int) -> str:
    c = int(chance or 0)
    if c >= 90:
        return "kans heel groot"
    if c >= 80:
        return "kans groot"
    if c >= 70:
        return "kans goed"
    return "kans laag"


# ==========================================================
# DB QUERIES
# ==========================================================
def fetch_prebuys(
    conn,
    *,
    limit: int = 10,
    include_expired: bool = False,
    only_active: bool = True,
) -> List[Dict[str, Any]]:
    """
    - only_active=True  -> alleen PENDING/APPROVED
    - include_expired=False -> alleen expires_at>NOW()
    """
    where_status = "COALESCE(status,'PENDING') IN ('PENDING','APPROVED')" if only_active else "TRUE"
    expired_clause = "" if include_expired else "AND (expires_at IS NULL OR expires_at > NOW())"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
              id, symbol, setup_type, timeframe, regime,
              score, raw_score, chance, confidence,
              entry, stop, target,
              status, created_at, expires_at,
              approved_at, rejected_at, consumed_at
            FROM public.pending_approvals
            WHERE {where_status}
              {expired_clause}
            ORDER BY
              COALESCE(chance,0) DESC,
              created_at ASC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

    return [dict(r) for r in rows]


def stats(conn) -> Dict[str, int]:
    """
    Helpt diagnose:
    - pending_or_approved: hoeveel records staan op PENDING/APPROVED
    - active_now: hoeveel daarvan zijn NIET expired
    - expired_now: hoeveel daarvan zijn expired
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE COALESCE(status,'PENDING') IN ('PENDING','APPROVED'))::int AS pending_or_approved,
              COUNT(*) FILTER (
                WHERE COALESCE(status,'PENDING') IN ('PENDING','APPROVED')
                  AND (expires_at IS NULL OR expires_at > NOW())
              )::int AS active_now,
              COUNT(*) FILTER (
                WHERE COALESCE(status,'PENDING') IN ('PENDING','APPROVED')
                  AND expires_at IS NOT NULL
                  AND expires_at <= NOW()
              )::int AS expired_now
            FROM public.pending_approvals
            """
        )
        row = cur.fetchone() or (0, 0, 0)
    return {"pending_or_approved": row[0], "active_now": row[1], "expired_now": row[2]}


def get_by_id(conn, prebuy_id: str) -> Optional[Dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
              id, symbol, setup_type, timeframe, regime,
              score, raw_score, chance, confidence,
              entry, stop, target,
              status, created_at, expires_at,
              approved_at, rejected_at, consumed_at
            FROM public.pending_approvals
            WHERE id=%s
            """,
            (prebuy_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_top_one(conn) -> Optional[Dict[str, Any]]:
    rows = fetch_prebuys(conn, limit=1, include_expired=False, only_active=True)
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
    conn.commit()


def mark_approved(conn, prebuy_id: str) -> None:
    """
    Belangrijk: APPROVED blijft staan als BUY faalt.
    Zo kun jij retry-en met YES opnieuw.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.pending_approvals
            SET status='APPROVED', approved_at=NOW()
            WHERE id=%s
            """,
            (prebuy_id,),
        )
    conn.commit()


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
    conn.commit()


# ==========================================================
# TRADER LOADER (paper/live/auto + compat meta)
# ==========================================================
def _call_buy_compat(buy_fn, symbol: str, amount_eur: float, meta: Dict[str, Any]) -> Any:
    """
    Compat call: sommige buy_eur() hebben extra params, andere niet.
    """
    try:
        sig = inspect.signature(buy_fn)
        params = sig.parameters
    except Exception:
        return buy_fn(symbol, amount_eur)

    args = [symbol, amount_eur]
    kwargs: Dict[str, Any] = {}

    # meest veilig: meta dict
    if "meta" in params:
        kwargs["meta"] = meta

    # sommige varianten gebruiken losse args
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
    mod = __import__(module_path, fromlist=["buy_eur"])
    fn = getattr(mod, "buy_eur", None)
    if not callable(fn):
        raise AttributeError(f"{module_path}.buy_eur ontbreekt")
    return fn


def execute_buy(prebuy: Dict[str, Any], amount_eur: int) -> Tuple[bool, str]:
    """
    Execute BUY:
    - TRADER_MODE=paper -> alleen paper
    - TRADER_MODE=live  -> alleen live
    - TRADER_MODE=auto  -> eerst paper proberen, daarna live (jouw originele gedrag)
    """
    symbol = (prebuy.get("symbol") or "").strip()
    prebuy_id = str(prebuy.get("id") or "").strip()

    entry = safe_float(prebuy.get("entry"), 0.0)
    stop = safe_float(prebuy.get("stop"), 0.0)
    target = safe_float(prebuy.get("target"), 0.0)

    if not symbol:
        return False, "BUY faalde: symbol ontbreekt."

    # ✅ max open trades check
    ok_open, open_msg = can_open_new_trade()
    if not ok_open:
        return False, open_msg

    meta = {"prebuy_id": prebuy_id, "entry": entry, "stop": stop, "target": target}

    attempts: List[str] = []

    def try_mode(mode: str) -> Tuple[bool, str]:
        module_path = "trading.paper_trader" if mode == "paper" else "trading.live_trader"
        try:
            buy_fn = _get_buy_fn(module_path)
        except Exception as e:
            return False, f"{mode} trader niet beschikbaar: {type(e).__name__}: {e}"

        try:
            res = _call_buy_compat(buy_fn, symbol, float(amount_eur), meta)
            if isinstance(res, dict):
                if res.get("ok") is True:
                    return True, f"BUY uitgevoerd ({mode}) {symbol} €{amount_eur}"
                # laat hele res zien (super belangrijk bij live debug)
                return False, f"{mode} BUY faalde: {res}"
            return True, f"BUY uitgevoerd ({mode}) {symbol} €{amount_eur} (no-dict return)"
        except Exception as e:
            return False, f"{mode} buy error: {type(e).__name__}: {e}"

    if TRADER_MODE == "paper":
        return try_mode("paper")
    if TRADER_MODE == "live":
        return try_mode("live")

    # auto: paper -> live
    ok1, msg1 = try_mode("paper")
    if ok1:
        return True, msg1
    attempts.append(msg1)

    ok2, msg2 = try_mode("live")
    if ok2:
        return True, msg2
    attempts.append(msg2)

    return False, "BUY NIET uitgevoerd.\n" + "\n".join(attempts)


# ==========================================================
# FORMATTING (LIST/TOP/DETAIL)
# ==========================================================
def fmt_row_short(p: Dict[str, Any]) -> str:
    """
    Korte regel voor LIST/TOP.
    Duidelijk: coin, kans-tekst, chance%, score, status, ID.
    """
    symbol = p.get("symbol")
    st = (p.get("status") or "PENDING").upper()
    chance = safe_int(p.get("chance"), 0)
    score = safe_int(p.get("score"), 0)
    label = chance_label(chance)
    return f"{symbol} | {label} ({chance}%) | score={score} | {st} | ID: {p.get('id')}"


def fmt_detail(p: Dict[str, Any]) -> str:
    """
    Volledige trade detail (voor PB-ID of SHOW <ID>)
    """
    symbol = p.get("symbol")
    st = (p.get("status") or "PENDING").upper()

    timeframe = p.get("timeframe") or "-"
    regime = p.get("regime") or "-"
    setup = p.get("setup_type") or "-"

    score = safe_int(p.get("score"), 0)
    raw_score = p.get("raw_score")
    chance = safe_int(p.get("chance"), 0)
    confidence = p.get("confidence")

    entry = p.get("entry")
    stop = p.get("stop")
    target = p.get("target")

    created_at = p.get("created_at")
    expires_at = p.get("expires_at")
    approved_at = p.get("approved_at")
    consumed_at = p.get("consumed_at")

    created_s = created_at.isoformat() if isinstance(created_at, datetime) else str(created_at) if created_at else "-"
    expires_s = expires_at.isoformat() if isinstance(expires_at, datetime) else str(expires_at) if expires_at else "-"
    approved_s = approved_at.isoformat() if isinstance(approved_at, datetime) else str(approved_at) if approved_at else "-"
    consumed_s = consumed_at.isoformat() if isinstance(consumed_at, datetime) else str(consumed_at) if consumed_at else "-"

    raw_s = str(raw_score) if raw_score is not None else "-"

    label = chance_label(chance)

    return (
        f"📌 TRADE DETAIL\n"
        f"COIN: {symbol}\n"
        f"ID: {p.get('id')}\n"
        f"STATUS: {st}\n\n"
        f"{label}\n"
        f"chance: {chance}% | confidence: {confidence} | score: {score} | raw_score: {raw_s}\n\n"
        f"timeframe: {timeframe}\n"
        f"setup: {setup}\n"
        f"regime: {regime}\n\n"
        f"ENTRY:  {entry}\n"
        f"STOP:   {stop}\n"
        f"TARGET: {target}\n\n"
        f"created_at: {created_s}\n"
        f"expires_at: {expires_s}\n"
        f"approved_at: {approved_s}\n"
        f"consumed_at: {consumed_s}\n\n"
        f"👉 Kopen: YES 10 {p.get('id')}\n"
        f"👉 Bedragen: 5/10/15/20/30/40/50\n"
    )


# ==========================================================
# COMMANDS
# ==========================================================
HELP_TEXT = (
    "Commands:\n"
    "HELP\n"
    "LIST (active PENDING/APPROVED)\n"
    "TOP (top 10 - active)\n"
    "SHOW <ID>  (volledige details)\n"
    "STATS (active vs expired)\n"
    "LISTALL (ook expired)\n"
    "OPEN (open trades teller)\n"
    "YES <bedrag> [ID]  (zonder ID = pakt TOP 1)\n"
    "NO <ID>\n\n"
    "Bedragen: 5/10/15/20/30/40/50\n"
    f"MAX_OPEN_TRADES={MAX_OPEN_TRADES}\n"
    "TRADER_MODE=paper|live|auto"
)


def parse_command(text: str) -> Tuple[str, List[str]]:
    t = (text or "").strip()
    if not t:
        return "HELP", []
    parts = t.split()
    return parts[0].upper(), parts[1:]


@app.get("/")
def root():
    return "OK", 200


@app.get("/healthz")
def healthz():
    return "OK", 200


@app.post("/whatsapp")
def whatsapp():
    try:
        body = (request.values.get("Body") or "").strip()
        cmd, args = parse_command(body)

        if not DATABASE_URL:
            return twiml("DATABASE_URL ontbreekt in Render Environment.")

        with db_connect() as conn:
            # 0) Direct PB-ID sturen => detail
            if is_prebuy_id(body):
                p = get_by_id(conn, body.strip())
                if not p:
                    return twiml("ID niet gevonden.")
                return twiml(fmt_detail(p))

            # 1) HELP
            if cmd in ("HELP", "?"):
                return twiml(HELP_TEXT)

            # 2) OPEN (hoeveel open trades)
            if cmd == "OPEN":
                n = count_open_trades()
                return twiml(f"OPEN TRADES: {n}/{MAX_OPEN_TRADES}\nState: {PAPER_STATE_PATH}")

            # 3) STATS
            if cmd == "STATS":
                s = stats(conn)
                return twiml(
                    "STATS:\n"
                    f"pending_or_approved = {s['pending_or_approved']}\n"
                    f"active_now          = {s['active_now']}\n"
                    f"expired_now         = {s['expired_now']}\n\n"
                    "Als active_now=0 en expired_now>0:\n"
                    "- expires_at wordt te vroeg gezet (timing/UTC)\n"
                    "- of multi_coin_score draait niet / maakt niets aan\n"
                    "Tip: stuur LISTALL om te zien wat er in de tabel staat."
                )

            # 4) LIST
            if cmd == "LIST":
                rows = fetch_prebuys(conn, limit=LIST_LIMIT, include_expired=False, only_active=True)
                if not rows:
                    s = stats(conn)
                    return twiml(
                        "Geen ACTIVE pending/approved Pre-BUYs.\n"
                        f"(expired_now={s['expired_now']})\n\n"
                        "Tip: STATS of LISTALL."
                    )

                lines = [f"ACTIVE TRADES (max {LIST_LIMIT}):"]
                lines += [fmt_row_short(p) for p in rows]
                lines.append("\nDetail: stuur PB-ID of gebruik SHOW <ID>")
                lines.append("Kopen: YES 10 <ID>  | Afwijzen: NO <ID>")
                return twiml("\n".join(lines))

            # 5) LISTALL (incl expired)
            if cmd == "LISTALL":
                rows = fetch_prebuys(conn, limit=LIST_LIMIT, include_expired=True, only_active=True)
                if not rows:
                    return twiml("Geen pending/approved records gevonden.")
                lines = [f"PENDING/APPROVED incl EXPIRED (max {LIST_LIMIT}):"]
                lines += [fmt_row_short(p) for p in rows]
                lines.append("\nTip: PB-ID sturen voor full details.")
                return twiml("\n".join(lines))

            # 6) TOP (top 10)
            if cmd == "TOP":
                rows = fetch_prebuys(conn, limit=TOP_LIMIT, include_expired=False, only_active=True)
                if not rows:
                    s = stats(conn)
                    return twiml(
                        "Geen ACTIVE pending/approved Pre-BUYs.\n"
                        f"(expired_now={s['expired_now']})\n\n"
                        "Tip: STATS"
                    )
                lines = [f"TOP {TOP_LIMIT} (chance) - ACTIVE:"]
                lines += [fmt_row_short(p) for p in rows]
                lines.append("\nKopen: YES 10 <ID>")
                return twiml("\n".join(lines))

            # 7) SHOW <ID> (detail op ID)
            if cmd == "SHOW":
                if not args:
                    return twiml("Gebruik: SHOW <ID>")
                prebuy_id = args[0].strip()
                p = get_by_id(conn, prebuy_id)
                if not p:
                    return twiml("ID niet gevonden.")
                return twiml(fmt_detail(p))

            # 8) NO <ID>
            if cmd == "NO":
                if not args:
                    return twiml("Gebruik: NO <ID>")
                prebuy_id = args[0].strip()
                p = get_by_id(conn, prebuy_id)
                if not p:
                    return twiml("ID niet gevonden.")
                mark_rejected(conn, prebuy_id)
                return twiml(f"Afgewezen: {prebuy_id}")

            # 9) YES <bedrag> [ID]
            if cmd == "YES":
                if len(args) < 1:
                    return twiml("Gebruik: YES <bedrag> [ID]")

                amount = safe_int(args[0], 0)
                if amount not in ALLOWED_AMOUNTS:
                    return twiml("Bedrag ongeldig. Toegestaan: 5/10/15/20/30/40/50")

                # ID meegegeven?
                if len(args) >= 2:
                    prebuy_id = args[1].strip()
                    p = get_by_id(conn, prebuy_id)
                    if not p:
                        return twiml("ID niet gevonden.")
                else:
                    # Geen ID => TOP 1
                    p = get_top_one(conn)
                    if not p:
                        return twiml("Geen ACTIVE pending/approved Pre-BUYs om te keuren.")
                    prebuy_id = str(p.get("id"))

                status = (p.get("status") or "PENDING").upper()
                if status not in ("PENDING", "APPROVED"):
                    return twiml(f"Kan niet YES doen: status is {status}")

                # Expiry check (maar we rejecten niet automatisch — jij wil kunnen debuggen)
                exp = as_aware_utc(p.get("expires_at"))
                if exp and exp <= now_utc():
                    return twiml(
                        "Deze Pre-BUY is verlopen.\n"
                        f"ID: {prebuy_id}\n\n"
                        "Tip: LISTALL + STATS.\n"
                        "Fix zit meestal in multi_coin_score: expires_at moet NOW()+4h zijn (UTC)."
                    )

                # Mark approved (ook bij retry)
                mark_approved(conn, prebuy_id)

                ok, msg = execute_buy(p, amount)

                if ok:
                    mark_consumed(conn, prebuy_id)
                    return twiml(
                        f"GOEDGEKEURD ✅\n{msg}\nID: {prebuy_id}\n\n"
                        f"Open trades check: {count_open_trades()}/{MAX_OPEN_TRADES}"
                    )

                # BUY faalde => blijft APPROVED zodat je kunt retryen
                return twiml(
                    f"GOEDGEKEURD ✅\nMaar BUY faalde:\n{msg}\nID: {prebuy_id}\n\n"
                    f"TIP: probeer nogmaals YES 10 {prebuy_id}\n"
                    "En check Render logs van crypto_ai (webhook service) + live_trader."
                )

            # Default
            return twiml("Onbekend command.\n\n" + HELP_TEXT)

    except Exception as e:
        log("❌ ERROR in /whatsapp")
        log(str(e))
        log(traceback.format_exc())
        return twiml("Interne fout. Check Render logs.")


if __name__ == "__main__":
    port = int(os.getenv("PORT") or "10000")
    app.run(host="0.0.0.0", port=port)
