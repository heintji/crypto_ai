#!/usr/bin/env python3
"""FAITHFUL BACKTEST op GATE — dezelfde live-parameters als research/strat_shadow.py /
gate_shadow.py, maar op Gate's BREDERE universum (USDT-paren) en historie. Antwoordt
op de vraag: maakt een breder universum (meer/kleinere coins, memes) de strategieen
beter of juist slechter dan op Bitvavo?

Strategieen: FABER (BTC/ETH>200d), ROTATIE (weekly top-4 28d-momentum, regime-gate),
DONCHIAN (55x4h-breakout + volume + ATR-chandelier), C2V (crash-bounce + kapitulatie,
bear-only).

HARDE filters (gelijk aan gate_shadow): tradable, geen st_tag, listing-leeftijd
>=30d, geen hefboom-tokens (2/3/4/5 L/S), 24h-volumedrempels, top-N op liquiditeit.

Kosten: Gate taker 0,2%/kant = 0,40% rt + getrapte spread (major/mid/micro).
Read-only, publieke Gate-API, geen key. SURVIVORSHIP-BIAS: alleen NU-liquide/
genoteerde coins -> reeel LAGER (gedelischte meme-flops ontbreken volledig).
"""
import time
import requests
from datetime import datetime, timezone, timedelta

GATE = "https://api.gateio.ws/api/v4"
TOP_N = 60
DAYS_4H = 400
DAYS_D = 800
MIN_AGE_DAYS = 30
MAJORS = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LINK", "AVAX", "DOT", "POL"}
SKIP = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "PYUSD", "EURC", "USDE",
        "USDD", "GUSD", "USDP", "BUSD", "EUR"}
_LEV = tuple(f"{n}{d}" for n in ("2", "3", "4", "5") for d in ("L", "S"))
FEE_RT = 0.40             # Gate taker 0,2%/kant
DON_N = 55
DON_EXIT_N = 20
ATR_MULT = 3.0
VOL_MULT = 1.5
ROT_LOOKBACK = 168
ROT_TREND = 360
ROT_STEP = 42
ROT_TOP = 4
ROT_LIQ = 250_000.0
DON_LIQ = 100_000.0
# C2V
C2V_LIQ = 2_000_000.0
C2V_COST_RT = 0.60
C2V_DROP = -0.10
C2V_RSI = 30.0
C2V_CLIMAX = 3.0
C2V_WICK = 0.60
C2V_BTC_FLOOR = -0.02
C2V_ATR_STOP = 1.2
C2V_RR = 1.5
C2V_HOLD = 12


def g(path, params=None):
    for _ in range(3):
        try:
            r = requests.get(f"{GATE}{path}", params=params, timeout=25)
            if r.status_code in (400, 404):
                return None
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(1)
    return None


def now_s():
    return int(datetime.now(timezone.utc).timestamp())


def _norm(c):
    # Gate: [ts_s, quote_vol, close, high, low, open, base_vol, closed]
    # -> (ts_ms, o, h, l, c, base_vol) net als de Bitvavo-backtest
    return (int(c[0]) * 1000, float(c[5]), float(c[3]), float(c[4]), float(c[2]), float(c[6]))


def candles(pair, interval, days):
    per = 6 if interval == "4h" else 1
    want = days * per
    start_s = now_s() - days * 86400
    acc = {}
    to = now_s()
    for _ in range(12):
        d = g("/spot/candlesticks", {"currency_pair": pair, "interval": interval,
                                     "limit": 1000, "to": to})
        if not d:
            break
        for c in d:
            t = int(c[0])
            if t >= start_s:
                acc[t] = _norm(c)
        oldest = min(int(c[0]) for c in d)
        if oldest <= start_s or len(d) < 1000:
            break
        to = oldest - 1
        time.sleep(0.1)
        if len(acc) >= want:
            break
    rows = [acc[k] for k in sorted(acc)]
    # nog-vormende laatste candle weg
    if rows and now_s() * 1000 - rows[-1][0] < (4 * 3600 * 1000 if interval == "4h" else 86_400_000):
        rows.pop()
    return rows


def daily(pair, days=DAYS_D):
    return candles(pair, "1d", days)


def hist_4h(pair, days=DAYS_4H):
    return candles(pair, "4h", days)


# ── indicatoren (index: 0=ts,1=o,2=h,3=l,4=c,5=vol) ──────────────────────────
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


def rsi(cl, i, p=14):
    if i < p:
        return None
    gain = loss = 0.0
    for k in range(i - p + 1, i + 1):
        d = cl[k] - cl[k - 1]
        gain += max(d, 0)
        loss += max(-d, 0)
    return 100 - 100 / (1 + gain / loss) if loss > 0 else 100.0


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def vol24(rows, i):
    return sum(rows[k][5] * rows[k][4] for k in range(max(0, i - 5), i + 1))


def cost_rt(base, v):
    spread = 0.10 if base in MAJORS else (0.80 if (v and 0 < v < 5_000_000) else 0.30)
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


# ── universum + regime ───────────────────────────────────────────────────────
def liquid_universe(top=TOP_N):
    tickers = g("/spot/tickers") or []
    meta = {m["id"]: m for m in (g("/spot/currency_pairs") or [])}
    now = now_s()
    cand = []
    for t in tickers:
        pair = t.get("currency_pair", "")
        if not pair.endswith("_USDT"):
            continue
        base = pair[:-5]
        if base in SKIP or base.endswith(_LEV):
            continue
        m = meta.get(pair)
        if not m or m.get("trade_status") != "tradable" or m.get("st_tag"):
            continue
        bs = int(m.get("buy_start") or 0)
        if bs > 0 and (now - bs) < MIN_AGE_DAYS * 86400:
            continue
        try:
            qv = float(t.get("quote_volume") or 0)
        except (TypeError, ValueError):
            qv = 0.0
        cand.append((base, pair, qv))
    cand.sort(key=lambda z: -z[2])
    return cand[:top]


def bouw_regime(btc):
    reg = {}
    cl = [r[4] for r in btc]
    for i in range(len(btc)):
        s = sma(cl, 200, i)
        d = datetime.fromtimestamp(btc[i][0] / 1000, tz=timezone.utc).date().isoformat()
        reg[d] = (s is not None and cl[i] > s)
    return reg


def regime_op(reg, ts_ms):
    return reg.get(datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat(), False)


# ── strategieen ──────────────────────────────────────────────────────────────
def bt_faber(dseries):
    trades = []
    for base in ("BTC", "ETH"):
        rows = dseries.get(base)
        if not rows:
            continue
        cl = [r[4] for r in rows]
        pos = None
        for i in range(200, len(rows)):
            above = cl[i] > sma(cl, 200, i)
            if pos is None and above:
                pos = cl[i]
            elif pos is not None and not above:
                trades.append({"net": (cl[i] - pos) / pos * 100 - cost_rt(base, 9e9)})
                pos = None
        if pos is not None:
            trades.append({"net": (cl[-1] - pos) / pos * 100 - cost_rt(base, 9e9)})
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


def bt_c2v(series4, reg, btc4):
    """Crash-bounce, bear-only. btc4 = {ts_ms: 4u-return BTC} voor de freefall-filter."""
    trades = []
    for base, rows in series4.items():
        if base == "BTC" or len(rows) < 25:
            continue
        cl = [r[4] for r in rows]
        i = 20
        while i < len(rows):
            t = rows[i][0]
            drop = cl[i] / cl[i - 12] - 1 if i >= 12 else 0
            rv = rsi(cl, i)
            a = atr(rows, 14, i)
            v24 = vol24(rows, i)
            vmed = median([rows[k][5] for k in range(i - 20, i)])
            hi, lo, c = rows[i][2], rows[i][3], cl[i]
            wick = (c - lo) / (hi - lo) if hi > lo else 0
            climax = rows[i][5] >= C2V_CLIMAX * vmed and wick >= C2V_WICK
            btc_ret = btc4.get(t, 0)
            if (not regime_op(reg, t) and drop <= C2V_DROP and rv is not None and rv < C2V_RSI
                    and a and a > 0 and v24 >= C2V_LIQ and climax and btc_ret >= C2V_BTC_FLOOR):
                entry = c
                stopd = C2V_ATR_STOP * a
                stp = entry - stopd
                tgt = entry + C2V_RR * stopd
                res = None
                j = i + 1
                while j < min(i + 1 + C2V_HOLD, len(rows)):
                    if rows[j][3] <= stp:
                        res = (stp - entry) / entry * 100
                        break
                    if rows[j][2] >= tgt:
                        res = (tgt - entry) / entry * 100
                        break
                    j += 1
                if res is None:
                    j = min(i + C2V_HOLD, len(rows) - 1)
                    res = (rows[j][4] - entry) / entry * 100
                trades.append({"net": res - C2V_COST_RT})
                i = j
            i += 1
    return trades


def main():
    print("Gate-universum ophalen (met leeftijd/hefboom/liquiditeit-filters)...", flush=True)
    uni = liquid_universe()
    print(f"  top-{len(uni)} liquide, gerijpte coins", flush=True)
    dseries = {"BTC": daily("BTC_USDT"), "ETH": daily("ETH_USDT")}
    reg = bouw_regime(dseries["BTC"])
    bull = sum(1 for v in reg.values() if v)
    print(f"  regime: {bull}/{len(reg)} dagen bull (BTC>200d, USDT)", flush=True)
    print("4h-historie ophalen (paginatie, ~1-3 min)...", flush=True)
    series4 = {"BTC": hist_4h("BTC_USDT")}
    for base, pair, _ in uni:
        if base in series4:
            continue
        r = hist_4h(pair)
        if len(r) > (DON_N + 25):
            series4[base] = r
    btc4 = {series4["BTC"][k][0]: series4["BTC"][k][4] / series4["BTC"][k - 1][4] - 1
            for k in range(1, len(series4["BTC"]))}
    print(f"  {len(series4)} coins met 4h-historie (~{len(series4['BTC'])} candles/coin)\n", flush=True)

    res = {"FABER": stats(bt_faber(dseries)),
           "DONCHIAN": stats(bt_donchian(series4, reg)),
           "ROTATIE": stats(bt_rotatie(series4, reg)),
           "C2V": stats(bt_c2v(series4, reg, btc4))}
    print(f"{'STRATEGIE':<11}{'trades':>7}{'W/L':>10}{'winrate':>9}{'gem.W':>8}{'gem.V':>8}{'som%':>9}{'/trade':>9}")
    for nm, s in res.items():
        if s.get("n", 0) == 0:
            print(f"{nm:<11}{'0':>7}   (geen trades)")
            continue
        print(f"{nm:<11}{s['n']:>7}{str(s['win'])+'/'+str(s['los']):>10}{str(s['winrate'])+'%':>9}"
              f"{'+'+str(s['gem_win']):>8}{str(s['gem_los']):>8}{('+' if s['som']>=0 else '')+str(s['som']):>9}"
              f"{('+' if s['per_trade']>=0 else '')+str(s['per_trade'])+'%':>9}")
    print(f"\nGate faithful · ~{DAYS_4H}d 4h · live-parameters · kosten fee {FEE_RT}%+spread rt.")
    print("Filters: >=30d genoteerd, geen hefboom/st_tag, top-60 liquide.")
    print("SURVIVORSHIP-BIAS sterk (gedelischte meme-flops ontbreken) -> reeel LAGER.")


if __name__ == "__main__":
    main()
