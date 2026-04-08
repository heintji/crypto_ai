"""
ai_coach.py  Fase 2 Multi-Agent v4.0
Dagrapport (22:00 UTC), Weekrapport (ma 06:00), Maandanalyse (1e vd maand).
Claude analyseert winrates, patronen, aanbevelingen.
"""
from __future__ import annotations
import os, sys, json, time, traceback
from datetime import datetime, timezone, timedelta

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import anthropic
import psycopg2
from db import db_connect

CLAUDE_MODEL   = "claude-sonnet-4-6"
MAX_TOKENS     = 1200
WHATSAPP_URL   = os.getenv("WEBHOOK_BASE_URL", "")
# Auto-detect mode op basis van dag (tenzij COACH_MODE handmatig ingesteld)
def _auto_mode() -> str:
    m = os.getenv("COACH_MODE", "")
    if m: return m.lower()
    now = datetime.now(timezone.utc)
    if now.day == 1:             return "maand"   # 1e vd maand = maandanalyse
    if now.weekday() == 0:       return "week"    # Maandag = weekrapport
    return "dag"                                   # Rest = dagrapport

REPORT_MODE = _auto_mode()

def now_utc():
    return datetime.now(timezone.utc)

def log(msg):
    print(f"[AI_COACH {now_utc():%H:%M:%S}] {msg}", flush=True)

def _stuur_whatsapp(tekst: str) -> bool:
    if not WHATSAPP_URL:
        log("WhatsApp URL niet ingesteld")
        return False
    try:
        import requests
        r = requests.post(f"{WHATSAPP_URL}/send_message",
                          json={"message": tekst}, timeout=15)
        return r.status_code == 200
    except Exception as e:
        log(f"WhatsApp fout: {e}")
        return False

def _log_agent(conn, mode, tokens, duration, status, fout=""):
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO public.agent_logs
                    (agent_name, status, input_json, output_json,
                     tokens_used, duration_s, error_msg)
                VALUES ('ai_coach', %s, %s, %s, %s, %s, %s)
            """, (status, json.dumps({"mode": mode}), json.dumps({}),
                  tokens, duration, fout))
        conn.commit()
    except Exception as e:
        log(f"agent_log fout: {e}")
        try: conn.rollback()
        except: pass

def _haal_stats(conn, dagen: int) -> dict:
    """Haal trade stats op voor de afgelopen N dagen."""
    try:
        cutoff = (now_utc() - timedelta(days=dagen)).isoformat()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) as totaal,
                    SUM(CASE WHEN UPPER(outcome)='WIN'  THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN UPPER(outcome)='LOSS' THEN 1 ELSE 0 END) as losses,
                    ROUND(AVG(CASE WHEN pnl_eur IS NOT NULL THEN pnl_eur ELSE 0 END)::numeric, 4) as gem_pnl,
                    ROUND(SUM(COALESCE(pnl_eur,0))::numeric, 2) as totaal_pnl,
                    ROUND(AVG(CASE WHEN UPPER(outcome)='WIN' THEN 1.0 ELSE 0.0 END)*100, 1) as winrate
                FROM public.experience_trades
                WHERE UPPER(COALESCE(source,'')) IN ('LIVE','REAL')
                  AND timestamp >= %s
                  AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
            """, (cutoff,))
            row = cur.fetchone()
            live = {"totaal": row[0] or 0, "wins": row[1] or 0,
                    "losses": row[2] or 0, "gem_pnl": float(row[3] or 0),
                    "totaal_pnl": float(row[4] or 0), "winrate": float(row[5] or 0)}

            # Shadow stats
            cur.execute("""
                SELECT COUNT(*),
                    ROUND(AVG(CASE WHEN UPPER(outcome)='WIN' THEN 1.0 ELSE 0.0 END)*100,1)
                FROM public.experience_trades
                WHERE UPPER(COALESCE(source,''))='SHADOW'
                  AND timestamp >= %s
                  AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
            """, (cutoff,))
            sr = cur.fetchone()
            shadow = {"totaal": sr[0] or 0, "winrate": float(sr[1] or 0)}

            # Top 3 best scorende coins
            cur.execute("""
                SELECT coin,
                    COUNT(*) as n,
                    ROUND(AVG(CASE WHEN UPPER(outcome)='WIN' THEN 1.0 ELSE 0.0 END)*100,1) as wr
                FROM public.experience_trades
                WHERE UPPER(COALESCE(source,'')) IN ('LIVE','REAL','SHADOW')
                  AND timestamp >= %s
                  AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
                GROUP BY coin HAVING COUNT(*) >= 3
                ORDER BY wr DESC LIMIT 5
            """, (cutoff,))
            top_coins = [{"coin": r[0], "n": r[1], "wr": float(r[2])} for r in cur.fetchall()]

            # BTC regime
            cur.execute("""
                SELECT regime FROM public.btc_regime_4h
                ORDER BY open_time DESC LIMIT 1
            """)
            br = cur.fetchone()
            regime = br[0] if br else "ONBEKEND"

        return {"live": live, "shadow": shadow,
                "top_coins": top_coins, "regime": regime, "ok": True}
    except Exception as e:
        log(f"Stats fout: {e}")
        return {"ok": False, "error": str(e)}

def dagrapport(conn):
    log(" Dagrapport genereren...")
    t0 = time.time()
    stats = _haal_stats(conn, 1)
    if not stats["ok"]:
        return False

    s = stats["live"]
    sh = stats["shadow"]
    tc = stats.get("top_coins", [])
    top_str = ", ".join([f"{c['coin']} ({c['wr']}%)" for c in tc]) or "geen data"

    prompt = f"""Je bent een crypto trading coach. Analyseer dit dagrapport:

 Afgelopen 24 uur:
 Live trades: {s['totaal']} ({s['wins']}W / {s['losses']}L)
 Live winrate: {s['winrate']}%
 Live PnL: {s['totaal_pnl']:.2f}
 Shadow winrate: {sh['winrate']}% ({sh['totaal']} trades)
 BTC regime: {stats['regime']}
 Top coins: {top_str}

Geef in 4-5 regels:
1. Hoe was de dag?
2. Wat valt op (goed/slecht)?
3. 1 concrete actie voor morgen.

Schrijf in het Nederlands, beknopt en direct."""

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}]
        )
        analyse = resp.content[0].text.strip()
        tokens = resp.usage.input_tokens + resp.usage.output_tokens
        duration = round(time.time() - t0, 2)

        bericht = (
            f" *DAGRAPPORT*  {now_utc():%d %b %Y}\n"
            f"{""*30}\n"
            f"Live: {s['totaal']} trades | WR: {s['winrate']}% | PnL: {s['totaal_pnl']:.2f}\n"
            f"Shadow: {sh['totaal']} trades | WR: {sh['winrate']}%\n"
            f"Regime: {stats['regime']}\n\n"
            f" *Claude analyse:*\n{analyse}"
        )
        _stuur_whatsapp(bericht)
        _log_agent(conn, "dagrapport", tokens, duration, "OK")
        log(f" Dagrapport verstuurd ({tokens} tokens, {duration}s)")
        return True
    except Exception as e:
        _log_agent(conn, "dagrapport", 0, time.time()-t0, "ERROR", str(e))
        log(f" Dagrapport fout: {e}")
        return False

def weekrapport(conn):
    log(" Weekrapport genereren...")
    t0 = time.time()
    stats7  = _haal_stats(conn, 7)
    stats30 = _haal_stats(conn, 30)
    if not stats7["ok"]:
        return False

    s7 = stats7["live"]; s30 = stats30["live"]
    sh7 = stats7["shadow"]; sh30 = stats30["shadow"]
    tc = stats7.get("top_coins", [])
    top_str = ", ".join([f"{c['coin']} ({c['wr']}%)" for c in tc]) or "geen data"

    prompt = f"""Je bent een crypto trading coach. Analyseer dit weekrapport:

 Afgelopen 7 dagen (live):
 Trades: {s7['totaal']} ({s7['wins']}W / {s7['losses']}L)
 Winrate: {s7['winrate']}%
 PnL: {s7['totaal_pnl']:.2f}

 Afgelopen 30 dagen (live):
 Trades: {s30['totaal']} | Winrate: {s30['winrate']}% | PnL: {s30['totaal_pnl']:.2f}

Shadow 7d: {sh7['winrate']}% ({sh7['totaal']} trades)
Shadow 30d: {sh30['winrate']}% ({sh30['totaal']} trades)
BTC regime: {stats7['regime']}
Beste coins: {top_str}

Geef een wekelijkse analyse in 5-6 regels:
1. Trend: gaat het beter of slechter dan vorige week?
2. Vergelijk live vs shadow (leer het systeem van de shadow)?
3. 2 concrete aanbevelingen voor volgende week.
4. Moet de score drempel omhoog/omlaag?

Schrijf in het Nederlands, als een echte trading coach."""

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}]
        )
        analyse = resp.content[0].text.strip()
        tokens = resp.usage.input_tokens + resp.usage.output_tokens
        duration = round(time.time() - t0, 2)

        bericht = (
            f" *WEEKRAPPORT*  week {now_utc():%W %Y}\n"
            f"{""*30}\n"
            f"7d: {s7['totaal']} trades | WR: {s7['winrate']}% | {s7['totaal_pnl']:.2f}\n"
            f"30d: {s30['totaal']} trades | WR: {s30['winrate']}% | {s30['totaal_pnl']:.2f}\n"
            f"Shadow 7d: {sh7['winrate']}% | Regime: {stats7['regime']}\n\n"
            f" *Claude analyse:*\n{analyse}"
        )
        _stuur_whatsapp(bericht)
        _log_agent(conn, "weekrapport", tokens, duration, "OK")
        log(f" Weekrapport verstuurd ({tokens} tokens, {duration}s)")
        return True
    except Exception as e:
        _log_agent(conn, "weekrapport", 0, time.time()-t0, "ERROR", str(e))
        log(f" Weekrapport fout: {e}")
        return False

def maandanalyse(conn):
    log(" Maandanalyse genereren...")
    t0 = time.time()
    stats30 = _haal_stats(conn, 30)
    if not stats30["ok"]:
        return False

    s = stats30["live"]; sh = stats30["shadow"]
    tc = stats30.get("top_coins", [])

    # Blacklist kandidaten (winrate < 35% na 5+ trades)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT coin, COUNT(*) as n,
                    ROUND(AVG(CASE WHEN UPPER(outcome)='WIN' THEN 1.0 ELSE 0.0 END)*100,1) as wr
                FROM public.experience_trades
                WHERE timestamp >= NOW() - INTERVAL '30 days'
                  AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
                GROUP BY coin HAVING COUNT(*) >= 5 AND
                    AVG(CASE WHEN UPPER(outcome)='WIN' THEN 1.0 ELSE 0.0 END) < 0.35
                ORDER BY wr ASC LIMIT 5
            """)
            blacklist_kand = [{"coin": r[0], "n": r[1], "wr": float(r[2])} for r in cur.fetchall()]
    except:
        blacklist_kand = []

    bl_str = ", ".join([f"{c['coin']} ({c['wr']}%)" for c in blacklist_kand]) or "geen"
    top_str = ", ".join([f"{c['coin']} ({c['wr']}%)" for c in tc]) or "geen"

    prompt = f"""Je bent een crypto trading coach. Analyseer de maandelijkse resultaten:

 Afgelopen 30 dagen:
 Live: {s['totaal']} trades | WR: {s['winrate']}% | PnL: {s['totaal_pnl']:.2f}
 Shadow: {sh['totaal']} trades | WR: {sh['winrate']}%
 BTC regime: {stats30['regime']}
 Beste coins: {top_str}
 Blacklist kandidaten (<35% WR): {bl_str}

Geef een maandelijkse diepte-analyse in 8-10 regels:
1. Overall performance beoordeling (goed/matig/slecht + waarom)
2. Live vs shadow vergelijking  wat leert de shadow ons?
3. Coins om te blacklisten (als er zijn)?
4. Parameter aanpassingen: score drempel, positiegrootte, cooldown
5. Doelen voor volgende maand
6. 1 strategische aanbeveling

Schrijf in het Nederlands, professioneel en actionable."""

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        analyse = resp.content[0].text.strip()
        tokens = resp.usage.input_tokens + resp.usage.output_tokens
        duration = round(time.time() - t0, 2)

        bericht = (
            f" *MAANDANALYSE*  {now_utc():%B %Y}\n"
            f"{""*30}\n"
            f"30d live: {s['totaal']} trades | WR: {s['winrate']}% | {s['totaal_pnl']:.2f}\n"
            f"30d shadow: {sh['totaal']} trades | WR: {sh['winrate']}%\n"
            f"Regime: {stats30['regime']}\n"
            f"Blacklist kandidaten: {bl_str}\n\n"
            f" *Claude diepte-analyse:*\n{analyse}"
        )
        _stuur_whatsapp(bericht)
        _log_agent(conn, "maandanalyse", tokens, duration, "OK")
        log(f" Maandanalyse verstuurd ({tokens} tokens, {duration}s)")
        return True
    except Exception as e:
        _log_agent(conn, "maandanalyse", 0, time.time()-t0, "ERROR", str(e))
        log(f" Maandanalyse fout: {e}")
        return False

def top5_check(conn, kandidaten: list) -> list:
    """
    d3: Claude top-5 kwaliteitscheck.
    Filtert de beste 5 van een lijst kandidaten via Claude.
    kandidaten = [{"symbol":..., "score":..., "regime":..., "setup":...}, ...]
    """
    if not kandidaten:
        return kandidaten[:5]
    log(f" Top-5 check: {len(kandidaten)} kandidaten...")
    t0 = time.time()

    prompt = f"""Je bent een crypto trading selectie-agent. Kies de BESTE 5 trades uit deze lijst:

{json.dumps(kandidaten[:15], indent=2, ensure_ascii=False)}

Criteria (zwaarste eerst):
1. Score  82 (hard vereiste)
2. R-ratio  1.5
3. Regime: BULL > RANGE > BEAR
4. Setup: BREAKOUT > MOMENTUM > MEAN_REVERSION
5. Diversificatie: niet 2 identieke setups

Antwoord ALLEEN als JSON array met de symbols van de gekozen 5, bijv:
["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","BNBUSDT"]
(geen markdown, geen uitleg)"""

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        raw_clean = raw.replace("```json","").replace("```","").strip()
        gekozen = json.loads(raw_clean)
        tokens = resp.usage.input_tokens + resp.usage.output_tokens
        duration = round(time.time() - t0, 2)

        # Filter originele kandidaten op gekozen symbols
        gekozen_set = set(gekozen)
        result = [k for k in kandidaten if k.get("symbol") in gekozen_set]
        _log_agent(conn, "top5_check", tokens, duration, "OK")
        log(f" Top-5: {gekozen} ({tokens} tokens)")
        return result
    except Exception as e:
        _log_agent(conn, "top5_check", 0, time.time()-t0, "ERROR", str(e))
        log(f" Top-5 fout (fallback top-5): {e}")
        return sorted(kandidaten, key=lambda x: x.get("score",0), reverse=True)[:5]

def main():
    mode = REPORT_MODE.lower()
    log(f"AI Coach gestart  mode: {mode}")
    conn = db_connect()
    try:
        if mode == "dag":
            dagrapport(conn)
        elif mode == "week":
            weekrapport(conn)
        elif mode == "maand":
            maandanalyse(conn)
        elif mode == "top5":
            # Test mode
            test = [{"symbol": "BTCUSDT", "score": 88, "regime": "BULL", "setup": "BREAKOUT"},
                    {"symbol": "ETHUSDT", "score": 82, "regime": "RANGE", "setup": "MOMENTUM"}]
            print(top5_check(conn, test))
        else:
            log(f"Onbekende mode: {mode}. Gebruik: dag | week | maand | top5")
    finally:
        conn.close()
    log("AI Coach klaar.")

if __name__ == "__main__":
    main()
