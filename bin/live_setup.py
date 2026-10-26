"""
Save Binance API keys for the live engine, safely (run it yourself in Terminal; never paste keys in any chat).

  python3 ~/BinanceData/bin/live_setup.py testnet     # keys from testnet.binance.vision (fake money)
  python3 ~/BinanceData/bin/live_setup.py live        # real keys: trading ON, withdrawals OFF, IP-restricted

The key and secret are read with hidden input, checked against Binance (the key must be able to trade and,
for live, must NOT be able to withdraw), and saved to a private file (mode 600) only if the check passes.
"""
import getpass
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import live_exchange as X


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("testnet", "live"):
        print(__doc__)
        return 1
    path = X.CRED if mode == "live" else os.path.join(X.ROOT, ".binance_testnet")
    if mode == "live":
        print("Live keys: in Binance > API Management create a key with ONLY 'Enable Spot & Margin Trading' ticked,")
        print("withdrawals NOT ticked, and 'Restrict access to trusted IPs only' set to this machine's public IP.")
    key = getpass.getpass("API key (hidden): ").strip()
    sec = getpass.getpass("Secret key (hidden): ").strip()
    if not (re.fullmatch(X.KEY_RE, key) and re.fullmatch(X.KEY_RE, sec)):
        print("Those don't look like Binance keys (64 letters/digits each). Nothing was saved.")
        return 1
    ex = X.Spot(mode, (key, sec))
    try:
        ex.sync_time()
        a = ex.account()                       # refuses keys that can withdraw (live)
    except Exception as e:
        print("Binance rejected the check:", str(e)[:200], "\nNothing was saved.")
        return 1
    if mode == "live" and a.get("canWithdraw"):
        print("This key can withdraw. Create a new one with withdrawals disabled. Nothing was saved.")
        return 1
    if os.path.islink(path):
        print("Refusing: the credentials path is a link.")
        return 1
    old = os.umask(0o077)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key + "\n" + sec + "\n")
        os.chmod(path, 0o600)
    finally:
        os.umask(old)
    usdt = next((b["free"] for b in a.get("balances", []) if b["asset"] == "USDT"), "0")
    print(f"Saved privately. Key can trade: {a.get('canTrade')}, can withdraw: {a.get('canWithdraw')}. Free USDT: {float(usdt):,.2f}")
    print("Nothing trades until ~/BinanceData/live.json says so.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
