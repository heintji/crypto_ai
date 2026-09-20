#!/usr/bin/env python3
"""One short Gate VBREAK V1 executor run.

Render cron may invoke this every minute. It is fail-closed: no explicit pair
list, database, credentials, or LIVE_V1 confirmation means no orders. The first
live rollout should use one pair and a tiny quote budget.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import psycopg2

from trading.gate_alert import kanaal_gereed, maak_alert
from trading.gate_api import GateAPI, GateError
from trading.gate_live_engine import Engine, Limits
from trading.gate_live_store import Store
from trading.gate_v1 import Candle, Position, Quote, entry_signal, exit_signal


UTC = timezone.utc


def now():
    return datetime.now(UTC)


def candle_rows(rows, interval_hours):
    out = []
    for row in rows:
        try:
            if len(row) > 7 and str(row[7]).lower() != "true":
                continue
            ts = datetime.fromtimestamp(int(float(row[0])), UTC)
            end = ts.replace()  # the API interval is known by the caller
            from datetime import timedelta
            end += timedelta(hours=interval_hours)
            out.append(Candle(ts, end, float(row[5]), float(row[3]), float(row[4]),
                              float(row[2]), float(row[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return sorted(out, key=lambda b: b.start)


def quote_from(ticker, at):
    bid, ask = float(ticker["highest_bid"]), float(ticker["lowest_ask"])
    return Quote(at, bid, ask)


def pair_age(meta, current):
    for key in ("trade_start", "trade_start_time"):
        if meta.get(key) not in (None, "", 0):
            try:
                value = float(meta[key])
                return max(0.0, (current.timestamp() - value) / 86400)
            except (ValueError, TypeError):
                pass
    return 0.0


def load_pairs():
    raw = os.getenv("GATE_V1_PAIRS", "").upper()
    return sorted({p.strip() for p in raw.split(",") if p.strip()})


def main():
    pairs = load_pairs()
    database = os.getenv("DATABASE_URL", "").strip()
    key, secret = os.getenv("GATE_API_KEY", ""), os.getenv("GATE_API_SECRET", "")
    live = os.getenv("GATE_V1_LIVE", "false").lower() == "true"
    confirmed = os.getenv("GATE_V1_CONFIRM", "") == "LIVE_V1"
    # Zonder werkend meldkanaal niet handelen: de hele veiligheid van deze bot
    # leunt erop dat een pauze of een onbeschermde positie bij Hein terechtkomt
    # (Fable-controle 20-9).
    meld_ok, meld_reden = kanaal_gereed()
    if live and confirmed and not meld_ok:
        print(f"GATE V1: safe no-op — geen meldkanaal ({meld_reden})", flush=True)
        return 0
    if not pairs or not database or not key or not secret or (live and not confirmed):
        print("GATE V1: safe no-op (pairs/database/credentials/confirmation ontbreken)", flush=True)
        return 0

    conn = psycopg2.connect(database, sslmode="require", connect_timeout=10)
    store = Store(conn, os.getenv("GATE_V1_ACCOUNT", "default"))
    store.install()
    # Eén beurs voor alles. Gate EU heeft een eigen host; tegen api.gateio.ws
    # geeft een EU-sleutel INVALID_KEY (gemeten 20-9), wat er ten onrechte
    # uitzag als een ongeldige sleutel.
    basis = os.getenv("GATE_API_BASE", "https://api.gateeu.com").strip()
    api = GateAPI(key, secret, base_url=basis, trading_enabled=live and confirmed)
    quote_munt = os.getenv("GATE_V1_QUOTE", "USDC").strip().upper()
    limits = Limits(
        order_quote=Decimal(os.getenv("GATE_V1_ORDER_USDT", "10")),
        capital_quote=Decimal(os.getenv("GATE_V1_CAPITAL_USDT", "10")),
        daily_loss_quote=Decimal(os.getenv("GATE_V1_DAILY_LOSS_USDT", "1")),
        max_positions=int(os.getenv("GATE_V1_MAX_POSITIONS", "1")),
        allow_entries=live and confirmed,
        quote_currency=quote_munt,
    )
    alert = maak_alert()
    engine = Engine(api, store, limits, alert=alert)
    current = now()
    with store.run_lock() as locked:
        if not locked:
            print("GATE V1: vorige run draait nog", flush=True)
            return 0
        store.heartbeat("start")
        try:
            # Recover every nonterminal position before considering an entry.
            for position in store.positions():
                engine.reconcile(position)
            active = store.positions()
            # VERKOPEN EERST, en voor ELKE open positie — los van de kooplijst,
            # los van de handelsstatus en los van de vraag of er al een gesloten
            # uurcandle van vandaag is. Die drie dingen zaten eerder in één
            # `continue` boven de verkooplus: vlak na middernacht bleef de
            # dagwissel-verkoop daardoor liggen, een munt die "alleen
            # verkoopbaar" werd verloor zijn verkoopcontrole juist op het moment
            # dat je eruit wilt, en een munt die uit de lijst gehaald werd kreeg
            # helemaal geen controle meer (Astra + Fable, 20-9).
            for position in active:
                if position["state"] != "PROTECTED":
                    continue
                pair = position["pair"]
                data = position["data"]
                try:
                    quote = quote_from(api.ticker(pair), current)
                    hourly = candle_rows(api.candles(pair, "1h", 30), 1)
                except GateError as exc:
                    # Eén hapering mag niet alle andere posities meesleuren.
                    alert(f"Verkoopcontrole {pair} mislukt: {type(exc).__name__}")
                    continue
                decision = exit_signal(Position(datetime.fromisoformat(data["entry_at"]),
                                                float(data["entry_price"]), float(data["day_open"])),
                                        quote, hourly, current)
                if decision:
                    engine.close(position, decision.reason)
            # Referentiemarkt in dezelfde tegenmunt: BTC_USDT bestaat niet op
            # elke beurs (op Gate EU niet).
            btc_paar = os.getenv("GATE_V1_BTC_PAIR", f"BTC_{quote_munt}")
            btc = candle_rows(api.candles(btc_paar, "1d", 220), 24)
            if not btc:
                raise RuntimeError(f"Dagdata voor {btc_paar} ontbreekt")
            active = store.positions()
            for pair in pairs:
                meta = api.pair(pair)
                if (meta.get("quote") or pair.rpartition("_")[2]) != quote_munt:
                    alert(f"{pair} handelt niet in {quote_munt} en wordt overgeslagen")
                    continue
                if meta.get("trade_status") not in ("tradable", "buyable"):
                    continue
                ticker = api.ticker(pair)
                quote = quote_from(ticker, current)
                hourly = candle_rows(api.candles(pair, "1h", 30), 1)
                today = [b for b in hourly if b.start.date() == current.date()]
                if not today:
                    continue
                day_open = today[0].open
                prev = store.cache_get("quote:" + pair)
                previous = None
                if prev:
                    previous = Quote(datetime.fromisoformat(prev["at"]), float(prev["bid"]), float(prev["ask"]))
                volume = float(ticker.get("quote_volume") or 0)
                signal = entry_signal(pair, candle_rows(api.candles(pair, "1d", 40), 24), day_open,
                                      previous, quote, btc, current,
                                      quote_volume_24h=volume,
                                      listing_age_days=pair_age(meta, current),
                                      traded_today=any(p["pair"] == pair and p["day"] == current.date()
                                                       for p in active))
                store.cache_set("quote:" + pair, {"at": quote.at, "bid": quote.bid, "ask": quote.ask})
                if signal and live and confirmed:
                    engine.open(signal, meta)
            store.heartbeat("success")
        except Exception as exc:  # noqa: BLE001 — elke crash moet zichtbaar zijn
            store.heartbeat("failure", type(exc).__name__)
            store.pause("Executor failed: " + type(exc).__name__)
            alert(f"Uitvoerder gestopt met {type(exc).__name__}: {exc}. "
                  "Het account staat op pauze tot je het vrijgeeft.")
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
