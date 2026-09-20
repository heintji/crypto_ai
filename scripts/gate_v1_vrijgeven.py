"""Een pauze opheffen, bewust en met een spoor.

Een tijdelijke koersfout mag geen permanente stilstand worden, maar
vrijgeven moet wel een handeling van jou zijn: het script eist dat je de
reden van de pauze hebt gelezen en bevestigt.

Gebruik:  python3 scripts/gate_v1_vrijgeven.py --bevestig
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2

from trading.gate_alert import stuur


def hoofd(argv: list[str]) -> int:
    database = os.getenv("DATABASE_URL", "").strip()
    account = os.getenv("GATE_V1_ACCOUNT", "default")
    if not database:
        print("geen DATABASE_URL", flush=True)
        return 1
    conn = psycopg2.connect(database, sslmode="require", connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT paused,pause_reason FROM gate_v1_accounts WHERE account=%s", (account,))
            rij = cur.fetchone()
        if rij is None:
            print("account bestaat niet", flush=True)
            return 1
        paused, reden = rij
        if not paused:
            print("het account staat niet op pauze", flush=True)
            return 0
        print("Reden van de pauze:", reden or "(niet vastgelegd)", flush=True)
        if "--bevestig" not in argv:
            print("Niets gedaan. Heb je de oorzaak weggenomen, draai dan hetzelfde "
                  "commando met --bevestig erachter.", flush=True)
            return 0
        with conn.cursor() as cur:
            cur.execute("UPDATE gate_v1_accounts SET paused=false,pause_reason=null WHERE account=%s",
                        (account,))
            cur.execute("INSERT INTO gate_v1_events(account,kind,data) VALUES(%s,%s,%s::jsonb)",
                        (account, "UNPAUSED", '{"door":"handmatig"}'))
        conn.commit()
        print("vrijgegeven", flush=True)
        stuur(f"Pauze opgeheven. Vorige reden: {reden or 'onbekend'}", onderwerp="Gate V1 vrijgegeven")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(hoofd(sys.argv[1:]))
