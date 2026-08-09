#!/usr/bin/env python3
"""MR-TRAIL MEETLAAG deel 2+3: uitvoerbaarheids-cap + go/no-go-rapport.

Herspeelt mr_trail_trades chronologisch en telt alleen de trades mee die een
ECHT account had kunnen nemen:
  - max N gelijktijdige posities (bot_state `mr_max_concurrent`, default 12)
  - nooit een tweede open positie in dezelfde munt
Rekenbasis is de NETTO pnl (pnl_net_pct, gevuld door research/mr_trail_cost.py).
Kapitaal-aanname: gelijke stakes over N slots; elke trade beweegt het account
met pnl_net/N, samengesteld in volgorde van sluiting.

100% LEZEND op mr_trail_trades — dit script wijzigt geen enkele trade- of
strategie-kolom. Het enige schrijven is een samenvattings-JSON naar
bot_state key `mr_trail_report` (voor dashboard/naslag), in eigen try/except.

Output:
  - netto rendement op kapitaal, winrate, max drawdown, langste verliesreeks
  - per week en per BTC-regime (join op btc_regime_4h op entry-candle)
  - GO/NO-GO-tabel: bruto vs netto vs netto-gecapt naast elkaar,
    met de expliciete kanttekening dat het bewijs regime-beperkt is.

Draaien (los van de scan; veiligst — zie docstring onderaan main):
    .venv/bin/python research/mr_trail_report.py
Env: DATABASE_URL. Idempotent: herhaald draaien herrekent alleen.

Config (bot_state):
    mr_max_concurrent   default 12   max gelijktijdige posities in de simulatie

Conservatieve regels in de herspeling:
  - Een slot komt pas vrij als exit_ts STRIKT vóór de nieuwe entry_ts ligt
    (zelfde candle = nog bezet).
  - Gesloten trades zonder pnl_net_pct (kosten nog niet gebackfilld) krijgen
    het mid-tarief als fallback (0.50 fee + 0.30 spread = -0.80%-punt) en
    worden geteld; het rapport meldt hoeveel rijen dat waren.
"""
import json
import os
from bisect import bisect_right
from collections import OrderedDict, defaultdict
from datetime import timezone

FALLBACK_COST_PCT = 0.80   # mid-tarief round-trip als kostenkolommen leeg zijn
DEFAULT_MAX_CONCURRENT = 12


# ────────────────────────────────────────────────────────────
# Pure functies (geen DB) — hier zitten de tests op
# ────────────────────────────────────────────────────────────
def replay_cap(trades, max_concurrent):
    """Chronologische herspeling met accountbeperkingen.

    trades: iterabele van dicts met keys coin, entry_ts, exit_ts, pnl_net
            (moet op entry_ts gesorteerd zijn; ties op id/insert-volgorde).
    Regels: max `max_concurrent` open slots; geen tweede positie in dezelfde
    munt; slot komt pas vrij als exit_ts < entry_ts van de kandidaat.
    Retourneert (genomen_trades, skipped_cap, skipped_dubbel)."""
    open_pos = []          # lijst van (exit_ts, coin)
    taken, skip_cap, skip_dup = [], 0, 0
    for tr in trades:
        open_pos = [p for p in open_pos if not (p[0] < tr["entry_ts"])]
        if any(c == tr["coin"] for _, c in open_pos):
            skip_dup += 1
            continue
        if len(open_pos) >= max_concurrent:
            skip_cap += 1
            continue
        open_pos.append((tr["exit_ts"], tr["coin"]))
        taken.append(tr)
    return taken, skip_cap, skip_dup


def equity_metrics(taken, slots):
    """Samengestelde equity over N gelijke slots, in volgorde van sluiting.
    Retourneert dict: return_pct, max_drawdown_pct, langste_verliesreeks,
    winrate_pct, n, som_netto_pct, gem_netto_pct."""
    out = {"n": len(taken), "return_pct": 0.0, "max_drawdown_pct": 0.0,
           "langste_verliesreeks": 0, "winrate_pct": 0.0,
           "som_netto_pct": 0.0, "gem_netto_pct": 0.0}
    if not taken:
        return out
    cap, peak, max_dd = 1.0, 1.0, 0.0
    streak, worst_streak, wins = 0, 0, 0
    for tr in sorted(taken, key=lambda t: (t["exit_ts"], t["entry_ts"])):
        cap *= 1.0 + (tr["pnl_net"] / 100.0) / slots
        peak = max(peak, cap)
        max_dd = max(max_dd, (peak - cap) / peak)
        if tr["pnl_net"] > 0:
            wins += 1
            streak = 0
        else:
            streak += 1
            worst_streak = max(worst_streak, streak)
    som = sum(t["pnl_net"] for t in taken)
    out.update({
        "return_pct": round((cap - 1.0) * 100.0, 2),
        "max_drawdown_pct": round(max_dd * 100.0, 2),
        "langste_verliesreeks": worst_streak,
        "winrate_pct": round(100.0 * wins / len(taken), 1),
        "som_netto_pct": round(som, 1),
        "gem_netto_pct": round(som / len(taken), 3),
    })
    return out


def regime_lookup(regime_rows):
    """regime_rows: gesorteerde lijst (open_time, regime).
    Retourneert functie ts -> regime van de laatste candle <= ts ('ONBEKEND'
    als er geen eerdere candle is)."""
    times = [r[0] for r in regime_rows]
    names = [r[1] for r in regime_rows]

    def lookup(ts):
        i = bisect_right(times, ts) - 1
        return names[i] if i >= 0 else "ONBEKEND"
    return lookup


def week_key(ts):
    y, w, _ = ts.isocalendar()
    return f"{y}-W{w:02d}"


# ────────────────────────────────────────────────────────────
# DB-laag
# ────────────────────────────────────────────────────────────
def _connect():
    import psycopg2
    url = os.environ["DATABASE_URL"]
    try:
        return psycopg2.connect(url)
    except psycopg2.OperationalError:
        import re
        ext = re.sub(r"@(dpg-[a-z0-9]+-a)/",
                     r"@\1.frankfurt-postgres.render.com/", url)
        if "sslmode" not in ext:
            ext += ("&" if "?" in ext else "?") + "sslmode=require"
        return psycopg2.connect(ext)


def load_data(cur):
    cur.execute("SELECT value FROM bot_state WHERE key='mr_max_concurrent'")
    row = cur.fetchone()
    try:
        slots = max(1, int(float(row[0]))) if row and row[0] else DEFAULT_MAX_CONCURRENT
    except Exception:
        slots = DEFAULT_MAX_CONCURRENT
    cur.execute("""
        SELECT id, coin, entry_ts, exit_ts, pnl_pct, pnl_net_pct, status
          FROM mr_trail_trades
         WHERE status IN ('WIN','LOSS','TIME')
           AND exit_ts IS NOT NULL AND pnl_pct IS NOT NULL
         ORDER BY entry_ts, id
    """)
    trades, n_fallback = [], 0
    for tid, coin, ets, xts, pnl, net, status in cur.fetchall():
        if net is None:
            net = float(pnl) - FALLBACK_COST_PCT
            n_fallback += 1
        trades.append({"id": tid, "coin": coin, "entry_ts": ets, "exit_ts": xts,
                       "pnl": float(pnl), "pnl_net": float(net), "status": status})
    cur.execute("SELECT open_time, regime FROM btc_regime_4h ORDER BY open_time")
    regimes = cur.fetchall()
    return slots, trades, n_fallback, regimes


def bruto_netto_stats(trades):
    """Ongecapte statistiek over ALLE gesloten trades: bruto en netto."""
    n = len(trades)
    if not n:
        return {"n": 0}
    wins_b = sum(1 for t in trades if t["pnl"] > 0)
    wins_n = sum(1 for t in trades if t["pnl_net"] > 0)
    som_b = sum(t["pnl"] for t in trades)
    som_n = sum(t["pnl_net"] for t in trades)
    return {
        "n": n,
        "bruto": {"winrate_pct": round(100 * wins_b / n, 1),
                  "som_pct": round(som_b, 1), "gem_pct": round(som_b / n, 3)},
        "netto": {"winrate_pct": round(100 * wins_n / n, 1),
                  "som_pct": round(som_n, 1), "gem_pct": round(som_n / n, 3)},
    }


def main():
    conn = _connect()
    try:
        with conn.cursor() as cur:
            slots, trades, n_fallback, regimes = load_data(cur)
    finally:
        pass  # verbinding pas sluiten na evt. bot_state-write onderaan

    taken, skip_cap, skip_dup = replay_cap(trades, slots)
    cap_m = equity_metrics(taken, slots)
    alle = bruto_netto_stats(trades)
    lookup = regime_lookup(regimes)

    # per week (op sluitmoment) — alleen de gecapte (uitvoerbare) selectie
    per_week = OrderedDict()
    for t in sorted(taken, key=lambda t: t["exit_ts"]):
        wk = week_key(t["exit_ts"].astimezone(timezone.utc))
        d = per_week.setdefault(wk, {"n": 0, "som_netto_pct": 0.0, "wins": 0})
        d["n"] += 1
        d["som_netto_pct"] += t["pnl_net"]
        d["wins"] += 1 if t["pnl_net"] > 0 else 0

    # per BTC-regime (op entry-moment)
    per_regime = defaultdict(lambda: {"n": 0, "som_netto_pct": 0.0, "wins": 0})
    for t in taken:
        d = per_regime[lookup(t["entry_ts"])]
        d["n"] += 1
        d["som_netto_pct"] += t["pnl_net"]
        d["wins"] += 1 if t["pnl_net"] > 0 else 0

    W = 78
    print("=" * W)
    print("MR-TRAIL MEETRAPPORT — uitvoerbaarheids-cap + kosten "
          f"(max {slots} posities, geen dubbele munt)")
    print("=" * W)
    if n_fallback:
        print(f"LET OP: {n_fallback} trades zonder kostenkolommen -> mid-fallback "
              f"-{FALLBACK_COST_PCT}%-punt gebruikt (draai research/mr_trail_cost.py).")
    print(f"Gesloten trades totaal : {alle.get('n', 0)}")
    print(f"Uitvoerbaar (genomen)  : {len(taken)}  |  geskipt door cap: {skip_cap}"
          f"  |  geskipt dubbele munt: {skip_dup}")
    print("-" * W)
    kop = f"{'':24}{'BRUTO (alles)':>16}{'NETTO (alles)':>16}{'NETTO-GECAPT':>18}"
    print(kop)
    if alle.get("n"):
        b, ne = alle["bruto"], alle["netto"]
        print(f"{'trades':24}{alle['n']:>16}{alle['n']:>16}{cap_m['n']:>18}")
        print(f"{'winrate':24}{b['winrate_pct']:>15}%{ne['winrate_pct']:>15}%"
              f"{cap_m['winrate_pct']:>17}%")
        print(f"{'gem per trade':24}{b['gem_pct']:>15}%{ne['gem_pct']:>15}%"
              f"{cap_m['gem_netto_pct']:>17}%")
        print(f"{'som pnl':24}{b['som_pct']:>15}%{ne['som_pct']:>15}%"
              f"{cap_m['som_netto_pct']:>17}%")
        print(f"{'rendement op kapitaal':24}{'-':>16}{'-':>16}"
              f"{cap_m['return_pct']:>17}%")
        print(f"{'max drawdown':24}{'-':>16}{'-':>16}"
              f"{cap_m['max_drawdown_pct']:>17}%")
        print(f"{'langste verliesreeks':24}{'-':>16}{'-':>16}"
              f"{cap_m['langste_verliesreeks']:>18}")
    print("-" * W)
    print("Per week (gecapt, netto):")
    for wk, d in per_week.items():
        wr = round(100 * d["wins"] / d["n"], 1)
        print(f"  {wk}: {d['n']:>4} trades | som {d['som_netto_pct']:>7.1f}% "
              f"| winrate {wr}%")
    print("Per BTC-regime (gecapt, netto, op entry):")
    for rg, d in sorted(per_regime.items()):
        wr = round(100 * d["wins"] / d["n"], 1)
        print(f"  {rg:8}: {d['n']:>4} trades | som {d['som_netto_pct']:>7.1f}% "
              f"| winrate {wr}%")
    print("-" * W)
    positief = cap_m["return_pct"] > 0
    dominant = max(per_regime.items(), key=lambda kv: kv[1]["n"])[0] if per_regime else "?"
    aandeel = (round(100 * per_regime[dominant]["n"] / len(taken), 1)
               if taken else 0)
    print("GO/NO-GO-BEELD:")
    print(f"  Uitvoerbare, kostengecorrigeerde variant is "
          f"{'POSITIEF' if positief else 'NEGATIEF'}: "
          f"{cap_m['return_pct']}% op kapitaal over {cap_m['n']} trades "
          f"(max DD {cap_m['max_drawdown_pct']}%).")
    print(f"  KANTTEKENING: {aandeel}% van het bewijs komt uit BTC-regime "
          f"{dominant} — dit zegt nog NIETS over andere regimes. "
          f"Geen go/no-go op minder dan ~4 weken en zonder regime-spreiding.")
    print("=" * W)

    # Samenvatting naar bot_state (naslag/dashboard) — eigen try/except,
    # raakt mr_trail_trades of strategie-state niet.
    try:
        summary = {
            "generated_at": __import__("datetime").datetime.now(timezone.utc).isoformat(),
            "slots": slots, "totaal": alle, "gecapt": cap_m,
            "skipped_cap": skip_cap, "skipped_dubbel": skip_dup,
            "kosten_fallback_rijen": n_fallback,
            "per_week": {k: {"n": v["n"], "som_netto_pct": round(v["som_netto_pct"], 1)}
                         for k, v in per_week.items()},
            "per_regime": {k: {"n": v["n"], "som_netto_pct": round(v["som_netto_pct"], 1)}
                           for k, v in per_regime.items()},
        }
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_state (key, value, updated_at)
                VALUES ('mr_trail_report', %s, NOW())
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,
                                                updated_at=NOW()
            """, (json.dumps(summary),))
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[mr-trail-report] bot_state-samenvatting overgeslagen: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
