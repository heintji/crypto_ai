#!/usr/bin/env python3
"""MR-TRAIL MEETLAAG deel 1: netto-PnL met kosten (fees + spread/slippage).

Doel: de bruto pnl_pct van mr_trail_trades eerlijk maken. Raakt de
STRATEGIE-logica (entries/exits/trailing) NIET aan — dit schrijft alleen
drie nieuwe kostenkolommen op GESLOTEN trades:

    fee_pct      round-trip handelskosten  (2 x fee per kant)
    spread_pct   round-trip spread/slippage (2 x spread per kant, getrapt)
    pnl_net_pct  pnl_pct - fee_pct - spread_pct

KOSTENMODEL (defaults, overschrijfbaar via bot_state):
    bot_state key              default   betekenis
    mr_fee_pct_per_side        0.25      taker-fee per kant (Bitvavo) -> 0.50% rt
    mr_spread_major_pct        0.05      spread per kant voor majors  -> 0.10% rt
    mr_spread_mid_pct          0.15      spread per kant overig       -> 0.30% rt
    mr_spread_micro_pct        0.40      spread per kant microcaps    -> 0.80% rt
    mr_micro_volume_eur        5000000   24h-quote-volume drempel voor microcap
    mr_majors                  JSON-lijst symbolen die als major tellen

LIQUIDITEITSTRAP:
    - major : symbool in mr_majors (BTC/ETH/SOL/XRP/ADA/DOGE/LINK/AVAX/DOT/POL)
    - micro : 24h-volume (som quote_volume van de 4h-candles in de 24u vóór
              entry, in USDT ~ als EUR behandeld) < mr_micro_volume_eur
    - mid   : al het overige
    FALLBACK: is er géén volumedata voor de munt (0 of ontbrekend), dan mid —
    behalve majors, die blijven major. Dit is gedocumenteerd conservatief
    genoeg omdat vrijwel alle munten 4h-candles met quote_volume hebben.

IDEMPOTENT: kolommen via ADD COLUMN IF NOT EXISTS; alleen rijen met
pnl_net_pct IS NULL worden gevuld, dus herhaald draaien doet niets dubbel.
Dezelfde functie is daarmee zowel de doorlopende berekening (meelift in
mr_trail.main, eigen try/except) als de eenmalige backfill:

    .venv/bin/python research/mr_trail_cost.py     # backfill / handmatig

Env: DATABASE_URL. GEEN echte orders, 100% schaduw-administratie.
"""
import json
import os

MAJORS_DEFAULT = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
    "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT", "POLUSDT",
)

DEFAULTS = {
    "mr_fee_pct_per_side": 0.25,
    "mr_spread_major_pct": 0.05,
    "mr_spread_mid_pct": 0.15,
    "mr_spread_micro_pct": 0.40,
    "mr_micro_volume_eur": 5_000_000.0,
}


# ────────────────────────────────────────────────────────────
# Pure functies (geen DB) — hier zitten de tests op
# ────────────────────────────────────────────────────────────
def spread_tier(coin, vol_24h, cfg):
    """Bepaal liquiditeitstrap: 'major' | 'mid' | 'micro'.
    vol_24h None/0 = onbekend -> fallback mid (tenzij major)."""
    if coin in cfg["majors"]:
        return "major"
    if vol_24h is None or vol_24h <= 0:
        return "mid"          # fallback: geen volumedata -> mid
    if vol_24h < cfg["mr_micro_volume_eur"]:
        return "micro"
    return "mid"


def compute_costs(pnl_pct, coin, vol_24h, cfg):
    """Round-trip kosten + netto pnl voor één gesloten trade.
    Retourneert (fee_pct, spread_pct, pnl_net_pct), alle in %-punten."""
    tier = spread_tier(coin, vol_24h, cfg)
    per_side = cfg["mr_spread_%s_pct" % tier]
    fee_rt = round(2.0 * cfg["mr_fee_pct_per_side"], 4)
    spread_rt = round(2.0 * per_side, 4)
    net = round(float(pnl_pct) - fee_rt - spread_rt, 4)
    return fee_rt, spread_rt, net


# ────────────────────────────────────────────────────────────
# DB-laag
# ────────────────────────────────────────────────────────────
def load_cfg(cur):
    """Lees kostenmodel-config uit bot_state, met veilige defaults.
    Onleesbare waarden vallen stil terug op de default (meetlaag mag nooit
    de scan breken)."""
    cfg = dict(DEFAULTS)
    for key in DEFAULTS:
        try:
            cur.execute("SELECT value FROM bot_state WHERE key=%s", (key,))
            row = cur.fetchone()
            if row and row[0] not in (None, ""):
                cfg[key] = float(row[0])
        except Exception:
            pass
    majors = set(MAJORS_DEFAULT)
    try:
        cur.execute("SELECT value FROM bot_state WHERE key=%s", ("mr_majors",))
        row = cur.fetchone()
        if row and row[0]:
            lst = json.loads(row[0])
            if isinstance(lst, list) and lst:
                majors = {str(s).upper() for s in lst}
    except Exception:
        pass
    cfg["majors"] = majors
    return cfg


def ensure_cost_columns(cur):
    """Idempotent: voeg kostenkolommen toe als ze nog niet bestaan."""
    cur.execute("""
        ALTER TABLE mr_trail_trades
            ADD COLUMN IF NOT EXISTS fee_pct     DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS spread_pct  DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS pnl_net_pct DOUBLE PRECISION
    """)


def apply_costs(conn):
    """Vul fee/spread/netto op alle gesloten trades waar dat nog mist.
    Idempotent (alleen pnl_net_pct IS NULL). Commit zelf. Retourneert het
    aantal bijgewerkte rijen. 24h-volume = som quote_volume van de 4h-candles
    in de 24 uur vóór entry (USDT, ~EUR)."""
    with conn.cursor() as cur:
        ensure_cost_columns(cur)
        cfg = load_cfg(cur)
        cur.execute("""
            SELECT t.id, t.coin, t.pnl_pct,
                   (SELECT COALESCE(SUM(c.quote_volume), 0)
                      FROM candles c
                     WHERE c.symbol = t.coin AND c.timeframe = '4h'
                       AND c.open_time >= t.entry_ts - INTERVAL '24 hours'
                       AND c.open_time <  t.entry_ts) AS vol_24h
              FROM mr_trail_trades t
             WHERE t.status IN ('WIN','LOSS','TIME')
               AND t.pnl_pct IS NOT NULL
               AND t.pnl_net_pct IS NULL
        """)
        rows = cur.fetchall()
        updates = []
        for tid, coin, pnl, vol in rows:
            fee, spread, net = compute_costs(pnl, coin, float(vol or 0), cfg)
            updates.append((fee, spread, net, tid))
        if updates:
            cur.executemany("""
                UPDATE mr_trail_trades
                   SET fee_pct=%s, spread_pct=%s, pnl_net_pct=%s
                 WHERE id=%s AND pnl_net_pct IS NULL
            """, updates)
    conn.commit()
    return len(updates)


def main():
    import psycopg2
    url = os.environ["DATABASE_URL"]
    try:
        conn = psycopg2.connect(url)
    except psycopg2.OperationalError:
        # Lokaal draaien: interne Render-host -> externe host + ssl
        import re
        ext = re.sub(r"@(dpg-[a-z0-9]+-a)/",
                     r"@\1.frankfurt-postgres.render.com/", url)
        if "sslmode" not in ext:
            ext += ("&" if "?" in ext else "?") + "sslmode=require"
        conn = psycopg2.connect(ext)
    try:
        n = apply_costs(conn)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*), ROUND(AVG(pnl_pct)::numeric, 3),
                       ROUND(AVG(pnl_net_pct)::numeric, 3)
                  FROM mr_trail_trades WHERE pnl_net_pct IS NOT NULL
            """)
            tot, avg_b, avg_n = cur.fetchone()
        print(f"[mr-trail-cost] bijgewerkt {n} | totaal met kosten {tot} | "
              f"gem bruto {avg_b}% | gem netto {avg_n}%", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
