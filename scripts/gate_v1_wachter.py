"""Onafhankelijke bewaking van de Gate V1-bot.

Drie dingen die de bot zelf niet kan melden, omdat hij dan al stilstaat:
1. de uitvoerder draait niet meer (geen hartslag),
2. het account staat op pauze,
3. de administratie klopt niet met wat er werkelijk bij Gate staat.

Draait als aparte cron, los van de uitvoerder. Leest alleen; plaatst nooit een
order. Meldt via e-mail (trading/gate_alert.py).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2

from trading.gate_alert import stuur
from trading.gate_api import GateAPI, GateError

STIL_NA_MINUTEN = int(os.getenv("GATE_V1_STIL_NA_MINUTEN", "30"))


def dec(waarde) -> Decimal:
    return Decimal(str(waarde or "0"))


def hoofd() -> int:
    database = os.getenv("DATABASE_URL", "").strip()
    account = os.getenv("GATE_V1_ACCOUNT", "default")
    if not database:
        print("wachter: geen DATABASE_URL, niets te bewaken", flush=True)
        return 0

    conn = psycopg2.connect(database, sslmode="require", connect_timeout=10)
    meldingen: list[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT paused,pause_reason,started_at,succeeded_at,failed_at,last_error "
                        "FROM gate_v1_accounts WHERE account=%s", (account,))
            rij = cur.fetchone()
        if rij is None:
            print("wachter: account bestaat nog niet; niets te bewaken", flush=True)
            return 0
        paused, reden, gestart, geslaagd, gefaald, fout = rij
        nu = datetime.now(timezone.utc)

        # 1. hartslag
        laatste = max([t for t in (gestart, geslaagd) if t], default=None)
        if laatste is None:
            meldingen.append("De uitvoerder heeft nog nooit gedraaid.")
        elif nu - laatste > timedelta(minutes=STIL_NA_MINUTEN):
            stil = int((nu - laatste).total_seconds() // 60)
            meldingen.append(f"De uitvoerder draait al {stil} minuten niet meer "
                             f"(laatste teken van leven: {laatste:%d-%m %H:%M} UTC). "
                             "Een open positie wordt in die tijd niet bewaakt; "
                             "de stop bij Gate vervalt na 48 uur.")

        # 2. pauze
        if paused:
            meldingen.append(f"Het account staat op pauze: {reden or 'geen reden vastgelegd'}. "
                             "De bot doet niets tot je hem vrijgeeft "
                             "(scripts/gate_v1_vrijgeven.py).")
        if gefaald and (not geslaagd or gefaald > geslaagd):
            meldingen.append(f"Laatste run mislukte ({fout or 'onbekend'}) om {gefaald:%d-%m %H:%M} UTC.")

        # 3. administratie tegen de werkelijkheid
        with conn.cursor() as cur:
            cur.execute("SELECT id,pair,state,data FROM gate_v1_positions "
                        "WHERE account=%s AND state NOT IN ('CLOSED','REJECTED','PAPER_CLOSED')",
                        (account,))
            posities = cur.fetchall()
        sleutel, geheim = os.getenv("GATE_API_KEY", ""), os.getenv("GATE_API_SECRET", "")
        if posities and sleutel and geheim:
            api = GateAPI(sleutel, geheim, trading_enabled=False)   # alleen lezen
            try:
                saldi = {r["currency"]: dec(r.get("available")) for r in api.accounts()}
                open_stops = api.list_stops(status="open")
            except GateError as exc:
                meldingen.append(f"Kon de stand bij Gate niet opvragen: {type(exc).__name__}.")
                saldi, open_stops = None, None
            if saldi is not None:
                for pos_id, pair, state, data in posities:
                    munt = pair.removesuffix("_USDT")
                    verwacht = dec((data or {}).get("remaining"))
                    werkelijk = saldi.get(munt, Decimal(0))
                    if verwacht and werkelijk < verwacht * Decimal("0.99"):
                        meldingen.append(
                            f"{pair}: administratie zegt {verwacht} {munt}, bij Gate staat {werkelijk}. "
                            f"Positie {pos_id} in status {state}.")
                    if state == "PROTECTED":
                        stop_id = str((data or {}).get("stop_id") or "")
                        ids = {str(s.get("id")) for s in (open_stops or [])}
                        if stop_id and stop_id not in ids:
                            meldingen.append(
                                f"{pair}: de stop die wij denken te hebben ({stop_id}) staat niet "
                                "meer open bij Gate. De positie kan onbeschermd zijn.")
    finally:
        conn.close()

    if not meldingen:
        print("wachter: alles in orde", flush=True)
        return 0
    tekst = "Bewaking Gate V1 vond het volgende:\n\n- " + "\n- ".join(meldingen)
    print(tekst, flush=True)
    stuur(tekst, onderwerp="Gate V1 bewaking")
    return 0


if __name__ == "__main__":
    raise SystemExit(hoofd())
