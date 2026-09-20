from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from trading.gate_v1 import (
    Candle, Quote, Position, build_day_setup, btc_regime, entry_signal, exit_signal,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 20, 12, 30, tzinfo=UTC)
DAY = NOW.replace(hour=0, minute=0)


def bars(count=31, btc=False):
    result = []
    for i in range(count):
        start = DAY - timedelta(days=count-i)
        close = 101 if btc and i == count-1 else 100
        result.append(Candle(start, start+timedelta(days=1), 100, 102, 98,
                             close, 3_000_000 if i == count-1 else 2_000_000))
    return result


def entry(**overrides):
    args = dict(pair="TEST_USDT", daily_bars=bars(), day_open=101,
                previous_quote=Quote(NOW-timedelta(minutes=1), 102, 102.5),
                quote=Quote(NOW, 103, 103.2), btc_daily_bars=bars(201, True),
                now=NOW, quote_volume_24h=3_000_000, listing_age_days=40)
    args.update(overrides)
    return entry_signal(**args)


def test_crossing_uses_current_ask_not_historical_level():
    signal = entry()
    assert signal.level == 103
    assert signal.reference_price == 103.2
    assert signal.stop_price == pytest.approx(103.2 * .97)


def test_first_observation_above_level_is_not_a_retrospective_entry():
    assert entry(previous_quote=None) is None
    assert entry(previous_quote=Quote(NOW-timedelta(minutes=1), 103, 104)) is None


@pytest.mark.parametrize("overrides", [
    {"quote": Quote(NOW-timedelta(minutes=3), 103, 103.2)},
    {"quote": Quote(NOW+timedelta(seconds=1), 103, 103.2)},
    {"previous_quote": Quote(NOW-timedelta(minutes=4), 102, 102.5)},
    {"previous_quote": Quote(NOW, 102, 102.5)},
    {"quote_volume_24h": 1_999_999},
    {"listing_age_days": 34},
    {"quote": Quote(NOW, 100, 104)},
    {"traded_today": True},
    {"quote_volume_24h": float("nan")},
])
def test_entry_fails_closed(overrides):
    assert entry(**overrides) is None


def test_daily_history_must_be_complete_closed_and_contiguous():
    history = bars()
    assert build_day_setup(history, 101, NOW).level == 103
    assert build_day_setup(history[:10]+history[11:], 101, NOW) is None
    assert build_day_setup(bars(32)[:10]+bars(32)[11:], 101, NOW) is None
    assert build_day_setup(history[:-1], 101, NOW) is None
    assert build_day_setup(history+[replace(history[-1], start=DAY,
                                            end=DAY+timedelta(days=1))], 101, NOW) is None
    assert build_day_setup(list(reversed(history)), 101, NOW) is None


def test_exact_shadow_volume_and_ma_filters():
    history = bars()
    history[-1] = replace(history[-1], quote_volume=2_000_000)
    assert build_day_setup(history, 101, NOW) is None
    assert build_day_setup(bars(), 100, NOW) is None
    assert btc_regime(bars(201, True), NOW)
    assert not btc_regime(bars(201), NOW)
    assert not btc_regime(bars(200, True), NOW)


def test_entry_cutoff_includes_hour_20_but_not_21():
    for hour in (20, 21):
        now = NOW.replace(hour=hour)
        signal = entry(now=now, quote=Quote(now, 103, 103.2),
                       previous_quote=Quote(now-timedelta(minutes=1), 102, 102.5))
        assert (signal is not None) == (hour == 20)


def test_stop_can_trigger_in_entry_hour_and_uses_current_bid():
    position = Position(NOW-timedelta(seconds=30), 100, 99)
    decision = exit_signal(position, Quote(NOW, 95, 96), [], NOW)
    assert decision.reason == "STOP"
    assert decision.reference_price == 95  # no imaginary guaranteed 97 fill


def test_day_rollover_exits_even_after_midnight_was_missed():
    position = Position(DAY-timedelta(minutes=10), 100, 99)
    assert exit_signal(position, Quote(NOW, 101, 102), [], NOW).reason == "TIME_DAG"


def test_hour_close_under_open_executes_at_observable_price():
    position = Position(NOW.replace(hour=11, minute=30), 100, 100)
    bar = Candle(NOW.replace(hour=11, minute=0), NOW.replace(hour=12, minute=0),
                 101, 102, 98, 99, 1000)
    decision = exit_signal(position, Quote(NOW, 100.5, 101), [bar], NOW)
    assert decision.reason == "ONDER_OPEN"
    assert decision.reference_price == 100.5
    assert exit_signal(position, Quote(NOW, 100.5, 101),
                       [replace(bar, start=bar.start+timedelta(hours=1),
                                end=bar.end+timedelta(hours=1))], NOW) is None


def test_exit_ignores_candle_closed_before_entry_and_stale_quote():
    position = Position(NOW, 100, 100)
    bar = Candle(NOW.replace(hour=11, minute=0), NOW.replace(hour=12, minute=0),
                 101, 102, 98, 99, 1000)
    assert exit_signal(position, Quote(NOW, 101, 102), [bar], NOW) is None
    assert exit_signal(position, Quote(NOW-timedelta(minutes=5), 95, 96), [], NOW) is None


def test_naive_time_is_rejected():
    with pytest.raises(ValueError):
        entry(now=NOW.replace(tzinfo=None))
