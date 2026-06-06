#!/usr/bin/env python3
"""WACHTER (kolom Crypto): Reconciliatie-Wachter — laag 2 van de toezichtpiramide.

Vergelijkt de geregistreerde open LIVE-posities (experience_trades) met wat
Bitvavo ÉCHT aanhoudt (/v2/balance). Bij afwijking > 1%:
  - bot pauzeren (bot_state.bot_paused — wordt door de bot zelf gerespecteerd)
  - KRITIEK-alarm direct naar Hein (spoedregel) + kopie in het Centrale Logboek
Bij geen afwijking: stille hartslag naar bot_state
  (reconciliatie_last_check + reconciliatie_status).

v3.0-eisen, strikt:
  - db_connect() met retries; conn=None + cleanup in finally
  - safe_rollback() bij fouten; GEEN retry op 4xx van Bitvavo
  - BITVAVO_OPERATOR_ID-header alleen meesturen als de env-var gezet is
  - shadow/sim-trades worden NOOIT aangeraakt (filter: is_shadow=FALSE, source='LIVE')
  - deze wachter mag nergens een werkbot mee laten crashen: bij twijfel
    loggen en doorgaan; het script eindigt altijd netjes (exit 0)

Env: DATABASE_URL (crypto), LOGBOEK_DATABASE_URL (centraal logboek, Koa-DB),
     BITVAVO_API_KEY, BITVAVO_API_SECRET, [BITVAVO_OPERATOR_ID],
     RESEND_API_KEY, RESEND_FROM, ALERT_EMAIL,
     AFWIJKING_DREMPEL_PCT (default 1.0), ONBEKEND_ASSET_MIN_EUR (default 5)
"""
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import requests

BASE_URL = "https://api.bitvavo.com"
TIMEOUT = 15
DB_RETRIES = 3
DREMPEL = float(os.environ.get("AFWIJKING_DREMPEL_PCT", "1.0"))
MIN_EUR = float(os.environ.get("ONBEKEND_ASSET_MIN_EUR", "5"))
WACHTER = "reconciliatie-wachter"
KOLOM = "crypto"


# ── v3.0 database-helpers ────────────────────────────────────────────────
def db_connect(url_env: str, retries: int = DB_RETRIES):
    laatste = None
    for poging in range(1, retries + 1):
        try:
            return psycopg2.connect(os.environ[url_env])
        except Exception as e:  # noqa: BLE001 — bewust breed: wachter mag niet crashen
            laatste = e
            print(f"[recon] db_connect({url_env}) poging {poging}/{retries}: {e}", flush=True)
            time.sleep(2 * poging)
    raise RuntimeError(f"db_connect({url_env}) faalde na {retries} pogingen: {laatste}")


def safe_rollback(conn) -> None:
    try:
        if conn is not None:
            conn.rollback()
    except Exception as e:  # noqa: BLE001
        print(f"[recon] safe_rollback faalde (genegeerd): {e}", flush=True)


# ── Centraal Logboek (laag 3 leest dit) ──────────────────────────────────
def logboek(ernst: str, melding: str, details: dict | None = None) -> None:
    """Schrijft een bevinding naar het Centrale Logboek. Faalt stil (loggen mag
    nooit de reden zijn dat een alarm niet doorgaat)."""
    conn = None
    try:
        conn = db_connect("LOGBOEK_DATABASE_URL", retries=2)
        with conn.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS toezicht_logboek (
                       id BIGSERIAL PRIMARY KEY,
                       ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                       wachter TEXT NOT NULL,
                       kolom TEXT NOT NULL,
                       ernst TEXT NOT NULL,
                       melding TEXT NOT NULL,
                       details JSONB
                   )"""
            )
            cur.execute(
                "INSERT INTO toezicht_logboek (wachter, kolom, ernst, melding, details) VALUES (%s,%s,%s,%s,%s)",
                (WACHTER, KOLOM, ernst, melding[:1000], json.dumps(details or {})),
            )
        conn.commit()
        print(f"[recon] logboek <- {ernst}: {melding[:80]}", flush=True)
    except Exception as e:  # noqa: BLE001
        safe_rollback(conn)
        print(f"[recon] logboek niet bereikbaar (genegeerd): {e}", flush=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


# ── Spoedregel: KRITIEK gaat direct naar Hein ────────────────────────────
def alarm_mail(onderwerp: str, tekst: str) -> None:
    try:
        req = requests.post(
            "https://api.resend.com/emails",
            json={"from": os.environ["RESEND_FROM"],
                  "to": [os.environ["ALERT_EMAIL"]],
                  "subject": onderwerp, "text": tekst},
            headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
                     "User-Agent": "JarvisReconWachter/1.0 (koa-ai.nl)"},
            timeout=20,
        )
        print(f"[recon] alarm-mail ({req.status_code}): {onderwerp}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[recon] alarm-mail faalde: {e}", flush=True)


# ── Bitvavo (handtekening overgenomen uit account_snapshot.py) ───────────
def _auth_headers(method: str, path: str, body: str = "") -> dict:
    timestamp = str(int(time.time() * 1000))
    message = timestamp + method.upper() + path + (body or "")
    signature = hmac.new(
        os.environ["BITVAVO_API_SECRET"].encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    headers = {
        "Bitvavo-Access-Key": os.environ["BITVAVO_API_KEY"],
        "Bitvavo-Access-Signature": signature,
        "Bitvavo-Access-Timestamp": timestamp,
        "Bitvavo-Access-Window": "10000",
        "Content-Type": "application/json",
    }
    # v3.0: operator-header ALLEEN als de env-var echt gezet is
    operator = os.environ.get("BITVAVO_OPERATOR_ID", "").strip()
    if operator:
        headers["Bitvavo-Access-Operator-Id"] = operator
    return headers


def bitvavo_balances() -> dict:
    """Echte holdings: {asset: available + inOrder}. GEEN retry op 4xx."""
    path = "/v2/balance"
    r = requests.get(BASE_URL + path, headers=_auth_headers("GET", path), timeout=TIMEOUT)
    if 400 <= r.status_code < 500:
        raise RuntimeError(f"Bitvavo 4xx (geen retry): {r.status_code} {r.text[:150]}")
    if r.status_code != 200:
        # één herkansing voor 5xx/netwerk-hik
        time.sleep(3)
        r = requests.get(BASE_URL + path, headers=_auth_headers("GET", path), timeout=TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"Bitvavo error na herkansing: {r.status_code} {r.text[:150]}")
    saldi = {}
    for item in r.json():
        sym = item.get("symbol")
        totaal = float(item.get("available", 0) or 0) + float(item.get("inOrder", 0) or 0)
        if sym and sym != "EUR" and totaal > 0:
            saldi[sym] = totaal
    return saldi


def asset_waarde_eur(asset: str, hoeveelheid: float) -> float:
    """Publieke prijs voor waarde-schatting van onverwachte assets."""
    try:
        r = requests.get(f"{BASE_URL}/v2/ticker/price?market={asset}-EUR", timeout=10)
        if r.status_code == 200:
            return hoeveelheid * float(r.json().get("price", 0) or 0)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


# ── bot_state helpers (zelfde patroon als trade_monitor) ─────────────────
def set_state(cur, key: str, value: str) -> None:
    cur.execute(
        """INSERT INTO bot_state (key, value) VALUES (%s, %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
        (key, value),
    )


def pauzeer_bot(conn, reden: str) -> None:
    """Noodrem: bot mag geen nieuwe orders plaatsen. Shadow-metingen lopen door."""
    with conn.cursor() as cur:
        set_state(cur, "bot_paused", "true")
        set_state(cur, "bot_paused_until",
                  (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat())
        set_state(cur, "bot_paused_reason", f"reconciliatie: {reden}"[:300])
    conn.commit()


def main() -> None:
    conn = None
    try:
        conn = db_connect("DATABASE_URL")

        # 1. Geregistreerde open LIVE-posities (NOOIT shadow/sim — v3.0-eis)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT bitvavo_market, SUM(qty) FROM experience_trades
                   WHERE status = 'OPEN' AND is_shadow = FALSE AND source = 'LIVE'
                   GROUP BY bitvavo_market"""
            )
            verwacht = {}
            for markt, qty in cur.fetchall():
                asset = (markt or "").split("-")[0]
                if asset and qty:
                    verwacht[asset] = verwacht.get(asset, 0.0) + float(qty)

        # 2. Echte holdings bij Bitvavo
        echt = bitvavo_balances()

        # 3. Vergelijken
        afwijkingen = []
        for asset, exp in verwacht.items():
            werkelijk = echt.get(asset, 0.0)
            afw_pct = abs(werkelijk - exp) / exp * 100 if exp > 0 else 100.0
            if afw_pct > DREMPEL:
                afwijkingen.append(
                    f"{asset}: geregistreerd {exp:.8f}, bij Bitvavo {werkelijk:.8f} "
                    f"({afw_pct:.1f}% afwijking)"
                )
        for asset, werkelijk in echt.items():
            if asset not in verwacht:
                waarde = asset_waarde_eur(asset, werkelijk)
                if waarde >= MIN_EUR:
                    afwijkingen.append(
                        f"{asset}: ~EUR {waarde:.2f} bij Bitvavo maar NIET geregistreerd "
                        f"als open positie ({werkelijk:.8f})"
                    )

        # 4. Verdict — alarm alleen bij VERANDERING (anti-spam, Nachtwaker-patroon)
        nu = datetime.now(timezone.utc)
        nu_iso = nu.isoformat()
        if afwijkingen:
            reden = "; ".join(afwijkingen)[:400]
            vingerafdruk = hashlib.sha256(
                "|".join(sorted(a.split(":")[0] for a in afwijkingen)).encode()
            ).hexdigest()[:16]
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM bot_state WHERE key='reconciliatie_vingerafdruk'")
                rij = cur.fetchone()
                vorige_vinger = rij[0] if rij else ""
                cur.execute("SELECT value FROM bot_state WHERE key='reconciliatie_alarm_ts'")
                rij = cur.fetchone()
                laatste_alarm = rij[0] if rij else ""
            herinnering_nodig = True
            if laatste_alarm:
                try:
                    herinnering_nodig = (nu - datetime.fromisoformat(laatste_alarm)) > timedelta(hours=24)
                except ValueError:
                    pass
            nieuw_alarm = vingerafdruk != vorige_vinger

            pauzeer_bot(conn, reden)
            with conn.cursor() as cur:
                set_state(cur, "reconciliatie_last_check", nu_iso)
                set_state(cur, "reconciliatie_status", "AFWIJKING")
                set_state(cur, "reconciliatie_vingerafdruk", vingerafdruk)
            conn.commit()

            if nieuw_alarm or herinnering_nodig:
                tekst = (
                    "JARVIS RECONCILIATIE-ALARM (KRITIEK)\n\n"
                    "De administratie en Bitvavo zeggen niet hetzelfde:\n\n"
                    + "\n".join(f"  - {a}" for a in afwijkingen)
                    + "\n\nACTIE GEDAAN: trading-bot GEPAUZEERD (geen nieuwe orders; "
                    "schaduw-metingen lopen door).\n"
                    "WAT JIJ DOET: open Claude en zeg 'Jarvis, reconciliatie-alarm' — "
                    "dan zoeken we samen uit wat er afwijkt voor de bot weer aan mag.\n\n"
                    "(Je krijgt deze mail alleen bij een nieuwe afwijking of als "
                    "herinnering na 24 uur — niet elke 5 minuten.)"
                )
                alarm_mail("🚨 KRITIEK: posities wijken af van Bitvavo — bot gepauzeerd", tekst)
                with conn.cursor() as cur:
                    set_state(cur, "reconciliatie_alarm_ts", nu_iso)
                conn.commit()
                logboek("KRITIEK", f"Afwijking(en); bot gepauzeerd: {reden}",
                        {"afwijkingen": afwijkingen, "verwacht": verwacht, "echt": echt})
            else:
                print(f"[recon] afwijking houdt aan (zelfde vingerafdruk {vingerafdruk}) — "
                      "geen herhaal-mail", flush=True)
        else:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM bot_state WHERE key='reconciliatie_status'")
                rij = cur.fetchone()
                was_afwijking = bool(rij and rij[0] == "AFWIJKING")
                set_state(cur, "reconciliatie_last_check", nu_iso)
                set_state(cur, "reconciliatie_status", "OK")
                set_state(cur, "reconciliatie_vingerafdruk", "")
            conn.commit()
            if was_afwijking:
                alarm_mail("✅ Reconciliatie hersteld: administratie en Bitvavo kloppen weer",
                           "De eerder gemelde afwijking is opgelost — alles sluit weer aan.\n"
                           "Let op: de pauze-vlag van de bot blijft staan tot jij of Jarvis "
                           "hem bewust vrijgeeft (veiligheid eerst).\n\n— Reconciliatie-Wachter")
                logboek("MEDIUM", "Afwijking opgelost; administratie sluit weer aan op Bitvavo")
            print(f"[recon] OK — {len(verwacht)} geregistreerde posities, "
                  f"{len(echt)} assets bij Bitvavo, alles binnen {DREMPEL}%", flush=True)

    except Exception as e:  # noqa: BLE001 — wachter mag nooit hard falen
        safe_rollback(conn)
        print(f"[recon] FOUT (wachter zelf): {e}", flush=True)
        logboek("HOOG", f"Reconciliatie-Wachter kon zijn ronde niet afmaken: {e}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    main()
