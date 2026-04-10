# trading/shadow_trades.py
# ============================================================
# Crypto AI Bot — Shadow Trades v2.0
# ============================================================
#
# WAT DIT BESTAND DOET:
# ─────────────────────────────────────────────────────────────
# Shadow trades zijn meekijkende trades die parallel aan echte
# live trades worden uitgevoerd — maar zonder echt geld.
#
# Het doel van shadow trades is drieledig:
#
# 1. LEREN ZONDER RISICO
#    Shadow trades volgen elk signaal dat de scanner genereert,
#    ook als de bot geen echt geld mag gebruiken (bijv. daglimiet
#    bereikt, bot gepauzeerd, BTC in BEAR regime). Zo leer je
#    wat er zou zijn gebeurd als je WEL zou hebben gehandeld.
#
# 2. VALIDATIE VAN DE STRATEGIE
#    Door shadow win rate te vergelijken met live win rate kun je
#    edge decay detecteren. Als shadow veel beter presteert dan
#    live, zijn er problemen met de live uitvoering (fees, slippage,
#    timing, Bitvavo universe mismatch).
#    → compare_shadow_vs_live() doet deze vergelijking
#    → trade_monitor.py stuurt WhatsApp als verschil > 10%
#
# 3. EXPERIENCE OPBOUW
#    Shadow trades worden gelogd naar experience_trades met
#    source=SHADOW. Ze dragen bij aan het experience_scoreboard
#    dat door multi_coin_score.py wordt gebruikt om betere
#    signalen te genereren. Meer data = betere scores.
#
# HOE HET SYSTEEM WERKT:
# ─────────────────────────────────────────────────────────────
# STAP 1 — Signaal ontvangen:
#   multi_coin_score.py genereert een Pre-BUY signaal
#   → schrijft naar public.pending_approvals
#   → triggert /auto_buy op whatsapp_webhook.py
#
# STAP 2 — Shadow trade openen:
#   whatsapp_webhook.py /auto_buy roept open_shadow_trade() aan
#   → shadow positie aangemaakt in shadow_trades.json
#   → geen DB entry nodig bij openen (wordt bij sluiting gelogd)
#   → GEEN limieten: shadow trades altijd open, ook bij BEAR BTC
#
# STAP 3 — Shadow trade bewaken:
#   trade_monitor.py roept evaluate_shadow_for_symbol() aan
#   → controleert dezelfde exit condities als live trades:
#      - Stop loss geraakt?
#      - Target geraakt?
#      - Max houdtijd (48u) bereikt?
#      - Structuur gebroken (had >1R, nu <0.5R)?
#   → update_shadow_tracking() wordt aangeroepen voor MFE/MAE
#
# STAP 4 — Shadow trade sluiten:
#   close_shadow_trade() wordt aangeroepen met exit prijs
#   → PnL berekening inclusief gesimuleerde fees + slippage
#   → R-multiple, MFE, MAE worden berekend
#   → log_shadow_to_db() logt naar experience_trades (source=SHADOW)
#   → update_experience_scoreboard() werkt scoreboard bij
#   → positie verwijderd uit shadow_trades.json
#
# STAP 5 — Analyse:
#   compare_shadow_vs_live() vergelijkt performance elke 20 trades
#   get_shadow_win_rate_by_setup() geeft breakdown per setup type
#   get_shadow_rolling_stats() geeft statistieken voor rapporten
#
# SAMENWERKING MET ANDERE BESTANDEN:
# ─────────────────────────────────────────────────────────────
# ← Aangeroepen door: whatsapp_webhook.py (open_shadow_trade)
# ← Aangeroepen door: trade_monitor.py (evaluate_shadow_for_symbol,
#                                        update_shadow_tracking,
#                                        close_shadow_trade)
# ← Aangeroepen door: app.py (get_all_shadow_positions voor dashboard)
# → Schrijft naar:    /data/shadow_trades.json (state bestand)
# → Schrijft naar:    public.experience_trades (source=SHADOW)
# → Schrijft naar:    public.experience_scoreboard (win rates)
# ← Leest van:        public.pending_approvals (voor sync check)
# ← Leest van:        public.btc_regime_4h (voor context logging)
#
# SHADOW vs LIVE — BELANGRIJKE VERSCHILLEN:
# ─────────────────────────────────────────────────────────────
# SHADOW:
#   - Geen echt geld — geen BUY/SELL order op Bitvavo
#   - Geen daglimiet (10 trades/dag)
#   - Geen max open trades limiet (5 tegelijk)
#   - Geen trading hours restrictie (24/7)
#   - Geen coin cooldown check (leert ook van slechte coins)
#   - Fees en slippage worden WEL gesimuleerd voor realisme
#   - source=SHADOW in experience_trades
#   - is_shadow=TRUE in experience_trades
#
# LIVE:
#   - Echt geld op Bitvavo
#   - MAX_REAL_TRADES_PER_DAY = 10
#   - MAX_OPEN_REAL_TRADES = 5
#   - TRADING_HOURS_START = 8, TRADING_HOURS_END = 22
#   - Coin cooldown 24u na verlies
#   - source=LIVE in experience_trades
#   - is_shadow=FALSE
#
# STATE BESTAND — shadow_trades.json:
# ─────────────────────────────────────────────────────────────
# {
#   "positions": {
#     "ETHUSDT": {
#       "symbol": "ETHUSDT",
#       "entry": 2450.00,
#       "stop_loss": 2400.00,
#       "target": 2550.00,
#       "qty": 0.000204,
#       "amount_eur": 0.50,
#       "opened_at": 1234567890,    # Unix timestamp
#       "setup_type": "TREND_PULLBACK",
#       "regime": "BULL",
#       "score": 87,
#       "had_over_1r": false,       # Was prijs ooit >1R?
#       "max_price_seen": 2460.00,  # Voor MFE berekening
#       "min_price_seen": 2440.00,  # Voor MAE berekening
#       "mfe_r": 0.2,               # Maximum Favorable Excursion
#       "mae_r": 0.2,               # Maximum Adverse Excursion
#     }
#   },
#   "open_trades": [{"symbol": "ETHUSDT", "opened_at": 1234567890}],
#   "stats": {
#     "total_opened": 100,
#     "total_closed": 95,
#     "total_wins": 52,
#     "total_losses": 43,
#     "total_pnl": 1.23
#   }
# }
#
# IDENTIEK AAN ALLE ANDERE BESTANDEN:
# ─────────────────────────────────────────────────────────────
# ✅ sslmode="require" op DB connectie
# ✅ Zelfde send_whatsapp() implementatie
# ✅ Zelfde _claude_analyse() implementatie
# ✅ Zelfde safe_int / safe_float / safe_str helpers
# ✅ Zelfde now_utc() patroon
# ✅ Zelfde DB logging naar experience_trades
# ✅ Zelfde fee + slippage berekening
# ✅ Zelfde exit logica als trade_monitor.py
# ✅ Zelfde MAX_HOLD_HOURS (48u)
#
# BUGS GEFIXED vs origineel:
# ─────────────────────────────────────────────────────────────
# ✅ get_all_shadow_positions was twee keer gedefinieerd
# ✅ evaluate_shadow_for_symbol was twee keer gedefinieerd
# ✅ Atomisch opslaan van state via .tmp file
# ✅ MFE/MAE worden correct bijgehouden
# ✅ had_over_1r tracking correct
# ✅ Scoreboard bijdrage na elke shadow trade
# ============================================================

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
import requests


# ============================================================
# ENVIRONMENT VARIABELEN
# ─────────────────────────────────────────────────────────────
# Alle configuratie via Render Environment Variables.
# Nooit hardcoded waarden in de code.
# Identiek aan alle andere bestanden in het systeem.
# ============================================================
DATABASE_URL      = (os.getenv("DATABASE_URL") or "").strip()
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()

TWILIO_ACCOUNT_SID   = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN    = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
TWILIO_WHATSAPP_TO   = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()

# ─────────────────────────────────────────────────────────────
# FEE + SLIPPAGE SIMULATIE
# ─────────────────────────────────────────────────────────────
# Shadow trades simuleren fees en slippage voor realisme.
# Zonder fee simulatie zou shadow altijd beter presteren dan live.
# Bitvavo fee: 0.25% per transactie (koper én verkoper)
# Slippage:    0.1% voor marktorder (verschil bid/ask spread)
# Totaal:      0.35% per trade (heen + terug = 0.70%)
# ─────────────────────────────────────────────────────────────
BITVAVO_FEE_PCT = float(os.getenv("BITVAVO_FEE_PCT", "0.0025"))
SLIPPAGE_PCT    = float(os.getenv("SLIPPAGE_PCT", "0.001"))
TOTAL_COST_PCT  = BITVAVO_FEE_PCT + SLIPPAGE_PCT  # 0.35%

# ─────────────────────────────────────────────────────────────
# TRADING PARAMETERS
# ─────────────────────────────────────────────────────────────
# MAX_HOLD_HOURS: Maximale houdtijd per shadow trade.
# Identiek aan live trades in trade_monitor.py.
# Na 48u wordt shadow trade automatisch gesloten als LOSS.
# ─────────────────────────────────────────────────────────────
MAX_HOLD_HOURS   = float(os.getenv("MAX_HOLD_HOURS", "48.0"))
ATR_MULTIPLIER   = float(os.getenv("ATR_MULTIPLIER", "2.0"))
ATR_TARGET_R     = float(os.getenv("ATR_TARGET_R", "2.0"))

# ─────────────────────────────────────────────────────────────
# STANDAARD POSITIE GROOTTE
# ─────────────────────────────────────────────────────────────
# Identiek aan MAX_PER_TRADE_EUR in alle andere bestanden.
# €0.50 per shadow trade voor consistente vergelijking met live.
# ─────────────────────────────────────────────────────────────
DEFAULT_SHADOW_AMOUNT_EUR = float(os.getenv("MAX_PER_TRADE_EUR", "0.50"))

# ─────────────────────────────────────────────────────────────
# STRUCTUUR BREAK THRESHOLD
# ─────────────────────────────────────────────────────────────
# Als shadow trade >1R heeft bereikt maar daarna terugvalt
# tot <0.5R, wordt de trade gesloten (structuur gebroken).
# Identiek aan de logica in trade_monitor.py process_live_trade().
# ─────────────────────────────────────────────────────────────
STRUCTUUR_BREAK_R = float(os.getenv("STRUCTUUR_BREAK_R", "0.5"))


def _get_data_dir() -> str:
    """
    Bepaalt de data directory voor de shadow_trades.json state file.

    Op Render met persistent disk: /data
    Op Render zonder persistent disk: /tmp/data (verloren bij restart)
    Lokaal: /tmp/data

    Configureer DATA_DIR in Render environment variables als je
    een persistent disk hebt gekoppeld aan de service.
    """
    d = (os.getenv("DATA_DIR") or "").strip()
    if d:
        return d
    return "/data" if os.path.isdir("/data") else "/tmp/data"


DATA_DIR          = _get_data_dir()
SHADOW_STATE_PATH = os.path.join(DATA_DIR, "shadow_trades.json")

# Database tabel namen — identiek aan alle andere bestanden
BOT_STATE_TABLE   = "public.bot_state"


# ============================================================
# BASIS HELPERS
# ─────────────────────────────────────────────────────────────
# Identiek aan alle andere bestanden in het systeem.
# Elke helper is simpel en crasht nooit.
# ============================================================
def now_utc() -> datetime:
    """
    Geeft de huidige UTC tijd terug.
    Identiek aan now_utc() in alle andere bestanden.
    UTC wordt overal gebruikt — nooit lokale tijd.
    """
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    """
    Logt een bericht met timestamp naar stdout.
    Render vangt stdout op en toont het in de service logs.
    [SHADOW] tag maakt het makkelijk te filteren.
    """
    print(f"[{now_utc().strftime('%Y-%m-%d %H:%M:%S')}] [SHADOW] {msg}", flush=True)


def safe_float(x: Any, default: float = 0.0) -> float:
    """
    Veilige float conversie.
    Geeft default terug bij fout, None of lege string.
    Crasht nooit — heel belangrijk voor productie code.
    """
    try:
        return float(x)
    except Exception:
        return default


def safe_int(x: Any, default: int = 0) -> int:
    """
    Veilige integer conversie.
    Geeft default terug bij fout of None.
    """
    try:
        return int(x)
    except Exception:
        return default


def safe_str(x: Any, default: str = "") -> str:
    """
    Veilige string conversie.
    Geeft default terug bij None of lege string na strip().
    """
    if x is None:
        return default
    try:
        s = str(x).strip()
        return s if s else default
    except Exception:
        return default


def _ensure_dir(path: str) -> None:
    """
    Maakt de directory aan voor een bestandspad als die niet bestaat.
    Veilig om meerdere keren aan te roepen.
    """
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


# ============================================================
# WHATSAPP COMMUNICATIE
# ─────────────────────────────────────────────────────────────
# Identieke implementatie als alle andere bestanden.
# Alleen voor KRITIEKE fouten in shadow trades — geen spam.
# Shadow trades zijn leerdata, niet kritieke productie code.
#
# Wanneer WhatsApp WORDT gestuurd vanuit shadow_trades.py:
# - Kritieke DB fout (data gaat verloren)
# - Edge decay gedetecteerd (shadow >> live)
# - State file corruptie
#
# Wanneer WhatsApp NIET wordt gestuurd:
# - Normale open/sluit events (te veel spam)
# - Individuele WIN/LOSS (niet relevant voor gebruiker)
# - Technische details die alleen voor ontwikkelaar zijn
# ============================================================
def send_whatsapp(message: str) -> bool:
    """
    Stuurt WhatsApp bericht via Twilio API.

    Identieke implementatie als in:
    - whatsapp_webhook.py
    - live_trader.py
    - trade_monitor.py
    - multi_coin_score.py
    - build_btc_regime.py
    - history_fetcher.py
    - regime_labeler.py

    Geeft True terug bij succes, False bij fout.
    """
    if not all([
        TWILIO_ACCOUNT_SID,
        TWILIO_AUTH_TOKEN,
        TWILIO_WHATSAPP_FROM,
        TWILIO_WHATSAPP_TO,
    ]):
        log(f"WhatsApp (geen Twilio config): {message[:80]}")
        return False

    try:
        resp = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts"
            f"/{TWILIO_ACCOUNT_SID}/Messages.json",
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={
                "From": TWILIO_WHATSAPP_FROM,
                "To":   TWILIO_WHATSAPP_TO,
                "Body": message,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            log(f"✅ WhatsApp verzonden ({len(message)} tekens)")
            return True
        log(f"❌ WhatsApp fout {resp.status_code}: {resp.text[:200]}")
        return False
    except requests.exceptions.Timeout:
        log("❌ WhatsApp timeout na 15 seconden")
        return False
    except Exception as e:
        log(f"❌ WhatsApp exception: {type(e).__name__}: {e}")
        return False


# ============================================================
# CLAUDE AI MONITORING
# ─────────────────────────────────────────────────────────────
# Identieke implementatie als alle andere bestanden.
# Claude wordt gebruikt voor:
# - Analyse van shadow vs live performance (edge decay)
# - Uitleg bij kritieke fouten
# - Interpretatie van shadow statistieken
# ============================================================
def _claude_analyse(prompt: str, max_tokens: int = 250) -> str:
    """
    Roept Claude API aan voor analyse.

    Identiek aan _claude_analyse() in alle andere bestanden.
    Model: claude-sonnet-4-20250514 (altijd Sonnet 4)
    Timeout: 25 seconden
    Geeft lege string bij fout — shadow trades gaan altijd door.
    """
    if not ANTHROPIC_API_KEY:
        return ""
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": max_tokens,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=25,
        )
        if resp.status_code == 200:
            content = resp.json().get("content", [])
            if content:
                return content[0]["text"].strip()
        log(f"⚠️ Claude API status {resp.status_code}")
        return ""
    except requests.exceptions.Timeout:
        log("⚠️ Claude API timeout")
        return ""
    except Exception as e:
        log(f"⚠️ Claude API fout: {type(e).__name__}: {e}")
        return ""


def claude_analyseer_shadow_vs_live(
    shadow_wr: float,
    live_wr:   float,
    diff:      float,
    n_shadow:  int,
    n_live:    int,
) -> str:
    """
    Claude analyseert het verschil tussen shadow en live win rate.
    Wordt aangeroepen als het verschil > 10% is (edge decay).

    De analyse helpt de gebruiker begrijpen wat er mis is
    en welke actie ondernomen moet worden.
    """
    prompt = f"""
Je bent een crypto trading bot edge decay specialist.
Analyseer dit verschil in 3 zinnen Nederlands.

SHADOW vs LIVE VERGELIJKING (30 dagen):
- Shadow win rate: {shadow_wr:.1f}% ({n_shadow} trades)
- Live win rate:   {live_wr:.1f}% ({n_live} trades)
- Verschil:        {diff:.1f}% (grens: 10%)

Shadow trades hebben geen limieten, fees worden gesimuleerd.
Live trades hebben daglimiet, max open trades, trading hours.

1. Wat verklaart het grote verschil?
2. Welke aanpassing is het meest effectief?
3. Is het zorgwekkend of normaal?
""".strip()

    return _claude_analyse(prompt, max_tokens=200)


def claude_analyseer_shadow_performance(
    wins:      int,
    losses:    int,
    pf:        float,
    top_setup: str,
    bot_setup: str,
) -> str:
    """
    Claude analyseert de algehele shadow performance.
    Wordt aangeroepen in het wekelijks rapport via whatsapp_webhook.py.
    """
    prompt = f"""
Je bent een crypto trading coach.
Analyseer de shadow trade performance in 3 zinnen Nederlands.

SHADOW STATISTIEKEN:
- Wins:           {wins}
- Losses:         {losses}
- Win rate:       {wins/(wins+losses)*100:.1f if (wins+losses) > 0 else 0.0}% (als data beschikbaar)
- Profit Factor:  {pf:.2f}
- Beste setup:    {top_setup}
- Slechtste setup: {bot_setup}

1. Wat vertelt de shadow data over de strategie?
2. Welke setup verdient meer aandacht?
3. Aanbeveling voor volgende week?
""".strip()

    return _claude_analyse(prompt, max_tokens=180)


# ============================================================
# DATABASE VERBINDING
# ─────────────────────────────────────────────────────────────
# sslmode="require" is identiek aan alle andere bestanden.
# Render PostgreSQL staat geen onbeveiligde verbindingen toe.
# ============================================================
def db_connect():
    """
    Maakt een nieuwe PostgreSQL verbinding aan.

    sslmode="require" is verplicht voor Render.
    Identiek aan db_connect() in alle andere bestanden.

    Geeft None terug als DATABASE_URL ontbreekt of verbinding mislukt.
    Callers moeten altijd controleren of de verbinding None is.
    """
    if not DATABASE_URL:
        log("⚠️ DATABASE_URL ontbreekt — geen DB verbinding")
        return None
    try:
        return psycopg2.connect(DATABASE_URL, sslmode="require")
    except Exception as e:
        log(f"⚠️ DB connectie fout: {type(e).__name__}: {e}")
        return None


# ============================================================
# STATE FILE MANAGEMENT
# ─────────────────────────────────────────────────────────────
# shadow_trades.json bevat alle open shadow posities en
# globale statistieken.
#
# Het bestand wordt atomisch opgeslagen via een .tmp bestand.
# Dit voorkomt corruptie als de service crasht tijdens schrijven.
#
# Structuur van het bestand:
# {
#   "positions": {SYMBOL: {positie data}},
#   "open_trades": [{symbol, opened_at}],  # snelle lookup
#   "stats": {totale statistieken}
# }
#
# WAARSCHUWING: Als het bestand corrupt is of ontbreekt,
# wordt een leeg state object terugegeven. Open trades
# gaan dan verloren maar de DB logging is al gedaan.
# ============================================================
def load_shadow_state() -> Dict[str, Any]:
    """
    Laadt de shadow trade state uit shadow_trades.json.

    Wordt aangeroepen door:
    - open_shadow_trade() — voor het toevoegen van een positie
    - close_shadow_trade() — voor het ophalen en verwijderen
    - update_shadow_tracking() — voor het bijwerken van MFE/MAE
    - get_all_shadow_positions() — voor dashboard weergave
    - trade_monitor.py — voor bewaking van open trades

    Bij fout of ontbrekend bestand: geeft leeg state object terug.
    Bij corruptie: logt een waarschuwing en geeft leeg object terug.
    """
    _ensure_dir(SHADOW_STATE_PATH)

    if not os.path.exists(SHADOW_STATE_PATH):
        log("ℹ️ shadow_trades.json bestaat nog niet — nieuw state aangemaakt")
        return _empty_state()

    try:
        with open(SHADOW_STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except json.JSONDecodeError as e:
        log(f"⚠️ shadow_trades.json corruptie: {e} — nieuw state aangemaakt")
        return _empty_state()
    except Exception as e:
        log(f"⚠️ shadow_trades.json leesfouten: {e}")
        return _empty_state()

    # Zorg dat alle vereiste keys aanwezig zijn
    s.setdefault("positions", {})
    s.setdefault("open_trades", [])
    s.setdefault("stats", _empty_stats())

    # Zorg dat stats alle keys heeft
    stats = s["stats"]
    for key, default in _empty_stats().items():
        stats.setdefault(key, default)

    return s


def _empty_state() -> Dict[str, Any]:
    """
    Geeft een leeg state object terug.
    Gebruikt bij initialisatie of herstel van corruptie.
    """
    return {
        "positions":   {},
        "open_trades": [],
        "stats":       _empty_stats(),
    }


def _empty_stats() -> Dict[str, Any]:
    """
    Geeft lege statistieken terug.
    Elke sleutel heeft een zinvolle default waarde.
    """
    return {
        "total_opened":  0,
        "total_closed":  0,
        "total_wins":    0,
        "total_losses":  0,
        "total_pnl":     0.0,
        "last_updated":  "",
    }


def save_shadow_state(state: Dict[str, Any]) -> bool:
    """
    Slaat de shadow state atomisch op naar shadow_trades.json.

    ATOMISCH OPSLAAN:
    1. Schrijf naar shadow_trades.json.tmp
    2. os.replace() vervangt het origineel atomisch
    Dit voorkomt dat het bestand half beschreven is als de
    service crasht tijdens het opslaan.

    Geeft True terug bij succes, False bij fout.
    """
    _ensure_dir(SHADOW_STATE_PATH)
    tmp_path = SHADOW_STATE_PATH + ".tmp"

    # Update timestamp
    state.setdefault("stats", _empty_stats())
    state["stats"]["last_updated"] = now_utc().isoformat()

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp_path, SHADOW_STATE_PATH)
        return True
    except Exception as e:
        log(f"⚠️ State opslaan fout: {type(e).__name__}: {e}")
        # Verwijder .tmp als het bestaat
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


# ============================================================
# DATABASE LOGGING
# ─────────────────────────────────────────────────────────────
# Alle shadow trades worden gelogd naar experience_trades.
# Dit maakt het mogelijk om shadow data te analyseren via SQL
# en te gebruiken voor het experience_scoreboard.
#
# DB SCHEMA van experience_trades:
# - trade_key:    unieke sleutel (SHADOW|SYMBOL|PREBUY_ID)
# - source:       'SHADOW'
# - is_shadow:    TRUE
# - coin:         symbol (bijv. ETHUSDT)
# - outcome:      'WIN' of 'LOSS'
# - pnl_eur:      PnL inclusief gesimuleerde fees
# - pnl_r:        R-multiple bij exit
# - result_r:     identiek aan pnl_r
# - mfe_r:        Maximum Favorable Excursion in R
# - mae_r:        Maximum Adverse Excursion in R
# - time_minutes: duur van de trade in minuten
# ============================================================
def log_shadow_to_db(
    symbol:     str,
    prebuy_id:  str,
    outcome:    str,
    entry:      float,
    exit_price: float,
    pnl_eur:    float,
    setup_type: str,
    regime:     str,
    score:      int,
    hold_min:   float = 0.0,
    exit_reden: str   = "",
    mfe_r:      float = 0.0,
    mae_r:      float = 0.0,
    exit_r:     float = 0.0,
) -> None:
    """
    Logt een gesloten shadow trade naar experience_trades.

    Parameters:
    - symbol:     coin symbol (bijv. ETHUSDT)
    - prebuy_id:  ID van het Pre-BUY signal dat de trade startte
    - outcome:    'WIN' of 'LOSS'
    - entry:      entry prijs
    - exit_price: exit prijs
    - pnl_eur:    PnL inclusief fees en slippage
    - setup_type: type setup (TREND_PULLBACK, BREAKOUT, etc.)
    - regime:     marktregime (BULL, BEAR, RANGE)
    - score:      score van het signaal (0-100)
    - hold_min:   duur in minuten
    - exit_reden: reden van exit (STOP_LOSS, TARGET_HIT, etc.)
    - mfe_r:      Maximum Favorable Excursion in R-multiples
    - mae_r:      Maximum Adverse Excursion in R-multiples
    - exit_r:     R-multiple bij exit (negatief = verlies)

    Samenwerking:
    - multi_coin_score.py leest deze data voor experience weight
    - app.py toont deze data in het scoreboard dashboard
    - whatsapp_webhook.py gebruikt deze data voor rapporten
    """
    conn = db_connect()
    if not conn:
        log(f"⚠️ Geen DB — shadow trade niet gelogd: {symbol} {outcome}")
        return

    trade_key = f"SHADOW|{symbol}|{prebuy_id or int(time.time())}"

    try:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO public.experience_trades (
                trade_key,
                source,
                is_shadow,
                coin,
                timestamp,
                entry_time,
                exit_time,
                setup_type,
                market_regime,
                entry,
                outcome,
                pnl_eur,
                pnl_r,
                result_r,
                bot_confidence,
                mfe_r,
                mae_r,
                time_minutes,
                created_at,
                updated_at
            )
            VALUES (
                %s,
                'SHADOW',
                TRUE,
                %s,
                NOW(),
                NOW(),
                NOW(),
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                NOW(),
                NOW()
            )
            ON CONFLICT (trade_key) DO UPDATE SET
                outcome    = EXCLUDED.outcome,
                pnl_eur    = EXCLUDED.pnl_eur,
                pnl_r      = EXCLUDED.pnl_r,
                result_r   = EXCLUDED.result_r,
                exit_time  = NOW(),
                mfe_r      = EXCLUDED.mfe_r,
                mae_r      = EXCLUDED.mae_r,
                time_minutes = EXCLUDED.time_minutes,
                updated_at = NOW()
            """, (
                trade_key,
                symbol,
                setup_type,
                regime,
                entry,
                outcome,
                pnl_eur,
                exit_r,     # pnl_r
                exit_r,     # result_r
                score,      # bot_confidence
                mfe_r,
                mae_r,
                hold_min,
            ))

        conn.commit()
        log(
            f"✅ DB gelogd (shadow): {symbol} {outcome} "
            f"€{pnl_eur:.4f} R={exit_r:.2f} "
            f"MFE={mfe_r:.2f}R MAE={mae_r:.2f}R"
        )

    except Exception as e:
        log(f"⚠️ DB log fout ({symbol}): {type(e).__name__}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass

    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# EXPERIENCE SCOREBOARD BIJWERKEN
# ─────────────────────────────────────────────────────────────
# Na elke gesloten shadow trade wordt het experience_scoreboard
# bijgewerkt. Dit is het lerende systeem van de bot.
#
# Het scoreboard bevat voor elke (coin, setup_type, regime) combo:
# - n:          totaal aantal trades
# - wins:       aantal wins
# - losses:     aantal losses
# - win_rate:   win rate (0.0 - 1.0)
# - avg_pnl:    gemiddelde PnL per trade
# - avg_r:      gemiddeld R-multiple
#
# multi_coin_score.py leest dit scoreboard via get_experience()
# en gebruikt de win rate als gewicht in de score berekening.
# Een coin/setup combo met 60%+ win rate krijgt meer punten.
# Een combo met 30%- win rate krijgt weinig punten.
# ============================================================
def update_experience_scoreboard(
    conn,
    symbol:     str,
    setup_type: str,
    regime:     str,
    outcome:    str,
    pnl_eur:    float,
    exit_r:     float,
) -> None:
    """
    Werkt het experience_scoreboard bij na een gesloten shadow trade.

    Gebruikt gewogen gemiddelden:
    - win_rate  = (oude_wins + nieuwe_win) / (oud_n + 1)
    - avg_pnl   = (oud_avg * oud_n + nieuwe_pnl) / (oud_n + 1)
    - avg_r     = (oud_avg_r * oud_n + nieuwe_r) / (oud_n + 1)

    ON CONFLICT zorgt voor INSERT als combo nieuw is,
    of UPDATE als combo al bestaat.

    Samenwerking:
    - Geschreven door: shadow_trades.py (dit bestand)
    - Gelezen door:    multi_coin_score.py get_experience()
    - Getoond door:    app.py scoreboard pagina
    """
    if not conn:
        log("⚠️ Geen DB verbinding — scoreboard niet bijgewerkt")
        return

    try:
        is_win  = 1 if outcome == "WIN"  else 0
        is_loss = 1 if outcome == "LOSS" else 0

        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO public.experience_scoreboard
                (symbol, setup_type, regime, n, wins, losses,
                 win_rate, avg_pnl_eur, avg_r, updated_at)
            VALUES (
                %s, %s, %s,
                1, %s, %s,
                %s::float,
                %s, %s,
                NOW()
            )
            ON CONFLICT (symbol, setup_type, regime)
            DO UPDATE SET
                n           = experience_scoreboard.n + 1,
                wins        = experience_scoreboard.wins        + %s,
                losses      = experience_scoreboard.losses      + %s,
                win_rate    = (experience_scoreboard.wins + %s)::float
                              / (experience_scoreboard.n + 1),
                avg_pnl_eur = (experience_scoreboard.avg_pnl_eur
                               * experience_scoreboard.n + %s)
                              / (experience_scoreboard.n + 1),
                avg_r       = (experience_scoreboard.avg_r
                               * experience_scoreboard.n + %s)
                              / (experience_scoreboard.n + 1),
                updated_at  = NOW()
            """, (
                # INSERT params
                symbol, setup_type, regime,
                is_win, is_loss,
                float(is_win),  # win_rate voor INSERT
                pnl_eur, exit_r,
                # UPDATE params
                is_win, is_loss,
                is_win,         # win_rate numerator voor UPDATE
                pnl_eur, exit_r,
            ))

        conn.commit()
        log(f"✅ Scoreboard bijgewerkt: {symbol}/{setup_type}/{regime} → {outcome}")

    except Exception as e:
        log(f"⚠️ Scoreboard update fout ({symbol}): {type(e).__name__}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


# ============================================================
# EXIT LOGICA
# ─────────────────────────────────────────────────────────────
# De exit logica van shadow trades is identiek aan de exit
# logica in trade_monitor.py. Dit is bewust — als shadow
# trades een andere exit logica hadden, zou de vergelijking
# shadow vs live niet eerlijk zijn.
#
# EXIT CONDITIES (in volgorde van prioriteit):
# 1. MAX_HOLD_TIME: 48u bereikt → LOSS (time-based exit)
# 2. STOP_LOSS:     prijs <= stop of R < 0 → LOSS
# 3. TARGET_HIT:    prijs >= target → WIN
# 4. STRUCTUUR_FAIL: had >1R maar nu <0.5R → LOSS
#    (prijs is teruggevallen na het bereiken van 1R)
# ============================================================
def _calc_r_multiple(entry: float, stop: float, current: float) -> float:
    """
    Berekent het R-multiple voor een trade.

    R-multiple = (huidige prijs - entry) / risico per unit
    waarbij risico per unit = (entry - stop)

    Positief R-multiple = prijs is gestegen t.o.v. entry
    Negatief R-multiple = prijs is gedaald t.o.v. entry
    R = 1.0 = prijs heeft het eerste doel bereikt (1x risico)
    R = 2.0 = prijs heeft het tweede doel bereikt (2x risico)

    Identiek aan _calc_r() in trade_monitor.py.
    """
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    return (current - entry) / risk


def _holding_minutes(trade: Dict[str, Any]) -> float:
    """
    Berekent hoe lang een trade al open staat in minuten.

    Gebruikt opened_at (Unix timestamp) die bij openen wordt
    opgeslagen. Als opened_at ontbreekt, geeft 0 terug.

    Identiek aan _holding_minutes() in trade_monitor.py.
    """
    opened_at = safe_float(trade.get("opened_at"))
    if not opened_at:
        return 0.0
    elapsed_sec = time.time() - opened_at
    return elapsed_sec / 60.0


def evaluate_shadow_exit(
    symbol:        str,
    shadow_trade:  Dict[str, Any],
    current_price: float,
) -> Tuple[bool, str]:
    """
    Controleert alle exit condities voor een shadow trade.

    Identieke logica als process_live_trade() in trade_monitor.py.
    Dit is bewust — shadow en live moeten vergelijkbaar zijn.

    Parameters:
    - symbol:        coin symbol (voor logging)
    - shadow_trade:  de positie data uit shadow_trades.json
    - current_price: huidige marktprijs

    Geeft (should_exit, exit_reden) terug:
    - (True, "STOP_LOSS") als stop geraakt is
    - (True, "TARGET_HIT") als target bereikt is
    - (True, "MAX_HOLD_TIME") als 48u verstreken is
    - (True, "STRUCTUUR_FAIL") als structuur gebroken is
    - (False, "") als geen exit conditie geldt

    BELANGRIJK: Deze functie past de state NIET aan.
    Dat gebeurt in close_shadow_trade().
    """
    # Validatie — geen exit als prijs ongeldig is
    if current_price is None or current_price <= 0:
        return False, ""

    entry    = safe_float(shadow_trade.get("entry"))
    stop     = safe_float(shadow_trade.get("stop_loss") or shadow_trade.get("stop"))
    target   = safe_float(shadow_trade.get("target"))
    hold_min = _holding_minutes(shadow_trade)
    r        = _calc_r_multiple(entry, stop, current_price)

    # ── EXIT 1: Max houdtijd (48 uur) ─────────────────────
    # Na 48u gaat de trade automatisch dicht.
    # Reden: marktomstandigheden zijn dan mogelijk sterk veranderd.
    # De trade was dan geen goede setup meer.
    if hold_min >= MAX_HOLD_HOURS * 60:
        log(
            f"⏰ {symbol} shadow: max houdtijd ({hold_min:.0f} min) → exit"
        )
        return True, "MAX_HOLD_TIME"

    # ── EXIT 2: Stop loss ─────────────────────────────────
    # Prijs is gedaald tot of onder de stop loss.
    # Of R is negatief (prijs onder entry minus risico).
    if current_price <= stop or r < 0:
        log(
            f"🛑 {symbol} shadow: stop geraakt "
            f"({current_price:.6f} ≤ {stop:.6f}, R={r:.2f}) → exit"
        )
        return True, "STOP_LOSS"

    # ── EXIT 3: Target bereikt ────────────────────────────
    # Prijs heeft het target niveau bereikt.
    # In shadow trades is dit een directe WIN exit.
    # In live trades wordt er soms een trailing stop gebruikt.
    if target > 0 and current_price >= target:
        log(
            f"🎯 {symbol} shadow: target bereikt "
            f"({current_price:.6f} ≥ {target:.6f}) → exit"
        )
        return True, "TARGET_HIT"

    # ── EXIT 4: Structuur gebroken ────────────────────────
    # Trade had op enig moment >1R bereikt (had_over_1r=True)
    # maar is daarna teruggevallen tot <0.5R (STRUCTUUR_BREAK_R).
    # Dit betekent dat de trend is omgekeerd na een goed begin.
    if shadow_trade.get("had_over_1r") and r < STRUCTUUR_BREAK_R:
        log(
            f"📉 {symbol} shadow: structuur gebroken "
            f"(R={r:.2f} < {STRUCTUUR_BREAK_R}) → exit"
        )
        return True, "STRUCTUUR_FAIL"

    # Geen exit conditie van toepassing
    return False, ""


# ============================================================
# SHADOW TRADE BEHEER
# ─────────────────────────────────────────────────────────────
# open_shadow_trade() → opent een nieuwe shadow positie
# close_shadow_trade() → sluit een shadow positie
# update_shadow_tracking() → werkt MFE/MAE bij
# ============================================================
def open_shadow_trade(
    symbol:     str,
    entry:      float,
    stop:       float,
    target:     float,
    amount_eur: float = DEFAULT_SHADOW_AMOUNT_EUR,
    meta:       Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Opent een nieuwe shadow trade positie.

    Dit is de enige manier om een shadow trade te starten.
    Wordt aangeroepen door whatsapp_webhook.py /auto_buy route.
    Parallel aan live_trader.buy_eur() — maar zonder BUY order.

    GEEN LIMIETEN voor shadow trades:
    - Geen daglimiet (MAX_REAL_TRADES_PER_DAY)
    - Geen max open trades (MAX_OPEN_REAL_TRADES)
    - Geen trading hours (TRADING_HOURS_START/END)
    - Geen coin cooldown check
    - Geen BTC regime check (ook in BEAR regime shadow traden)
    - Geen EUR balance check

    Fees worden WEL berekend voor de entry:
    - fee_entry = amount_eur * TOTAL_COST_PCT
    - Dit wordt meegenomen bij de PnL berekening bij sluiting

    Parameters:
    - symbol:     coin symbol (bijv. ETHUSDT)
    - entry:      entry prijs van het signaal
    - stop:       stop loss prijs (ATR-based in multi_coin_score.py)
    - target:     target prijs (2x risico standaard)
    - amount_eur: bedrag in euro (standaard €0.50)
    - meta:       extra data van het Pre-BUY signaal

    Geeft True terug bij succes, False als trade al open is.
    """
    meta  = meta or {}
    state = load_shadow_state()

    # Validatie — geen dubbele positie voor zelfde coin
    if symbol in (state.get("positions") or {}):
        log(f"ℹ️ {symbol} al open in shadow — skip")
        return False

    # Validatie — geldige entry prijs vereist
    if entry <= 0:
        log(f"⚠️ Ongeldige entry prijs voor {symbol}: {entry}")
        return False

    # Validatie — stop moet onder entry liggen
    if stop >= entry:
        log(f"⚠️ Stop ({stop:.6f}) >= entry ({entry:.6f}) voor {symbol} — skip")
        return False

    # Bereken qty en fees
    qty       = amount_eur / entry
    fee_entry = round(amount_eur * TOTAL_COST_PCT, 6)

    # Bouw de positie data op
    position = {
        # Identificatie
        "symbol":     symbol,
        "market":     meta.get("market", f"{symbol[:-4]}-EUR"),
        "prebuy_id":  safe_str(meta.get("prebuy_id")),
        "source":     "SHADOW",

        # Prijsdata
        "entry":      entry,
        "stop_loss":  stop,
        "stop":       stop,
        "target":     target,

        # Hoeveelheid
        "qty":           qty,
        "amount_eur":    amount_eur,
        "fee_entry_eur": fee_entry,

        # Timing
        "opened_at":  int(time.time()),
        "opened_utc": now_utc().isoformat(),

        # Signaal data
        "setup_type": safe_str(meta.get("setup_type"), "UNKNOWN"),
        "regime":     safe_str(meta.get("regime"), "UNKNOWN"),
        "score":      safe_int(meta.get("score")),
        "timeframe":  safe_str(meta.get("timeframe"), "4h"),
        "chance":     safe_int(meta.get("chance")),
        "confidence": safe_int(meta.get("confidence")),
        "why_tag":    safe_str(meta.get("why_tag")),

        # Status tracking
        "status":     "OPEN",

        # Exit tracking
        "had_over_1r":       False,   # Was prijs ooit >1R?
        "partial_sold_40":   False,   # (ongebruikt in shadow)
        "below_1r_count":    0,       # (ongebruikt in shadow)

        # MFE/MAE tracking (Maximum Favorable/Adverse Excursion)
        "max_price_seen": entry,      # Hoogste prijs gezien
        "min_price_seen": entry,      # Laagste prijs gezien
        "mfe_r":          0.0,        # Beste punt in R
        "mae_r":          0.0,        # Slechtste punt in R

        # Monitoring
        "last_check": int(time.time()),
    }

    # State bijwerken
    state["positions"][symbol] = position
    state["open_trades"] = [
        t for t in (state.get("open_trades") or [])
        if t.get("symbol") != symbol
    ]
    state["open_trades"].append({
        "symbol":    symbol,
        "opened_at": position["opened_at"],
    })

    # Statistieken bijwerken
    stats = state.setdefault("stats", _empty_stats())
    stats["total_opened"] = stats.get("total_opened", 0) + 1

    # Opslaan
    if save_shadow_state(state):
        log(
            f"✅ Shadow geopend: {symbol} "
            f"entry={entry:.6f} stop={stop:.6f} target={target:.6f} | "
            f"setup={position['setup_type']} regime={position['regime']} "
            f"score={position['score']}"
        )
        return True
    else:
        log(f"❌ Shadow openen mislukt (state save fout): {symbol}")
        return False


def close_shadow_trade(
    symbol:     str,
    exit_price: float,
    exit_reden: str = "UNKNOWN",
) -> Optional[Dict[str, Any]]:
    """
    Sluit een open shadow trade en berekent de uitkomst.

    Stappen:
    1. Positie ophalen uit state
    2. PnL berekenen (inclusief gesimuleerde fees)
    3. R-multiple berekenen
    4. MFE/MAE finaliseren
    5. WIN/LOSS bepalen
    6. DB logging via log_shadow_to_db()
    7. Scoreboard bijwerken via update_experience_scoreboard()
    8. Positie verwijderen uit state
    9. Statistieken bijwerken

    Parameters:
    - symbol:     coin symbol
    - exit_price: prijs waartegen gesloten wordt
    - exit_reden: STOP_LOSS / TARGET_HIT / MAX_HOLD_TIME / STRUCTUUR_FAIL

    Geeft een result dict terug met alle trade details.
    Geeft None terug als symbol niet gevonden is.
    """
    state = load_shadow_state()
    pos   = (state.get("positions") or {}).get(symbol)

    if not pos:
        log(f"⚠️ {symbol} niet gevonden in shadow positions")
        return None

    # Trade data ophalen
    entry      = safe_float(pos.get("entry"))
    stop       = safe_float(pos.get("stop_loss") or pos.get("stop"))
    qty        = safe_float(pos.get("qty"))
    amount_eur = safe_float(pos.get("amount_eur"))
    fee_entry  = safe_float(pos.get("fee_entry_eur"))
    hold_min   = _holding_minutes(pos)

    # ── PnL BEREKENING ────────────────────────────────────
    # Gesimuleerde fees en slippage worden meegenomen.
    # Dit maakt shadow vergelijkbaar met live trades.
    #
    # Formule:
    # fee_exit   = exit_price * qty * TOTAL_COST_PCT
    # gross_pnl  = (exit_price - entry) * qty
    # net_pnl    = gross_pnl - fee_entry - fee_exit
    # ─────────────────────────────────────────────────────
    fee_exit  = round(exit_price * qty * TOTAL_COST_PCT, 6)
    gross_pnl = (exit_price - entry) * qty if entry > 0 and qty > 0 else 0.0
    pnl_eur   = gross_pnl - fee_entry - fee_exit

    # ── R-MULTIPLE BEREKENING ────────────────────────────
    # exit_r = positief bij win, negatief bij loss
    risk   = abs(entry - stop) if stop > 0 else 0.0
    exit_r = ((exit_price - entry) / risk) if risk > 0 else 0.0

    # ── MFE/MAE FINALISEREN ──────────────────────────────
    # Gebruik de bijgehouden max/min of bereken uit pos
    max_seen = safe_float(pos.get("max_price_seen"), entry)
    min_seen = safe_float(pos.get("min_price_seen"), entry)

    if risk > 0:
        mfe_r = (max_seen - entry) / risk
        mae_r = (entry - min_seen) / risk
    else:
        mfe_r = safe_float(pos.get("mfe_r"))
        mae_r = safe_float(pos.get("mae_r"))

    # ── WIN/LOSS BEPALEN ─────────────────────────────────
    # WIN als PnL positief is (na fees), anders LOSS
    outcome = "WIN" if pnl_eur > 0 else "LOSS"

    # ── RESULT OBJECT ────────────────────────────────────
    result = {
        "symbol":      symbol,
        "entry":       entry,
        "exit_price":  exit_price,
        "pnl_eur":     round(pnl_eur, 4),
        "gross_pnl":   round(gross_pnl, 4),
        "fee_entry":   round(fee_entry, 6),
        "fee_exit":    round(fee_exit, 6),
        "outcome":     outcome,
        "exit_r":      round(exit_r, 2),
        "mfe_r":       round(mfe_r, 2),
        "mae_r":       round(mae_r, 2),
        "hold_min":    round(hold_min, 1),
        "exit_reden":  exit_reden,
        "setup_type":  safe_str(pos.get("setup_type")),
        "regime":      safe_str(pos.get("regime")),
        "score":       safe_int(pos.get("score")),
    }

    log(
        f"📊 Shadow gesloten: {symbol} {outcome} "
        f"€{pnl_eur:.4f} R={exit_r:.2f} "
        f"MFE={mfe_r:.2f}R MAE={mae_r:.2f}R "
        f"duur={hold_min:.0f}min via {exit_reden}"
    )

    # ── DB LOGGING ────────────────────────────────────────
    log_shadow_to_db(
        symbol     = symbol,
        prebuy_id  = safe_str(pos.get("prebuy_id")),
        outcome    = outcome,
        entry      = entry,
        exit_price = exit_price,
        pnl_eur    = pnl_eur,
        setup_type = safe_str(pos.get("setup_type")),
        regime     = safe_str(pos.get("regime")),
        score      = safe_int(pos.get("score")),
        hold_min   = hold_min,
        exit_reden = exit_reden,
        mfe_r      = mfe_r,
        mae_r      = mae_r,
        exit_r     = exit_r,
    )

    # ── SCOREBOARD BIJWERKEN ──────────────────────────────
    conn = db_connect()
    if conn:
        try:
            update_experience_scoreboard(
                conn       = conn,
                symbol     = symbol,
                setup_type = safe_str(pos.get("setup_type")),
                regime     = safe_str(pos.get("regime")),
                outcome    = outcome,
                pnl_eur    = pnl_eur,
                exit_r     = exit_r,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ── STATE BIJWERKEN ───────────────────────────────────
    state["positions"].pop(symbol, None)
    state["open_trades"] = [
        t for t in (state.get("open_trades") or [])
        if t.get("symbol") != symbol
    ]

    stats = state.setdefault("stats", _empty_stats())
    stats["total_closed"]  = stats.get("total_closed", 0) + 1
    stats["total_wins"]    = stats.get("total_wins",   0) + (1 if outcome == "WIN"  else 0)
    stats["total_losses"]  = stats.get("total_losses", 0) + (1 if outcome == "LOSS" else 0)
    stats["total_pnl"]     = round(stats.get("total_pnl", 0.0) + pnl_eur, 4)

    save_shadow_state(state)

    return result


def update_shadow_tracking(
    symbol:        str,
    current_price: float,
) -> None:
    """
    Werkt MFE/MAE en had_over_1r bij voor een open shadow trade.

    Wordt aangeroepen door trade_monitor.py bij elke monitor run
    (elke 30 seconden).

    Bijgehouden velden:
    - max_price_seen: hoogste prijs gezien (voor MFE berekening)
    - min_price_seen: laagste prijs gezien (voor MAE berekening)
    - mfe_r: Maximum Favorable Excursion in R-multiples
    - mae_r: Maximum Adverse Excursion in R-multiples
    - had_over_1r: True als prijs ooit > 1R heeft bereikt

    had_over_1r is belangrijk voor de STRUCTUUR_FAIL exit:
    Als prijs >1R bereikt maar dan terugvalt tot <0.5R,
    wordt de trade gesloten via STRUCTUUR_FAIL.
    """
    if current_price is None or current_price <= 0:
        return

    state = load_shadow_state()
    pos   = (state.get("positions") or {}).get(symbol)

    if not pos:
        return

    entry = safe_float(pos.get("entry"))
    stop  = safe_float(pos.get("stop_loss") or pos.get("stop"))
    risk  = abs(entry - stop) if stop > 0 else 0.0
    r     = _calc_r_multiple(entry, stop, current_price)

    changed = False

    # ── Update max_price_seen en MFE ──────────────────────
    current_max = safe_float(pos.get("max_price_seen"), current_price)
    if current_price > current_max:
        pos["max_price_seen"] = current_price
        if risk > 0:
            pos["mfe_r"] = round((current_price - entry) / risk, 4)
        changed = True

    # ── Update min_price_seen en MAE ──────────────────────
    current_min = safe_float(pos.get("min_price_seen"), current_price)
    if current_price < current_min:
        pos["min_price_seen"] = current_price
        if risk > 0:
            new_mae = round((entry - current_price) / risk, 4)
            pos["mae_r"] = max(pos.get("mae_r", 0.0), new_mae)
        changed = True

    # ── Track had_over_1r ─────────────────────────────────
    if r >= 1.0 and not pos.get("had_over_1r"):
        pos["had_over_1r"] = True
        log(f"📈 {symbol} shadow: eerste keer >1R bereikt (R={r:.2f})")
        changed = True

    # ── Update last_check ─────────────────────────────────
    pos["last_check"] = int(time.time())
    changed = True

    if changed:
        state["positions"][symbol] = pos
        save_shadow_state(state)


# ============================================================
# PUBLIEKE HELPER FUNCTIES
# ─────────────────────────────────────────────────────────────
# Deze functies worden aangeroepen vanuit andere bestanden.
# Ze bieden een veilige interface naar de shadow state.
# ============================================================
def get_open_shadow_symbols() -> List[str]:
    """
    Geeft een lijst van alle open shadow trade symbolen.

    Aangeroepen door trade_monitor.py om te weten welke
    symbols bewaakt moeten worden.
    """
    state = load_shadow_state()
    return list((state.get("positions") or {}).keys())


def get_shadow_position(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Haalt de positie data op voor één specifiek shadow symbol.

    Geeft None terug als het symbol niet open staat.
    Aangeroepen door trade_monitor.py voor bewaking.
    """
    state = load_shadow_state()
    return (state.get("positions") or {}).get(symbol)


def get_all_shadow_positions() -> List[Dict[str, Any]]:
    """
    Geeft alle open shadow posities terug als een lijst.

    Elke positie bevat het symbol als 'symbol' key plus
    alle positie data (entry, stop, target, etc.)

    Aangeroepen door:
    - trade_monitor.py — voor bewaking van alle posities
    - app.py — voor weergave in het dashboard

    FIX: Was twee keer gedefinieerd in het origineel.
    Nu één correcte definitie die beide use cases dekt.
    """
    state     = load_shadow_state()
    positions = state.get("positions") or {}
    return [{"symbol": k, **v} for k, v in positions.items()]


def has_open_shadow(symbol: str) -> bool:
    """
    Controleert of er al een open shadow trade is voor dit symbol.

    Gebruik dit voor duplicate check voordat je open_shadow_trade()
    aanroept. open_shadow_trade() doet dit ook intern, maar
    deze check is sneller als je alleen wil weten of het open staat.
    """
    state = load_shadow_state()
    return symbol in (state.get("positions") or {})


def get_shadow_stats() -> Dict[str, Any]:
    """
    Geeft alle shadow statistieken terug uit de state file.

    Bevat:
    - open_count:    aantal open shadow trades
    - open_symbols:  lijst van open symbol namen
    - total_opened:  totaal geopend ooit
    - total_closed:  totaal gesloten ooit
    - total_wins:    totaal wins
    - total_losses:  totaal losses
    - win_rate_pct:  win rate in procenten
    - total_pnl:     gesimuleerde totale PnL (inclusief fees)
    - total_amount:  totaal geïnvesteerd in open trades

    Aangeroepen door whatsapp_webhook.py voor dagrapport.
    """
    state    = load_shadow_state()
    positions = state.get("positions") or {}
    stats    = state.get("stats") or _empty_stats()

    total_closed = safe_int(stats.get("total_closed"))
    wins         = safe_int(stats.get("total_wins"))
    losses       = safe_int(stats.get("total_losses"))
    win_rate     = (wins / total_closed * 100) if total_closed > 0 else 0.0

    return {
        "open_count":    len(positions),
        "open_symbols":  list(positions.keys()),
        "total_opened":  safe_int(stats.get("total_opened")),
        "total_closed":  total_closed,
        "total_wins":    wins,
        "total_losses":  losses,
        "win_rate_pct":  round(win_rate, 1),
        "total_pnl":     round(safe_float(stats.get("total_pnl")), 4),
        "total_amount":  sum(
            safe_float(p.get("amount_eur")) for p in positions.values()
        ),
    }


def get_shadow_summary() -> str:
    """
    Geeft een korte tekstuele samenvatting van shadow trades.

    Formaat: klaar voor WhatsApp bericht of dashboard weergave.
    Aangeroepen door whatsapp_webhook.py voor STATUS command.
    """
    stats = get_shadow_stats()
    lines = [
        f"🎭 SHADOW TRADES",
        f"Open:      {stats['open_count']}",
        f"Gesloten:  {stats['total_closed']} "
        f"({stats['total_wins']}W/{stats['total_losses']}L)",
        f"Win rate:  {stats['win_rate_pct']:.1f}%",
        f"Sim. PnL:  €{stats['total_pnl']:.2f}",
    ]
    if stats["open_symbols"]:
        lines.append(f"Open: {', '.join(stats['open_symbols'][:5])}")
    return "\n".join(lines)


# ============================================================
# DATABASE ANALYTISCHE FUNCTIES
# ─────────────────────────────────────────────────────────────
# Deze functies lezen shadow data uit de PostgreSQL database
# voor diepgaande analyse en rapportage.
# ============================================================
def get_shadow_win_rate_by_setup(conn) -> List[Dict[str, Any]]:
    """
    Berekent win rate per setup type voor shadow trades.

    Identiek aan de scoreboard query maar gefilterd op source=SHADOW.
    Toont hoe goed elke setup type presteert in shadow trades.

    Samenwerking:
    - Aangeroepen door app.py voor shadow analyse pagina
    - Aanvullend op experience_scoreboard (dat alle sources combineert)

    Geeft minimaal 3 trades vereist per combo (HAVING COUNT >= 3).
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
            SELECT
                COALESCE(setup_type, 'UNKNOWN')    AS setup_type,
                COALESCE(market_regime, 'UNKNOWN') AS regime,
                COUNT(*) AS n,
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN') AS wins,
                ROUND(
                    COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')::numeric
                    / NULLIF(COUNT(*), 0) * 100, 1
                ) AS win_rate_pct,
                ROUND(AVG(COALESCE(pnl_eur, 0))::numeric, 4) AS avg_pnl,
                ROUND(AVG(COALESCE(pnl_r, result_r, 0))::numeric, 2) AS avg_r,
                ROUND(COALESCE(SUM(
                    CASE WHEN UPPER(outcome)='WIN'
                    THEN ABS(COALESCE(pnl_eur,0)) ELSE 0 END
                ), 0)::numeric, 4) AS total_winst,
                ROUND(COALESCE(SUM(
                    CASE WHEN UPPER(outcome)='LOSS'
                    THEN ABS(COALESCE(pnl_eur,0)) ELSE 0 END
                ), 0.001)::numeric, 4) AS total_verlies
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) = 'SHADOW'
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
            GROUP BY 1, 2
            HAVING COUNT(*) >= 3
            ORDER BY win_rate_pct DESC
            LIMIT 20
            """)
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log(f"⚠️ get_shadow_win_rate_by_setup fout: {type(e).__name__}: {e}")
        return []


def get_shadow_rolling_stats(conn, days: int = 7) -> Tuple[int, int, float]:
    """
    Haalt shadow statistieken op over de laatste X dagen.

    Geeft (wins, losses, pnl_eur) terug.

    Identieke structuur als get_rolling_stats() in:
    - whatsapp_webhook.py
    - trade_monitor.py

    Wordt gebruikt door:
    - whatsapp_webhook.py → dagrapport sectie 'SHADOW'
    - app.py → performance pagina shadow kolom
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')  AS wins,
                COUNT(*) FILTER (WHERE UPPER(outcome)='LOSS') AS losses,
                COALESCE(SUM(
                    CASE
                        WHEN UPPER(outcome)='WIN'  THEN  ABS(COALESCE(pnl_eur,0))
                        WHEN UPPER(outcome)='LOSS' THEN -ABS(COALESCE(pnl_eur,0))
                        ELSE 0
                    END
                ), 0) AS pnl
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) = 'SHADOW'
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '1 day' * %s
            """, (days,))
            row = cur.fetchone()
            if row:
                return safe_int(row[0]), safe_int(row[1]), safe_float(row[2])
    except Exception as e:
        log(f"⚠️ get_shadow_rolling_stats fout: {type(e).__name__}: {e}")
    return 0, 0, 0.0


def compare_shadow_vs_live(conn, days: int = 30) -> Dict[str, Any]:
    """
    Vergelijkt shadow performance met live performance.

    Dit is de kern van de edge decay detectie.
    Als shadow win rate veel hoger is dan live win rate,
    is er iets mis met de live uitvoering.

    Mogelijke oorzaken van groot verschil (>10%):
    1. Fees/slippage: shadow heeft gesimuleerde fees maar
       live heeft werkelijke fees die anders kunnen zijn
    2. Timing: shadow fill is exact op signaalprijs,
       live order heeft slippage door marktimpact
    3. Bitvavo universe: shadow trades op coins die live
       niet tradable zijn (andere liquiditeit)
    4. Score drempel: te laag — te veel matige signalen
    5. Marktverandering: sim data is van een andere markt
       dan de huidige live omstandigheden

    Geeft een dict terug met:
    - shadow:      shadow statistieken
    - live:        live statistieken
    - diff_pct:    verschil in win rate (shadow - live)
    - edge_decay:  True als diff > 10% en beide hebben data

    Samenwerking:
    - Aangeroepen door trade_monitor.py (elke 20 trades)
    - Aangeroepen door whatsapp_webhook.py /send_health_check
    - Weergegeven door app.py edge decay pagina
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                UPPER(COALESCE(source,'')) AS src,
                COUNT(*) AS n,
                COUNT(*) FILTER (WHERE UPPER(outcome)='WIN') AS wins,
                ROUND(
                    COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')::numeric
                    / NULLIF(COUNT(*),0) * 100, 1
                ) AS win_rate_pct,
                ROUND(AVG(COALESCE(pnl_eur,0))::numeric, 4) AS avg_pnl,
                ROUND(AVG(COALESCE(pnl_r, result_r, 0))::numeric, 2) AS avg_r
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) IN ('SHADOW','REAL','LIVE')
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '1 day' * %s
            GROUP BY 1
            """, (days,))
            rows = cur.fetchall()

        data: Dict[str, Dict] = {}
        for row in rows:
            src = safe_str(row[0])
            key = "LIVE" if src in ("REAL", "LIVE") else src
            data[key] = {
                "n":        safe_int(row[1]),
                "wins":     safe_int(row[2]),
                "win_rate": safe_float(row[3]),
                "avg_pnl":  safe_float(row[4]),
                "avg_r":    safe_float(row[5]),
            }

        shadow_data = data.get("SHADOW", {})
        live_data   = data.get("LIVE",   {})
        shadow_wr   = safe_float(shadow_data.get("win_rate"))
        live_wr     = safe_float(live_data.get("win_rate"))
        diff        = shadow_wr - live_wr

        # Edge decay gedetecteerd als verschil > 10% en beide hebben data
        edge_decay = (
            diff > 10.0
            and shadow_wr > 0
            and live_wr > 0
            and shadow_data.get("n", 0) >= 5
            and live_data.get("n", 0) >= 5
        )

        return {
            "shadow":      shadow_data,
            "live":        live_data,
            "diff_pct":    round(diff, 1),
            "edge_decay":  edge_decay,
            "days":        days,
        }

    except Exception as e:
        log(f"⚠️ compare_shadow_vs_live fout: {type(e).__name__}: {e}")
        return {}


def get_shadow_profit_factor(conn, days: int = 30) -> float:
    """
    Berekent de Profit Factor voor shadow trades.

    Profit Factor = Totale winst / Totale verlies
    Doel: >1.5 (zelfde als live trades)

    Als shadow PF veel hoger is dan live PF,
    duidt dit op edge decay (strategie werkt beter in sim).

    Samenwerking:
    - Aangeroepen door app.py performance pagina
    - Aangeroepen door whatsapp_webhook.py rapporten
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT
                COALESCE(SUM(
                    CASE WHEN UPPER(outcome)='WIN'
                    THEN ABS(COALESCE(pnl_eur,0)) ELSE 0 END
                ), 0) AS totaal_winst,
                COALESCE(SUM(
                    CASE WHEN UPPER(outcome)='LOSS'
                    THEN ABS(COALESCE(pnl_eur,0)) ELSE 0 END
                ), 0.001) AS totaal_verlies
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) = 'SHADOW'
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
              AND COALESCE(exit_time, updated_at) >= NOW() - INTERVAL '1 day' * %s
            """, (days,))
            row = cur.fetchone()
            if row:
                winst   = safe_float(row[0])
                verlies = max(safe_float(row[1]), 0.001)
                return round(winst / verlies, 2)
    except Exception as e:
        log(f"⚠️ get_shadow_profit_factor fout: {e}")
    return 0.0


def get_shadow_top_coins(conn, limit: int = 5) -> List[Dict]:
    """
    Haalt de beste coins op basis van shadow win rate.

    Minimaal 5 shadow trades vereist per coin.
    Geeft (coin, n, win_rate, total_pnl) terug.

    Gebruikt door:
    - app.py → setup analyse pagina
    - whatsapp_webhook.py → wekelijks rapport
    """
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
            SELECT
                COALESCE(coin, '?') AS coin,
                COUNT(*) AS n,
                ROUND(
                    COUNT(*) FILTER (WHERE UPPER(outcome)='WIN')::numeric
                    / NULLIF(COUNT(*),0) * 100, 1
                ) AS win_rate_pct,
                ROUND(COALESCE(SUM(
                    CASE
                        WHEN UPPER(outcome)='WIN'  THEN  ABS(COALESCE(pnl_eur,0))
                        WHEN UPPER(outcome)='LOSS' THEN -ABS(COALESCE(pnl_eur,0))
                        ELSE 0
                    END
                ),0)::numeric, 4) AS total_pnl
            FROM public.experience_trades
            WHERE UPPER(COALESCE(source,'')) = 'SHADOW'
              AND UPPER(COALESCE(outcome,'')) IN ('WIN','LOSS')
            GROUP BY 1
            HAVING COUNT(*) >= 5
            ORDER BY win_rate_pct DESC, n DESC
            LIMIT %s
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log(f"⚠️ get_shadow_top_coins fout: {e}")
        return []


# ============================================================
# ONDERHOUD EN CLEANUP
# ─────────────────────────────────────────────────────────────
# Functies voor het opschonen van de shadow state.
# Voorkomt dat de state file oneindig groot wordt.
# ============================================================
def cleanup_old_shadow_state() -> int:
    """
    Verwijdert shadow trades die ouder zijn dan MAX_HOLD_HOURS.

    Wordt uitgevoerd bij opstarten van trade_monitor.py
    om de state file schoon te houden.

    Een shadow trade die ouder is dan 48u had al automatisch
    gesloten moeten zijn via de MAX_HOLD_TIME exit.
    Als het toch nog open staat, is er iets misgegaan en
    wordt het hier verwijderd als 'verlopen'.

    Geeft het aantal verwijderde trades terug.
    """
    state     = load_shadow_state()
    positions = state.get("positions") or {}
    removed   = 0
    now_ts    = time.time()
    max_hold_sec = MAX_HOLD_HOURS * 3600

    to_remove = []
    for symbol, pos in positions.items():
        opened_at = safe_float(pos.get("opened_at"))
        if opened_at and (now_ts - opened_at) >= max_hold_sec:
            log(
                f"🧹 Shadow trade verlopen: {symbol} "
                f"(open {(now_ts - opened_at)/3600:.1f}u geleden)"
            )
            to_remove.append(symbol)

    for symbol in to_remove:
        state["positions"].pop(symbol, None)
        state["open_trades"] = [
            t for t in (state.get("open_trades") or [])
            if t.get("symbol") != symbol
        ]
        removed += 1

    if removed > 0:
        save_shadow_state(state)
        log(f"🧹 {removed} verlopen shadow trades verwijderd")

    return removed


def sync_shadow_state_to_db(conn) -> int:
    """
    Synchroniseert open shadow posities naar experience_trades.

    Zorgt dat open shadow trades zichtbaar zijn in de database
    zodat het dashboard (app.py) ze kan tonen via SQL.

    Gebruikt ON CONFLICT zodat dit veilig meerdere keren
    kan worden aangeroepen zonder duplicaten.

    Geeft het aantal gesynchroniseerde trades terug.

    Aangeroepen bij opstarten van trade_monitor.py.
    """
    if not conn:
        return 0

    positions = (load_shadow_state().get("positions") or {})
    synced    = 0

    for symbol, pos in positions.items():
        prebuy_id = safe_str(pos.get("prebuy_id"))
        trade_key = f"SHADOW|{symbol}|{prebuy_id or 'open'}"
        entry     = safe_float(pos.get("entry"))
        setup     = safe_str(pos.get("setup_type"))
        regime    = safe_str(pos.get("regime"))
        score     = safe_int(pos.get("score"))

        try:
            with conn.cursor() as cur:
                cur.execute("""
                INSERT INTO public.experience_trades (
                    trade_key, source, is_shadow, coin,
                    timestamp, entry_time,
                    setup_type, market_regime, entry,
                    outcome, bot_confidence,
                    created_at, updated_at
                )
                VALUES (%s,'SHADOW',TRUE,%s,NOW(),NOW(),%s,%s,%s,'OPEN',%s,NOW(),NOW())
                ON CONFLICT (trade_key) DO UPDATE SET
                    updated_at = NOW()
                """, (trade_key, symbol, setup, regime, entry, score))
            conn.commit()
            synced += 1
        except Exception as e:
            log(f"⚠️ Shadow sync fout ({symbol}): {type(e).__name__}: {e}")

    if synced > 0:
        log(f"✅ {synced} open shadow trades gesynchroniseerd naar DB")

    return synced


def reset_shadow_state() -> bool:
    """
    Reset de volledige shadow state.

    WAARSCHUWING: Dit verwijdert alle open shadow posities
    en statistieken uit de state file. DB data blijft bewaard.

    Gebruik alleen als er ernstige corruptie is of
    voor een fresh start.

    Geeft True terug bij succes.
    """
    log("⚠️ Shadow state wordt gereset...")
    return save_shadow_state(_empty_state())


# ============================================================
# MAIN — test en health check
# ============================================================
if __name__ == "__main__":
    log("=" * 60)
    log("Shadow Trades v2.0 — health check")
    log("=" * 60)
    log(f"Database:       {'✅' if DATABASE_URL else '❌ ONTBREEKT'}")
    log(f"Twilio:         {'✅' if TWILIO_ACCOUNT_SID else '⚠️ niet ingesteld'}")
    log(f"Claude API:     {'✅' if ANTHROPIC_API_KEY else '⚠️ niet ingesteld'}")
    log(f"Data dir:       {DATA_DIR}")
    log(f"State file:     {SHADOW_STATE_PATH}")
    log(f"Max hold:       {MAX_HOLD_HOURS}u")
    log(f"Fee+slippage:   {TOTAL_COST_PCT*100:.2f}%")
    log(f"Standaard amt:  €{DEFAULT_SHADOW_AMOUNT_EUR:.2f}")
    log(f"Structuur break: R < {STRUCTUUR_BREAK_R}")
    log("=" * 60)

    # State lezen
    stats = get_shadow_stats()
    log(f"\nSHADOW STATISTIEKEN:")
    log(f"  Open trades:     {stats['open_count']}")
    log(f"  Totaal geopend:  {stats['total_opened']}")
    log(f"  Totaal gesloten: {stats['total_closed']}")
    log(f"  Wins:            {stats['total_wins']}")
    log(f"  Losses:          {stats['total_losses']}")
    log(f"  Win rate:        {stats['win_rate_pct']:.1f}%")
    log(f"  Gesim. PnL:      €{stats['total_pnl']:.4f}")

    if stats["open_symbols"]:
        log(f"\n  Open coins: {', '.join(stats['open_symbols'])}")

    # DB verbinding testen
    if DATABASE_URL:
        conn = db_connect()
        if conn:
            log("\n✅ Database verbonden")

            # Shadow vs live vergelijking
            result = compare_shadow_vs_live(conn, days=30)
            if result:
                sh = result.get("shadow", {})
                li = result.get("live", {})
                log(f"\nSHADOW vs LIVE (30 dagen):")
                log(f"  Shadow: {sh.get('win_rate',0):.1f}% WR ({sh.get('n',0)} trades)")
                log(f"  Live:   {li.get('win_rate',0):.1f}% WR ({li.get('n',0)} trades)")
                log(f"  Verschil: {result['diff_pct']:.1f}%")
                if result["edge_decay"]:
                    log("  ⚠️ EDGE DECAY GEDETECTEERD")
                else:
                    log("  ✅ Geen edge decay")

            # Cleanup
            removed = cleanup_old_shadow_state()
            if removed > 0:
                log(f"\n🧹 {removed} verlopen trades verwijderd")

            conn.close()
        else:
            log("❌ Database verbinding mislukt")

    log("\n✅ Shadow Trades health check klaar")
