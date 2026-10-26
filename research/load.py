import csv, gzip
def load(path="daily_v1.csv.gz", pairs="pairs_v1.csv"):
    tokens = {r["symbol"]: r["token"] for r in csv.DictReader(open(pairs))}
    rows = []
    with gzip.open(path, "rt") as f:
        rd = csv.reader(f); next(rd)
        for s, d, o, h, l, c, v, tb, n in rd:
            rows.append((s, d, float(o), float(h), float(l), float(c), float(v or 0)))
    return rows, tokens
