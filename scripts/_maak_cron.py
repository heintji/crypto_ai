"""Maakt de bewakings-cron op Render. Waarden van geheimen worden nooit getoond."""
import json
import sys
import urllib.request

BRON = "crn-d8hvia8jo6nc73cnk850"          # bestaande crypto_ai-cron, alleen als bron
NAAM = "gate-v1-wachter"
NODIG = ("DATABASE_URL", "GATE_API_KEY", "GATE_API_SECRET",
         "RESEND_API_KEY", "RESEND_FROM", "ALERT_EMAIL")
# Geen geheim, wel bepalend: zonder deze praat de wachter met de verkeerde beurs.
EXTRA = {"GATE_API_BASE": "https://api.gateeu.com"}


def api(key, url, data=None, methode="GET"):
    r = urllib.request.Request(url, data=(json.dumps(data).encode() if data else None),
                               method=methode,
                               headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                                        "Content-Type": "application/json"})
    tekst = urllib.request.urlopen(r).read().decode()
    return json.loads(tekst, strict=False) if tekst.strip() else {}


def main():
    key = sys.argv[1]
    diensten = api(key, "https://api.render.com/v1/services?limit=100")
    for d in diensten:
        if d["service"]["name"] != NAAM:
            continue
        sid = d["service"]["id"]
        detail = d["service"].get("serviceDetails", {})
        aanwezig = {e["envVar"]["key"] for e in
                    api(key, f"https://api.render.com/v1/services/{sid}/env-vars?limit=100")}
        mist = [n for n in NODIG if n not in aanwezig]
        start = (detail.get("startCommand")
                 or detail.get("envSpecificDetails", {}).get("startCommand") or "")
        klachten = []
        if mist:
            klachten.append("mist instellingen: " + ", ".join(mist))
        if "gate_v1_wachter" not in start:
            klachten.append(f"draait niet de wachter maar: {start or '(leeg)'}")
        if detail.get("schedule") and detail["schedule"] != "*/15 * * * *":
            klachten.append(f"ander schema: {detail['schedule']}")
        if klachten:
            print(f"{NAAM} bestaat al ({sid}) maar klopt niet:")
            for k in klachten:
                print("  -", k)
            print("Repareer die in Render; ik raak een bestaande service niet aan.")
            return 1
        print(f"{NAAM} bestaat al ({sid}) en is goed ingesteld — niets gedaan.")
        return 0
    owner = diensten[0]["service"]["ownerId"]

    # geheimen verzamelen uit bestaande services; nooit printen
    waarden = {}
    for d in diensten:
        sid = d["service"]["id"]
        if "crypto_ai" not in (d["service"].get("repo") or "") and sid != BRON:
            continue
        for e in api(key, f"https://api.render.com/v1/services/{sid}/env-vars?limit=100"):
            naam = e["envVar"]["key"]
            if naam in NODIG and naam not in waarden and e["envVar"].get("value"):
                waarden[naam] = e["envVar"]["value"]
    ontbreekt = [n for n in NODIG if n not in waarden]
    if ontbreekt:
        # Nooit een betaalde bewaker aanmaken die niets kan bewaken of niets kan
        # melden. Eerst aanvullen, dan opnieuw draaien (Astra-controle 20-9).
        print("NIETS GEDAAN. Deze instellingen staan nergens op een crypto_ai-service:")
        for naam in ontbreekt:
            print("  -", naam)
        print("Zet ze eerst op een crypto_ai-service in Render (of vul GATE_API_BASE/"
              "RESEND-gegevens aan) en draai dit script opnieuw.")
        return 1

    nieuw = api(key, "https://api.render.com/v1/services", {
        "type": "cron_job",
        "name": NAAM,
        "ownerId": owner,
        "repo": "https://github.com/heintji/crypto_ai",
        "branch": "main",
        "autoDeploy": "yes",
        "serviceDetails": {
            "runtime": "python",
            "plan": "starter",
            "region": "frankfurt",
            "schedule": "*/15 * * * *",
            "envSpecificDetails": {
                "buildCommand": "pip install -r requirements.txt",
                "startCommand": "python scripts/gate_v1_wachter.py",
            },
        },
        "envVars": ([{"key": k, "value": v} for k, v in waarden.items()]
                    + [{"key": k, "value": v} for k, v in EXTRA.items()]),
    }, "POST")
    dienst = nieuw.get("service", nieuw)
    print("aangemaakt:", dienst.get("name"), dienst.get("id"))
    print("overgenomen instellingen:", ", ".join(sorted(waarden)) or "geen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
