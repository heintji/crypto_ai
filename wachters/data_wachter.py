#!/usr/bin/env python3
"""Data-Wachter (laag 2, kolom crypto) — klopt de meet-data nog?

Bewaakt precies de drie data-rotsoorten die onze backtest van 6 juni
om zeep hielpen, plus de waterdichtheid van Plan U:

  1. ONVERANDERBAARHEID: bestaat de bescherm-trigger op plan_u_trades nog
     en staat de unieke signaal-index er nog?
  2. DUBBELTELLINGEN: zelfde coin meermaals geopend binnen 1 minuut
     (experience_trades, laatste 24u) — het INX-3x-patroon
  3. ONLOGISCHE STOPS: long-trades waarvan de stop BOVEN de entry staat
     (laatste 24u) — het stops-muteren-patroon
  4. LEGE MFE/MAE: recent gesloten Plan U-trades zonder excursie-data
  5. VERWAARLOOSDE RESOLUTIE: open Plan U-trades die >2u niet zijn bijgewerkt

Bevindingen -> Centraal Logboek; HOOG -> direct mail. Draait dagelijks.

Env: DATABASE_URL (crypto), LOGBOEK_DATABASE_URL (Koa),
     RESEND_API_KEY, RESEND_FROM, ALERT_EMAIL
"""
import json
import os
import urllib.request
from datetime import datetime, timezone

import psycopg2

WACHTER = "data-wachter-crypto"
KOLOM = "crypto"


def mail(onderwerp: str, tekst: str) -> None:
    try:
        req = urllib.request.Request(
            "https://api.resend.com/emails", method="POST",
            data=json.dumps({"from": os.environ["RESEND_FROM"],
                             "to": [os.environ["ALERT_EMAIL"]],
                             "subject": onderwerp, "text": tekst}).encode(),
            headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
                     "Content-Type": "application/json",
                     "User-Agent": "JarvisDataWachter/1.0 (koa-ai.nl)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"[data] mail ({r.status}): {onderwerp}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[data] mail faalde: {e}", flush=True)


def logboek(ernst: str, melding: str, details: dict | None = None) -> None:
    conn = None
    try:
        conn = psycopg2.connect(os.environ["LOGBOEK_DATABASE_URL"])
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS toezicht_logboek (
                id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                wachter TEXT NOT NULL, kolom TEXT NOT NULL, ernst TEXT NOT NULL,
                melding TEXT NOT NULL, details JSONB)""")
            cur.execute("INSERT INTO toezicht_logboek (wachter, kolom, ernst, melding, details)"
                        " VALUES (%s,%s,%s,%s,%s)",
                        (WACHTER, KOLOM, ernst, melding[:1000], json.dumps(details or {})))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[data] logboek onbereikbaar (genegeerd): {e}", flush=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def main() -> None:
    conn = None
    bevindingen: list[tuple[str, str]] = []  # (ernst, melding)
    try:
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        with conn.cursor() as cur:
            # 1. Waterdichtheid: trigger + unieke index aanwezig?
            cur.execute("SELECT COUNT(*) FROM pg_trigger WHERE tgname='plan_u_bescherm_trg'")
            if cur.fetchone()[0] == 0:
                bevindingen.append(("HOOG", "Bescherm-trigger op plan_u_trades is VERDWENEN — "
                                            "startwaarden zijn niet langer onveranderbaar!"))
            cur.execute("""SELECT COUNT(*) FROM pg_indexes
                           WHERE tablename='plan_u_trades' AND indexdef ILIKE '%UNIQUE%signaal_id%'""")
            if cur.fetchone()[0] == 0:
                bevindingen.append(("HOOG", "Unieke signaal-index op plan_u_trades ontbreekt — "
                                            "dubbeltellingen weer mogelijk!"))

            # 2. Dubbeltellingen in experience_trades (zelfde coin, zelfde minuut)
            cur.execute("""SELECT symbol, date_trunc('minute', entry_time), COUNT(*)
                           FROM experience_trades
                           WHERE entry_time >= NOW() - INTERVAL '24 hours'
                           GROUP BY 1, 2 HAVING COUNT(*) > 1 LIMIT 5""")
            dubbels = cur.fetchall()
            if dubbels:
                voorbeelden = ", ".join(f"{r[0]} {r[2]}x" for r in dubbels)
                bevindingen.append(("MEDIUM", f"Dubbeltellingen laatste 24u: {voorbeelden}"))

            # 3. Onlogische stops (long: stop hoort ONDER entry)
            cur.execute("""SELECT COUNT(*) FROM experience_trades
                           WHERE entry_time >= NOW() - INTERVAL '24 hours'
                             AND stop IS NOT NULL AND entry IS NOT NULL AND stop > entry""")
            n_rare_stops = cur.fetchone()[0]
            if n_rare_stops:
                bevindingen.append(("HOOG", f"{n_rare_stops} trade(s) laatste 24u met stop BOVEN entry "
                                            "— stop-administratie muteert weer verkeerd"))

            # 4. Plan U: gesloten zonder MFE/MAE
            cur.execute("""SELECT COUNT(*) FROM plan_u_trades
                           WHERE status != 'OPEN' AND exit_ts >= NOW() - INTERVAL '24 hours'
                             AND mfe_pct = 0 AND mae_pct = 0""")
            n_leeg = cur.fetchone()[0]
            if n_leeg:
                bevindingen.append(("MEDIUM", f"{n_leeg} gesloten Plan U-trade(s) zonder MFE/MAE-data"))

            # 5. Plan U: open trades verwaarloosd (resolver stuk?)
            cur.execute("""SELECT COUNT(*) FROM plan_u_trades
                           WHERE status='OPEN' AND bijgewerkt < NOW() - INTERVAL '2 hours'""")
            n_oud = cur.fetchone()[0]
            if n_oud:
                bevindingen.append(("HOOG", f"{n_oud} open Plan U-trade(s) al >2u niet bijgewerkt "
                                            "— draait de schaduw-cron nog?"))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        try:
            if conn is not None:
                conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        bevindingen.append(("HOOG", f"Data-Wachter kon zijn ronde niet afmaken: {e}"))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    if not bevindingen:
        print("[data] alles waterdicht — geen bevindingen, geen mail", flush=True)
        return

    hoog = [m for e, m in bevindingen if e == "HOOG"]
    for ernst, melding in bevindingen:
        logboek(ernst, melding)
    if hoog:
        mail("🚨 Data-Wachter: meet-data in gevaar",
             "De Data-Wachter vond problemen die je cijfers onbetrouwbaar maken:\n\n"
             + "\n".join(f"  - {m}" for m in hoog)
             + "\n\nOverige bevindingen staan in het Centrale Logboek."
             + "\n\n— Data-Wachter (kolom crypto)")
    print(f"[data] {len(bevindingen)} bevinding(en), waarvan {len(hoog)} HOOG", flush=True)


if __name__ == "__main__":
    main()
