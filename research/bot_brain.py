#!/usr/bin/env python3
"""
Bot Brain v1.0 — Zelflerend Trading Geheugen
=============================================
Analyseert alle trade data en past de bot automatisch aan.

Draait dagelijks als Render Cron Job.

Stappen:
1. Verzamel resultaten (LIVE, SHADOW, REPLAY)
2. Analyseer per coin, setup, regime, score
3. Update blacklist/whitelist automatisch
4. Pas score drempels aan op basis van data
5. Vraag Claude om strategisch advies
6. Log alles in coach_memory
7. Stuur samenvatting via WhatsApp
"""
from __future__ import annotations
import os, sys, time, json
import psycopg2
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple, Any, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from bot_health_helper import health_update
except ImportError:
    def health_update(*a, **k): pass

DATABASE_URL = os.environ.get("DATABASE_URL", "")
MIN_TRADES_FOR_DECISION = int(os.environ.get("BRAIN_MIN_TRADES", "5"))
BLACKLIST_MAX_WR = float(os.environ.get("BRAIN_BLACKLIST_WR", "25"))
WHITELIST_MIN_WR = float(os.environ.get("BRAIN_WHITELIST_WR", "60"))

def log(msg: str):
    print(f"[BRAIN {datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)

def db_connect():
    return psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=10)


# ============================================================
# ANALYSE FUNCTIES
# ============================================================

def get_coin_performance(conn, days: int = 30) -> List[Dict]:
    """Win rate per coin over de laatste N dagen."""
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol,
               COUNT(*) as trades,
               COUNT(*) FILTER(WHERE outcome='WIN') as wins,
               COUNT(*) FILTER(WHERE outcome='LOSS') as losses,
               ROUND(100.0 * COUNT(*) FILTER(WHERE outcome='WIN') / NULLIF(COUNT(*),0), 1) as wr,
               COALESCE(ROUND(SUM(pnl_eur)::numeric, 2), 0) as pnl,
               COALESCE(ROUND(AVG(result_r)::numeric, 3), 0) as avg_r
        FROM experience_trades
        WHERE status='CLOSED' AND outcome IN ('WIN','LOSS')
        AND exit_time >= NOW() - INTERVAL '%s days'
        GROUP BY symbol
        HAVING COUNT(*) >= %s
        ORDER BY wr ASC
    """ % (days, MIN_TRADES_FOR_DECISION))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_setup_performance(conn, days: int = 30) -> List[Dict]:
    """Win rate per setup type."""
    cur = conn.cursor()
    cur.execute("""
        SELECT setup_type,
               COUNT(*) as trades,
               COUNT(*) FILTER(WHERE outcome='WIN') as wins,
               ROUND(100.0 * COUNT(*) FILTER(WHERE outcome='WIN') / NULLIF(COUNT(*),0), 1) as wr,
               COALESCE(ROUND(AVG(result_r)::numeric, 3), 0) as avg_r
        FROM experience_trades
        WHERE status='CLOSED' AND outcome IN ('WIN','LOSS')
        AND setup_type IS NOT NULL AND setup_type != ''
        AND exit_time >= NOW() - INTERVAL '%s days'
        GROUP BY setup_type
        HAVING COUNT(*) >= %s
        ORDER BY wr ASC
    """ % (days, MIN_TRADES_FOR_DECISION))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_score_bucket_performance(conn, days: int = 30) -> List[Dict]:
    """Win rate per score range."""
    cur = conn.cursor()
    cur.execute("""
        SELECT
            CASE
                WHEN score >= 95 THEN '95-100'
                WHEN score >= 90 THEN '90-94'
                WHEN score >= 85 THEN '85-89'
                WHEN score >= 80 THEN '80-84'
                ELSE '<80'
            END as bucket,
            COUNT(*) as trades,
            COUNT(*) FILTER(WHERE outcome='WIN') as wins,
            ROUND(100.0 * COUNT(*) FILTER(WHERE outcome='WIN') / NULLIF(COUNT(*),0), 1) as wr,
            COALESCE(ROUND(AVG(result_r)::numeric, 3), 0) as avg_r
        FROM experience_trades
        WHERE status='CLOSED' AND outcome IN ('WIN','LOSS')
        AND exit_time >= NOW() - INTERVAL '%s days'
        GROUP BY bucket
        HAVING COUNT(*) >= 3
        ORDER BY bucket
    """ % days)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_regime_performance(conn, days: int = 30) -> List[Dict]:
    """Win rate per BTC regime."""
    cur = conn.cursor()
    cur.execute("""
        SELECT market_regime,
               COUNT(*) as trades,
               COUNT(*) FILTER(WHERE outcome='WIN') as wins,
               ROUND(100.0 * COUNT(*) FILTER(WHERE outcome='WIN') / NULLIF(COUNT(*),0), 1) as wr,
               COALESCE(ROUND(AVG(result_r)::numeric, 3), 0) as avg_r
        FROM experience_trades
        WHERE status='CLOSED' AND outcome IN ('WIN','LOSS')
        AND market_regime IS NOT NULL
        AND exit_time >= NOW() - INTERVAL '%s days'
        GROUP BY market_regime
        HAVING COUNT(*) >= %s
        ORDER BY wr ASC
    """ % (days, MIN_TRADES_FOR_DECISION))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_exit_analysis(conn, days: int = 30) -> Dict:
    """Analyse van exit efficiency — MFE vs actual R."""
    cur = conn.cursor()
    cur.execute("""
        SELECT ROUND(AVG(mfe_r)::numeric, 2) as avg_mfe,
               ROUND(AVG(mae_r)::numeric, 2) as avg_mae,
               ROUND(AVG(result_r)::numeric, 2) as avg_r,
               COUNT(*) as trades
        FROM experience_trades
        WHERE status='CLOSED' AND outcome IN ('WIN','LOSS')
        AND mfe_r IS NOT NULL AND result_r IS NOT NULL
        AND exit_time >= NOW() - INTERVAL '%s days'
    """ % days)
    r = cur.fetchone()
    if r:
        return {"avg_mfe": float(r[0] or 0), "avg_mae": float(r[1] or 0),
                "avg_r": float(r[2] or 0), "trades": int(r[3] or 0)}
    return {"avg_mfe": 0, "avg_mae": 0, "avg_r": 0, "trades": 0}


# ============================================================
# ACTIE FUNCTIES
# ============================================================

def update_blacklist(conn, coins: List[Dict]) -> List[str]:
    """Blacklist coins met WR < threshold."""
    changes = []
    cur = conn.cursor()

    for coin in coins:
        wr = float(coin["wr"])
        symbol = coin["symbol"]
        trades = int(coin["trades"])

        if wr <= BLACKLIST_MAX_WR and trades >= MIN_TRADES_FOR_DECISION:
            # Check of al op blacklist
            cur.execute("SELECT value FROM bot_state WHERE key='coin_blacklist'")
            r = cur.fetchone()
            current = json.loads(r[0]) if r and r[0] else []

            if symbol not in current:
                current.append(symbol)
                cur.execute(
                    "INSERT INTO bot_state (key,value) VALUES ('coin_blacklist',%s) "
                    "ON CONFLICT (key) DO UPDATE SET value=%s",
                    (json.dumps(current), json.dumps(current)))
                conn.commit()
                changes.append(f"BLACKLIST +{symbol} ({wr}% WR, {trades} trades)")
                log(f"  BLACKLIST: {symbol} ({wr}% WR, {trades} trades)")

    return changes


def update_whitelist(conn, coins: List[Dict]) -> List[str]:
    """Whitelist coins met WR > threshold."""
    changes = []
    cur = conn.cursor()

    for coin in coins:
        wr = float(coin["wr"])
        symbol = coin["symbol"]
        trades = int(coin["trades"])

        if wr >= WHITELIST_MIN_WR and trades >= MIN_TRADES_FOR_DECISION:
            cur.execute("SELECT value FROM bot_state WHERE key='coin_whitelist'")
            r = cur.fetchone()
            current = json.loads(r[0]) if r and r[0] else []

            if symbol not in current:
                current.append(symbol)
                cur.execute(
                    "INSERT INTO bot_state (key,value) VALUES ('coin_whitelist',%s) "
                    "ON CONFLICT (key) DO UPDATE SET value=%s",
                    (json.dumps(current), json.dumps(current)))
                conn.commit()
                changes.append(f"WHITELIST +{symbol} ({wr}% WR, {trades} trades)")
                log(f"  WHITELIST: {symbol} ({wr}% WR, {trades} trades)")

    return changes


def save_memory(conn, mem_type: str, key: str, value: str, score: float = 0):
    """Sla een herinnering op in coach_memory."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO coach_memory (type, sleutel, waarde, score, bijgewerkt)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (type, sleutel) DO UPDATE SET
            waarde = %s, score = %s, bijgewerkt = NOW()
    """, (mem_type, key, value, score, value, score))
    conn.commit()


def save_brain_log(conn, changes: List[str], insights: List[str]):
    """Log brain beslissingen naar coach_memory."""
    entry = {
        "datum": datetime.now(timezone.utc).isoformat(),
        "aanpassingen": changes,
        "inzichten": insights,
    }
    save_memory(conn, "brain_log",
                f"run_{datetime.now(timezone.utc).strftime('%Y%m%d')}",
                json.dumps(entry, ensure_ascii=False))


def get_claude_advice(conn, coin_data, setup_data, score_data, exit_data) -> str:
    """Vraag Claude om strategisch advies."""
    try:
        import anthropic
        client = anthropic.Anthropic()

        prompt = f"""Je bent een crypto trading coach. Analyseer deze bot-resultaten en geef 3 concrete acties.

COIN PERFORMANCE (laatste 30 dagen):
Beste: {', '.join(f"{c['symbol']} {c['wr']}% ({c['trades']}t)" for c in coin_data[-3:]) if coin_data else 'geen data'}
Slechtste: {', '.join(f"{c['symbol']} {c['wr']}% ({c['trades']}t)" for c in coin_data[:3]) if coin_data else 'geen data'}

SETUP PERFORMANCE:
{chr(10).join(f"- {s['setup_type']}: {s['wr']}% WR ({s['trades']} trades)" for s in setup_data) if setup_data else 'geen data'}

SCORE RANGES:
{chr(10).join(f"- Score {s['bucket']}: {s['wr']}% WR ({s['trades']} trades)" for s in score_data) if score_data else 'geen data'}

EXIT EFFICIENCY:
- Avg MFE: {exit_data['avg_mfe']}R (max bereikt)
- Avg MAE: {exit_data['avg_mae']}R (max drawdown)
- Avg R bij exit: {exit_data['avg_r']}R (werkelijk resultaat)
- MFE-R gap: {exit_data['avg_mfe'] - exit_data['avg_r']:.2f}R (gemiste winst)

Geef exact 3 acties in Nederlands. Per actie: wat aanpassen en waarom.
Kort en concreet, geen algemeenheden."""

        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.content[0].text if resp.content else ""
    except Exception as e:
        log(f"Claude advies fout: {e}")
        return ""


# ============================================================
# HOOFD FUNCTIE
# ============================================================

def run():
    log("=" * 60)
    log("Bot Brain v1.0 — Zelflerend Trading Geheugen")
    log("=" * 60)

    if not DATABASE_URL:
        log("FOUT: DATABASE_URL niet ingesteld")
        return

    conn = db_connect()
    changes = []
    insights = []

    try:
        # 1. Analyseer coin performance
        log("Stap 1: Coin analyse...")
        coins = get_coin_performance(conn, 30)
        log(f"  {len(coins)} coins met >= {MIN_TRADES_FOR_DECISION} trades")

        bad_coins = [c for c in coins if float(c["wr"]) <= BLACKLIST_MAX_WR]
        good_coins = [c for c in coins if float(c["wr"]) >= WHITELIST_MIN_WR]

        if bad_coins:
            insights.append(f"Slechtste coins: {', '.join(c['symbol'] + ' ' + str(c['wr']) + '%' for c in bad_coins[:5])}")
        if good_coins:
            insights.append(f"Beste coins: {', '.join(c['symbol'] + ' ' + str(c['wr']) + '%' for c in good_coins[:5])}")

        # 2. Update blacklist/whitelist
        log("Stap 2: Blacklist/whitelist update...")
        changes += update_blacklist(conn, coins)
        changes += update_whitelist(conn, coins)

        # 3. Analyseer setups
        log("Stap 3: Setup analyse...")
        setups = get_setup_performance(conn, 30)
        for s in setups:
            log(f"  {s['setup_type']}: {s['wr']}% WR ({s['trades']} trades)")

        # 4. Analyseer score ranges
        log("Stap 4: Score analyse...")
        scores = get_score_bucket_performance(conn, 30)
        for s in scores:
            log(f"  Score {s['bucket']}: {s['wr']}% WR ({s['trades']} trades)")
            if float(s["wr"]) < 30 and s["bucket"] in ("80-84", "85-89"):
                insights.append(f"Score range {s['bucket']} presteert slecht ({s['wr']}% WR) — overweeg hogere drempel")

        # 5. Analyseer exits
        log("Stap 5: Exit analyse...")
        exit_data = get_exit_analysis(conn, 30)
        mfe_gap = exit_data["avg_mfe"] - exit_data["avg_r"]
        log(f"  MFE: {exit_data['avg_mfe']}R | MAE: {exit_data['avg_mae']}R | Avg R: {exit_data['avg_r']}R")
        log(f"  MFE-R gap: {mfe_gap:.2f}R (gemiste winst)")
        if mfe_gap > 1.0:
            insights.append(f"MFE-R gap is {mfe_gap:.1f}R — exits zijn te laat of te vroeg, verliest {mfe_gap:.1f}x het risico aan gemiste winst")

        # 6. Regime analyse
        log("Stap 6: Regime analyse...")
        regimes = get_regime_performance(conn, 30)
        for r in regimes:
            log(f"  {r['market_regime']}: {r['wr']}% WR ({r['trades']} trades)")

        # 7. Claude advies
        log("Stap 7: Claude advies...")
        advice = get_claude_advice(conn, coins, setups, scores, exit_data)
        if advice:
            log(f"  Claude: {advice[:200]}")
            insights.append(f"Claude advies: {advice[:500]}")
            save_memory(conn, "claude_advice",
                        f"daily_{datetime.now(timezone.utc).strftime('%Y%m%d')}",
                        advice)

        # 8. Opslaan
        log("Stap 8: Geheugen opslaan...")
        save_brain_log(conn, changes, insights)

        # Sla coin rankings op
        for coin in coins:
            save_memory(conn, "coin_score", coin["symbol"],
                        json.dumps(coin, default=str),
                        float(coin["wr"]))

        # 9. Samenvatting
        log(f"\n{'=' * 60}")
        log(f"BRAIN SAMENVATTING")
        log(f"  Aanpassingen: {len(changes)}")
        log(f"  Inzichten:    {len(insights)}")
        for c in changes:
            log(f"  > {c}")
        for i in insights[:5]:
            log(f"  * {i[:100]}")
        log(f"{'=' * 60}")

        # 10. Heartbeat
        health_update("bot_brain", "OK",
                      f"{len(changes)} changes, {len(insights)} insights")

    except Exception as e:
        log(f"FOUT: {e}")
        import traceback
        traceback.print_exc()
        health_update("bot_brain", "ERROR", str(e)[:200])
    finally:
        conn.close()


if __name__ == "__main__":
    run()
