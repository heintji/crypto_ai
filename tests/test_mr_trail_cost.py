"""Tests voor research/mr_trail_cost.py (meetlaag: kostenmodel).

Fake-DB, geen netwerk. Bewijst:
  1. de liquiditeitstrap (major/mid/micro) + volume-fallback;
  2. de netto-berekening pnl_net = pnl - fee_rt - spread_rt;
  3. bot_state-overrides winnen van de defaults, kapotte waarden niet;
  4. apply_costs is idempotent (alleen rijen met pnl_net_pct IS NULL) en
     raakt geen strategie-kolommen aan.
"""
import pytest

from research import mr_trail_cost as mc


def cfg_default():
    cfg = dict(mc.DEFAULTS)
    cfg["majors"] = set(mc.MAJORS_DEFAULT)
    return cfg


# ────────────────────────────────────────────────────────────
# 1+2: trap en netto-berekening
# ────────────────────────────────────────────────────────────
def test_spread_tier_major_ongeacht_volume():
    cfg = cfg_default()
    assert mc.spread_tier("BTCUSDT", 1_000, cfg) == "major"
    assert mc.spread_tier("BTCUSDT", None, cfg) == "major"


def test_spread_tier_micro_onder_drempel():
    cfg = cfg_default()
    assert mc.spread_tier("PEPEUSDT", 4_999_999, cfg) == "micro"
    assert mc.spread_tier("PEPEUSDT", 5_000_000, cfg) == "mid"


def test_spread_tier_fallback_zonder_volume_is_mid():
    cfg = cfg_default()
    assert mc.spread_tier("OBSCUURUSDT", None, cfg) == "mid"
    assert mc.spread_tier("OBSCUURUSDT", 0, cfg) == "mid"


def test_compute_costs_major():
    fee, spread, net = mc.compute_costs(2.0, "ETHUSDT", 1e9, cfg_default())
    assert fee == 0.50          # 2 x 0.25
    assert spread == 0.10       # 2 x 0.05
    assert net == pytest.approx(1.40)


def test_compute_costs_mid_en_micro():
    cfg = cfg_default()
    _, spread_mid, net_mid = mc.compute_costs(2.0, "LTCUSDT", 50e6, cfg)
    assert spread_mid == 0.30   # 2 x 0.15
    assert net_mid == pytest.approx(1.20)
    _, spread_mic, net_mic = mc.compute_costs(2.0, "MICROUSDT", 1e6, cfg)
    assert spread_mic == 0.80   # 2 x 0.40
    assert net_mic == pytest.approx(0.70)


def test_verliezer_wordt_netto_slechter():
    _, _, net = mc.compute_costs(-2.0, "MICROUSDT", 1e6, cfg_default())
    assert net == pytest.approx(-3.30)   # micro: -2.0 - 0.50 fee - 0.80 spread


# ────────────────────────────────────────────────────────────
# Fake DB
# ────────────────────────────────────────────────────────────
class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._result = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.db.executed.append((s, params))
        self._result = []
        if s.startswith("ALTER TABLE"):
            self.db.altered = True
        elif "SELECT value FROM bot_state" in s:
            v = self.db.bot_state.get(params[0])
            self._result = [(v,)] if v is not None else []
        elif "FROM mr_trail_trades t" in s and "pnl_net_pct IS NULL" in s:
            self._result = [r for r in self.db.trade_rows
                            if r[0] not in self.db.netted]

    def executemany(self, sql, seq):
        s = " ".join(sql.split())
        assert "SET fee_pct=%s, spread_pct=%s, pnl_net_pct=%s" in s
        # bewijs: alleen kostenkolommen, nooit strategie-velden
        for kolom in ("status", "exit_prijs", "exit_ts", "stop", "target",
                      "entry", "pnl_pct="):
            assert kolom not in s
        for fee, spread, net, tid in seq:
            self.db.updates[tid] = (fee, spread, net)
            self.db.netted.add(tid)

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, trade_rows, bot_state=None):
        self.executed = []
        self.altered = False
        self.bot_state = bot_state or {}
        self.trade_rows = trade_rows      # (id, coin, pnl_pct, vol_24h)
        self.updates = {}
        self.netted = set()
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1


# ────────────────────────────────────────────────────────────
# 3: config uit bot_state
# ────────────────────────────────────────────────────────────
def test_load_cfg_overrides_en_kapotte_waarden():
    conn = FakeConn([], bot_state={
        "mr_fee_pct_per_side": "0.15",
        "mr_micro_volume_eur": "1000000",
        "mr_spread_micro_pct": "niet-een-getal",   # kapot -> default blijft
        "mr_majors": '["BTCUSDT","ETHUSDT"]',
    })
    cfg = mc.load_cfg(conn.cursor())
    assert cfg["mr_fee_pct_per_side"] == 0.15
    assert cfg["mr_micro_volume_eur"] == 1_000_000
    assert cfg["mr_spread_micro_pct"] == mc.DEFAULTS["mr_spread_micro_pct"]
    assert cfg["majors"] == {"BTCUSDT", "ETHUSDT"}


# ────────────────────────────────────────────────────────────
# 4: apply_costs — vult, en is idempotent
# ────────────────────────────────────────────────────────────
def test_apply_costs_vult_en_is_idempotent():
    rows = [
        (1, "BTCUSDT", 2.0, 5e9),     # major : net 2.0-0.5-0.1 = 1.40
        (2, "LTCUSDT", -2.0, 50e6),   # mid   : net -2.0-0.5-0.3 = -2.80
        (3, "MICROUSDT", 4.0, 1e6),   # micro : net 4.0-0.5-0.8 = 2.70
        (4, "GEENDATAUSDT", 1.0, 0),  # fallback mid: 1.0-0.8 = 0.20
    ]
    conn = FakeConn(rows)
    n = mc.apply_costs(conn)
    assert n == 4 and conn.altered and conn.commits >= 1
    assert conn.updates[1] == (0.50, 0.10, pytest.approx(1.40))
    assert conn.updates[2] == (0.50, 0.30, pytest.approx(-2.80))
    assert conn.updates[3] == (0.50, 0.80, pytest.approx(2.70))
    assert conn.updates[4] == (0.50, 0.30, pytest.approx(0.20))
    # tweede run: alles al gevuld -> 0 updates (idempotent)
    assert mc.apply_costs(conn) == 0
