import os
import time
import subprocess
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def run(cmd: str):
    print(f"\n[{datetime.utcnow().isoformat()}] RUN: {cmd}", flush=True)
    subprocess.run(cmd, shell=True, cwd=PROJECT_ROOT, check=False)

# tijden
next_multi = datetime.utcnow()
next_history = datetime.utcnow()  # direct bij start 1x history pakken
next_sim = datetime.utcnow()      # direct daarna simulatie (optioneel)

while True:
    now = datetime.utcnow()

    # multi_coin elke 30 min
    if now >= next_multi:
        run("python analysis/multi_coin_score.py")
        next_multi = now + timedelta(minutes=30)

    # history_fetcher 1x per dag
    if now >= next_history:
        run("python research/history_fetcher.py")
        next_history = now + timedelta(hours=24)

    # simulator (bv na history) 1x per dag
    if now >= next_sim:
        run("python research/history_simulator.py")
        next_sim = now + timedelta(hours=24)

    time.sleep(20)
