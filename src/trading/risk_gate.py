"""
risk_gate.py - Fase 4: Risico Poortwachter met Cross-Referencing
=================================================================
Versie: 1.0

Evalueert of een trade veilig is op basis van:
  1. BTC regime (BULL/RANGE/BEAR)
  2. Fear en Greed Index
  3. Binance Open Interest trend
  4. BTC dominantie
  5. Funding rate voor het specifieke coin
  6. Bot limieten (drawdown, consecutive losses, daily max)

Geeft GO / NO-GO terug met uitleg.
In BEAR regime altijd NO-GO tenzij overrule.
"""
from __future__ import annotations
import os, sys, time, psycopg2
from datetime import datetime, timezone
from typing import Dict, Optional

DATABASE_URL = os.environ.get("DATABASE_URL", "")

def now_utc(): return datetime.now(timezone.utc)
def log(m): print(f"[RISKGATE {now_utc():%H:%M:%S}] {m}", flush=True)
def sf(v, d=0.0):
    try: return float(v or d)
    except: return d
def safe_rb(conn):
    try: conn.rollback()
    except: pass

def db_connect(retries=3):
    for p in range(1, retries+1):
        try:
            conn=psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=10)
            conn.autocommit=False; return conn
        except Exception as e:
            log(f"DB poging {p}/{retries}: {e}")
            if p<retries: time.sleep(3)
    raise RuntimeError(f"DB verbinding mislukt na {retries} pogingen.")

def evalueer_trade(symbool: str, score: int, regime: str) -> Dict:
    """
    Hoofdfunctie: evalueert of een trade GO of NO-GO is.

    Doorloopt alle checks in volgorde:
    1. Hard blocks: BEAR regime, drawdown pauze, consecutive loss pauze
    2. Cross-referencing: externe marktdata score
    3. Coin-specifiek: funding rate bonus/malus

    Args:
        symbool: Handelspaar bijv BTCUSDT
        score:   Signaal score van multi_coin_score (0-100)
        regime:  BTC marktregime BULL/RANGE/BEAR

    Returns:
        dict met go (bool), advies (GO/NO-GO), reden, score_aanpassing
    """
    conn = db_connect()
    try:
        from market_data import MarketData
        md = MarketData(conn)

        checks = []
        score_aanpassing = 0

        # Check 1: BEAR regime = altijd NO-GO
        if regime == "BEAR":
            return {"go":False,"advies":"NO-GO","reden":"BTC regime=BEAR: geen long trades",
                "score_aanpassing":0,"checks":checks}

        # Check 1b: Extreme Greed blokkering (F&G > 80)
        # Backtest op 43.021 SIM trades toonde aan: WR=28.9% bij Extreme Greed
        # vs baseline 34.1% => 5.2% slechter. Dit filter heeft aantoonbaar waarde.
        try:
            fng_r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
            fng_val = int(fng_r.json()["data"][0]["value"])
            if fng_val > 80:
                return {"go":False,"advies":"NO-GO",
                    "reden":f"Extreme Greed geblokkeerd: F&G={fng_val}/100 (>80). Backtest: WR 28.9% vs 34.1% baseline.",
                    "score_aanpassing":0,"checks":[f"F&G:{fng_val} EXTREME_GREED"]}
            checks.append(f"F&G:{fng_val} OK")
        except Exception as _fe:
            log(f"F&G check fout (skip): {_fe}")

        # Check 2: Drawdown pauze
        with conn.cursor() as cur:
            cur.execute("""SELECT COALESCE(SUM(pnl_eur),0) FROM public.experience_trades
                WHERE UPPER(source)='LIVE' AND entry_time>NOW()-INTERVAL'1 day'""" )
            dagpnl = sf(cur.fetchone()[0])
        if dagpnl <= -5.0:
            return {"go":False,"advies":"NO-GO","reden":f"Drawdown pauze: dag PnL={dagpnl:.2f} EUR (limiet -5 EUR)",
                "score_aanpassing":0,"checks":checks}
        checks.append(f"DagPnL:{dagpnl:+.2f}EUR OK")

        # Check 3: Consecutive losses pauze (3 op rij = 2u wachten)
        with conn.cursor() as cur:
            cur.execute("""SELECT outcome FROM public.experience_trades
                WHERE UPPER(source)='LIVE' ORDER BY exit_time DESC LIMIT 3""")
            laatste = [r[0] for r in cur.fetchall()]
        if len(laatste)==3 and all(o and o.upper()=="LOSS" for o in laatste):
            with conn.cursor() as cur:
                cur.execute("""SELECT exit_time FROM public.experience_trades
                    WHERE UPPER(source)='LIVE' ORDER BY exit_time DESC LIMIT 1""")
                rij=cur.fetchone()
            if rij:
                seconden=(now_utc()-rij[0].replace(tzinfo=timezone.utc)).total_seconds()
                if seconden < 7200:
                    return {"go":False,"advies":"NO-GO","reden":f"3 consecutive losses, wacht nog {int((7200-seconden)/60)} min",
                        "score_aanpassing":0,"checks":checks}
        checks.append("ConsecLoss: OK")

        # Check 4: Cross-referencing marktdata
        advies_data = md.cross_reference_advies()
        cross_score = advies_data.get("totaal_score", 0)
        cross_advies = advies_data.get("advies", "VOORZICHTIG")
        checks.append(advies_data.get("uitleg", ""))

        if cross_advies == "STOP":
            return {"go":False,"advies":"NO-GO",
                "reden":f"Cross-ref STOP: {advies_data.get('uitleg','')}",
                "score_aanpassing":0,"checks":checks}

        # Cross-ref score beinvloedt coin score
        if cross_advies == "GO":
            score_aanpassing += 5   # Markt gunstig: +5
        elif cross_advies == "VOORZICHTIG":
            score_aanpassing += 0   # Neutraal
        checks.append(f"CrossRef:{cross_advies} ({cross_score:+d})")

        # Check 5: Funding rate coin-specifiek
        fr_bonus = md.funding_rate_bonus(symbool)
        score_aanpassing += fr_bonus
        fr_sig = md.haal_funding_rate_coin_op(symbool)
        checks.append(f"FR:{fr_sig.get('label','?')}, bonus:{fr_bonus:+d}")

        # Minimale score na aanpassing
        score_effectief = score + score_aanpassing
        if score_effectief < 70:
            return {"go":False,"advies":"NO-GO",
                "reden":f"Score te laag na aanpassing: {score}+{score_aanpassing}={score_effectief} (min 70)",
                "score_aanpassing":score_aanpassing,"checks":checks}

        # Alles OK -> GO
        reden = f"Score:{score}+{score_aanpassing}={score_effectief} | {cross_advies} | {fr_sig.get('label','?')}"
        log(f"GO: {symbool} {reden}")
        return {"go":True,"advies":"GO","reden":reden,
            "score_aanpassing":score_aanpassing,"score_effectief":score_effectief,
            "cross_advies":cross_advies,"checks":checks}

    except Exception as e:
        log(f"Risk gate fout: {e}"
        ); safe_rb(conn)
        # Bij fout: laat trade door maar log
        return {"go":True,"advies":"GO","reden":f"Risk gate fout (fallback GO): {e}",
            "score_aanpassing":0,"checks":[]}
    finally:
        conn.close()

if __name__ == "__main__":
    print(evalueer_trade("BTCUSDT", 80, "RANGE"))
