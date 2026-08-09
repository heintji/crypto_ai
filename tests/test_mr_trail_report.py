"""Tests voor research/mr_trail_report.py (meetlaag: uitvoerbaarheids-cap).

Pure functies, geen DB. Bewijst:
  1. de cap op max gelijktijdige posities;
  2. geen tweede open positie in dezelfde munt;
  3. slot komt pas vrij als exit STRIKT voor de nieuwe entry ligt;
  4. equity-metriek: rendement, drawdown, verliesreeks, winrate;
  5. regime-lookup pakt de laatste candle <= entry.
"""
import datetime as dt
from datetime import timezone

import pytest

from research import mr_trail_report as mr


def ts(h):
    return dt.datetime(2026, 7, 1, tzinfo=timezone.utc) + dt.timedelta(hours=h)


def trade(coin, e_h, x_h, net=1.0):
    return {"coin": coin, "entry_ts": ts(e_h), "exit_ts": ts(x_h), "pnl_net": net}


# ────────────────────────────────────────────────────────────
# replay_cap
# ────────────────────────────────────────────────────────────
def test_cap_max_gelijktijdig():
    trades = [trade(f"C{i}USDT", 0, 48) for i in range(5)]
    taken, skip_cap, skip_dup = mr.replay_cap(trades, max_concurrent=3)
    assert len(taken) == 3 and skip_cap == 2 and skip_dup == 0


def test_geen_dubbele_munt():
    trades = [trade("AUSDT", 0, 48), trade("AUSDT", 4, 52), trade("BUSDT", 4, 52)]
    taken, skip_cap, skip_dup = mr.replay_cap(trades, max_concurrent=10)
    assert [t["coin"] for t in taken] == ["AUSDT", "BUSDT"]
    assert skip_dup == 1 and skip_cap == 0


def test_slot_komt_vrij_na_exit_strikt():
    # exit op h=8; entry op h=8 (zelfde candle) mag NIET, entry op h=12 wel
    trades = [trade("AUSDT", 0, 8),
              trade("BUSDT", 8, 20),    # cap=1: slot nog bezet op h=8
              trade("CUSDT", 12, 20)]   # slot vrij
    taken, skip_cap, _ = mr.replay_cap(trades, max_concurrent=1)
    assert [t["coin"] for t in taken] == ["AUSDT", "CUSDT"]
    assert skip_cap == 1


def test_dezelfde_munt_mag_na_sluiting_weer():
    trades = [trade("AUSDT", 0, 8), trade("AUSDT", 12, 20)]
    taken, _, skip_dup = mr.replay_cap(trades, max_concurrent=5)
    assert len(taken) == 2 and skip_dup == 0


# ────────────────────────────────────────────────────────────
# equity_metrics
# ────────────────────────────────────────────────────────────
def test_equity_leeg():
    m = mr.equity_metrics([], slots=12)
    assert m["n"] == 0 and m["return_pct"] == 0.0


def test_equity_rendement_en_winrate():
    # 1 slot: +10% dan -10% samengesteld = -1%
    taken = [trade("A", 0, 4, net=10.0), trade("B", 4, 8, net=-10.0)]
    m = mr.equity_metrics(taken, slots=1)
    assert m["return_pct"] == pytest.approx(-1.0)
    assert m["winrate_pct"] == 50.0
    assert m["som_netto_pct"] == 0.0
    assert m["max_drawdown_pct"] == pytest.approx(10.0)


def test_equity_slots_dempen():
    # zelfde trades over 10 slots: elk beweegt het account 1/10e
    taken = [trade("A", 0, 4, net=10.0)]
    m = mr.equity_metrics(taken, slots=10)
    assert m["return_pct"] == pytest.approx(1.0)


def test_langste_verliesreeks():
    taken = [trade("A", 0, 4, net=-1), trade("B", 4, 8, net=-1),
             trade("C", 8, 12, net=2), trade("D", 12, 16, net=-1)]
    m = mr.equity_metrics(taken, slots=12)
    assert m["langste_verliesreeks"] == 2


# ────────────────────────────────────────────────────────────
# regime_lookup
# ────────────────────────────────────────────────────────────
def test_regime_lookup_laatste_candle():
    rows = [(ts(0), "RANGE"), (ts(4), "BULL"), (ts(8), "BEAR")]
    lookup = mr.regime_lookup(rows)
    assert lookup(ts(0)) == "RANGE"
    assert lookup(ts(5)) == "BULL"      # tussen candles -> laatste ervoor
    assert lookup(ts(8)) == "BEAR"
    assert lookup(ts(-4)) == "ONBEKEND"
