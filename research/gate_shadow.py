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
import json
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
MIN_AGE_DAYS = 14            # min. leeftijd; onder ~14d te weinig candle-historie voor indicatoren
TOP_N = 200                  # zo breed als betrouwbaar kan (Hein: alle coins meenemen);
                             # ~2200 coins/uur knalt tegen Gate rate-limits vanaf Render-IP
LIQ_MIN = 25_000.0           # lage drempel: ook kleine coins mee (24h-quotevolume, USDT ~ EUR)
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

# --- VBREAK: volatility-breakout (Larry Williams / Koreaans K=0,5), regime-gated ---
# Backtest Gate 1h ~240d: 73,7% wr, +5,68%/trade op 38 trades (klein sample +
# survivorship -> shadow-test moet het bevestigen). Beste daytrade-kandidaat uit
# het Fable-onderzoek (West+Azie convergeren). Long-only, 1h-timing op dag-grid.
VBREAK_K = 0.5             # koopniveau = day_open + 0,5 x vorige-dag-range
VBREAK_STOP = 0.03        # harde stop -3%
VBREAK_COST_RT = 0.60     # (legacy; v1/v2 gebruiken kosten-per-grootte via _vb_cost)
# v2-fixes (Fable5+Opus5 consensus): close-confirm+volfilter, dag-cap, partial+trail
VB2_PARTIAL = 0.04        # 50% afbouwen op +4%
VB2_ATR_TRAIL = 2.5       # rest: chandelier hh - 2,5xATR, vloer break-even
VB2_MAX_HOLD_H = 120      # 5 dagen safety time-exit
VB2_K_PER_DAY = 8         # max nieuwe v2-entries per UTC-dag (anti-overtrading)
VB2_VOL_CONFIRM = 1.5     # breakout-uur volume > 1,5x gem(20u)

# --- VOLLEDIGE-MARKT prescreen (alle ~2200 coins, rate-limit-veilig) ---
# Ontwerp Fable5: 2 goedkope calls (/tickers + /currency_pairs) screenen ALLE coins;
# alleen kandidaten krijgen dure candles. Regimes sluiten elkaar uit (C2V=bear,
# VBREAK=bull) -> max 1 strategie fetcht entries per run.
SCAN_BUDGET = 60          # max candle-calls per run (gedeeld Render-IP -> conservatief)
PACE_S = 0.30             # pauze tussen candle-calls (~3 req/s, ver onder de limiet)
SPREAD_MAX = 0.02         # ticker-spread-sanity (bid/ask)
C2V_PRE_QV = 1_600_000.0  # C2V-prescreen volume (0,8x de 2M exacte eis, marge)
C2V_PRE_DROP = -6.0       # C2V-prescreen: min(24h%, last/high_24h) <= -6%
VB_PRE_QV = 2_000_000.0   # VBREAK-prescreen volume-vloer ≥2M (consensus-fix: microcaps
                          # sloopten de winrate — live 13% wr <1M vs 41% ≥1M)
VB_MIN_AGE_D = 35         # VBREAK heeft >=31 daily bars nodig
VB_EPS = 0.15             # marge op de 0,5x-range-term (snapshot-offset rond 00:00)

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
    for attempt in range(3):
        try:
            r = requests.get(f"{GATE}{path}", params=params, timeout=20)
            if r.status_code in (400, 404):
                return None
            if r.status_code == 429:                       # rate-limit -> backoff
                wait = float(r.headers.get("Retry-After", 0)) or (2 ** attempt * 2)
                log(f"429 op {path} — backoff {wait}s")
                time.sleep(wait)
                continue
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
    per = {"1h": 24, "4h": 6, "1d": 1}.get(interval, 6)
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
    cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS day_open DOUBLE PRECISION")
    cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS atr_entry DOUBLE PRECISION")


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


def _vb_cost(v24):
    """Realistische round-trip kosten per coin-grootte (fee + spread)."""
    return 0.5 if (v24 or 0) >= 10_000_000 else 0.9


def _close_vb(cur, tid, strat, exit_p, exit_ts, reden, gross_pnl, v24):
    net = round(gross_pnl - _vb_cost(v24), 4)
    cur.execute(f"""UPDATE {TABLE} SET status=%s, exit_prijs=%s, exit_ts=%s,
        exit_reden=%s, pnl_pct=%s, fee_pct=%s, spread_pct=%s, pnl_net_pct=%s WHERE id=%s""",
                ("WIN" if net > 0 else "LOSS", exit_p, exit_ts, reden, round(gross_pnl, 4),
                 _vb_cost(v24), 0.0, net, tid))


def _resolve_v1(cur, rows, tid, coin, ets, entry, stop, day_open, v24):
    """v1-exit: stop -3%, close<day_open, of volgende dag 00:00 (time)."""
    for r in rows:
        if r[0] <= ets:
            continue
        t, o, _h, l, c = r[0], r[1], r[2], r[3], r[4]
        if t.date() > ets.date() and t.hour == 0:
            _close_vb(cur, tid, "VBREAK_V1", o, t, "TIME_DAG", (o - entry) / entry * 100, v24); return 1
        if l <= stop:
            _close_vb(cur, tid, "VBREAK_V1", stop, t, "STOP", (stop - entry) / entry * 100, v24); return 1
        if c < day_open:
            _close_vb(cur, tid, "VBREAK_V1", c, t, "ONDER_OPEN", (c - entry) / entry * 100, v24); return 1
    return 0


def _resolve_v2(cur, rows, tid, coin, ets, entry, stop, target, day_open, atr_e, v24):
    """v2-exit: stop -3%, close<day_open, 50% op +4% dan rest ATR-trailing, 5d-safety.
    Stateless: herleidt partial-status uit de candles sinds entry."""
    partial = False
    hh = entry
    n = 0
    for r in rows:
        if r[0] <= ets:
            continue
        n += 1
        t, o, h, l, c = r[0], r[1], r[2], r[3], r[4]
        if not partial:
            if l <= stop:
                _close_vb(cur, tid, "VBREAK_V2", stop, t, "STOP", (stop - entry) / entry * 100, v24); return 1
            if c < day_open:
                _close_vb(cur, tid, "VBREAK_V2", c, t, "ONDER_OPEN", (c - entry) / entry * 100, v24); return 1
            if h >= target:
                partial = True
                hh = max(hh, h)
        else:
            hh = max(hh, h)
            trail = max(entry, hh - VB2_ATR_TRAIL * atr_e)
            if l <= trail:
                runner = (trail - entry) / entry * 100
                _close_vb(cur, tid, "VBREAK_V2", trail, t, "TRAIL", 0.5 * VB2_PARTIAL * 100 + 0.5 * runner, v24); return 1
        if n >= VB2_MAX_HOLD_H:
            rr = (c - entry) / entry * 100
            g = (0.5 * VB2_PARTIAL * 100 + 0.5 * rr) if partial else rr
            _close_vb(cur, tid, "VBREAK_V2", c, t, "TIME_5D", g, v24); return 1
    return 0


def run_vbreak(cur, regime_ok, universe_bases, qvmap=None):
    """Draait VBREAK v1 EN v2 NAAST elkaar op het ≥2M-universum (aparte strategie-labels
    VBREAK_V1 / VBREAK_V2), plus resolve van legacy 'VBREAK'-open-trades met v1-logica."""
    qvmap = qvmap or {}
    dicht = 0
    # --- open trades resolven (1h sinds entry per coin, hergebruikt voor v1+v2) ---
    cur.execute(f"""SELECT id,strategie,coin,entry_ts,entry,stop,target,day_open,atr_entry,vol24_usdt
                    FROM {TABLE} WHERE strategie IN ('VBREAK','VBREAK_V1','VBREAK_V2') AND status='OPEN'""")
    opens = cur.fetchall()
    cache1h = {}
    for tid, strat, coin, ets, entry, stop, target, day_open, atr_e, v24 in opens:
        rows = cache1h.get(coin)
        if rows is None:
            rows = gate_candles(f"{coin}_USDT", "1h", 6)
            cache1h[coin] = rows
        if not rows:
            continue
        do = day_open if day_open is not None else target  # legacy VBREAK: day_open zat in target
        if strat == "VBREAK_V2" and atr_e:
            dicht += _resolve_v2(cur, rows, tid, coin, ets, entry, stop, target, do, atr_e, v24)
        else:
            dicht += _resolve_v1(cur, rows, tid, coin, ets, entry, stop, do, v24)

    nieuw = 0
    if not regime_ok:
        return nieuw, dicht
    # v2 dag-cap: hoeveel al vandaag geopend
    cur.execute(f"""SELECT COUNT(*) FROM {TABLE} WHERE strategie='VBREAK_V2'
                    AND entry_ts::date = (NOW() AT TIME ZONE 'UTC')::date""")
    v2_today = cur.fetchone()[0]
    cur.execute(f"SELECT strategie,coin FROM {TABLE} WHERE strategie IN ('VBREAK_V1','VBREAK_V2') AND status='OPEN'")
    open_set = {(r[0], r[1]) for r in cur.fetchall()}
    # ranking: hoogste volume eerst (voor de dag-cap)
    bases = sorted(universe_bases, key=lambda b: -qvmap.get(b, 0))
    for coin in bases:
        d = fetch_daily(coin, 40)
        if len(d) < 31:
            continue
        h1 = gate_candles(f"{coin}_USDT", "1h", 3)
        if not h1:
            continue
        today = h1[-1][0].date()
        opens0 = [r for r in h1 if r[0].date() == today and r[0].hour == 0]
        if not opens0:
            continue
        day_open = opens0[0][1]
        prev_range = d[-1][2] - d[-1][3]
        if prev_range <= 0:
            continue
        ma5 = sum(x[4] for x in d[-5:]) / 5
        vmed = median([x[6] for x in d[-31:-1]])
        if not (day_open > ma5 and d[-1][6] > vmed):
            continue
        level = day_open + VBREAK_K * prev_range
        v24 = d[-1][6]
        today_bars = [r for r in h1 if r[0].date() == today]
        # --- v1: eerste intrabar-touch boven level ---
        if ("VBREAK_V1", coin) not in open_set:
            trig = next((r for r in today_bars if r[0].hour <= 20 and r[2] >= level), None)
            if trig:
                cur.execute(f"""INSERT INTO {TABLE} (strategie,coin,entry_ts,entry,stop,day_open,vol24_usdt)
                    VALUES ('VBREAK_V1',%s,%s,%s,%s,%s,%s) ON CONFLICT (strategie,coin,entry_ts) DO NOTHING""",
                            (coin, trig[0], level, level * (1 - VBREAK_STOP), day_open, v24))
                nieuw += cur.rowcount
        # --- v2: close-confirm + volumebevestiging, entry op volgende bar-open, dag-cap ---
        if ("VBREAK_V2", coin) not in open_set and v2_today < VB2_K_PER_DAY:
            for k, r in enumerate(h1):
                if r[0].date() != today or r[0].hour > 20 or k < 20 or k + 1 >= len(h1):
                    continue
                vavg = sum(h1[j][5] for j in range(k - 20, k)) / 20
                if r[4] >= level and vavg and r[5] > VB2_VOL_CONFIRM * vavg:
                    a = atr(h1[:k + 1], 14)
                    if a and a > 0:
                        e = h1[k + 1][1]
                        cur.execute(f"""INSERT INTO {TABLE}
                            (strategie,coin,entry_ts,entry,stop,target,day_open,atr_entry,vol24_usdt)
                            VALUES ('VBREAK_V2',%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (strategie,coin,entry_ts) DO NOTHING""",
                                    (coin, h1[k + 1][0], e, e * (1 - VBREAK_STOP), e * (1 + VB2_PARTIAL),
                                     day_open, a, v24))
                        if cur.rowcount:
                            nieuw += 1
                            v2_today += 1
                    break
    return nieuw, dicht


# ── VOLLEDIGE-MARKT scanner (prescreen alle coins -> gerichte candle-fetch) ──
class _Budget:
    def __init__(self, n):
        self.left = n

    def take(self):
        if self.left <= 0:
            return False
        self.left -= 1
        time.sleep(PACE_S)
        return True


def load_market():
    """2 calls -> {base: dict} voor ALLE verhandelbare USDT-paren (goedkoop, geen candles)."""
    tick = gate_get("/spot/tickers") or []
    meta = {m["id"]: m for m in (gate_get("/spot/currency_pairs") or [])}
    now_s, out = now_utc().timestamp(), {}
    for t in tick:
        pair = t.get("currency_pair", "")
        if not pair.endswith("_USDT"):
            continue
        base = pair[:-5]
        if base in _SKIP_BASE or _is_derivative(base):
            continue
        m = meta.get(pair)
        if not m or m.get("trade_status") != "tradable" or m.get("st_tag"):
            continue
        try:
            last, hi, lo = float(t["last"]), float(t["high_24h"]), float(t["low_24h"])
            qv = float(t.get("quote_volume") or 0)
            chg = float(t.get("change_percentage") or 0)
            bid, ask = float(t.get("highest_bid") or 0), float(t.get("lowest_ask") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        bs = int(m.get("buy_start") or 0)
        age_d = (now_s - bs) / 86400 if bs > 0 else 9999
        if age_d < MIN_AGE_DAYS or last <= 0:
            continue
        spread = (ask - bid) / last if (bid > 0 and ask > bid) else 9.9
        out[base] = dict(last=last, hi=hi, lo=lo, qv=qv, chg=chg, spread=spread, age=age_d)
    return out


def maybe_snapshot(cur, market):
    """Sla 1x per dag (rond 00:00 UTC) een ticker-snapshot op zodat VBREAK day_open +
    gisteren-range goedkoop kan reconstrueren."""
    if now_utc().hour == 0 and state_get(cur, "gate_snap_date") != str(now_utc().date()):
        snap = {b: {"o": m["last"], "yh": m["hi"], "yl": m["lo"]} for b, m in market.items()}
        state_set(cur, "gate_dayopen_snapshot", json.dumps(snap))
        state_set(cur, "gate_snap_date", str(now_utc().date()))


def prescreen_c2v(market, open_coins):
    out = []
    for b, m in market.items():
        if b == "BTC" or b in open_coins:
            continue
        if m["qv"] < C2V_PRE_QV or m["spread"] > SPREAD_MAX:
            continue
        dp = min(m["chg"], (m["last"] / m["hi"] - 1) * 100 if m["hi"] > 0 else 0)
        if dp <= C2V_PRE_DROP:
            out.append((dp, b))
    return [b for _, b in sorted(out)]              # zwaarste crash eerst


def prescreen_vbreak(market, snap, open_coins):
    out = []
    for b, m in market.items():
        if b in open_coins or m["qv"] < VB_PRE_QV or m["spread"] > SPREAD_MAX:
            continue
        if m["age"] < VB_MIN_AGE_D:
            continue
        s = (snap or {}).get(b)
        if s and s["yl"] > 0 and s["yh"] > s["yl"]:
            level = s["o"] + 0.5 * (s["yh"] - s["yl"])
            hit = max(m["hi"], m["last"])
            marge = VB_EPS * 0.5 * (s["yh"] - s["yl"])
            if hit >= level - marge:
                out.append((-(hit / level - 1), b))  # grootste overshoot eerst
        elif m["chg"] >= 2.0 or (m["hi"] > 0 and m["last"] >= 0.97 * m["hi"] and m["chg"] >= -1.0):
            out.append((-m["chg"], b))               # fallback (snapshot mist)
    return [b for _, b in sorted(out)]


def _carryover(cur, key, shortlist):
    prev = json.loads(state_get(cur, key) or "[]")
    sset = set(shortlist)
    keep = [b for b in prev if b in sset]
    return keep + [b for b in shortlist if b not in set(keep)]


def _cut(cur, key, lst, maxn, naam):
    keep, rest = lst[:max(maxn, 0)], lst[max(maxn, 0):]
    if rest:
        log(f"AFKAP {naam} ({len(rest)}): {', '.join(rest[:15])}...")
    state_set(cur, key, json.dumps(rest))
    return keep


def scan_fullmarket(cur):
    """Volledige-markt shadow-scan: screent ALLE coins, fetcht candles alleen voor de
    shortlist + open trades, en voedt run_c2v/run_vbreak. Retourneert (cn,cd,vn,vd)."""
    market = load_market()                               # 2 calls
    maybe_snapshot(cur, market)
    regime = btc_regime()                                # 1 call
    btc4 = gate_candles("BTC_USDT", "4h", 10)            # 1 call
    btc_ret = (btc4[-1][4] / btc4[-2][4] - 1) if btc4 and len(btc4) >= 2 else None
    bud = _Budget(SCAN_BUDGET - 4)

    cur.execute(f"SELECT DISTINCT coin FROM {TABLE} WHERE strategie='C2V' AND status='OPEN'")
    open_c2v = {r[0] for r in cur.fetchall()}
    cur.execute(f"SELECT DISTINCT coin FROM {TABLE} WHERE strategie='VBREAK' AND status='OPEN'")
    open_vb = {r[0] for r in cur.fetchall()}

    # open C2V-trades: exit-candles altijd eerst (los van prescreen)
    exit_candles = {}
    for c in open_c2v:
        if bud.take():
            rows = gate_candles(f"{c}_USDT", "4h", 10)
            if rows:
                exit_candles[c] = rows
    bud.left -= len(open_vb)                              # run_vbreak fetcht zelf 1h/open trade

    universe, vb_bases = {}, []
    if regime is False and (btc_ret is None or btc_ret >= C2V_BTC_FLOOR):
        short = _carryover(cur, "gate_co_c2v", prescreen_c2v(market, open_c2v))
        for b in _cut(cur, "gate_co_c2v", short, bud.left, "C2V"):
            if not bud.take():
                break
            rows = gate_candles(f"{b}_USDT", "4h", 10)
            if len(rows) >= 25:
                universe[b] = rows
    elif regime is True:
        snap = json.loads(state_get(cur, "gate_dayopen_snapshot") or "null")
        short = _carryover(cur, "gate_co_vb", prescreen_vbreak(market, snap, open_vb))
        vb_bases = _cut(cur, "gate_co_vb", short, bud.left // 2, "VBREAK")

    log(f"prescreen: {len(market)} paren -> C2V-shortlist {len(universe)} / "
        f"VBREAK-shortlist {len(vb_bases)} | regime {regime} | budget-rest {bud.left}")
    cn, cd = run_c2v(cur, regime, btc_ret, universe, exit_candles)
    qvmap = {b: m.get("qv", 0) for b, m in market.items()}
    vn, vd = run_vbreak(cur, regime is True, vb_bases, qvmap)
    return cn, cd, vn, vd


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
def main(c2v_only=False):
    """c2v_only=True: alleen de C2V-strategie draaien (enige met bewezen edge in de
    Gate-backtest). FABER/DONCHIAN/ROTATIE werden afgevoerd (negatief/survivorship)."""
    if os.environ.get("NODB"):
        selftest()
        return
    c2v_only = c2v_only or bool(os.environ.get("GATE_C2V_ONLY"))
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
            fn = fd = dn = dd = rn = rd = 0
            if c2v_only:
                # LIVE-modus: volledige-markt prescreen (alle ~2200 coins) -> C2V + VBREAK
                cn, cd, vn, vd = scan_fullmarket(cur)
            else:
                # legacy test-modus: top-N universum + alle 5 strategieen
                cfg = costmod.load_cfg(cur)
                cfg["majors"] = set(cfg["majors"]) | {m.replace("USDT", "") for m in cfg["majors"]}
                conn.commit()
                universe = gate_universe()
                regime = btc_regime()
                regime_ok = bool(regime)
                log(f"{len(universe)} liquide Gate-coins | BTC>200d-SMA: {regime} | DRY={dry}")
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
                vn, vd = run_vbreak(cur, regime_ok, list(universe.keys()))

            if dry:
                conn.rollback()
                log("DRY: rollback (niets weggeschreven)")
            else:
                state_set(cur, "gate_shadow_last_run", now_utc().isoformat())
                conn.commit()
            mode = "volledige-markt" if c2v_only else "top-N/alle"
            log(f"[{mode}] FABER {fn}/{fd} | DONCHIAN {dn}/{dd} | ROTATIE {rn}/{rd} "
                f"| C2V {cn}/{cd} | VBREAK {vn}/{vd}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
