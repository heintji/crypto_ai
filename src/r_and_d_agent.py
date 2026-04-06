"""
r_and_d_agent.py - Fase 5: Zelflerend R&D Agent
=================================================
Versie: 1.0

Draait automatisch op 1e van elke maand (Render Cron Job).
Triggert ook als win rate 3 weken achter elkaar daalt.

Taken:
  1. Analyseer prestaties afgelopen maand (win rate trends, beste combis)
  2. Vergelijk huidige strategieen met sim data  promoveer of diskwalificeer
  3. Controleer coin clusters op veranderingen  update STAR/ZOMBIE lijst
  4. Zoek naar nieuwe patronen in combinatie matrix
  5. Stel concrete aanpassingen voor via WhatsApp
  6. Logt alle bevindingen in agent_logs tabel

Kostenschatting: ~5 Claude API calls/maand = ~0.10 EUR/maand
"""
from __future__ import annotations
import os, sys, time, json, requests, psycopg2
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

DATABASE_URL = os.environ.get("DATABASE_URL", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")
TWILIO_TO = os.environ.get("TWILIO_WHATSAPP_TO", "")

def now_utc(): return datetime.now(timezone.utc)
def log(m): print(f"[R&D {now_utc():%H:%M:%S}] {m}", flush=True)
def sf(v, d=0.0):
    try: return float(v or d)
    except: return d

def db_connect(retries=3):
    for p in range(1, retries+1):
        try:
            conn = psycopg2.connect(DATABASE_URL, sslmode="require", connect_timeout=10)
            conn.autocommit = False
            return conn
        except Exception as e:
            log(f"DB poging {p}: {e}")
            if p < retries: time.sleep(3)
    raise RuntimeError("DB verbinding mislukt")

def safe_rb(conn):
    try: conn.rollback()
    except: pass

def stuur_whatsapp(bericht: str) -> bool:
    """Stuur WhatsApp bericht via Twilio."""
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, TWILIO_TO]):
        log("WhatsApp config ontbreekt"); return False
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            auth=(TWILIO_SID, TWILIO_TOKEN),
            data={"From": TWILIO_FROM, "To": TWILIO_TO, "Body": bericht[:1500]},
            timeout=10
        )
        log(f"WhatsApp: {r.status_code}"); return r.status_code == 201
    except Exception as e:
        log(f"WhatsApp fout: {e}"); return False

def vraag_claude(prompt: str, max_tokens: int = 800) -> str:
    """Vraag Claude om analyse. Circuit breaker bij fouten."""
    if not ANTHROPIC_API_KEY:
        return "[Claude niet beschikbaar - geen API key]"
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        if r.status_code == 200:
            return r.json()["content"][0]["text"]
        else:
            log(f"Claude API fout: {r.status_code}"); return f"[API fout {r.status_code}]"
    except Exception as e:
        log(f"Claude fout: {e}"); return f"[Claude fout: {e}]"

def log_naar_db(conn, actie: str, bevinding: str, aanbeveling: str = ""):
    """Sla R&D bevinding op in agent_logs tabel."""
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO public.agent_logs
                (agent_naam, actie, bevinding, aanbeveling, tijdstip)
                VALUES (%s,%s,%s,%s,NOW())""",
                ("r_and_d_agent", actie, bevinding[:2000], aanbeveling[:1000]))
        conn.commit()
    except Exception as e:
        log(f"Log DB fout: {e}"); safe_rb(conn)

# ================================================================
# ANALYSE 1: Win rate trends afgelopen maand
# ================================================================

def analyseer_winrate_trends(conn) -> Dict:
    """Vergelijk win rate deze maand vs vorige maand per strategie."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                COALESCE(setup_type, strategy_id::text, 'onbekend') as strat,
                COUNT(*) as n,
                ROUND(100.0*SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END)/COUNT(*),1) as wr,
                DATE_TRUNC('month', timestamp) as mnd
            FROM public.experience_trades
            WHERE UPPER(source) IN ('SHADOW','SIM')
              AND outcome IN ('WIN','LOSS')
              AND timestamp > NOW() - INTERVAL '2 months'
            GROUP BY 1,4 HAVING COUNT(*) >= 20
            ORDER BY 1,4
        """)
        rows = cur.fetchall()

    trends = {}
    for r in rows:
        strat, n, wr, mnd = str(r[0]), int(r[1]), float(r[2] or 0), r[3]
        if strat not in trends: trends[strat] = []
        trends[strat].append({"maand": mnd.strftime("%Y-%m"), "n": n, "wr": wr})

    stijgers = []; dalers = []
    for strat, data in trends.items():
        if len(data) >= 2:
            oud, nieuw = data[-2]["wr"], data[-1]["wr"]
            verschil = nieuw - oud
            if verschil >= 3: stijgers.append((strat, oud, nieuw, verschil))
            elif verschil <= -3: dalers.append((strat, oud, nieuw, verschil))

    return {"trends": trends, "stijgers": stijgers, "dalers": dalers}

# ================================================================
# ANALYSE 2: Beste combinatie matrix
# ================================================================

def analyseer_combinaties(conn) -> Dict:
    """Zoek de beste strategie+cluster+fng combinaties."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                COALESCE(strategy_id::text,'?') as strat,
                COALESCE(coin_cluster,'onbekend') as cluster,
                COALESCE(fng_label,'onbekend') as fng,
                COUNT(*) as n,
                ROUND(100.0*SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END)/COUNT(*),1) as wr
            FROM public.experience_trades
            WHERE UPPER(source)='SIM'
              AND outcome IN ('WIN','LOSS')
              AND coin_cluster IS NOT NULL
            GROUP BY 1,2,3 HAVING COUNT(*) >= 15
            ORDER BY wr DESC LIMIT 10
        """)
        beste = cur.fetchall()

    return {"beste_combis": [
        {"strat": r[0], "cluster": r[1], "fng": r[2], "n": r[3], "wr": float(r[4] or 0)}
        for r in beste
    ]}

# ================================================================
# ANALYSE 3: Coin cluster update
# ================================================================

def update_coin_clusters(conn) -> Dict:
    """Herbereken coin clusters op basis van laatste 30 dagen shadow data."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.coin_clusters (symbool, cluster, win_rate, n_trades, bijgewerkt)
            SELECT coin,
                CASE
                    WHEN ROUND(100.0*SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END)/COUNT(*),1) >= 55 THEN 'STAR'
                    WHEN ROUND(100.0*SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END)/COUNT(*),1) >= 45 THEN 'STABLE'
                    WHEN ROUND(100.0*SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END)/COUNT(*),1) >= 35 THEN 'WEAK'
                    ELSE 'ZOMBIE'
                END,
                ROUND(100.0*SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END)/COUNT(*),1),
                COUNT(*), NOW()
            FROM public.experience_trades
            WHERE UPPER(source) IN ('SHADOW','SIM')
              AND outcome IN ('WIN','LOSS')
              AND timestamp > NOW() - INTERVAL '30 days'
            GROUP BY coin HAVING COUNT(*) >= 10
            ON CONFLICT(symbool) DO UPDATE SET
                cluster=EXCLUDED.cluster,
                win_rate=EXCLUDED.win_rate,
                n_trades=EXCLUDED.n_trades,
                bijgewerkt=NOW()
        """)
        bijgewerkt = cur.rowcount
        conn.commit()

        cur.execute("SELECT cluster, COUNT(*) FROM public.coin_clusters GROUP BY cluster")
        verdeling = {r[0]: r[1] for r in cur.fetchall()}

    return {"bijgewerkt": bijgewerkt, "verdeling": verdeling}

# ================================================================
# ANALYSE 4: Win rate daling detectie
# ================================================================

def check_winrate_daling(conn) -> bool:
    """Check of win rate 3 weken achter elkaar gedaald is."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                DATE_TRUNC('week', timestamp) as week,
                ROUND(100.0*SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END)/COUNT(*),1) as wr
            FROM public.experience_trades
            WHERE UPPER(source) IN ('SHADOW','LIVE')
              AND outcome IN ('WIN','LOSS')
              AND timestamp > NOW() - INTERVAL '4 weeks'
            GROUP BY 1 HAVING COUNT(*) >= 5
            ORDER BY 1
        """)
        weken = [(r[0], float(r[1] or 0)) for r in cur.fetchall()]

    if len(weken) < 3: return False
    laatste3 = weken[-3:]
    daalt = all(laatste3[i][1] > laatste3[i+1][1] for i in range(len(laatste3)-1))
    if daalt:
        log(f"Win rate daalt 3 weken: {[w[1] for w in laatste3]}")
    return daalt

# ================================================================
# HOOFDFUNCTIE
# ================================================================

def main():
    log("="*60)
    log("R&D Agent v1.0 gestart")
    log("="*60)

    conn = db_connect()
    try:
        # Analyse 1: Win rate trends
        log("Analyseer win rate trends...")
        trends = analyseer_winrate_trends(conn)
        log(f"Stijgers: {len(trends['stijgers'])} | Dalers: {len(trends['dalers'])}")
        log_naar_db(conn, "winrate_trends",
            json.dumps({"stijgers": trends['stijgers'], "dalers": trends['dalers']}))

        # Analyse 2: Beste combinaties
        log("Analyseer combinatie matrix...")
        combis = analyseer_combinaties(conn)
        beste = combis["beste_combis"][:3]
        log(f"Beste combi: {beste[0] if beste else 'geen data'}")
        log_naar_db(conn, "beste_combinaties", json.dumps(beste))

        # Analyse 3: Coin clusters updaten
        log("Update coin clusters...")
        clusters = update_coin_clusters(conn)
        log(f"Clusters bijgewerkt: {clusters['bijgewerkt']} coins")
        log(f"Verdeling: {clusters['verdeling']}")
        log_naar_db(conn, "cluster_update", json.dumps(clusters))

        # Analyse 4: Win rate daling check
        daling = check_winrate_daling(conn)

        # Stel prompt samen voor Claude analyse
        prompt = f"""Je bent een crypto trading bot analist. Analyseer deze maandelijkse bot prestaties en geef concrete aanbevelingen.

Win rate trends (stijgers/dalers):
{json.dumps(trends['stijgers'][:5] + trends['dalers'][:5], indent=2)}

Beste strategie+cluster+F&G combinaties:
{json.dumps(beste, indent=2)}

Coin cluster verdeling: {clusters['verdeling']}
Win rate 3 weken dalend: {daling}

Geef MAXIMAAL 3 concrete aanbevelingen in het Nederlands.
Elke aanbeveling: wat aanpassen, waarom (data), verwacht effect.
Wees specifiek en beknopt. Max 200 woorden totaal."""

        log("Claude analyse...")
        analyse = vraag_claude(prompt, max_tokens=400)
        log(f"Claude: {analyse[:100]}...")
        log_naar_db(conn, "claude_analyse", analyse)

        # WhatsApp rapport
        bericht = f"""*R&D Agent Maandrapport*

Stijgende strategieen: {len(trends['stijgers'])}
Dalende strategieen: {len(trends['dalers'])}
Coin clusters bijgewerkt: {clusters['bijgewerkt']}
STAR coins: {clusters['verdeling'].get('STAR', 0)}
ZOMBIE coins: {clusters['verdeling'].get('ZOMBIE', 0)}

Beste combi: {beste[0]['strat'] if beste else '?'} + {beste[0]['cluster'] if beste else '?'} WR={beste[0]['wr'] if beste else '?'}%

Aanbevelingen:
{analyse[:500]}"""

        stuur_whatsapp(bericht)
        log("R&D Agent klaar")

    except Exception as e:
        log(f"Fout: {e}")
        import traceback; traceback.print_exc()
        safe_rb(conn)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
