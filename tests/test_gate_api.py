from decimal import Decimal

import pytest
import requests

from trading.gate_api import (
    GateAPI, GateRejected, GateUnknownOutcome, floor_amount, validate_minimums,
)


class Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class Session:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.response


def test_disabled_mutation_has_no_network_call():
    session = Session(Response(payload={"id": "1"}))
    api = GateAPI("k", "s", session=session, trading_enabled=False)
    with pytest.raises(GateRejected, match="TRADING_DISABLED"):
        api.buy_market("BTC_USDT", "10", "t-abc")
    assert not session.calls


def test_mutation_timeout_is_unknown_and_never_retried():
    session = Session(error=requests.Timeout())
    api = GateAPI("k", "s", session=session, trading_enabled=True)
    with pytest.raises(GateUnknownOutcome, match="TRANSPORT_ERROR"):
        api.sell_market("BTC_USDT", "0.01", "t-abc")
    assert len(session.calls) == 1


def test_finished_order_search_is_separate_from_pending_text_lookup():
    session = Session(Response(payload=[{"id": "1", "currency_pair": "BTC_USDT"}]))
    api = GateAPI("k", "s", session=session)
    rows = api.list_orders("BTC_USDT", status="finished")
    assert rows[0]["id"] == "1"
    assert "status=finished" in session.calls[0][0][1]


def test_signature_headers_and_json_body_are_sent():
    session = Session(Response(payload={"id": "1"}))
    api = GateAPI("k", "s", session=session, clock=lambda: 1700000000,
                  trading_enabled=True)
    api.buy_market("BTC_USDT", Decimal("10.5"), "t-abc")
    args, kwargs = session.calls[0]
    assert kwargs["headers"]["KEY"] == "k"
    assert kwargs["headers"]["Timestamp"] == "1700000000"
    assert '"side":"buy"' in kwargs["data"]
    assert kwargs["data"].count("10.5") == 1


def test_stop_has_no_false_client_id_claim_and_bounded_expiration():
    session = Session(Response(payload={"id": "stop-1", "status": "open"}))
    api = GateAPI("k", "s", session=session, trading_enabled=True)
    result = api.create_stop("BTC_USDT", "0.01", "95", "t-correlate", expiration=172800)
    assert result["id"] == "stop-1"
    body = session.calls[0][1]["data"]
    assert '"text":"api"' in body
    assert "t-correlate" not in body
    with pytest.raises(ValueError):
        api.create_stop("BTC_USDT", "0.01", "95", "t-correlate", expiration=172801)


def test_decimal_and_minimum_validation_fail_closed():
    assert floor_amount("1.239", 2) == Decimal("1.23")
    with pytest.raises(ValueError):
        validate_minimums({"trade_status": "tradable", "min_base_amount": "1"},
                          base_amount="0.9", side="sell")
