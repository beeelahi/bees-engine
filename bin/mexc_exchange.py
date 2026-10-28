"""
MEXC spot REST client (API v3) for the market-making bot. Python 3.9 stdlib only.

Same security model as live_exchange.py:
  * credentials in ~/BinanceData/.mexc_live (two lines: access key, secret), private (600), owned by this user,
    symlinks refused, never logged or put on a command line (curl reads URL and headers from stdin);
  * the key must have spot trading ON, withdrawals OFF, and be bound to the server's IP.
Every order carries a client id with the "bm" prefix so the bot only ever manages its own orders.
"""
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import time
import urllib.parse
from decimal import Decimal, ROUND_DOWN, ROUND_UP

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CRED = os.path.join(ROOT, ".mexc_live")
BASE = "https://api.mexc.com"
ACCESS_RE, SECRET_RE = r"mx0v[A-Za-z0-9]{10,64}", r"[A-Za-z0-9]{24,64}"


class MexcError(Exception):
    pass


def read_cred(path=CRED):
    if not os.path.exists(path):
        raise RuntimeError("mexc: no API credentials (run mm_setup.py)")
    if os.path.islink(path):
        raise RuntimeError("mexc: credentials file must not be a link")
    st = os.stat(path)
    if st.st_uid != os.getuid():
        raise RuntimeError("mexc: credentials file is not owned by this user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(path, 0o600)
    with open(path) as f:
        lines = [x.strip() for x in f.read().splitlines() if x.strip()]
    if len(lines) != 2 or not re.fullmatch(ACCESS_RE, lines[0]) or not re.fullmatch(SECRET_RE, lines[1]):
        raise RuntimeError("mexc: credentials file must hold exactly two lines: access key, secret")
    return lines[0], lines[1]


class Mexc:
    def __init__(self, cred=None, transport=None):
        """cred=False: public market data only (paper trading). No key is read and every signed call is refused."""
        self._key, self._secret = (None, None) if cred is False else (cred if cred else read_cred())
        self._t = transport or self._curl
        self._offset = 0
        self._info = {}

    def __repr__(self):
        return "<Mexc spot>"

    def _curl(self, method, url, headers):
        cfg = 'url = "%s"\nrequest = "%s"\n' % (url, method) + "".join('header = "%s: %s"\n' % kv for kv in headers.items())
        p = subprocess.run(["curl", "-sS", "--max-time", "15", "-w", "\n%{http_code}", "--config", "-"],
                           input=cfg, capture_output=True, text=True)
        if p.returncode != 0:
            raise MexcError(f"network error (curl {p.returncode})")
        body, _, code = p.stdout.rpartition("\n")
        return int(code or 0), body

    def _call(self, method, path, params=None, signed=False):
        params = dict(params or {})
        headers = {"Content-Type": "application/json"}
        if signed and not self._key:
            raise MexcError("public-only client: signed calls are disabled")
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self._offset
            params["recvWindow"] = 5000
            q = urllib.parse.urlencode(params)
            q += "&signature=" + hmac.new(self._secret.encode(), q.encode(), hashlib.sha256).hexdigest()
            headers["X-MEXC-APIKEY"] = self._key
        else:
            q = urllib.parse.urlencode(params)
        status, body = self._t(method, BASE + path + ("?" + q if q else ""), headers)
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            raise MexcError(f"unexpected reply ({status})")
        if status != 200 or (isinstance(data, dict) and data.get("code") not in (None, 0, 200) and "msg" in data):
            raise MexcError(f"MEXC error {data.get('code', status) if isinstance(data, dict) else status}: "
                            f"{(data.get('msg', '') if isinstance(data, dict) else '')[:160]}")
        return data

    # public
    def sync_time(self):
        self._offset = int(self._call("GET", "/api/v3/time")["serverTime"] - time.time() * 1000)

    def info(self, symbol):
        if symbol not in self._info:
            s = self._call("GET", "/api/v3/exchangeInfo", {"symbol": symbol})["symbols"][0]
            step = Decimal(str(s.get("baseSizePrecision") or "0")) or Decimal(1).scaleb(-int(s.get("baseAssetPrecision", 8)))
            tick = Decimal(1).scaleb(-int(s.get("quotePrecision", s.get("quoteAssetPrecision", 8))))
            self._info[symbol] = {"base": s["baseAsset"], "quote": s["quoteAsset"], "step": step, "tick": tick,
                                  "min_quote": Decimal(str(s.get("quoteAmountPrecision") or "1")),
                                  "trading": str(s.get("status")) in ("1", "ENABLED", "TRADING") and s.get("isSpotTradingAllowed", True)}
        return self._info[symbol]

    def book(self, symbol, limit=20):
        d = self._call("GET", "/api/v3/depth", {"symbol": symbol, "limit": limit})
        return [(float(p), float(q)) for p, q in d["bids"]], [(float(p), float(q)) for p, q in d["asks"]]

    def trades(self, symbol, limit=1000):
        """Recent public trades: [(time_ms, price, qty, taker_side)] oldest first; taker_side BUY = buyer took an ask."""
        d = self._call("GET", "/api/v3/trades", {"symbol": symbol, "limit": limit})
        out = [(int(t["time"]), float(t["price"]), float(t["qty"]), "SELL" if t.get("isBuyerMaker") else "BUY") for t in d]
        return sorted(out, key=lambda x: x[0])

    def klines(self, symbol, interval, limit=3):
        """Recent candles as [(open_ms, close, close_ms, open, high, low)], oldest first; the last one may still be
        running. MEXC intervals: 60m, 4h, 1d ..."""
        d = self._call("GET", "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        step = {"60m": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}.get(interval)
        return [(int(r[0]), float(r[4]), int(r[0]) + step if step else int(r[6]), float(r[1]), float(r[2]), float(r[3])) for r in d]

    def qty(self, symbol, q):
        st = self.info(symbol)["step"]
        return (Decimal(str(q)) / st).to_integral_value(rounding=ROUND_DOWN) * st

    def px(self, symbol, p, up=False):
        tk = self.info(symbol)["tick"]
        return (Decimal(str(p)) / tk).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * tk

    # signed
    def account(self):
        a = self._call("GET", "/api/v3/account", {}, signed=True)
        if a.get("canWithdraw"):
            raise RuntimeError("mexc: this API key can WITHDRAW. Create a new key with withdrawals disabled.")
        if a.get("canTrade") is False:
            raise RuntimeError("mexc: this API key cannot trade.")
        return a

    def balances(self):
        return {b["asset"]: (float(b["free"]), float(b["locked"])) for b in self.account().get("balances", [])}

    def open_orders(self, symbol):
        return [o for o in self._call("GET", "/api/v3/openOrders", {"symbol": symbol}, signed=True)
                if str(o.get("clientOrderId", "")).startswith("bm")]

    def limit_maker(self, symbol, side, qty, price, client_id):
        """Post-only quote: rejected instead of filled if it would cross the book (never takes, never self-trades)."""
        assert client_id.startswith("bm")
        return self._call("POST", "/api/v3/order", {"symbol": symbol, "side": side, "type": "LIMIT_MAKER",
                                                    "quantity": str(qty), "price": str(price), "newClientOrderId": client_id}, signed=True)

    def limit(self, symbol, side, qty, price, client_id):
        """Plain limit order (may take liquidity). Used only to rebuy at or below the planned rebuy price."""
        assert client_id.startswith("bm")
        return self._call("POST", "/api/v3/order", {"symbol": symbol, "side": side, "type": "LIMIT",
                                                    "quantity": str(qty), "price": str(price), "newClientOrderId": client_id}, signed=True)

    def order(self, symbol, client_id):
        return self._call("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_id}, signed=True)

    def cancel(self, symbol, client_id):
        return self._call("DELETE", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_id}, signed=True)

    def my_trades(self, symbol, start_ms):
        return self._call("GET", "/api/v3/myTrades", {"symbol": symbol, "startTime": start_ms, "limit": 1000}, signed=True)
