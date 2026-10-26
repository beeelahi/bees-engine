"""
Telegram alerts for Bee's Dashboard (Python 3.9 stdlib only).

What is sent (each event once, newest data only, at most MAX_PER_RUN per run):
  * bot actions: entry signal, buy, partial profit, sale (paper trading)
  * the BTC 200-day market filter switching the breakout bots on or off
  * new "Possible major top (weekly)" setups, and the other leading setups (daily tops/bottoms, breakouts)
  * channel breakouts / breakdowns (daily and 4h), and coins reaching a channel edge (100 most liquid, once a week)
  * engine problems: sync incomplete, website publish failed, 4h candles disagreeing with daily candles

Security:
  * The bot token and chat id live in ~/BinanceData/.telegram (two lines: token, chat id), created by
    `python3 ~/BinanceData/bin/alerts.py --setup`, which reads the token with hidden input. The file must be
    a regular file owned by this user; it is forced to mode 600; symlinks are refused.
  * The token never appears on a command line or in logs: curl reads the request URL from stdin (--config -).
  * Messages contain only public market data from the dashboard. Nothing is ever read from Telegram except
    during --setup (to find your chat id).
"""
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CRED = os.path.join(ROOT, ".telegram")
EXPORT = os.path.join(ROOT, "web_export", "highlights.json")
SITE = "https://beesells.xyz"
MAX_PER_RUN = 12
FRESH_DAYS = 2            # never alert on anything older than this (no backlog floods)
TOKEN_RE = r"[0-9]{6,12}:[A-Za-z0-9_-]{30,64}"
CHAT_RE = r"-?[0-9]{3,20}"

SCHEMA = "CREATE TABLE IF NOT EXISTS alerts_sent(key TEXT PRIMARY KEY, sent_utc TEXT, text TEXT)"


def read_cred():
    if not os.path.exists(CRED):
        return None
    if os.path.islink(CRED):
        raise RuntimeError("alerts: .telegram must be a regular file, not a link")
    st = os.stat(CRED)
    if st.st_uid != os.getuid():
        raise RuntimeError("alerts: .telegram is not owned by this user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(CRED, 0o600)
    with open(CRED) as f:
        lines = [x.strip() for x in f.read().splitlines() if x.strip()]
    if len(lines) != 2 or not re.fullmatch(TOKEN_RE, lines[0]) or not re.fullmatch(CHAT_RE, lines[1]):
        raise RuntimeError("alerts: .telegram must hold exactly two lines: bot token, chat id")
    return lines[0], lines[1]


def _curl(url, payload=None):
    """POST JSON (or GET) with the URL passed on stdin so the token is never on the command line."""
    cfg = 'url = "%s"\n' % url
    args = ["curl", "-sS", "--max-time", "20", "--config", "-"]
    if payload is not None:
        args += ["-H", "Content-Type: application/json", "--data-binary", "@" + payload]
    p = subprocess.run(args, input=cfg, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"alerts: network error (curl {p.returncode})")
    try:
        return json.loads(p.stdout or "{}")
    except ValueError:
        raise RuntimeError("alerts: unexpected reply from Telegram")


def send(token, chat, text):
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"chat_id": chat, "text": text[:3900], "disable_web_page_preview": True}, f)
        path = f.name
    try:
        os.chmod(path, 0o600)
        r = _curl(f"https://api.telegram.org/bot{token}/sendMessage", path)
    finally:
        os.unlink(path)
    if not r.get("ok"):
        raise RuntimeError("alerts: Telegram refused the message (%s)" % str(r.get("description", ""))[:80])


def _px(v):
    if v is None:
        return "-"
    a = abs(v)
    return f"{v:,.2f}" if a >= 100 else f"{v:.4f}" if a >= 1 else f"{v:.6g}"


def events(con, today):
    """(key, text) for every alert-worthy event in the latest export."""
    if not os.path.exists(EXPORT):
        return []
    with open(EXPORT) as f:
        hl = json.load(f)
    cut = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=FRESH_DAYS)).strftime("%Y-%m-%d")
    out = []
    cols = hl.get("columns", [])
    for row in hl.get("rows", []):
        h = dict(zip(cols, row))
        kind, day, sym = h.get("kind", ""), h.get("period_start", ""), h.get("symbol", "")
        if day < cut or not (kind.startswith("bot_") or kind == "btc_regime"):
            continue
        try:
            d = json.loads(h.get("detail") or "{}")
        except ValueError:
            d = {}
        name, coin = d.get("bot_name", "Bot"), sym.replace("USDT", "")
        if kind == "btc_regime":
            text = ("Market filter ON: BTC closed above its 200-day average. Breakout bots may buy."
                    if d.get("on") else "Market filter OFF: BTC closed below its 200-day average. Breakout bots stop buying.")
        elif kind == "bot_signal":
            text = f"{name}: entry signal on {coin} (close {_px(d.get('ref_close'))}). Buys at the next open, stop {d.get('stop_dist_pct', '-')}% below. Paper trade."
        elif kind == "bot_buy":
            text = f"{name}: bought {coin} at {_px(d.get('entry'))}, stop {_px(d.get('stop'))}, size {d.get('size_pct', '-')}% of the bot. Paper trade."
        elif kind == "bot_partial":
            text = f"{name}: took partial profit on {coin} at {_px(d.get('price'))}. Paper trade."
        elif kind == "bot_sell":
            text = f"{name}: sold {coin} at {_px(d.get('exit'))} ({d.get('reason', '')}), result {d.get('pnl_pct', '-')}%. Paper trade."
        else:
            continue
        out.append((f"{kind}:{d.get('bot', '')}:{sym}:{day}", text))
    S = hl.get("setups") or {}
    kinds = S.get("kinds") or {}
    for x in S.get("rows", []):
        if x.get("date", "") < cut:
            continue
        k = kinds.get(x["kind"], {})
        major = x["kind"] == "top_w_div"
        text = (("MAJOR: " if major else "") + f"{k.get('label', x['kind'])} on {x['symbol'].replace('USDT', '')}"
                f" ({'weekly' if x.get('tf') == '1w' else 'daily'} chart, close {_px(x.get('close'))}); invalid beyond {_px(x.get('stop'))}."
                + (f" Measured target {_px(x.get('target'))}." if x.get("target") else "") + f" Liquidity #{x.get('liq_rank')}. Not advice.")
        out.append((f"setup:{x['kind']}:{x['symbol']}:{x['date']}", text))
    C = hl.get("channels") or {}
    week = datetime.strptime(today, "%Y-%m-%d").strftime("%G-W%V")
    for x in C.get("rows", []):
        coin, tf = x["symbol"].replace("USDT", ""), ("4h" if x.get("tf") == "4h" else "daily")
        lines = f"bottom {_px(x.get('lo1'))}, top {_px(x.get('hi1'))}"
        if x["status"] in ("breakout", "breakdown") and (x.get("ago") or 0) <= 1:
            up = x["status"] == "breakout"
            text = (f"Channel {'BREAKOUT' if up else 'BREAKDOWN'} on {coin} ({tf} chart): closed {'above the top' if up else 'below the bottom'} "
                    f"line {_px(x.get('level'))} on heavy volume. Channel {lines}. Liquidity #{x.get('liq_rank')}. Check the chart. Not advice.")
            out.append((f"chan:{x['status']}:{x['symbol']}:{x.get('tf')}:{x.get('event_t')}", text))
        elif x["status"] in ("near_bottom", "near_top") and (x.get("liq_rank") or 999) <= 100:
            text = (f"{coin} is near its channel {'bottom' if x['status'] == 'near_bottom' else 'top'} ({tf} chart, close {_px(x.get('close'))}): "
                    f"{lines}, {x.get('touches', ['-', '-'])[0]}/{x.get('touches', ['-', '-'])[1]} touches. Edges are decision points: watch for a bounce or a break. Not advice.")
            out.append((f"chan:{x['status']}:{x['symbol']}:{x.get('tf')}:{week}", text))
    return out


def run(con, log=print, problems=()):
    if os.path.exists(os.path.join(ROOT, ".no_publish")):
        return 0                                  # not the live machine: the publisher sends the alerts
    cred = read_cred()
    if not cred:
        return 0
    con.execute(SCHEMA)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sent = {k for (k,) in con.execute("SELECT key FROM alerts_sent")}
    todo = [(k, t) for k, t in events(con, today) if k not in sent]
    todo += [(f"problem:{p}:{today}", f"Dashboard problem: {p}") for p in problems if f"problem:{p}:{today}" not in sent]
    n = 0
    lines = []
    for k, t in todo[:MAX_PER_RUN]:
        lines.append("• " + t)
        con.execute("INSERT OR IGNORE INTO alerts_sent VALUES(?,?,?)", (k, stamp, t))
    if lines:
        more = len(todo) - MAX_PER_RUN
        msg = "Bee's Dashboard\n" + "\n".join(lines) + (f"\n…and {more} more on the site." if more > 0 else "") + f"\n{SITE}"
        send(*cred, msg)
        n = len(lines)
    con.commit()
    return n


def setup():
    """Interactive: save the bot token (hidden input), find the chat id, send a test message."""
    import getpass
    print("1) In Telegram, open @BotFather, send /newbot and follow the steps. Copy the token it gives you.")
    print("2) Open your new bot in Telegram and press Start (or send it any message).")
    tok = getpass.getpass("Paste the bot token here (hidden, press Enter): ").strip()
    if not re.fullmatch(TOKEN_RE, tok):
        print("That does not look like a Telegram bot token. Nothing was saved.")
        return 1
    r = _curl(f"https://api.telegram.org/bot{tok}/getUpdates")
    chats = {}
    for u in r.get("result", []):
        m = u.get("message") or u.get("my_chat_member") or {}
        c = (m.get("chat") or {})
        if c.get("type") == "private" and c.get("id") is not None:
            chats[str(c["id"])] = (c.get("first_name") or "") + (" @" + c["username"] if c.get("username") else "")
    if not chats:
        print("No message found. Open your bot in Telegram, press Start, then run this again. Nothing was saved.")
        return 1
    if len(chats) > 1:
        print("More than one person has messaged this bot:", "; ".join(chats.values()))
        print("For safety, create a fresh bot with @BotFather and run this again. Nothing was saved.")
        return 1
    chat, who = next(iter(chats.items()))
    if input(f"Send alerts to the Telegram account '{who}'? Type yes: ").strip().lower() != "yes":
        print("Nothing was saved.")
        return 1
    old = os.umask(0o077)
    try:
        if os.path.islink(CRED):
            print("Refusing: ~/BinanceData/.telegram is a link.")
            return 1
        fd = os.open(CRED, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tok + "\n" + chat + "\n")
        os.chmod(CRED, 0o600)
    finally:
        os.umask(old)
    send(tok, chat, "Bee's Dashboard alerts are on. You'll get bot trades, market-filter changes and major setups here.")
    print("Saved (private to your user) and a test message was sent. Alerts start with the next hourly run.")
    return 0


if __name__ == "__main__":
    if "--setup" in sys.argv:
        sys.exit(setup())
    print("Usage: python3 ~/BinanceData/bin/alerts.py --setup")
