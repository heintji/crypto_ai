#!/usr/bin/env python3
"""VBREAK v2 — SIMULATIE van de aangepaste volatility-breakout met de consensus-fixes
van Fable5 + Opus5 (beide 100% eens), getest op echte Gate 1h-data.

Fixes t.o.v. v1:
  1. Liquiditeitsvloer 24h-volume >= 2M USDT (v1 liet 25k toe -> microcap-ruis).
  2. Entry pas als een 1h-candle SLUIT boven day_open+0,5*(gisteren-range) + het
     breakout-uur heeft volume > 1,5x gemiddeld uurvolume (echte participatie).
     Instap op de OPEN van de volgende candle (geen look-ahead).
  3. Max 8 nieuwe trades/dag, gerankt op 24h-volume (tegen overtrading/correlatie).
  4. Exit: harde stop -3%; 50% afbouwen op +4%; rest ATR-trailing (chandelier
     hh-2,5*ATR, vloer op break-even na partial); vroege exit als candle < day_open;
     safety time-exit na 5 dagen. (v1 = alles op middernacht.)
  5. Realistische kosten per coin-grootte: 0,5% rt >=10M, 0,9% rt 2-10M.

Vergelijkt v1 (oude regels) en v2 op HETZELFDE >=2M-universum, zodat het effect van
de exit/entry-wijzigingen los van de volumevloer zichtbaar is. Read-only, geen key.

EERLIJK: backtest heeft survivorship-bias (alleen nu-genoteerde coins) + 1 regime.
Shadow forward blijft de echte proef. Dit toont of de aangepaste LOGICA klopt.
"""
import time
from datetime import datetime, timezone

import backtest_gate as bg

DAYS_1H = 220
TOP_N = 80            # ruim ophalen; daarna hard op qv>=2M filteren
LIQ_MIN = 2_000_000.0
K_PER_DAY = 8
VOL_CONFIRM = 1.5    # breakout-uur volume > 1,5x gem(20u)
KFACTOR = 0.5
STOP = 0.03
PARTIAL_TGT = 0.04
ATR_TRAIL = 2.5
MAX_HOLD_H = 120     # 5 dagen safety


def now_s():
    return int(datetime.now(timezone.utc).timestamp())


def hist_1h(pair, days=DAYS_1H):
    start_s = now_s() - days * 86400
    acc, to = {}, now_s()
    for _ in range(12):
        d = bg.g("/spot/candlesticks", {"currency_pair": pair, "interval": "1h", "limit": 1000, "to": to})
        if not d:
            break
        for c in d:
            t = int(c[0])
            if t >= start_s:
                acc[t] = bg._norm(c)
        oldest = min(int(c[0]) for c in d)
        if oldest <= start_s or len(d) < 1000:
            break
        to = oldest - 1
        time.sleep(0.1)
    rows = [acc[k] for k in sorted(acc)]
    if rows and now_s() * 1000 - rows[-1][0] < 3600 * 1000:
        rows.pop()
    return rows


sma, atr = bg.sma, bg.atr


def dkey(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()


def hour_of(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour


def cost_v2(qv):
    return 0.5 if qv >= 10_000_000 else 0.9


def daily_from_1h(rows):
    days, order = {}, []
    for idx, r in enumerate(rows):
        k = dkey(r[0])
        if k not in days:
            days[k] = [r[1], r[2], r[3], r[4], r[5], [idx]]
            order.append(k)
        else:
            d = days[k]
            d[1] = max(d[1], r[2]); d[2] = min(d[2], r[3]); d[3] = r[4]
            d[4] += r[5]; d[5].append(idx)
    return days, order


def stats(trades):
    if not trades:
        return dict(n=0)
    nets = [t for t in trades]
    wins = [x for x in nets if x > 0]
    los = [x for x in nets if x <= 0]
    return dict(n=len(nets), win=len(wins), los=len(los),
                winrate=round(100 * len(wins) / len(nets), 1),
                gemw=round(sum(wins) / len(wins), 2) if wins else 0,
                geml=round(sum(los) / len(los), 2) if los else 0,
                per=round(sum(nets) / len(nets), 3), som=round(sum(nets), 1))


# ── v1 (oude regels) op >=2M-universum ───────────────────────────────────────
def sim_v1(series, reg, qvmap):
    out = []
    for base, rows in series.items():
        days, order = daily_from_1h(rows)
        dcl = [days[d][3] for d in order]
        dvol = [days[d][4] for d in order]
        for di in range(31, len(order) - 1):
            o, hi, lo, c, v, idxs = days[order[di]]
            if not bg.regime_op(reg, rows[idxs[0]][0]):
                continue
            pr = days[order[di - 1]][1] - days[order[di - 1]][2]
            if pr <= 0:
                continue
            if not (o > sum(dcl[di - 5:di]) / 5 and days[order[di - 1]][4] > bg.median(dvol[di - 30:di])):
                continue
            level = o + KFACTOR * pr
            ei = next((i for i in idxs if hour_of(rows[i][0]) <= 20 and rows[i][2] >= level), None)
            if ei is None:
                continue
            entry, stop = level, level * (1 - STOP)
            nxt = days[order[di + 1]][5][0]
            res = None
            for j in range(ei + 1, nxt + 1):
                if rows[j][3] <= stop:
                    res = -STOP * 100
                    break
                if rows[j][4] < o:
                    res = (rows[j][4] - entry) / entry * 100; break
                if j == nxt:
                    res = (rows[j][1] - entry) / entry * 100
            if res is not None:
                out.append(res - cost_v2(qvmap[base]))
    return out


# ── v2 (aangepaste regels) op >=2M-universum, met dag-cap ────────────────────
def sim_v2(series, reg, qvmap):
    signals = []   # (date, -qv, base, entry_idx, entry, day_open, atr)
    for base, rows in series.items():
        days, order = daily_from_1h(rows)
        dcl = [days[d][3] for d in order]
        dvol = [days[d][4] for d in order]
        for di in range(31, len(order) - 1):
            o, hi, lo, c, v, idxs = days[order[di]]
            if not bg.regime_op(reg, rows[idxs[0]][0]):
                continue
            pr = days[order[di - 1]][1] - days[order[di - 1]][2]
            if pr <= 0:
                continue
            if not (o > sum(dcl[di - 5:di]) / 5 and days[order[di - 1]][4] > bg.median(dvol[di - 30:di])):
                continue
            level = o + KFACTOR * pr
            # close-confirm + volumebevestiging, entry op OPEN volgende candle
            sig = None
            for i in idxs:
                if hour_of(rows[i][0]) > 20:
                    break
                if i < 20 or i + 1 >= len(rows):
                    continue
                vavg = sum(rows[k][5] for k in range(i - 20, i)) / 20
                if rows[i][4] >= level and vavg and rows[i][5] > VOL_CONFIRM * vavg:
                    a = atr(rows, 14, i)
                    if a and a > 0:
                        sig = (order[di], -qvmap[base], base, i + 1, rows[i + 1][1], o, a)
                    break
            if sig:
                signals.append(sig)
    # dag-cap: per dag top-K op volume
    signals.sort(key=lambda s: (s[0], s[1]))
    per_day = {}
    kept = []
    for s in signals:
        per_day.setdefault(s[0], 0)
        if per_day[s[0]] < K_PER_DAY:
            per_day[s[0]] += 1
            kept.append(s)
    # simuleer elke gehouden trade
    out = []
    for _, _, base, ei, entry, day_open, a in kept:
        rows = series[base]
        stop = entry * (1 - STOP)
        ptgt = entry * (1 + PARTIAL_TGT)
        hh = entry
        partial = False
        ret_p = 0.0
        res = None
        for n, j in enumerate(rows[ei:], start=1):
            h, l, c = j[2], j[3], j[4]
            if not partial:
                if l <= stop:
                    res = -STOP * 100
                    break
                if c < day_open:
                    res = (c - entry) / entry * 100
                    break
                if h >= ptgt:
                    partial = True
                    ret_p = PARTIAL_TGT * 100
                    hh = max(hh, h)
                    continue
            else:
                hh = max(hh, h)
                trail = max(entry, hh - ATR_TRAIL * a)
                if l <= trail:
                    ret_r = (trail - entry) / entry * 100
                    res = 0.5 * ret_p + 0.5 * ret_r
                    break
            if n >= MAX_HOLD_H:
                if partial:
                    res = 0.5 * ret_p + 0.5 * ((c - entry) / entry * 100)
                else:
                    res = (c - entry) / entry * 100
                break
        if res is None:
            c = rows[-1][4]
            res = (0.5 * ret_p + 0.5 * ((c - entry) / entry * 100)) if partial else ((c - entry) / entry * 100)
        out.append(res - cost_v2(qvmap[base]))
    return out


def main():
    print("Universum ophalen (>=2M USDT volume)...", flush=True)
    uni = [(b, p, q) for b, p, q in bg.liquid_universe(TOP_N) if q >= LIQ_MIN]
    print(f"  {len(uni)} coins >= 2M", flush=True)
    btc = bg.daily("BTC_USDT")
    reg = bg.bouw_regime(btc)
    bull = sum(1 for v in reg.values() if v)
    print(f"  regime: {bull}/{len(reg)} dagen bull", flush=True)
    import os
    import pickle
    cache = f"/private/tmp/claude-501/-Users-hein/fc8ba38b-0aff-4404-98d9-ddbcbf3cb154/scratchpad/vbreak_series_{datetime.now(timezone.utc).date()}.pkl"
    if os.path.exists(cache):
        with open(cache, "rb") as f:
            series, qvmap = pickle.load(f)
        print(f"  {len(series)} coins uit cache\n", flush=True)
    else:
        print(f"1h-historie ophalen (~{DAYS_1H}d, paginatie)...", flush=True)
        series, qvmap = {}, {}
        for b, p, q in uni:
            r = hist_1h(p)
            if len(r) > 24 * 40:
                series[b] = r
                qvmap[b] = q
        with open(cache, "wb") as f:
            pickle.dump((series, qvmap), f)
        print(f"  {len(series)} coins met historie (gecached)\n", flush=True)

    r1 = stats(sim_v1(series, reg, qvmap))
    r2 = stats(sim_v2(series, reg, qvmap))
    print(f"{'VARIANT':<10}{'trades':>7}{'W/L':>10}{'winrate':>9}{'gemW':>8}{'gemV':>8}{'/trade':>9}{'som%':>9}")
    for nm, s in (("v1 (>=2M)", r1), ("v2 (fixes)", r2)):
        if not s.get("n"):
            print(f"{nm:<10}   (geen trades)"); continue
        wl = f"{s['win']}/{s['los']}"
        per = ('+' if s['per'] >= 0 else '') + str(s['per']) + '%'
        som = ('+' if s['som'] >= 0 else '') + str(s['som'])
        print(f"{nm:<10}{s['n']:>7}{wl:>10}{str(s['winrate'])+'%':>9}"
              f"{'+'+str(s['gemw']):>8}{str(s['geml']):>8}{per:>9}{som:>9}")
    print(f"\nGate 1h ~{DAYS_1H}d · >=2M-universum · kosten 0,5%/0,9% rt · entry op open t+1.")
    print("v1 = oude regels, v2 = consensus-fixes (close-confirm+volfilter, dag-cap 8, partial+ATR-trail).")
    print("SURVIVORSHIP-BIAS + 1 bull-regime -> reeel lager; shadow forward blijft de echte proef.")


if __name__ == "__main__":
    main()
