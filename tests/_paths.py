"""Puts bin/ on the import path. Tests never touch the network or the live data directory."""
import os
import sys

BIN = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if BIN not in sys.path:
    sys.path.insert(0, BIN)
