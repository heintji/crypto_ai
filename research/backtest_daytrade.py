#!/usr/bin/env python3
"""DAYTRADE-STRATEGIEEN BACKTEST op Gate 1h-data — de beste kandidaten uit het
Fable-onderzoek (West + Azie), eerlijk getest met echte kosten om de winnaar te
kiezen (hoogste winst x winrate na kosten). Read-only, publieke Gate-API, geen key.

Kandidaten (West+Azie convergeren op een paar):
  RSI2      — Connors RSI(2)<10 pullback + SMA200-trendfilter + ATR%-filter (MR, hoge wr)
  VBREAK    — Larry Williams / Koreaanse volatility-breakout: koop day_open+0,5xprev_range
  VOLBRK_CN — Chinese 涨停-stijl volume-klimax-breakout (memecoin-momentum)
  BOLL      — Bollinger 2sigma-3sigma fade naar middenband (MR)
  TD9       — TD Sequential buy-setup 9 + perfection + oversold-filter (MR benchmark)

Methodiek (identiek voor alle, apples-to-apples):
  - Signaal op close van bar i -> entry op OPEN van bar i+1 (geen look-ahead).
  - Kosten: Gate-fee 0,4% rt + getrapte spread (major/mid/micro) = conservatief;
    mean-reversion zou met maker-fills lager kunnen -> netto reeel iets beter.
  - Filters uit het onderzoek: ATR%-minimum (kosten-overleefbaarheid), regime-gate
    (BTC>200d) op breakouts, trendfilter (SMA200) op mean-reversion.
  - SURVIVORSHIP-BIAS: alleen nu-liquide/genoteerde coins -> reeel LAGER.

Filters op universum: tradable, geen st_tag, >=30d genoteerd, geen hefboom-tokens,
top-N op 24h-volume (hergebruikt research/backtest_gate.py).
"""
import time
from datetime import datetime, timezone, timedelta

import backtest_gate as bg   # hergebruik: g(), liquid_universe(), indicatoren, cost_rt, stats, daily, regime

TOP_N = 50
DAYS_1H = 240
ATRP_MIN = 0.008          # ATR(14,1h)/close >= 0,8% (kosten-overleefbaarheid)


def now_s():
    return int(datetime.now(timezone.utc).timestamp())


def hist_1h(pair, days=DAYS_1H):
    """1h-candles (ts_ms,o,h,l,c,base_vol), oplopend, met correcte paginatie."""
    start_s = now_s() - days * 86400
    acc = {}
    to = now_s()
    for _ in range(12):
        d = bg.g("/spot/candlesticks", {"currency_pair": pair, "interval": "1h",
                                        "limit": 1000, "to": to})
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
        rows.pop()             # nog-vormende laatste candle weg
    return rows


# indicatoren uit backtest_gate + lokale stdev
sma, atr, rsi, median, vol24, cost_rt, stats = (
    bg.sma, bg.atr, bg.rsi, bg.median, bg.vol24, bg.cost_rt, bg.stats)


def stdev(cl, p, i):
    if i < p - 1:
        return None
    seg = cl[i - p + 1:i + 1]
    m = sum(seg) / p
    return (sum((x - m) ** 2 for x in seg) / p) ** 0.5


def day_key(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()


def hour_of(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).hour


# ── STRATEGIE 1: RSI(2) pullback (mean-reversion) ────────────────────────────
def bt_rsi2(series):
    trades = []
    for base, rows in series.items():
        cl = [r[4] for r in rows]
        n = len(rows)
        if n < 210:
            continue
        i = 200
        while i < n - 1:
            a = atr(rows, 14, i)
            if not a or a <= 0:
                i += 1
                continue
            if a / cl[i] >= ATRP_MIN and cl[i] > sma(cl, 200, i):
                r2 = rsi(cl, i, 2)
                if r2 is not None and r2 < 10:
                    entry = rows[i + 1][1]
                    stop = entry - 3 * a
                    res = None
                    jexit = i + 1
                    for k, j in enumerate(range(i + 1, n), start=1):
                        lo, c = rows[j][3], rows[j][4]
                        if lo <= stop:
                            res = (stop - entry) / entry * 100; jexit = j; break
                        r2j = rsi(cl, j, 2)
                        if c > sma(cl, 5, j) or (r2j is not None and r2j > 65):
                            res = (c - entry) / entry * 100; jexit = j; break
                        if k >= 48:
                            res = (c - entry) / entry * 100; jexit = j; break
                    if res is None:
                        jexit = n - 1; res = (cl[-1] - entry) / entry * 100
                    trades.append({"net": res - cost_rt(base, vol24(rows, i))})
                    i = jexit
            i += 1
    return trades


# ── STRATEGIE 2: Volatility Breakout (Williams / Koreaans K=0,5) ──────────────
def _daily_from_1h(rows):
    """Aggregeer 1h -> dag: {date: (open, high, low, close, vol, [1h-indices])}."""
    days = {}
    order = []
    for idx, r in enumerate(rows):
        dk = day_key(r[0])
        if dk not in days:
            days[dk] = [r[1], r[2], r[3], r[4], r[5], [idx]]
            order.append(dk)
        else:
            d = days[dk]
            d[1] = max(d[1], r[2]); d[2] = min(d[2], r[3]); d[3] = r[4]
            d[4] += r[5]; d[5].append(idx)
    return days, order


def bt_vbreak(series, reg=None):
    trades = []
    for base, rows in series.items():
        if len(rows) < 24 * 40:
            continue
        days, order = _daily_from_1h(rows)
        dcloses = [days[d][3] for d in order]
        dvols = [days[d][4] for d in order]
        for di in range(31, len(order) - 1):
            dk = order[di]
            o, hi, lo, c, v, idxs = days[dk]
            if reg is not None and not bg.regime_op(reg, rows[idxs[0]][0]):
                continue                                  # regime-gate (BTC>200d)
            prev = days[order[di - 1]]
            prev_range = prev[1] - prev[2]
            if prev_range <= 0:
                continue
            ma5 = sum(dcloses[di - 5:di]) / 5
            vmed = median(dvols[di - 30:di])
            if not (o > ma5 and prev[4] > vmed):
                continue
            level = o + 0.5 * prev_range
            entry_idx = None
            for idx in idxs:
                if hour_of(rows[idx][0]) > 20:
                    break
                if rows[idx][2] >= level:
                    entry_idx = idx; break
            if entry_idx is None:
                continue
            entry = level
            stop = entry * 0.97
            # exit: volgende dag 00:00 open (time), stop -3%, of close<day_open
            next_open_idx = days[order[di + 1]][5][0]
            res = None
            for j in range(entry_idx + 1, next_open_idx + 1):
                lo_j, c_j = rows[j][3], rows[j][4]
                if lo_j <= stop:
                    res = (stop - entry) / entry * 100; break
                if c_j < o:
                    res = (c_j - entry) / entry * 100; break
                if j == next_open_idx:
                    res = (rows[j][1] - entry) / entry * 100  # exit op open volgende dag
            if res is not None:
                trades.append({"net": res - cost_rt(base, vol24(rows, entry_idx))})
    return trades


# ── STRATEGIE 3: Chinese volume-klimax breakout (memecoin-momentum) ──────────
def bt_volbrk_cn(series, reg=None):
    trades = []
    for base, rows in series.items():
        cl = [r[4] for r in rows]
        n = len(rows)
        if n < 170:
            continue
        i = 168
        while i < n - 1:
            o, h, l, c, v = rows[i][1], rows[i][2], rows[i][3], rows[i][4], rows[i][5]
            if reg is not None and not bg.regime_op(reg, rows[i][0]):
                i += 1; continue                          # regime-gate (BTC>200d)
            hh24 = max(x[2] for x in rows[i - 24:i])
            vavg = sma([x[5] for x in rows], 24, i)
            body = (c - o) / (h - l) if h > l else 0
            ma168 = sma(cl, 168, i)
            if (vavg and c > hh24 and v > 3 * vavg and body > 0.6 and c / o >= 1.02
                    and ma168 and c < 1.25 * ma168):
                entry = rows[i + 1][1]
                stop = max(l, entry * 0.95)
                res = None
                jexit = i + 1
                for k, j in enumerate(range(i + 1, n), start=1):
                    lo_j, c_j = rows[j][3], rows[j][4]
                    if lo_j <= stop:
                        res = (stop - entry) / entry * 100; jexit = j; break
                    trail = min(x[3] for x in rows[max(0, j - 6):j])
                    if k >= 2 and c_j < trail:
                        res = (c_j - entry) / entry * 100; jexit = j; break
                    if k >= 24:
                        res = (c_j - entry) / entry * 100; jexit = j; break
                if res is None:
                    jexit = n - 1; res = (cl[-1] - entry) / entry * 100
                trades.append({"net": res - cost_rt(base, vol24(rows, i))})
                i = jexit
            i += 1
    return trades


# ── STRATEGIE 4: Bollinger 2sigma-3sigma fade (mean-reversion) ───────────────
def bt_boll(series):
    trades = []
    for base, rows in series.items():
        cl = [r[4] for r in rows]
        n = len(rows)
        if n < 210:
            continue
        i = 200
        while i < n - 1:
            mid = sma(cl, 20, i)
            sd = stdev(cl, 20, i)
            if mid and sd and sd > 0 and cl[i] > sma(cl, 200, i):
                lower2, lower3 = mid - 2 * sd, mid - 3 * sd
                if lower3 < cl[i] < lower2 and rows[i][4] < rows[i][1]:
                    entry = rows[i + 1][1]
                    res = None
                    jexit = i + 1
                    for k, j in enumerate(range(i + 1, n), start=1):
                        c_j = rows[j][4]
                        midj = sma(cl, 20, j)
                        sd3 = stdev(cl, 20, j)
                        if sd3 and c_j < (midj - 3 * sd3):
                            res = (c_j - entry) / entry * 100; jexit = j; break
                        if midj and c_j >= midj:
                            res = (c_j - entry) / entry * 100; jexit = j; break
                        if k >= 36:
                            res = (c_j - entry) / entry * 100; jexit = j; break
                    if res is None:
                        jexit = n - 1; res = (cl[-1] - entry) / entry * 100
                    trades.append({"net": res - cost_rt(base, vol24(rows, i))})
                    i = jexit
            i += 1
    return trades


# ── STRATEGIE 5: TD Sequential buy-setup 9 (mean-reversion benchmark) ────────
def bt_td9(series):
    trades = []
    for base, rows in series.items():
        cl = [r[4] for r in rows]
        n = len(rows)
        if n < 175:
            continue
        i = 168
        while i < n - 1:
            setup = all(cl[k] < cl[k - 4] for k in range(i - 8, i + 1))
            if setup:
                perfection = (rows[i - 1][3] < rows[i - 3][3]) or (rows[i][3] < rows[i - 2][3])
                rv = rsi(cl, i, 14)
                ma168 = sma(cl, 168, i)
                if perfection and rv is not None and rv < 35 and ma168 and cl[i] >= 0.85 * ma168:
                    entry = rows[i + 1][1]
                    a = atr(rows, 14, i) or 0
                    lows9 = min(rows[k][3] for k in range(i - 8, i + 1))
                    highs9 = max(rows[k][2] for k in range(i - 8, i + 1))
                    stop = lows9 - 0.5 * a
                    target = min(highs9, entry * 1.06)
                    res = None
                    jexit = i + 1
                    for k, j in enumerate(range(i + 1, n), start=1):
                        h_j, lo_j, c_j = rows[j][2], rows[j][3], rows[j][4]
                        if lo_j <= stop:
                            res = (stop - entry) / entry * 100; jexit = j; break
                        if h_j >= target:
                            res = (target - entry) / entry * 100; jexit = j; break
                        if k >= 12:
                            res = (c_j - entry) / entry * 100; jexit = j; break
                    if res is None:
                        jexit = n - 1; res = (cl[-1] - entry) / entry * 100
                    trades.append({"net": res - cost_rt(base, vol24(rows, i))})
                    i = jexit
            i += 1
    return trades


def main():
    print("Gate-universum ophalen (filters: >=30d, geen hefboom/st_tag, top-50 liquide)...", flush=True)
    uni = bg.liquid_universe(TOP_N)
    print(f"  top-{len(uni)} coins", flush=True)
    print(f"1h-historie ophalen (~{DAYS_1H}d, paginatie, ~3-5 min)...", flush=True)
    series = {}
    for base, pair, _ in uni:
        r = hist_1h(pair)
        if len(r) > 24 * 40:
            series[base] = r
    tot = sum(len(v) for v in series.values())
    print(f"  {len(series)} coins, ~{tot // max(1,len(series))} candles/coin", flush=True)
    btc_d = bg.daily("BTC_USDT")
    reg = bg.bouw_regime(btc_d)
    bull = sum(1 for x in reg.values() if x)
    print(f"  regime: {bull}/{len(reg)} dagen bull (BTC>200d) — _G = regime-gated\n", flush=True)

    res = {"RSI2": stats(bt_rsi2(series)),
           "VBREAK": stats(bt_vbreak(series)),
           "VBREAK_G": stats(bt_vbreak(series, reg)),
           "VOLBRK_CN": stats(bt_volbrk_cn(series)),
           "VOLBRK_CN_G": stats(bt_volbrk_cn(series, reg)),
           "BOLL": stats(bt_boll(series)),
           "TD9": stats(bt_td9(series))}
    print(f"{'STRATEGIE':<11}{'trades':>7}{'W/L':>11}{'winrate':>9}{'gem.W':>8}{'gem.V':>8}{'som%':>10}{'/trade':>9}")
    for nm, s in sorted(res.items(), key=lambda kv: -(kv[1].get("per_trade", -99) if kv[1].get("n") else -99)):
        if s.get("n", 0) == 0:
            print(f"{nm:<11}{'0':>7}   (geen trades)")
            continue
        print(f"{nm:<11}{s['n']:>7}{str(s['win'])+'/'+str(s['los']):>11}{str(s['winrate'])+'%':>9}"
              f"{'+'+str(s['gem_win']):>8}{str(s['gem_los']):>8}{('+' if s['som']>=0 else '')+str(s['som']):>10}"
              f"{('+' if s['per_trade']>=0 else '')+str(s['per_trade'])+'%':>9}")
    print(f"\nGate 1h · ~{DAYS_1H}d · entry op open bar t+1 · kosten Gate-fee 0,4%+spread rt (conservatief).")
    print("Mean-reversion (RSI2/BOLL/TD9) kan met maker-fills reeel iets beter; breakouts incl. taker.")
    print("SURVIVORSHIP-BIAS: alleen nu-liquide coins -> reeel LAGER.")


if __name__ == "__main__":
    main()
