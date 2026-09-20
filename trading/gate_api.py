"""Small Gate v4 spot transport. No environment loading or implicit retries.

Contract: https://www.gate.com/docs/developers/apiv4/en/spot/
An uncertain mutation MUST be reconciled by the caller, never resubmitted.
Custom order text is a correlation ID, NOT an exchange idempotency guarantee.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from urllib.parse import urlencode

import requests


class GateError(RuntimeError):
    """Safe error: never includes credentials, raw response or request headers."""

    def __init__(self, label, status=None):
        self.label = label if re.fullmatch(r"[A-Z0-9_]{1,80}", str(label)) else "GATE_ERROR"
        self.status = status
        super().__init__(f"Gate {self.label}" + (f" (HTTP {status})" if status else ""))


class GateRejected(GateError):
    """A definite local refusal or documented 4xx rejection."""


class GateUnknownOutcome(GateError):
    """Mutation may have executed. Reconcile before any further mutation."""


class GateUnavailable(GateError):
    """A read did not produce a trustworthy response."""


def decimal_value(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Invalid decimal") from None
    if not result.is_finite() or result < 0:
        raise ValueError("Expected finite nonnegative decimal")
    return result


def decimal_string(value):
    return format(decimal_value(value), "f")


def floor_amount(value, precision):
    if isinstance(precision, bool) or not isinstance(precision, int) or not 0 <= precision <= 18:
        raise ValueError("Invalid precision")
    return decimal_value(value).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)


def validate_minimums(pair_info, *, base_amount=None, quote_amount=None, side="buy"):
    """Check only known dimensions; caller supplies conservative quote valuation."""
    if side not in ("buy", "sell"):
        raise ValueError("Invalid side")
    if pair_info.get("trade_status") not in ("tradable", "buyable" if side == "buy" else "sellable"):
        raise ValueError("Pair not tradable for side")
    for currency, amount in (("base", base_amount), ("quote", quote_amount)):
        if amount is None:
            continue
        number = decimal_value(amount)
        if number <= 0:
            raise ValueError("Order amount must be positive")
        for prefix, bad in (("min", lambda x: number < x), ("max", lambda x: number > x)):
            bound = pair_info.get(f"{prefix}_{currency}_amount")
            if bound not in (None, "", "0", 0) and bad(decimal_value(bound)):
                raise ValueError(f"Order outside {currency} {prefix}imum")


def _pair(pair):
    if not isinstance(pair, str) or not re.fullmatch(r"[A-Z0-9]+_USDT", pair):
        raise ValueError("Only USDT spot pairs supported")
    return pair


def _identifier(value):
    value = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
        raise ValueError("Invalid order identifier")
    return value


def _client_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"t-[A-Za-z0-9_.-]{1,28}", value):
        raise ValueError("Invalid client order ID")
    return value


def _positive(value):
    value = decimal_value(value)
    if value <= 0:
        raise ValueError("Amount/price must be positive")
    return format(value, "f")


class GateAPI:
    def __init__(self, api_key="", api_secret="", *, session=None, clock=time.time,
                 base_url="https://api.gateio.ws", timeout=(3.05, 10), trading_enabled=False):
        if base_url not in ("https://api.gateio.ws", "https://api.gate.com"):
            raise ValueError("Unapproved Gate API origin")
        if len(timeout) != 2 or any(not 0 < float(x) <= 30 for x in timeout):
            raise ValueError("Timeouts must be bounded")
        self._key, self._secret = api_key, api_secret
        self._session = session if session is not None else requests.Session()
        # An injected requests.Session must not secretly retry mutations.
        if isinstance(self._session, requests.Session):
            self._session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        self._clock, self._base, self._timeout = clock, base_url, timeout
        self.trading_enabled = trading_enabled is True

    def _request(self, method, path, *, params=None, body=None, private=True):
        mutation = method != "GET"
        if mutation and not self.trading_enabled:
            raise GateRejected("TRADING_DISABLED")
        if private and (not self._key or not self._secret):
            raise GateRejected("MISSING_CREDENTIALS")
        query = urlencode([(k, str(v)) for k, v in (params or {}).items() if v is not None])
        payload = "" if body is None else json.dumps(body, separators=(",", ":"), allow_nan=False)
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        full_path = "/api/v4" + path
        if private:
            stamp = str(int(self._clock()))
            message = "\n".join((method, full_path, query, hashlib.sha512(payload.encode()).hexdigest(), stamp))
            signature = hmac.new(self._secret.encode(), message.encode(), hashlib.sha512).hexdigest()
            headers.update(KEY=self._key, Timestamp=stamp, SIGN=signature)
        try:
            response = self._session.request(method, self._base + full_path + ("?" + query if query else ""),
                                             data=payload or None, headers=headers, timeout=self._timeout,
                                             allow_redirects=False)
        except requests.RequestException:
            raise (GateUnknownOutcome if mutation else GateUnavailable)("TRANSPORT_ERROR") from None
        status = response.status_code
        try:
            result = response.json()
        except (ValueError, TypeError):
            raise (GateUnknownOutcome if mutation else GateUnavailable)("INVALID_RESPONSE", status) from None
        if 200 <= status < 300:
            if not isinstance(result, (dict, list)):
                raise (GateUnknownOutcome if mutation else GateUnavailable)("INVALID_RESPONSE", status)
            return result
        label = result.get("label", "HTTP_ERROR") if isinstance(result, dict) else "HTTP_ERROR"
        # 408 / 5xx and unexpected redirect responses cannot prove no execution.
        if 400 <= status < 500 and status != 408:
            raise GateRejected(label, status)
        raise (GateUnknownOutcome if mutation else GateUnavailable)(label, status)

    def pair(self, pair):
        return self._request("GET", f"/spot/currency_pairs/{_pair(pair)}", private=False)

    currency_pair = pair

    def ticker(self, pair):
        rows = self._request("GET", "/spot/tickers", params={"currency_pair": _pair(pair)}, private=False)
        if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("currency_pair") != pair:
            raise GateUnavailable("INVALID_TICKER")
        return rows[0]

    def candles(self, pair, interval="1h", limit=100, **bounds):
        if interval not in ("1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d", "7d"):
            raise ValueError("Invalid candle interval")
        if not 1 <= limit <= 1000 or set(bounds) - {"from", "to"}:
            raise ValueError("Invalid candle request")
        params = {"currency_pair": _pair(pair), "interval": interval}
        params.update(bounds if bounds else {"limit": limit})
        return self._request("GET", "/spot/candlesticks", params=params, private=False)

    def accounts(self):
        return self._request("GET", "/spot/accounts")

    def _market(self, pair, amount, client_id, side):
        return self._request("POST", "/spot/orders", body={
            "currency_pair": _pair(pair), "account": "spot", "type": "market", "side": side,
            "amount": _positive(amount), "time_in_force": "ioc", "text": _client_id(client_id),
        })

    def buy_market(self, pair, quote_amount, client_id):
        return self._market(pair, quote_amount, client_id, "buy")

    def sell_market(self, pair, base_amount, client_id):
        return self._market(pair, base_amount, client_id, "sell")

    def order(self, pair, id_or_text):
        """Text lookup covers pending orders ONLY. Reconcile finished history too."""
        return self._request("GET", f"/spot/orders/{_identifier(id_or_text)}",
                             params={"currency_pair": _pair(pair), "account": "spot"})

    get_order = order

    def list_orders(self, pair, status="open", page=1, limit=100):
        if status not in ("open", "finished") or page < 1 or not 1 <= limit <= 100:
            raise ValueError("Invalid order listing")
        return self._request("GET", "/spot/orders", params={"currency_pair": _pair(pair),
                             "status": status, "page": page, "limit": limit, "account": "spot"})

    def open_orders(self, pair):
        return self.list_orders(pair)

    def fills(self, pair, order_id=None, page=1, limit=100):
        if page < 1 or not 1 <= limit <= 100:
            raise ValueError("Invalid fill listing")
        return self._request("GET", "/spot/my_trades", params={"currency_pair": _pair(pair),
                             "order_id": _identifier(order_id) if order_id is not None else None,
                             "page": page, "limit": limit, "account": "spot"})

    trades = fills

    def create_stop(self, pair, base_amount, trigger_price, client_id, expiration=172800):
        """Native stop has NO documented custom ID. Save returned id immediately.

        client_id is validated for caller correlation but not sent: price_orders
        put.text is documented as order source ('api'), not custom order text.
        A timeout is ambiguous and MUST NOT cause a blind retry or second sell.
        """
        _client_id(client_id)
        if not isinstance(expiration, int) or not 60 <= expiration <= 172800:
            raise ValueError("Invalid stop expiration")
        return self._request("POST", "/spot/price_orders", body={
            "market": _pair(pair),
            "trigger": {"price": _positive(trigger_price), "rule": "<=", "expiration": expiration},
            "put": {"type": "market", "side": "sell", "price": "0", "amount": _positive(base_amount),
                    "account": "normal", "time_in_force": "ioc", "text": "api"},
        })

    def stop(self, order_id):
        return self._request("GET", f"/spot/price_orders/{_identifier(order_id)}")

    get_stop = stop

    def cancel_stop(self, order_id):
        return self._request("DELETE", f"/spot/price_orders/{_identifier(order_id)}")

    def cancel_order(self, pair, order_id):
        return self._request("DELETE", f"/spot/orders/{_identifier(order_id)}",
                             params={"currency_pair": _pair(pair), "account": "spot"})

    def list_stops(self, status="open", pair=None, page=1, limit=100):
        if status not in ("open", "finished") or page < 1 or not 1 <= limit <= 100:
            raise ValueError("Invalid stop listing")
        return self._request("GET", "/spot/price_orders", params={"status": status,
                             "market": _pair(pair) if pair else None, "page": page, "limit": limit})
