#!/usr/bin/env python3
"""
Market Simulator v2.0 — Simuleert bot op ECHTE signalen + candle data
=====================================================================
Gebruikt echte pending_approvals signalen (niet eigen detectie).
Past EXACTE monitor logica toe (trailing stop, 1R/2R, structuur, hold).
Resultaat: hoe had de bot gepresteerd over de hele signaal historie.

source='MARKETSIM' in experience_trades.
"""
from __future__ import annotations
import os, sys, json, time, traceback
import psycopg2
from datetime import datetime, timezone
from typing import Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from bot_health_helper import health_update
except ImportError:
    def health_update(*a, **k): pass

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Configuratie (exact als live bot)
SCORE_DREMPEL = int(os.environ.get("SIM_SCORE_DREMPEL", "85"))
MAX_OPEN = int(os.environ.get("SIM_MAX_OPEN", "10"))
POSITION_EUR = float(os.environ.get("SIM_POSITION_EUR", "15.0"))
MAX_HOLD_H = float(os.environ.get("SIM_MAX_HOLD", "36"))
TRAILING_PCT = float(os.environ.get("SIM_TRAILING_PCT", "0.03"))
FEE_PCT = 0.0025
SLIP_PCT = 0.001
SPREAD_PCT = 0.0015
COOLDOWN_H = 24
START_CAPITAL = float(os.environ.get("SIM_CAPITAL", "500.0"))

def log(msg):
    print(f"[MARKETSIM {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)

def db_connect():
    for attempt in range(3):
        try:
            conn = psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=10)
            return conn
        except Exception as e:
            log(f"DB connect poging {attempt+1}/3 mislukt: {e}")
            if attempt < 2:
                time.sleep(3)
            else:
                raise


def get_signals(conn) -> List[Dict]:
    """Haal alle echte signalen op uit pending_approvals."""
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT id, symbol, score, entry, stop, target, setup_type,
                   COALESCE(regime, '') AS btc_regime, created_at,
                   EXTRACT(EPOCH FROM created_at) * 1000 AS ts_ms
            FROM pending_approvals
            WHERE score >= %s
            AND entry IS NOT NULL AND entry > 0
            AND stop IS NOT NULL AND stop > 0
            AND target IS NOT NULL AND target > 0
            ORDER BY created_at ASC
        """, (SCORE_DREMPEL,))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        log(f"  Query returned {len(rows)} signalen (score >= {SCORE_DREMPEL})")
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        log(f"FOUT in get_signals: {e}")
        traceback.print_exc()
        # Probeer met minimale kolommen als fallback
        try:
            conn.rollback()
            cur.execute("""
                SELECT id, symbol, score, entry, stop, target, setup_type,
                       '' AS btc_regime, created_at,
                       EXTRACT(EPOCH FROM created_at) * 1000 AS ts_ms
                FROM pending_approvals
                WHERE score >= %s
                AND entry IS NOT NULL AND entry > 0
                AND stop IS NOT NULL AND stop > 0
                AND target IS NOT NULL AND target > 0
                ORDER BY created_at ASC
            """, (SCORE_DREMPEL,))
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            log(f"  Fallback query returned {len(rows)} signalen")
            return [dict(zip(cols, r)) for r in rows]
        except Exception as e2:
            log(f"FOUT in fallback query: {e2}")
            return []


def get_candles_for_symbol(conn, symbol: str) -> List[Dict]:
    """Haal alle 1h candles op voor een symbol, gesorteerd op tijd."""
    cur = conn.cursor()
    cur.execute("""
        SELECT open, high, low, close, volume,
               EXTRACT(EPOCH FROM open_time) * 1000 AS ts_ms
        FROM candles
        WHERE symbol = %s AND timeframe = '1h'
        ORDER BY open_time ASC
    """, (symbol,))
    return [{"open": float(r[0]), "high": float(r[1]), "low": float(r[2]),
             "close": float(r[3]), "volume": float(r[4]), "ts_ms": int(r[5])}
            for r in cur.fetchall()]


def simulate_trade(signal: Dict, candles: List[Dict]) -> Optional[Dict]:
    """
    Simuleer een trade op candle data met volledige monitor logica.
    Exact zoals trade_monitor.py: trailing stop, 1R/2R, structuur, max hold.
    """
    entry = float(signal["entry"]) * (1 + SPREAD_PCT + SLIP_PCT)
    stop = float(signal["stop"])
    target = float(signal["target"])
    score = signal["score"]
    open_ts = float(signal["ts_ms"])

    original_stop = stop
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    # Vind candles NA het signaal
    future = [c for c in candles if c["ts_ms"] > open_ts]
    if len(future) < 2:
        return None

    max_candles = int(MAX_HOLD_H)  # 1h candles = uren
    max_price = entry
    min_price = entry
    had_1r = False
    had_2r = False
    mode = "NORMAL"
    struct_high = 0

    for i, c in enumerate(future[:max_candles]):
        high = c["high"]
        low = c["low"]
        close = c["close"]

        max_price = max(max_price, high)
        min_price = min(min_price, low)
        r = (close - entry) / risk

        # 1. STOP LOSS
        if low <= stop:
            return _build_result(signal, stop, open_ts, c["ts_ms"], entry, original_stop,
                                 target, max_price, min_price, risk, "STOP_LOSS")

        # 2. TARGET → STRUCTUUR mode
        if high >= target and mode == "NORMAL":
            mode = "STRUCTUUR"
            struct_high = high

        # 3. STRUCTUUR break (-1%)
        if mode == "STRUCTUUR":
            struct_high = max(struct_high, high)
            if close < struct_high * 0.99:
                return _build_result(signal, close, open_ts, c["ts_ms"], entry, original_stop,
                                     target, max_price, min_price, risk, "STRUCTUUR_BREAK")

        # 4. 1R → stop naar break-even
        if r >= 1.0 and not had_1r:
            had_1r = True
            stop = entry

        # 5. 2R → stop naar +1R
        if r >= 2.0 and not had_2r:
            had_2r = True
            stop = entry + risk

        # 6. Trailing stop
        if close > entry:
            trailing = close * (1 - TRAILING_PCT)
            if trailing > stop:
                stop = trailing

    # MAX HOLD bereikt — sluit op laatste close
    if future:
        last = future[min(max_candles - 1, len(future) - 1)]
        return _build_result(signal, last["close"], open_ts, last["ts_ms"], entry, original_stop,
                             target, max_price, min_price, risk, "MAX_HOLD")

    return None


def _build_result(signal, exit_price, open_ts, close_ts, entry, original_stop,
                  target, max_price, min_price, risk, reden):
    """Bouw trade resultaat met fee/slippage."""
    qty = POSITION_EUR / entry if entry > 0 else 0
    entry_cost = POSITION_EUR * (FEE_PCT + SLIP_PCT + SPREAD_PCT)
    exit_cost = exit_price * qty * (FEE_PCT + SLIP_PCT)
    gross_pnl = (exit_price - entry) * qty
    net_pnl = gross_pnl - entry_cost - exit_cost

    mfe_r = round((max_price - entry) / risk, 3) if risk > 0 else 0
    mae_r = round((entry - min_price) / risk, 3) if risk > 0 else 0
    result_r = round((exit_price - entry) / risk, 3) if risk > 0 else 0
    hold_h = round((close_ts - open_ts) / 3600000, 1)

    return {
        "symbol": signal["symbol"],
        "signal_id": signal["id"],
        "entry": entry,
        "exit_price": exit_price,
        "stop": original_stop,
        "target": target,
        "score": signal["score"],
        "setup_type": signal.get("setup_type", "TREND_PULLBACK"),
        "regime": signal.get("btc_regime", ""),
        "outcome": "WIN" if net_pnl > 0 else "LOSS",
        "pnl_eur": round(net_pnl, 4),
        "result_r": result_r,
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "hold_h": hold_h,
        "exit_reden": reden,
        "open_ts": open_ts,
        "close_ts": close_ts,
    }


def run():
    log("=" * 60)
    log("Market Simulator v2.1 — Echte Signalen + Candle Data")
    log(f"Score: {SCORE_DREMPEL} | Hold: {MAX_HOLD_H}h | EUR: {POSITION_EUR}")
    log(f"Trailing: {TRAILING_PCT*100}% | Cooldown: {COOLDOWN_H}h | Capital: {START_CAPITAL}")
    log("=" * 60)

    if not DATABASE_URL:
        log("FOUT: DATABASE_URL niet ingesteld!")
        health_update("market_simulator", "ERROR", "DATABASE_URL niet ingesteld")
        return

    try:
        conn = db_connect()
        log("DB verbinding OK")
    except Exception as e:
        log(f"FOUT: kan niet verbinden met DB: {e}")
        health_update("market_simulator", "ERROR", f"DB connect: {e}")
        return

    try:
        _run_simulation(conn)
    except Exception as e:
        log(f"FATALE FOUT: {e}")
        traceback.print_exc()
        health_update("market_simulator", "ERROR", str(e)[:200])
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _run_simulation(conn):
    """Hoofdlogica van de simulatie — apart zodat run() altijd opruimt."""

    # Haal echte signalen
    signals = get_signals(conn)
    log(f"Signalen totaal: {len(signals)}")

    if not signals:
        log("WAARSCHUWING: Geen signalen gevonden! Controleer pending_approvals tabel.")
        # Debug: check wat er in de tabel zit
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM pending_approvals")
            total_pa = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM pending_approvals WHERE score >= %s", (SCORE_DREMPEL,))
            scored_pa = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM pending_approvals WHERE entry > 0 AND stop > 0 AND target > 0")
            valid_pa = cur.fetchone()[0]
            log(f"  pending_approvals totaal: {total_pa}")
            log(f"  score >= {SCORE_DREMPEL}: {scored_pa}")
            log(f"  entry/stop/target > 0: {valid_pa}")
            # Sample een paar rijen
            cur.execute("SELECT id, symbol, score, entry, stop, target FROM pending_approvals ORDER BY created_at DESC LIMIT 3")
            for row in cur.fetchall():
                log(f"  voorbeeld: id={row[0]} sym={row[1]} score={row[2]} entry={row[3]} stop={row[4]} target={row[5]}")
        except Exception as e:
            log(f"  Debug query fout: {e}")
        health_update("market_simulator", "ERROR", "0 signalen gevonden")
        return

    # Blacklist
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM bot_state WHERE key='coin_blacklist'")
        r = cur.fetchone()
        blacklist = set(json.loads(r[0])) if r and r[0] else set()
    except Exception:
        blacklist = set()
    log(f"Blacklist: {len(blacklist)} coins")

    # Pre-load candles per symbol
    symbols_needed = set(s["symbol"] for s in signals if s["symbol"] not in blacklist)
    log(f"Candles laden voor {len(symbols_needed)} coins...")
    candle_cache = {}
    no_candles = []
    for sym in symbols_needed:
        try:
            candles = get_candles_for_symbol(conn, sym)
            if len(candles) >= 10:
                candle_cache[sym] = candles
            else:
                no_candles.append(f"{sym}({len(candles)})")
        except Exception as e:
            log(f"  Candle fout voor {sym}: {e}")
    log(f"  {len(candle_cache)} coins met candle data")
    if no_candles and len(no_candles) <= 10:
        log(f"  Geen/weinig candles: {', '.join(no_candles)}")
    elif no_candles:
        log(f"  {len(no_candles)} coins zonder voldoende candle data")

    # Simulatie
    log("Simulatie starten...")
    capital = START_CAPITAL
    equity = []
    closed = []
    open_symbols = set()
    cooldown = {}
    skipped = 0

    for i, sig in enumerate(signals):
        sym = sig["symbol"]
        ts = float(sig["ts_ms"])

        # Filters
        if sym in blacklist:
            skipped += 1; continue
        if sym not in candle_cache:
            skipped += 1; continue
        if sym in open_symbols:
            skipped += 1; continue
        if len(open_symbols) >= MAX_OPEN:
            skipped += 1; continue
        if sym in cooldown and ts < cooldown[sym]:
            skipped += 1; continue

        # Simuleer
        try:
            result = simulate_trade(sig, candle_cache[sym])
        except Exception as e:
            log(f"  Sim fout {sym}: {e}")
            skipped += 1; continue

        if not result:
            skipped += 1; continue

        closed.append(result)
        capital += result["pnl_eur"]

        if result["outcome"] == "LOSS":
            cooldown[sym] = ts + COOLDOWN_H * 3600 * 1000

        equity.append({"ts": ts, "capital": round(capital, 2)})

        # Progress
        if (i + 1) % 500 == 0:
            wins = len([t for t in closed if t["outcome"] == "WIN"])
            total = len(closed)
            wr = round(wins / total * 100, 1) if total > 0 else 0
            log(f"  [{i+1}/{len(signals)}] {total} trades, {wr}% WR, capital={capital:.2f}")

    # Resultaten
    wins = len([t for t in closed if t["outcome"] == "WIN"])
    losses = len([t for t in closed if t["outcome"] == "LOSS"])
    total = wins + losses
    wr = round(wins / total * 100, 1) if total > 0 else 0
    total_pnl = sum(t["pnl_eur"] for t in closed)
    avg_r = sum(t["result_r"] for t in closed) / total if total > 0 else 0
    avg_mfe = sum(t["mfe_r"] for t in closed) / total if total > 0 else 0
    avg_mae = sum(t["mae_r"] for t in closed) / total if total > 0 else 0

    # Drawdown
    peak = START_CAPITAL
    max_dd = 0
    for eq in equity:
        if eq["capital"] > peak: peak = eq["capital"]
        dd = peak - eq["capital"]
        if dd > max_dd: max_dd = dd

    # Per reden
    by_reden = {}
    for t in closed:
        r = t["exit_reden"]
        if r not in by_reden:
            by_reden[r] = {"n": 0, "w": 0}
        by_reden[r]["n"] += 1
        if t["outcome"] == "WIN":
            by_reden[r]["w"] += 1

    log("")
    log("=" * 60)
    log("RESULTATEN")
    log("=" * 60)
    log(f"  Signalen:     {len(signals)} (verwerkt: {total}, overgeslagen: {skipped})")
    log(f"  Trades:       {total} ({wins}W/{losses}L)")
    log(f"  Win rate:     {wr}%")
    log(f"  PnL:          EUR {total_pnl:+.2f}")
    log(f"  Avg R:        {avg_r:.3f}")
    log(f"  Avg MFE:      {avg_mfe:.2f}R")
    log(f"  Avg MAE:      {avg_mae:.2f}R")
    log(f"  Start:        EUR {START_CAPITAL:.2f}")
    log(f"  Eind:         EUR {capital:.2f}")
    log(f"  Rendement:    {(capital - START_CAPITAL) / START_CAPITAL * 100:+.1f}%")
    log(f"  Max drawdown: EUR {max_dd:.2f}")
    log("")
    log("  Per exit reden:")
    for reden, stats in sorted(by_reden.items(), key=lambda x: x[1]["n"], reverse=True):
        rwr = round(stats["w"] / stats["n"] * 100, 1) if stats["n"] > 0 else 0
        log(f"    {reden:<20} {stats['n']:>4} trades  {rwr}% WR")
    log("=" * 60)

    if total == 0:
        log("WAARSCHUWING: 0 trades gesimuleerd ondanks signalen!")
        health_update("market_simulator", "ERROR", f"{len(signals)} signalen maar 0 trades")
        return

    # Opslaan in DB
    log("Opslaan in DB...")
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM experience_trades WHERE source='MARKETSIM'")
        log(f"  Oud verwijderd: {cur.rowcount}")

        saved = 0
        for t in closed:
            try:
                tk = f"MARKETSIM|{t['symbol']}|{t['signal_id']}"
                coin = t["symbol"].replace("USDT", "").replace("BUSD", "")
                market = f"{coin}-EUR"
                cur.execute("""
                    INSERT INTO experience_trades
                    (trade_key, source, symbol, coin, bitvavo_market, status, outcome,
                     entry, exit_price, stop, target, amount_eur, pnl_eur,
                     result_r, mfe_r, mae_r, setup_type, market_regime, score,
                     entry_time, exit_time, exit_reden, is_shadow,
                     created_at, updated_at)
                    VALUES (%s, 'MARKETSIM', %s, %s, %s, 'CLOSED', %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            to_timestamp(%s/1000.0), to_timestamp(%s/1000.0), %s, FALSE,
                            NOW(), NOW())
                    ON CONFLICT (trade_key) DO NOTHING
                """, (tk, t["symbol"], coin, market, t["outcome"],
                      t["entry"], t["exit_price"], t["stop"], t["target"],
                      POSITION_EUR, t["pnl_eur"], t["result_r"], t["mfe_r"], t["mae_r"],
                      t["setup_type"], t["regime"], t["score"],
                      t["open_ts"], t["close_ts"], t["exit_reden"]))
                saved += 1
            except Exception as e:
                if saved < 5:
                    log(f"  Insert fout: {e}")

        # Equity + results in bot_state
        eq_short = equity[::max(1, len(equity) // 200)]
        results = {"trades": total, "wins": wins, "losses": losses, "wr": wr,
                   "pnl": round(total_pnl, 2), "avg_r": round(avg_r, 3),
                   "start": START_CAPITAL, "end": round(capital, 2),
                   "drawdown": round(max_dd, 2), "by_reden": by_reden,
                   "run_date": datetime.now(timezone.utc).isoformat()}

        cur.execute("INSERT INTO bot_state (key,value) VALUES ('marketsim_results',%s) ON CONFLICT (key) DO UPDATE SET value=%s",
                    (json.dumps(results), json.dumps(results)))
        cur.execute("INSERT INTO bot_state (key,value) VALUES ('marketsim_equity',%s) ON CONFLICT (key) DO UPDATE SET value=%s",
                    (json.dumps(eq_short), json.dumps(eq_short)))

        conn.commit()
        log(f"  {saved} trades opgeslagen")
    except Exception as e:
        log(f"DB fout: {e}")
        traceback.print_exc()
        try: conn.rollback()
        except: pass

    health_update("market_simulator", "OK", f"{total} trades, {wr}% WR, PnL={total_pnl:+.2f}")
    log("Klaar!")


if __name__ == "__main__":
    run()
