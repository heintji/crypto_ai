#!/usr/bin/env python3
"""MEAN-REVERSION SHADOW — test van de RSI<25-strategie (geteste 67% winrate).

Zelfstandige schaduw-motor, NAAST de bestaande bot (raakt de live-bot niet).
Doel: een week vooruit-meten of de geteste 67% winrate in de praktijk standhoudt
— zonder echt geld.

Strategie (exact zoals gebacktest op 4h-candles):
  - Entry : laatste GESLOTEN 4h-candle met RSI14 < 25 (diep oversold).
  - Stop  : entry - 3,0 x ATR14   (ruime stop).
  - Doel  : entry + 0,5R = entry + 1,5 x ATR14   (klein doel -> hoge winrate).
  - Max   : 12 candles (48 uur), anders TIME-exit op slotkoers.
  - Max 1 open trade per munt. Waterdicht: UNIQUE(coin, entry_ts), idempotent.

Leest candles uit de bestaande `candles`-tabel (door de bot vers gehouden).
Logt naar nieuwe tabel `mr_shadow_trades`. Bedoeld als Render-cron (elk uur).

Env: DATABASE_URL.  Dependencies: psycopg2-binary.
"""
import os
from datetime import datetime, timezone

import psycopg2

INTERVAL_S = 4 * 3600  # 4h-candle in seconden (voor 'afgesloten?'-check)
ATR_MULT = 3.0      # stop = entry - 3 x ATR
TARGET_R = 0.5      # doel = 0,5 x risico  (=> 1,5 x ATR boven entry)
RSI_DREMPEL = 25.0
MAX_HOLD = 12       # 12 x 4h = 48 uur
ATR_PERIOD = 14
RSI_PERIOD = 14


def rsi(closes, p=RSI_PERIOD):
    if len(closes) < p + 1:
        return None
    g = l = 0.0
    for k in range(-p, 0):
        d = closes[k] - closes[k - 1]
        g += max(d, 0)
        l += max(-d, 0)
    return 100 - 100 / (1 + g / l) if l > 0 else 100.0


def atr(rows, p=ATR_PERIOD):
    # rows: list van (ts, o, h, l, c)
    if len(rows) < p + 1:
        return None
    s = 0.0
    for k in range(-p, 0):
        h, lo, pc = rows[k][2], rows[k][3], rows[k - 1][4]
        s += max(h - lo, abs(h - pc), abs(lo - pc))
    return s / p


def ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mr_shadow_trades (
            id          SERIAL PRIMARY KEY,
            coin        TEXT NOT NULL,
            entry_ts    TIMESTAMPTZ NOT NULL,
            entry       DOUBLE PRECISION NOT NULL,
            stop        DOUBLE PRECISION NOT NULL,
            target      DOUBLE PRECISION NOT NULL,
            rsi_entry   DOUBLE PRECISION,
            atr_entry   DOUBLE PRECISION,
            status      TEXT NOT NULL DEFAULT 'OPEN',
            exit_ts     TIMESTAMPTZ,
            exit_prijs  DOUBLE PRECISION,
            pnl_pct     DOUBLE PRECISION,
            mfe_pct     DOUBLE PRECISION DEFAULT 0,
            mae_pct     DOUBLE PRECISION DEFAULT 0,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (coin, entry_ts)
        )
    """)


def load_candles(cur, days=20):
    """Alle 4h-candles per munt (laatste `days` dagen), gesorteerd op tijd."""
    cur.execute("""
        SELECT symbol, open_time, open, high, low, close
        FROM candles WHERE timeframe='4h' AND open_time >= NOW() - INTERVAL '%s days'
        ORDER BY symbol, open_time
    """ % int(days))
    out = {}
    for sym, t, o, h, lo, c in cur.fetchall():
        out.setdefault(sym, []).append((t, float(o), float(h), float(lo), float(c)))
    return out


def resolve_open(cur, candles):
    """Werk open trades bij: stop/target/tijd + MFE/MAE."""
    cur.execute("""SELECT id, coin, entry_ts, entry, stop, target FROM mr_shadow_trades
                   WHERE status='OPEN'""")
    open_rows = cur.fetchall()
    closed = 0
    for tid, coin, ets, entry, stop, target in open_rows:
        rows = candles.get(coin)
        if not rows:
            continue
        fut = [r for r in rows if r[0] > ets]
        if not fut:
            continue
        status = exit_p = exit_t = None
        mfe = mae = 0.0
        for n, (t, o, h, lo, c) in enumerate(fut, start=1):
            mfe = max(mfe, (h - entry) / entry * 100)
            mae = min(mae, (lo - entry) / entry * 100)
            if lo <= stop:
                status, exit_p, exit_t = 'LOSS', stop, t
                break
            if h >= target:
                status, exit_p, exit_t = 'WIN', target, t
                break
            if n >= MAX_HOLD:
                status, exit_p, exit_t = 'TIME', c, t
                break
        if status:
            pnl = (exit_p - entry) / entry * 100
            cur.execute("""UPDATE mr_shadow_trades
                SET status=%s, exit_prijs=%s, exit_ts=%s, pnl_pct=%s, mfe_pct=%s, mae_pct=%s
                WHERE id=%s""", (status, exit_p, exit_t, round(pnl, 4),
                                 round(mfe, 4), round(mae, 4), tid))
            closed += 1
        else:
            cur.execute("UPDATE mr_shadow_trades SET mfe_pct=%s, mae_pct=%s WHERE id=%s",
                        (round(mfe, 4), round(mae, 4), tid))
    return closed, len(open_rows)


def scan_signals(cur, candles, now):
    """Zoek nieuwe RSI<25-signalen op de laatste AFGESLOTEN 4h-candle per munt.

    Belangrijk: de nieuwste candle in de DB is meestal de nog-VORMENDE bar
    (open_time tot +4u). Die sluiten we uit — anders is RSI/entry op halve data
    en wijken we af van de backtest (die op afgesloten candles werkte)."""
    cur.execute("SELECT coin FROM mr_shadow_trades WHERE status='OPEN'")
    open_coins = {r[0] for r in cur.fetchall()}
    nieuw = 0
    for coin, rows in candles.items():
        if coin in open_coins:
            continue
        # alleen afgesloten candles (now >= open_time + 4u)
        closed = [r for r in rows if (now - r[0]).total_seconds() >= INTERVAL_S]
        if len(closed) < ATR_PERIOD + 2:
            continue
        closes = [r[4] for r in closed]
        r = rsi(closes)
        if r is None or r >= RSI_DREMPEL:
            continue
        a = atr(closed)
        if not a or a <= 0:
            continue
        last = closed[-1]
        entry = last[4]
        stop = entry - ATR_MULT * a
        risk = entry - stop
        if risk <= 0:
            continue
        target = entry + TARGET_R * risk
        try:
            cur.execute("""INSERT INTO mr_shadow_trades
                (coin, entry_ts, entry, stop, target, rsi_entry, atr_entry)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (coin, entry_ts) DO NOTHING""",
                (coin, last[0], entry, stop, target, round(r, 2), round(a, 8)))
            if cur.rowcount:
                nieuw += 1
        except Exception as e:
            print(f"[scan err] {coin}: {e}", flush=True)
    return nieuw


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            ensure_table(cur)
            conn.commit()
            now = datetime.now(timezone.utc)
            candles = load_candles(cur, days=20)
            closed, open_n = resolve_open(cur, candles)
            conn.commit()
            nieuw = scan_signals(cur, candles, now)
            conn.commit()
            cur.execute("""SELECT
                COUNT(*) FILTER (WHERE status='OPEN'),
                COUNT(*) FILTER (WHERE status IN('WIN','LOSS','TIME')),
                COUNT(*) FILTER (WHERE status='WIN'),
                COUNT(*) FILTER (WHERE status='LOSS')
                FROM mr_shadow_trades""")
            o, dicht, w, l = cur.fetchone()
            wr = round(100 * w / (w + l), 1) if (w + l) else 0
            print(f"[mr-shadow] {len(candles)} munten | nieuw {nieuw} | gesloten nu {closed} | "
                  f"open {o} | totaal dicht {dicht} (W{w}/L{l}) | winrate {wr}%", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
