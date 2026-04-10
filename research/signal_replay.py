#!/usr/bin/env python3
"""
Signal Replay Engine v1.0
=========================
Evalueert historische Pre-BUY signalen achteraf tegen candle data.

Doel: "Had ik dit signaal moeten nemen?"

Pakt EXPIRED/SKIPPED/FAILED signalen uit pending_approvals,
simuleert entry/stop/target tegen echte 1h candles,
en slaat het resultaat op als source='REPLAY' in experience_trades.

Draait als Render Cron Job (elke 6 uur).

Regels (consistent met trade_monitor):
- Entry: open prijs van eerste candle na signaal
- Stop: als low <= stop_loss → LOSS
- Target: als high >= target → WIN
- Max hold: 24 candles (24h) → dan sluit op close prijs
- Outcome altijd WIN of LOSS, nooit UNKNOWN
"""
from __future__ import annotations
import os
import sys
import time
import psycopg2
from datetime import datetime, timezone
from typing import Optional, Tuple

# Path fix voor Render
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from bot_health_helper import health_update
except ImportError:
    def health_update(*a, **k): pass

DATABASE_URL = os.environ.get("DATABASE_URL", "")
MAX_HOLD_CANDLES = int(os.environ.get("REPLAY_MAX_HOLD", "24"))  # 24h
BATCH_SIZE = int(os.environ.get("REPLAY_BATCH_SIZE", "100"))

def log(msg: str):
    print(f"[REPLAY {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def db_connect():
    for attempt in range(3):
        try:
            conn = psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=10)
            conn.autocommit = False
            return conn
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                raise


def get_replayable_signals(conn, limit: int = BATCH_SIZE) -> list:
    """Haal signalen op die nog niet gereplayd zijn."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pa.id, pa.symbol, pa.entry, pa.stop, pa.target,
                   pa.score, pa.setup_type, pa.market_regime, pa.regime,
                   pa.created_at, pa.kelly_grootte_eur, pa.coin,
                   pa.btc_regime, pa.rr_ratio, pa.timeframe, pa.status
            FROM pending_approvals pa
            WHERE pa.score >= 80
            AND pa.entry IS NOT NULL AND pa.entry > 0
            AND pa.stop IS NOT NULL AND pa.stop > 0
            AND pa.target IS NOT NULL AND pa.target > 0
            AND NOT EXISTS (
                SELECT 1 FROM experience_trades et
                WHERE et.trade_key = 'REPLAY|' || pa.symbol || '|' || pa.id
            )
            ORDER BY pa.created_at DESC
            LIMIT %s
        """, (limit,))
        return cur.fetchall()


def get_candles_after(conn, symbol: str, after: datetime, limit: int = 48) -> list:
    """Haal 1h candles op na een bepaald tijdstip."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT open_time, open, high, low, close, volume
            FROM candles
            WHERE symbol = %s AND timeframe = '1h'
            AND open_time >= %s
            ORDER BY open_time ASC
            LIMIT %s
        """, (symbol, after, limit))
        return cur.fetchall()


def replay_signal(entry: float, stop: float, target: float,
                  candles: list, max_hold: int = MAX_HOLD_CANDLES
                  ) -> Tuple[str, float, int, float, float]:
    """
    Simuleer een trade op basis van candle data.

    Returns: (outcome, exit_price, hold_candles, mfe_r, mae_r)

    Regels:
    - Candle high >= target → WIN op target prijs
    - Candle low <= stop → LOSS op stop prijs
    - Als beide in dezelfde candle: check of stop eerder geraakt
      (conservatief: als low <= stop, altijd LOSS)
    - Na max_hold candles: sluit op close, WIN als close > entry, LOSS anders
    """
    if not candles or entry <= 0 or stop <= 0 or target <= 0:
        return "LOSS", entry, 0, 0.0, 0.0

    risk = abs(entry - stop)
    if risk <= 0:
        return "LOSS", entry, 0, 0.0, 0.0

    mfe = 0.0  # max favorable excursion in R
    mae = 0.0  # max adverse excursion in R

    for i, candle in enumerate(candles[:max_hold]):
        _open_time, _open, high, low, close, _vol = candle

        # Update MFE/MAE
        if high > entry:
            cur_mfe = (high - entry) / risk
            mfe = max(mfe, cur_mfe)
        if low < entry:
            cur_mae = (entry - low) / risk
            mae = max(mae, cur_mae)

        # Check stop (conservatief: stop eerst als beide geraakt)
        if low <= stop:
            return "LOSS", stop, i + 1, round(mfe, 4), round(mae, 4)

        # Check target
        if high >= target:
            return "WIN", target, i + 1, round(mfe, 4), round(mae, 4)

    # Max hold bereikt: sluit op laatste close
    last_close = candles[min(max_hold - 1, len(candles) - 1)][4]
    outcome = "WIN" if last_close > entry else "LOSS"
    return outcome, last_close, min(max_hold, len(candles)), round(mfe, 4), round(mae, 4)


def save_replay_trade(conn, signal: tuple, outcome: str, exit_price: float,
                      hold_candles: int, mfe_r: float, mae_r: float):
    """Sla replay resultaat op in experience_trades."""
    pa_id = signal[0]
    symbol = signal[1]
    entry = float(signal[2])
    stop = float(signal[3])
    target = float(signal[4])
    score = signal[5]
    setup_type = signal[6] or ""
    regime = signal[7] or signal[8] or ""
    created_at = signal[9]
    kelly_eur = float(signal[10] or 5.0)
    coin = signal[11] or symbol.replace("USDT", "")
    btc_regime = signal[12] or ""
    rr_ratio = float(signal[13] or 0)
    timeframe = signal[14] or "1h"
    orig_status = signal[15] or ""

    trade_key = f"REPLAY|{symbol}|{pa_id}"
    risk = abs(entry - stop)
    result_r = round((exit_price - entry) / risk, 4) if risk > 0 else 0
    pnl_pct = round((exit_price - entry) / entry * 100, 4) if entry > 0 else 0
    pnl_eur = round(kelly_eur * pnl_pct / 100, 4)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO experience_trades (
                trade_key, source, symbol, coin, status, outcome,
                entry, exit_price, stop, target, amount_eur,
                pnl_eur, pnl_pct, result_r, mfe_r, mae_r,
                setup_type, market_regime, score,
                entry_time, exit_time,
                exit_reden, timeframe, is_shadow,
                created_at, updated_at
            ) VALUES (
                %s, 'REPLAY', %s, %s, 'CLOSED', %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s + INTERVAL '%s hours',
                %s, %s, FALSE,
                NOW(), NOW()
            )
            ON CONFLICT (trade_key) DO NOTHING
        """, (
            trade_key, symbol, coin, outcome,
            entry, exit_price, stop, target, kelly_eur,
            pnl_eur, pnl_pct, result_r, mfe_r, mae_r,
            setup_type, regime, score,
            created_at, created_at, hold_candles,
            f"REPLAY_{outcome}_{orig_status}", timeframe,
        ))
    conn.commit()


def run():
    log("=" * 60)
    log("Signal Replay Engine v1.0 gestart")
    log("=" * 60)

    if not DATABASE_URL:
        log("FOUT: DATABASE_URL niet ingesteld")
        return

    conn = db_connect()
    total = wins = losses = skipped = 0

    try:
        signals = get_replayable_signals(conn, BATCH_SIZE)
        log(f"Gevonden: {len(signals)} signalen om te replayen")

        for signal in signals:
            pa_id = signal[0]
            symbol = signal[1]
            entry = float(signal[2] or 0)
            stop = float(signal[3] or 0)
            target = float(signal[4] or 0)
            created_at = signal[9]

            if entry <= 0 or stop <= 0 or target <= 0:
                skipped += 1
                continue

            # Haal candles op na signaal tijdstip
            candles = get_candles_after(conn, symbol, created_at, MAX_HOLD_CANDLES + 5)

            if len(candles) < 3:
                skipped += 1
                continue

            # Replay
            outcome, exit_price, hold, mfe_r, mae_r = replay_signal(
                entry, stop, target, candles, MAX_HOLD_CANDLES
            )

            # Opslaan
            try:
                save_replay_trade(conn, signal, outcome, exit_price, hold, mfe_r, mae_r)
                total += 1
                if outcome == "WIN":
                    wins += 1
                else:
                    losses += 1

                if total <= 20:
                    risk = abs(entry - stop)
                    result_r = round((exit_price - entry) / risk, 2) if risk > 0 else 0
                    log(f"  {symbol:<12} {outcome:<5} R={result_r:+.2f} hold={hold}h "
                        f"MFE={mfe_r:.2f}R MAE={mae_r:.2f}R score={signal[5]}")
            except Exception as e:
                log(f"  {symbol} opslaan fout: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass

        wr = round(wins / total * 100, 1) if total > 0 else 0
        log(f"\n{'=' * 60}")
        log(f"Resultaat: {total} trades | {wins}W {losses}L | {wr}% win rate")
        log(f"Overgeslagen: {skipped} (geen candle data)")
        log(f"{'=' * 60}")

        # Heartbeat
        try:
            health_update("signal_replay", "OK",
                          f"{total} trades, {wins}W/{losses}L, {wr}% WR")
        except Exception:
            pass

    except Exception as e:
        log(f"FOUT: {e}")
        import traceback
        traceback.print_exc()
        try:
            health_update("signal_replay", "ERROR", str(e)[:200])
        except Exception:
            pass
    finally:
        conn.close()


if __name__ == "__main__":
    run()
