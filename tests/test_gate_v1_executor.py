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
                 "GATE_V1_LIVE", "GATE_V1_CONFIRM"):
        monkeypatch.delenv(naam, raising=False)
    for naam, waarde in env.items():
        monkeypatch.setenv(naam, waarde)
    mod = importlib.import_module("scripts.gate_v1_executor")
    return importlib.reload(mod)


def _alles_gezet():
    return dict(GATE_V1_PAIRS="TEST_USDT", DATABASE_URL="postgres://x",
                GATE_API_KEY="k", GATE_API_SECRET="s")


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
            def __init__(self, key, secret, trading_enabled=False):
                gemaakt.append(trading_enabled)
        monkeypatch.setattr(mod, "GateAPI", NepAPI)
        monkeypatch.setattr(mod.psycopg2, "connect", lambda *a, **kw: _NepConn())
        monkeypatch.setattr(mod, "Store", lambda conn, account: _NepStore())
        monkeypatch.setattr(mod, "Engine", lambda api, store, limits: object())
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
