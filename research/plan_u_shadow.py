#!/usr/bin/env python3
"""PLAN U — schaduw-motor (regime + C1 Rotatie + C2 Bounce).

Draait elke 15 min. Raakt de bestaande bot NIET aan: leest alleen publieke
Bitvavo-koersdata en schrijft schaduw-trades naar eigen tabellen
(plan_u_trades / plan_u_regime) met waterdichte regels:
  - startwaarden onveranderbaar (DB-trigger dwingt af)
  - 1 signaal = 1 trade (UNIQUE signaal_id)
  - MFE/MAE per check bijgewerkt

REGIME (de dirigent):
  GROEN : BTC > MA200 en geen 3-daagse daling >= 5%
  ROOD  : BTC < MA200 of 3-daags <= -5%
  STORM : 48-uurs <= -8%  (acute crash)

C1 ROTATIE (alleen ROOD/STORM): steunbreuk = prijs < laagste low van 20 dagen
  én dagvolume >= 1.5x gemiddeld -> schaduw-"verkoop naar EUR".
  WIN  = prijs zakt 10% verder (daar zou je goedkoper terugkopen)
  LOSS = prijs loopt 5% omhoog (rotatie was onterecht)

C2 BOUNCE (alleen ROOD/STORM): coin -10%+ in 48u én RSI14(1u) < 25
  -> schaduw-long. target +3%, stop -2%, max 12 uur (TIME-exit op slot).

Env: DATABASE_URL. Dependencies: requests, psycopg2-binary.
"""
import os
from datetime import datetime, timedelta, timezone

import psycopg2
import requests

BITVAVO = "https://api.bitvavo.com/v2"
UA = {"User-Agent": "PlanU-Shadow/1.0 (crypto_ai)"}
UNIVERSE = ["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR", "ADA-EUR",
            "LINK-EUR", "AVAX-EUR", "DOGE-EUR", "POL-EUR", "DOT-EUR"]

C1_WIN_PCT = 10.0    # zoveel % lager = geslaagde rotatie (herkoop-niveau)
C1_LOSS_PCT = 5.0    # zoveel % hoger = mislukte rotatie
C1_MAX_DAGEN = 14
C2_TARGET = 3.0
C2_STOP = 2.0
C2_MAX_UREN = 12


def candles(markt: str, interval: str, limit: int = 200, start_ms: int = None):
    url = f"{BITVAVO}/{markt}/candles?interval={interval}&limit={limit}"
    if start_ms:
        url += f"&start={start_ms}"
    r = requests.get(url, headers=UA, timeout=15)
    r.raise_for_status()
    rijen = [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])]
             for c in r.json()]  # ts, open, high, low, close, volume
    return sorted(rijen, key=lambda x: x[0])


def rsi14(closes):
    if len(closes) < 15:
        return None
    winsten, verliezen = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        winsten.append(max(d, 0.0))
        verliezen.append(max(-d, 0.0))
    # Wilder smoothing over de laatste 14
    avg_w = sum(winsten[:14]) / 14
    avg_v = sum(verliezen[:14]) / 14
    for i in range(14, len(winsten)):
        avg_w = (avg_w * 13 + winsten[i]) / 14
        avg_v = (avg_v * 13 + verliezen[i]) / 14
    if avg_v == 0:
        return 100.0
    rs = avg_w / avg_v
    return 100 - 100 / (1 + rs)


def bepaal_regime(cur):
    dag = candles("BTC-EUR", "1d", 210)
    if len(dag) < 200:
        return "GROEN", None  # onvoldoende data -> veilige default, niets nieuws openen
    closes = [c[4] for c in dag]
    prijs = closes[-1]
    ma200 = sum(closes[-200:]) / 200
    drie = (closes[-1] - closes[-4]) / closes[-4] * 100 if len(closes) >= 4 else 0.0
    uur = candles("BTC-EUR", "1h", 50)
    p48 = next((c[4] for c in uur if c[0] >= int((datetime.now(timezone.utc)
                - timedelta(hours=48)).timestamp() * 1000)), uur[0][4])
    v48 = (uur[-1][4] - p48) / p48 * 100

    if v48 <= -8.0:
        regime = "STORM"
    elif prijs < ma200 or drie <= -5.0:
        regime = "ROOD"
    else:
        regime = "GROEN"
    cur.execute(
        """INSERT INTO plan_u_regime (regime, btc_prijs, btc_ma200, drie_daags_pct, achtenveertig_uur_pct)
           VALUES (%s,%s,%s,%s,%s)""",
        (regime, prijs, ma200, round(drie, 2), round(v48, 2)),
    )
    print(f"[regime] {regime} | BTC €{prijs:,.0f} vs MA200 €{ma200:,.0f} | 3d {drie:+.1f}% | 48u {v48:+.1f}%", flush=True)
    return regime, prijs


def huidige_prijs(markt: str) -> float:
    r = requests.get(f"{BITVAVO}/ticker/price?market={markt}", headers=UA, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])


def resolve_open_trades(cur):
    """Werk open schaduw-trades bij: MFE/MAE + stop/target/tijd-checks."""
    cur.execute("""SELECT id, strategie, markt, entry_ts, entry_prijs, initial_stop,
                          initial_target, mfe_pct, mae_pct FROM plan_u_trades WHERE status='OPEN'""")
    for (tid, strat, markt, entry_ts, entry, stop, target, mfe, mae) in cur.fetchall():
        entry, stop, target = float(entry), float(stop), float(target)
        try:
            cs = candles(markt, "15m", 672, int(entry_ts.timestamp() * 1000))
        except Exception as e:
            print(f"[resolve] {markt}: geen data ({e})", flush=True)
            continue
        if not cs:
            continue
        status, exit_prijs, exit_ts = None, None, None
        for ts, _o, high, low, close, _v in cs:
            mfe = max(float(mfe), (high - entry) / entry * 100)
            mae = min(float(mae), (low - entry) / entry * 100)
            if strat == "C2_BOUNCE":  # long: stop onder, target boven
                if low <= stop:
                    status, exit_prijs = "LOSS", stop
                elif high >= target:
                    status, exit_prijs = "WIN", target
            else:  # C1_ROTATIE: "win" = prijs zakt naar herkoop-niveau (target ONDER entry)
                if high >= stop:
                    status, exit_prijs = "LOSS", stop
                elif low <= target:
                    status, exit_prijs = "WIN", target
            if status:
                exit_ts = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                break
        # tijd-exit
        if not status:
            max_duur = timedelta(hours=C2_MAX_UREN) if strat == "C2_BOUNCE" else timedelta(days=C1_MAX_DAGEN)
            if datetime.now(timezone.utc) - entry_ts > max_duur:
                status = "TIME"
                exit_prijs = cs[-1][4]
                exit_ts = datetime.now(timezone.utc)
        if status:
            if strat == "C2_BOUNCE":
                pnl = (exit_prijs - entry) / entry * 100
            else:  # rotatie: winst als exit (herkoop) LAGER ligt dan entry (verkoop)
                pnl = (entry - exit_prijs) / entry * 100
            cur.execute(
                """UPDATE plan_u_trades SET status=%s, exit_prijs=%s, exit_ts=%s,
                          pnl_pct=%s, mfe_pct=%s, mae_pct=%s WHERE id=%s""",
                (status, exit_prijs, exit_ts, round(pnl, 4), round(mfe, 4), round(mae, 4), tid),
            )
            print(f"[resolve] #{tid} {strat} {markt}: {status} {pnl:+.2f}%", flush=True)
        else:
            cur.execute("UPDATE plan_u_trades SET mfe_pct=%s, mae_pct=%s WHERE id=%s",
                        (round(mfe, 4), round(mae, 4), tid))


def zoek_signalen(cur, regime):
    if regime == "GROEN":
        return  # C1/C2 zijn daling-specialisten; in GROEN doet Plan G v2 het werk
    nu = datetime.now(timezone.utc)
    for markt in UNIVERSE:
        try:
            dag = candles(markt, "1d", 25)
            uur = candles(markt, "1h", 80)
        except Exception:
            continue
        if len(dag) < 21 or len(uur) < 50:
            continue
        prijs = uur[-1][4]

        # ── C1 ROTATIE: steunbreuk + volume ──
        laagste20 = min(c[3] for c in dag[-21:-1])
        vol_gem = sum(c[5] for c in dag[-21:-1]) / 20
        vol_vandaag = dag[-1][5]
        if prijs < laagste20 and vol_vandaag >= 1.5 * vol_gem:
            sid = f"C1|{markt}|{dag[-1][0]}"  # max 1 per markt per dag
            cur.execute(
                """INSERT INTO plan_u_trades (strategie, markt, regime, signaal_id, candle_ts,
                       entry_prijs, initial_stop, initial_target, notitie)
                   VALUES ('C1_ROTATIE', %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (signaal_id) DO NOTHING""",
                (markt, regime, sid, datetime.fromtimestamp(dag[-1][0] / 1000, tz=timezone.utc),
                 prijs, prijs * (1 + C1_LOSS_PCT / 100), prijs * (1 - C1_WIN_PCT / 100),
                 f"steunbreuk<20d-low {laagste20:.6g}, vol {vol_vandaag/vol_gem:.1f}x"),
            )
            if cur.rowcount:
                print(f"[C1] rotatie-signaal {markt} @ {prijs:.6g}", flush=True)

        # ── C2 BOUNCE: -10% in 48u + RSI(1u) < 25 ──
        p48 = next((c[4] for c in uur if c[0] >= int((nu - timedelta(hours=48)).timestamp() * 1000)), None)
        if p48 is None:
            continue
        val48 = (prijs - p48) / p48 * 100
        rsi = rsi14([c[4] for c in uur])
        if val48 <= -10.0 and rsi is not None and rsi < 25:
            sid = f"C2|{markt}|{uur[-1][0]}"  # max 1 per markt per uur-candle
            cur.execute(
                """INSERT INTO plan_u_trades (strategie, markt, regime, signaal_id, candle_ts,
                       entry_prijs, initial_stop, initial_target, notitie)
                   VALUES ('C2_BOUNCE', %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (signaal_id) DO NOTHING""",
                (markt, regime, sid, datetime.fromtimestamp(uur[-1][0] / 1000, tz=timezone.utc),
                 prijs, prijs * (1 - C2_STOP / 100), prijs * (1 + C2_TARGET / 100),
                 f"48u {val48:.1f}%, RSI1u {rsi:.0f}"),
            )
            if cur.rowcount:
                print(f"[C2] bounce-signaal {markt} @ {prijs:.6g} (RSI {rsi:.0f})", flush=True)


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    cur = conn.cursor()
    regime, _ = bepaal_regime(cur)
    resolve_open_trades(cur)
    zoek_signalen(cur, regime)
    cur.execute("SELECT status, COUNT(*) FROM plan_u_trades GROUP BY status")
    print("[stand]", dict(cur.fetchall()), flush=True)
    conn.close()


if __name__ == "__main__":
    main()
