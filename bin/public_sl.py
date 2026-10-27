"""
Bee's Shortlist, public read-only copy (Python 3.9 stdlib only).

Two sources, merged into web_export/shortlist.json for the website, keeping ONLY the fields below:
  * automatic picks (auto_picks.py): the engine's rules, recorded in a SHA-256-chained ledger on the server whose
    head hash is published with the list;
  * manual picks, if any, in the public repository (picks/picks.json): each one is a commit.
Returns True when the public copy changed (so the caller republishes).
"""
import json
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "picks", "picks.json")          # deployed from the repository (picks/), one commit per change
AUTO = os.path.join(ROOT, "data", "auto_picks.json")     # written by auto_picks.py on the server
DST = os.path.join(ROOT, "web_export", "shortlist.json")
FIELDS = {"symbol": str, "status": str, "entry": float, "entry_date": str, "stop": float, "risk_pct": float,
          "approved_at": str, "exit": float, "exit_date": str, "closed_at": str, "close_reason": str, "source": str, "why": list, "side": str, "market": str}
WHY_RE = re.compile(r"(1d|1w):[a-z0-9_]{1,40}")
SYM_RE = re.compile(r"[A-Z0-9]{1,20}USDT")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}([T ][0-9:.]+Z?)?")
REASONS = {"Removed", "Stop hit", "Target reached", "Closed", "Signal exit"}


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
        elif t is list:
            if not isinstance(v, list) or not all(isinstance(z, str) and WHY_RE.fullmatch(z) for z in v):
                return None
            v = v[:6]
        elif not isinstance(v, str):
            return None
        out[k] = v
    if not SYM_RE.fullmatch(out.get("symbol", "")) or out.get("status") not in ("active", "closed"):
        return None
    for k in ("entry_date", "exit_date", "approved_at", "closed_at"):
        if k in out and not DATE_RE.fullmatch(out[k]):
            return None
    out["side"] = "short" if out.get("side") == "short" else "long"
    if out.get("market") not in (None, "btc_above_200d", "btc_below_200d"):
        out.pop("market")
    if "entry" not in out or "stop" not in out or "entry_date" not in out:
        return None
    if (out["side"] == "long" and out["stop"] >= out["entry"]) or (out["side"] == "short" and out["stop"] <= out["entry"]):
        return None
    out["source"] = "auto" if out.get("source") == "auto" else "bee"
    if "close_reason" in out:                      # free text the owner typed: publish only standard reasons
        out["close_reason"] = out["close_reason"] if out["close_reason"] in REASONS else "Closed"
    return out


def _read(path):
    if not os.path.exists(path) or os.path.islink(path):
        return None
    with open(path) as f:
        return json.load(f)


def build(src=SRC, auto=AUTO):
    raw, au = _read(src), _read(auto)
    if raw is None and au is None:
        return None
    rows = []
    for book, src_name in ((raw, "bee"), (au, "auto")):
        for d in (book or {}).get("rows", []):
            if isinstance(d, dict):
                x = _clean(dict(d, source=src_name))
                if x:
                    rows.append(x)
    rows.sort(key=lambda x: (x.get("approved_at", x["entry_date"]), x["symbol"]), reverse=True)
    as_of = (raw or {}).get("as_of")
    as_of = as_of if isinstance(as_of, str) and DATE_RE.fullmatch(as_of) else None
    out = {"as_of": as_of, "rows": rows}
    if au is not None:
        head = au.get("head", "")
        out["auto"] = {"market_ok": au.get("market_ok") if isinstance(au.get("market_ok"), bool) else None,
                       "rules": au.get("rules") if isinstance(au.get("rules"), int) else 1,
                       "last_date": au.get("last_date") if isinstance(au.get("last_date"), str) else None,
                       "ledger_head": head if re.fullmatch(r"[0-9a-f]{64}|", head or "") else ""}
        if out["auto"]["last_date"] and DATE_RE.fullmatch(out["auto"]["last_date"]):
            out["as_of"] = out["auto"]["last_date"]           # the daily close the automatic picks are based on
    return out


def sync(log=print, src=SRC, dst=DST, auto=AUTO):
    data = build(src, auto)
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
