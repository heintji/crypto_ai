"""Zorg dat de repo-root op sys.path staat zodat tests de bot-modules importeren."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
