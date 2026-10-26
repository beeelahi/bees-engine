"""Add or close a public pick in picks/picks.json, using the published data of beesells.xyz.

    python3 tools/pick.py add SYMBOL --data PATH_TO_SITE_DATA      (e.g. a checkout of beeelahi/beesells, folder data/)
    python3 tools/pick.py close SYMBOL --data PATH [--reason "Stop hit"|"Target reached"|"Removed"]

Entry and exit = the latest closed daily candle in the published screener; stop = entry - 2 x ATR(14), fixed now.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PICKS = os.path.join(HERE, "..", "picks", "picks.json")
REASONS = ("Removed", "Stop hit", "Target reached", "Closed")


def load_screener(data_dir):
    with open(os.path.join(data_dir, "screener.json")) as f:
        s = json.load(f)
    with open(os.path.join(data_dir, "overview.json")) as f:
        ov = json.load(f)
    cols = s["columns"]
    rows = {r[cols.index("symbol")]: dict(zip(cols, r)) for r in s["rows"]}
    return rows, ov["last_date"]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["add", "close"])
    ap.add_argument("symbol")
    ap.add_argument("--data", required=True)
    ap.add_argument("--reason", default="Removed", choices=REASONS)
    a = ap.parse_args(argv)
    sym = a.symbol.upper()
    sym = sym if sym.endswith("USDT") else sym + "USDT"
    rows, last_date = load_screener(a.data)
    r = rows.get(sym)
    if not r or not r.get("last_close"):
        sys.exit(f"{sym} is not in the published universe")
    with open(PICKS) as f:
        book = json.load(f)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    active = [x for x in book["rows"] if x["symbol"] == sym and x["status"] == "active"]
    px = float(r["last_close"])
    if a.action == "add":
        if active:
            sys.exit(f"{sym} is already an active pick")
        atr_pct = r.get("d_atr14_pct")
        if not atr_pct:
            sys.exit(f"{sym} has no ATR yet (too little history)")
        stop = px * (1 - 2 * float(atr_pct) / 100)
        book["rows"].append({"symbol": sym, "status": "active", "entry": float(f"{px:.8g}"), "entry_date": last_date,
                             "stop": float(f"{stop:.8g}"), "risk_pct": round(2 * float(atr_pct), 2), "approved_at": now})
        msg = f"{sym}: entry {px:.8g} ({last_date} close), stop {stop:.8g} (-{2 * float(atr_pct):.1f}%)"
    else:
        if not active:
            sys.exit(f"{sym} has no active pick")
        x = active[0]
        x.update(status="closed", exit=float(f"{px:.8g}"), exit_date=last_date, closed_at=now, close_reason=a.reason)
        msg = f"{sym}: closed at {px:.8g} ({last_date} close), {100 * (px / x['entry'] - 1):+.1f}%, reason {a.reason}"
    book["as_of"] = now
    with open(PICKS, "w") as f:
        json.dump(book, f, indent=2)
        f.write("\n")
    print(msg)


if __name__ == "__main__":
    main()
