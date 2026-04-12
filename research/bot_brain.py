#!/usr/bin/env python3
"""
Bot Brain v2.0 — Knowledge-Driven Trading Brain
================================================
Steps 3-6 of the knowledge system:
  3. Write knowledge daily (refresh brain_knowledge from experience_trades)
  4. Claude reads knowledge, gives advice
  5. Automatic actions (blacklist/whitelist based on data)
  6. Verification loop (check if past decisions improved results)

Runs daily as Render Cron Job.
"""
from __future__ import annotations
import os, sys, json, traceback
import psycopg2
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from bot_health_helper import health_update
except ImportError:
    def health_update(*a, **k): pass

DATABASE_URL = os.environ.get("DATABASE_URL", "")
TODAY = datetime.now(timezone.utc).strftime("%Y%m%d")

def log(msg: str):
    print(f"[BRAIN {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)

def db_connect():
    return psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=10)


# ============================================================
# HELPER: save knowledge topic
# ============================================================
def save_knowledge(cur, topic, category, content, confidence, evidence,
                   wr=None, avg_r=None, pnl=None, links=None, meta=None):
    cur.execute("""
        INSERT INTO brain_knowledge (topic, category, content, confidence, evidence,
            win_rate, avg_r, total_pnl, links, metadata)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (topic) DO UPDATE SET
            content=EXCLUDED.content, confidence=EXCLUDED.confidence,
            evidence=EXCLUDED.evidence, win_rate=EXCLUDED.win_rate,
            avg_r=EXCLUDED.avg_r, total_pnl=EXCLUDED.total_pnl,
            links=EXCLUDED.links, metadata=EXCLUDED.metadata, updated_at=NOW()
    """, (topic, category, content, confidence, evidence,
          wr, avg_r, pnl, links or [], json.dumps(meta or {})))


# ============================================================
# STEP 3: Write knowledge daily
# ============================================================
def step3_write_knowledge(conn) -> int:
    """Refresh all brain_knowledge from experience_trades. Returns topic count."""
    log("STEP 3: Writing knowledge from trade data...")
    cur = conn.cursor()
    count = 0

    # --- COINS ---
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
    for r in cur.fetchall():
        sym, n, w, wr, ar, pnl, mfe, mae, setup, regime = r
        wr, ar, pnl = float(wr or 0), float(ar or 0), float(pnl or 0)
        mfe, mae = float(mfe or 0), float(mae or 0)
        conf = min(1.0, n / 50)
        v = "KOPEN" if wr >= 55 else ("VERMIJDEN" if wr < 35 else "NEUTRAAL")
        txt = f"{v}: {wr}% WR ({w}W/{n-w}L), R={ar}, PnL={pnl}, MFE={mfe}R, MAE={mae}R. Setup: {setup}, regime: {regime}."
        links = []
        if setup: links.append(f"setup/{setup}")
        if regime: links.append(f"regime/{regime}")
        save_knowledge(cur, f"coin/{sym}", "coin", txt, conf, n, wr, ar, pnl, links,
                       {"verdict": v, "mfe": mfe, "mae": mae})
        count += 1
    log(f"  {count} coins")

    # --- SETUPS ---
    cur.execute("""
        SELECT setup_type, COUNT(*) as n, COUNT(*) FILTER(WHERE outcome='WIN') as w,
               ROUND(100.0*COUNT(*) FILTER(WHERE outcome='WIN')/NULLIF(COUNT(*),0),1) as wr,
               ROUND(AVG(result_r)::numeric,3) as ar, ROUND(SUM(pnl_eur)::numeric,2) as pnl,
               ROUND(AVG(mfe_r)::numeric,2) as mfe, ROUND(AVG(mae_r)::numeric,2) as mae
        FROM experience_trades WHERE status='CLOSED' AND outcome IN ('WIN','LOSS')
        AND setup_type IS NOT NULL AND setup_type != ''
        GROUP BY setup_type HAVING COUNT(*) >= 3
    """)
    sc = 0
    for r in cur.fetchall():
        s, n, w, wr, ar, pnl, mfe, mae = r
        wr, ar, pnl = float(wr or 0), float(ar or 0), float(pnl or 0)
        mfe, mae = float(mfe or 0), float(mae or 0)
        gap = round(mfe - ar, 2)
        v = "STERK" if wr >= 50 else ("ZWAK" if wr < 35 else "GEMIDDELD")
        txt = f"{v}: {wr}% WR ({w}W/{n-w}L), R={ar}, PnL={pnl}. MFE={mfe}R, gap={gap}R."
        save_knowledge(cur, f"setup/{s}", "setup", txt, min(1.0, n/100), n, wr, ar, pnl,
                       [], {"verdict": v, "gap": gap})
        sc += 1
    log(f"  {sc} setups")

    # --- REGIMES ---
    cur.execute("""
        SELECT market_regime, COUNT(*) as n, COUNT(*) FILTER(WHERE outcome='WIN') as w,
               ROUND(100.0*COUNT(*) FILTER(WHERE outcome='WIN')/NULLIF(COUNT(*),0),1) as wr,
               ROUND(AVG(result_r)::numeric,3) as ar, ROUND(SUM(pnl_eur)::numeric,2) as pnl
        FROM experience_trades WHERE status='CLOSED' AND outcome IN ('WIN','LOSS')
        AND market_regime IS NOT NULL
        GROUP BY market_regime HAVING COUNT(*) >= 3
    """)
    rc = 0
    for r in cur.fetchall():
        regime, n, w, wr, ar, pnl = r
        wr, ar, pnl = float(wr or 0), float(ar or 0), float(pnl or 0)
        txt = f"{wr}% WR ({w}W/{n-w}L), R={ar}, PnL={pnl}."
        save_knowledge(cur, f"regime/{regime}", "regime", txt, min(1.0, n/200), n, wr, ar, pnl)
        rc += 1
    log(f"  {rc} regimes")

    # --- SCORE RANGES ---
    cur.execute("""
        SELECT CASE WHEN score>=95 THEN '95-100' WHEN score>=90 THEN '90-94'
                    WHEN score>=85 THEN '85-89' WHEN score>=80 THEN '80-84' ELSE 'onder-80' END as b,
               COUNT(*) as n, COUNT(*) FILTER(WHERE outcome='WIN') as w,
               ROUND(100.0*COUNT(*) FILTER(WHERE outcome='WIN')/NULLIF(COUNT(*),0),1) as wr,
               ROUND(AVG(result_r)::numeric,3) as ar, ROUND(SUM(pnl_eur)::numeric,2) as pnl
        FROM experience_trades WHERE status='CLOSED' AND outcome IN ('WIN','LOSS') AND score IS NOT NULL
        GROUP BY b HAVING COUNT(*) >= 3 ORDER BY b
    """)
    bc = 0
    for r in cur.fetchall():
        b, n, w, wr, ar, pnl = r
        wr, ar, pnl = float(wr or 0), float(ar or 0), float(pnl or 0)
        v = "GOED" if wr >= 45 else "SLECHT"
        txt = f"{v}: score {b} = {wr}% WR ({w}W/{n-w}L), R={ar}, PnL={pnl}."
        save_knowledge(cur, f"score/{b}", "score", txt, min(1.0, n/100), n, wr, ar, pnl,
                       [], {"verdict": v})
        bc += 1
    log(f"  {bc} score ranges")

    # --- EXIT EFFICIENCY ---
    cur.execute("""
        SELECT ROUND(AVG(mfe_r)::numeric,2), ROUND(AVG(mae_r)::numeric,2),
               ROUND(AVG(result_r)::numeric,2), COUNT(*)
        FROM experience_trades WHERE status='CLOSED' AND outcome IN ('WIN','LOSS') AND mfe_r IS NOT NULL
    """)
    r = cur.fetchone()
    if r and r[3] and r[3] > 0:
        mfe, mae, ar, n = float(r[0] or 0), float(r[1] or 0), float(r[2] or 0), r[3]
        gap = round(mfe - ar, 2)
        advice = "Trailing stop te los!" if gap > 1.0 else "Acceptabel."
        txt = f"MFE={mfe}R, MAE={mae}R, Avg R={ar}R. Gap={gap}R. {advice}"
        save_knowledge(cur, "exit/efficiency", "exit", txt, min(1.0, n/500), n, None, ar, None,
                       [], {"mfe": mfe, "mae": mae, "gap": gap})
        log(f"  exit gap={gap}R")

    # --- SOURCES ---
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
        save_knowledge(cur, f"source/{src}", "source", txt, min(1.0, n/100), n, wr, ar, pnl)

    conn.commit()
    cur.execute("SELECT COUNT(*) FROM brain_knowledge")
    total = cur.fetchone()[0]
    log(f"  Total knowledge topics: {total}")
    return total


# ============================================================
# STEP 4: Claude reads knowledge, gives advice
# ============================================================
def step4_claude_advice(conn) -> str:
    """Gather top-20 topics, ask Claude for 3 changes. Returns advice text."""
    log("STEP 4: Claude reads knowledge...")
    cur = conn.cursor()

    # Top 20 by confidence * evidence (most reliable knowledge)
    cur.execute("""
        SELECT topic, content, confidence, evidence, win_rate, avg_r
        FROM brain_knowledge
        WHERE category IN ('coin','setup','regime','score','exit')
        ORDER BY confidence DESC, evidence DESC
        LIMIT 20
    """)
    rows = cur.fetchall()
    if not rows:
        log("  No knowledge topics found, skipping Claude")
        return ""

    # Build compact prompt (~500 tokens)
    lines = []
    for topic, content, conf, ev, wr, ar in rows:
        short = content[:80] if content else ""
        lines.append(f"- {topic}: {short} (conf={conf:.2f}, n={ev})")
    knowledge_block = "\n".join(lines)

    prompt = f"""Je bent een crypto trading coach. Hier is de bot's kennis:

{knowledge_block}

Gebaseerd op deze kennis, wat zijn de 3 belangrijkste dingen die ik moet veranderen?
Per punt: concrete actie + verwachte impact. Kort en bondig, max 3 zinnen per punt."""

    advice = ""
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        advice = resp.content[0].text if resp.content else ""
    except Exception as e:
        log(f"  Claude call failed (optional): {e}")
        advice = f"[Claude unavailable: {str(e)[:100]}]"

    # Store advice as knowledge topic
    if advice:
        save_knowledge(cur, f"advice/daily_{TODAY}", "advice", advice[:2000],
                       0.8, len(rows), None, None, None, [], {"date": TODAY, "topics_used": len(rows)})
        conn.commit()
        log(f"  Advice stored ({len(advice)} chars)")

    return advice


# ============================================================
# STEP 5: Automatic actions (blacklist / whitelist)
# ============================================================
def step5_automatic_actions(conn) -> List[str]:
    """Auto blacklist/whitelist coins based on knowledge. Returns list of changes."""
    log("STEP 5: Automatic actions...")
    cur = conn.cursor()
    changes = []
    decision_nr = 0

    # Get current lists from bot_state
    def get_list(key):
        cur.execute("SELECT value FROM bot_state WHERE key=%s", (key,))
        r = cur.fetchone()
        return json.loads(r[0]) if r and r[0] else []

    def set_list(key, lst):
        cur.execute("""
            INSERT INTO bot_state (key, value, updated_at) VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
        """, (key, json.dumps(lst)))

    blacklist = get_list("coin_blacklist")
    whitelist = get_list("coin_whitelist")

    # Get coin knowledge with enough data
    cur.execute("""
        SELECT topic, win_rate, confidence, evidence, content
        FROM brain_knowledge
        WHERE category='coin' AND confidence > 0.5 AND evidence >= 5
    """)
    for topic, wr, conf, ev, content in cur.fetchall():
        sym = topic.replace("coin/", "")
        wr = float(wr or 0)

        # BLACKLIST: WR < 25%
        if wr < 25 and sym not in blacklist:
            blacklist.append(sym)
            decision_nr += 1
            reason = f"BLACKLIST +{sym}: {wr}% WR, conf={conf:.2f}, n={ev}"
            changes.append(reason)
            log(f"  {reason}")
            save_knowledge(cur, f"decision/{TODAY}_{decision_nr}", "decision",
                           reason, conf, ev, wr, None, None,
                           [f"coin/{sym}"], {"action": "blacklist_add", "coin": sym, "wr": wr})

        # WHITELIST: WR > 60%
        elif wr > 60 and sym not in whitelist:
            whitelist.append(sym)
            decision_nr += 1
            reason = f"WHITELIST +{sym}: {wr}% WR, conf={conf:.2f}, n={ev}"
            changes.append(reason)
            log(f"  {reason}")
            save_knowledge(cur, f"decision/{TODAY}_{decision_nr}", "decision",
                           reason, conf, ev, wr, None, None,
                           [f"coin/{sym}"], {"action": "whitelist_add", "coin": sym, "wr": wr})

    # Write updated lists to bot_state
    set_list("coin_blacklist", blacklist)
    set_list("coin_whitelist", whitelist)
    conn.commit()

    log(f"  Blacklist: {len(blacklist)} coins, Whitelist: {len(whitelist)} coins")
    log(f"  New decisions: {decision_nr}")
    return changes


# ============================================================
# STEP 6: Verification loop
# ============================================================
def step6_verification(conn) -> List[str]:
    """Check past decisions from last 7 days, verify if they improved results."""
    log("STEP 6: Verification loop...")
    cur = conn.cursor()
    results = []

    # Get decisions from last 7 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y%m%d")
    cur.execute("""
        SELECT topic, content, metadata, created_at
        FROM brain_knowledge
        WHERE category='decision' AND topic LIKE 'decision/%%'
        AND created_at >= NOW() - INTERVAL '7 days'
        ORDER BY created_at DESC
    """)
    decisions = cur.fetchall()
    if not decisions:
        log("  No recent decisions to verify")
        return results

    log(f"  Checking {len(decisions)} decisions...")

    for topic, content, meta_raw, created_at in decisions:
        meta = meta_raw if isinstance(meta_raw, dict) else (json.loads(meta_raw) if meta_raw else {})
        action = meta.get("action", "")
        coin = meta.get("coin", "")
        old_wr = meta.get("wr", 0)

        if not coin or "verified" in meta:
            continue

        # Check current performance of coin since decision
        cur.execute("""
            SELECT COUNT(*) as n,
                   COUNT(*) FILTER(WHERE outcome='WIN') as w,
                   ROUND(100.0*COUNT(*) FILTER(WHERE outcome='WIN')/NULLIF(COUNT(*),0),1) as wr
            FROM experience_trades
            WHERE status='CLOSED' AND outcome IN ('WIN','LOSS')
            AND symbol=%s AND exit_time >= %s
        """, (coin, created_at))
        r = cur.fetchone()
        new_trades, new_wins, new_wr = (r[0] or 0), (r[1] or 0), float(r[2] or 0)

        if new_trades < 2:
            verdict = "ONVOLDOENDE DATA"
            rollback = False
        elif action == "blacklist_add":
            # Good decision if coin still bad (we avoided losses)
            verdict = "CORRECT" if new_wr < 40 else "TWIJFELACHTIG"
            rollback = new_wr >= 50  # Coin recovered, maybe remove from blacklist
        elif action == "whitelist_add":
            # Good decision if coin still good
            verdict = "CORRECT" if new_wr >= 50 else "TWIJFELACHTIG"
            rollback = new_wr < 35  # Coin dropped, maybe remove from whitelist
        else:
            verdict = "ONBEKEND"
            rollback = False

        verification = (f"Verificatie: {verdict}. Sinds beslissing: {new_trades} trades, "
                        f"{new_wr}% WR (was {old_wr}%).")
        if rollback:
            verification += f" ROLLBACK AANBEVOLEN: {coin} prestatie veranderd."

        # Update decision with verification
        meta["verified"] = True
        meta["verification_date"] = TODAY
        meta["new_wr"] = new_wr
        meta["new_trades"] = new_trades
        meta["verdict"] = verdict
        meta["rollback_suggested"] = rollback
        cur.execute("""
            UPDATE brain_knowledge SET
                content = content || %s,
                metadata = %s,
                updated_at = NOW()
            WHERE topic = %s
        """, (f" | {verification}", json.dumps(meta), topic))

        result_line = f"  {topic}: {verdict} (was {old_wr}%, nu {new_wr}%)"
        if rollback:
            result_line += " [ROLLBACK]"
        results.append(result_line)
        log(result_line)

    conn.commit()
    log(f"  Verified {len(results)} decisions")
    return results


# ============================================================
# MAIN
# ============================================================
def run():
    log("=" * 60)
    log("Bot Brain v2.0 — Knowledge-Driven Trading Brain")
    log("=" * 60)

    if not DATABASE_URL:
        log("ERROR: DATABASE_URL not set")
        health_update("bot_brain", "ERROR", "No DATABASE_URL")
        return

    conn = db_connect()
    summary = {"topics": 0, "advice": "", "changes": [], "verifications": []}

    try:
        # Step 3: Write knowledge
        summary["topics"] = step3_write_knowledge(conn)

        # Step 4: Claude advice
        summary["advice"] = step4_claude_advice(conn)

        # Step 5: Automatic actions
        summary["changes"] = step5_automatic_actions(conn)

        # Step 6: Verification
        summary["verifications"] = step6_verification(conn)

        # Summary
        log("")
        log("=" * 60)
        log("BRAIN SUMMARY")
        log(f"  Knowledge topics: {summary['topics']}")
        log(f"  New changes:      {len(summary['changes'])}")
        log(f"  Verifications:    {len(summary['verifications'])}")
        if summary["advice"]:
            log(f"  Claude advice:    {summary['advice'][:150]}...")
        for c in summary["changes"]:
            log(f"  > {c}")
        log("=" * 60)

        health_update("bot_brain", "OK",
                      f"{summary['topics']} topics, {len(summary['changes'])} changes, "
                      f"{len(summary['verifications'])} verified")

    except Exception as e:
        log(f"ERROR: {e}")
        traceback.print_exc()
        health_update("bot_brain", "ERROR", str(e)[:200])
    finally:
        conn.close()


if __name__ == "__main__":
    run()
