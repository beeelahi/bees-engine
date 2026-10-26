"""
Funding-rate carry study on the engine's own funding history (read-only; run on the server).

Trade: buy the coin on spot and short the same amount on the USDT-M perpetual. Price moves cancel; the position
collects funding every 8 hours while funding is positive (pays it while negative).
Rule tested: enter when the trailing 7-day average funding, annualised, is at least ENTER; exit when the trailing
3-day average falls below EXIT. Costs per round trip: spot 0.1% x 2, futures 0.05% x 2, 0.1% slippage = 0.4%.
Returns are on the capital IN the trade (spot leg + futures margin at 1x = 2x the notional, so the yield on
capital is HALF the funding yield on notional). Reported per year, for the 30 most liquid perpetuals.

    python3 bin/carry_study.py            (prints a plain-text report)
"""
import os
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB = os.path.join(ROOT, "data", "binance.db")
COST = 0.004
RULES = ((10, 3), (15, 5), (25, 8), (40, 10))
TOP = 30


def load(con):
    liq = []
    for (s,) in con.execute("SELECT DISTINCT symbol FROM funding"):
        v = con.execute("SELECT AVG(volume_usdt) FROM (SELECT volume_usdt FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 30)", (s,)).fetchone()[0] or 0
        liq.append((v, s))
    liq.sort(reverse=True)
    pick = [s for _, s in liq[:TOP]]
    rows = {s: con.execute("SELECT time_ms, rate FROM funding WHERE symbol=? ORDER BY time_ms", (s,)).fetchall() for s in pick}
    return rows


def simulate(xs, enter, exit_):
    """Returns list of (year, earned_fraction_on_notional, periods_in, trips) per year."""
    by = defaultdict(lambda: [0.0, 0, 0, 0])            # earned, periods in, trips, periods total
    pos = False
    for i in range(21, len(xs)):
        y = datetime.fromtimestamp(xs[i][0] / 1000, tz=timezone.utc).year
        a7 = sum(r for _, r in xs[i - 21:i]) / 21 * 3 * 365 * 100
        a3 = sum(r for _, r in xs[i - 9:i]) / 9 * 3 * 365 * 100
        if not pos and a7 >= enter:
            pos = True
            by[y][0] -= COST
            by[y][2] += 1
        elif pos and a3 < exit_:
            pos = False
        if pos:
            by[y][0] += xs[i][1]
            by[y][1] += 1
        by[y][3] += 1
    return by


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = load(con)
    print(f"Funding carry study, {len(rows)} most liquid perpetuals, funding history from "
          f"{min(datetime.fromtimestamp(x[0][0] / 1000, tz=timezone.utc).date() for x in rows.values() if x)} "
          f"to {max(datetime.fromtimestamp(x[-1][0] / 1000, tz=timezone.utc).date() for x in rows.values() if x)}")
    print("Yield = on capital (spot + 1x futures margin), costs included. Portfolio = equal weight across coins, idle cash earns 0.\n")
    for enter, exit_ in RULES:
        per_year = defaultdict(list)
        in_mkt = defaultdict(list)
        for s, xs in rows.items():
            if len(xs) < 60:
                continue
            for y, (earned, pin, trips, tot) in simulate(xs, enter, exit_).items():
                if tot >= 300:                          # coin traded most of that year
                    per_year[y].append(earned / 2 * 100)    # on capital = half the notional yield
                    in_mkt[y].append(100 * pin / tot)
        print(f"Enter at {enter}%/yr funding, exit below {exit_}%:")
        for y in sorted(per_year):
            v = per_year[y]
            print(f"  {y}: coins {len(v):2d} | portfolio {st.mean(v):6.1f}% | median coin {st.median(v):6.1f}% | "
                  f"best {max(v):6.1f}% | worst {min(v):6.1f}% | time in trade {st.mean(in_mkt[y]):4.0f}%")
        print()
    print("Risks not in these numbers: exchange failure (both legs on one exchange), the short leg's margin needs a "
          "buffer in sharp rallies (liquidation risk at 1x is low but not zero), and funding can flip negative quickly.")


if __name__ == "__main__":
    main()
