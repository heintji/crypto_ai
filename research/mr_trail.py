#!/usr/bin/env python3
"""MR-TRAIL — verbeterde variant van de mean-reversion shadow, PARALLEL getest.

Zelfde instap-signalen als mr_shadow (RSI<25), gespiegeld uit `mr_shadow_trades`,
maar met twee data-bewezen aanpassingen (backtest 30-6-2026, 52 trades):
  - Stop : VAST -2% (i.p.v. 3xATR) -> verliezers veel kleiner.
  - Na het doel: NIET verkopen, maar TRAILING stop 1xATR. Winst wordt vergrendeld
    op doelniveau en het net schuift mee omhoog -> winnaars lopen door.
Backtest: netto +84% vs +19% (huidig), profit-factor 2,66, NIET afhankelijk van
uitschieters (zonder top-3 nog +34% mét slippage). Doel: vooruit-meten of dat
in de praktijk standhoudt. GEEN echt geld.

Logt naar nieuwe tabel `mr_trail_trades`. Raakt mr_shadow EN de live-bot NIET aan.
Env: DATABASE_URL.
"""
import os
import psycopg2

STOP_PCT = 0.02      # vaste stop -2%
TRAIL_MULT = 1.0     # trailing stop = 1x ATR onder de piek (na het doel)
MAX_HOLD = 12        # 12x4h = 48u om doel of stop te raken (anders TIME-exit)
MAX_TOTAL = 30       # daarna nog trailen tot max 30 candles vanaf entry (~5 dgn)


def ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mr_trail_trades (
            id          SERIAL PRIMARY KEY,
            coin        TEXT NOT NULL,
            entry_ts    TIMESTAMPTZ NOT NULL,
            entry       DOUBLE PRECISION NOT NULL,
            stop        DOUBLE PRECISION NOT NULL,
            target      DOUBLE PRECISION NOT NULL,
            atr_entry   DOUBLE PRECISION,
            status      TEXT NOT NULL DEFAULT 'OPEN',
            target_hit  BOOLEAN DEFAULT FALSE,
            exit_ts     TIMESTAMPTZ,
            exit_prijs  DOUBLE PRECISION,
            pnl_pct     DOUBLE PRECISION,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (coin, entry_ts)
        )
    """)


def mirror_entries(cur):
    """Spiegel elke mr_shadow-entry naar mr_trail: zelfde instap + doel, stop -2%.
    Idempotent via UNIQUE(coin, entry_ts)."""
    cur.execute("""
        INSERT INTO mr_trail_trades (coin, entry_ts, entry, stop, target, atr_entry)
        SELECT s.coin, s.entry_ts, s.entry, s.entry*(1-%s), s.target, s.atr_entry
        FROM mr_shadow_trades s
        ON CONFLICT (coin, entry_ts) DO NOTHING
    """, (STOP_PCT,))
    return cur.rowcount


def load_candles(cur, days=20):
    cur.execute("""SELECT symbol, open_time, high, low, close FROM candles
                   WHERE timeframe='4h' AND open_time >= NOW() - INTERVAL '%s days'
                   ORDER BY symbol, open_time""" % int(days))
    out = {}
    for sym, t, h, lo, c in cur.fetchall():
        out.setdefault(sym, []).append((t, float(h), float(lo), float(c)))
    return out


def resolve_open(cur, candles):
    """Stateless: herleid per open trade de uitkomst uit de candles.
    Fase 1 (tot doel/stop): stop -2% of doel. Fase 2 (na doel): trailing 1xATR,
    stop vergrendeld op doel (kan nooit onder het doel zakken)."""
    cur.execute("""SELECT id, coin, entry_ts, entry, stop, target, atr_entry
                   FROM mr_trail_trades WHERE status='OPEN'""")
    closed = 0
    for tid, coin, ets, entry, stop, target, atr in cur.fetchall():
        rows = candles.get(coin)
        if not rows:
            continue
        fut = [r for r in rows if r[0] > ets]
        if not fut:
            continue
        atr = atr or 0.0
        status = exit_p = exit_t = None
        target_hit = False
        peak = target
        for n, (t, h, lo, c) in enumerate(fut, start=1):
            if not target_hit:
                if lo <= stop:                       # stop -2% geraakt
                    status, exit_p, exit_t = 'LOSS', stop, t
                    break
                if h >= target:                      # doel geraakt -> ga trailen
                    target_hit = True
                    peak = max(target, h)
                    continue                         # trailing pas vanaf volgende candle (conservatief)
                if n >= MAX_HOLD:                     # noch doel noch stop -> tijd-exit
                    status, exit_p, exit_t = 'TIME', c, t
                    break
            else:
                peak = max(peak, h)
                tstop = max(target, peak - TRAIL_MULT * atr)   # net schuift mee, vloer op doel
                if lo <= tstop:
                    status, exit_p, exit_t = 'WIN', tstop, t
                    break
                if n >= MAX_TOTAL:                    # forceer sluiten na lange trail
                    status, exit_p, exit_t = 'WIN', c, t
                    break
        if status:
            pnl = (exit_p - entry) / entry * 100
            cur.execute("""UPDATE mr_trail_trades
                SET status=%s, exit_prijs=%s, exit_ts=%s, pnl_pct=%s, target_hit=%s
                WHERE id=%s""", (status, exit_p, exit_t, round(pnl, 4), target_hit, tid))
            closed += 1
        elif target_hit:
            cur.execute("UPDATE mr_trail_trades SET target_hit=TRUE WHERE id=%s", (tid,))
    return closed


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            ensure_table(cur)
            conn.commit()
            nieuw = mirror_entries(cur)
            conn.commit()
            candles = load_candles(cur, days=20)
            closed = resolve_open(cur, candles)
            conn.commit()
            cur.execute("""SELECT COUNT(*) FILTER (WHERE status='OPEN'),
                COUNT(*) FILTER (WHERE status IN('WIN','LOSS','TIME')),
                COUNT(*) FILTER (WHERE status='WIN'),
                COUNT(*) FILTER (WHERE status='LOSS'),
                ROUND(COALESCE(SUM(pnl_pct) FILTER (WHERE status IN('WIN','LOSS','TIME')),0)::numeric,1)
                FROM mr_trail_trades""")
            o, dicht, w, l, sompnl = cur.fetchone()
            wr = round(100 * w / (w + l), 1) if (w + l) else 0
            print(f"[mr-trail] nieuw {nieuw} | gesloten nu {closed} | open {o} | "
                  f"dicht {dicht} (W{w}/L{l}) | winrate {wr}% | som-pnl {sompnl}%", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
