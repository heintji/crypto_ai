#!/usr/bin/env python3
"""COMBI-SHADOW — DONCHIAN (3 plekken) + MR (2 plekken) als EEN portefeuille op
Bitvavo-EUR, met virtueel kapitaal in euro's. GEEN echt geld, GEEN orders.

Waarom deze combinatie: uit de meting over 30 dagen (21-9-2026) kwam dat de twee
elkaar aanvullen — DONCHIAN wint zelden maar groot (30% winrate, winnaars ~3,5x de
verliezers), MR wint vaak maar klein (59% winrate, netto bijna nul na kosten).
Gescheiden plekken zijn noodzakelijk: in een gedeelde pot verdringen MR's vele
signalen precies de zeldzame DONCHIAN-winnaar (gemeten: -13% i.p.v. +76%).

Regels (identiek aan de bestaande shadows, zodat vergelijken eerlijk blijft):
  DONCHIAN — entry: close > hoogste high van de 55 voorgaande 4h-bars EN
             bar-volume > 1,5x SMA20(volume) EN 24h-quotevolume >= 100k EUR EN
             BTC > 200d-SMA. Stop = close - 3xATR14, daarna chandelier-trailing
             (hoogste high - 3xATR_entry). Exit ook bij close < laagste low van
             de 20 voorgaande bars.
  MR       — entry: RSI14 < 25 op de laatste AFGESLOTEN 4h-bar (geen regime-gate,
             zoals mr_shadow). Stop = entry - 3xATR14, doel = entry + 1,5xATR14,
             tijd-exit na 12 bars (48u).

Portefeuille: max 3 DONCHIAN + 2 MR tegelijk, nooit twee posities in dezelfde
munt, inleg = eigen vermogen / 5 begrensd op de vrije kas. Kosten via
mr_trail_cost (fee per kant + getrapte spread). Kas staat in bot_state.

Draaien:
  DRY=1 .venv/bin/python research/combi_shadow.py   # alles berekenen, niets opslaan
        .venv/bin/python research/combi_shadow.py   # schaduw-administratie bijwerken
Env: DATABASE_URL, COMBI_START_EUR (default 1000).
"""
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
PROJECT_ROOT = os.path.dirname(HERE)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import mr_trail_cost as costmod
import strat_shadow as ss

TABEL = "combi_shadow_trades"
PLEKKEN = {"DONCHIAN": 3, "MR": 2}
START_EUR = float(os.environ.get("COMBI_START_EUR", "1000"))
KAS_KEY = "combi_shadow_kas"
RUN_KEY = "combi_shadow_last_run"

DONCHIAN_N = ss.DONCHIAN_N          # 55
DONCHIAN_EXIT_N = ss.DONCHIAN_EXIT_N  # 20
ATR_MULT = ss.ATR_MULT              # 3.0
VOL_MULT = ss.VOL_MULT              # 1.5
LIQ_MIN_EUR = ss.LIQ_MIN_EUR        # 100_000
MR_RSI = 25.0
MR_TARGET_ATR = 1.5
MR_MAX_HOLD = 12


def ensure_table(cur):
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABEL} (
            id          SERIAL PRIMARY KEY,
            strategie   TEXT NOT NULL,
            coin        TEXT NOT NULL,
            entry_ts    TIMESTAMPTZ NOT NULL,
            entry       DOUBLE PRECISION NOT NULL,
            stop        DOUBLE PRECISION,
            target      DOUBLE PRECISION,
            atr_entry   DOUBLE PRECISION,
            inleg_eur   DOUBLE PRECISION NOT NULL,
            status      TEXT NOT NULL DEFAULT 'OPEN',
            exit_ts     TIMESTAMPTZ,
            exit_prijs  DOUBLE PRECISION,
            exit_reden  TEXT,
            pnl_pct     DOUBLE PRECISION,
            fee_pct     DOUBLE PRECISION,
            spread_pct  DOUBLE PRECISION,
            pnl_net_pct DOUBLE PRECISION,
            pnl_eur     DOUBLE PRECISION,
            vol24_eur   DOUBLE PRECISION,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (strategie, coin, entry_ts)
        )""")


# ------------------------------------------------------------------ kas
def kas_get(cur):
    v = ss.state_get(cur, KAS_KEY)
    if v is None:
        return START_EUR
    try:
        return float(v)
    except (TypeError, ValueError):
        return START_EUR


def kas_set(cur, bedrag):
    ss.state_set(cur, KAS_KEY, f"{bedrag:.4f}")


def open_posities(cur):
    cur.execute(f"""SELECT id, strategie, coin, entry_ts, entry, stop, target, atr_entry,
                           inleg_eur, vol24_eur FROM {TABEL} WHERE status='OPEN'
                    ORDER BY entry_ts""")
    return [dict(zip(("id", "strategie", "coin", "entry_ts", "entry", "stop", "target",
                      "atr_entry", "inleg_eur", "vol24_eur"), r)) for r in cur.fetchall()]


def sluit(cur, cfg, pos, exit_p, exit_ts, reden):
    """Sluit een positie af, boek de netto-euro's terug op de kas."""
    entry = pos["entry"]
    pnl = (exit_p - entry) / entry * 100
    fee, spread, net = costmod.compute_costs(pnl, pos["coin"], pos["vol24_eur"] or 0, cfg)
    pnl_eur = pos["inleg_eur"] * net / 100.0
    status = "WIN" if net > 0 else "LOSS"
    cur.execute(f"""UPDATE {TABEL} SET status=%s, exit_prijs=%s, exit_ts=%s, exit_reden=%s,
                    pnl_pct=%s, fee_pct=%s, spread_pct=%s, pnl_net_pct=%s, pnl_eur=%s
                    WHERE id=%s AND status='OPEN'""",
                (status, exit_p, exit_ts, reden, round(pnl, 4), fee, spread, net,
                 round(pnl_eur, 4), pos["id"]))
    if cur.rowcount:
        kas_set(cur, kas_get(cur) + pos["inleg_eur"] + pnl_eur)
    return cur.rowcount


# ------------------------------------------------------------------ exits
def _donchian_exit(pos, rows):
    """Chandelier-trailing + close onder 20-bar-low. Zelfde volgorde als
    strat_shadow.run_donchian: eerst stoppen tegen de trail van VOOR deze bar."""
    atr_e = pos["atr_entry"]
    if not atr_e and pos["stop"] and pos["entry"] > pos["stop"]:
        atr_e = (pos["entry"] - pos["stop"]) / ATR_MULT
    hoogste = pos["entry"]
    trail = pos["stop"] if pos["stop"] else pos["entry"]
    for i, r in enumerate(rows):
        t, o, h, l, c = r[0], r[1], r[2], r[3], r[4]
        if t <= pos["entry_ts"]:
            continue
        if l <= trail:
            return (o if o < trail else trail), t, "TRAIL_STOP"
        hoogste = max(hoogste, h)
        if atr_e:
            trail = max(trail, hoogste - ATR_MULT * atr_e)
        eerdere = [x for x in rows[:i] if x[0] > pos["entry_ts"] - timedelta(days=30)]
        venster = eerdere[-DONCHIAN_EXIT_N:] if eerdere else []
        if venster and c < min(x[3] for x in venster):
            return c, t, "ONDER_20LOW"
    return None


def _mr_exit(pos, rows):
    """Stop voor doel binnen dezelfde bar; tijd-exit na 12 bars (mr_shadow)."""
    n = 0
    for r in rows:
        t, o, h, l, c = r[0], r[1], r[2], r[3], r[4]
        if t <= pos["entry_ts"]:
            continue
        n += 1
        if l <= pos["stop"]:
            return pos["stop"], t, "STOP"
        if pos["target"] and h >= pos["target"]:
            return pos["target"], t, "TARGET"
        if n >= MR_MAX_HOLD:
            return c, t, "TIJD"
    return None


def run_exits(cur, cfg, candles):
    dicht = 0
    for pos in open_posities(cur):
        rows = candles.get(pos["coin"])
        if not rows:
            continue
        uit = _donchian_exit(pos, rows) if pos["strategie"] == "DONCHIAN" else _mr_exit(pos, rows)
        if uit:
            dicht += sluit(cur, cfg, pos, uit[0], uit[1], uit[2])
    return dicht


# ------------------------------------------------------------------ entries
def _signalen(candles, regime_ok):
    """Alle geldige entry-signalen op de laatste afgesloten bar, meest liquide eerst."""
    uit = []
    for coin, rows in candles.items():
        if len(rows) < DONCHIAN_N + 21:
            continue
        i = len(rows) - 1
        laatste = rows[i]
        v24 = ss.vol24(rows, i)
        if v24 < LIQ_MIN_EUR:
            continue
        a = ss.atr(rows[:i + 1], 14)
        if not a or a <= 0:
            continue
        sluitprijs = laatste[4]
        if regime_ok:
            hoogste = max(x[2] for x in rows[i - DONCHIAN_N:i])
            vgem = ss.sma([x[5] for x in rows[:i]], 20)
            if vgem and sluitprijs > hoogste and laatste[5] > VOL_MULT * vgem:
                uit.append({"strategie": "DONCHIAN", "coin": coin, "entry_ts": laatste[0],
                            "entry": sluitprijs, "stop": sluitprijs - ATR_MULT * a,
                            "target": None, "atr": a, "v24": v24})
        r = ss.rsi([x[4] for x in rows[:i + 1]], 14)
        if r is not None and r < MR_RSI:
            uit.append({"strategie": "MR", "coin": coin, "entry_ts": laatste[0],
                        "entry": sluitprijs, "stop": sluitprijs - ATR_MULT * a,
                        "target": sluitprijs + MR_TARGET_ATR * a, "atr": a, "v24": v24})
    uit.sort(key=lambda s: -s["v24"])
    return uit


def run_entries(cur, candles, regime_ok):
    posities = open_posities(cur)
    bezet = {p["coin"] for p in posities}
    per_strat = {s: sum(1 for p in posities if p["strategie"] == s) for s in PLEKKEN}
    kas = kas_get(cur)
    vermogen = kas + sum(p["inleg_eur"] for p in posities)
    nieuw = 0
    for sig in _signalen(candles, regime_ok):
        if sig["coin"] in bezet:
            continue
        if per_strat.get(sig["strategie"], 0) >= PLEKKEN[sig["strategie"]]:
            continue
        inleg = min(vermogen / sum(PLEKKEN.values()), kas)
        if inleg < 5.0:                      # onder de minimale ordergrootte van Bitvavo
            continue
        cur.execute(f"""INSERT INTO {TABEL}
            (strategie, coin, entry_ts, entry, stop, target, atr_entry, inleg_eur, vol24_eur)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (strategie, coin, entry_ts) DO NOTHING""",
                    (sig["strategie"], sig["coin"], sig["entry_ts"], sig["entry"], sig["stop"],
                     sig["target"], sig["atr"], round(inleg, 4), sig["v24"]))
        if cur.rowcount:
            nieuw += 1
            kas -= inleg
            bezet.add(sig["coin"])
            per_strat[sig["strategie"]] = per_strat.get(sig["strategie"], 0) + 1
            kas_set(cur, kas)
    return nieuw


# ------------------------------------------------------------------ main
def main():
    dry = bool(os.environ.get("DRY"))
    conn = ss.db()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            ensure_table(cur)
            if not dry:
                laatste = ss.state_get(cur, RUN_KEY)
                if laatste:
                    try:
                        if (ss.now_utc() - datetime.fromisoformat(laatste)).total_seconds() < 55 * 60:
                            ss.log("throttle: <1u sinds vorige run — overslaan")
                            conn.rollback()
                            return
                    except ValueError:
                        pass
            cfg = costmod.load_cfg(cur)
            cfg["majors"] = set(cfg["majors"]) | {m.replace("USDT", "") for m in cfg["majors"]}
            conn.commit()

            universum = ss.liquid_universe()
            regime = ss.btc_regime()
            ss.log(f"{len(universum)} liquide munten | BTC>200d-SMA: {regime} | DRY={dry}")

            # munten met een open positie die uit de top-40 vielen apart bijhalen,
            # anders kan hun exit nooit resolven
            exit_universum = dict(universum)
            for pos in open_posities(cur):
                if pos["coin"] not in exit_universum:
                    rows = ss.fetch_4h(pos["coin"] + "-EUR")
                    if rows:
                        exit_universum[pos["coin"]] = rows

            dicht = run_exits(cur, cfg, exit_universum)
            nieuw = run_entries(cur, universum, bool(regime))

            posities = open_posities(cur)
            kas = kas_get(cur)
            ss.log(f"COMBI: {nieuw} nieuw / {dicht} gesloten | open {len(posities)} "
                   f"| kas EUR {kas:.2f} | ingelegd EUR {sum(p['inleg_eur'] for p in posities):.2f}")
            if dry:
                conn.rollback()
                ss.log("DRY: rollback (niets weggeschreven)")
            else:
                ss.state_set(cur, RUN_KEY, ss.now_utc().isoformat())
                conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
