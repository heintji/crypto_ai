#!/usr/bin/env python3
"""
process_watchdog.py — Bot Process Monitor Agent
================================================
Bewaakt alle lopende processen van de crypto bot.
Controleert elke run of:
  - Live trades worden uitgevoerd (LIVE|)
  - Shadow trades parallel lopen (SHADOW|)
  - SIM trades actief zijn (SIM|)
  - Trade_monitor open posities bewaakt
  - History_fetcher candles ophaalt
  - BTC regime actueel is
  - bot_state heartbeats recent zijn

Draait als Cron Job op Render.
Stuurt WhatsApp alert bij problemen.
"""

import os, sys, time
import psycopg2
from datetime import datetime, timezone, timedelta
import urllib.request, json

# ─── Configuratie ────────────────────────────────────────
DATABASE_URL          = os.getenv("DATABASE_URL", "")
TWILIO_ACCOUNT_SID    = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN     = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM  = os.getenv("TWILIO_WHATSAPP_FROM", "")
TWILIO_WHATSAPP_TO    = os.getenv("TWILIO_WHATSAPP_TO", "")
WEBHOOK_BASE_URL      = os.getenv("WEBHOOK_BASE_URL", "")

# Drempelwaarden
MAX_UUR_GEEN_CANDLES    = 3    # Alert als candles > 3u oud
MAX_UUR_GEEN_REGIME     = 5    # Alert als BTC regime > 5u oud
MAX_UUR_GEEN_MONITOR    = 1    # Alert als trade_monitor > 1u stil
MAX_DAGEN_GEEN_SHADOW   = 1    # Alert als geen shadow trades in 24u bij actieve bot
MAX_DAGEN_GEEN_SIM      = 2    # Alert als geen SIM trades in 48u

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [WATCHDOG] {msg}", flush=True)

def db_connect():
    return psycopg2.connect(DATABASE_URL)

def stuur_wa(bericht: str) -> bool:
    """Stuur WhatsApp alert via Twilio."""
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO]):
        log(f"WA (geen Twilio): {bericht[:80]}")
        return False
    try:
        import urllib.parse
        data = urllib.parse.urlencode({
            "From": TWILIO_WHATSAPP_FROM,
            "To":   TWILIO_WHATSAPP_TO,
            "Body": bericht,
        }).encode()
        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
        import base64
        credentials = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()).decode()
        req = urllib.request.Request(url, data=data, headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        urllib.request.urlopen(req, timeout=15)
        log("WhatsApp alert verstuurd")
        return True
    except Exception as e:
        log(f"WhatsApp fout: {e}")
        return False

def now_utc():
    return datetime.now(timezone.utc)

def uur_geleden(uren: float) -> datetime:
    return now_utc() - timedelta(hours=uren)

def run_checks() -> list:
    """Voer alle checks uit. Geeft lijst van problemen terug."""
    problemen = []
    waarschuwingen = []

    try:
        conn = db_connect()
        cur  = conn.cursor()
        log("DB verbonden — checks starten")

        # ─────────────────────────────────────────────
        # CHECK 1: Candles — history_fetcher actief?
        # ─────────────────────────────────────────────
        cur.execute("""
            SELECT MAX(close_time) FROM candles
            WHERE timeframe = '1h'
        """)
        laatste_candle = cur.fetchone()[0]
        if laatste_candle is None:
            problemen.append("❌ CANDLES: Geen candles gevonden in DB!")
        else:
            if laatste_candle.tzinfo is None:
                laatste_candle = laatste_candle.replace(tzinfo=timezone.utc)
            oud_uren = (now_utc() - laatste_candle).total_seconds() / 3600
            if oud_uren > MAX_UUR_GEEN_CANDLES:
                problemen.append(f"❌ CANDLES: Laatste candle is {oud_uren:.1f}u oud (max {MAX_UUR_GEEN_CANDLES}u) — history_fetcher vastgelopen?")
            else:
                log(f"✅ Candles OK — {oud_uren:.1f}u oud")

        # ─────────────────────────────────────────────
        # CHECK 2: BTC Regime — build_btc_regime actief?
        # ─────────────────────────────────────────────
        try:
            cur.execute("""
                SELECT MAX(open_time) FROM btc_regime_4h
            """)
            laatste_regime = cur.fetchone()[0]
            if laatste_regime is None:
                problemen.append("❌ BTC REGIME: Geen data in btc_regime_4h tabel!")
            else:
                if laatste_regime.tzinfo is None:
                    laatste_regime = laatste_regime.replace(tzinfo=timezone.utc)
                oud_uren = (now_utc() - laatste_regime).total_seconds() / 3600
                if oud_uren > MAX_UUR_GEEN_REGIME:
                    problemen.append(f"❌ BTC REGIME: {oud_uren:.1f}u oud (max {MAX_UUR_GEEN_REGIME}u)")
                else:
                    log(f"✅ BTC Regime OK — {oud_uren:.1f}u oud")
        except Exception as e:
            waarschuwingen.append(f"⚠️ BTC REGIME check fout: {e}")

        # ─────────────────────────────────────────────
        # CHECK 3: Trade_monitor heartbeat
        # ─────────────────────────────────────────────
        try:
            cur.execute("""
                SELECT value FROM bot_state
                WHERE key = 'trade_monitor_last_check'
            """)
            row = cur.fetchone()
            if row is None:
                waarschuwingen.append("⚠️ MONITOR: Geen heartbeat in bot_state — trade_monitor draait niet?")
            else:
                try:
                    ts = float(row[0])
                    oud_uren = (time.time() - ts) / 3600
                    if oud_uren > MAX_UUR_GEEN_MONITOR:
                        problemen.append(f"❌ MONITOR: Laatste heartbeat {oud_uren:.1f}u geleden!")
                    else:
                        log(f"✅ Trade_monitor OK — {oud_uren:.1f}u geleden")
                except Exception:
                    waarschuwingen.append("⚠️ MONITOR: Heartbeat waarde onleesbaar")
        except Exception as e:
            waarschuwingen.append(f"⚠️ MONITOR check: {e}")

        # ─────────────────────────────────────────────
        # CHECK 4: Live trades — bot is actief?
        # ─────────────────────────────────────────────
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE status='OPEN')  AS open_live,
                COUNT(*) FILTER (WHERE entry_time >= NOW() - INTERVAL '24 hours') AS vandaag
            FROM experience_trades
            WHERE trade_key LIKE 'LIVE|%%'
        """)
        r = cur.fetchone()
        open_live, vandaag_live = r
        log(f"Live trades: {open_live} open, {vandaag_live} vandaag")

        # ─────────────────────────────────────────────
        # CHECK 5: Shadow trades — lopen ze parallel?
        # ─────────────────────────────────────────────
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE status='OPEN')  AS open_shadow,
                COUNT(*) FILTER (WHERE entry_time >= NOW() - INTERVAL '24 hours') AS vandaag
            FROM experience_trades
            WHERE trade_key LIKE 'SHADOW|%%'
        """)
        r = cur.fetchone()
        open_shadow, vandaag_shadow = r
        log(f"Shadow trades: {open_shadow} open, {vandaag_shadow} vandaag")

        # Check: als er live trades zijn maar geen shadow trades, is dat een probleem
        if open_live > 0 and open_shadow == 0:
            problemen.append(f"❌ SHADOW: {open_live} live trades open maar 0 shadow trades! shadow_buy werkt niet?")
        elif vandaag_live > 0 and vandaag_shadow == 0:
            problemen.append(f"❌ SHADOW: {vandaag_live} live trades vandaag maar 0 shadow trades vandaag")
        else:
            log(f"✅ Shadow trades OK — {open_shadow} open parallel aan {open_live} live")

        # ─────────────────────────────────────────────
        # CHECK 6: SIM trades — strategy_simulator actief?
        # ─────────────────────────────────────────────
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE status='OPEN')  AS open_sim,
                COUNT(*) FILTER (WHERE entry_time >= NOW() - INTERVAL '48 hours') AS recent
            FROM experience_trades
            WHERE trade_key LIKE 'SIM|%%'
        """)
        r = cur.fetchone()
        open_sim, recent_sim = r
        log(f"SIM trades: {open_sim} open, {recent_sim} afgelopen 48u")

        if recent_sim == 0:
            problemen.append(f"❌ SIM: Geen SIM trades in afgelopen 48u — strategy_simulator vastgelopen?")
        else:
            log(f"✅ SIM trades OK — {recent_sim} trades afgelopen 48u")

        # ─────────────────────────────────────────────
        # CHECK 7: Open trades vs stop-loss/target check
        # ─────────────────────────────────────────────
        cur.execute("""
            SELECT symbol, entry_time, stop_loss, target
            FROM experience_trades
            WHERE trade_key LIKE 'LIVE|%%'
              AND status = 'OPEN'
              AND entry_time < NOW() - INTERVAL '49 hours'
        """)
        te_oud = cur.fetchall()
        if te_oud:
            symbolen = [r[0] for r in te_oud]
            problemen.append(f"❌ STALE: {len(te_oud)} live trades ouder dan 49u: {', '.join(symbolen)} — max hold overschreden?")
        else:
            log("✅ Geen stale live trades")

        # ─────────────────────────────────────────────
        # CHECK 8: Bot state — is de bot aan?
        # ─────────────────────────────────────────────
        cur.execute("""
            SELECT key, value FROM bot_state
            WHERE key IN ('bot_running', 'live_trader_busy', 'last_action')
        """)
        bot_state_rows = {r[0]: r[1] for r in cur.fetchall()}
        log(f"Bot state: {bot_state_rows}")

        # ─────────────────────────────────────────────
        # CHECK 9: Dagbudget check
        # ─────────────────────────────────────────────
        cur.execute("""
            SELECT
                SUM(CASE WHEN UPPER(outcome)='LOSS' THEN ABS(COALESCE(pnl_eur,0)) ELSE 0 END) AS dagverlies,
                COUNT(*) FILTER (WHERE status='CLOSED') AS gesloten_vandaag
            FROM experience_trades
            WHERE trade_key LIKE 'LIVE|%%'
              AND entry_time >= CURRENT_DATE
        """)
        r = cur.fetchone()
        dagverlies, gesloten_vandaag = (float(r[0] or 0), r[1])
        if dagverlies >= 5.0:
            waarschuwingen.append(f"⚠️ DAGBUDGET: EUR{dagverlies:.2f} verlies vandaag — bot zou gestopt moeten zijn")
        else:
            log(f"✅ Dagbudget OK — EUR{dagverlies:.2f} verlies")

        # ─────────────────────────────────────────────
        # CHECK 10: Pending approvals ophoping
        # ─────────────────────────────────────────────
        try:
            cur.execute("""
                SELECT COUNT(*) FROM pending_approvals
                WHERE created_at < NOW() - INTERVAL '2 hours'
                  AND status = 'PENDING'
            """)
            oud_pending = cur.fetchone()[0]
            if oud_pending > 10:
                problemen.append(f"❌ PENDING: {oud_pending} goedkeuringen ouder dan 2u — webhook verstopt?")
            else:
                log(f"✅ Pending approvals OK — {oud_pending} oud")
        except Exception as e:
            log(f"Pending check overgeslagen: {e}")

        conn.close()

    except Exception as e:
        problemen.append(f"❌ DB FOUT: Kan niet verbinden — {type(e).__name__}: {str(e)[:100]}")

    return problemen, waarschuwingen, {
        'open_live': open_live if 'open_live' in dir() else '?',
        'open_shadow': open_shadow if 'open_shadow' in dir() else '?',
        'open_sim': open_sim if 'open_sim' in dir() else '?',
        'vandaag_live': vandaag_live if 'vandaag_live' in dir() else '?',
    }

def main():
    log("=" * 50)
    log("Process Watchdog gestart")
    log("=" * 50)

    try:
        problemen, waarschuwingen, stats = run_checks()
    except Exception as e:
        problemen = [f"❌ WATCHDOG CRASH: {e}"]
        waarschuwingen = []
        stats = {}

    # Rapporteer
    totaal_issues = len(problemen) + len(waarschuwingen)
    log(f"Checks klaar: {len(problemen)} problemen, {len(waarschuwingen)} waarschuwingen")

    for p in problemen:
        log(p)
    for w in waarschuwingen:
        log(w)

    # Stuur WhatsApp alert als er problemen zijn
    if problemen:
        bericht = "\U0001f6a8 WATCHDOG ALERT\n"
        bericht += "=" * 30 + "\n"
        bericht += f"\U0001f4ca Status:\n"
        bericht += f"  Live: {stats.get('open_live','?')} open | Shadow: {stats.get('open_shadow','?')} open | SIM: {stats.get('open_sim','?')} open\n\n"
        bericht += "Problemen:\n"
        for p in problemen:
            bericht += f"{p}\n"
        if waarschuwingen:
            bericht += "\nWaarschuwingen:\n"
            for w in waarschuwingen[:3]:
                bericht += f"{w}\n"
        bericht += "\nCheck Render logs!"
        stuur_wa(bericht)
    elif waarschuwingen:
        log("Alleen waarschuwingen — geen WA")
    else:
        log("Alle checks OK \u2705")

    # Schrijf heartbeat naar bot_state
    try:
        conn = db_connect()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO bot_state (key, value)
            VALUES ('watchdog_last_run', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (str(time.time()),))
        conn.commit()
        conn.close()
        log("Heartbeat geschreven naar bot_state")
    except Exception as e:
        log(f"Heartbeat fout: {e}")

    log("Watchdog run klaar")

if __name__ == "__main__":
    main()
