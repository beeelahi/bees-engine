"""
When was each entry first listed (Python 3.9 stdlib only)? Every list on the website that is rebuilt each day
(channel setups, head and shoulders, cup and handle, double tops / bottoms, ...) gets an "added" date per row: the
day the entry first appeared, kept while it stays listed without a break, reset if it drops out for more than
GAP_DAYS and comes back later (then it is a new entry).

Table first_seen(section, key, first_date, last_date). Seed: an entry that is new to the table but was already posted
to the highlights feed (highlight_log) recently takes the date of that post, so entries listed before this module
existed keep their real date.
"""
from datetime import date, timedelta

GAP_DAYS = 3


def _d(s):
    return date.fromisoformat(s[:10])


def stamp(con, section, rows, key, today, seed=None, field="added"):
    """Sets row[field] = first date for every row. key(row) -> str; seed(row) -> 'YYYY-MM-DD' or None."""
    con.execute("CREATE TABLE IF NOT EXISTS first_seen(section TEXT, key TEXT, first_date TEXT, last_date TEXT, "
                "PRIMARY KEY(section, key))")
    have = {k: (f, l) for k, f, l in con.execute("SELECT key, first_date, last_date FROM first_seen WHERE section=?", (section,))}
    cut = (_d(today) - timedelta(days=GAP_DAYS)).isoformat()
    for r in rows:
        k = key(r)
        old = have.get(k)
        if old and old[1] >= cut:
            first = old[0]
        else:
            first = None
            if not old and seed:
                s = seed(r)
                if s and s <= today:
                    first = s
            first = first or today
        r[field] = first
        con.execute("INSERT OR REPLACE INTO first_seen VALUES(?,?,?,?)", (section, k, first, today))
    con.commit()
    return rows


def feed_date(con, kind, symbol, direction=None, since_days=60, today=None, latest=False):
    """Date of this symbol's post of `kind` in the highlights feed (earliest, or latest), within since_days."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='highlight_log'").fetchone():
        return None
    lo = (_d(today) - timedelta(days=since_days)).isoformat() if today else "0000"
    q = "SELECT %s(period_start) FROM highlight_log WHERE kind=? AND symbol=? AND period_start>=?" % ("MAX" if latest else "MIN")
    args = [kind, symbol, lo]
    if direction:
        q += " AND direction=?"
        args.append(direction)
    r = con.execute(q, args).fetchone()
    return r[0] if r and r[0] else None
