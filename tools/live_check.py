"""
Daily check of the LIVE website's data (run from the private ops repository; Python 3.9 stdlib only).

    python3 tools/live_check.py https://beesells.xyz

Fails (exit 1) and lists every problem when the published data is stale, missing, malformed or inconsistent:
daily package built within 36 hours and data through yesterday or the day before; live prices refreshed within
45 minutes for 90%+ of the universe; 4-hour charts within 6 hours, 1-hour charts within 3 hours (ZIG included);
every core file parses; the bots' integrity check is "ok"; the page carries its security policy; the privacy and
terms pages answer. Prints a short summary either way.
"""
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

UA = {"User-Agent": "bees-live-check/1.0", "Cache-Control": "no-cache"}


def get(base, path):
    req = urllib.request.Request(base.rstrip("/") + "/" + path + ("&" if "?" in path else "?") + "t=" + str(int(datetime.now().timestamp())), headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read().decode("utf-8")


def ts(s):
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def main(base):
    now, probs, notes = datetime.now(timezone.utc), [], []
    J = {}
    for f in ("overview", "screener", "highlights", "signals", "bot", "prices", "moves", "shortlist", "h4/index", "h1/index", "pairs/index"):
        try:
            code, body = get(base, f"data/{f}.json")
            J[f] = json.loads(body)
        except Exception as e:
            probs.append(f"data/{f}.json: {str(e)[:120]}")
    ov = J.get("overview") or {}
    if ov:
        age = (now - ts(ov["generated_utc"])).total_seconds() / 3600
        notes.append(f"daily package built {age:.1f} h ago, data through {ov.get('last_date')}")
        if age > 36:
            probs.append(f"daily package is {age:.0f} hours old")
        if ov.get("last_date", "") < (now - timedelta(days=2)).strftime("%Y-%m-%d"):
            probs.append(f"daily data only through {ov.get('last_date')}")
        n = (ov.get("counts") or {}).get("pairs_filtered", 0)
        notes.append(f"{n} pairs in the universe")
        if n < 100:
            probs.append(f"only {n} pairs in the universe")
        st = ov.get("status") or {}
        if st.get("last_run_ok") is False:
            probs.append("the last engine run reported a problem")
    px = J.get("prices")
    if px:
        age = (now - ts(px["as_of"])).total_seconds() / 60
        cov = len(px.get("rows", {})) / max(1, (ov.get("counts") or {}).get("pairs_filtered", 1))
        notes.append(f"live prices {age:.0f} min old for {len(px.get('rows', {}))} pairs")
        if age > 45:
            probs.append(f"live prices are {age:.0f} minutes old")
        if cov < 0.9:
            probs.append(f"live prices cover only {100 * cov:.0f}% of the universe")
        if "ZIGUSDT" not in px.get("rows", {}):
            probs.append("no live price for ZIG")
    for sub, max_h, bar in (("h4", 6, 4), ("h1", 3, 1)):
        ix = J.get(f"{sub}/index")
        if not ix:
            continue
        if "ZIGUSDT" not in ix:
            probs.append(f"ZIG missing from the {sub} charts")
        try:
            sym = "ZIGUSDT" if "ZIGUSDT" in ix else next(iter(ix))
            _, body = get(base, f"data/{sub}/{ix[sym]}")
            rows = json.loads(body)["pairs"][sym]
            last_close = datetime.fromtimestamp(rows[-1][0] + bar * 3600, timezone.utc)
            age = (now - last_close).total_seconds() / 3600
            notes.append(f"{sub} charts: {len(ix)} pairs, last {sym} candle closed {age:.1f} h ago ({len(rows)} candles)")
            if age > max_h:
                probs.append(f"{sub} charts are {age:.1f} hours behind")
        except Exception as e:
            probs.append(f"{sub} chart file: {str(e)[:120]}")
    bot = J.get("bot") or {}
    integ = (bot.get("portfolio") or {}).get("integrity")
    if bot and integ not in (None, "ok"):
        probs.append(f"bot integrity check: {integ}")
    try:
        code, page = get(base, "index.html")
        if "Content-Security-Policy" not in page:
            probs.append("the page has no Content-Security-Policy")
        if 'id="hlNav"' not in page:
            probs.append("the page looks like an old version")
    except Exception as e:
        probs.append(f"index.html: {str(e)[:120]}")
    for f in ("privacy.html", "terms.html"):
        try:
            get(base, f)
        except Exception as e:
            probs.append(f"{f}: {str(e)[:80]}")
    print("Live data check, " + now.strftime("%Y-%m-%d %H:%M UTC"))
    for n in notes:
        print("  " + n)
    if probs:
        print(f"\n{len(probs)} PROBLEM(S):")
        for p in probs:
            print("  - " + p)
        return 1
    print("\nall good")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "https://beesells.xyz"))
