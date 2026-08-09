#!/usr/bin/env python3
"""Regime-watcher (Hein 10-8).

Legt periodiek VAST of de bot de marktsoort betrouwbaar herkent, zodat we later
kunnen terugkijken. Puur meten: raakt geen strategie, plaatst geen orders, wijzigt
geen bestaande tabellen. Schrijft hooguit 1x per ~20u een rij naar `regime_watch`.

Per snapshot: het regime volgens de 4h-builder, volgens bot_state, en volgens
Plan U's eigen bepaling, plus of ze het EENS zijn en hoe vers de candle-data is.
Zo zien we achteraf of de herkenning betrouwbaar was (versheid + consistentie).

Lift mee op de bestaande multi_coin_score-cron; eigen try/except zodat het de scan
nooit kan breken.
"""
import os
from datetime import datetime, timezone

import psycopg2


def _connect():
    return psycopg2.connect(os.environ["DATABASE_URL"], sslmode="require",
                            connect_timeout=25)


def _norm(x: str) -> str:
    """Verschillende vocabulaires op één noemer: daling / stijging / range."""
    x = (x or "").strip().upper()
    if x in ("ROOD", "STORM", "BEAR", "DALEND", "CRASH"):
        return "DALING"
    if x in ("GROEN", "BULL", "STIJGEND", "UP"):
        return "STIJGING"
    if x in ("RANGE", "ZIJWAARTS", "NEUTRAL", "SIDEWAYS"):
        return "RANGE"
    return x or "ONBEKEND"


def regimes_eens(labels) -> bool:
    """True als alle aanwezige regime-labels dezelfde marktrichting aanwijzen."""
    genormaliseerd = {_norm(x) for x in labels if x}
    return len(genormaliseerd) <= 1 if genormaliseerd else False


def ensure_table(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS regime_watch (
            id               SERIAL PRIMARY KEY,
            ts               TIMESTAMPTZ DEFAULT NOW(),
            regime_4h        TEXT,
            regime_bot_state TEXT,
            regime_plan_u    TEXT,
            eens             BOOLEAN,
            candle_lag_uur   NUMERIC,
            btc_prijs        NUMERIC,
            ema200_4h        NUMERIC
        )
    """)


def main() -> None:
    conn = _connect()
    try:
        # Tabel apart committen, zodat een latere fout de aanmaak niet terugdraait.
        with conn.cursor() as cur:
            ensure_table(cur)
        conn.commit()

        with conn.cursor() as cur:
            # Idempotent: max 1 snapshot per ~20u (piggyback draait elke 15 min).
            cur.execute("SELECT 1 FROM regime_watch WHERE ts > NOW() - INTERVAL '20 hours' LIMIT 1")
            if cur.fetchone():
                conn.commit()
                return

            # 4h-builder: nieuwste candle + versheid
            regime_4h = btc = ema = lag = None
            cur.execute("SELECT regime, close, ema200, open_time FROM btc_regime_4h "
                        "ORDER BY open_time DESC LIMIT 1")
            row = cur.fetchone()
            if row:
                regime_4h = row[0]
                btc = float(row[1]) if row[1] is not None else None
                ema = float(row[2]) if row[2] is not None else None
                if row[3]:
                    lag = round((datetime.now(timezone.utc) - row[3]).total_seconds() / 3600, 1)

            # bot_state (wat de live-gate leest)
            cur.execute("SELECT value FROM bot_state WHERE key='btc_regime_huidig'")
            r = cur.fetchone()
            regime_bs = r[0] if r else None

            # Plan U's eigen bepaling — savepoint zodat een afwijkend schema de
            # snapshot-insert niet terugdraait.
            regime_pu = None
            cur.execute("SAVEPOINT sp_plan_u")
            try:
                cur.execute("SELECT regime FROM plan_u_regime ORDER BY ts DESC LIMIT 1")
                r = cur.fetchone()
                regime_pu = r[0] if r else None
                cur.execute("RELEASE SAVEPOINT sp_plan_u")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_plan_u")

            eens = regimes_eens([regime_4h, regime_bs, regime_pu])
            cur.execute(
                """INSERT INTO regime_watch
                     (regime_4h, regime_bot_state, regime_plan_u, eens,
                      candle_lag_uur, btc_prijs, ema200_4h)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (regime_4h, regime_bs, regime_pu, eens, lag, btc, ema),
            )
            conn.commit()
            print(f"[regime-watch] 4h={regime_4h} bot_state={regime_bs} plan_u={regime_pu} "
                  f"eens={eens} lag={lag}u", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
