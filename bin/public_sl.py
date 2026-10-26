"""
Bee's Shortlist, public read-only copy (Python 3.9 stdlib only).

The picks live in the public repository (picks/picks.json): every pick and every close is a commit, so the record
is verifiable. The deployment copies that folder to the server; this module turns it into web_export/shortlist.json
for the website, keeping ONLY the fields below. Returns True when the public copy changed (so the caller
republishes).
"""
import json
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "picks", "picks.json")          # deployed from the repository (picks/), one commit per change
DST = os.path.join(ROOT, "web_export", "shortlist.json")
FIELDS = {"symbol": str, "status": str, "entry": float, "entry_date": str, "stop": float, "risk_pct": float,
          "approved_at": str, "exit": float, "exit_date": str, "closed_at": str, "close_reason": str}
SYM_RE = re.compile(r"[A-Z0-9]{2,20}USDT")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}([T ][0-9:.]+Z?)?")
REASONS = {"Removed", "Stop hit", "Target reached", "Closed"}


def _clean(d):
    out = {}
    for k, t in FIELDS.items():
        v = d.get(k)
        if v is None:
            continue
        if t is float:
            try:
                v = float(v)
            except (TypeError, ValueError):
                return None
            if not (v == v) or v <= 0:
                return None
        elif not isinstance(v, str):
            return None
        out[k] = v
    if not SYM_RE.fullmatch(out.get("symbol", "")) or out.get("status") not in ("active", "closed"):
        return None
    for k in ("entry_date", "exit_date", "approved_at", "closed_at"):
        if k in out and not DATE_RE.fullmatch(out[k]):
            return None
    if "entry" not in out or "stop" not in out or "entry_date" not in out or out["stop"] >= out["entry"]:
        return None
    if "close_reason" in out:                      # free text the owner typed: publish only standard reasons
        out["close_reason"] = out["close_reason"] if out["close_reason"] in REASONS else "Closed"
    return out


def build(src=SRC):
    if not os.path.exists(src) or os.path.islink(src):
        return None
    with open(src) as f:
        raw = json.load(f)
    rows = [x for x in (_clean(d) for d in raw.get("rows", []) if isinstance(d, dict)) if x]
    rows.sort(key=lambda x: x.get("approved_at", x["entry_date"]), reverse=True)
    as_of = raw.get("as_of") if isinstance(raw.get("as_of"), str) and DATE_RE.fullmatch(raw.get("as_of", "")) else None
    return {"as_of": as_of, "rows": rows}


def sync(log=print, src=SRC, dst=DST):
    data = build(src)
    if data is None or not os.path.isdir(os.path.dirname(dst)):
        return False
    text = json.dumps(data, separators=(",", ":"), sort_keys=True)
    if os.path.exists(dst):
        with open(dst) as f:
            if f.read() == text:
                return False
    tmp = dst + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, dst)
    log(f"public shortlist: {sum(1 for x in data['rows'] if x['status'] == 'active')} active, "
        f"{sum(1 for x in data['rows'] if x['status'] == 'closed')} closed")
    return True
