#!/usr/bin/env python3
"""
process_watchdog.py — Waterdichte Bot Monitor
==============================================
Draait elk uur als Cron Job.
Checkt of elke service zijn heartbeat heeft bijgewerkt.
Stuurt een duidelijk WhatsApp bericht als iets mis is:
- Welke service
- Wat er mis is
- Directe Render link naar de logs
- Hoe lang het al mis is
"""

import os, psycopg2, time, urllib.request, urllib.parse, base64
from datetime import datetime, timezone, timedelta

def send_whatsapp(msg: str) -> bool:
    """Stuur WhatsApp alert via Twilio."""
    try:
        sid    = os.environ.get("TWILIO_SID", "")
        auth   = os.environ.get("TWILIO_AUTH", "")
        to_num = os.environ.get("WA_TO_NUMBER", "")
        fr_num = os.environ.get("WA_FROM_NUMBER", "")
        if not all([sid, auth, to_num, fr_num]):
            print(f"[WATCHDOG] WA config ontbreekt")
            return False
        data = urllib.parse.urlencode({
            "Body": msg[:1500],
            "From": f"whatsapp:{fr_num}",
            "To":   f"whatsapp:{to_num}",
        }, encoding="utf-8").encode("utf-8")
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        creds = base64.b64encode(f"{sid}:{auth}".encode()).decode()
        req = urllib.request.Request(url, data=data, headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f"[WATCHDOG] WA fout: {e}")
        return False


DATABASE_URL         = os.getenv('DATABASE_URL', '')
TWILIO_ACCOUNT_SID   = os.getenv('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN    = os.getenv('TWILIO_AUTH_TOKEN', '')
TWILIO_WHATSAPP_FROM = os.getenv('TWILIO_WHATSAPP_FROM', '')
TWILIO_WHATSAPP_TO   = os.getenv('TWILIO_WHATSAPP_TO', '')

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] [WATCHDOG] {msg}", flush=True)

def stuur_wa(tekst):
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO]):
        log(f"WA (geen config): {tekst[:60]}")
        return
    try:
        data = urllib.parse.urlencode({
            'From': TWILIO_WHATSAPP_FROM, 'To': TWILIO_WHATSAPP_TO, 'Body': tekst
        }).encode()
        creds = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()).decode()
        req = urllib.request.Request(
            f'https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json',
            data=data,
            headers={'Authorization': f'Basic {creds}', 'Content-Type': 'application/x-www-form-urlencoded'}
        )
        urllib.request.urlopen(req, timeout=15)
        log("WhatsApp verstuurd")
    except Exception as e:
        log(f"WhatsApp fout: {e}")

def run():
    log("Watchdog gestart")
    problemen = []

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur  = conn.cursor()

        # Haal alle services op met hun verwachte heartbeat
        cur.execute("""
            SELECT
                service,
                status,
                laatste_run,
                heartbeat_sec,
                details,
                fout,
                render_url,
                live_count,
                shadow_count,
                sim_count,
                EXTRACT(EPOCH FROM (NOW() - laatste_run)) AS seconden_geleden
            FROM bot_health
            ORDER BY service
        """)
        services = cur.fetchall()

        log(f"{len(services)} services gevonden")

        for svc, status, laatste_run, hb_sec, details, fout, render_url, live, shadow, sim, sec_oud in services:
            sec_oud = int(sec_oud or 0)
            min_oud = sec_oud // 60
            uur_oud = min_oud // 60
            tolerantie = hb_sec * 2  # Geef dubbele tijd als tolerantie

            # Label hoe lang geleden
            if sec_oud < 60:
                tijdlabel = f"{sec_oud}s geleden"
            elif min_oud < 60:
                tijdlabel = f"{min_oud}m geleden"
            else:
                tijdlabel = f"{uur_oud}u {min_oud % 60}m geleden"

            # Check 1: Heartbeat te oud?
            if sec_oud > tolerantie:
                problemen.append({
                    'service':   svc,
                    'type':      'GEEN HEARTBEAT',
                    'detail':    f"Laatste run: {tijdlabel} (max: {tolerantie//60}m)",
                    'render':    render_url or '',
                    'ernst':     'KRITIEK' if sec_oud > tolerantie * 3 else 'HOOG',
                })
                log(f"PROBLEEM [{svc}]: geen heartbeat — {tijdlabel}")
            else:
                log(f"OK [{svc}]: {tijdlabel} — {status} — {details or ''}")

            # Check 2: Status is ERROR?
            if status == 'ERROR':
                problemen.append({
                    'service':  svc,
                    'type':     'ERROR STATUS',
                    'detail':   fout or details or 'Onbekende fout',
                    'render':   render_url or '',
                    'ernst':    'KRITIEK',
                })
                log(f"ERROR [{svc}]: {fout or details}")

            # Check 3: Live trader heeft shadow maar geen shadow trades?
            if svc == 'live_trader' and live > 0 and shadow == 0 and status == 'OK':
                problemen.append({
                    'service':  'shadow_trades',
                    'type':     'SHADOW ONTBREEKT',
                    'detail':   f"{live} live trades open maar 0 shadow trades — shadow_buy werkt niet",
                    'render':   render_url or '',
                    'ernst':    'HOOG',
                })

        # Extra check: zijn er live trades ouder dan 49u?
        cur.execute("""
            SELECT symbol, EXTRACT(EPOCH FROM (NOW() - entry_time))/3600 AS uur_oud
            FROM experience_trades
            WHERE trade_key LIKE 'LIVE|%%' AND status='OPEN'
              AND entry_time < NOW() - INTERVAL '49 hours'
        """)
        stale = cur.fetchall()
        if stale:
            problemen.append({
                'service': 'live_trader',
                'type':    'STALE TRADES',
                'detail':  f"{len(stale)} trades ouder dan 49u: " + ', '.join(f"{r[0]} ({int(r[1])}u)" for r in stale),
                'render':  'https://dashboard.render.com/worker/srv-d762id7kijhs73ca4agg/logs',
                'ernst':   'HOOG',
            })

        conn.close()

    except Exception as e:
        log(f"DB FOUT: {e}")
        problemen.append({
            'service': 'DATABASE',
            'type':    'DB ONBEREIKBAAR',
            'detail':  str(e)[:200],
            'render':  '',
            'ernst':   'KRITIEK',
        })

    # ─── Rapporteer ───────────────────────────────────────
    if problemen:
        kritiek = [p for p in problemen if p['ernst'] == 'KRITIEK']
        hoog    = [p for p in problemen if p['ernst'] == 'HOOG']

        bericht  = "\U0001f6a8 BOT WATCHDOG ALERT\n"
        bericht += "=" * 32 + "\n"
        bericht += f"{len(problemen)} problemen gevonden\n\n"

        for p in problemen:
            emoji = "\U0001f534" if p['ernst'] == 'KRITIEK' else "\U0001f7e1"
            bericht += f"{emoji} {p['service'].upper()}\n"
            bericht += f"   {p['type']}: {p['detail'][:100]}\n"
            if p['render']:
                bericht += f"   Logs: {p['render']}\n"
            bericht += "\n"

        bericht += "Check direct!"
        # ── Deduplicatie per service: max 1 WA per 4 uur per service ──
        try:
            _conn2 = psycopg2.connect(os.environ["DATABASE_URL"])
            _cur2  = _conn2.cursor()
            _te_sturen = []
            for _p in problemen:
                _wa_key = f"watchdog_wa_{_p['service'].lower().replace(' ','_')}"
                _cur2.execute("SELECT value FROM bot_state WHERE key=%s", (_wa_key,))
                _row2 = _cur2.fetchone()
                _mag_sturen = True
                if _row2:
                    try:
                        _last = datetime.fromisoformat(_row2[0])
                        if (datetime.now(timezone.utc) - _last).total_seconds() < 4 * 3600:
                            _mag_sturen = False
                    except Exception:
                        pass
                if _mag_sturen:
                    _te_sturen.append(_p)
                    _cur2.execute(
                        "INSERT INTO bot_state (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=%s",
                        (_wa_key, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat())
                    )
            _conn2.commit()
            _conn2.close()
            if _te_sturen:
                stuur_wa(bericht)
        except Exception as _e2:
            log(f"WA dedup fout: {_e2}")
            stuur_wa(bericht)

        except Exception as _de:
            log(f"Dedup fout: {_de}")
            stuur_wa(bericht)  # Bij fout toch sturen
        log(f"Alert verstuurd: {len(kritiek)} kritiek, {len(hoog)} hoog")
    else:
        log("Alle services OK \u2705 — geen alert")

    # Schrijf eigen heartbeat
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO bot_health (service, status, laatste_run, details, heartbeat_sec)
            VALUES ('watchdog', 'OK', NOW(), %s, 3600)
            ON CONFLICT (service) DO UPDATE SET
                status='OK', laatste_run=NOW(), details=EXCLUDED.details
        """, (f"{len(problemen)} problemen gevonden",))
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"Eigen heartbeat fout: {e}")

    log("Klaar")

if __name__ == '__main__':
    run()
