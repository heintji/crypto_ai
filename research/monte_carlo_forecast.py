#!/usr/bin/env python3
"""
Monte Carlo Forecast v1.0 — Toekomstvoorspelling op basis van trade data
========================================================================
Pakt alle MARKETSIM trades, simuleert 10.000 scenario's voor de
komende X trades, en berekent verwachte PnL, drawdown, en kansen.

Resultaten worden opgeslagen in bot_state als 'monte_carlo_forecast'
zodat het dashboard ze kan tonen.
"""
from __future__ import annotations
import os, sys, json, random, traceback
import psycopg2
from datetime import datetime, timezone
from typing import Dict, List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from bot_health_helper import health_update
except ImportError:
    def health_update(*a, **k): pass

DATABASE_URL = os.environ.get("DATABASE_URL", "")
NUM_SIMULATIONS = int(os.environ.get("MC_SIMULATIONS", "10000"))
FORECAST_TRADES = int(os.environ.get("MC_FORECAST_TRADES", "100"))
POSITION_EUR = float(os.environ.get("SIM_POSITION_EUR", "15.0"))
START_CAPITAL = float(os.environ.get("SIM_CAPITAL", "500.0"))


def log(msg):
    print(f"[FORECAST {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def db_connect():
    for attempt in range(3):
        try:
            return psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=10)
        except Exception as e:
            if attempt < 2:
                import time; time.sleep(3)
            else:
                raise


def get_historical_trades(conn) -> List[Dict]:
    """Haal alle MARKETSIM + REPLAY + SHADOW trades op."""
    cur = conn.cursor()
    cur.execute("""
        SELECT pnl_eur, result_r, exit_reden, market_regime, outcome
        FROM experience_trades
        WHERE source IN ('MARKETSIM', 'REPLAY', 'SHADOW')
        AND status = 'CLOSED'
        AND pnl_eur IS NOT NULL
        ORDER BY entry_time
    """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def run_monte_carlo(trades: List[Dict], n_sims: int, n_trades: int) -> Dict:
    """
    Monte Carlo simulatie.
    Trekt willekeurig trades uit de historische data en simuleert n_trades vooruit.
    Herhaalt dit n_sims keer.
    """
    pnls = [float(t["pnl_eur"]) for t in trades]
    r_multiples = [float(t["result_r"]) for t in trades]

    if not pnls:
        return {}

    # Basis stats
    avg_pnl = sum(pnls) / len(pnls)
    win_rate = len([p for p in pnls if p > 0]) / len(pnls) * 100
    avg_win = sum(p for p in pnls if p > 0) / max(1, len([p for p in pnls if p > 0]))
    avg_loss = sum(p for p in pnls if p <= 0) / max(1, len([p for p in pnls if p <= 0]))

    # Simulaties
    final_pnls = []
    max_drawdowns = []
    max_win_streaks = []
    max_loss_streaks = []
    equity_curves = []  # sample 100 curves voor visualisatie

    for sim in range(n_sims):
        capital = 0.0
        peak = 0.0
        max_dd = 0.0
        win_streak = 0
        loss_streak = 0
        best_win_streak = 0
        best_loss_streak = 0
        curve = [0.0]

        for _ in range(n_trades):
            # Trek willekeurige trade uit historische data
            pnl = random.choice(pnls)
            capital += pnl

            # Track equity curve
            if sim < 100:  # alleen eerste 100 opslaan
                curve.append(round(capital, 2))

            # Drawdown
            if capital > peak:
                peak = capital
            dd = peak - capital
            if dd > max_dd:
                max_dd = dd

            # Streaks
            if pnl > 0:
                win_streak += 1
                loss_streak = 0
                if win_streak > best_win_streak:
                    best_win_streak = win_streak
            else:
                loss_streak += 1
                win_streak = 0
                if loss_streak > best_loss_streak:
                    best_loss_streak = loss_streak

        final_pnls.append(round(capital, 2))
        max_drawdowns.append(round(max_dd, 2))
        max_win_streaks.append(best_win_streak)
        max_loss_streaks.append(best_loss_streak)
        if sim < 100:
            equity_curves.append(curve)

    # Sorteer voor percentielen
    final_pnls.sort()
    max_drawdowns.sort()
    max_loss_streaks.sort()

    def percentile(lst, p):
        idx = int(len(lst) * p / 100)
        return lst[min(idx, len(lst) - 1)]

    # Resultaten
    return {
        # Input stats
        "historische_trades": len(trades),
        "historische_wr": round(win_rate, 1),
        "historische_avg_pnl": round(avg_pnl, 4),
        "historische_avg_win": round(avg_win, 4),
        "historische_avg_loss": round(avg_loss, 4),

        # Forecast settings
        "simulaties": n_sims,
        "forecast_trades": n_trades,
        "positie_eur": POSITION_EUR,

        # PnL forecast
        "verwacht_pnl": round(sum(final_pnls) / len(final_pnls), 2),
        "pnl_mediaan": round(percentile(final_pnls, 50), 2),
        "pnl_p5": round(percentile(final_pnls, 5), 2),    # worst 5%
        "pnl_p10": round(percentile(final_pnls, 10), 2),
        "pnl_p25": round(percentile(final_pnls, 25), 2),
        "pnl_p75": round(percentile(final_pnls, 75), 2),
        "pnl_p90": round(percentile(final_pnls, 90), 2),
        "pnl_p95": round(percentile(final_pnls, 95), 2),   # best 5%
        "pnl_min": final_pnls[0],
        "pnl_max": final_pnls[-1],

        # Kans op winst
        "kans_winst": round(len([p for p in final_pnls if p > 0]) / len(final_pnls) * 100, 1),
        "kans_verlies_20": round(len([p for p in final_pnls if p < -20]) / len(final_pnls) * 100, 1),

        # Drawdown
        "verwacht_max_dd": round(sum(max_drawdowns) / len(max_drawdowns), 2),
        "dd_mediaan": round(percentile(max_drawdowns, 50), 2),
        "dd_p95": round(percentile(max_drawdowns, 95), 2),  # worst 5%

        # Streaks
        "verwacht_max_loss_streak": round(sum(max_loss_streaks) / len(max_loss_streaks), 1),
        "loss_streak_p95": percentile(max_loss_streaks, 95),
        "verwacht_max_win_streak": round(sum(max_win_streaks) / len(max_win_streaks), 1),

        # Per maand schatting (aanname: ~30 trades/maand)
        "trades_per_maand_schatting": 30,
        "verwacht_pnl_per_maand": round(avg_pnl * 30, 2),
        "pnl_per_maand_range": [
            round(percentile(final_pnls, 10) / (n_trades / 30), 2),
            round(percentile(final_pnls, 90) / (n_trades / 30), 2),
        ],

        # Equity curves (sample van 100)
        "equity_curves_sample": equity_curves[:20],  # 20 curves voor dashboard

        # Timestamp
        "run_date": datetime.now(timezone.utc).isoformat(),
    }


def run_regime_analysis(trades: List[Dict]) -> Dict:
    """Analyseer performance per markt regime."""
    regimes = {}
    for t in trades:
        regime = t.get("market_regime") or "UNKNOWN"
        if regime not in regimes:
            regimes[regime] = {"trades": 0, "wins": 0, "pnl": 0.0, "pnls": []}
        regimes[regime]["trades"] += 1
        if t["outcome"] == "WIN":
            regimes[regime]["wins"] += 1
        pnl = float(t["pnl_eur"])
        regimes[regime]["pnl"] += pnl
        regimes[regime]["pnls"].append(pnl)

    result = {}
    for regime, data in regimes.items():
        n = data["trades"]
        if n < 3:
            continue
        pnls = data["pnls"]
        avg_pnl = sum(pnls) / n
        result[regime] = {
            "trades": n,
            "wins": data["wins"],
            "wr": round(data["wins"] / n * 100, 1),
            "pnl": round(data["pnl"], 2),
            "avg_pnl": round(avg_pnl, 4),
            "verwacht_per_30_trades": round(avg_pnl * 30, 2),
        }

    return result


def run_exit_analysis(trades: List[Dict]) -> Dict:
    """Analyseer welke exit strategieen het beste werken."""
    exits = {}
    for t in trades:
        reden = t.get("exit_reden", "UNKNOWN")
        if reden not in exits:
            exits[reden] = {"trades": 0, "wins": 0, "pnl": 0.0, "r_sum": 0.0}
        exits[reden]["trades"] += 1
        if t["outcome"] == "WIN":
            exits[reden]["wins"] += 1
        exits[reden]["pnl"] += float(t["pnl_eur"])
        exits[reden]["r_sum"] += float(t["result_r"])

    result = {}
    for reden, data in exits.items():
        n = data["trades"]
        result[reden] = {
            "trades": n,
            "wins": data["wins"],
            "wr": round(data["wins"] / n * 100, 1),
            "pnl": round(data["pnl"], 2),
            "avg_r": round(data["r_sum"] / n, 3),
        }

    return result


def run():
    log("=" * 60)
    log("Monte Carlo Forecast v1.0")
    log(f"Simulaties: {NUM_SIMULATIONS} | Forecast: {FORECAST_TRADES} trades")
    log("=" * 60)

    if not DATABASE_URL:
        log("FOUT: DATABASE_URL niet ingesteld")
        return

    try:
        conn = db_connect()
    except Exception as e:
        log(f"DB fout: {e}")
        return

    try:
        # Haal trades op
        trades = get_historical_trades(conn)
        log(f"Historische trades: {len(trades)}")

        if len(trades) < 20:
            log("Te weinig trades voor betrouwbare forecast (min 20)")
            health_update("monte_carlo", "ERROR", f"Te weinig trades: {len(trades)}")
            return

        # Monte Carlo
        log("Monte Carlo simulatie draaien...")
        forecast = run_monte_carlo(trades, NUM_SIMULATIONS, FORECAST_TRADES)

        # Regime analyse
        log("Regime analyse...")
        regime_data = run_regime_analysis(trades)

        # Exit analyse
        log("Exit strategie analyse...")
        exit_data = run_exit_analysis(trades)

        # Print resultaten
        log("")
        log("=" * 60)
        log("FORECAST RESULTATEN")
        log("=" * 60)
        log(f"  Gebaseerd op:     {forecast['historische_trades']} trades ({forecast['historische_wr']}% WR)")
        log(f"  Simulaties:       {NUM_SIMULATIONS}")
        log(f"  Voorspelling:     komende {FORECAST_TRADES} trades")
        log("")
        log(f"  VERWACHTE PnL:    EUR {forecast['verwacht_pnl']:+.2f}")
        log(f"  Mediaan PnL:      EUR {forecast['pnl_mediaan']:+.2f}")
        log(f"  Best case (95%):  EUR {forecast['pnl_p95']:+.2f}")
        log(f"  Worst case (5%):  EUR {forecast['pnl_p5']:+.2f}")
        log(f"  Kans op winst:    {forecast['kans_winst']}%")
        log(f"  Kans verlies>20:  {forecast['kans_verlies_20']}%")
        log("")
        log(f"  Max drawdown (gem):  EUR {forecast['verwacht_max_dd']:.2f}")
        log(f"  Max drawdown (95%):  EUR {forecast['dd_p95']:.2f}")
        log(f"  Max verliesreeks:    {forecast['verwacht_max_loss_streak']:.0f} trades (95%: {forecast['loss_streak_p95']})")
        log("")
        log(f"  Per maand (~30 trades):")
        log(f"    Verwacht:  EUR {forecast['verwacht_pnl_per_maand']:+.2f}")
        log(f"    Range:     EUR {forecast['pnl_per_maand_range'][0]:+.2f} tot {forecast['pnl_per_maand_range'][1]:+.2f}")
        log("")
        log("  Per regime:")
        for regime, data in sorted(regime_data.items()):
            log(f"    {regime:<12} {data['trades']:>4} trades  {data['wr']}% WR  PnL={data['pnl']:+.2f}  (~{data['verwacht_per_30_trades']:+.2f}/maand)")
        log("")
        log("  Per exit strategie:")
        for reden, data in sorted(exit_data.items(), key=lambda x: x[1]["trades"], reverse=True):
            log(f"    {reden:<20} {data['trades']:>4} trades  {data['wr']}% WR  avg_R={data['avg_r']:+.3f}")
        log("=" * 60)

        # Opslaan in DB
        log("Opslaan in DB...")
        cur = conn.cursor()

        full_results = {
            "forecast": forecast,
            "regime": regime_data,
            "exit_analyse": exit_data,
        }

        cur.execute(
            "INSERT INTO bot_state (key,value) VALUES ('monte_carlo_forecast',%s) "
            "ON CONFLICT (key) DO UPDATE SET value=%s",
            (json.dumps(full_results), json.dumps(full_results))
        )
        conn.commit()
        log("Opgeslagen in bot_state")

        health_update("monte_carlo", "OK",
                      f"Forecast: {forecast['kans_winst']}% winstkans, "
                      f"PnL={forecast['verwacht_pnl']:+.2f} over {FORECAST_TRADES} trades")
        log("Klaar!")

    except Exception as e:
        log(f"FOUT: {e}")
        traceback.print_exc()
        health_update("monte_carlo", "ERROR", str(e)[:200])
    finally:
        conn.close()


if __name__ == "__main__":
    run()
