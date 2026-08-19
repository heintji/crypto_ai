#!/usr/bin/env python3
"""SHADOW-STRATEGIEEN — 3 nieuwe strategieen die MEEDRAAIEN naast mr_trail e.a.,
zodat we eerlijk kunnen vergelijken welke wint. Raakt de live-bot NIET; schrijft
alleen naar de eigen tabel strat_shadow_trades. Idempotent (UNIQUE per strategie/
coin/entry). Bedoeld als Render-cron (elk uur), net als mr_shadow.

Strategieen (uit onderzoek West+China, beide onafhankelijk aanbevolen):
  FABER    — controle-arm: hou een major (BTC/ETH) zolang close > 200d-SMA, anders
             cash. 1 parameter, bijna niet te overfitten. Benchmark voor de rest.
  ROTATIE  — weekly momentum-rotatie: koop top-4 op 28d-rendement uit LIQUIDE coins,
             alleen als BTC > 200d-SMA (regime-gate); anders cash.
  DONCHIAN — 55-bar breakout (4h) + BTC-regimefilter + volumefilter; exit via
             ATR-chandelier-trailing of close < 20-bar-low.

Gedeeld: liquiditeitsfilter (24h-quotevolume), realistische kosten (mr_trail_cost:
fees + getrapte spread), BTC-regime uit VERSE Bitvavo daily-candles (niet uit de
mogelijk ondiepe candles-tabel).

Env: DATABASE_URL. DRY=1 -> alles berekenen maar niets wegschrijven (rollback).
"""
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import psycopg2
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
PROJECT_ROOT = os.path.dirname(HERE)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
import mr_trail_cost as costmod  # fees + spread-tier, herbruikt

BITVAVO = "https://api.bitvavo.com/v2"

LIQ_MIN_EUR = 100_000.0     # algemene liquiditeitsondergrens (24h-quotevolume)
ROT_LIQ_MIN_EUR = 250_000.0  # strengere grens voor rotatie (koopt grotere posities)
MAJORS_FABER = ["BTC", "ETH"]
DONCHIAN_N = 55
DONCHIAN_EXIT_N = 20
ATR_MULT = 3.0
VOL_MULT = 1.5              # volume-filter breakout
ROT_LOOKBACK_BARS = 168    # 28 dagen x 6 (4h-candles)
ROT_TOP = 4
ROT_TREND_MA_BARS = 360    # ~60d trendfilter per coin
MAX_DAYS = 70              # candles-venster: 70d zodat de 360-bar (60d) trend-MA echt data heeft
FOUR_H = timedelta(hours=4)


def log(m):
    print(f"[STRAT {datetime.now(timezone.utc):%H:%M:%S}] {m}", flush=True)


def now_utc():
    return datetime.now(timezone.utc)


def ms_to_ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


# ------------------------------------------------------------------ indicatoren
def sma(xs, p):
    return sum(xs[-p:]) / p if len(xs) >= p else None


def atr(rows, p=14):
    # rows: (ts, o, h, l, c, ...) -> index 2=h, 3=l, 4=c
    if len(rows) < p + 1:
        return None
    s = 0.0
    for k in range(-p, 0):
        h, lo, pc = rows[k][2], rows[k][3], rows[k - 1][4]
        s += max(h - lo, abs(h - pc), abs(lo - pc))
    return s / p


# ------------------------------------------------------------------ db + data
def db():
    return psycopg2.connect(os.environ["DATABASE_URL"], sslmode="require", connect_timeout=15)


def ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS strat_shadow_trades (
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
            vol24_eur   DOUBLE PRECISION,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (strategie, coin, entry_ts)
        )
    """)


def load_4h(cur, days=MAX_DAYS):
    """coin -> lijst van (ts, o, h, l, c, volume, quote_eur), oplopend op tijd.
    quote_eur valt terug op volume*close als quote_volume 0/ontbreekt (oude data)."""
    cur.execute("""
        SELECT symbol, open_time, open, high, low, close, volume, quote_volume
        FROM candles WHERE timeframe='4h' AND open_time >= NOW() - INTERVAL '%s days'
        ORDER BY symbol, open_time
    """ % int(days))
    out = {}
    for sym, t, o, h, l, c, v, qv in cur.fetchall():
        qe = float(qv) if qv and float(qv) > 0 else float(v) * float(c)
        out.setdefault(sym, []).append((t, float(o), float(h), float(l), float(c), float(v), qe))
    # verwijder een eventuele nog-vormende laatste candle per coin
    now = now_utc()
    for sym, rows in out.items():
        if rows and (now - rows[-1][0]) < FOUR_H:
            rows.pop()
    return out


def vol24(rows, i):
    """24h-quotevolume in EUR = som van candle i-5..i (6 x 4h)."""
    return sum(rows[k][6] for k in range(max(0, i - 5), i + 1))


def fetch_daily(coin, days=260):
    market = f"{coin}-EUR"
    start = int((now_utc() - timedelta(days=days)).timestamp() * 1000)
    try:
        r = requests.get(f"{BITVAVO}/{market}/candles",
                         params={"interval": "1d", "limit": 1000, "start": start}, timeout=15)
        if r.status_code in (400, 404):
            return []
        r.raise_for_status()
        data = r.json()
        rows = [(c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])) for c in data]
        rows.sort(key=lambda x: x[0])   # oplopend
        return rows
    except Exception as e:
        log(f"daily-fout {market}: {e}")
        return []


def fetch_4h(market, days=MAX_DAYS):
    """4h-candles voor een Bitvavo EUR-markt -> (ts_datetime, o,h,l,c, volume, quote_eur).
    Laat de nog-vormende laatste candle weg."""
    start = int((now_utc() - timedelta(days=days)).timestamp() * 1000)
    try:
        r = requests.get(f"{BITVAVO}/{market}/candles",
                         params={"interval": "4h", "limit": 1000, "start": start}, timeout=15)
        if r.status_code in (400, 404):
            return []
        r.raise_for_status()
        data = r.json()
        rows = []
        for c in data:
            ts, o, h, l, cl, v = c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])
            rows.append((ms_to_ts(ts), o, h, l, cl, v, v * cl))
        rows.sort(key=lambda x: x[0])
        if rows and (now_utc() - rows[-1][0]) < FOUR_H:
            rows.pop()
        return rows
    except Exception as e:
        log(f"4h-fout {market}: {e}")
        return []


# stablecoins/uitzonderingen die niet als 'coin' meedoen in het liquide universum
_SKIP_BASE = {"EUR", "USDT", "USDC", "EURT", "DAI", "TUSD", "FDUSD", "PYUSD", "EURC"}


def liquid_universe(days=MAX_DAYS, top=40):
    """De meest liquide Bitvavo-EUR-markten (op 24h-quotevolume) + hun 4h-candles.
    Dit is het universum voor ROTATIE en DONCHIAN — NIET de micro-cap scanner-tabel.
    coin = base-symbool (bv. 'BTC'), zodat mr_trail_cost majors correct herkent."""
    try:
        data = requests.get(f"{BITVAVO}/ticker/24h", timeout=15).json()
    except Exception as e:
        log(f"ticker/24h-fout: {e}")
        return {}
    eur = []
    for x in data:
        mkt = x.get("market", "")
        if not mkt.endswith("-EUR"):
            continue
        base = mkt.split("-")[0]
        if base in _SKIP_BASE:
            continue
        try:
            qv = float(x.get("volumeQuote") or 0)
        except Exception:
            qv = 0.0
        eur.append((mkt, base, qv))
    eur.sort(key=lambda z: -z[2])
    out = {}
    for mkt, base, _ in eur[:top]:
        rows = fetch_4h(mkt, days)
        if len(rows) >= DONCHIAN_N + 21:
            out[base] = rows
        time.sleep(0.1)
    return out


def btc_regime():
    """(bool boven 200d-SMA, daily-rows). None als onvoldoende data."""
    rows = fetch_daily("BTC", 260)
    closed = rows[:-1] if rows and (now_utc() - ms_to_ts(rows[-1][0])) < timedelta(days=1) else rows
    if len(closed) < 201:
        return None
    closes = [r[4] for r in closed]
    ma = sma(closes, 200)
    return closes[-1] > ma if ma else None


# ------------------------------------------------------------------ afsluiten
def close_trade(cur, tid, strat, coin, entry, exit_p, exit_ts, reden, v24, cfg):
    pnl = (exit_p - entry) / entry * 100
    base = coin.replace("USDT", "")
    fee, spread, net = costmod.compute_costs(pnl, base, v24, cfg)
    status = "WIN" if net > 0 else "LOSS"
    cur.execute("""UPDATE strat_shadow_trades SET status=%s, exit_prijs=%s, exit_ts=%s,
        exit_reden=%s, pnl_pct=%s, fee_pct=%s, spread_pct=%s, pnl_net_pct=%s WHERE id=%s""",
                (status, exit_p, exit_ts, reden, round(pnl, 4), fee, spread, net, tid))


def state_get(cur, k):
    cur.execute("SELECT value FROM bot_state WHERE key=%s", (k,))
    r = cur.fetchone()
    return r[0] if r else None


def state_set(cur, k, v):
    cur.execute("""INSERT INTO bot_state(key,value) VALUES(%s,%s)
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""", (k, v))


# ------------------------------------------------------------------ FABER
def run_faber(cur, cfg):
    cur.execute("SELECT id, coin, entry FROM strat_shadow_trades WHERE strategie='FABER' AND status='OPEN'")
    opens = {r[1]: (r[0], r[2]) for r in cur.fetchall()}
    nieuw = dicht = 0
    for coin in MAJORS_FABER:
        rows = fetch_daily(coin, 260)
        closed = rows[:-1] if rows and (now_utc() - ms_to_ts(rows[-1][0])) < timedelta(days=1) else rows
        if len(closed) < 201:
            continue
        closes = [r[4] for r in closed]
        ma = sma(closes, 200)
        if not ma:
            continue
        last, last_ts = closes[-1], ms_to_ts(closed[-1][0])
        above = last > ma
        if coin in opens and not above:
            tid, entry = opens[coin]
            close_trade(cur, tid, "FABER", coin, entry, last, last_ts, "ONDER_MA200", 1e9, cfg)
            dicht += 1
        elif coin not in opens and above:
            cur.execute("""INSERT INTO strat_shadow_trades (strategie,coin,entry_ts,entry,vol24_eur)
                VALUES ('FABER',%s,%s,%s,%s) ON CONFLICT (strategie,coin,entry_ts) DO NOTHING""",
                        (coin, last_ts, last, 1e9))
            nieuw += cur.rowcount
    return nieuw, dicht


# ------------------------------------------------------------------ DONCHIAN
def run_donchian(cur, cfg, regime_ok, candles, exit_candles=None):
    # 1) open trades bijwerken (exit_candles bevat óók coins die uit de top-40 vielen)
    ex = exit_candles if exit_candles is not None else candles
    cur.execute("SELECT id, coin, entry_ts, entry, stop FROM strat_shadow_trades WHERE strategie='DONCHIAN' AND status='OPEN'")
    dicht = 0
    for tid, coin, ets, entry, stop in cur.fetchall():
        rows = ex.get(coin)
        if not rows:
            continue
        atr_e = (entry - stop) / ATR_MULT if stop and entry > stop else None
        highest = entry
        trail = stop if stop else entry
        for i, r in enumerate(rows):
            if r[0] <= ets:
                continue
            t, o, h, l, c = r[0], r[1], r[2], r[3], r[4]
            # 1) EERST stop-check tegen de trail van VÓÓR deze candle
            #    (geen aanname dat de high vóór de low kwam)
            if l <= trail:
                exit_p = o if o < trail else trail  # gap-down onder trail -> exit op open
                close_trade(cur, tid, "DONCHIAN", coin, entry, exit_p, t, "TRAIL_STOP", vol24(rows, i), cfg)
                dicht += 1
                break
            # 2) PAS DAARNA highest/trail bijwerken met de high van deze candle
            highest = max(highest, h)
            if atr_e:
                trail = max(trail, highest - ATR_MULT * atr_e)
            low_n = min(x[3] for x in rows[max(0, i - DONCHIAN_EXIT_N):i]) if i >= 1 else None
            if low_n is not None and c < low_n:
                close_trade(cur, tid, "DONCHIAN", coin, entry, c, t, "ONDER_20LOW", vol24(rows, i), cfg)
                dicht += 1
                break
    # 2) nieuwe entries (alleen bij gunstig regime)
    nieuw = 0
    if not regime_ok:
        return nieuw, dicht
    cur.execute("SELECT coin FROM strat_shadow_trades WHERE strategie='DONCHIAN' AND status='OPEN'")
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
        if v24 < LIQ_MIN_EUR:
            continue
        if last[4] > prior_high and last[5] > VOL_MULT * vavg:
            stop = last[4] - ATR_MULT * a
            cur.execute("""INSERT INTO strat_shadow_trades (strategie,coin,entry_ts,entry,stop,vol24_eur)
                VALUES ('DONCHIAN',%s,%s,%s,%s,%s) ON CONFLICT (strategie,coin,entry_ts) DO NOTHING""",
                        (coin, last[0], last[4], stop, v24))
            nieuw += cur.rowcount
    return nieuw, dicht


# ------------------------------------------------------------------ ROTATIE
def run_rotatie(cur, cfg, regime_ok, candles, exit_candles=None):
    if not candles:
        return 0, 0  # lege/gefaalde universe-fetch mag de weekly slot niet verbranden
    ex = exit_candles if exit_candles is not None else candles
    last = state_get(cur, "rotatie_last_rebalance")
    if last:
        try:
            if (now_utc() - datetime.fromisoformat(last)).days < 6:
                return 0, 0  # nog geen week
        except Exception:
            pass
    # targets = top-4 op 28d-rendement, liquide, boven trend-MA, regime-gate
    targets = []
    if regime_ok:
        scored = []
        for coin, rows in candles.items():
            if len(rows) < ROT_LOOKBACK_BARS + 1:
                continue
            i = len(rows) - 1
            if vol24(rows, i) < ROT_LIQ_MIN_EUR:
                continue
            closes = [x[4] for x in rows]
            mom = closes[-1] / closes[-1 - ROT_LOOKBACK_BARS] - 1
            ma = sma(closes, ROT_TREND_MA_BARS) if len(closes) >= ROT_TREND_MA_BARS else None
            if mom > 0 and (ma is None or closes[-1] > ma):
                scored.append((mom, coin, closes[-1], vol24(rows, i)))
        scored.sort(reverse=True)
        targets = scored[:ROT_TOP]
    target_coins = {c for _, c, _, _ in targets}

    cur.execute("SELECT id, coin, entry FROM strat_shadow_trades WHERE strategie='ROTATIE' AND status='OPEN'")
    opens = {r[1]: (r[0], r[2]) for r in cur.fetchall()}
    dicht = nieuw = 0
    # sluit posities die niet meer in de top-4 staan (ook als de coin uit de top-40 viel)
    for coin, (tid, entry) in opens.items():
        if coin not in target_coins:
            rows = ex.get(coin)
            if not rows:
                continue
            i = len(rows) - 1
            close_trade(cur, tid, "ROTATIE", coin, entry, rows[i][4], rows[i][0], "UIT_TOP", vol24(rows, i), cfg)
            dicht += 1
    # open nieuwe targets
    for _, coin, px, v in targets:
        if coin not in opens:
            cur.execute("""INSERT INTO strat_shadow_trades (strategie,coin,entry_ts,entry,vol24_eur)
                VALUES ('ROTATIE',%s,%s,%s,%s) ON CONFLICT (strategie,coin,entry_ts) DO NOTHING""",
                        (coin, candles[coin][-1][0], px, v))
            nieuw += cur.rowcount
    state_set(cur, "rotatie_last_rebalance", now_utc().isoformat())
    return nieuw, dicht


# ------------------------------------------------------------------ main
def main():
    dry = bool(os.environ.get("DRY"))
    conn = db()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            ensure_table(cur)
            # Throttle: dit lift mee op de bestaande 15-min cron maar doet max ~1x/uur
            # echt werk (bespaart Bitvavo-API-calls; geen aparte cron -> geen extra kosten).
            if not dry:
                _last = state_get(cur, "strat_shadow_last_run")
                if _last:
                    try:
                        if (now_utc() - datetime.fromisoformat(_last)).total_seconds() < 55 * 60:
                            log("throttle: <1u sinds vorige run — overslaan")
                            conn.rollback()
                            return
                    except Exception:
                        pass
            cfg = costmod.load_cfg(cur)
            # M1: majors matchen op zowel 'BTC' als 'BTCUSDT' (universum gebruikt base-symbolen)
            cfg["majors"] = set(cfg["majors"]) | {m.replace("USDT", "") for m in cfg["majors"]}
            conn.commit()
            universe = liquid_universe()   # top-liquide Bitvavo-EUR-markten (vers)
            regime = btc_regime()
            regime_ok = bool(regime)
            log(f"{len(universe)} liquide coins | BTC>200d-SMA: {regime} | DRY={dry}")

            # H2: coins met OPEN trades die uit de top-40 vielen apart bijhalen,
            # zodat hun exits altijd kunnen resolven (entries blijven op universe)
            exit_universe = dict(universe)
            cur.execute("""SELECT DISTINCT coin FROM strat_shadow_trades
                           WHERE strategie IN ('DONCHIAN','ROTATIE') AND status='OPEN'""")
            for (coin,) in cur.fetchall():
                if coin not in exit_universe:
                    rows = fetch_4h(coin + "-EUR")
                    if rows:
                        exit_universe[coin] = rows
                        log(f"exit-universe: {coin} apart bijgehaald ({len(rows)} candles)")

            fn, fd = run_faber(cur, cfg)
            dn, dd = run_donchian(cur, cfg, regime_ok, universe, exit_universe)
            rn, rd = run_rotatie(cur, cfg, regime_ok, universe, exit_universe)

            if dry:
                conn.rollback()
                log("DRY: rollback (niets weggeschreven)")
            else:
                state_set(cur, "strat_shadow_last_run", now_utc().isoformat())
                conn.commit()
            log(f"FABER  nieuw {fn} dicht {fd} | DONCHIAN nieuw {dn} dicht {dd} | ROTATIE nieuw {rn} dicht {rd}")
            if not dry:
                try:
                    from bot_health_helper import health_update
                    health_update("strat_shadow", "OK",
                                  f"FABER {fn}n/{fd}d DONCHIAN {dn}n/{dd}d ROTATIE {rn}n/{rd}d")
                except Exception as e:
                    log(f"health_update fout (genegeerd): {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
