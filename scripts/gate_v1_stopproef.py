"""Accepteert Gate onze stop-order eigenlijk wel?

De stop wordt verstuurd als prijsorder met `put.type=market`, prijs "0" en
`ioc`. Geen enkele test raakt dat: het is een aanname over Gate's API die
niemand gemeten heeft. Klopt de vorm niet, dan eindigt élke trade in een
onmiddellijke marktverkoop en handelt de strategie nooit zoals bedoeld.

Deze proef doet dat veilig: hij stuurt de order voor een munt waarvan je NIETS
bezit. Dan kan er per definitie geen stop ontstaan. Wat we willen weten is
alleen WELKE fout Gate teruggeeft:

  - een fout over saldo/hoeveelheid  -> de VORM is goed, alleen het saldo ontbreekt
  - een fout over parameters/velden  -> de vorm klopt NIET; eerst repareren

Gebruik:
    GATE_API_KEY=... GATE_API_SECRET=... python3 scripts/gate_v1_stopproef.py BTC_USDT
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.gate_api import GateAPI, GateError, GateRejected, GateUnknownOutcome

VORM_GOED = ("BALANCE_NOT_ENOUGH", "INSUFFICIENT", "BALANCE", "TOO_SMALL", "AMOUNT")
VORM_FOUT = ("INVALID_PARAM", "INVALID_REQUEST_BODY", "MISSING", "PARAMETER", "NOT_SUPPORTED")


def hoofd(argv: list[str]) -> int:
    pair = (argv[0] if argv else "BTC_USDT").upper()
    sleutel, geheim = os.getenv("GATE_API_KEY", ""), os.getenv("GATE_API_SECRET", "")
    if not sleutel or not geheim:
        print("zet GATE_API_KEY en GATE_API_SECRET", flush=True)
        return 1

    api = GateAPI(sleutel, geheim, trading_enabled=True)
    munt = pair.removesuffix("_USDT")
    saldo = Decimal("0")
    try:
        for rij in api.accounts():
            if rij.get("currency") == munt:
                saldo = Decimal(str(rij.get("available") or "0"))
    except GateError as exc:
        print(f"kon saldo niet opvragen: {type(exc).__name__}: {exc}", flush=True)
        return 1
    if saldo > 0:
        print(f"LET OP: je bezit {saldo} {munt}. Kies een munt die je NIET hebt, "
              "anders kan er echt een stop ontstaan. Niets gedaan.", flush=True)
        return 1

    meta = api.pair(pair)
    prijs = Decimal(str(api.ticker(pair).get("last") or "1"))
    hoeveelheid = Decimal(str(meta.get("min_base_amount") or "0.001"))
    trigger = (prijs * Decimal("0.5")).quantize(Decimal(1).scaleb(-int(meta.get("precision", 4))))
    print(f"proef: stop voor {hoeveelheid} {munt} met trigger {trigger} (saldo is 0, dus "
          "er kan niets ontstaan)", flush=True)
    try:
        # Zestig seconden geldig: mocht Gate hem tóch aanmaken en het annuleren
        # mislukken, dan is het restrisico één minuut (Fable-controle 20-9).
        uit = api.create_stop(pair, hoeveelheid, trigger, "t-vormproef0", expiration=60)
    except GateRejected as exc:
        tekst = str(exc).upper()
        if any(w in tekst for w in VORM_GOED):
            print(f"UITSLAG: de VORM is goed — Gate weigert alleen op saldo/hoeveelheid ({exc})",
                  flush=True)
            return 0
        if any(w in tekst for w in VORM_FOUT):
            print(f"UITSLAG: de VORM KLOPT NIET — Gate weigert de opbouw van de order ({exc}). "
                  "Repareer dit vóór je live gaat.", flush=True)
            return 2
        print(f"UITSLAG: onduidelijke weigering, beoordeel zelf: {exc}", flush=True)
        return 2
    except GateUnknownOutcome as exc:
        print(f"UITSLAG: geen antwoord gekregen ({exc}). Kijk bij Gate of er een prijsorder "
              "is ontstaan voordat je opnieuw probeert.", flush=True)
        return 2
    # Onverwacht: Gate accepteerde hem toch. Meteen opruimen.
    print(f"LET OP: Gate accepteerde de order ({uit}). Ik annuleer hem direct.", flush=True)
    try:
        api.cancel_stop(str(uit["id"]))
        print("geannuleerd; de vorm is in elk geval goed", flush=True)
    except GateError as exc:
        print(f"ANNULEREN MISLUKT ({exc}) — annuleer prijsorder {uit.get('id')} zelf bij Gate!",
              flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(hoofd(sys.argv[1:]))
