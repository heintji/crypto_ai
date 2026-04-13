#!/usr/bin/env python3
"""
Market Simulator v1.0 — Complete Bot Simulatie op Historische Data
=================================================================
Simuleert de HELE bot (scanner + monitor + sell) op historische candle data.
Exact dezelfde regels als live — TREND_PULLBACK, score, stop/target/trailing.

Features:
- ATR-based stop/target
- Regime filter (BEAR skip)
- Spread + fee + slippage simulatie
- Cooldown per coin
- Max open trades
- Equity tracking + drawdown
- Per-trade logging naar DB (source='MARKETSIM')

Draait als Render Cron of handmatig.
"""
from __future__ import annotations
import os, sys, time, json, math
import psycopg2
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple, Optional, Any

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from bot_health_helper import health_update
except ImportError:
    def health_update(*a, **k): pass

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# === CONFIGURATIE (exact zoals live bot) ===
SCORE_DREMPEL = int(os.environ.get("SIM_SCORE_DREMPEL", "85"))
MAX_OPEN_TRADES = int(os.environ.get("SIM_MAX_OPEN", "10"))
POSITION_EUR = float(os.environ.get("SIM_POSITION_EUR", "15.0"))
MAX_HOLD_HOURS = float(os.environ.get("SIM_MAX_HOLD_HOURS", "36"))
TRAILING_STOP_PCT = float(os.environ.get("SIM_TRAILING_PCT", "0.03"))
BITVAVO_FEE_PCT = 0.0025
SLIPPAGE_PCT = 0.001
SPREAD_PCT = 0.0015
COOLDOWN_HOURS = 24
ATR_PERIOD = 14
ATR_MULTIPLIER = float(os.environ.get("SIM_ATR_MULT", "1.6"))
START_CAPITAL = float(os.environ.get("SIM_START_CAPITAL", "500.0"))

# Simulatie periode
SIM_START = os.environ.get("SIM_START", "2025-01-01")
SIM_END = os.environ.get("SIM_END", "")  # leeg = tot nu

def log(msg: str):
    print(f"[MARKETSIM {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)

def db_connect():
    return psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=10)


# ============================================================
# INDICATOR BEREKENINGEN
# ============================================================

def calc_atr(candles: List[Dict], period: int = ATR_PERIOD) -> float:
    """Average True Range."""
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs[-period:]) / period if trs else 0

def calc_rsi(candles: List[Dict], period: int = 14) -> float:
    """Relative Strength Index."""
    if len(candles) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(candles)):
        diff = candles[i]["close"] - candles[i-1]["close"]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_sma(candles: List[Dict], period: int) -> float:
    """Simple Moving Average."""
    if len(candles) < period:
        return 0
    return sum(c["close"] for c in candles[-period:]) / period

def calc_ema(candles: List[Dict], period: int) -> float:
    """Exponential Moving Average."""
    if len(candles) < period:
        return candles[-1]["close"] if candles else 0
    mult = 2 / (period + 1)
    ema = candles[0]["close"]
    for c in candles[1:]:
        ema = (c["close"] - ema) * mult + ema
    return ema

def calc_bollinger_position(candles: List[Dict], period: int = 20) -> float:
    """Waar zit de prijs t.o.v. Bollinger Bands? 0=lower, 1=upper."""
    if len(candles) < period:
        return 0.5
    closes = [c["close"] for c in candles[-period:]]
    sma = sum(closes) / period
    std = (sum((c - sma) ** 2 for c in closes) / period) ** 0.5
    if std == 0:
        return 0.5
    upper = sma + 2 * std
    lower = sma - 2 * std
    return (candles[-1]["close"] - lower) / (upper - lower) if upper != lower else 0.5


# ============================================================
# SIGNAAL DETECTIE (TREND_PULLBACK)
# ============================================================

def detect_trend_pullback(candles_1h: List[Dict], candles_4h: List[Dict],
                          btc_regime: str) -> Optional[Dict]:
    """
    Detecteert TREND_PULLBACK signaal.
    Simpele versie van multi_coin_score logica.
    """
    if len(candles_1h) < 55 or len(candles_4h) < 20:
        return None

    close = candles_1h[-1]["close"]
    high = candles_1h[-1]["high"]
    low = candles_1h[-1]["low"]

    # Trend check: EMA9 > EMA21 > EMA55 op 4h
    ema9 = calc_ema(candles_4h, 9)
    ema21 = calc_ema(candles_4h, 21)
    ema55 = calc_ema(candles_4h, 55)
    uptrend = ema9 > ema21 > ema55

    if not uptrend:
        return None

    # Pullback check: RSI op 1h tussen 35-55 (oversold maar niet crashed)
    rsi = calc_rsi(candles_1h)
    if not (30 <= rsi <= 55):
        return None

    # Prijs bij SMA20 of lager (pullback)
    sma20 = calc_sma(candles_1h, 20)
    if close > sma20 * 1.02:  # meer dan 2% boven SMA = geen pullback
        return None

    # Bollinger positie
    bb_pos = calc_bollinger_position(candles_1h)
    if bb_pos > 0.6:  # te hoog in de band
        return None

    # ATR voor stop/target
    atr = calc_atr(candles_1h)
    if atr <= 0:
        return None

    stop = close - atr * ATR_MULTIPLIER
    target = close + atr * ATR_MULTIPLIER * 2  # 2:1 R:R

    # Score berekenen (simpele versie)
    score = 70
    if uptrend: score += 10
    if 35 <= rsi <= 50: score += 5
    if bb_pos < 0.3: score += 5
    if btc_regime == "BULL": score += 5
    if btc_regime == "RANGE": score += 3

    if score < SCORE_DREMPEL:
        return None

    return {
        "entry": close,
        "stop": stop,
        "target": target,
        "score": score,
        "atr": atr,
        "rsi": rsi,
        "setup_type": "TREND_PULLBACK",
    }


# ============================================================
# BTC REGIME DETECTIE
# ============================================================

def detect_btc_regime(btc_candles_4h: List[Dict]) -> str:
    """Detecteert BTC regime op basis van EMA200."""
    if len(btc_candles_4h) < 200:
        return "RANGE"
    ema200 = calc_ema(btc_candles_4h, 200)
    close = btc_candles_4h[-1]["close"]
    pct = (close - ema200) / ema200 * 100
    if pct > 5:
        return "BULL"
    elif pct < -5:
        return "BEAR"
    return "RANGE"


# ============================================================
# TRADE MANAGEMENT
# ============================================================

class SimTrade:
    def __init__(self, symbol, entry, stop, target, score, atr, timestamp, amount_eur):
        self.symbol = symbol
        self.entry = entry
        self.stop = stop
        self.original_stop = stop
        self.target = target
        self.score = score
        self.atr = atr
        self.open_time = timestamp
        self.amount_eur = amount_eur
        self.qty = amount_eur / entry if entry > 0 else 0
        self.max_price = entry
        self.min_price = entry
        self.had_1r = False
        self.had_2r = False
        self.partial_sold = False
        self.mode = "NORMAL"

    def update(self, candle: Dict) -> Optional[Dict]:
        """Update trade met nieuwe candle. Return result als trade sluit."""
        high = candle["high"]
        low = candle["low"]
        close = candle["close"]
        ts = candle["ts_ms"]

        # Update MFE/MAE
        self.max_price = max(self.max_price, high)
        self.min_price = min(self.min_price, low)

        risk = abs(self.entry - self.original_stop)
        if risk <= 0:
            risk = self.entry * 0.02

        r = (close - self.entry) / risk
        hold_hours = (ts - self.open_time) / (3600 * 1000)

        # 1. Stop loss
        if low <= self.stop:
            return self._close(self.stop, ts, "STOP_LOSS", risk)

        # 2. Max hold
        if hold_hours >= MAX_HOLD_HOURS:
            return self._close(close, ts, "MAX_HOLD", risk)

        # 3. Target bereikt -> STRUCTUUR mode
        if high >= self.target and self.mode == "NORMAL":
            self.mode = "STRUCTUUR"
            self.max_price = high

        # 4. STRUCTUUR break
        if self.mode == "STRUCTUUR":
            if close < self.max_price * 0.99:
                return self._close(close, ts, "STRUCTUUR_BREAK", risk)

        # 5. 1R bereikt -> stop naar break-even
        if r >= 1.0 and not self.had_1r:
            self.had_1r = True
            self.stop = self.entry

        # 6. 2R bereikt -> stop naar +1R
        if r >= 2.0 and not self.had_2r:
            self.had_2r = True
            self.stop = self.entry + risk

        # 7. Trailing stop
        if close > self.entry:
            trailing = close * (1 - TRAILING_STOP_PCT)
            if trailing > self.stop:
                self.stop = trailing

        return None

    def _close(self, exit_price, ts, reden, risk):
        """Sluit trade en bereken resultaten."""
        # Fee + slippage
        entry_cost = self.amount_eur * (BITVAVO_FEE_PCT + SLIPPAGE_PCT + SPREAD_PCT)
        exit_cost = exit_price * self.qty * (BITVAVO_FEE_PCT + SLIPPAGE_PCT)
        gross_pnl = (exit_price - self.entry) * self.qty
        net_pnl = gross_pnl - entry_cost - exit_cost

        mfe_r = (self.max_price - self.entry) / risk if risk > 0 else 0
        mae_r = (self.entry - self.min_price) / risk if risk > 0 else 0
        result_r = (exit_price - self.entry) / risk if risk > 0 else 0
        hold_hours = (ts - self.open_time) / (3600 * 1000)

        outcome = "WIN" if net_pnl > 0 else "LOSS"

        return {
            "symbol": self.symbol,
            "entry": self.entry,
            "exit_price": exit_price,
            "stop": self.original_stop,
            "target": self.target,
            "score": self.score,
            "outcome": outcome,
            "pnl_eur": round(net_pnl, 4),
            "result_r": round(result_r, 3),
            "mfe_r": round(mfe_r, 3),
            "mae_r": round(mae_r, 3),
            "hold_hours": round(hold_hours, 1),
            "exit_reden": reden,
            "amount_eur": self.amount_eur,
            "open_time": self.open_time,
            "close_time": ts,
        }


# ============================================================
# HOOFD SIMULATIE
# ============================================================

def load_candles_for_symbol(conn, symbol: str, timeframe: str,
                            start: str, end: str) -> List[Dict]:
    """Laad candles uit DB voor een symbol."""
    cur = conn.cursor()
    end_clause = f"AND open_time <= '{end}'" if end else ""
    cur.execute(f"""
        SELECT open, high, low, close, volume,
               EXTRACT(EPOCH FROM open_time) * 1000 AS ts_ms
        FROM candles
        WHERE symbol = %s AND timeframe = %s
        AND open_time >= %s {end_clause}
        ORDER BY open_time ASC
    """, (symbol, timeframe, start))
    return [{"open": float(r[0]), "high": float(r[1]), "low": float(r[2]),
             "close": float(r[3]), "volume": float(r[4]), "ts_ms": int(r[5])}
            for r in cur.fetchall()]


def get_all_symbols(conn, min_candles: int = 100) -> List[str]:
    """Haal alle symbols met genoeg candle data."""
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol FROM candles
        WHERE timeframe = '1h' AND exchange IN ('binance','bitvavo')
        GROUP BY symbol HAVING COUNT(*) >= %s
        ORDER BY symbol
    """, (min_candles,))
    return [r[0] for r in cur.fetchall()]


def run():
    log("=" * 60)
    log("Market Simulator v1.0")
    log(f"Periode: {SIM_START} tot {SIM_END or 'nu'}")
    log(f"Score: {SCORE_DREMPEL} | Hold: {MAX_HOLD_HOURS}h | EUR: {POSITION_EUR}")
    log("=" * 60)

    if not DATABASE_URL:
        log("FOUT: DATABASE_URL niet ingesteld")
        return

    conn = db_connect()

    # Haal BTC 4h candles voor regime detectie
    log("BTC candles laden...")
    btc_4h = load_candles_for_symbol(conn, "BTCUSDT", "4h", SIM_START, SIM_END)
    log(f"  BTC 4h: {len(btc_4h)} candles")

    if len(btc_4h) < 200:
        log("FOUT: niet genoeg BTC candles")
        conn.close()
        return

    # Haal alle symbols
    symbols = get_all_symbols(conn)
    log(f"Symbols: {len(symbols)}")

    # Blacklist
    cur = conn.cursor()
    cur.execute("SELECT value FROM bot_state WHERE key='coin_blacklist'")
    r = cur.fetchone()
    blacklist = set(json.loads(r[0])) if r and r[0] else set()
    log(f"Blacklist: {len(blacklist)} coins")

    # Simulatie state
    capital = START_CAPITAL
    equity_curve = []
    open_trades: List[SimTrade] = []
    closed_trades: List[Dict] = []
    cooldown: Dict[str, int] = {}  # symbol -> timestamp wanneer cooldown eindigt
    total_signals = 0

    # Loop door tijd (per 1h candle)
    log("Simulatie starten...")
    btc_idx = 200  # start na 200 candles voor EMA200

    # Pre-load alle 1h candles per symbol
    log("Candles laden per symbol...")
    all_candles_1h = {}
    all_candles_4h = {}
    for sym in symbols:
        if sym in blacklist:
            continue
        c1h = load_candles_for_symbol(conn, sym, "1h", SIM_START, SIM_END)
        if len(c1h) >= 55:
            all_candles_1h[sym] = c1h
            c4h = load_candles_for_symbol(conn, sym, "4h", SIM_START, SIM_END)
            all_candles_4h[sym] = c4h
    log(f"  {len(all_candles_1h)} symbols met data geladen")

    # Maak een tijdlijn van alle BTC 4h candles
    for btc_i in range(btc_idx, len(btc_4h)):
        btc_time = btc_4h[btc_i]["ts_ms"]
        btc_regime = detect_btc_regime(btc_4h[:btc_i+1])

        # BEAR skip
        if btc_regime == "BEAR":
            continue

        # Update open trades met huidige candle
        trades_to_remove = []
        for trade in open_trades:
            sym = trade.symbol
            # Vind de juiste candle voor deze timestamp
            candles = all_candles_1h.get(sym, [])
            # Zoek candles rond dit tijdstip
            for c in candles:
                if c["ts_ms"] > trade.open_time and c["ts_ms"] <= btc_time:
                    result = trade.update(c)
                    if result:
                        closed_trades.append(result)
                        capital += result["pnl_eur"]
                        trades_to_remove.append(trade)
                        if result["outcome"] == "LOSS":
                            cooldown[sym] = btc_time + COOLDOWN_HOURS * 3600 * 1000
                        break

        for t in trades_to_remove:
            if t in open_trades:
                open_trades.remove(t)

        # Zoek nieuwe signalen
        if len(open_trades) >= MAX_OPEN_TRADES:
            continue

        for sym in all_candles_1h:
            if sym in blacklist:
                continue
            if sym in cooldown and btc_time < cooldown[sym]:
                continue
            if any(t.symbol == sym for t in open_trades):
                continue
            if len(open_trades) >= MAX_OPEN_TRADES:
                break

            candles_1h = [c for c in all_candles_1h[sym] if c["ts_ms"] <= btc_time]
            candles_4h = [c for c in all_candles_4h.get(sym, []) if c["ts_ms"] <= btc_time]

            if len(candles_1h) < 55:
                continue

            signal = detect_trend_pullback(candles_1h[-55:], candles_4h[-20:], btc_regime)
            if signal:
                total_signals += 1
                # Simuleer entry met spread/slippage
                entry = signal["entry"] * (1 + SPREAD_PCT + SLIPPAGE_PCT)
                trade = SimTrade(
                    symbol=sym,
                    entry=entry,
                    stop=signal["stop"],
                    target=signal["target"],
                    score=signal["score"],
                    atr=signal["atr"],
                    timestamp=btc_time,
                    amount_eur=POSITION_EUR,
                )
                open_trades.append(trade)

        # Equity tracking
        equity_curve.append({
            "ts": btc_time,
            "capital": round(capital, 2),
            "open_trades": len(open_trades),
            "regime": btc_regime,
        })

        # Progress logging
        if btc_i % 500 == 0:
            wins = len([t for t in closed_trades if t["outcome"] == "WIN"])
            losses = len([t for t in closed_trades if t["outcome"] == "LOSS"])
            wr = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0
            log(f"  [{btc_i}/{len(btc_4h)}] {len(closed_trades)} trades, {wr}% WR, capital={capital:.2f}, regime={btc_regime}")

    # Sluit resterende open trades
    for trade in open_trades:
        sym = trade.symbol
        candles = all_candles_1h.get(sym, [])
        if candles:
            last = candles[-1]
            risk = abs(trade.entry - trade.original_stop) or trade.entry * 0.02
            result = trade._close(last["close"], last["ts_ms"], "SIM_END", risk)
            closed_trades.append(result)
            capital += result["pnl_eur"]

    # ============================================================
    # RESULTATEN
    # ============================================================
    wins = len([t for t in closed_trades if t["outcome"] == "WIN"])
    losses = len([t for t in closed_trades if t["outcome"] == "LOSS"])
    total = wins + losses
    wr = round(wins / total * 100, 1) if total > 0 else 0
    total_pnl = sum(t["pnl_eur"] for t in closed_trades)
    avg_r = sum(t["result_r"] for t in closed_trades) / total if total > 0 else 0
    avg_mfe = sum(t["mfe_r"] for t in closed_trades) / total if total > 0 else 0
    avg_mae = sum(t["mae_r"] for t in closed_trades) / total if total > 0 else 0

    # Drawdown
    peak = START_CAPITAL
    max_dd = 0
    for eq in equity_curve:
        if eq["capital"] > peak:
            peak = eq["capital"]
        dd = peak - eq["capital"]
        if dd > max_dd:
            max_dd = dd

    # Sharpe ratio (vereenvoudigd)
    if closed_trades:
        returns = [t["pnl_eur"] for t in closed_trades]
        avg_ret = sum(returns) / len(returns)
        std_ret = (sum((r - avg_ret) ** 2 for r in returns) / len(returns)) ** 0.5
        sharpe = avg_ret / std_ret if std_ret > 0 else 0
    else:
        sharpe = 0

    log("")
    log("=" * 60)
    log("RESULTATEN")
    log("=" * 60)
    log(f"  Periode:      {SIM_START} - {SIM_END or 'nu'}")
    log(f"  Signalen:     {total_signals}")
    log(f"  Trades:       {total} ({wins}W/{losses}L)")
    log(f"  Win rate:     {wr}%")
    log(f"  Totaal PnL:   EUR {total_pnl:+.2f}")
    log(f"  Avg R:        {avg_r:.3f}")
    log(f"  Avg MFE:      {avg_mfe:.2f}R")
    log(f"  Avg MAE:      {avg_mae:.2f}R")
    log(f"  Start:        EUR {START_CAPITAL:.2f}")
    log(f"  Eind:         EUR {capital:.2f}")
    log(f"  Rendement:    {(capital - START_CAPITAL) / START_CAPITAL * 100:+.1f}%")
    log(f"  Max drawdown: EUR {max_dd:.2f}")
    log(f"  Sharpe ratio: {sharpe:.3f}")
    log("=" * 60)

    # Opslaan in DB
    log("Resultaten opslaan in DB...")
    try:
        cur = conn.cursor()

        # Verwijder oude MARKETSIM trades
        cur.execute("DELETE FROM experience_trades WHERE source='MARKETSIM'")
        log(f"  Oude trades verwijderd: {cur.rowcount}")

        # Sla nieuwe trades op
        saved = 0
        for t in closed_trades:
            try:
                tk = f"MARKETSIM|{t['symbol']}|{t['open_time']}"
                coin = t['symbol'].replace('USDT', '').replace('BUSD', '')
                market = f"{coin}-EUR"
                cur.execute("""
                    INSERT INTO experience_trades
                    (trade_key, source, symbol, coin, bitvavo_market, status, outcome,
                     entry, exit_price, stop, target, amount_eur, pnl_eur,
                     result_r, mfe_r, mae_r, setup_type, score,
                     entry_time, exit_time, exit_reden, is_shadow,
                     created_at, updated_at)
                    VALUES (%s, 'MARKETSIM', %s, %s, %s, 'CLOSED', %s,
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, 'TREND_PULLBACK', %s,
                            to_timestamp(%s/1000.0), to_timestamp(%s/1000.0), %s, FALSE,
                            NOW(), NOW())
                    ON CONFLICT (trade_key) DO NOTHING
                """, (tk, t["symbol"], coin, market, t["outcome"],
                      t["entry"], t["exit_price"], t["stop"], t["target"],
                      t["amount_eur"], t["pnl_eur"],
                      t["result_r"], t["mfe_r"], t["mae_r"], t["score"],
                      t["open_time"], t["close_time"], t["exit_reden"]))
                saved += 1
            except Exception as e:
                log(f"  Save fout {t['symbol']}: {e}")

        # Sla equity curve op als JSON in bot_state
        eq_summary = equity_curve[::max(1, len(equity_curve) // 200)]  # max 200 punten
        cur.execute("""
            INSERT INTO bot_state (key, value) VALUES ('marketsim_equity', %s)
            ON CONFLICT (key) DO UPDATE SET value = %s
        """, (json.dumps(eq_summary), json.dumps(eq_summary)))

        # Sla resultaten samenvatting op
        results = {
            "trades": total, "wins": wins, "losses": losses, "wr": wr,
            "pnl": round(total_pnl, 2), "avg_r": round(avg_r, 3),
            "avg_mfe": round(avg_mfe, 2), "avg_mae": round(avg_mae, 2),
            "start_capital": START_CAPITAL, "end_capital": round(capital, 2),
            "max_drawdown": round(max_dd, 2), "sharpe": round(sharpe, 3),
            "period": f"{SIM_START} - {SIM_END or 'now'}",
            "run_date": datetime.now(timezone.utc).isoformat(),
        }
        cur.execute("""
            INSERT INTO bot_state (key, value) VALUES ('marketsim_results', %s)
            ON CONFLICT (key) DO UPDATE SET value = %s
        """, (json.dumps(results), json.dumps(results)))

        conn.commit()
        log(f"  {saved} trades opgeslagen")
    except Exception as e:
        log(f"DB fout: {e}")
        conn.rollback()

    health_update("market_simulator", "OK",
                  f"{total} trades, {wr}% WR, PnL={total_pnl:+.2f}")

    conn.close()
    log("Klaar!")


if __name__ == "__main__":
    run()
