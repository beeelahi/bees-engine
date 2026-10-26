"""
Binance spot REST client for live trading (Python 3.9 stdlib only).

Security:
  * Credentials live in ~/BinanceData/.binance_live (two lines: API key, secret), created by the owner with
    `python3 ~/BinanceData/bin/live_setup.py` (hidden input). Regular file owned by this user, forced to 600,
    symlinks refused. The secret is used only to sign requests in memory; it is never logged, printed,
    written elsewhere or put on a command line.
  * Requests go through curl with the URL and the API-key header passed on stdin (--config -), so neither
    appears in the process list.
  * The key must be created with "Enable Spot & Margin Trading" ON and "Enable Withdrawals" OFF, restricted
    to the server's IP. account() refuses to continue if the key reports withdrawals enabled.
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
from decimal import Decimal, ROUND_DOWN

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CRED = os.path.join(ROOT, ".binance_live")
HOSTS = {"live": "https://api.binance.com", "testnet": "https://testnet.binance.vision"}
KEY_RE = r"[A-Za-z0-9]{64}"


class ExchangeError(Exception):
    def __init__(self, code, msg, status=None):
        super().__init__(f"Binance error {code}: {msg}")
        self.code, self.msg, self.status = code, msg, status


def read_cred(path=CRED):
    if not os.path.exists(path):
        raise RuntimeError("live: no API credentials (run live_setup.py)")
    if os.path.islink(path):
        raise RuntimeError("live: credentials file must not be a link")
    st = os.stat(path)
    if st.st_uid != os.getuid():
        raise RuntimeError("live: credentials file is not owned by this user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(path, 0o600)
    with open(path) as f:
        lines = [x.strip() for x in f.read().splitlines() if x.strip()]
    if len(lines) != 2 or not all(re.fullmatch(KEY_RE, x) for x in lines):
        raise RuntimeError("live: credentials file must hold exactly two lines: API key, secret")
    return lines[0], lines[1]


def _dec(x):
    return Decimal(str(x))


class Spot:
    def __init__(self, mode, cred=None, transport=None):
        if mode not in HOSTS:
            raise ValueError("mode must be live or testnet")
        self.mode, self.base = mode, HOSTS[mode]
        self._key, self._secret = cred if cred else read_cred()
        self._t = transport or self._curl
        self._offset = 0
        self._filters = {}

    def __repr__(self):                        # never show credentials
        return f"<Spot {self.mode}>"

    # ------------------------------------------------------------ transport
    def _curl(self, method, url, headers):
        cfg = 'url = "%s"\nrequest = "%s"\n' % (url, method) + "".join('header = "%s: %s"\n' % kv for kv in headers.items())
        p = subprocess.run(["curl", "-sS", "--max-time", "20", "-w", "\n%{http_code}", "--config", "-"],
                           input=cfg, capture_output=True, text=True)
        if p.returncode != 0:
            raise ExchangeError("network", f"curl exit {p.returncode}")
        body, _, code = p.stdout.rpartition("\n")
        return int(code or 0), body

    def _call(self, method, path, params=None, signed=False):
        params = dict(params or {})
        headers = {}
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self._offset
            params["recvWindow"] = 5000
            q = urllib.parse.urlencode(params)
            sig = hmac.new(self._secret.encode(), q.encode(), hashlib.sha256).hexdigest()
            q += "&signature=" + sig
            headers["X-MBX-APIKEY"] = self._key
        else:
            q = urllib.parse.urlencode(params)
        url = self.base + path + ("?" + q if q else "")
        status, body = self._t(method, url, headers)
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            raise ExchangeError("bad_json", body[:120], status)
        if status != 200 or (isinstance(data, dict) and "code" in data and data.get("code", 0) < 0):
            raise ExchangeError(data.get("code", status) if isinstance(data, dict) else status,
                                (data.get("msg", "") if isinstance(data, dict) else str(data))[:200], status)
        return data

    # ------------------------------------------------------------ public
    def sync_time(self):
        t = self._call("GET", "/api/v3/time")["serverTime"]
        self._offset = int(t - time.time() * 1000)
        return self._offset

    def filters(self, symbol):
        if symbol not in self._filters:
            info = self._call("GET", "/api/v3/exchangeInfo", {"symbol": symbol})["symbols"][0]
            f = {x["filterType"]: x for x in info["filters"]}
            self._filters[symbol] = {
                "status": info["status"],
                "step": _dec(f["LOT_SIZE"]["stepSize"]), "min_qty": _dec(f["LOT_SIZE"]["minQty"]),
                "tick": _dec(f["PRICE_FILTER"]["tickSize"]),
                "min_notional": _dec((f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}).get("minNotional", "5")),
                "oco": bool(info.get("ocoAllowed")), "base": info["baseAsset"], "quote": info["quoteAsset"]}
        return self._filters[symbol]

    def price(self, symbol):
        return float(self._call("GET", "/api/v3/ticker/price", {"symbol": symbol})["price"])

    # ------------------------------------------------------------ rounding to exchange rules
    def qty(self, symbol, q):
        f = self.filters(symbol)
        v = (_dec(q) / f["step"]).to_integral_value(rounding=ROUND_DOWN) * f["step"]
        return v if v >= f["min_qty"] else Decimal(0)

    def px(self, symbol, p, up=False):
        f = self.filters(symbol)
        from decimal import ROUND_UP
        return (_dec(p) / f["tick"]).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * f["tick"]

    # ------------------------------------------------------------ signed
    def account(self):
        a = self._call("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)
        if a.get("canWithdraw") and self.mode == "live":
            raise RuntimeError("live: this API key can WITHDRAW. Create a new key with withdrawals disabled.")
        if not a.get("canTrade"):
            raise RuntimeError("live: this API key cannot trade.")
        return a

    def balances(self):
        return {b["asset"]: (float(b["free"]), float(b["locked"])) for b in self.account()["balances"]}

    def open_orders(self, symbol=None):
        return self._call("GET", "/api/v3/openOrders", {"symbol": symbol} if symbol else {}, signed=True)

    def order(self, symbol, client_id):
        return self._call("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_id}, signed=True)

    def market_buy_quote(self, symbol, usdt, client_id):
        return self._call("POST", "/api/v3/order", {"symbol": symbol, "side": "BUY", "type": "MARKET",
                                                    "quoteOrderQty": str(_dec(usdt).quantize(Decimal("0.01"), rounding=ROUND_DOWN)),
                                                    "newClientOrderId": client_id, "newOrderRespType": "FULL",
                                                    "selfTradePreventionMode": "EXPIRE_BOTH"}, signed=True)

    def market_sell(self, symbol, qty, client_id):
        return self._call("POST", "/api/v3/order", {"symbol": symbol, "side": "SELL", "type": "MARKET",
                                                    "quantity": str(qty), "newClientOrderId": client_id,
                                                    "newOrderRespType": "FULL", "selfTradePreventionMode": "EXPIRE_BOTH"}, signed=True)

    def stop_sell(self, symbol, qty, stop, limit, client_id):
        return self._call("POST", "/api/v3/order", {"symbol": symbol, "side": "SELL", "type": "STOP_LOSS_LIMIT",
                                                    "timeInForce": "GTC", "quantity": str(qty), "stopPrice": str(stop),
                                                    "price": str(limit), "newClientOrderId": client_id,
                                                    "selfTradePreventionMode": "EXPIRE_BOTH"}, signed=True)

    def oco_sell(self, symbol, qty, take, stop, stop_limit, list_id):
        """Take-profit limit above + stop-limit below, one cancels the other (exchange-side)."""
        return self._call("POST", "/api/v3/orderList/oco", {
            "symbol": symbol, "side": "SELL", "quantity": str(qty), "listClientOrderId": list_id,
            "aboveType": "LIMIT_MAKER", "abovePrice": str(take), "aboveClientOrderId": list_id + "a",
            "belowType": "STOP_LOSS_LIMIT", "belowStopPrice": str(stop), "belowPrice": str(stop_limit),
            "belowTimeInForce": "GTC", "belowClientOrderId": list_id + "b",
            "selfTradePreventionMode": "EXPIRE_BOTH"}, signed=True)

    def cancel(self, symbol, client_id):
        return self._call("DELETE", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_id}, signed=True)

    def cancel_list(self, symbol, list_id):
        return self._call("DELETE", "/api/v3/orderList", {"symbol": symbol, "listClientOrderId": list_id}, signed=True)

    def cancel_all(self, symbol):
        return self._call("DELETE", "/api/v3/openOrders", {"symbol": symbol}, signed=True)
