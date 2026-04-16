# research/regime_position_reassess.py
# ============================================================
# Crypto AI Bot — Position Reassessment bij BTC regime-wissel
# ============================================================
# Getriggerd door build_btc_regime.notify_regime_change() via
# subprocess. Leest alle open LIVE trades, vraagt Claude (Sonnet)
# per positie een EXIT / HOLD / MOVE_STOP advies, stuurt via WA
# en slaat op in bot_state.
#
# Sonnet blijft hier — kritieke beslissing bij regime-shift.
# ============================================================

from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import requests

# ------------------------------------------------------------
# Config
# ------------------------------------------------------------
DATABASE_URL       = os.getenv("DATABASE_URL", "")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
OLD_REGIME         = os.getenv("OLD_REGIME", "?")
NEW_REGIME         = os.getenv("NEW_REGIME", "?")
TWILIO_SID         = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOK         = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM        = os.getenv("TWILIO_WHATSAPP_FROM", "")
TWILIO_TO          = os.getenv("TWILIO_WHATSAPP_TO", "")

CLAUDE_MODEL       = "claude-sonnet-4-6"   # Kritieke beslissing = Sonnet
BITVAVO_BASE       = "https://api.bitvavo.com/v2"


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[REASSESS {ts}] {msg}", flush=True)


def send_whatsapp(msg: str) -> None:
    if not all([TWILIO_SID, TWILIO_TOK, TWILIO_FROM, TWILIO_TO]):
        log("WhatsApp credentials ontbreken — skip notify")
        return
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            auth=(TWILIO_SID, TWILIO_TOK),
            data={"From": TWILIO_FROM, "To": TWILIO_TO, "Body": msg[:1500]},
            timeout=15,
        )
        if r.status_code not in (200, 201):
            log(f"Twilio {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log(f"Twilio fout: {e}")


def claude_reassess(positions: list) -> str:
    """Stuur alle open posities naar Claude, vraag per positie een advies."""
    if not ANTHROPIC_API_KEY or not positions:
        return ""
    lines = "\n".join(
        f"- {p['symbol']}: entry=€{p['entry']:.6f}, current=€{p['current']:.6f}, "
        f"pnl_r={p['pnl_r']:+.2f}R, hold_h={p['hold_h']:.1f}h, setup={p['setup_type']}"
        for p in positions
    )
    prompt = (
        f"BTC regime is zojuist gewisseld van {OLD_REGIME} naar {NEW_REGIME}.\n"
        f"Er zijn {len(positions)} open LIVE posities. Geef per positie een "
        f"duidelijke beslissing: EXIT / HOLD / MOVE_STOP (met reden).\n"
        f"Formaat per regel: `SYMBOL: ACTIE — reden (max 15 woorden)`\n\n"
        f"Posities:\n{lines}\n\n"
        f"Regels:\n"
        f"- BULL→BEAR of BULL→RANGE: overweeg EXIT bij open posities met pnl_r < 0\n"
        f"- BEAR→BULL: HOLD en eventueel MOVE_STOP strakker om winst vast te zetten\n"
        f"- RANGE→BULL of BEAR→RANGE: HOLD, pas eventueel stops aan\n"
        f"Kort en concreet. Geen lange uitleg."
    )
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 600,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()["content"][0]["text"]
        log(f"Claude {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log(f"Claude fout: {e}")
    return ""


def get_current_price(symbol_usdt: str) -> float:
    """Bitvavo REST ticker — convert BTCUSDT → BTC-EUR."""
    sym = symbol_usdt.replace("USDT", "-EUR").replace("BUSD", "-EUR")
    try:
        r = requests.get(f"{BITVAVO_BASE}/ticker/price?market={sym}", timeout=10)
        if r.status_code == 200:
            return float(r.json().get("price", 0))
    except Exception:
        pass
    return 0.0


def run() -> None:
    log(f"=== Position reassess {OLD_REGIME} → {NEW_REGIME} ===")
    if not DATABASE_URL:
        log("DATABASE_URL niet gezet — abort")
        return
    try:
        conn = psycopg2.connect(
            DATABASE_URL, sslmode="require", connect_timeout=10
        )
    except Exception as e:
        log(f"DB-connect fout: {e}")
        return

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT trade_key, symbol, entry, stop, target, score,
                       setup_type, market_regime, entry_time, mfe_r, mae_r
                FROM public.experience_trades
                WHERE UPPER(COALESCE(source,'')) IN ('LIVE','REAL')
                  AND UPPER(COALESCE(outcome,'OPEN')) = 'OPEN'
                  AND UPPER(COALESCE(status,'OPEN')) = 'OPEN'
                """
            )
            open_trades = cur.fetchall()

        if not open_trades:
            msg = (
                f"📊 BTC regime {OLD_REGIME}→{NEW_REGIME}\n\n"
                "Geen open LIVE posities — niks te doen."
            )
            log(msg.replace("\n", " | "))
            send_whatsapp(msg)
            return

        positions = []
        for t in open_trades:
            entry = float(t["entry"] or 0)
            stop = float(t["stop"] or 0)
            current = get_current_price(t["symbol"])
            risk = abs(entry - stop) if entry and stop else 1.0
            pnl_r = (current - entry) / risk if (risk > 0 and current) else 0.0
            entry_time = t["entry_time"]
            if entry_time:
                # Zorg dat tz-aware is
                if entry_time.tzinfo is None:
                    entry_time = entry_time.replace(tzinfo=timezone.utc)
                hold_h = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            else:
                hold_h = 0.0
            positions.append({
                "symbol": t["symbol"],
                "entry": entry,
                "current": current,
                "pnl_r": pnl_r,
                "hold_h": hold_h,
                "setup_type": t["setup_type"] or "?",
            })

        advice = claude_reassess(positions)
        if advice:
            msg = (
                f"🚨 BTC REGIME {OLD_REGIME}→{NEW_REGIME}\n"
                f"{len(positions)} open live posities\n\n"
                f"{advice}"
            )
            send_whatsapp(msg)

            # Sla advies op in bot_state voor dashboard
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.bot_state (key, value, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (key)
                    DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
                    """,
                    (
                        f"regime_reassess_last",
                        json.dumps({
                            "old_regime": OLD_REGIME,
                            "new_regime": NEW_REGIME,
                            "positions": positions,
                            "advice": advice,
                            "ts": datetime.now(timezone.utc).isoformat(),
                        }),
                    ),
                )
                conn.commit()
            log(f"Advies verstuurd voor {len(positions)} posities.")
        else:
            log("Geen advies ontvangen van Claude — geen actie.")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
