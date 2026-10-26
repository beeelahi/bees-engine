"""Shared paths for the research scripts. Data comes from read-only exports of the engine database:
  daily_v2.csv.gz  symbol,date,open,high,low,close,volume_usdt,taker_buy,trades   (all pairs, full history)
  pairs_v2.csv     symbol,token
  candles_4h.csv.gz symbol,open_ms,open,high,low,close,volume_usdt
Set RESEARCH_DATA to the folder that holds them (default: ./research_data)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("RESEARCH_DATA", os.path.join(HERE, "..", "research_data"))
for p in (HERE, os.path.join(HERE, "..", "bin")):
    if p not in sys.path:
        sys.path.insert(0, p)


def path(name):
    return os.path.join(DATA, name)
