from datetime import datetime, timezone
from decimal import Decimal

from trading.gate_api import GateUnknownOutcome
from trading.gate_live_engine import Engine, Limits, filled_order
from trading.gate_v1 import EntrySignal


UTC = timezone.utc


class Store:
    def __init__(self):
        self.locked = True
        self.position = None
        self.paused = None

    def require_lock(self):
        assert self.locked

    def account_state(self):
        return {"paused": bool(self.paused), "reason": self.paused}

    def daily_pnl(self, _now):
        return Decimal("0")

    def positions(self):
        return [self.position] if self.position and self.position["state"] not in ("CLOSED", "REJECTED") else []

    def create(self, pair, day, data):
        self.position = {"id": "abc", "pair": pair, "day": day, "state": "INTENT", "data": data}
        return self.position

    def update(self, position, state, **data):
        position["state"] = state
        position["data"].update(data)

    def pause(self, reason, position_id=None):
        self.paused = reason

    def event(self, *args, **kwargs):
        pass


class API:
    def __init__(self, exception=None):
        self.exception = exception

    def accounts(self):
        return [{"currency": "USDT", "available": "100"}, {"currency": "TEST", "available": "0"}]

    def buy_market(self, *args):
        if self.exception:
            raise self.exception
        return {"id": "1"}


def signal():
    at = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    return EntrySignal("TEST_USDT", at, 100, 100, 99, 97)


def metadata():
    return {"trade_status": "tradable", "min_quote_amount": "3", "min_base_amount": "0.001",
            "amount_precision": 6, "precision": 4}


def test_unknown_buy_pauses_without_retrying():
    store = Store()
    api = API(GateUnknownOutcome("TRANSPORT_ERROR"))
    engine = Engine(api, store, Limits(order_quote=Decimal("10"), capital_quote=Decimal("10"),
                                       daily_loss_quote=Decimal("1"), allow_entries=True),
                    clock=lambda: datetime(2026, 9, 20, 12, 0, 1, tzinfo=UTC))
    position = engine.open(signal(), metadata())
    assert position["state"] == "UNKNOWN_BUY"
    assert "unknown" in store.paused.lower()


def test_filled_order_requires_terminal_actual_fill():
    pending = {"currency_pair": "TEST_USDT", "side": "buy", "status": "open",
               "filled_amount": "1", "filled_total": "100", "fee": "0"}
    assert filled_order(pending, "TEST_USDT", "buy") is None
    closed = dict(pending, status="closed")
    assert filled_order(closed, "TEST_USDT", "buy")["base"] == Decimal("1")


def test_external_fee_currency_is_not_silently_valued():
    """Kosten in een vreemde munt worden NIET meegerekend, maar ze mogen de
    afhandeling ook niet stoppen: dan blijft een gekochte positie onbeschermd
    staan. Vandaar een markering in plaats van een fout (Astra + Fable, 20-9)."""
    order = {"currency_pair": "TEST_USDT", "side": "sell", "status": "closed",
             "filled_amount": "1", "filled_total": "100", "fee": "1", "fee_currency": "KOIN"}
    fill = filled_order(order, "TEST_USDT", "sell")
    assert fill["base"] == Decimal("1") and fill["quote"] == Decimal("100")
    assert "KOIN" in fill["fee_note"]


def test_gt_korting_stopt_de_afhandeling_niet():
    """Met GT-korting aan gooide filled_order een ValueError, VOOR het plaatsen
    van de stop. Resultaat: gekocht, onbeschermd, en elke run dezelfde fout."""
    order = {"currency_pair": "TEST_USDT", "side": "buy", "status": "closed",
             "filled_amount": "1", "filled_total": "100", "fee": "0", "gt_fee": "0.01"}
    fill = filled_order(order, "TEST_USDT", "buy")
    assert fill["base"] == Decimal("1") and fill["quote"] == Decimal("100")
    assert "GT" in fill["fee_note"]


def test_een_koop_met_gt_korting_wordt_alsnog_beschermd():
    """Het hele pad: gevulde koop met GT-kosten moet eindigen in een geplaatste
    stop, met een melding over de onzekere kostprijs."""
    store, api = Store(), StopAPI()
    api.order = {"id": "1", "currency_pair": "TEST_USDT", "side": "buy", "status": "closed",
                 "filled_amount": "1", "filled_total": "100", "fee": "0", "gt_fee": "0.01"}
    meldingen = []
    engine = Engine(api, store, Limits(order_quote=Decimal("10"), capital_quote=Decimal("10"),
                                       daily_loss_quote=Decimal("1"), allow_entries=True),
                    clock=lambda: datetime(2026, 9, 20, 12, 0, 1, tzinfo=UTC),
                    alert=meldingen.append)
    positie = store.create("TEST_USDT", "2026-09-20", {"metadata": metadata()})
    store.update(positie, "BUY_SUBMITTED", buy_id="1", buy_tag="t-x")
    engine.reconcile(positie)
    assert positie["state"] == "PROTECTED", positie["state"]
    assert api.stops, "geen stop geplaatst bij een koop met GT-kosten"
    assert any("kostprijs" in m.lower() for m in meldingen), meldingen


# ── beschermen: een gevulde koop mag nooit zonder stop blijven staan ───────
# Aanleiding (Fable-controle 20-9): protect() gaf "api" mee als correlatie-id.
# create_stop valideert dat met hetzelfde patroon als elke order-id (t-...) en
# gooide dus een ValueError VOOR er een verzoek uitging. Resultaat: gekocht,
# niet beschermd, en elke volgende run zette de bot alleen maar op pauze.

class StopAPI(API):
    """Legt vast waarmee create_stop geroepen wordt, met de ECHTE validatie."""

    def __init__(self, weiger=None):
        super().__init__()
        self.weiger = weiger
        self.stops = []
        self.order = None

    def get_order(self, pair, order_id):
        return self.order

    def list_orders(self, *a, **kw):
        return [self.order] if self.order else []

    def accounts(self):
        return [{"currency": "USDT", "available": "100"}, {"currency": "TEST", "available": "1"}]

    def create_stop(self, pair, base_amount, trigger, client_id, expiration=172800):
        from trading.gate_api import _client_id
        _client_id(client_id)          # dezelfde controle als de echte API
        if self.weiger:
            raise self.weiger
        self.stops.append((pair, base_amount, trigger, client_id))
        return {"id": "stop-1"}

    def get_stop(self, stop_id):
        # protect() gelooft de aanmaak-respons bewust niet en vraagt de stand op.
        return {"id": stop_id, "status": "open"}


def _positie_na_koop(store, engine):
    positie = store.create("TEST_USDT", "2026-09-20", {"metadata": metadata()})
    store.update(positie, "BOUGHT", remaining="1", bought_base="1", cost="100",
                 entry_price="100")
    return positie


def _engine(api, store):
    return Engine(api, store, Limits(order_quote=Decimal("10"), capital_quote=Decimal("10"),
                                     daily_loss_quote=Decimal("1"), allow_entries=True),
                  clock=lambda: datetime(2026, 9, 20, 12, 0, 1, tzinfo=UTC))


def test_een_gekochte_positie_krijgt_echt_een_stop():
    store, api = Store(), StopAPI()
    engine = _engine(api, store)
    positie = _positie_na_koop(store, engine)
    engine.protect(positie)
    assert api.stops, "er is geen stop geplaatst"
    assert positie["state"] == "PROTECTED", positie["state"]
    assert store.paused is None, store.paused
    # De trigger ligt onder de instap, niet erboven.
    assert Decimal(str(api.stops[0][2])) < Decimal("100")


def test_de_stop_gebruikt_een_geldige_correlatie_id():
    """Met 'api' gooide create_stop een ValueError en bleef de positie
    onbeschermd staan — zonder dat een test dat zag."""
    store, api = Store(), StopAPI()
    engine = _engine(api, store)
    engine.protect(_positie_na_koop(store, engine))
    _, _, _, client_id = api.stops[0]
    import re
    assert re.fullmatch(r"t-[A-Za-z0-9_.-]{1,28}", client_id), client_id


def test_weigert_gate_de_stop_dan_gaat_de_positie_dicht():
    """Geen positie zonder bescherming: wordt de stop geweigerd, dan moet de
    bot verkopen in plaats van hem te laten staan."""
    from trading.gate_api import GateRejected
    store, api = Store(), StopAPI(weiger=GateRejected("INVALID_PARAM"))
    verkocht = []
    engine = _engine(api, store)
    engine.sell = lambda positie: verkocht.append(positie)
    positie = _positie_na_koop(store, engine)
    engine.protect(positie)
    assert positie["state"] == "UNPROTECTED", positie["state"]
    assert verkocht, "een onbeschermde positie moet gesloten worden"
    assert store.paused


# ── herstel na een onzekere stop ──────────────────────────────────────────
# Eerder stond hier alleen een pauze: de positie bleef onbeschermd staan tot
# iemand het toevallig zag (Astra + Fable, 20-9).

class HerstelAPI(StopAPI):
    def __init__(self, open_stops=None, saldo="1", lijst_fout=None):
        super().__init__()
        self.open_stops = open_stops if open_stops is not None else []
        self.saldo = saldo
        self.lijst_fout = lijst_fout

    def accounts(self):
        return [{"currency": "USDT", "available": "100"},
                {"currency": "TEST", "available": self.saldo}]

    def list_stops(self, status="open", pair=None, **kw):
        if self.lijst_fout:
            raise self.lijst_fout
        return self.open_stops

    def get_stop(self, stop_id):
        return {"id": stop_id, "status": "open"}


def _onzekere_positie(store):
    positie = store.create("TEST_USDT", "2026-09-20", {"metadata": metadata()})
    store.update(positie, "UNKNOWN_STOP", remaining="1", bought_base="1", cost="100",
                 proceeds="0", entry_price="100", stop_trigger="97", protected_amount="1")
    return positie


def test_een_teruggevonden_stop_maakt_de_positie_weer_beschermd():
    store = Store()
    api = HerstelAPI(open_stops=[{"id": "stop-9", "market": "TEST_USDT",
                                  "trigger": {"price": "97"}}])
    engine = _engine(api, store)
    positie = _onzekere_positie(store)
    engine.reconcile(positie)
    assert positie["state"] == "PROTECTED", positie["state"]
    assert positie["data"]["stop_id"] == "stop-9"


def test_geen_stop_maar_wel_de_munt_dan_wordt_hij_opnieuw_geplaatst():
    """Dit is herstel, geen blinde tweede order: we hebben eerst vastgesteld
    dat er géén stop openstaat en dat de positie er nog is."""
    store = Store()
    api = HerstelAPI(open_stops=[], saldo="1")
    engine = _engine(api, store)
    positie = _onzekere_positie(store)
    engine.reconcile(positie)
    assert api.stops, "er is geen nieuwe stop geplaatst"
    assert positie["state"] == "PROTECTED", positie["state"]


def test_geen_stop_en_geen_munt_dan_alleen_melden():
    """De stop is waarschijnlijk al gevuurd. Nooit een nieuwe stop of verkoop
    op goed geluk."""
    store = Store()
    api = HerstelAPI(open_stops=[], saldo="0")
    engine = _engine(api, store)
    positie = _onzekere_positie(store)
    engine.reconcile(positie)
    assert not api.stops
    assert store.paused and "handmatig" in store.paused.lower()


def test_kan_de_lijst_niet_opgevraagd_worden_dan_pauzeren_we():
    from trading.gate_api import GateUnknownOutcome
    store = Store()
    api = HerstelAPI(lijst_fout=GateUnknownOutcome("TRANSPORT_ERROR"))
    engine = _engine(api, store)
    positie = _onzekere_positie(store)
    engine.reconcile(positie)
    assert not api.stops
    assert store.paused
