"""De noodverkoop van begin tot eind (Astra + Fable, 20-9).

De eerdere test verving sell() door een functie die alleen noteerde dat hij
geroepen was. Dat bewijst niet dat er verkocht wordt en de positie sluit. Deze
tests leggen het hele pad af, inclusief gedeeltelijke verkoop, een geweigerde
verkoop en een herstart midden in de verkoop.
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trading.gate_api import GateRejected, GateUnknownOutcome
from trading.gate_live_engine import Engine, Limits

UTC = timezone.utc


def metadata():
    return {"trade_status": "tradable", "min_quote_amount": "3", "min_base_amount": "0.001",
            "amount_precision": 6, "precision": 4}


class Store:
    def __init__(self):
        self.position = None
        self.paused = None
        self.geschiedenis = []

    def require_lock(self):
        pass

    def account_state(self):
        return {"paused": bool(self.paused), "reason": self.paused}

    def positions(self):
        return [self.position] if self.position else []

    def create(self, pair, day, data):
        self.position = {"id": "pos1", "pair": pair, "day": day, "state": "INTENT", "data": dict(data)}
        return self.position

    def update(self, position, state, **data):
        position["state"] = state
        position["data"].update(data)
        self.geschiedenis.append(state)

    def pause(self, reason, position_id=None):
        self.paused = reason

    def event(self, *a, **kw):
        pass


class VerkoopAPI:
    """Nep-Gate die echte orderrijen teruggeeft, zodat filled_order echt werkt."""

    def __init__(self, verkoop_fill=None, weiger=None, saldo="1"):
        self.verkoop_fill = verkoop_fill
        self.weiger = weiger
        self.saldo = saldo
        self.verkopen = []
        self.order = None

    def accounts(self):
        return [{"currency": "USDT", "available": "100"},
                {"currency": "TEST", "available": self.saldo}]

    def sell_market(self, pair, amount, tag):
        if self.weiger:
            raise self.weiger
        self.verkopen.append((pair, amount, tag))
        basis, totaal = self.verkoop_fill or ("1", "97")
        self.order = {"id": "sell-1", "currency_pair": pair, "side": "sell", "status": "closed",
                      "filled_amount": basis, "filled_total": totaal, "fee": "0"}
        return {"id": "sell-1"}

    def get_order(self, pair, order_id):
        return self.order

    def list_orders(self, *a, **kw):
        return [self.order] if self.order else []


def _opzet(api, store=None):
    store = store or Store()
    engine = Engine(api, store, Limits(order_quote=Decimal("10"), capital_quote=Decimal("10"),
                                       daily_loss_quote=Decimal("100"), allow_entries=True),
                    clock=lambda: datetime(2026, 9, 21, 0, 5, tzinfo=UTC),
                    alert=lambda m: None)
    positie = store.create("TEST_USDT", "2026-09-20", {"metadata": metadata()})
    store.update(positie, "UNPROTECTED", remaining="1", bought_base="1", cost="100",
                 proceeds="0", entry_price="100")
    return store, engine, positie


def test_een_volledige_verkoop_sluit_de_positie_echt():
    api = VerkoopAPI(verkoop_fill=("1", "97"))
    store, engine, positie = _opzet(api)
    engine.sell(positie)
    assert api.verkopen, "er is niets verkocht"
    assert positie["state"] == "CLOSED", positie["state"]
    assert Decimal(str(positie["data"]["pnl_usdt"])) == Decimal("-3")
    assert store.paused is None, store.paused


def test_een_gedeeltelijke_verkoop_sluit_de_positie_niet():
    """Halve vulling: de rest moet blijven staan en gemeld worden, niet stil
    als gesloten geboekt."""
    api = VerkoopAPI(verkoop_fill=("0.4", "39"))
    store, engine, positie = _opzet(api)
    engine.sell(positie)
    assert positie["state"] != "CLOSED"
    assert Decimal(str(positie["data"]["remaining"])) == Decimal("0.6")
    assert store.paused, "een half verkochte positie moet gemeld worden"


def test_een_geweigerde_verkoop_laat_de_voorraad_staan_en_meldt():
    api = VerkoopAPI(weiger=GateRejected("INVALID_PARAM"))
    store, engine, positie = _opzet(api)
    engine.sell(positie)
    assert positie["state"] == "EXIT_REJECTED"
    assert not api.verkopen
    assert store.paused and "manual" in store.paused.lower()


def test_na_een_herstart_wordt_de_verkoop_afgemaakt_zonder_tweede_order():
    """De verkoop was verstuurd, daarna viel het proces om. Bij de volgende run
    moet de bot de bestaande order afhandelen, niet opnieuw verkopen."""
    api = VerkoopAPI(verkoop_fill=("1", "97"))
    store, engine, positie = _opzet(api)
    # het proces valt om direct NA het versturen, vóór het afhandelen
    echte_settle, engine.settle_sell = engine.settle_sell, lambda p: None
    engine.sell(positie)
    assert len(api.verkopen) == 1 and positie["state"] == "SELL_SUBMITTED"
    # volgende run: de order bestaat al en moet afgehandeld worden
    engine.settle_sell = echte_settle
    engine.reconcile(positie)
    assert len(api.verkopen) == 1, "tweede verkooporder na herstart"
    assert positie["state"] == "CLOSED", positie["state"]


def test_een_onzekere_verkoop_wordt_nooit_blind_herhaald():
    api = VerkoopAPI(weiger=GateUnknownOutcome("TRANSPORT_ERROR"))
    store, engine, positie = _opzet(api)
    engine.sell(positie)
    assert positie["state"] == "UNKNOWN_SELL"
    assert not api.verkopen
    assert store.paused
