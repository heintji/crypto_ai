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
    engine = Engine(api, store, Limits(order_usdt=Decimal("10"), capital_usdt=Decimal("10"),
                                       daily_loss_usdt=Decimal("1"), allow_entries=True),
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
    order = {"currency_pair": "TEST_USDT", "side": "sell", "status": "closed",
             "filled_amount": "1", "filled_total": "100", "fee": "1", "fee_currency": "KOIN"}
    try:
        filled_order(order, "TEST_USDT", "sell")
    except ValueError as exc:
        assert "fee currency" in str(exc)
    else:
        raise AssertionError("external fee must stop reconciliation")


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
    return Engine(api, store, Limits(order_usdt=Decimal("10"), capital_usdt=Decimal("10"),
                                     daily_loss_usdt=Decimal("1"), allow_entries=True),
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
