#!/usr/bin/env python3
"""FAITHFUL 4h-BACKTEST van FABER / ROTATIE / DONCHIAN — exact de live-parameters
uit research/strat_shadow.py, op historische Bitvavo-4h-data.

- DONCHIAN : 55x4h-high-breakout + regime (BTC>200d) + volume>1.5x SMA(20) +
             liquiditeit >=100k; stop entry-3xATR(14); exit chandelier(hh-3xATR)
             of close < 20-bar-low.
- ROTATIE  : wekelijks (42x4h) top-4 op 168x4h (28d) rendement, trend-MA 360x4h
             (60d), liquiditeit >=250k, regime-gate.
- FABER    : BTC/ETH boven 200d-SMA (daily; inherent dagelijks).

Realistische kosten (fee 0,5% rt + getrapte spread). Read-only (Bitvavo publieke
API). Survivorship-bias: alleen nu-liquide coins -> reeel iets lager.
"""
import time
import json
import urllib.request
from datetime import datetime, timezone, timedelta

BITVAVO = "https://api.bitvavo.com/v2"
TOP_N = 40
DAYS_4H = 400              # 4h-historie
DAYS_D = 800              # daily-historie (regime + FABER)
MAJORS = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LINK", "AVAX", "DOT", "POL"}
SKIP = {"EUR", "USDT", "USDC", "EURT", "DAI", "TUSD", "FDUSD", "PYUSD", "EURC"}
FEE_RT = 0.50
DON_N = 55
DON_EXIT_N = 20
ATR_MULT = 3.0
VOL_MULT = 1.5
ROT_LOOKBACK = 168        # 28d x 6
ROT_TREND = 360           # 60d x 6
ROT_STEP = 42             # 7d x 6 (wekelijks)
ROT_TOP = 4
ROT_LIQ = 250_000.0
DON_LIQ = 100_000.0
FOUR_H_MS = 4 * 3600 * 1000


def get(path):
    for _ in range(3):
        try:
            return json.load(urllib.request.urlopen(f"{BITVAVO}{path}", timeout=20))
        except Exception:
            time.sleep(1)
    return None


def now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def daily(market, days=DAYS_D):
    start = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    d = get(f"/{market}/candles?interval=1d&limit=1000&start={start}")
    if not d:
        return []
    rows = sorted([(c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])) for c in d], key=lambda x: x[0])
    if rows and now_ms() - rows[-1][0] < 86_400_000:
        rows.pop()
    return rows


def hist_4h(market, days=DAYS_4H):
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    acc = {}
    end = now_ms()
    for _ in range(12):
        d = get(f"/{market}/candles?interval=4h&limit=1000&end={end}")
        if not d:
            break
        ts_all = []
        for c in d:
            t = c[0]
            ts_all.append(t)
            if t >= start_ms:
                acc[t] = (t, float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5]))
        oldest = min(ts_all)
        if oldest <= start_ms or len(d) < 1000:
            break
        end = oldest - 1
        time.sleep(0.12)
    rows = [acc[k] for k in sorted(acc)]
    if rows and now_ms() - rows[-1][0] < FOUR_H_MS:
        rows.pop()
    return rows


def sma(xs, p, i):
    return sum(xs[i - p + 1:i + 1]) / p if i >= p - 1 else None


def atr(rows, p, i):
    if i < p:
        return None
    s = 0.0
    for k in range(i - p + 1, i + 1):
        h, lo, pc = rows[k][2], rows[k][3], rows[k - 1][4]
        s += max(h - lo, abs(h - pc), abs(lo - pc))
    return s / p


def vol24(rows, i):
    return sum(rows[k][5] * rows[k][4] for k in range(max(0, i - 5), i + 1))


def cost_rt(base, v):
    if base in MAJORS:
        spread = 0.10
    elif v and 0 < v < 5_000_000:
        spread = 0.80
    else:
        spread = 0.30
    return FEE_RT + spread


def stats(trades):
    if not trades:
        return dict(n=0)
    nets = [t["net"] for t in trades]
    wins = [x for x in nets if x > 0]
    los = [x for x in nets if x <= 0]
    return dict(n=len(nets), win=len(wins), los=len(los),
                winrate=round(100 * len(wins) / len(nets), 1),
                gem_win=round(sum(wins) / len(wins), 2) if wins else 0,
                gem_los=round(sum(los) / len(los), 2) if los else 0,
                som=round(sum(nets), 1), per_trade=round(sum(nets) / len(nets), 3))


def liquid_universe(top=TOP_N):
    data = get("/ticker/24h") or []
    eur = []
    for x in data:
        mkt = x.get("market", "")
        if not mkt.endswith("-EUR"):
            continue
        base = mkt.split("-")[0]
        if base in SKIP:
            continue
        try:
            qv = float(x.get("volumeQuote") or 0)
        except Exception:
            qv = 0.0
        eur.append((base, mkt, qv))
    eur.sort(key=lambda z: -z[2])
    return eur[:top]


def bouw_regime(btc):
    reg = {}
    closes = [r[4] for r in btc]
    for i in range(len(btc)):
        s = sma(closes, 200, i)
        d = datetime.fromtimestamp(btc[i][0] / 1000, tz=timezone.utc).date().isoformat()
        reg[d] = (s is not None and closes[i] > s)
    return reg


def regime_op(reg, ts):
    return reg.get(datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat(), False)


def bt_faber(dseries):
    trades = []
    for base in ("BTC", "ETH"):
        rows = dseries.get(base)
        if not rows:
            continue
        closes = [r[4] for r in rows]
        pos = None
        for i in range(200, len(rows)):
            above = closes[i] > sma(closes, 200, i)
            if pos is None and above:
                pos = closes[i]
            elif pos is not None and not above:
                trades.append({"net": (closes[i] - pos) / pos * 100 - cost_rt(base, 9e9)})
                pos = None
        if pos is not None:
            trades.append({"net": (closes[-1] - pos) / pos * 100 - cost_rt(base, 9e9)})
    return trades


def bt_donchian(series4, reg):
    trades = []
    for base, rows in series4.items():
        if len(rows) < DON_N + 25:
            continue
        pos = None
        for i in range(DON_N + 21, len(rows)):
            t, o, h, l, c, v = rows[i]
            if pos is None:
                prior_high = max(x[2] for x in rows[i - DON_N:i])
                vavg = sma([x[5] for x in rows], 20, i - 1)
                a = atr(rows, 14, i)
                v24 = vol24(rows, i)
                if (regime_op(reg, t) and c > prior_high and vavg and rows[i][5] > VOL_MULT * vavg
                        and a and a > 0 and v24 >= DON_LIQ):
                    pos = {"entry": c, "atr": a, "hh": c, "v": v24}
            else:
                pos["hh"] = max(pos["hh"], h)
                trail = pos["hh"] - ATR_MULT * pos["atr"]
                low_n = min(x[3] for x in rows[max(0, i - DON_EXIT_N):i])
                exitp = None
                if l <= trail:
                    exitp = min(o, trail) if o < trail else trail
                elif c < low_n:
                    exitp = c
                if exitp is not None:
                    trades.append({"net": (exitp - pos["entry"]) / pos["entry"] * 100 - cost_rt(base, pos["v"])})
                    pos = None
        if pos is not None:
            trades.append({"net": (rows[-1][4] - pos["entry"]) / pos["entry"] * 100 - cost_rt(base, pos["v"])})
    return trades


def bt_rotatie(series4, reg):
    # gemeenschappelijk tijd-grid = BTC-4h-timestamps
    grid = [r[0] for r in series4["BTC"]]
    close_at = {b: {r[0]: r[4] for r in rows} for b, rows in series4.items()}
    vol_at = {b: {r[0]: vol24(rows, i) for i, r in enumerate(rows)} for b, rows in series4.items()}
    ma_at = {}
    for b, rows in series4.items():
        cl = [r[4] for r in rows]
        ma_at[b] = {rows[i][0]: sma(cl, ROT_TREND, i) for i in range(len(rows))}
    coins = [b for b in series4 if b not in ("BTC", "ETH")]
    holdings = {}
    trades = []
    for gi in range(ROT_LOOKBACK, len(grid), ROT_STEP):
        ts = grid[gi]
        ts0 = grid[gi - ROT_LOOKBACK]
        targets = []
        if regime_op(reg, ts):
            scored = []
            for b in coins:
                px = close_at[b].get(ts)
                px0 = close_at[b].get(ts0)
                v = vol_at[b].get(ts)
                if px is None or px0 is None or px0 <= 0 or not v or v < ROT_LIQ:
                    continue
                ma = ma_at[b].get(ts)
                mom = px / px0 - 1
                if mom > 0 and (ma is None or px > ma):
                    scored.append((mom, b, px, v))
            scored.sort(reverse=True)
            targets = scored[:ROT_TOP]
        tset = {b for _, b, _, _ in targets}
        for b in list(holdings):
            if b not in tset:
                px = close_at[b].get(ts)
                if px:
                    v = vol_at[b].get(ts)
                    trades.append({"net": (px - holdings[b]) / holdings[b] * 100 - cost_rt(b, v)})
                del holdings[b]
        for _, b, px, v in targets:
            holdings.setdefault(b, px)
    return trades


def main():
    print("Universum ophalen...", flush=True)
    uni = liquid_universe()
    print(f"  top-{len(uni)} liquide coins", flush=True)
    dseries = {"BTC": daily("BTC-EUR"), "ETH": daily("ETH-EUR")}
    reg = bouw_regime(dseries["BTC"])
    bull = sum(1 for v in reg.values() if v)
    print(f"  regime: {bull}/{len(reg)} dagen bull (BTC>200d)", flush=True)
    print("4h-historie ophalen (dit duurt even, paginatie)...", flush=True)
    series4 = {}
    series4["BTC"] = hist_4h("BTC-EUR")
    for base, mkt, _ in uni:
        if base in series4:
            continue
        r = hist_4h(mkt)
        if len(r) > (DON_N + 25):
            series4[base] = r
    print(f"  {len(series4)} coins met 4h-historie ({len(series4['BTC'])} candles/coin ~)\n", flush=True)

    res = {"FABER": stats(bt_faber(dseries)),
           "DONCHIAN": stats(bt_donchian(series4, reg)),
           "ROTATIE": stats(bt_rotatie(series4, reg))}
    print(f"{'STRATEGIE':<11}{'trades':>7}{'W/L':>10}{'winrate':>9}{'gem.W':>8}{'gem.V':>8}{'som%':>9}{'/trade':>9}")
    for nm, s in res.items():
        if s.get("n", 0) == 0:
            print(f"{nm:<11}{'0':>7}   (geen trades)")
            continue
        print(f"{nm:<11}{s['n']:>7}{str(s['win'])+'/'+str(s['los']):>10}{str(s['winrate'])+'%':>9}"
              f"{'+'+str(s['gem_win']):>8}{str(s['gem_los']):>8}{('+' if s['som']>=0 else '')+str(s['som']):>9}"
              f"{('+' if s['per_trade']>=0 else '')+str(s['per_trade'])+'%':>9}")
    print(f"\nFaithful 4h · ~{DAYS_4H} dagen · live-parameters uit strat_shadow.py · kosten fee {FEE_RT}%+spread rt.")
    print("Survivorship-bias (alleen nu-liquide coins) -> reeel iets lager.")


if __name__ == "__main__":
    main()
