#!/usr/bin/env python3
"""
History Simulator — doorverwijst naar Market Simulator v2.1
===========================================================
Dit bestand bestond vroeger als aparte simulator.
Nu roept het market_simulator.run() aan zodat de Render cron job
(die python research/history_simulator.py draait) gewoon werkt.
"""
import os, sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from research.market_simulator import run

if __name__ == "__main__":
    run()
