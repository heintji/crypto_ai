"""Het meldkanaal zelf (Astra + Fable, 20-9).

"GATE ALERT" printen in een cron-log is geen melding. Deze tests leggen vast
dat er echt verstuurd wordt, dat het nooit stil misgaat, en dat de aanroeper
kan zien of het gelukt is.
"""
import trading.gate_alert as ga


def _omgeving(monkeypatch, compleet=True):
    for naam, waarde in (("RESEND_API_KEY", "sleutel"), ("RESEND_FROM", "bot@koa-ai.nl"),
                         ("ALERT_EMAIL", "hein@example.com")):
        if compleet:
            monkeypatch.setenv(naam, waarde)
        else:
            monkeypatch.delenv(naam, raising=False)


def test_een_melding_wordt_echt_verstuurd(monkeypatch):
    _omgeving(monkeypatch)
    verstuurd = []

    class Antwoord:
        status_code = 200

    monkeypatch.setattr(ga.requests, "post", lambda *a, **kw: verstuurd.append(kw) or Antwoord())
    assert ga.stuur("positie onbeschermd") is True
    assert verstuurd, "er is niets verstuurd"
    body = verstuurd[0]["json"]
    assert body["to"] == ["hein@example.com"]
    assert "onbeschermd" in body["text"]


def test_zonder_instellingen_meldt_hij_dat_hardop(monkeypatch, capsys):
    _omgeving(monkeypatch, compleet=False)
    monkeypatch.setattr(ga.requests, "post",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("mag niet versturen")))
    assert ga.stuur("test") is False
    assert "KAN NIET MELDEN" in capsys.readouterr().out


def test_een_geweigerde_mail_geldt_niet_als_verstuurd(monkeypatch):
    _omgeving(monkeypatch)

    class Antwoord:
        status_code = 422

    monkeypatch.setattr(ga.requests, "post", lambda *a, **kw: Antwoord())
    assert ga.stuur("test") is False


def test_een_kapot_netwerk_laat_de_bot_niet_omvallen(monkeypatch):
    _omgeving(monkeypatch)
    monkeypatch.setattr(ga.requests, "post",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("netwerk weg")))
    assert ga.stuur("test") is False          # geen uitzondering naar buiten


def test_de_engine_krijgt_een_alert_die_ook_echt_mailt(monkeypatch):
    """maak_alert() print én verstuurt; eerder gebeurde alleen het eerste."""
    _omgeving(monkeypatch)
    verstuurd = []

    class Antwoord:
        status_code = 200

    monkeypatch.setattr(ga.requests, "post", lambda *a, **kw: verstuurd.append(kw) or Antwoord())
    alert = ga.maak_alert()
    alert("stop ontbreekt")
    assert verstuurd, "maak_alert() verstuurde niets"
