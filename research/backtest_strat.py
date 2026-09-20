#!/usr/bin/env python3
"""BACKTEST van de 3 nieuwe trend-strategieen (FABER / ROTATIE / DONCHIAN) op
HISTORISCHE Bitvavo-data, zodat we niet hoeven te wachten tot de markt draait.

Gebruikt daily candles (licht + robuust). De live-varianten draaien op 4h; dit is
de daily-benadering van dezelfde ideeen — doel: zien of trend-following op deze
coins werkt over bull EN bear. Realistische kosten (fees + getrapte spread).

- FABER    : hou BTC/ETH zolang close > 200d-SMA, anders cash.
- DONCHIAN : entry op 20d-high-breakout (regime BTC>200d), exit op 10d-low of
             3xATR-chandelier-trailing.
- ROTATIE  : wekelijks top-4 op 28d-rendement uit liquide coins, regime-gate.

Universum = huidige top-N liquide Bitvavo-EUR-coins (LET OP: survivorship-bias —
alleen coins die NU liquide zijn; historische winst is dus geflatteerd).

Read-only: praat alleen met de Bitvavo publieke API. Geen DB, niets wegschrijven.
"""
import time
import json
import urllib.request
from datetime import datetime, timezone, timedelta

BITVAVO = "https://api.bitvavo.com/v2"
TOP_N = 40                 # universum-grootte (liquide coins)
DAYS = 800                 # historie (200d warmup + ~2j tradeable)
MAJORS = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LINK", "AVAX", "DOT", "POL"}
SKIP = {"EUR", "USDT", "USDC", "EURT", "DAI", "TUSD", "FDUSD", "PYUSD", "EURC"}
FEE_RT = 0.50              # 0,25%/kant round-trip
DON_N = 20                 # entry: 20d-high
DON_EXIT_N = 10            # exit: 10d-low
ATR_MULT = 3.0
ROT_TOP = 4
ROT_LOOKBACK = 28          # dagen
ROT_LIQ = 250_000.0
DON_LIQ = 100_000.0


def get(path):
    for _ in range(3):
        try:
            return json.load(urllib.request.urlopen(f"{BITVAVO}{path}", timeout=20))
        except Exception:
            time.sleep(1)
    return None


def daily(market, days=DAYS):
    start = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    d = get(f"/{market}/candles?interval=1d&limit=1000&start={start}")
    if not d:
        return []
    rows = [(c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])) for c in d]
    rows.sort(key=lambda x: x[0])
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if rows and now_ms - rows[-1][0] < 86_400_000:   # laatste = vormende dag
        rows.pop()
    return rows   # (ts,o,h,l,c,v)


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


def cost_rt(base, vol24):
    if base in MAJORS:
        spread = 0.10
    elif vol24 and 0 < vol24 < 5_000_000:
        spread = 0.80
    else:
        spread = 0.30
    return FEE_RT + spread


def stats(trades):
    """trades: lijst van dicts met net_pct."""
    if not trades:
        return dict(n=0)
    nets = [t["net"] for t in trades]
    wins = [x for x in nets if x > 0]
    los = [x for x in nets if x <= 0]
    return dict(
        n=len(nets), win=len(wins), los=len(los),
        winrate=round(100 * len(wins) / len(nets), 1),
        gem_win=round(sum(wins) / len(wins), 2) if wins else 0,
        gem_los=round(sum(los) / len(los), 2) if los else 0,
        som=round(sum(nets), 1),
        per_trade=round(sum(nets) / len(nets), 3),
    )


# ---------------------------------------------------------------- universum
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


# ---------------------------------------------------------------- regime
def bouw_regime(btc):
    """date-string -> BTC boven 200d-SMA (op de close van die dag)."""
    reg = {}
    for i in range(len(btc)):
        s = sma([r[4] for r in btc], 200, i)
        d = datetime.fromtimestamp(btc[i][0] / 1000, tz=timezone.utc).date().isoformat()
        reg[d] = (s is not None and btc[i][4] > s)
    return reg


def regime_op(reg, ts):
    d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat()
    return reg.get(d, False)


# ---------------------------------------------------------------- FABER
def bt_faber(series_map):
    trades = []
    for base in ("BTC", "ETH"):
        rows = series_map.get(base)
        if not rows:
            continue
        closes = [r[4] for r in rows]
        pos = None
        for i in range(200, len(rows)):
            above = closes[i] > sma(closes, 200, i)
            if pos is None and above:
                pos = closes[i]
            elif pos is not None and not above:
                pnl = (closes[i] - pos) / pos * 100
                trades.append({"net": pnl - cost_rt(base, 9e9)})
                pos = None
        if pos is not None:
            pnl = (closes[-1] - pos) / pos * 100
            trades.append({"net": pnl - cost_rt(base, 9e9)})
    return trades


# ---------------------------------------------------------------- DONCHIAN
def bt_donchian(series_map, reg):
    trades = []
    for base, rows in series_map.items():
        if base in ("BTC", "ETH") or len(rows) < DON_N + 25:
            continue
        pos = None
        for i in range(DON_N + 20, len(rows)):
            t, o, h, l, c, v = rows[i]
            vol24 = rows[i][5] * rows[i][4]  # daily quote-volume ~ v*c
            if pos is None:
                prior_high = max(x[2] for x in rows[i - DON_N:i])
                if regime_op(reg, t) and c > prior_high and vol24 >= DON_LIQ:
                    a = atr(rows, 14, i)
                    if a and a > 0:
                        pos = {"entry": c, "atr": a, "hh": c, "base": base, "v": vol24}
            else:
                pos["hh"] = max(pos["hh"], h)
                trail = pos["hh"] - ATR_MULT * pos["atr"]
                low10 = min(x[3] for x in rows[max(0, i - DON_EXIT_N):i])
                exitp = None
                if l <= trail:
                    exitp = min(o, trail) if o < trail else trail
                elif c < low10:
                    exitp = c
                if exitp is not None:
                    pnl = (exitp - pos["entry"]) / pos["entry"] * 100
                    trades.append({"net": pnl - cost_rt(base, pos["v"])})
                    pos = None
        if pos is not None:
            pnl = (rows[-1][4] - pos["entry"]) / pos["entry"] * 100
            trades.append({"net": pnl - cost_rt(base, pos["v"])})
    return trades


# ---------------------------------------------------------------- ROTATIE
def bt_rotatie(series_map, reg):
    # gemeenschappelijk daglijst-grid op basis van BTC-dagen
    btc = series_map["BTC"]
    dagen = [r[0] for r in btc]
    # per coin: dict ts->close (voor snelle lookup) en sorted ts
    idx = {}
    for base, rows in series_map.items():
        idx[base] = {r[0]: r[4] for r in rows}
    coins = [b for b in series_map if b not in ("BTC", "ETH")]
    holdings = {}   # base -> entry_price
    trades = []
    for di in range(ROT_LOOKBACK, len(dagen), 7):   # wekelijks
        ts = dagen[di]
        ts_prev = dagen[di - ROT_LOOKBACK]
        targets = []
        if regime_op(reg, ts):
            scored = []
            for base in coins:
                px = idx[base].get(ts)
                px0 = idx[base].get(ts_prev)
                if px is None or px0 is None or px0 <= 0:
                    continue
                rows = series_map[base]
                # vind index van ts voor volume
                v = None
                for r in reversed(rows):
                    if r[0] == ts:
                        v = r[5] * r[4]
                        break
                if not v or v < ROT_LIQ:
                    continue
                mom = px / px0 - 1
                if mom > 0:
                    scored.append((mom, base, px))
            scored.sort(reverse=True)
            targets = scored[:ROT_TOP]
        target_bases = {b for _, b, _ in targets}
        # sluit posities uit de top
        for base in list(holdings):
            if base not in target_bases:
                px = idx[base].get(ts)
                if px:
                    pnl = (px - holdings[base]) / holdings[base] * 100
                    v = None
                    for r in reversed(series_map[base]):
                        if r[0] == ts:
                            v = r[5] * r[4]; break
                    trades.append({"net": pnl - cost_rt(base, v)})
                del holdings[base]
        # open nieuwe
        for _, base, px in targets:
            if base not in holdings:
                holdings[base] = px
    return trades


# ---------------------------------------------------------------- main
def main():
    print("Universum ophalen...", flush=True)
    uni = liquid_universe()
    print(f"  top-{len(uni)} liquide coins", flush=True)
    series = {}
    print("Historie ophalen (BTC/ETH + universum, daily)...", flush=True)
    for base in ("BTC", "ETH"):
        series[base] = daily(f"{base}-EUR")
        time.sleep(0.1)
    for base, mkt, _ in uni:
        if base not in series:
            r = daily(mkt)
            if len(r) > 250:
                series[base] = r
            time.sleep(0.1)
    print(f"  {len(series)} coins met historie", flush=True)
    reg = bouw_regime(series["BTC"])
    bull = sum(1 for v in reg.values() if v)
    print(f"  regime: {bull}/{len(reg)} dagen bull (BTC>200d)\n", flush=True)

    res = {
        "FABER": stats(bt_faber(series)),
        "DONCHIAN": stats(bt_donchian(series, reg)),
        "ROTATIE": stats(bt_rotatie(series, reg)),
    }
    print(f"{'STRATEGIE':<11}{'trades':>7}{'W/L':>10}{'winrate':>9}{'gem.W':>8}{'gem.V':>8}{'som%':>9}{'/trade':>9}")
    for nm, s in res.items():
        if s.get("n", 0) == 0:
            print(f"{nm:<11}{'0':>7}   (geen trades in de periode)")
            continue
        print(f"{nm:<11}{s['n']:>7}{str(s['win'])+'/'+str(s['los']):>10}{str(s['winrate'])+'%':>9}"
              f"{'+'+str(s['gem_win']):>8}{str(s['gem_los']):>8}{('+' if s['som']>=0 else '')+str(s['som']):>9}"
              f"{('+' if s['per_trade']>=0 else '')+str(s['per_trade'])+'%':>9}")
    print(f"\nPeriode ~{DAYS} dagen · kosten: fee {FEE_RT}% + spread (major .10 / mid .30 / micro .80) rt.")
    print("LET OP: survivorship-bias (alleen nu-liquide coins) -> reeel iets lager.")


if __name__ == "__main__":
    main()
