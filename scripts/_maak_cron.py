"""Maakt de bewakings-cron op Render. Waarden van geheimen worden nooit getoond."""
import json
import sys
import urllib.request

BRON = "crn-d8hvia8jo6nc73cnk850"          # bestaande crypto_ai-cron, alleen als bron
NAAM = "gate-v1-wachter"
NODIG = ("DATABASE_URL", "GATE_API_KEY", "GATE_API_SECRET",
         "RESEND_API_KEY", "RESEND_FROM", "ALERT_EMAIL")


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
        if d["service"]["name"] == NAAM:
            print(f"{NAAM} bestaat al ({d['service']['id']}) — niets gedaan.")
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
        print("Deze instellingen staan nergens op een crypto_ai-service en moet je zelf "
              "toevoegen in Render nadat de cron is aangemaakt: " + ", ".join(ontbreekt))

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
        "envVars": [{"key": k, "value": v} for k, v in waarden.items()],
    }, "POST")
    dienst = nieuw.get("service", nieuw)
    print("aangemaakt:", dienst.get("name"), dienst.get("id"))
    print("overgenomen instellingen:", ", ".join(sorted(waarden)) or "geen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
