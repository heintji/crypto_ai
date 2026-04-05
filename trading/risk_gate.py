"""
risk_gate.py  Fase 2 Multi-Agent v4.0
Claude poortwachter: beoordeelt elke trade vr uitvoering.
Geeft GO / NO-GO terug op basis van marktcontext + score + regime.
"""
from __future__ import annotations
import os, sys, json, time, traceback
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import anthropic
from db import db_connect, set_bot_state, get_bot_state
try:
    from trading.market_sentiment import get_market_context
    SENTIMENT_OK = True
except ImportError:
    SENTIMENT_OK = False
    def get_market_context(symbol=""): return {}

# 
CLAUDE_MODEL     = "claude-sonnet-4-6"
MAX_TOKENS       = 600
CIRCUIT_ERRORS   = 0
CIRCUIT_LIMIT    = 3
CIRCUIT_RESET_S  = 3600
_circuit_open_ts = 0.0

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def log(msg: str) -> None:
    print(f"[RISK_GATE {now_utc().strftime('%H:%M:%S')}] {msg}", flush=True)

# 
# DB: agent_logs schrijven
# 
def _log_agent(conn, status: str, inp: dict, out: dict,
               tokens: int = 0, duration: float = 0.0, error: str = "") -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO public.agent_logs
                    (agent_name, status, input_json, output_json,
                     tokens_used, duration_s, error_msg)
                VALUES ('risk_gate', %s, %s, %s, %s, %s, %s)
            """, (status, json.dumps(inp), json.dumps(out), tokens, duration, error))
        conn.commit()
    except Exception as e:
        log(f"agent_logs fout: {e}")
        try: conn.rollback()
        except: pass

# 
# Claude circuit breaker
# 
def _circuit_ok() -> bool:
    global CIRCUIT_ERRORS, _circuit_open_ts
    if CIRCUIT_ERRORS >= CIRCUIT_LIMIT:
        if time.time() - _circuit_open_ts > CIRCUIT_RESET_S:
            CIRCUIT_ERRORS = 0
            log("Circuit breaker gereset")
        else:
            return False
    return True

def _circuit_fail() -> None:
    global CIRCUIT_ERRORS, _circuit_open_ts
    CIRCUIT_ERRORS += 1
    if CIRCUIT_ERRORS >= CIRCUIT_LIMIT:
        _circuit_open_ts = time.time()
        log(f" Circuit open: {CIRCUIT_ERRORS} fouten")

def _circuit_success() -> None:
    global CIRCUIT_ERRORS
    CIRCUIT_ERRORS = 0

# 
# Hoofd functie: beoordeel trade
# 
def beoordeel_trade(
    symbol: str,
    score: int,
    regime: str,
    entry: float,
    stop: float,
    target: float,
    setup_type: str = "",
    extra_context: dict | None = None,
) -> dict:
    """
    Vraagt Claude of deze trade een GO of NO-GO is.
    Geeft terug: {go: bool, reden: str, tokens: int, script_mode: bool}
    """
    extra = extra_context or {}
    inp = {
        "symbol": symbol, "score": score, "regime": regime,
        "entry": entry, "stop": stop, "target": target,
        "setup_type": setup_type, **extra,
    }

    # R-berekening
    risk_r = round((target - entry) / (entry - stop), 2) if entry > stop else 0.0

    # Fear & Greed harde blokkade
    if SENTIMENT_OK:
        try:
            ctx = get_market_context(symbol)
            fng_val = ctx.get("fear_greed", {}).get("value", 50)
            fng_lbl = ctx.get("fear_greed", {}).get("label", "Neutral")
            cp_sent = ctx.get("cryptopanic", {}).get("sentiment", "neutral")
            # Extreme fear (<20) + bearish nieuws = altijd NO-GO
            if fng_val < 20 and cp_sent == "bearish":
                msg = f" Auto NO-GO: Extreme Fear ({fng_val}) + bearish nieuws"
                log(msg)
                return {"go": False, "reden": msg, "tokens": 0, "script_mode": False, "fng": fng_val}
            # Extreme greed (>85) = extra voorzichtig (laat Claude beslissen)
            extra["fear_greed"] = f"{fng_val} ({fng_lbl})"
            extra["cryptopanic"] = cp_sent
        except Exception as _se:
            log(f"Sentiment fout (skip): {_se}")

    # Script mode als circuit open
    if not _circuit_ok():
        log(f" Script mode  circuit open. Auto-GO voor {symbol}")
        return {"go": True, "reden": "Script mode (Claude circuit open)", "tokens": 0, "script_mode": True}

    prompt = f"""Je bent een crypto trading risk manager. Beoordeel deze potentile trade:

Coin: {symbol}
Score: {score}/100
Setup: {setup_type or 'onbekend'}
Regime: {regime}
Entry: {entry:.4f}
Stop: {stop:.4f}
Target: {target:.4f}
R-ratio: {risk_r:.1f}R

Extra context: {json.dumps(extra, ensure_ascii=False) if extra else 'geen'}

Factoren die zwaar wegen:
- R-ratio < 1.5 = NO-GO
- Regime BEAR = extra voorzichtig
- Fear & Greed < 30 = voorzichtiger, < 20 = NO-GO tenzij score >90
- Fear & Greed > 80 = FOMO-gevaar, wees strenger

Geef een GO of NO-GO met maximaal 2 zinnen uitleg.
Antwoord ALLEEN in dit JSON formaat (geen markdown):
{{"go": true/false, "reden": "jouw uitleg hier"}}"""

    t0 = time.time()
    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        duration = round(time.time() - t0, 2)
        tokens   = resp.usage.input_tokens + resp.usage.output_tokens
        raw      = resp.content[0].text.strip()

        # Parse JSON
        raw_clean = raw.replace("```json", "").replace("```", "").strip()
        result    = json.loads(raw_clean)
        go        = bool(result.get("go", True))
        reden     = str(result.get("reden", ""))

        _circuit_success()
        log(f"{' GO' if go else ' NO-GO'}: {symbol}  {reden[:80]}")

        conn = db_connect()
        _log_agent(conn, "GO" if go else "NO-GO", inp,
                   {"go": go, "reden": reden}, tokens, duration)
        conn.close()

        return {"go": go, "reden": reden, "tokens": tokens, "script_mode": False}

    except Exception as e:
        duration = round(time.time() - t0, 2)
        _circuit_fail()
        log(f" Claude fout: {e}")
        # Fallback: als Claude faalt, laat trade door (bot mag niet stoppen)
        try:
            conn = db_connect()
            _log_agent(conn, "ERROR", inp, {}, 0, duration, str(e))
            conn.close()
        except: pass
        return {"go": True, "reden": f"Claude fout (auto-GO): {str(e)[:60]}", "tokens": 0, "script_mode": True}


if __name__ == "__main__":
    # Test
    result = beoordeel_trade(
        symbol="BTCUSDT", score=85, regime="BULL",
        entry=65000, stop=63000, target=70000, setup_type="BREAKOUT"
    )
    print("Resultaat:", result)
