#!/usr/bin/env python3
"""MR-ULTIMATE — data-verzoende "ultimate" mean-reversion shadow, PARALLEL getest.

Zelfstandige schaduw-motor, NAAST de bestaande bot en NAAST mr_shadow/mr_trail
(raakt niets van dat alles aan). Dit is de verzoende variant uit een 3-AI-analyse
van de echte trade-historie. Doel: een week vooruit-meten of deze regels in de
praktijk standhouden — zonder echt geld.

Verschillen t.o.v. de basis (mr_shadow, RSI<25 + 3xATR-stop):
  - ENTRY  : laatste GESLOTEN 4h-candle met RSI14 in de band [15, 25) — dus
             RSI >= 15 EN RSI < 25. KRITIEK: RSI < 15 wordt OVERGESLAGEN
             (data: RSI<10 = 46% winrate "vallende messen"; extreem oversold
             verliest). Dit is het kernverschil met de basis (die alle RSI<25 pakt).
  - VOLA-FILTER : atr_pct = atr/entry*100; alleen entries met atr_pct tussen
             0,5 en 2,0 (te vlak én te wild overslaan; data: lage ATR ~69% WR
             vs hoge ATR ~58%).
  - STOP   : VAST -2% (entry*0.98). De ruime 3xATR-stop van de basis gaf -4,3%
             gem. verlies dat het net-na-kosten kapotmaakte; -2% snijdt gem.
             verlies naar ~-1,9%.
  - DOEL   : VAST +3,5% (entry*1.035) => ~1,75:1 reward:risk. (Goede varianten
             haalden +3,7-3,9% per winst.)
  - MAX    : 12 candles (48 uur), anders TIME-exit op slotkoers.
  - Max 1 open trade per munt. Waterdicht: UNIQUE(coin, entry_ts), idempotent.

mfe_pct/mae_pct worden belangrijk: daarmee checken we volgende week fill-kwaliteit
/phantom-fills en of trailing geholpen zou hebben.

Leest candles uit de bestaande `candles`-tabel (door de bot vers gehouden).
Logt naar nieuwe tabel `mr_ultimate_trades`. Meelift op de 15-min-scanner-cron.

Env: DATABASE_URL.  Dependencies: psycopg2-binary.
"""
import os
from datetime import datetime, timezone

import psycopg2

INTERVAL_S = 4 * 3600  # 4h-candle in seconden (voor 'afgesloten?'-check)
RSI_MIN = 15.0         # entry-band ondergrens (RSI >= 15) — RSI<15 overslaan
RSI_MAX = 25.0         # entry-band bovengrens (RSI < 25)
ATR_PCT_MIN = 0.5      # vola-filter ondergrens (atr/entry*100)
ATR_PCT_MAX = 2.0      # vola-filter bovengrens
STOP_PCT = 0.02        # vaste stop -2%
TARGET_PCT = 0.035     # vast doel +3,5%
MAX_HOLD = 12          # 12 x 4h = 48 uur
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
        CREATE TABLE IF NOT EXISTS mr_ultimate_trades (
            id          SERIAL PRIMARY KEY,
            coin        TEXT NOT NULL,
            entry_ts    TIMESTAMPTZ NOT NULL,
            entry       DOUBLE PRECISION NOT NULL,
            stop        DOUBLE PRECISION NOT NULL,
            target      DOUBLE PRECISION NOT NULL,
            rsi_entry   DOUBLE PRECISION,
            atr_entry   DOUBLE PRECISION,
            atr_pct     DOUBLE PRECISION,
            status      TEXT NOT NULL DEFAULT 'OPEN',
            target_hit  BOOLEAN DEFAULT FALSE,
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
    """Werk open trades bij: stop -2% / doel +3,5% / tijd + MFE/MAE."""
    cur.execute("""SELECT id, coin, entry_ts, entry, stop, target FROM mr_ultimate_trades
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
        target_hit = False
        mfe = mae = 0.0
        for n, (t, o, h, lo, c) in enumerate(fut, start=1):
            mfe = max(mfe, (h - entry) / entry * 100)
            mae = min(mae, (lo - entry) / entry * 100)
            if lo <= stop:
                status, exit_p, exit_t = 'LOSS', stop, t
                break
            if h >= target:
                status, exit_p, exit_t, target_hit = 'WIN', target, t, True
                break
            if n >= MAX_HOLD:
                status, exit_p, exit_t = 'TIME', c, t
                break
        if status:
            pnl = (exit_p - entry) / entry * 100
            cur.execute("""UPDATE mr_ultimate_trades
                SET status=%s, target_hit=%s, exit_prijs=%s, exit_ts=%s,
                    pnl_pct=%s, mfe_pct=%s, mae_pct=%s
                WHERE id=%s""", (status, target_hit, exit_p, exit_t, round(pnl, 4),
                                 round(mfe, 4), round(mae, 4), tid))
            closed += 1
        else:
            cur.execute("UPDATE mr_ultimate_trades SET mfe_pct=%s, mae_pct=%s WHERE id=%s",
                        (round(mfe, 4), round(mae, 4), tid))
    return closed, len(open_rows)


def scan_signals(cur, candles, now):
    """Zoek nieuwe entries: laatste AFGESLOTEN 4h-candle met RSI in [15,25)
    én atr_pct in [0,5 ; 2,0].

    Belangrijk: de nieuwste candle in de DB is meestal de nog-VORMENDE bar
    (open_time tot +4u). Die sluiten we uit — anders is RSI/entry op halve data."""
    cur.execute("SELECT coin FROM mr_ultimate_trades WHERE status='OPEN'")
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
        # entry-band: RSI >= 15 EN RSI < 25 (RSI<15 = vallende messen -> overslaan)
        if r is None or r < RSI_MIN or r >= RSI_MAX:
            continue
        a = atr(closed)
        if not a or a <= 0:
            continue
        last = closed[-1]
        entry = last[4]
        if entry <= 0:
            continue
        atr_pct = a / entry * 100
        # vola-filter: te vlak (<0,5%) én te wild (>2,0%) overslaan
        if atr_pct < ATR_PCT_MIN or atr_pct > ATR_PCT_MAX:
            continue
        stop = entry * (1 - STOP_PCT)
        target = entry * (1 + TARGET_PCT)
        try:
            cur.execute("""INSERT INTO mr_ultimate_trades
                (coin, entry_ts, entry, stop, target, rsi_entry, atr_entry, atr_pct)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (coin, entry_ts) DO NOTHING""",
                (coin, last[0], entry, stop, target, round(r, 2),
                 round(a, 8), round(atr_pct, 4)))
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
                COUNT(*) FILTER (WHERE status='LOSS'),
                ROUND(COALESCE(SUM(pnl_pct) FILTER (WHERE status IN('WIN','LOSS','TIME')),0)::numeric,1)
                FROM mr_ultimate_trades""")
            o, dicht, w, l, sompnl = cur.fetchone()
            wr = round(100 * w / (w + l), 1) if (w + l) else 0
            print(f"[mr-ultimate] {len(candles)} munten | nieuw {nieuw} | gesloten nu {closed} | "
                  f"open {o} | totaal dicht {dicht} (W{w}/L{l}) | winrate {wr}% | "
                  f"som-pnl {sompnl}%", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
