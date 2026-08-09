#!/usr/bin/env python3
"""PLAN G v2 — schaduw-consumer (de GROENE kant van de schaduw-motor).

WAAROM DIT BESTAND BESTAAT (root cause 26-6):
  Shadow-trades voor Plan G v2 werden NIET door de scanner geschreven, maar
  door live_trader.py's main_loop (het "shadow-only block"), dat pending_
  approvals consumeert en via shadow_buy() naar experience_trades schrijft.
  live_trader draait als achtergrond-worker op Render en staat STIL
  (gesuspendeerd) — daardoor verlopen alle pendings (status EXPIRED) en is
  experience_trades stil sinds 2026-06-26. Daarbovenop lag de scanner zelf
  stil van 26-6 t/m 7-8 (import-bug, gefixt in commit b1a0306).

  Dit script herstelt ALLEEN de plumbing: het voert exact hetzelfde
  shadow-only block uit als live_trader.main_loop (zelfde bot_state-gates,
  zelfde sizing, zelfde INSERT), plus de shadow-exits van trade_monitor
  (stop / target / 48u max-hold) — maar dan als geïsoleerde one-pass die
  op de bestaande 15-min scanner-cron meelift (zelfde patroon als
  mr_shadow). Strategie-logica is NIET veranderd.

VEILIGHEID:
  - Er wordt NOOIT echt gekocht of verkocht: geen enkele order-call.
    Bitvavo wordt alleen (read-only) gebruikt voor saldo (sizing) en
    publieke koersen. Alles is schaduw-administratie in de database.
  - Dubbel-werk-guards: het open-gedeelte slaat over als live_trader
    aantoonbaar zelf draait; het sluit-gedeelte slaat over als
    trade_monitor een verse heartbeat heeft.
  - Eigen try/except overal — kan de scanner-cron nooit breken.
  - Plan U (plan_u_shadow.py / plan_u_trades) wordt niet aangeraakt.

GEBRUIK:
  python research/plan_g_shadow.py                       # normale one-pass
  python research/plan_g_shadow.py --dry-run             # niets committen (alles rollback)
  python research/plan_g_shadow.py --forceer-tijdgates   # negeer uur/lunch/weekend-gates (alleen testen!)

Env: DATABASE_URL (verplicht), BITVAVO_API_KEY/SECRET (optioneel, alleen
     voor BALANCE_PCT-sizing; ontbreekt -> FLAT fallback, identiek aan
     live_trader-gedrag bij een balance-fetch-fout).
"""
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psycopg2
import requests

DATABASE_URL       = (os.getenv("DATABASE_URL") or "").strip()
BITVAVO_API_KEY    = (os.getenv("BITVAVO_API_KEY") or "").strip()
BITVAVO_API_SECRET = (os.getenv("BITVAVO_API_SECRET") or "").strip()

# Zelfde kosten-aannames als trade_monitor/_log_shadow_outcome
BITVAVO_FEE_PCT = float(os.getenv("BITVAVO_FEE_PCT") or "0.0025")
SLIPPAGE_PCT    = float(os.getenv("SLIPPAGE_PCT")    or "0.001")

# Zelfde houdtijd als trade_monitor: shadow = MAX_HOLD_HOURS * 2 (48u default)
MAX_HOLD_HOURS = int(os.getenv("MAX_HOLD_HOURS") or "24")

# Alleen verse pendings consumeren. live_trader (30s-loop) pakte signalen
# vrijwel direct na aanmaak op; deze one-pass draait elke 15 min. Oudere
# pendings openen op een uren-oude entry-koers = lookahead-bias. 20 min
# dekt de 15-min cron-cadans af zonder stale entries.
MAX_PENDING_AGE_MIN = int(os.getenv("PLAN_G_MAX_PENDING_AGE_MIN") or "20")

BINANCE_BASE = "https://api.binance.com/api/v3"
BITVAVO_BASE = "https://api.bitvavo.com"


def log(msg: str) -> None:
    print(f"[plan-g-shadow] {msg}", flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def safe_float(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def safe_int(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def safe_str(v, default="") -> str:
    return str(v) if v is not None else default


def db_connect():
    return psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=15)


def get_bot_state(conn, key: str, default: str = "") -> str:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM bot_state WHERE key=%s", (key,))
            row = cur.fetchone()
            return row[0] if row and row[0] is not None else default
    except Exception:
        conn.rollback()
        return default


def _heartbeat(conn, status: str, details: str) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE bot_health SET status=%s, laatste_run=NOW(), details=%s WHERE service=%s",
                (status, details[:200], "plan_g_shadow"))
            if cur.rowcount == 0:
                cur.execute(
                    "INSERT INTO bot_health (service,status,laatste_run,details) VALUES (%s,%s,NOW(),%s)",
                    ("plan_g_shadow", status, details[:200]))
        conn.commit()
    except Exception as e:
        log(f"heartbeat fout: {e}")


# ============================================================
# Bitvavo (READ-ONLY): saldo voor BALANCE_PCT-sizing
# ============================================================
def get_eur_balance() -> Optional[float]:
    """Read-only /v2/balance. Retourneert None bij elke fout -> FLAT fallback."""
    if not BITVAVO_API_KEY or not BITVAVO_API_SECRET:
        return None
    try:
        ts  = str(int(time.time() * 1000))
        sig = hmac.new(BITVAVO_API_SECRET.encode(),
                       (ts + "GET" + "/v2/balance").encode(),
                       hashlib.sha256).hexdigest()
        r = requests.get(BITVAVO_BASE + "/v2/balance", timeout=10, headers={
            "Bitvavo-Access-Key": BITVAVO_API_KEY,
            "Bitvavo-Access-Signature": sig,
            "Bitvavo-Access-Timestamp": ts,
            "Bitvavo-Access-Window": "10000",
        })
        r.raise_for_status()
        for item in r.json():
            if safe_str(item.get("symbol")) == "EUR":
                return safe_float(item.get("available", 0))
    except Exception as e:
        log(f"balance-fetch fout: {e} -> FLAT fallback")
    return None


def get_binance_price(symbol_usdt: str) -> Optional[float]:
    """Publieke Binance-koers (zelfde bron als de scanner-entries)."""
    try:
        r = requests.get(BINANCE_BASE + "/ticker/price",
                         params={"symbol": symbol_usdt}, timeout=10)
        r.raise_for_status()
        return safe_float(r.json().get("price"))
    except Exception:
        return None


# ============================================================
# OPEN-PASS — identiek aan live_trader.main_loop shadow-only block
# ============================================================
def open_pass(conn, forceer_tijdgates: bool = False) -> int:
    """Consumeert PENDING pending_approvals -> shadow-rows in experience_trades.

    Alle drempels via bot_state (zelfde keys + defaults als live_trader),
    zodat tunen geen redeploy vergt en het gedrag identiek blijft.
    """
    # Guard: als live_trader zelf draait (heartbeat < 3 min oud) niets doen
    try:
        _lt = get_bot_state(conn, "live_trader_last_ts", "")
        if _lt:
            _age = (now_utc() - datetime.fromisoformat(_lt)).total_seconds()
            if _age < 180:
                log(f"open-pass overgeslagen — live_trader actief ({_age:.0f}s geleden)")
                return 0
    except Exception:
        pass

    def _bs_bool(k, default="true"):
        return safe_str(get_bot_state(conn, k, default)).lower() in ("true", "1", "yes", "ja")

    def _bs_int(k, default):
        return safe_int(get_bot_state(conn, k, str(default)), default)

    def _bs_float(k, default):
        return safe_float(get_bot_state(conn, k, str(default)), default)

    _shadow_drempel    = _bs_int("score_drempel_shadow", 80)
    _shadow_score_max  = _bs_int("shadow_score_max", 100)
    _shadow_mult       = _bs_float("shadow_stop_target_mult", 1.5)
    _shadow_min_rr     = _bs_float("shadow_min_rr", 0.0)
    _shadow_min_vol    = _bs_float("shadow_min_volume_24h_eur", 0.0)
    _btc_regime_now    = safe_str(get_bot_state(conn, "btc_regime_huidig", "UNKNOWN")).upper()
    _skip_non_trending = _bs_bool("shadow_skip_non_trending", "true")
    _skip_weekend      = _bs_bool("shadow_skip_weekend", "true")
    _skip_lunch        = _bs_bool("shadow_skip_lunch", "true")
    _hour_min          = _bs_int("shadow_active_hours_min", 0)
    _hour_max          = _bs_int("shadow_active_hours_max", 24)
    _blocked_csv       = safe_str(get_bot_state(conn, "shadow_blocked_coins", ""))
    _blocked_set = {s.strip().upper() for s in _blocked_csv.split(",") if s.strip()}

    _now = now_utc()
    # Gate 1: BTC moet trending (BULL) zijn — tenzij uitgeschakeld via bot_state
    _gate1_open = (not _skip_non_trending) or _btc_regime_now == "BULL"
    # Gate dow: alleen Za/Zo blocken (zelfde correctie als live_trader)
    _dow = _now.isoweekday()
    _gate_dow = (not _skip_weekend) or _dow not in (6, 7)
    # Gate hour: active hours + lunch-skip
    _hour = _now.hour
    _gate_hour = (_hour_min <= _hour < _hour_max)
    if _skip_lunch and _hour in (12, 13, 22):
        _gate_hour = False

    if forceer_tijdgates:
        _gate_dow = _gate_hour = True

    if not _gate1_open:
        log(f"shadow dicht — BTC regime={_btc_regime_now} (gate1)")
        return 0
    if not _gate_dow:
        log(f"shadow dicht — weekend dow={_dow}")
        return 0
    if not _gate_hour:
        log(f"shadow dicht — uur {_hour} buiten {_hour_min}-{_hour_max}/lunch")
        return 0

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, symbol, bitvavo_market, score, entry, stop, target,
                   kelly_grootte_eur,
                   setup_type,
                   COALESCE(NULLIF(btc_regime_4h,''), NULLIF(market_regime,''),
                            NULLIF(regime,''), %s) AS regime_real
            FROM pending_approvals
            WHERE status = 'PENDING'
              AND score >= %s
              AND score <= %s
              AND expires_at > NOW()
              AND created_at > NOW() - make_interval(mins => %s)
            ORDER BY score DESC
            LIMIT 10
        """, (_btc_regime_now, _shadow_drempel, _shadow_score_max,
              MAX_PENDING_AGE_MIN))
        shadow_rows = cur.fetchall()

    if not shadow_rows:
        log("geen PENDING signalen binnen scoreband")
        return 0

    # Sizing-config 1x per pass (zelfde 3 modi als live_trader)
    _sizing_mode = safe_str(get_bot_state(conn, "shadow_sizing_mode", "KELLY")).upper()
    _sizing_balance_eur = None
    if _sizing_mode == "BALANCE_PCT":
        _sizing_balance_eur = get_eur_balance()
        if _sizing_balance_eur is None:
            _sizing_mode = "FLAT"

    _opened = 0
    for srow in shadow_rows:
        try:
            # Savepoint: een fout in 1 rij mag eerdere rijen niet terugdraaien
            with conn.cursor() as _sp:
                _sp.execute("SAVEPOINT sp_open_row")
            (sid, ssymbol, smarket, sscore, sentry, sstop, starget,
             skelly, ssetup, sregime) = srow
            sentry_f  = safe_float(sentry)
            sstop_f   = safe_float(sstop)
            starget_f = safe_float(starget)
            ssym_up   = (ssymbol or "").upper()
            _coin     = ssym_up.replace("USDT", "").replace("BUSD", "")

            with conn.cursor() as cur:
                # Gate 4: coin-blocklist
                if ssym_up in _blocked_set:
                    log(f"skip — {ssym_up} op blocklist")
                    cur.execute("UPDATE pending_approvals SET status='SHADOW_BLOCKED' WHERE id=%s", (sid,))
                    continue

                # Gate 5: min-RR
                if _shadow_min_rr > 0 and sentry_f > 0 and 0 < sstop_f < sentry_f:
                    _rr = (starget_f - sentry_f) / max(sentry_f - sstop_f, 1e-9)
                    if _rr < _shadow_min_rr:
                        log(f"skip — {ssym_up} RR={_rr:.2f} < {_shadow_min_rr}")
                        cur.execute("UPDATE pending_approvals SET status='SHADOW_LOW_RR' WHERE id=%s", (sid,))
                        continue

                # Gate 6: min-volume (payload optioneel; ontbreekt -> niet blokkeren)
                if _shadow_min_vol > 0:
                    try:
                        cur.execute("SELECT COALESCE((payload->>'volume_24h_eur')::float, 0) FROM pending_approvals WHERE id=%s", (sid,))
                        _v = cur.fetchone()
                        _vol = safe_float(_v[0]) if _v else 0.0
                        if 0 < _vol < _shadow_min_vol:
                            log(f"skip — {ssym_up} vol={_vol:.0f} < {_shadow_min_vol:.0f}")
                            cur.execute("UPDATE pending_approvals SET status='SHADOW_LOW_VOL' WHERE id=%s", (sid,))
                            continue
                    except Exception:
                        cur.execute("ROLLBACK TO SAVEPOINT sp_open_row")

                # Dubbel-check: coin heeft al een OPEN trade
                cur.execute("SELECT COUNT(*) FROM experience_trades WHERE coin=%s AND status='OPEN'", (_coin,))
                if cur.fetchone()[0] > 0:
                    log(f"skip — {_coin} heeft al een OPEN trade")
                    continue

                # Stop/target-multiplier (verbreed tegen ruis-stops)
                if (_shadow_mult and _shadow_mult != 1.0
                        and sentry_f > 0 and 0 < sstop_f < sentry_f):
                    _risk = sentry_f - sstop_f
                    sstop_adj = round(sentry_f - _risk * _shadow_mult, 8)
                    if starget_f > sentry_f:
                        starget_adj = round(sentry_f + (starget_f - sentry_f) * _shadow_mult, 8)
                    else:
                        starget_adj = starget_f
                else:
                    sstop_adj = sstop_f
                    starget_adj = starget_f

                # Trade-size: FLAT / BALANCE_PCT / KELLY (identiek aan live_trader)
                if _sizing_mode == "FLAT":
                    _trade_eur = _bs_float("shadow_trade_size_eur", 10.0)
                elif _sizing_mode == "BALANCE_PCT":
                    _pct  = _bs_float("shadow_sizing_pct", 0.12)
                    _smin = _bs_float("shadow_sizing_min", 5.0)
                    _smax = _bs_float("shadow_sizing_max", 15.0)
                    _trade_eur = max(_smin, min(_smax, _sizing_balance_eur * _pct))
                else:
                    _trade_eur = safe_float(skelly, 7.0) or 7.0
                sqty = round(_trade_eur / max(sentry_f, 0.0001), 6)

                # INSERT — zelfde kolommen/waarden als live_trader.shadow_buy()
                _tk = f"SHADOW|{ssymbol}|{int(time.time())}"
                _market = f"{_coin}-EUR"
                cur.execute("""
                    INSERT INTO experience_trades
                    (trade_key, source, coin, symbol, bitvavo_market, timestamp,
                     entry_time, setup_type, market_regime,
                     entry, stop, target, qty, amount_eur, score,
                     outcome, status, is_shadow, created_at)
                    VALUES (%s,'SHADOW',%s,%s,%s,NOW(),NOW(),%s,%s,%s,%s,%s,%s,%s,%s,'OPEN','OPEN',TRUE,NOW())
                    ON CONFLICT (trade_key) DO NOTHING
                """, (_tk, _coin, ssymbol, _market,
                      safe_str(ssetup, "TREND_PULLBACK"),
                      safe_str(sregime, _btc_regime_now or "UNKNOWN"),
                      sentry_f, sstop_adj, starget_adj,
                      sqty, _trade_eur, safe_float(sscore)))
                cur.execute("UPDATE pending_approvals SET status='SHADOW' WHERE id=%s", (sid,))
                _opened += 1
                log(f"shadow OPEN: {ssym_up} score={sscore} entry={sentry_f} "
                    f"stop={sstop_adj} target={starget_adj} eur={_trade_eur:.2f} "
                    f"regime={safe_str(sregime)}")
        except Exception as e:
            log(f"open-pass fout ({srow[1] if len(srow) > 1 else '?'}): {e}")
            try:
                with conn.cursor() as _sp:
                    _sp.execute("ROLLBACK TO SAVEPOINT sp_open_row")
            except Exception:
                conn.rollback()

    log(f"open-pass: {_opened}/{len(shadow_rows)} geopend")
    return _opened


# ============================================================
# CLOSE-PASS — zelfde exits als trade_monitor.evaluate_shadow_for_symbol
# (stop / target / 48u max-hold; outcome herberekend uit pnl_eur)
# ============================================================
def close_pass(conn) -> int:
    # Guard: als trade_monitor zelf draait (verse heartbeat) niets doen
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT laatste_run FROM bot_health WHERE service='trade_monitor'")
            row = cur.fetchone()
            if row and row[0] and (now_utc() - row[0]).total_seconds() < 600:
                log("close-pass overgeslagen — trade_monitor actief")
                return 0
    except Exception:
        conn.rollback()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT trade_key, symbol, entry, COALESCE(stop_loss, stop) AS stop,
                   target, qty, amount_eur, entry_time,
                   max_price_seen, min_price_seen
            FROM experience_trades
            WHERE status='OPEN' AND is_shadow=TRUE AND source='SHADOW'
            ORDER BY entry_time ASC
            LIMIT 40
        """)
        open_rows = cur.fetchall()

    if not open_rows:
        return 0

    max_hold_min = MAX_HOLD_HOURS * 2 * 60  # shadow = 48u, zelfde als trade_monitor
    _closed = 0
    for row in open_rows:
        try:
            with conn.cursor() as _sp:
                _sp.execute("SAVEPOINT sp_close_row")
            (tk, symbol, entry, stop, target, qty, amount_eur,
             entry_time, max_seen, min_seen) = row
            entry      = safe_float(entry)
            stop       = safe_float(stop)
            target     = safe_float(target)
            qty        = safe_float(qty)
            amount_eur = safe_float(amount_eur, 10.0)
            current    = get_binance_price(symbol)
            if not current or current <= 0:
                continue

            hold_min = ((now_utc() - entry_time).total_seconds() / 60) if entry_time else 0.0
            max_seen = max(safe_float(max_seen, current), current)
            min_seen = min(safe_float(min_seen, current) or current, current)

            exit_reden = None
            if hold_min >= max_hold_min:
                exit_reden = "MAX_HOLD_TIME"
            elif stop > 0 and current <= stop:
                exit_reden = "STOP_LOSS"
            elif target > 0 and current >= target:
                exit_reden = "TARGET_HIT"

            with conn.cursor() as cur:
                if not exit_reden:
                    cur.execute("""
                        UPDATE experience_trades
                        SET max_price_seen=%s, min_price_seen=%s, monitor_updated_at=NOW()
                        WHERE trade_key=%s AND status='OPEN'
                    """, (max_seen, min_seen, tk))
                    continue

                # PnL incl. fees — identiek aan trade_monitor._log_shadow_outcome
                if qty <= 0 and entry > 0:
                    qty = amount_eur / entry
                fee_entry = amount_eur * (BITVAVO_FEE_PCT + SLIPPAGE_PCT)
                fee_exit  = current * qty * (BITVAVO_FEE_PCT + SLIPPAGE_PCT) if qty > 0 else 0.0
                gross_pnl = (current - entry) * qty if entry > 0 and qty > 0 else 0.0
                pnl_eur   = gross_pnl - fee_entry - fee_exit
                pnl_pct   = round(((current - entry) / entry) * 100, 4) if entry > 0 else 0.0
                outcome   = "WIN" if pnl_eur > 0 else "LOSS"
                risk      = abs(entry - stop) if stop > 0 and entry > 0 else 0.0
                exit_r    = round((current - entry) / risk, 3) if risk > 0 else 0.0
                mfe_r     = round((max_seen - entry) / risk, 3) if risk > 0 else 0.0
                mae_r     = round((entry - min_seen) / risk, 3) if risk > 0 else 0.0

                cur.execute("""
                    UPDATE experience_trades SET
                        outcome=%s, pnl_eur=%s, pnl_pct=%s, result_r=%s, exit_price=%s,
                        status='CLOSED', closed_at=NOW(), exit_time=NOW(),
                        mfe_r=%s, mae_r=%s,
                        max_price_seen=%s, min_price_seen=%s, exit_reden=%s
                    WHERE trade_key=%s AND status='OPEN'
                """, (outcome, round(pnl_eur, 4), pnl_pct, exit_r, current,
                      mfe_r, mae_r, max_seen, min_seen, exit_reden, tk))
                if cur.rowcount > 0:
                    _closed += 1
                    log(f"shadow CLOSE: {symbol} {outcome} pnl={pnl_eur:.4f} "
                        f"R={exit_r:.2f} ({exit_reden})")
        except Exception as e:
            log(f"close-pass fout ({row[1] if len(row) > 1 else '?'}): {e}")
            try:
                with conn.cursor() as _sp:
                    _sp.execute("ROLLBACK TO SAVEPOINT sp_close_row")
            except Exception:
                conn.rollback()

    log(f"close-pass: {_closed}/{len(open_rows)} gesloten")
    return _closed


def main(dry_run: bool = False, forceer_tijdgates: bool = False) -> None:
    if not DATABASE_URL:
        log("KRITIEK: DATABASE_URL ontbreekt")
        return
    conn = None
    try:
        conn = db_connect()
        n_open  = open_pass(conn, forceer_tijdgates=forceer_tijdgates)
        n_close = close_pass(conn)
        if dry_run:
            conn.rollback()
            log(f"DRY-RUN: {n_open} open / {n_close} close — ALLES teruggedraaid (rollback)")
        else:
            conn.commit()
            _heartbeat(conn, "OK", f"{n_open} geopend, {n_close} gesloten")
    except Exception as e:
        log(f"main fout: {e}")
        if conn:
            try:
                conn.rollback()
                if not dry_run:
                    _heartbeat(conn, "FOUT", str(e))
            except Exception:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv,
         forceer_tijdgates="--forceer-tijdgates" in sys.argv)
