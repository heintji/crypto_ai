"""Bewijs-tests voor research/plan_g_shadow.py (Plan G v2 schaduw-consumer).

Fake-DB (geen netwerk, geen echte database): bewijst dat
  1. een geldig PENDING signaal (score 80-89, RR >= min_rr) het INSERT-pad
     naar experience_trades bereikt en de pending op status SHADOW zet;
  2. de gates (blocklist / lage RR) correct blokkeren;
  3. de close-pass een open shadow bij stop-hit als CLOSED/LOSS wegschrijft
     en bij target-hit als CLOSED/WIN.
"""
import datetime as dt
from datetime import timezone

import pytest

from research import plan_g_shadow as pg


# ────────────────────────────────────────────────────────────
# Fake DB
# ────────────────────────────────────────────────────────────
class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._result = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.db.executed.append((s, params))
        self._result = []
        if "SELECT value FROM bot_state" in s:
            key = params[0]
            v = self.db.bot_state.get(key)
            self._result = [(v,)] if v is not None else []
        elif "FROM pending_approvals WHERE status = 'PENDING'" in s:
            self._result = list(self.db.pending_rows)
        elif "SELECT COUNT(*) FROM experience_trades" in s:
            self._result = [(0,)]
        elif "payload->>'volume_24h_eur'" in s:
            self._result = [(0,)]
        elif "FROM bot_health WHERE service='trade_monitor'" in s:
            self._result = [(self.db.monitor_last_run,)]
        elif "FROM experience_trades WHERE status='OPEN' AND is_shadow=TRUE" in s:
            self._result = list(self.db.open_shadow_rows)
        self.rowcount = 1

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self):
        self.executed = []
        self.bot_state = {
            "score_drempel_shadow": "80",
            "shadow_score_max": "89",
            "shadow_stop_target_mult": "1.8",
            "shadow_min_rr": "2.0",
            "shadow_min_volume_24h_eur": "0",
            "btc_regime_huidig": "BULL",
            "shadow_skip_non_trending": "false",
            "shadow_skip_weekend": "false",
            "shadow_skip_lunch": "false",
            "shadow_active_hours_min": "0",
            "shadow_active_hours_max": "24",
            "shadow_blocked_coins": "APEUSDT",
            "shadow_sizing_mode": "FLAT",
            "shadow_trade_size_eur": "10",
            "live_trader_last_ts": "",  # live_trader NIET actief
        }
        self.pending_rows = []
        self.open_shadow_rows = []
        self.monitor_last_run = None  # trade_monitor NIET actief

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


def _sqls(conn, needle):
    return [(s, p) for (s, p) in conn.executed if needle in s]


# ────────────────────────────────────────────────────────────
# OPEN-PASS
# ────────────────────────────────────────────────────────────
def test_open_pass_bereikt_insert_pad():
    """Geldig signaal (score 85, RR=2.0) -> INSERT experience_trades + status SHADOW."""
    conn = FakeConn()
    # (id, symbol, market, score, entry, stop, target, kelly, setup, regime)
    conn.pending_rows = [
        ("pid-1", "IMXUSDT", "IMX-EUR", 85, 0.0968, 0.095314, 0.099772, 7.0,
         "TREND_PULLBACK", "BULL"),
    ]
    n = pg.open_pass(conn)
    assert n == 1
    inserts = _sqls(conn, "INSERT INTO experience_trades")
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert "is_shadow" in sql and "'SHADOW'" in sql
    assert params[0].startswith("SHADOW|IMXUSDT|")   # trade_key
    assert params[1] == "IMX"                        # coin
    # stop/target verbreed met mult 1.8
    entry, stop_adj = params[6], params[7]
    assert stop_adj == pytest.approx(entry - (0.0968 - 0.095314) * 1.8, rel=1e-4)
    # pending op SHADOW gezet
    updates = _sqls(conn, "SET status='SHADOW' WHERE")
    assert updates and updates[0][1] == ("pid-1",)


def test_open_pass_blocklist_en_lage_rr_blokkeren():
    conn = FakeConn()
    conn.pending_rows = [
        # APEUSDT staat op blocklist
        ("pid-b", "APEUSDT", "APE-EUR", 85, 1.0, 0.98, 1.04, 7.0, "X", "BULL"),
        # RR = (1.01-1.00)/(1.00-0.99) = 1.0 < 2.0
        ("pid-r", "GALAUSDT", "GALA-EUR", 84, 1.0, 0.99, 1.01, 7.0, "X", "BULL"),
    ]
    n = pg.open_pass(conn)
    assert n == 0
    assert not _sqls(conn, "INSERT INTO experience_trades")
    assert _sqls(conn, "SET status='SHADOW_BLOCKED'")[0][1] == ("pid-b",)
    assert _sqls(conn, "SET status='SHADOW_LOW_RR'")[0][1] == ("pid-r",)


def test_open_pass_dicht_buiten_actieve_uren_en_forceerbaar():
    conn = FakeConn()
    conn.bot_state["shadow_active_hours_min"] = "0"
    conn.bot_state["shadow_active_hours_max"] = "0"   # altijd dicht
    conn.pending_rows = [
        ("pid-1", "IMXUSDT", "IMX-EUR", 85, 0.0968, 0.095314, 0.099772, 7.0,
         "TREND_PULLBACK", "BULL"),
    ]
    assert pg.open_pass(conn) == 0
    assert not _sqls(conn, "INSERT INTO experience_trades")
    # met forceer_tijdgates wél open
    conn2 = FakeConn()
    conn2.bot_state["shadow_active_hours_max"] = "0"
    conn2.pending_rows = list(conn.pending_rows)
    assert pg.open_pass(conn2, forceer_tijdgates=True) == 1


def test_open_pass_slaat_over_als_live_trader_actief():
    conn = FakeConn()
    conn.bot_state["live_trader_last_ts"] = pg.now_utc().isoformat()
    conn.pending_rows = [
        ("pid-1", "IMXUSDT", "IMX-EUR", 85, 0.0968, 0.095314, 0.099772, 7.0,
         "TREND_PULLBACK", "BULL"),
    ]
    assert pg.open_pass(conn) == 0
    assert not _sqls(conn, "INSERT INTO experience_trades")


# ────────────────────────────────────────────────────────────
# CLOSE-PASS
# ────────────────────────────────────────────────────────────
def _open_shadow_row(entry=1.0, stop=0.95, target=1.10, minutes_open=60):
    et = dt.datetime.now(timezone.utc) - dt.timedelta(minutes=minutes_open)
    # (trade_key, symbol, entry, stop, target, qty, amount_eur, entry_time, max_seen, min_seen)
    return ("SHADOW|TSTUSDT|1", "TSTUSDT", entry, stop, target, 10.0, 10.0, et, None, None)


def test_close_pass_stop_hit_wordt_loss(monkeypatch):
    conn = FakeConn()
    conn.open_shadow_rows = [_open_shadow_row()]
    monkeypatch.setattr(pg, "get_binance_price", lambda s: 0.94)  # onder stop
    assert pg.close_pass(conn) == 1
    sql, params = _sqls(conn, "status='CLOSED'")[0]
    outcome, exit_reden = params[0], params[-2]
    assert outcome == "LOSS" and exit_reden == "STOP_LOSS"


def test_close_pass_target_hit_wordt_win(monkeypatch):
    conn = FakeConn()
    conn.open_shadow_rows = [_open_shadow_row()]
    monkeypatch.setattr(pg, "get_binance_price", lambda s: 1.11)  # boven target
    assert pg.close_pass(conn) == 1
    sql, params = _sqls(conn, "status='CLOSED'")[0]
    assert params[0] == "WIN" and params[-2] == "TARGET_HIT"


def test_close_pass_max_hold_sluit_na_48u(monkeypatch):
    conn = FakeConn()
    conn.open_shadow_rows = [_open_shadow_row(minutes_open=49 * 60)]
    monkeypatch.setattr(pg, "get_binance_price", lambda s: 1.0)  # tussenin
    assert pg.close_pass(conn) == 1
    assert _sqls(conn, "status='CLOSED'")[0][1][-2] == "MAX_HOLD_TIME"


def test_close_pass_geen_exit_update_alleen_extremen(monkeypatch):
    conn = FakeConn()
    conn.open_shadow_rows = [_open_shadow_row()]
    monkeypatch.setattr(pg, "get_binance_price", lambda s: 1.02)
    assert pg.close_pass(conn) == 0
    assert not _sqls(conn, "status='CLOSED'")
    assert _sqls(conn, "SET max_price_seen")


def test_close_pass_slaat_over_als_monitor_actief(monkeypatch):
    conn = FakeConn()
    conn.monitor_last_run = pg.now_utc()
    conn.open_shadow_rows = [_open_shadow_row()]
    monkeypatch.setattr(pg, "get_binance_price", lambda s: 0.5)
    assert pg.close_pass(conn) == 0
