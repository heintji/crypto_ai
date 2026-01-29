r"""
ai_advisor.py
-------------
Doel:
- Pre-BUY setups laten beoordelen door AI als coach/analist (GEEN trade-executie).
- Output als JSON + logging naar CSV voor Excel-analyse.
- Kostenbeveiliging zodat uitgaven ruim onder $5 per maand blijven.

BELANGRIJK:
- API key staat NIET in dit bestand.
- API key staat in: C:\Users\heint\crypto_ai\.env
  met exact deze regel:
  OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx

Run:
  cd C:\Users\heint\crypto_ai
  python ai_advisor.py --input logs/prebuy_payload.json
"""

import os
import json
import csv
import argparse
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# =========================
# COST LIMITS (veilig < $5 / maand)
# =========================
MAX_CALLS_PER_DAY = 25
MAX_CALLS_PER_MONTH = 400
MAX_OUTPUT_TOKENS = 350

USAGE_FILE = Path("logs/ai_usage.json")
ADVICE_LOG = Path("logs/ai_advice.csv")

# =========================
# AI ADVISOR PROMPT
# =========================
AI_ADVISOR_SYSTEM_PROMPT = """
Jij bent AI Advisor voor een crypto trading systeem.

ROL:
- Coach / analist / leraar.
- NOOIT BUY, SELL, ENTER, EXIT, OPEN of CLOSE adviseren.
- Je overschrijft NOOIT het trading systeem.
- Je beoordeelt alleen kwaliteit, risico en context.

HARD RULES:
1) Geen trade-acties of timing.
2) Geen verplichte sizing.
3) Bij ontbrekende data: conservatief + needs_more_data=true.
4) Output ALTIJD valide JSON volgens schema.
5) Geen markdown code blocks (dus geen ```json ... ```). Geef pure JSON.

OUTPUT JSON:
{
  "verdict": "A" | "B" | "C",
  "confidence_pct": 0-100,
  "setup_quality": "strong" | "ok" | "weak",
  "market_regime": "trend" | "range" | "transition" | "unknown",
  "key_strengths": [max 4],
  "key_risks": [max 4],
  "invalidations_to_watch": [max 3],
  "notes": "max 2 zinnen NL",
  "needs_more_data": true|false,
  "missing_data": [max 3]
}
""".strip()


def build_prebuy_user_prompt(payload: dict) -> str:
    return f"""
Beoordeel deze PRE-BUY setup. Geef GEEN BUY/SELL advies.
Geef uitsluitend pure JSON volgens het schema.

DATA:
- coin: {payload.get("coin")}
- timestamp: {payload.get("timestamp")}
- timeframe_primary: {payload.get("timeframe_primary")}
- timeframe_entry: {payload.get("timeframe_entry")}
- score_pct: {payload.get("score_pct")}
- setup_type: {payload.get("setup_type")}
- trend_4h: {payload.get("trend_4h")}
- trend_1h: {payload.get("trend_1h")}
- rr: {payload.get("rr")}
- structure_notes: {payload.get("structure_notes")}
- nearby_levels: {payload.get("nearby_levels")}
- rsi_4h: {payload.get("rsi_4h")}
- rsi_1h: {payload.get("rsi_1h")}
- winrate_setup: {payload.get("winrate_setup")}
- news_risk: {payload.get("news_risk")}

CONTEXT:
- Bot geeft alleen Pre-BUY signalen.
- Mens beslist (YES bedrag).
- Exits zijn volledig geautomatiseerd.
""".strip()


# =========================
# USAGE LIMITERS
# =========================
def utc_now():
    return datetime.now(timezone.utc)


def load_usage():
    if not USAGE_FILE.exists():
        return {"day": "", "day_count": 0, "month": "", "month_count": 0}
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # defaults
        for k, v in {"day": "", "day_count": 0, "month": "", "month_count": 0}.items():
            if k not in data:
                data[k] = v
        return data
    except Exception:
        return {"day": "", "day_count": 0, "month": "", "month_count": 0}


def save_usage(data):
    USAGE_FILE.parent.mkdir(exist_ok=True)
    with open(USAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def can_call_ai():
    usage = load_usage()
    now = utc_now()

    today = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")

    if usage["day"] != today:
        usage["day"] = today
        usage["day_count"] = 0

    if usage["month"] != month:
        usage["month"] = month
        usage["month_count"] = 0

    if usage["day_count"] >= MAX_CALLS_PER_DAY:
        return False, "daglimiet bereikt", usage
    if usage["month_count"] >= MAX_CALLS_PER_MONTH:
        return False, "maandlimiet bereikt", usage

    usage["day_count"] += 1
    usage["month_count"] += 1
    save_usage(usage)
    return True, None, usage


# =========================
# IO + JSON PARSING
# =========================
def ensure_logs():
    Path("logs").mkdir(exist_ok=True)


def read_payload(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_json_safe(text: str) -> dict:
    """
    Maakt AI-output schoon en parseert JSON.
    Fix voor situaties waarin AI per ongeluk ```json ... ``` gebruikt.
    """
    if not text:
        return {"error": "empty_response"}

    cleaned = text.strip()

    # Strip markdown fences
    if cleaned.startswith("```"):
        cleaned = cleaned.replace("```json", "").replace("```", "").strip()

    # Soms zit er nog rommel voor/na de JSON. Pak alleen { ... }
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned_core = cleaned[start : end + 1]
    else:
        cleaned_core = cleaned

    try:
        return json.loads(cleaned_core)
    except Exception:
        return {"error": "invalid_json", "raw": cleaned[:500]}


def log_csv(payload: dict, advice: dict):
    ensure_logs()
    exists = ADVICE_LOG.exists()

    # Log iets breder zodat Excel later nuttig is
    row = {
        "utc_time": utc_now().isoformat(),
        "coin": payload.get("coin"),
        "timestamp": payload.get("timestamp"),
        "timeframe_primary": payload.get("timeframe_primary"),
        "timeframe_entry": payload.get("timeframe_entry"),
        "score_pct": payload.get("score_pct"),
        "setup_type": payload.get("setup_type"),
        "trend_4h": payload.get("trend_4h"),
        "trend_1h": payload.get("trend_1h"),
        "rr": payload.get("rr"),
        "verdict": advice.get("verdict"),
        "confidence_pct": advice.get("confidence_pct"),
        "setup_quality": advice.get("setup_quality"),
        "market_regime": advice.get("market_regime"),
        "needs_more_data": advice.get("needs_more_data"),
        "missing_data": "|".join(advice.get("missing_data", [])) if isinstance(advice.get("missing_data"), list) else "",
        "key_strengths": "|".join(advice.get("key_strengths", [])) if isinstance(advice.get("key_strengths"), list) else "",
        "key_risks": "|".join(advice.get("key_risks", [])) if isinstance(advice.get("key_risks"), list) else "",
        "invalidations_to_watch": "|".join(advice.get("invalidations_to_watch", [])) if isinstance(advice.get("invalidations_to_watch"), list) else "",
        "notes": advice.get("notes"),
        "error": advice.get("error", "")
    }

    with open(ADVICE_LOG, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not exists:
            writer.writeheader()
        writer.writerow(row)


# =========================
# MAIN
# =========================
def load_api_key():
    load_dotenv()
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        print("❌ OPENAI_API_KEY niet gevonden in .env")
        print("Zet deze regel in: C:\\Users\\heint\\crypto_ai\\.env")
        print("OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxx")
        raise SystemExit(1)
    return key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="logs/prebuy_payload.json")
    args = parser.parse_args()

    ensure_logs()

    allowed, reason, usage = can_call_ai()
    if not allowed:
        print(f"⛔ AI call overgeslagen ({reason})")
        print(f"Usage: day_count={usage.get('day_count')} / month_count={usage.get('month_count')}")
        return

    if not Path(args.input).exists():
        print(f"❌ Inputbestand ontbreekt: {args.input}")
        return

    payload = read_payload(args.input)
    api_key = load_api_key()
    client = OpenAI(api_key=api_key)

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": AI_ADVISOR_SYSTEM_PROMPT},
            {"role": "user", "content": build_prebuy_user_prompt(payload)}
        ],
        max_output_tokens=MAX_OUTPUT_TOKENS
    )

    advice = parse_json_safe(response.output_text)

    print("✅ AI ADVICE (JSON):")
    print(json.dumps(advice, indent=2, ensure_ascii=False))

    log_csv(payload, advice)
    print("📝 Gelogd naar logs/ai_advice.csv")


if __name__ == "__main__":
    main()
