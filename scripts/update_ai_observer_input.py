# scripts/update_ai_observer_input.py
# AI Observer — read-only meekijker voor analyse & advies

import os
import json
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

OBSERVER_FILE = os.path.join(LOGS_DIR, "ai_observer_input.json")

FILES_TO_WATCH = {
    "pending_approvals": os.path.join(DATA_DIR, "pending_approvals.json"),
    "shadow_trades": os.path.join(DATA_DIR, "shadow_trades.json"),
    "paper_trades": os.path.join(DATA_DIR, "paper_trades.csv"),
}

def safe_load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return "UNREADABLE"

def main():
    os.makedirs(LOGS_DIR, exist_ok=True)

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "AI_OBSERVER_READ_ONLY",
        "description": "ChatGPT kijkt mee — analyse & advies, geen controle",
        "data": {},
        "summary": {}
    }

    for key, path in FILES_TO_WATCH.items():
        snapshot["data"][key] = safe_load_json(path)

    pending = snapshot["data"].get("pending_approvals")
    shadows = snapshot["data"].get("shadow_trades")

    snapshot["summary"] = {
        "pending_prebuys": len(pending) if isinstance(pending, list) else 0,
        "open_shadow_trades": (
            len([s for s in shadows if s.get("status") == "OPEN"])
            if isinstance(shadows, list)
            else 0
        ),
        "note": "Gebruik dit bestand voor AI-advies over filters, timing en kwaliteit."
    }

    with open(OBSERVER_FILE, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    print("✅ AI observer snapshot aangemaakt:")
    print(OBSERVER_FILE)

if __name__ == "__main__":
    main()
