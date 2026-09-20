"""Causal VBREAK V1 decisions; no exchange, database or clock side effects.

Unlike gate_shadow, entry requires two actually observed quotes straddling the
level. A historical candle high never becomes an executable entry. Quotes are
indicative, not fills: the executor must compute protection from actual fills.
Polling can miss a breakout; it must never invent a historical fill to catch up.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from statistics import median
from typing import Sequence

UTC = timezone.utc
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class Candle:
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    quote_volume: float


@dataclass(frozen=True)
class Quote:
    at: datetime
    bid: float
    ask: float


@dataclass(frozen=True)
class DaySetup:
    day: datetime
    day_open: float
    level: float
    previous_quote_volume: float


@dataclass(frozen=True)
class EntrySignal:
    pair: str
    at: datetime
    reference_price: float
    level: float
    day_open: float
    stop_price: float


@dataclass(frozen=True)
class Position:
    entry_at: datetime
    entry_price: float
    day_open: float


@dataclass(frozen=True)
class ExitSignal:
    at: datetime
    reference_price: float
    reason: str


def _utc(t: datetime) -> datetime:
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError("timestamps must be timezone aware")
    return t.astimezone(UTC)


def _positive(*values: float) -> bool:
    return all(isfinite(v) and v > 0 for v in values)


def _valid_bar(bar: Candle, period: timedelta, now: datetime) -> bool:
    start, end = _utc(bar.start), _utc(bar.end)
    return (end - start == period and end <= now
            and _positive(bar.open, bar.high, bar.low, bar.close)
            and isfinite(bar.quote_volume) and bar.quote_volume >= 0
            and bar.low <= min(bar.open, bar.close)
            and bar.high >= max(bar.open, bar.close))


def _daily_tail(bars: Sequence[Candle], count: int, now: datetime):
    if len(bars) < count:
        return None
    # Reject future/incomplete input instead of silently treating it as closed.
    if any(not _valid_bar(b, DAY, now) for b in bars):
        return None
    if any(_utc(a.end) != _utc(b.start) for a, b in zip(bars, bars[1:])):
        return None
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if _utc(bars[-1].end) != today:
        return None
    return bars[-count:]


def btc_regime(bars: Sequence[Candle], now: datetime) -> bool:
    """Preserve shadow's last closed daily close > 200-day SMA, fail closed."""
    tail = _daily_tail(bars, 201, _utc(now))
    return bool(tail and tail[-1].close > sum(b.close for b in tail[-200:]) / 200)


def build_day_setup(daily_bars: Sequence[Candle], day_open: float,
                    now: datetime) -> DaySetup | None:
    now = _utc(now)
    tail = _daily_tail(daily_bars, 31, now)
    if not tail or not _positive(day_open):
        return None
    previous = tail[-1]
    previous_range = previous.high - previous.low
    ma5 = sum(b.close for b in tail[-5:]) / 5
    # Exactly the shadow slice: 30 preceding days, excluding yesterday.
    vmed = median(b.quote_volume for b in tail[-31:-1])
    if previous_range <= 0 or day_open <= ma5 or previous.quote_volume <= vmed:
        return None
    return DaySetup(now.replace(hour=0, minute=0, second=0, microsecond=0),
                    day_open, day_open + .5 * previous_range, previous.quote_volume)


def _fresh_quote(q: Quote, now: datetime, maximum_age: float) -> bool:
    age = (now - _utc(q.at)).total_seconds()
    return (0 <= age <= maximum_age and _positive(q.bid, q.ask) and q.bid <= q.ask)


def entry_signal(pair: str, daily_bars: Sequence[Candle], day_open: float,
                 previous_quote: Quote | None, quote: Quote,
                 btc_daily_bars: Sequence[Candle], now: datetime, *,
                 quote_volume_24h: float, listing_age_days: float,
                 traded_today: bool = False, max_quote_age_seconds: float = 120,
                 max_crossing_gap_seconds: float = 120) -> EntrySignal | None:
    now = _utc(now)
    if (traded_today or previous_quote is None or now.hour > 20
            or not isfinite(quote_volume_24h) or quote_volume_24h < 2_000_000
            or not isfinite(listing_age_days) or listing_age_days < 35
            or not _fresh_quote(quote, now, max_quote_age_seconds)
            or not _fresh_quote(previous_quote, now,
                                max_quote_age_seconds + max_crossing_gap_seconds)):
        return None
    gap = (_utc(quote.at) - _utc(previous_quote.at)).total_seconds()
    if (not 0 < gap <= max_crossing_gap_seconds
            or _utc(quote.at).date() != now.date()
            or _utc(previous_quote.at).date() != now.date()
            or (quote.ask - quote.bid) / quote.bid > .02):
        return None
    setup = build_day_setup(daily_bars, day_open, now)
    if not setup or not btc_regime(btc_daily_bars, now):
        return None
    if not previous_quote.ask < setup.level <= quote.ask:
        return None
    return EntrySignal(pair, _utc(quote.at), quote.ask, setup.level, day_open,
                       quote.ask * .97)


def exit_signal(position: Position, quote: Quote,
                closed_hourly_bars: Sequence[Candle], now: datetime, *,
                max_quote_age_seconds: float = 120) -> ExitSignal | None:
    now, entry_at = _utc(now), _utc(position.entry_at)
    if (entry_at > now or not _positive(position.entry_price, position.day_open)
            or not _fresh_quote(quote, now, max_quote_age_seconds)
            or _utc(quote.at) < entry_at):
        return None
    # Continuous/current quote checks include the entry hour, unlike shadow.
    if quote.bid <= position.entry_price * .97:
        return ExitSignal(_utc(quote.at), quote.bid, "STOP")
    if now.date() > entry_at.date():
        return ExitSignal(_utc(quote.at), quote.bid, "TIME_DAG")
    if any(not _valid_bar(b, HOUR, now) for b in closed_hourly_bars):
        return None
    if any(_utc(a.end) != _utc(b.start)
           for a, b in zip(closed_hourly_bars, closed_hourly_bars[1:])):
        return None
    for bar in closed_hourly_bars:
        if _utc(bar.end) > entry_at and bar.close < position.day_open:
            # Trigger known only at bar.end; execution price is current bid.
            return ExitSignal(_utc(quote.at), quote.bid, "ONDER_OPEN")
    return None
