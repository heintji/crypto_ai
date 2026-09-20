"""Melding die aankomt waar jij hem ziet, niet in een logbestand.

De engine had een alert-haak, maar de uitvoerder gaf er niets aan mee: elke
waarschuwing werd `print("GATE ALERT: ...")` in een cron-log dat niemand leest.
Voor een bot die met echt geld handelt is dat geen melding (Astra + Fable, 20-9).

Kanaal is e-mail via Resend, hetzelfde als de bestaande wachters in deze repo.
Bewust NIET stil: lukt het versturen niet, dan zegt deze module dat hardop en
geeft hij False terug, zodat de aanroeper kan besluiten te stoppen.
"""
from __future__ import annotations

import os

import requests

ONDERWERP = "Gate V1"


def kanaal_gereed() -> tuple[bool, str]:
    """Kan er überhaupt gemeld worden? Geeft (ja/nee, reden)."""
    ontbreekt = [n for n in ("RESEND_API_KEY", "RESEND_FROM", "ALERT_EMAIL")
                 if not os.getenv(n, "").strip()]
    if ontbreekt:
        return False, "ontbrekende instellingen: " + ", ".join(ontbreekt)
    return True, ""


def stuur(bericht: str, onderwerp: str | None = None, timeout: int = 20) -> bool:
    """True als Resend de mail heeft aangenomen. Nooit stil falen."""
    gereed, reden = kanaal_gereed()
    if not gereed:
        print(f"[gate-alert] KAN NIET MELDEN ({reden}): {bericht}", flush=True)
        return False
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            json={"from": os.environ["RESEND_FROM"],
                  "to": [os.environ["ALERT_EMAIL"]],
                  "subject": f"{onderwerp or ONDERWERP}: {bericht[:80]}",
                  "text": bericht},
            headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
                     "User-Agent": "GateV1/1.0 (koa-ai.nl)"},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[gate-alert] versturen faalde ({type(exc).__name__}): {bericht}", flush=True)
        return False
    gelukt = 200 <= r.status_code < 300
    print(f"[gate-alert] {'verstuurd' if gelukt else 'GEWEIGERD'} ({r.status_code}): {bericht}",
          flush=True)
    return gelukt


def maak_alert():
    """De functie die de engine gebruikt. Print altijd, mailt als het kan."""
    def alert(bericht: str) -> None:
        print("GATE ALERT: " + bericht, flush=True)
        stuur(bericht)
    return alert


if __name__ == "__main__":
    # Zelftest: bewijst dat er een mail aankomt. Draai dit één keer voordat je
    # de bot met geld laat werken — een meldkanaal dat je niet getest hebt, is
    # geen meldkanaal.
    gereed, reden = kanaal_gereed()
    if not gereed:
        print("zelftest kan niet: " + reden, flush=True)
        raise SystemExit(1)
    ok = stuur("Zelftest van het meldkanaal. Zie je deze mail, dan werkt het.",
               onderwerp="Gate V1 zelftest")
    print("zelftest:", "gelukt" if ok else "MISLUKT", flush=True)
    raise SystemExit(0 if ok else 1)
