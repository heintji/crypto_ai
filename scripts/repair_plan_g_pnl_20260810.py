#!/usr/bin/env python3
"""EENMALIGE reparatie — kapotte Plan G-shadow-closes van 9/10-8-2026.

WAT ER MIS WAS (zie research/plan_g_shadow.py, close_pass-docstring):
  close_pass vergeleek de EUR-entry (Bitvavo, via pending_approvals) met een
  live Binance-USDT-ticker. Daardoor sloten sinds 2026-08-09 22:36 vrijwel
  alle shadows direct als nep-TARGET_HIT (+15,6% gem. = EURUSD-koers) of
  nep-STOP_LOSS (-98,2%, FUN: ander geschaald asset op Binance).

WAT DIT SCRIPT DOET:
  Zet ALLEEN die kapotte rijen (is_shadow, source='SHADOW', CLOSED,
  closed_at >= 2026-08-09 22:36 UTC) terug op status OPEN en wist de
  exit-velden. De GEFIXTE candle-based close_pass herberekent ze daarna
  deterministisch uit de `candles`-tabel (zelfde EUR-bron als de entry),
  max 40 per pass -> binnen enkele cron-runs allemaal correct afgehandeld.
  Pre-26-6-data en alle niet-shadow-rijen worden NIET aangeraakt.

VOLGORDE (belangrijk!):
  EERST de gefixte plan_g_shadow.py deployen, DAN dit script draaien.
  Zolang de oude code nog als cron draait, zou die de teruggezette rijen
  direct weer fout sluiten.

GEBRUIK:
  python scripts/repair_plan_g_pnl_20260810.py            # dry-run (default)
  python scripts/repair_plan_g_pnl_20260810.py --voer-uit # echt uitvoeren

Env: DATABASE_URL.
"""
import os
import sys

import psycopg2

CUTOFF = "2026-08-09 22:36+00"

WHERE = """
    is_shadow = TRUE
    AND source = 'SHADOW'
    AND status = 'CLOSED'
    AND trade_key LIKE 'SHADOW|%%'
    AND closed_at >= %s
"""


def main() -> None:
    voer_uit = "--voer-uit" in sys.argv
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        print("KRITIEK: DATABASE_URL ontbreekt")
        sys.exit(1)
    conn = psycopg2.connect(url, sslmode="require", connect_timeout=15)
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE pnl_pct > 10),
                       COUNT(*) FILTER (WHERE pnl_pct < -50),
                       MIN(closed_at), MAX(closed_at)
                FROM experience_trades WHERE {WHERE}
            """, (CUTOFF,))
            n, n_fake_win, n_fake_loss, eerste, laatste = cur.fetchone()
            print(f"kapotte rijen sinds {CUTOFF}: {n} "
                  f"(nep-wins>+10%: {n_fake_win}, nep-losses<-50%: {n_fake_loss})")
            print(f"closed_at bereik: {eerste} .. {laatste}")
            if n == 0:
                print("niets te doen")
                return
            if not voer_uit:
                print("\nDRY-RUN — geen wijzigingen. Draai met --voer-uit om de "
                      "rijen op OPEN terug te zetten (pas NA deploy van de fix!).")
                return
            cur.execute(f"""
                UPDATE experience_trades SET
                    status='OPEN', outcome='OPEN',
                    pnl_eur=NULL, pnl_pct=NULL, result_r=NULL,
                    exit_price=NULL, closed_at=NULL, exit_time=NULL,
                    exit_reden=NULL, mfe_r=NULL, mae_r=NULL,
                    max_price_seen=NULL, min_price_seen=NULL,
                    monitor_updated_at=NOW()
                WHERE {WHERE}
            """, (CUTOFF,))
            print(f"teruggezet op OPEN: {cur.rowcount} rijen")
        conn.commit()
        print("klaar — de gefixte close-pass herberekent ze candle-based "
              "(max 40 per 15-min pass).")
    except Exception as e:
        conn.rollback()
        print(f"FOUT — alles teruggedraaid: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
