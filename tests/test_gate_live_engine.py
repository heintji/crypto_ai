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
