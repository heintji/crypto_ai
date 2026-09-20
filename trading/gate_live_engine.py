"""Gate execution state machine. No credentials, DB connections or orders on import.

Commit intentions BEFORE exchange calls. Unknown mutation outcomes are never
retried blindly. Session lock and durable daily uniqueness are mandatory.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from trading.gate_api import GateError, GateRejected, GateUnknownOutcome


def dec(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("non-finite Gate number")
    return number


@dataclass(frozen=True)
class Limits:
    """Bedragen zijn in de TEGENMUNT van de markt, niet per se in USDT.

    Op Gate EU bestaat XRP_USDT niet; daar wordt in USDC en EUR gehandeld. Eén
    bot handelt daarom in één tegenmunt tegelijk — anders zou "kapitaal 10"
    betekenisloos worden zodra er zowel een USDC- als een EUR-paar meedoet
    (gemeten op api.gateeu.com, 20-9).
    """
    order_quote: Decimal = Decimal("10")
    capital_quote: Decimal = Decimal("10")
    daily_loss_quote: Decimal = Decimal("1")
    max_positions: int = 1
    allow_entries: bool = False
    quote_currency: str = ""

    def __post_init__(self):
        for value in (self.order_quote, self.capital_quote, self.daily_loss_quote):
            if not dec(value) > 0:
                raise ValueError("Gate limits must be positive")
        if self.order_quote > self.capital_quote or self.max_positions < 1:
            raise ValueError("invalid Gate exposure limits")
        # Geen stille standaard: welke munt je inzet is een keuze van de
        # aanroeper, niet van de bibliotheek. Een verkeerde aanname betekent dat
        # de bot het verkeerde saldo meet (Fable-controle 20-9).
        if not self.quote_currency:
            raise ValueError("quote_currency moet expliciet gezet worden")


def munten(pair, metadata=None):
    """(basis, tegenmunt) van een paar.

    Gate EU handelt grotendeels in USDC en EUR; `XRP_USDT` bestaat daar niet
    eens. Aannemen dat de tegenmunt USDT is, zou op jouw beurs betekenen dat de
    bot het verkeerde saldo controleert en de verkeerde kosten verrekent
    (gemeten op api.gateeu.com, 20-9). Waar Gate de velden meelevert gebruiken
    we die; anders splitsen we op de laatste underscore.
    """
    if metadata and metadata.get("base") and metadata.get("quote"):
        return metadata["base"], metadata["quote"]
    basis, _, tegen = pair.rpartition("_")
    if not basis:
        raise ValueError(f"onbekend paar: {pair}")
    return basis, tegen


def filled_order(order, pair, side, metadata=None):
    """Require terminal, actual fill totals; never treat requested size as a fill."""
    if order.get("currency_pair") != pair or order.get("side") != side:
        raise ValueError("order identity mismatch")
    if order.get("status") not in ("closed", "cancelled"):
        return None
    base = dec(order["filled_amount"])
    quote = dec(order["filled_total"])
    fee = dec(order.get("fee", "0"))
    if base < 0 or quote < 0 or fee < 0:
        raise ValueError("negative fill")
    fee_currency = order.get("fee_currency", "")
    base_currency, quote_currency = munten(pair, metadata)
    # Kosten in GT of punten worden van een ANDER saldo afgeschreven; de
    # verhandelde hoeveelheden kloppen dus gewoon. Vroeger gooide dit een
    # ValueError en strandde de hele afhandeling VOOR het plaatsen van de stop:
    # gekocht, onbeschermd, en elke volgende run dezelfde fout. Bescherming mag
    # nooit afhangen van een kostenpost die we niet kunnen omrekenen — we
    # markeren de kostprijs als onzeker en gaan door (Astra + Fable, 20-9).
    onzeker = ""
    if dec(order.get("gt_fee", "0")) or dec(order.get("point_fee", "0")):
        onzeker = "kosten betaald in GT of punten; kostprijs is een ondergrens"
    elif fee and fee_currency not in (base_currency, quote_currency):
        onzeker = f"kosten in {fee_currency}; niet om te rekenen, kostprijs is een ondergrens"
    verrekenbaar = fee if fee_currency in (base_currency, quote_currency) else Decimal(0)
    if side == "buy":
        uit = {"base": base - (verrekenbaar if fee_currency == base_currency else 0),
               "quote": quote + (verrekenbaar if fee_currency == quote_currency else 0)}
    else:
        uit = {"base": base + (verrekenbaar if fee_currency == base_currency else 0),
               "quote": quote - (verrekenbaar if fee_currency == quote_currency else 0)}
    uit["fee_note"] = onzeker
    return uit


class Engine:
    def __init__(self, api, store, limits, clock=None, alert=None):
        self.api, self.store, self.limits = api, store, limits
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.alert = alert or (lambda message: print("GATE ALERT: " + message, flush=True))

    def halt(self, reason, position=None):
        self.store.pause(reason, position["id"] if position else None)
        self.alert(reason)

    def order_tag(self, position, side):
        # Gate allows at most 28 bytes AFTER t-. Position UUID stays in the DB.
        attempt = position["data"].get("sell_attempt", 0) if side == "s" else 0
        return "t-" + position["id"][:22] + side + str(attempt)

    def lookup(self, position, side):
        data = position["data"]
        order_id = data.get("buy_id" if side == "buy" else "sell_id")
        if order_id:
            return self.api.get_order(position["pair"], order_id)
        tag = data.get("buy_tag" if side == "buy" else "sell_tag") or self.order_tag(position, "b" if side == "buy" else "s")
        # Gate cannot look up COMPLETED orders by custom text. Search history;
        # absence remains UNKNOWN, never permission to repeat the mutation.
        matches = []
        for status in ("open", "finished"):
            for page in range(1, 11):
                rows = self.api.list_orders(position["pair"], status=status, page=page)
                matches.extend(r for r in rows if r.get("text") == tag)
                if len(rows) < 100:
                    break
            else:
                raise RuntimeError("order history incomplete; manual reconciliation required")
        unique = {str(r["id"]): r for r in matches}
        if len(unique) > 1:
            raise RuntimeError("duplicate exchange orders for one intent")
        return next(iter(unique.values()), None)

    def _available(self, currency):
        matches = [r for r in self.api.accounts() if r.get("currency") == currency]
        return dec(matches[0]["available"]) if len(matches) == 1 else Decimal(0)

    def open(self, signal, metadata):
        """signal is a freshly produced EntrySignal, not a shadow trade row."""
        self.store.require_lock()
        if not self.limits.allow_entries or self.store.account_state()["paused"]:
            return None
        now = self.clock()
        if (now - signal.at).total_seconds() < 0 or (now - signal.at).total_seconds() > 30:
            return None
        if self.store.daily_pnl(now) <= -self.limits.daily_loss_quote:
            self.halt("Daily loss limit reached; entries remain paused until reviewed")
            return None
        positions = self.store.positions()
        if len(positions) >= self.limits.max_positions:
            return None
        exposure = sum((dec(p["data"].get("quote_budget", "0")) for p in positions), Decimal(0))
        # Reserve quote fees as well; do not spend the last account cent.
        budget = self.limits.order_quote
        if exposure + budget * Decimal("1.01") > self.limits.capital_quote:
            budget = (self.limits.capital_quote - exposure) / Decimal("1.01")
        budget = budget.quantize(Decimal("0.00000001"), rounding="ROUND_DOWN")
        base_currency, quote_currency = munten(signal.pair, metadata)
        if quote_currency != self.limits.quote_currency:
            # Anders koop je met een saldo dat je niet meet en reken je winst in
            # twee munten door elkaar.
            self.halt(f"Paar {signal.pair} handelt in {quote_currency}, de bot in "
                      f"{self.limits.quote_currency}; niet gekocht")
            return None
        if self._available(quote_currency) < budget * Decimal("1.01"):
            return None
        # Do not silently mix manual holdings with strategy-owned inventory.
        if self._available(base_currency) != 0:
            self.halt("Existing holdings in candidate pair; ownership must be reconciled")
            return None
        if metadata.get("trade_status") != "tradable":
            return None
        if budget < dec(metadata.get("min_quote_amount", "0")):
            return None
        if budget / dec(signal.reference_price) < dec(metadata.get("min_base_amount", "0")):
            return None
        position = self.store.create(signal.pair, signal.at.date(), {
            "quote_budget": budget * Decimal("1.01"), "day_open": signal.day_open,
            "entry_at": signal.at, "metadata": metadata,
        })
        if not position:
            return None
        tag = self.order_tag(position, "b")
        self.store.update(position, "BUY_SUBMITTED", buy_tag=tag)
        try:
            result = self.api.buy_market(signal.pair, budget, tag)
        except GateRejected:
            self.store.update(position, "REJECTED")
            self.halt("Gate rejected buy; check permissions/minimums", position)
            return position
        except GateUnknownOutcome:
            self.store.update(position, "UNKNOWN_BUY")
            self.halt("Buy outcome unknown; reconciliation required before any new buy", position)
            return position
        self.store.update(position, "BUY_SUBMITTED", buy_id=str(result["id"]))
        self.reconcile(position)
        return position

    def reconcile(self, position):
        self.store.require_lock()
        state = position["state"]
        if state == "INTENT":
            # The process may have died after Gate accepted the request but
            # before BUY_SUBMITTED was persisted. Search by deterministic tag;
            # absence is still uncertain, so keep the intent and pause rather
            # than silently creating a second order.
            order = self.lookup(position, "buy")
            if order is None:
                self.halt("Buy intent has no resolved exchange order; manual reconciliation required", position)
                return
            self.store.update(position, "BUY_SUBMITTED", buy_id=str(order["id"]))
            self.reconcile(position)
        elif state in ("BUY_SUBMITTED", "UNKNOWN_BUY"):
            order = self.lookup(position, "buy")
            if order is None:
                self.halt("Buy still unknown; do not retry", position)
                return
            self.store.update(position, "BUY_SUBMITTED", buy_id=str(order["id"]))
            fill = filled_order(order, position["pair"], "buy",
                                position["data"].get("metadata"))
            if fill is None:
                # Market IOC should already be terminal. Cancel then wait for
                # authoritative terminal status; no competing protective sell.
                self.api.cancel_order(position["pair"], str(order["id"]))
                self.halt("Nonterminal market buy; cancelled and awaiting reconciliation", position)
                return
            if fill["base"] <= 0:
                if fill["quote"]:
                    raise RuntimeError("quote spent without a base fill")
                self.store.update(position, "REJECTED")
                return
            if fill["quote"] <= 0:
                raise RuntimeError("base fill without cost")
            self.store.update(position, "BOUGHT", remaining=fill["base"], bought_base=fill["base"],
                              cost=fill["quote"], proceeds="0", entry_price=fill["quote"] / fill["base"],
                              fee_note=fill.get("fee_note", ""))
            if fill.get("fee_note"):
                # Melden, niet stoppen: de positie moet eerst beschermd worden.
                self.alert("Kostprijs onzeker: " + fill["fee_note"])
            self.protect(position)
        elif state == "BOUGHT":
            self.protect(position)
        elif state in ("STOP_SUBMITTED", "UNKNOWN_STOP"):
            self.recover_stop(position)
        elif state in ("PROTECTED", "CANCEL_STOP"):
            stop = self.api.get_stop(position["data"]["stop_id"])
            status = stop.get("status")
            fired = stop.get("fired_order_id")
            if fired and str(fired) != "0":
                self.store.update(position, "SELL_SUBMITTED", sell_id=str(fired),
                                  sell_tag="native_stop", close_reason="STOP")
                self.settle_sell(position)
            elif status == "open":
                if state == "CANCEL_STOP":
                    self.cancel_then_close(position)
            elif status in ("cancelled", "failed", "expired"):
                self.store.update(position, "UNPROTECTED")
                self.halt("Protective order no longer active; closing remaining inventory", position)
                self.sell(position)
            else:
                self.halt("Protective order has unresolved execution state", position)
        elif state in ("UNPROTECTED", "SELL_READY"):
            self.sell(position)
        elif state in ("SELL_SUBMITTED", "UNKNOWN_SELL"):
            self.settle_sell(position)

    def recover_stop(self, position):
        """Is die stop er nu wel of niet?

        Vroeger stond hier alleen een pauze. Dat voorkomt een dubbele order,
        maar laat een gekochte positie onbeschermd staan tot iemand het toevallig
        ziet. We kijken nu bij Gate zelf: staat de stop in de lijst, dan nemen we
        hem over; staat hij er niet én hebben we de munt nog, dan plaatsen we hem
        alsnog (dat is herstel, geen blinde tweede order); kunnen we het niet
        vaststellen, dan pas pauzeren mét melding (Astra + Fable, 20-9).
        """
        data = position["data"]
        try:
            stops = self.api.list_stops(status="open", pair=position["pair"])
        except GateError:
            self.halt("Stop onzeker en lijst niet op te vragen; controleer Gate handmatig", position)
            return
        trigger = dec(data.get("stop_trigger", "0"))
        gevonden = None
        for stop in stops or []:
            if stop.get("market") not in (None, position["pair"]):
                continue
            try:
                prijs = dec((stop.get("trigger") or {}).get("price", "0"))
            except Exception:
                continue
            if trigger and prijs == trigger:
                gevonden = stop
                break
        if gevonden:
            self.store.update(position, "PROTECTED", stop_id=str(gevonden["id"]))
            self.alert("Stop teruggevonden bij Gate na onzekere uitkomst; positie is beschermd")
            self.reconcile(position)
            return
        # Geen stop gevonden. Hebben we de munt nog? Zo niet, dan is hij
        # waarschijnlijk al gevuurd en mogen we er zeker geen nieuwe zetten.
        base_currency, _ = munten(position["pair"], position["data"].get("metadata"))
        beschermd = dec(data.get("protected_amount", "0"))
        try:
            saldo = self._available(base_currency)
        except GateError:
            self.halt("Stop onzeker en saldo niet op te vragen; controleer Gate handmatig", position)
            return
        if beschermd and saldo >= beschermd:
            self.alert("Geen stop bij Gate gevonden terwijl de positie er nog staat; stop wordt opnieuw geplaatst")
            self.store.update(position, "BOUGHT")
            self.protect(position)
            return
        self.halt("Stop niet gevonden en saldo klopt niet met de positie; controleer Gate handmatig",
                  position)

    def quantity(self, position):
        quantum = Decimal(1).scaleb(-int(position["data"]["metadata"]["amount_precision"]))
        return dec(position["data"]["remaining"]).quantize(quantum, rounding="ROUND_DOWN")

    def protect(self, position):
        quantity = self.quantity(position)
        metadata = position["data"]["metadata"]
        price_quantum = Decimal(1).scaleb(-int(metadata["precision"]))
        trigger = (dec(position["data"]["entry_price"]) * Decimal("0.97")).quantize(price_quantum, rounding="ROUND_DOWN")
        if quantity <= 0 or quantity < dec(metadata.get("min_base_amount", "0")):
            self.halt("Filled quantity below minimum; manual dust reconciliation required", position)
            return
        self.store.update(position, "STOP_SUBMITTED", stop_trigger=trigger, protected_amount=quantity)
        try:
            # De correlatie-id moet aan hetzelfde patroon voldoen als elke andere
            # order-id (t-...). Met "api" gooide create_stop een ValueError VOOR
            # er een verzoek uitging: de aankoop was dan gevuld, de stop niet
            # geplaatst, en de positie bleef onbeschermd hangen op
            # STOP_SUBMITTED (Fable-controle 20-9).
            stop = self.api.create_stop(position["pair"], quantity, trigger,
                                        self.order_tag(position, "p"), expiration=172800)
        except GateRejected:
            self.store.update(position, "UNPROTECTED")
            self.halt("Gate rejected protective stop; closing immediately", position)
            self.sell(position)
            return
        except GateUnknownOutcome:
            self.store.update(position, "UNKNOWN_STOP")
            self.halt("Stop may exist; paused for reconciliation, no duplicate sell", position)
            return
        self.store.update(position, "PROTECTED", stop_id=str(stop["id"]))
        # Do not claim protection solely on a successful create response.
        self.reconcile(position)

    def close(self, position, reason):
        if position["state"] != "PROTECTED":
            return
        self.store.update(position, "CANCEL_STOP", close_reason=reason)
        self.cancel_then_close(position)

    def cancel_then_close(self, position):
        try:
            self.api.cancel_stop(position["data"]["stop_id"])
        except (GateRejected, GateUnknownOutcome):
            # It may have fired concurrently. Never sell until status is known.
            pass
        stop = self.api.get_stop(position["data"]["stop_id"])
        fired = stop.get("fired_order_id")
        if fired and str(fired) != "0":
            self.store.update(position, "SELL_SUBMITTED", sell_id=str(fired), sell_tag="native_stop")
            self.settle_sell(position)
        elif stop.get("status") in ("cancelled", "failed", "expired"):
            self.store.update(position, "SELL_READY")
            self.sell(position)
        else:
            self.halt("Stop cancellation not confirmed; sale withheld to prevent double execution", position)

    def sell(self, position):
        quantity = self.quantity(position)
        metadata = position["data"]["metadata"]
        if quantity <= 0 or quantity < dec(metadata.get("min_base_amount", "0")):
            self.halt("Remaining inventory below minimum; manual dust reconciliation required", position)
            return
        basis, _ = munten(position["pair"], position["data"].get("metadata"))
        if self._available(basis) < quantity:
            self.halt("Insufficient available inventory; reconcile locked balance before sale", position)
            return
        attempt = int(position["data"].get("sell_attempt", 0)) + 1
        self.store.update(position, "SELL_READY", sell_attempt=attempt)
        tag = self.order_tag(position, "s")
        self.store.update(position, "SELL_SUBMITTED", sell_tag=tag, sell_id=None)
        try:
            result = self.api.sell_market(position["pair"], quantity, tag)
        except GateRejected:
            self.store.update(position, "EXIT_REJECTED")
            self.halt("Exit rejected; inventory remains, urgent manual intervention", position)
            return
        except GateUnknownOutcome:
            self.store.update(position, "UNKNOWN_SELL")
            self.halt("Sell outcome unknown; no blind retry", position)
            return
        self.store.update(position, "SELL_SUBMITTED", sell_id=str(result["id"]))
        self.settle_sell(position)

    def settle_sell(self, position):
        order = self.lookup(position, "sell")
        if order is None:
            self.halt("Sell remains unknown", position)
            return
        fill = filled_order(order, position["pair"], "sell",
                            position["data"].get("metadata"))
        if fill is None:
            self.halt("Sell not terminal; awaiting final fills", position)
            return
        if fill["base"] <= 0:
            self.store.update(position, "EXIT_REJECTED")
            self.halt("Exit has zero fill; inventory still open", position)
            return
        if fill.get("fee_note"):
            self.alert("Opbrengst onzeker bij verkoop: " + fill["fee_note"])
        remaining = dec(position["data"]["remaining"]) - fill["base"]
        if remaining < 0:
            raise RuntimeError("sold more than strategy-owned inventory")
        proceeds = dec(position["data"]["proceeds"]) + fill["quote"]
        self.store.update(position, "SELL_READY", remaining=remaining, proceeds=proceeds)
        if remaining == 0:
            self.store.update(position, "CLOSED", pnl_usdt=proceeds - dec(position["data"]["cost"]),
                              closed_at=self.clock())
        elif self.quantity(position) > 0:
            # Separate intent next run, after terminal fill is durably accounted.
            self.halt("Partial exit; remainder scheduled for next reconciliation", position)
        else:
            self.store.update(position, "DUST")
            self.halt("Rounding dust remains; position is not silently marked closed", position)
