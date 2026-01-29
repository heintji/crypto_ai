# trading/shadow_trades.py
import json
import os
import time

DATA_DIR = "data"
SHADOW_PATH = os.path.join(DATA_DIR, "shadow_trades.json")


def ensure_file():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.isfile(SHADOW_PATH):
        with open(SHADOW_PATH, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)


def load_shadows():
    ensure_file()
    try:
        with open(SHADOW_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_shadows(shadows):
    ensure_file()
    with open(SHADOW_PATH, "w", encoding="utf-8") as f:
        json.dump(shadows, f, indent=2, ensure_ascii=False)


def create_shadow(prebuy_id, symbol, entry, stop, target):
    shadows = load_shadows()
    shadows.append({
        "id": prebuy_id,
        "symbol": symbol,
        "entry": entry,
        "stop": stop,
        "target": target,
        "status": "OPEN",
        "opened_at": int(time.time())
    })
    save_shadows(shadows)
