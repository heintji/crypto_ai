#!/usr/bin/env python3
"""Vul brain_knowledge tabel met kennis uit alle trade data."""
import os, sys, json, psycopg2
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATABASE_URL = os.environ.get("DATABASE_URL", "")

def run():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    cur = conn.cursor()

    def save(topic, category, content, confidence, evidence, wr, avg_r, pnl, links, meta={}):
        cur.execute("""
            INSERT INTO brain_knowledge (topic, category, content, confidence, evidence, win_rate, avg_r, total_pnl, links, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (topic) DO UPDATE SET
                content=EXCLUDED.content, confidence=EXCLUDED.confidence, evidence=EXCLUDED.evidence,
                win_rate=EXCLUDED.win_rate, avg_r=EXCLUDED.avg_r, total_pnl=EXCLUDED.total_pnl,
                links=EXCLUDED.links, metadata=EXCLUDED.metadata, updated_at=NOW()
        """, (topic, category, content, confidence, evidence, wr, avg_r, pnl, links, json.dumps(meta)))

    # 1. COINS
    print("1. Coins...")
    cur.execute("""
        SELECT symbol, COUNT(*) as n, COUNT(*) FILTER(WHERE outcome='WIN') as w,
               ROUND(100.0*COUNT(*) FILTER(WHERE outcome='WIN')/NULLIF(COUNT(*),0),1) as wr,
               ROUND(AVG(result_r)::numeric,3) as ar, ROUND(SUM(pnl_eur)::numeric,2) as pnl,
               ROUND(AVG(mfe_r)::numeric,2) as mfe, ROUND(AVG(mae_r)::numeric,2) as mae,
               MODE() WITHIN GROUP (ORDER BY setup_type) as setup,
               MODE() WITHIN GROUP (ORDER BY market_regime) as regime
        FROM experience_trades WHERE status='CLOSED' AND outcome IN ('WIN','LOSS')
        GROUP BY symbol HAVING COUNT(*) >= 3 ORDER BY wr DESC
    """)
    count = 0
    for r in cur.fetchall():
        sym, n, w, wr, ar, pnl, mfe, mae, setup, regime = r
        wr, ar, pnl, mfe, mae = float(wr or 0), float(ar or 0), float(pnl or 0), float(mfe or 0), float(mae or 0)
        conf = min(1.0, n / 50)
        v = "KOPEN" if wr >= 55 else ("VERMIJDEN" if wr < 35 else "NEUTRAAL")
        txt = f"{v}: {wr}% WR ({w}W/{n-w}L), R={ar}, PnL={pnl}, MFE={mfe}R, MAE={mae}R. Setup: {setup}, regime: {regime}."
        links = []
        if setup: links.append(f"setup/{setup}")
        if regime: links.append(f"regime/{regime}")
        save(f"coin/{sym}", "coin", txt, conf, n, wr, ar, pnl, links, {"verdict": v, "mfe": mfe, "mae": mae})
        count += 1
    print(f"  {count} coins")

    # 2. SETUPS
    print("2. Setups...")
    cur.execute("""
        SELECT setup_type, COUNT(*) as n, COUNT(*) FILTER(WHERE outcome='WIN') as w,
               ROUND(100.0*COUNT(*) FILTER(WHERE outcome='WIN')/NULLIF(COUNT(*),0),1) as wr,
               ROUND(AVG(result_r)::numeric,3) as ar, ROUND(SUM(pnl_eur)::numeric,2) as pnl,
               ROUND(AVG(mfe_r)::numeric,2) as mfe, ROUND(AVG(mae_r)::numeric,2) as mae
        FROM experience_trades WHERE status='CLOSED' AND outcome IN ('WIN','LOSS') AND setup_type IS NOT NULL AND setup_type != ''
        GROUP BY setup_type HAVING COUNT(*) >= 3
    """)
    count = 0
    for r in cur.fetchall():
        s, n, w, wr, ar, pnl, mfe, mae = r
        wr, ar, pnl, mfe, mae = float(wr or 0), float(ar or 0), float(pnl or 0), float(mfe or 0), float(mae or 0)
        gap = round(mfe - ar, 2)
        v = "STERK" if wr >= 50 else ("ZWAK" if wr < 35 else "GEMIDDELD")
        txt = f"{v}: {wr}% WR ({w}W/{n-w}L), R={ar}, PnL={pnl}. MFE={mfe}R, MAE={mae}R, gap={gap}R."
        save(f"setup/{s}", "setup", txt, min(1.0, n/100), n, wr, ar, pnl, [], {"verdict": v, "gap": gap})
        count += 1
    print(f"  {count} setups")

    # 3. REGIMES
    print("3. Regimes...")
    cur.execute("""
        SELECT market_regime, COUNT(*) as n, COUNT(*) FILTER(WHERE outcome='WIN') as w,
               ROUND(100.0*COUNT(*) FILTER(WHERE outcome='WIN')/NULLIF(COUNT(*),0),1) as wr,
               ROUND(AVG(result_r)::numeric,3) as ar, ROUND(SUM(pnl_eur)::numeric,2) as pnl
        FROM experience_trades WHERE status='CLOSED' AND outcome IN ('WIN','LOSS') AND market_regime IS NOT NULL
        GROUP BY market_regime HAVING COUNT(*) >= 3
    """)
    count = 0
    for r in cur.fetchall():
        regime, n, w, wr, ar, pnl = r
        wr, ar, pnl = float(wr or 0), float(ar or 0), float(pnl or 0)
        txt = f"{wr}% WR ({w}W/{n-w}L), R={ar}, PnL={pnl}."
        save(f"regime/{regime}", "regime", txt, min(1.0, n/200), n, wr, ar, pnl, [])
        count += 1
    print(f"  {count} regimes")

    # 4. SCORE RANGES
    print("4. Scores...")
    cur.execute("""
        SELECT CASE WHEN score>=95 THEN '95-100' WHEN score>=90 THEN '90-94'
                    WHEN score>=85 THEN '85-89' WHEN score>=80 THEN '80-84' ELSE 'onder-80' END as b,
               COUNT(*) as n, COUNT(*) FILTER(WHERE outcome='WIN') as w,
               ROUND(100.0*COUNT(*) FILTER(WHERE outcome='WIN')/NULLIF(COUNT(*),0),1) as wr,
               ROUND(AVG(result_r)::numeric,3) as ar, ROUND(SUM(pnl_eur)::numeric,2) as pnl
        FROM experience_trades WHERE status='CLOSED' AND outcome IN ('WIN','LOSS') AND score IS NOT NULL
        GROUP BY b HAVING COUNT(*) >= 3 ORDER BY b
    """)
    count = 0
    for r in cur.fetchall():
        b, n, w, wr, ar, pnl = r
        wr, ar, pnl = float(wr or 0), float(ar or 0), float(pnl or 0)
        v = "GOED" if wr >= 45 else "SLECHT"
        txt = f"{v}: score {b} = {wr}% WR ({w}W/{n-w}L), R={ar}, PnL={pnl}."
        save(f"score/{b}", "score", txt, min(1.0, n/100), n, wr, ar, pnl, [], {"verdict": v})
        count += 1
    print(f"  {count} score ranges")

    # 5. EXIT EFFICIENCY
    print("5. Exit...")
    cur.execute("""
        SELECT ROUND(AVG(mfe_r)::numeric,2), ROUND(AVG(mae_r)::numeric,2),
               ROUND(AVG(result_r)::numeric,2), COUNT(*)
        FROM experience_trades WHERE status='CLOSED' AND outcome IN ('WIN','LOSS') AND mfe_r IS NOT NULL
    """)
    r = cur.fetchone()
    if r and r[3] > 0:
        mfe, mae, ar, n = float(r[0] or 0), float(r[1] or 0), float(r[2] or 0), r[3]
        gap = round(mfe - ar, 2)
        advice = "Trailing stop te los!" if gap > 1.0 else "Acceptabel."
        txt = f"MFE={mfe}R, MAE={mae}R, Avg R={ar}R. Gap={gap}R. {advice}"
        save("exit/efficiency", "exit", txt, min(1.0, n/500), n, None, ar, None, [], {"mfe": mfe, "mae": mae, "gap": gap})
        print(f"  gap={gap}R")

    # 6. SOURCES
    print("6. Sources...")
    cur.execute("""
        SELECT source, COUNT(*) as n, COUNT(*) FILTER(WHERE outcome='WIN') as w,
               ROUND(100.0*COUNT(*) FILTER(WHERE outcome='WIN')/NULLIF(COUNT(*),0),1) as wr,
               ROUND(AVG(result_r)::numeric,3) as ar, ROUND(SUM(pnl_eur)::numeric,2) as pnl
        FROM experience_trades WHERE status='CLOSED' AND outcome IN ('WIN','LOSS')
        GROUP BY source
    """)
    for r in cur.fetchall():
        src, n, w, wr, ar, pnl = r
        wr, ar, pnl = float(wr or 0), float(ar or 0), float(pnl or 0)
        txt = f"{wr}% WR ({w}W/{n-w}L), R={ar}, PnL={pnl}."
        save(f"source/{src}", "source", txt, min(1.0, n/100), n, wr, ar, pnl, [])

    conn.commit()

    # TOTAAL
    cur.execute("SELECT category, COUNT(*) FROM brain_knowledge GROUP BY category ORDER BY category")
    print(f"\n=== BRAIN KNOWLEDGE ===")
    total = 0
    for r in cur.fetchall():
        print(f"  {r[0]:<10} {r[1]} topics")
        total += r[1]
    print(f"  TOTAAL:    {total} topics")

    conn.close()

if __name__ == "__main__":
    run()
