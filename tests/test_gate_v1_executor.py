"""De uitvoerder had geen enkele test (Fable-controle 20-9).

Juist daar zit de schakelaar die beslist of er met echt geld gehandeld wordt.
Deze tests houden die schakelaar vast: zonder de dubbele bevestiging mag er
geen verbinding met de database of met Gate gemaakt worden, en mag de
handelslaag niet aan staan.
"""
import importlib
import sys

import pytest


def _uitvoerder(monkeypatch, **env):
    """Laadt de uitvoerder met een schone omgeving."""
    for naam in ("GATE_V1_PAIRS", "DATABASE_URL", "GATE_API_KEY", "GATE_API_SECRET",
                 "GATE_V1_LIVE", "GATE_V1_CONFIRM",
                 "RESEND_API_KEY", "RESEND_FROM", "ALERT_EMAIL"):
        monkeypatch.delenv(naam, raising=False)
    for naam, waarde in env.items():
        monkeypatch.setenv(naam, waarde)
    mod = importlib.import_module("scripts.gate_v1_executor")
    return importlib.reload(mod)


def _alles_gezet():
    return dict(GATE_V1_PAIRS="TEST_USDT", DATABASE_URL="postgres://x",
                GATE_API_KEY="k", GATE_API_SECRET="s",
                RESEND_API_KEY="r", RESEND_FROM="bot@koa-ai.nl", ALERT_EMAIL="hein@example.com")


def test_zonder_bevestiging_raakt_de_uitvoerder_niets_aan(monkeypatch):
    """LIVE=true maar geen CONFIRM: geen database, geen Gate, geen order."""
    mod = _uitvoerder(monkeypatch, **_alles_gezet(), GATE_V1_LIVE="true")

    def niet_verbinden(*a, **kw):
        raise AssertionError("de uitvoerder mag zonder bevestiging geen verbinding maken")
    monkeypatch.setattr(mod.psycopg2, "connect", niet_verbinden)
    assert mod.main() == 0


def test_zonder_paren_of_sleutels_doet_hij_niets(monkeypatch):
    for ontbreekt in ("GATE_V1_PAIRS", "DATABASE_URL", "GATE_API_KEY", "GATE_API_SECRET"):
        env = _alles_gezet()
        env.pop(ontbreekt)
        mod = _uitvoerder(monkeypatch, **env, GATE_V1_LIVE="true", GATE_V1_CONFIRM="LIVE_V1")
        monkeypatch.setattr(mod.psycopg2, "connect",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                AssertionError(f"verbinding ondanks ontbrekende {ontbreekt}")))
        assert mod.main() == 0, ontbreekt


def test_de_handelslaag_staat_alleen_aan_bij_live_en_bevestiging(monkeypatch):
    """GateAPI mag alleen met trading_enabled=True gemaakt worden als beide
    vlaggen kloppen; dat is de laatste rem voor een echte order."""
    gemaakt = []

    for live, confirm, verwacht in (("true", "LIVE_V1", True),
                                    ("true", "FOUT", None),      # no-op, geen GateAPI
                                    ("false", "LIVE_V1", False)):
        env = _alles_gezet()
        mod = _uitvoerder(monkeypatch, **env, GATE_V1_LIVE=live, GATE_V1_CONFIRM=confirm)
        gemaakt.clear()

        class NepAPI:
            def __init__(self, key, secret, base_url=None, trading_enabled=False):
                gemaakt.append(trading_enabled)
        monkeypatch.setattr(mod, "GateAPI", NepAPI)
        monkeypatch.setattr(mod.psycopg2, "connect", lambda *a, **kw: _NepConn())
        monkeypatch.setattr(mod, "Store", lambda conn, account: _NepStore())
        monkeypatch.setattr(mod, "Engine", lambda api, store, limits, alert=None: object())
        mod.main()
        if verwacht is None:
            assert not gemaakt, f"GateAPI aangemaakt bij live={live} confirm={confirm}"
        else:
            assert gemaakt == [verwacht], (live, confirm, gemaakt)


class _NepConn:
    def close(self):
        pass


class _NepStore:
    def install(self):
        pass

    def run_lock(self):
        class Lock:
            def __enter__(self):
                return False          # doe alsof een andere run bezig is

            def __exit__(self, *a):
                return False
        return Lock()

    def heartbeat(self, *a, **kw):
        pass

    def pause(self, *a, **kw):
        pass


# ── verkoopcontrole mag nooit overgeslagen worden ─────────────────────────
# Eerder zat die achter drie `continue`s: geen gesloten uurcandle van vandaag
# (vlak na middernacht), munt niet meer "tradable" (delisting) of munt uit de
# kooplijst gehaald. Juist dan wil je eruit (Astra + Fable, 20-9).

class _API:
    """Nep-Gate zonder gesloten uurcandle van vandaag."""

    def __init__(self):
        self.gevraagd = []

    def candles(self, pair, interval, limit):
        self.gevraagd.append((pair, interval))
        if pair.startswith("BTC_"):
            # BTC-dagdata is er wél; die is alleen voor KOPEN nodig.
            return [[str(1758326400 - i * 86400), "1", "100", "101", "99", "100", "0", "true"]
                    for i in range(30)]
        # Voor de munt zelf: geen gesloten uurcandle van vandaag (net na
        # middernacht). De verkoopcontrole moet dan tóch draaien.
        return [["1758326400", "1", "100", "101", "99", "100", "0", "false"]]

    def ticker(self, pair):
        return {"lowest_ask": "100", "highest_bid": "99", "quote_volume": "1000000"}

    def pair(self, pair):
        return {"trade_status": "untradable", "quote": "USDC", "base": pair.rpartition("_")[0]}

    def accounts(self):
        return [{"currency": "USDT", "available": "100"}]


class _Store:
    def __init__(self, positie):
        self.positie = positie

    def install(self):
        pass

    def run_lock(self):
        class Lock:
            def __enter__(self_inner):
                return True

            def __exit__(self_inner, *a):
                return False
        return Lock()

    def positions(self):
        return [self.positie]

    def heartbeat(self, *a, **kw):
        pass

    def pause(self, *a, **kw):
        pass

    def cache_get(self, key):
        return None

    def cache_set(self, *a, **kw):
        pass

    def account_state(self):
        return {"paused": False, "reason": None}


def test_verkoopcontrole_draait_ook_zonder_uurcandle_en_buiten_de_kooplijst(monkeypatch):
    positie = {"id": "p1", "pair": "TEST_USDT", "state": "PROTECTED", "day": "2026-09-20",
               "data": {"entry_at": "2026-09-20T12:00:00+00:00", "entry_price": "100",
                        "day_open": "100", "metadata": {}}}
    # de munt staat NIET in de kooplijst
    mod = _uitvoerder(monkeypatch, GATE_V1_PAIRS="ANDERE_USDT", DATABASE_URL="postgres://x",
                      GATE_API_KEY="k", GATE_API_SECRET="s", GATE_V1_LIVE="false")
    gesloten = []

    class NepEngine:
        def __init__(self, *a, **kw):
            pass

        def reconcile(self, p):
            pass

        def close(self, p, reden):
            gesloten.append((p["id"], reden))

        def open(self, *a, **kw):
            raise AssertionError("mag niet kopen zonder bevestiging")

    monkeypatch.setattr(mod, "GateAPI", lambda *a, **kw: _API())
    monkeypatch.setattr(mod.psycopg2, "connect", lambda *a, **kw: _NepConn())
    monkeypatch.setattr(mod, "Store", lambda conn, account: _Store(positie))
    monkeypatch.setattr(mod, "Engine", NepEngine)
    # doe alsof het net na middernacht is: de dag van de instap is voorbij
    monkeypatch.setattr(mod, "now", lambda: __import__("datetime").datetime(
        2026, 9, 21, 0, 5, tzinfo=__import__("datetime").timezone.utc))
    assert mod.main() == 0
    assert gesloten, "de positie is niet gecontroleerd op verkoop"
    assert gesloten[0][1] == "TIME_DAG", gesloten


def test_zonder_meldkanaal_wordt_er_niet_gehandeld(monkeypatch):
    """De veiligheid leunt op meldingen: staat dat kanaal niet, dan hoort de bot
    niets te doen in plaats van blind door te draaien (Fable-controle 20-9)."""
    env = _alles_gezet()
    for naam in ("RESEND_API_KEY", "RESEND_FROM", "ALERT_EMAIL"):
        env.pop(naam)
    mod = _uitvoerder(monkeypatch, **env, GATE_V1_LIVE="true", GATE_V1_CONFIRM="LIVE_V1")
    monkeypatch.setattr(mod.psycopg2, "connect",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("mag niet handelen zonder meldkanaal")))
    assert mod.main() == 0
