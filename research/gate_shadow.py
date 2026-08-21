#!/usr/bin/env python3
"""GATE-SHADOW — dezelfde vergelijkings-strategieen als strat_shadow.py, maar op het
BREDERE universum van Gate.com (~2200 USDT-paren i.p.v. ~40 Bitvavo-EUR-markten).

Doel: eerlijk testen of een bredere markt (meer/kleinere coins) de strategieen helpt
OF juist meer verlies oplevert, VOORDAT er echt geld naar Gate gaat. Leest alleen
publieke Gate-marktdata (GEEN API-key, GEEN orders) en schrijft schaduw-trades naar
een EIGEN tabel `gate_shadow_trades`. Raakt de live-bot en de Bitvavo-shadow NIET aan.

HARDE FILTERS (les uit [[project_crypto_2026_08_19]]: het lek zit in illiquide micro-caps):
  - alleen `trade_status=tradable`, geen `st_tag` (Gate-risicovlag)
  - listing-leeftijd >= MIN_AGE_DAYS  -> GEEN net-uit-sniping
  - 24h-quotevolume (USDT) >= liquiditeitsdrempel per strategie
  - top-N meest liquide paren als universum

Strategieen (identiek aan strat_shadow, herbruikt reken-helpers + kostenmodel):
  FABER    — BTC/ETH boven 200d-SMA = hou, anders cash (benchmark).
  ROTATIE  — weekly top-4 op 28d-momentum uit liquide coins, regime-gated.
  DONCHIAN — 55-bar 4h-breakout + volume + ATR-chandelier-trailing, regime-gated.
  C2V      — crash-bounce met kapitulatie-bevestiging + ATR-exits, alleen in bear.

Draaien:
  NODB=1 python research/gate_shadow.py   # zelftest: fetch+filters+signalen, GEEN DB
  DRY=1  python research/gate_shadow.py    # volledige run maar rollback (niets wegschrijven)
         python research/gate_shadow.py    # echte schaduw-administratie (schrijft naar DB)

Env: DATABASE_URL (niet nodig bij NODB). Deps: requests, psycopg2-binary.
"""
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import mr_trail_cost as costmod                 # fees + spread-tier (hergebruikt)
from strat_shadow import sma, atr, rsi, median, vol24, ms_to_ts, now_utc  # pure helpers

GATE = "https://api.gateio.ws/api/v4"
TABLE = "gate_shadow_trades"

# ── filters ──────────────────────────────────────────────────────────────────
MIN_AGE_DAYS = 30            # coin moet >= 30d genoteerd zijn (geen net-uit-sniping)
TOP_N = 60                   # breder universum dan Bitvavo (40); Gate heeft er ~2200
LIQ_MIN = 100_000.0          # algemene 24h-quotevolume-ondergrens (USDT ~ EUR)
ROT_LIQ_MIN = 250_000.0      # strengere grens voor rotatie (grotere posities)
C2V_LIQ = 2_000_000.0        # C2V koopt alleen echt liquide coins

# ── strategie-parameters (gelijk aan strat_shadow) ───────────────────────────
MAJORS_FABER = ["BTC", "ETH"]
DONCHIAN_N = 55
DONCHIAN_EXIT_N = 20
ATR_MULT = 3.0
VOL_MULT = 1.5
ROT_LOOKBACK_BARS = 168      # 28d x 6 (4h)
ROT_TOP = 4
ROT_TREND_MA_BARS = 360      # ~60d trendfilter
MAX_DAYS = 70
FOUR_H = timedelta(hours=4)

C2V_COST_RT = 0.60
C2V_CLIMAX_VOL = 3.0
C2V_WICK_MIN = 0.60
C2V_BTC_FLOOR = -0.02
C2V_DROP = -0.10
C2V_RSI = 30.0
C2V_ATR_STOP = 1.2
C2V_RR = 1.5
C2V_MAX_HOLD = 12

_SKIP_BASE = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "PYUSD", "EURC", "USDE",
              "USDD", "GUSD", "USDP", "BUSD", "EUR"}
# Gate hefboom-/ETF-tokens (BASE + 2L/3L/5L/3S/5S ...): geen echte coins, decay +
# pathologisch gedrag -> uitsluiten. Ook wrapped-varianten weren.
_LEVERAGE_SUFFIX = tuple(f"{n}{d}" for n in ("2", "3", "4", "5") for d in ("L", "S"))


def _is_derivative(base):
    return base.endswith(_LEVERAGE_SUFFIX)


def log(m):
    print(f"[GATE {datetime.now(timezone.utc):%H:%M:%S}] {m}", flush=True)


# ── Gate publieke datalaag (geen key) ────────────────────────────────────────
def gate_get(path, params=None):
    last = None
    for _ in range(3):
        try:
            r = requests.get(f"{GATE}{path}", params=params, timeout=20)
            if r.status_code in (400, 404):
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(1)
    log(f"gate_get fout {path}: {last}")
    return None


def gate_candles(pair, interval, days):
    """Gate 4h/1d-candles -> (ts_dt, o, h, l, c, base_vol, quote_usdt), oplopend.
    Gate-formaat: [ts_s, quote_vol, close, high, low, open, base_vol, closed].
    De nog-vormende laatste candle (closed='false') wordt weggelaten."""
    per = 6 if interval == "4h" else 1
    limit = min(1000, days * per + 20)
    data = gate_get("/spot/candlesticks",
                    {"currency_pair": pair, "interval": interval, "limit": limit})
    if not data:
        return []
    rows = []
    for c in data:
        try:
            closed = (str(c[7]).lower() == "true") if len(c) > 7 else True
            rows.append((ms_to_ts(int(c[0]) * 1000), float(c[5]), float(c[3]),
                         float(c[4]), float(c[2]), float(c[6]), float(c[1]), closed))
        except (ValueError, IndexError):
            continue
    rows.sort(key=lambda x: x[0])
    if rows and not rows[-1][7]:
        rows.pop()
    return [r[:7] for r in rows]         # 'closed'-vlag eraf


def fetch_daily(coin, days=260):
    return gate_candles(f"{coin}_USDT", "1d", days)


def gate_universe(days=MAX_DAYS, top=TOP_N):
    """De top-N liquide, gerijpte, verhandelbare Gate-USDT-paren + hun 4h-candles.
    coin = base-symbool (bv. 'BTC'), zodat het kostenmodel majors herkent."""
    tickers = gate_get("/spot/tickers") or []
    meta = {m["id"]: m for m in (gate_get("/spot/currency_pairs") or [])}
    now_s = now_utc().timestamp()
    cand = []
    for t in tickers:
        pair = t.get("currency_pair", "")
        if not pair.endswith("_USDT"):
            continue
        base = pair[:-5]
        if base in _SKIP_BASE or _is_derivative(base):
            continue
        m = meta.get(pair)
        if not m or m.get("trade_status") != "tradable" or m.get("st_tag"):
            continue
        bs = int(m.get("buy_start") or 0)
        if bs > 0 and (now_s - bs) < MIN_AGE_DAYS * 86400:
            continue                      # te nieuw -> overslaan
        try:
            qv = float(t.get("quote_volume") or 0)
        except (TypeError, ValueError):
            qv = 0.0
        if qv < LIQ_MIN:
            continue
        cand.append((pair, base, qv))
    cand.sort(key=lambda z: -z[2])
    out = {}
    for pair, base, _ in cand[:top]:
        rows = gate_candles(pair, "4h", days)
        if len(rows) >= DONCHIAN_N + 21:
            out[base] = rows
        time.sleep(0.08)
    return out


def btc_regime():
    """(bool BTC boven 200d-SMA, of None bij te weinig data)."""
    rows = fetch_daily("BTC", 260)
    if len(rows) < 201:
        return None
    closes = [r[4] for r in rows]
    ma = sma(closes, 200)
    return (closes[-1] > ma) if ma else None


# ── DB-laag ──────────────────────────────────────────────────────────────────
def db():
    import psycopg2
    url = os.environ["DATABASE_URL"]
    try:
        return psycopg2.connect(url, connect_timeout=15)
    except psycopg2.OperationalError:
        import re
        ext = re.sub(r"@(dpg-[a-z0-9]+-a)/",
                     r"@\1.frankfurt-postgres.render.com/", url)
        if "sslmode" not in ext:
            ext += ("&" if "?" in ext else "?") + "sslmode=require"
        return psycopg2.connect(ext, connect_timeout=15)


def ensure_table(cur):
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            id          SERIAL PRIMARY KEY,
            strategie   TEXT NOT NULL,
            coin        TEXT NOT NULL,
            entry_ts    TIMESTAMPTZ NOT NULL,
            entry       DOUBLE PRECISION NOT NULL,
            stop        DOUBLE PRECISION,
            target      DOUBLE PRECISION,
            status      TEXT NOT NULL DEFAULT 'OPEN',
            exit_ts     TIMESTAMPTZ,
            exit_prijs  DOUBLE PRECISION,
            exit_reden  TEXT,
            pnl_pct     DOUBLE PRECISION,
            fee_pct     DOUBLE PRECISION,
            spread_pct  DOUBLE PRECISION,
            pnl_net_pct DOUBLE PRECISION,
            vol24_usdt  DOUBLE PRECISION,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (strategie, coin, entry_ts)
        )
    """)


def state_get(cur, k):
    cur.execute("SELECT value FROM bot_state WHERE key=%s", (k,))
    r = cur.fetchone()
    return r[0] if r else None


def state_set(cur, k, v):
    cur.execute("""INSERT INTO bot_state(key,value) VALUES(%s,%s)
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""", (k, v))


def close_trade(cur, tid, coin, entry, exit_p, exit_ts, reden, v24, cfg):
    pnl = (exit_p - entry) / entry * 100
    fee, spread, net = costmod.compute_costs(pnl, coin, v24, cfg)
    status = "WIN" if net > 0 else "LOSS"
    cur.execute(f"""UPDATE {TABLE} SET status=%s, exit_prijs=%s, exit_ts=%s,
        exit_reden=%s, pnl_pct=%s, fee_pct=%s, spread_pct=%s, pnl_net_pct=%s WHERE id=%s""",
                (status, exit_p, exit_ts, reden, round(pnl, 4), fee, spread, net, tid))


def _close_c2v(cur, tid, entry, exit_p, exit_ts, reden):
    pnl = (exit_p - entry) / entry * 100
    net = round(pnl - C2V_COST_RT, 4)
    cur.execute(f"""UPDATE {TABLE} SET status=%s, exit_prijs=%s, exit_ts=%s,
        exit_reden=%s, pnl_pct=%s, fee_pct=%s, spread_pct=%s, pnl_net_pct=%s WHERE id=%s""",
                ("WIN" if net > 0 else "LOSS", exit_p, exit_ts, reden, round(pnl, 4),
                 0.30, 0.30, net, tid))


# ── strategieen (zelfde logica als strat_shadow, tabel = gate_shadow_trades) ──
def run_faber(cur, cfg):
    cur.execute(f"SELECT id, coin, entry FROM {TABLE} WHERE strategie='FABER' AND status='OPEN'")
    opens = {r[1]: (r[0], r[2]) for r in cur.fetchall()}
    nieuw = dicht = 0
    for coin in MAJORS_FABER:
        rows = fetch_daily(coin, 260)
        if len(rows) < 201:
            continue
        closes = [r[4] for r in rows]
        ma = sma(closes, 200)
        if not ma:
            continue
        last, last_ts = closes[-1], rows[-1][0]
        above = last > ma
        if coin in opens and not above:
            tid, entry = opens[coin]
            close_trade(cur, tid, coin, entry, last, last_ts, "ONDER_MA200", 1e9, cfg)
            dicht += 1
        elif coin not in opens and above:
            cur.execute(f"""INSERT INTO {TABLE} (strategie,coin,entry_ts,entry,vol24_usdt)
                VALUES ('FABER',%s,%s,%s,%s) ON CONFLICT (strategie,coin,entry_ts) DO NOTHING""",
                        (coin, last_ts, last, 1e9))
            nieuw += cur.rowcount
    return nieuw, dicht


def run_donchian(cur, cfg, regime_ok, candles, exit_candles):
    cur.execute(f"SELECT id, coin, entry_ts, entry, stop FROM {TABLE} WHERE strategie='DONCHIAN' AND status='OPEN'")
    dicht = 0
    for tid, coin, ets, entry, stop in cur.fetchall():
        rows = exit_candles.get(coin)
        if not rows:
            continue
        atr_e = (entry - stop) / ATR_MULT if stop and entry > stop else None
        highest = entry
        trail = stop if stop else entry
        for i, r in enumerate(rows):
            if r[0] <= ets:
                continue
            t, o, h, l, c = r[0], r[1], r[2], r[3], r[4]
            if l <= trail:
                exit_p = o if o < trail else trail
                close_trade(cur, tid, coin, entry, exit_p, t, "TRAIL_STOP", vol24(rows, i), cfg)
                dicht += 1
                break
            highest = max(highest, h)
            if atr_e:
                trail = max(trail, highest - ATR_MULT * atr_e)
            low_n = min(x[3] for x in rows[max(0, i - DONCHIAN_EXIT_N):i]) if i >= 1 else None
            if low_n is not None and c < low_n:
                close_trade(cur, tid, coin, entry, c, t, "ONDER_20LOW", vol24(rows, i), cfg)
                dicht += 1
                break
    nieuw = 0
    if not regime_ok:
        return nieuw, dicht
    cur.execute(f"SELECT coin FROM {TABLE} WHERE strategie='DONCHIAN' AND status='OPEN'")
    open_coins = {r[0] for r in cur.fetchall()}
    for coin, rows in candles.items():
        if coin in open_coins or len(rows) < DONCHIAN_N + 21:
            continue
        i = len(rows) - 1
        last = rows[i]
        prior_high = max(x[2] for x in rows[i - DONCHIAN_N:i])
        vols = [x[5] for x in rows[:i]]
        vavg = sma(vols, 20)
        a = atr(rows[:i + 1], 14)
        if not vavg or not a or a <= 0:
            continue
        v24 = vol24(rows, i)
        if v24 < LIQ_MIN:
            continue
        if last[4] > prior_high and last[5] > VOL_MULT * vavg:
            stop = last[4] - ATR_MULT * a
            cur.execute(f"""INSERT INTO {TABLE} (strategie,coin,entry_ts,entry,stop,vol24_usdt)
                VALUES ('DONCHIAN',%s,%s,%s,%s,%s) ON CONFLICT (strategie,coin,entry_ts) DO NOTHING""",
                        (coin, last[0], last[4], stop, v24))
            nieuw += cur.rowcount
    return nieuw, dicht


def run_rotatie(cur, cfg, regime_ok, candles, exit_candles):
    if not candles:
        return 0, 0
    last = state_get(cur, "gate_rotatie_last_rebalance")
    if last:
        try:
            if (now_utc() - datetime.fromisoformat(last)).days < 6:
                return 0, 0
        except ValueError:
            pass
    targets = []
    if regime_ok:
        scored = []
        for coin, rows in candles.items():
            if len(rows) < ROT_LOOKBACK_BARS + 1:
                continue
            i = len(rows) - 1
            if vol24(rows, i) < ROT_LIQ_MIN:
                continue
            closes = [x[4] for x in rows]
            mom = closes[-1] / closes[-1 - ROT_LOOKBACK_BARS] - 1
            ma = sma(closes, ROT_TREND_MA_BARS) if len(closes) >= ROT_TREND_MA_BARS else None
            if mom > 0 and (ma is None or closes[-1] > ma):
                scored.append((mom, coin, closes[-1], vol24(rows, i)))
        scored.sort(reverse=True)
        targets = scored[:ROT_TOP]
    target_coins = {c for _, c, _, _ in targets}

    cur.execute(f"SELECT id, coin, entry FROM {TABLE} WHERE strategie='ROTATIE' AND status='OPEN'")
    opens = {r[1]: (r[0], r[2]) for r in cur.fetchall()}
    dicht = nieuw = 0
    for coin, (tid, entry) in opens.items():
        if coin not in target_coins:
            rows = exit_candles.get(coin)
            if not rows:
                continue
            i = len(rows) - 1
            close_trade(cur, tid, coin, entry, rows[i][4], rows[i][0], "UIT_TOP", vol24(rows, i), cfg)
            dicht += 1
    for _, coin, px, v in targets:
        if coin not in opens:
            cur.execute(f"""INSERT INTO {TABLE} (strategie,coin,entry_ts,entry,vol24_usdt)
                VALUES ('ROTATIE',%s,%s,%s,%s) ON CONFLICT (strategie,coin,entry_ts) DO NOTHING""",
                        (coin, candles[coin][-1][0], px, v))
            nieuw += cur.rowcount
    state_set(cur, "gate_rotatie_last_rebalance", now_utc().isoformat())
    return nieuw, dicht


def run_c2v(cur, regime, btc_last_ret, universe, exit_candles):
    cur.execute(f"SELECT id, coin, entry_ts, entry, stop, target FROM {TABLE} WHERE strategie='C2V' AND status='OPEN'")
    dicht = 0
    for tid, coin, ets, entry, stop, target in cur.fetchall():
        rows = exit_candles.get(coin)
        if not rows:
            continue
        n = 0
        for r in rows:
            if r[0] <= ets:
                continue
            n += 1
            t, o, h, l, c = r[0], r[1], r[2], r[3], r[4]
            if l <= stop:
                _close_c2v(cur, tid, entry, stop, t, "STOP"); dicht += 1; break
            if h >= target:
                _close_c2v(cur, tid, entry, target, t, "TARGET"); dicht += 1; break
            if n >= C2V_MAX_HOLD:
                _close_c2v(cur, tid, entry, c, t, "TIME"); dicht += 1; break
    nieuw = 0
    if regime is not False:               # entries alleen in bevestigde bear
        return nieuw, dicht
    if btc_last_ret is not None and btc_last_ret < C2V_BTC_FLOOR:
        return nieuw, dicht               # BTC in freefall -> geen mes vangen
    cur.execute(f"SELECT coin FROM {TABLE} WHERE strategie='C2V' AND status='OPEN'")
    open_coins = {r[0] for r in cur.fetchall()}
    for coin, rows in universe.items():
        if coin == "BTC" or coin in open_coins or len(rows) < 25:
            continue
        i = len(rows) - 1
        closes = [r[4] for r in rows]
        drop = closes[i] / closes[i - 12] - 1 if i >= 12 else 0
        r = rsi(closes)
        a = atr(rows, 14)
        v24 = vol24(rows, i)
        vmed = median([rows[k][5] for k in range(i - 20, i)])
        hi, lo, cl = rows[i][2], rows[i][3], closes[i]
        wick = (cl - lo) / (hi - lo) if hi > lo else 0
        climax = rows[i][5] >= C2V_CLIMAX_VOL * vmed and wick >= C2V_WICK_MIN
        if (drop <= C2V_DROP and r is not None and r < C2V_RSI and a and a > 0
                and v24 >= C2V_LIQ and climax):
            stopd = C2V_ATR_STOP * a
            cur.execute(f"""INSERT INTO {TABLE} (strategie,coin,entry_ts,entry,stop,target,vol24_usdt)
                VALUES ('C2V',%s,%s,%s,%s,%s,%s) ON CONFLICT (strategie,coin,entry_ts) DO NOTHING""",
                        (coin, rows[i][0], cl, cl - stopd, cl + C2V_RR * stopd, v24))
            nieuw += cur.rowcount
    return nieuw, dicht


# ── zelftest zonder DB (valideert datalaag + filters + signalen) ─────────────
def selftest():
    log("ZELFTEST (NODB) — Gate-universum ophalen...")
    uni = gate_universe()
    if not uni:
        log("GEEN universum opgehaald — check netwerk/Gate-API"); return
    reg = btc_regime()
    btc = uni.get("BTC") or gate_candles("BTC_USDT", "4h", MAX_DAYS)
    btc_ret = (btc[-1][4] / btc[-2][4] - 1) if btc and len(btc) >= 2 else None
    # top-5 op liquiditeit tonen
    tops = sorted(uni.items(), key=lambda kv: -vol24(kv[1], len(kv[1]) - 1))[:5]
    log(f"{len(uni)} coins door de filters (>= {int(MIN_AGE_DAYS)}d oud, >= {LIQ_MIN:,.0f} USDT/24u)")
    log("top-5 liquiditeit: " + ", ".join(f"{c} {vol24(r, len(r)-1)/1e6:.1f}M" for c, r in tops))
    log(f"BTC > 200d-SMA: {reg} | BTC 4u-return: {btc_ret*100:+.2f}%" if btc_ret is not None else f"regime {reg}")

    # zou-triggeren tellingen (pure checks, geen DB)
    donch = c2v = rot_cand = 0
    scored = []
    for coin, rows in uni.items():
        i = len(rows) - 1
        closes = [x[4] for x in rows]
        a = atr(rows, 14)
        v24 = vol24(rows, i)
        # DONCHIAN
        if len(rows) >= DONCHIAN_N + 21 and a and a > 0 and v24 >= LIQ_MIN:
            prior_high = max(x[2] for x in rows[i - DONCHIAN_N:i])
            vavg = sma([x[5] for x in rows[:i]], 20)
            if vavg and rows[i][4] > prior_high and rows[i][5] > VOL_MULT * vavg:
                donch += 1
        # C2V
        if coin != "BTC" and len(rows) >= 25 and a and a > 0 and v24 >= C2V_LIQ:
            drop = closes[i] / closes[i - 12] - 1 if i >= 12 else 0
            rv = rsi(closes)
            vmed = median([rows[k][5] for k in range(i - 20, i)])
            hi, lo, cl = rows[i][2], rows[i][3], closes[i]
            wick = (cl - lo) / (hi - lo) if hi > lo else 0
            if (drop <= C2V_DROP and rv is not None and rv < C2V_RSI
                    and rows[i][5] >= C2V_CLIMAX_VOL * vmed and wick >= C2V_WICK_MIN):
                c2v += 1
        # ROTATIE-kandidaten
        if len(rows) >= ROT_LOOKBACK_BARS + 1 and v24 >= ROT_LIQ_MIN:
            mom = closes[-1] / closes[-1 - ROT_LOOKBACK_BARS] - 1
            ma = sma(closes, ROT_TREND_MA_BARS) if len(closes) >= ROT_TREND_MA_BARS else None
            if mom > 0 and (ma is None or closes[-1] > ma):
                rot_cand += 1
                scored.append((mom, coin))
    scored.sort(reverse=True)
    log(f"zou NU triggeren: DONCHIAN-breakouts={donch} (regime_ok={bool(reg)}), "
        f"C2V-bounces={c2v} (bear-only, regime bear={reg is False}), ROTATIE-kandidaten={rot_cand}")
    if scored:
        log("ROTATIE top-4 (28d-momentum): " + ", ".join(f"{c} {m*100:+.0f}%" for m, c in scored[:4]))
    log("ZELFTEST OK — datalaag + filters + signaal-logica werken. Geen DB aangeraakt.")


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    if os.environ.get("NODB"):
        selftest()
        return
    dry = bool(os.environ.get("DRY"))
    conn = db()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            ensure_table(cur)
            if not dry:                    # throttle: max ~1x/uur echt werk
                _last = state_get(cur, "gate_shadow_last_run")
                if _last:
                    try:
                        if (now_utc() - datetime.fromisoformat(_last)).total_seconds() < 55 * 60:
                            log("throttle: <1u sinds vorige run — overslaan")
                            conn.rollback(); return
                    except ValueError:
                        pass
            cfg = costmod.load_cfg(cur)
            cfg["majors"] = set(cfg["majors"]) | {m.replace("USDT", "") for m in cfg["majors"]}
            conn.commit()

            universe = gate_universe()
            regime = btc_regime()
            regime_ok = bool(regime)
            log(f"{len(universe)} liquide Gate-coins | BTC>200d-SMA: {regime} | DRY={dry}")

            # coins met OPEN trades die uit de top-N vielen apart bijhalen voor exits
            exit_universe = dict(universe)
            cur.execute(f"""SELECT DISTINCT coin FROM {TABLE}
                           WHERE strategie IN ('DONCHIAN','ROTATIE','C2V') AND status='OPEN'""")
            for (coin,) in cur.fetchall():
                if coin not in exit_universe:
                    rows = gate_candles(f"{coin}_USDT", "4h", MAX_DAYS)
                    if rows:
                        exit_universe[coin] = rows

            btc = universe.get("BTC") or gate_candles("BTC_USDT", "4h", MAX_DAYS)
            btc_last_ret = (btc[-1][4] / btc[-2][4] - 1) if btc and len(btc) >= 2 else None

            fn, fd = run_faber(cur, cfg)
            dn, dd = run_donchian(cur, cfg, regime_ok, universe, exit_universe)
            rn, rd = run_rotatie(cur, cfg, regime_ok, universe, exit_universe)
            cn, cd = run_c2v(cur, regime, btc_last_ret, universe, exit_universe)

            if dry:
                conn.rollback()
                log("DRY: rollback (niets weggeschreven)")
            else:
                state_set(cur, "gate_shadow_last_run", now_utc().isoformat())
                conn.commit()
            log(f"FABER {fn}/{fd} | DONCHIAN {dn}/{dd} | ROTATIE {rn}/{rd} | C2V {cn}/{cd}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
