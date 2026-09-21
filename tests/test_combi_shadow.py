"""Bewijs-tests voor research/combi_shadow.py (DONCHIAN 3 + MR 2 als een portefeuille).

Fake-DB, geen netwerk. Bewijst gedrag, niet de tekst van de code:
  1. plekken: een 4e DONCHIAN-signaal komt er niet in, een MR-signaal wel;
  2. nooit twee posities in dezelfde munt, ook niet via de andere strategie;
  3. DONCHIAN sluit op de meegeschoven trailing stop, en op close < 20-bar-low;
  4. MR sluit op stop (stop gaat voor doel in dezelfde bar), op doel, en op tijd;
  5. kas-boekhouding: inleg eraf bij entry, inleg + netto-euro's erbij bij exit;
  6. geen entry als de vrije kas onder de minimale ordergrootte (EUR 5) zakt.
"""
from datetime import datetime, timedelta, timezone

import pytest

from research import combi_shadow as cs

UTC = timezone.utc
T0 = datetime(2026, 9, 1, tzinfo=UTC)
KOLOMMEN = ("id", "strategie", "coin", "entry_ts", "entry", "stop", "target",
            "atr_entry", "inleg_eur", "vol24_eur")


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._res = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.db.executed.append((s, params))
        self._res = []
        self.rowcount = 0
        if s.startswith("CREATE TABLE"):
            return
        if "SELECT value FROM bot_state" in s:
            v = self.db.state.get(params[0])
            self._res = [(v,)] if v is not None else []
        elif "INSERT INTO bot_state" in s:
            self.db.state[params[0]] = params[1]
            self.rowcount = 1
        elif f"FROM {cs.TABEL} WHERE status='OPEN'" in s:
            self._res = [tuple(r[k] for k in KOLOMMEN)
                         for r in self.db.rijen if r["status"] == "OPEN"]
        elif f"INSERT INTO {cs.TABEL}" in s:
            sleutel = (params[0], params[1], params[2])
            if any((r["strategie"], r["coin"], r["entry_ts"]) == sleutel for r in self.db.rijen):
                return
            self.db.rijen.append({
                "id": len(self.db.rijen) + 1, "strategie": params[0], "coin": params[1],
                "entry_ts": params[2], "entry": params[3], "stop": params[4],
                "target": params[5], "atr_entry": params[6], "inleg_eur": params[7],
                "vol24_eur": params[8], "status": "OPEN", "exit_prijs": None,
                "exit_reden": None, "pnl_net_pct": None, "pnl_eur": None})
            self.rowcount = 1
        elif f"UPDATE {cs.TABEL} SET status=" in s:
            tid = params[-1]
            for r in self.db.rijen:
                if r["id"] == tid and r["status"] == "OPEN":
                    (r["status"], r["exit_prijs"], r["exit_ts"], r["exit_reden"],
                     r["pnl_pct"], r["fee_pct"], r["spread_pct"], r["pnl_net_pct"],
                     r["pnl_eur"]) = params[:9]
                    self.rowcount = 1

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return self._res


class FakeDB:
    def __init__(self, kas=1000.0):
        self.rijen = []
        self.state = {cs.KAS_KEY: str(kas)}
        self.executed = []

    def cur(self):
        return FakeCursor(self)


CFG = {"mr_fee_pct_per_side": 0.25, "mr_spread_major_pct": 0.05,
       "mr_spread_mid_pct": 0.15, "mr_spread_micro_pct": 0.40,
       "mr_micro_volume_eur": 5_000_000.0, "majors": {"BTC", "ETH"}}


def bars(n, prijs=100.0, vol=1000.0, start=T0, stap=4):
    """n identieke 4h-bars; (ts, open, high, low, close, volume)."""
    return [(start + timedelta(hours=stap * i), prijs, prijs, prijs, prijs, vol, vol * prijs)
            for i in range(n)]


def donchian_universum(coin="AAA", doorbraak=True):
    """76 vlakke bars + een laatste bar die (optioneel) uitbreekt met volume."""
    rows = bars(76, 100.0, 1000.0)
    laatst = T0 + timedelta(hours=4 * 76)
    if doorbraak:
        rows.append((laatst, 100.0, 120.0, 99.0, 120.0, 5000.0, 5000.0 * 120.0))
    else:
        rows.append((laatst, 100.0, 100.5, 99.5, 100.2, 1000.0, 100_200.0))
    return {coin: rows}


def mr_universum(coin="BBB"):
    """76 bars die gestaag dalen -> RSI ver onder 25 op de laatste bar."""
    rows = []
    prijs = 200.0
    for i in range(77):
        prijs *= 0.97
        rows.append((T0 + timedelta(hours=4 * i), prijs, prijs * 1.001, prijs * 0.999, prijs, 1000.0, 1000.0 * prijs))
    return {coin: rows}


def open_zetten(db, strategie, coin, entry=100.0, stop=90.0, target=None,
                atr=10.0 / 3, inleg=200.0, ts=T0):
    db.rijen.append({"id": len(db.rijen) + 1, "strategie": strategie, "coin": coin,
                     "entry_ts": ts, "entry": entry, "stop": stop, "target": target,
                     "atr_entry": atr, "inleg_eur": inleg, "vol24_eur": 1_000_000.0,
                     "status": "OPEN"})


# ───────────────────────────── 1. plekken ─────────────────────────────
def test_vierde_donchian_komt_er_niet_in_maar_mr_wel():
    db = FakeDB()
    for coin in ("X1", "X2", "X3"):
        open_zetten(db, "DONCHIAN", coin)
    uni = donchian_universum("AAA")
    uni.update(mr_universum("BBB"))
    cur = db.cur()
    cs.run_entries(cur, uni, regime_ok=True)
    nieuw = [r for r in db.rijen if r["coin"] in ("AAA", "BBB")]
    assert [r["strategie"] for r in nieuw] == ["MR"], \
        f"verwacht alleen een MR-entry, kreeg {[(r['strategie'], r['coin']) for r in nieuw]}"


def test_tweede_mr_plek_is_de_laatste():
    db = FakeDB()
    open_zetten(db, "MR", "Y1")
    open_zetten(db, "MR", "Y2")
    cur = db.cur()
    cs.run_entries(cur, mr_universum("BBB"), regime_ok=False)
    assert not [r for r in db.rijen if r["coin"] == "BBB"]


# ───────────────────────── 2. een munt, een positie ─────────────────────────
def test_zelfde_munt_niet_twee_keer_ook_niet_via_andere_strategie():
    db = FakeDB()
    open_zetten(db, "DONCHIAN", "BBB")          # zelfde munt als het MR-signaal
    cur = db.cur()
    cs.run_entries(cur, mr_universum("BBB"), regime_ok=True)
    assert len([r for r in db.rijen if r["coin"] == "BBB"]) == 1


# ───────────────────────────── 3. DONCHIAN-exits ─────────────────────────────
def test_donchian_sluit_op_de_meegeschoven_trail_niet_op_de_oude_stop():
    db = FakeDB()
    open_zetten(db, "DONCHIAN", "AAA", entry=100.0, stop=70.0, atr=10.0)
    rows = [(T0, 100, 100, 100, 100, 1, 1_000_000),
            (T0 + timedelta(hours=4), 100, 200, 100, 190, 1, 1_000_000),   # piek 200 -> trail 170
            (T0 + timedelta(hours=8), 180, 185, 160, 165, 1, 1_000_000)]   # low 160 < trail 170
    cur = db.cur()
    cs.run_exits(cur, CFG, {"AAA": rows})
    r = db.rijen[0]
    assert r["status"] in ("WIN", "LOSS") and r["exit_reden"] == "TRAIL_STOP"
    assert r["exit_prijs"] == pytest.approx(170.0), \
        f"moet op de trail (170) sluiten, niet op de oude stop; kreeg {r['exit_prijs']}"


def test_donchian_sluit_onder_de_20_bar_low():
    db = FakeDB()
    # ruime ATR: de trailing stop blijft ver weg, zodat de 20-bar-low-regel aan bod komt
    open_zetten(db, "DONCHIAN", "AAA", entry=100.0, stop=10.0, atr=30.0)
    rows = [(T0 + timedelta(hours=4 * i), 100, 101, 99, 100, 1, 1_000_000) for i in range(25)]
    rows.append((T0 + timedelta(hours=4 * 25), 100, 100, 98.5, 98.0, 1, 1_000_000))  # close < 20-bar low 99
    cur = db.cur()
    cs.run_exits(cur, CFG, {"AAA": rows})
    assert db.rijen[0]["exit_reden"] == "ONDER_20LOW"


# ───────────────────────────── 4. MR-exits ─────────────────────────────
def test_mr_stop_gaat_voor_doel_in_dezelfde_bar():
    db = FakeDB()
    open_zetten(db, "MR", "BBB", entry=100.0, stop=90.0, target=105.0)
    rows = [(T0, 100, 100, 100, 100, 1, 1_000_000),
            (T0 + timedelta(hours=4), 100, 110, 85, 95, 1, 1_000_000)]   # raakt allebei
    cur = db.cur()
    cs.run_exits(cur, CFG, {"BBB": rows})
    assert db.rijen[0]["exit_reden"] == "STOP"
    assert db.rijen[0]["exit_prijs"] == pytest.approx(90.0)


def test_mr_sluit_op_het_doel():
    db = FakeDB()
    open_zetten(db, "MR", "BBB", entry=100.0, stop=90.0, target=105.0)
    rows = [(T0, 100, 100, 100, 100, 1, 1_000_000),
            (T0 + timedelta(hours=4), 100, 106, 99, 104, 1, 1_000_000)]
    cur = db.cur()
    cs.run_exits(cur, CFG, {"BBB": rows})
    assert db.rijen[0]["exit_reden"] == "TARGET"
    assert db.rijen[0]["exit_prijs"] == pytest.approx(105.0)


def test_mr_tijd_exit_na_twaalf_bars():
    db = FakeDB()
    open_zetten(db, "MR", "BBB", entry=100.0, stop=50.0, target=200.0)
    rows = [(T0, 100, 100, 100, 100, 1, 1_000_000)]
    rows += [(T0 + timedelta(hours=4 * i), 100, 101, 99, 100.0 + i, 1, 1_000_000) for i in range(1, 14)]
    cur = db.cur()
    cs.run_exits(cur, CFG, {"BBB": rows})
    r = db.rijen[0]
    assert r["exit_reden"] == "TIJD"
    assert r["exit_ts"] == rows[12][0], "de tijd-exit hoort op de 12e bar na entry te vallen"


# ───────────────────────────── 5. kas-boekhouding ─────────────────────────────
def test_entry_haalt_inleg_van_de_kas_en_exit_boekt_hem_met_resultaat_terug():
    db = FakeDB(kas=1000.0)
    cur = db.cur()
    cs.run_entries(cur, donchian_universum("AAA"), regime_ok=True)
    kas_na_entry = cs.kas_get(cur)
    inleg = db.rijen[0]["inleg_eur"]
    assert kas_na_entry == pytest.approx(1000.0 - inleg)
    assert inleg == pytest.approx(200.0), "eigen vermogen / 5 plekken"

    pos = db.rijen[0]
    rows = [(pos["entry_ts"], 120, 120, 120, 120, 1, 1_000_000),
            (pos["entry_ts"] + timedelta(hours=4), 120, 120, 60, 60, 1, 1_000_000)]  # stop geraakt
    cs.run_exits(cur, CFG, {"AAA": rows})
    r = db.rijen[0]
    assert r["status"] == "LOSS" and r["pnl_eur"] < 0
    assert cs.kas_get(cur) == pytest.approx(kas_na_entry + inleg + r["pnl_eur"])
    assert cs.kas_get(cur) < 1000.0, "een verlies hoort het vermogen te verlagen"


def test_kosten_worden_van_het_resultaat_afgetrokken():
    db = FakeDB(kas=1000.0)
    open_zetten(db, "MR", "BBB", entry=100.0, stop=90.0, target=101.0, inleg=200.0)
    rows = [(T0, 100, 100, 100, 100, 1, 1_000_000),
            (T0 + timedelta(hours=4), 100, 102, 99, 101, 1, 1_000_000)]
    cur = db.cur()
    cs.run_exits(cur, CFG, {"BBB": rows})
    r = db.rijen[0]
    assert r["pnl_pct"] == pytest.approx(1.0)
    assert r["pnl_net_pct"] < r["pnl_pct"], "netto moet lager zijn dan bruto (fee + spread)"


# ───────────────────────── 6. minimale ordergrootte ─────────────────────────
def test_geen_entry_als_de_vrije_kas_te_klein_is():
    db = FakeDB(kas=3.0)
    cur = db.cur()
    cs.run_entries(cur, donchian_universum("AAA"), regime_ok=True)
    assert not db.rijen, "onder EUR 5 mag er geen order worden aangemaakt"


def test_geen_donchian_entry_zonder_gunstig_regime():
    db = FakeDB()
    cur = db.cur()
    cs.run_entries(cur, donchian_universum("AAA"), regime_ok=False)
    assert not [r for r in db.rijen if r["strategie"] == "DONCHIAN"]
