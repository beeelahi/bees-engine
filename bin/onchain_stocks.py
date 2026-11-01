"""
On-chain tokenized US stocks (Python 3.9 stdlib only): the xStocks (Backed) and Ondo Global Markets tokens that trade on
Solana and BNB Chain, with live price, on-chain liquidity and 24h volume from public APIs (no keys, read-only).

    python3 bin/onchain_stocks.py --probe      what the public sources return (ops: onchain-probe)
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from world_markets import fetch  # noqa: E402

JUP_SEARCH = "https://lite-api.jup.ag/tokens/v2/search?query="
DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search?q="


def probe():
    for q in ("xStock", "NVDAx", "Ondo Tokenized", "NVDAon"):
        st, body = fetch(JUP_SEARCH + q.replace(" ", "%20"), timeout=20)
        try:
            d = json.loads(body)
        except ValueError:
            d = None
        print(f"== Jupiter search {q!r}: http {st}, {len(d) if isinstance(d, list) else type(d).__name__} results")
        for t in (d or [])[:6] if isinstance(d, list) else []:
            print("   ", {k: t.get(k) for k in ("id", "name", "symbol", "usdPrice", "liquidity", "mcap", "isVerified", "tags", "organicScore")},
                  "| 24h", (t.get("stats24h") or {}).get("buyVolume"), (t.get("stats24h") or {}).get("priceChange"))
        if isinstance(d, list) and d:
            print("    keys:", sorted(d[0].keys()))
        time.sleep(1)
    for q in ("NVDAx", "NVDAon", "TSLAon"):
        st, body = fetch(DEX_SEARCH + q, timeout=20)
        try:
            pairs = json.loads(body).get("pairs") or []
        except (ValueError, AttributeError):
            pairs = []
        print(f"== DexScreener search {q!r}: http {st}, {len(pairs)} pairs")
        for p in pairs[:6]:
            print("   ", p.get("chainId"), p.get("dexId"), (p.get("baseToken") or {}).get("symbol"), (p.get("baseToken") or {}).get("name"),
                  (p.get("baseToken") or {}).get("address"), "px", p.get("priceUsd"), "liq", (p.get("liquidity") or {}).get("usd"),
                  "vol24", (p.get("volume") or {}).get("h24"))
        time.sleep(1)


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
