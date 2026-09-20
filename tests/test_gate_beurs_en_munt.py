"""Gate EU en de tegenmunt (gemeten op api.gateeu.com, 20-9).

De bot ging uit van de internationale beurs en van USDT als tegenmunt. Op Heins
account klopt geen van beide: `api.gateio.ws` weigert zijn sleutel met
INVALID_KEY, `api.gate.com` bestaat niet eens, en `XRP_USDT` bestaat niet op
Gate EU — daar handelt XRP in USDC en EUR.
"""
from decimal import Decimal

import pytest

from trading.gate_api import GateAPI
from trading.gate_live_engine import Limits, filled_order, munten


def test_de_europese_beurs_is_toegestaan_en_de_dode_host_niet():
    GateAPI("k", "s", base_url="https://api.gateeu.com")      # mag
    GateAPI("k", "s", base_url="https://api.gateio.ws")       # mag
    for dood in ("https://api.gate.com", "https://api.example.com"):
        with pytest.raises(ValueError):
            GateAPI("k", "s", base_url=dood)


def test_munten_komen_uit_gate_zelf_en_niet_uit_de_naam():
    # Gate levert base/quote mee; die zijn leidend.
    assert munten("XRP_USDC", {"base": "XRP", "quote": "USDC"}) == ("XRP", "USDC")
    # Zonder metadata splitsen we op de laatste underscore — ook bij EUR.
    assert munten("BTC_EUR") == ("BTC", "EUR")
    assert munten("1000SATS_USDC") == ("1000SATS", "USDC")
    with pytest.raises(ValueError):
        munten("XRP")


def test_kosten_worden_in_de_juiste_tegenmunt_verrekend():
    """Met USDT hardgecodeerd werd een USDC-kostenpost als 'onbekend' gezien en
    dus niet verrekend."""
    order = {"currency_pair": "XRP_USDC", "side": "buy", "status": "closed",
             "filled_amount": "10", "filled_total": "13", "fee": "0.013",
             "fee_currency": "USDC"}
    fill = filled_order(order, "XRP_USDC", "buy", {"base": "XRP", "quote": "USDC"})
    assert fill["quote"] == Decimal("13.013")      # kosten meegerekend
    assert fill["fee_note"] == ""                  # niets onzekers


class _Store:
    def __init__(self):
        self.paused = None
        self.position = None

    def require_lock(self):
        pass

    def account_state(self):
        return {"paused": False, "reason": None}

    def daily_pnl(self, _):
        return Decimal("0")

    def positions(self):
        return []

    def pause(self, reason, position_id=None):
        self.paused = reason

    def event(self, *a, **kw):
        pass

    def create(self, *a, **kw):
        raise AssertionError("er mag geen positie ontstaan")


class _API:
    def accounts(self):
        return [{"currency": "USDC", "available": "100"}, {"currency": "XRP", "available": "0"}]


def test_een_paar_in_een_andere_tegenmunt_wordt_niet_gekocht():
    """Anders koop je met een saldo dat de bot niet meet, en reken je winst in
    twee munten door elkaar."""
    from datetime import datetime, timezone
    from trading.gate_v1 import EntrySignal
    store = _Store()
    from trading.gate_live_engine import Engine
    engine = Engine(_API(), store,
                    Limits(order_quote=Decimal("10"), capital_quote=Decimal("10"),
                           daily_loss_quote=Decimal("1"), allow_entries=True,
                           quote_currency="USDC"),
                    clock=lambda: datetime(2026, 9, 20, 12, 0, 1, tzinfo=timezone.utc),
                    alert=lambda m: None)
    signaal = EntrySignal("XRP_EUR", datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
                          1.3, 1.3, 1.29, 1.26)
    uit = engine.open(signaal, {"trade_status": "tradable", "base": "XRP", "quote": "EUR",
                                "min_quote_amount": "1", "min_base_amount": "0.001",
                                "amount_precision": 6, "precision": 4})
    assert uit is None
    assert store.paused and "EUR" in store.paused


def test_de_api_accepteert_de_paren_die_gate_eu_echt_heeft():
    """De paarvalidatie stond hardgecodeerd op _USDT. Daardoor viel élke
    aanroep met een EU-paar al om vóór het netwerk: geen koers, geen order,
    geen stop (Fable-controle 20-9)."""
    from trading.gate_api import _pair
    for geldig in ("XRP_USDC", "BTC_EUR", "1000SATS_USDC", "BTC_USDT"):
        assert _pair(geldig) == geldig
    for ongeldig in ("XRP", "xrp_usdc", "XRP_USDC_EXTRA", "XRP-USDC", ""):
        with pytest.raises(ValueError):
            _pair(ongeldig)


def test_de_api_kan_een_eu_paar_opvragen_zonder_netwerk():
    """Niet alleen de regex: de hele aanroepketen moet een USDC-paar aankunnen."""
    class NepSessie:
        def __init__(self):
            self.laatste = None

        def mount(self, *a, **kw):
            pass

        def request(self, method, url, **kw):
            self.laatste = url

            class Antwoord:
                status_code = 200

                @staticmethod
                def json():
                    return {"id": "XRP_USDC", "base": "XRP", "quote": "USDC",
                            "trade_status": "tradable"}
            return Antwoord()

    sessie = NepSessie()
    api = GateAPI("k", "s", session=sessie, base_url="https://api.gateeu.com")
    uit = api.pair("XRP_USDC")
    assert uit["quote"] == "USDC"
    assert "XRP_USDC" in sessie.laatste and "api.gateeu.com" in sessie.laatste
