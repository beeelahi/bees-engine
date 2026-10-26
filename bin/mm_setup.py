"""
Save MEXC API keys for the market-making bot (run it yourself on the server; never paste keys in any chat).

  python3 ~/BinanceData/bin/mm_setup.py

Create the key on MEXC with Spot trading ON, withdrawals OFF, and bound to the server's IP address.
The key and secret are read with hidden input, checked against MEXC (must trade, must NOT withdraw),
and saved to ~/BinanceData/.mexc_live (mode 600) only if the check passes.
"""
import getpass
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mexc_exchange as X


def main():
    print(__doc__)
    key = getpass.getpass("MEXC access key (hidden): ").strip()
    sec = getpass.getpass("MEXC secret key (hidden): ").strip()
    if not (re.fullmatch(X.ACCESS_RE, key) and re.fullmatch(X.SECRET_RE, sec)):
        print("Those don't look like MEXC API keys. Nothing was saved.")
        return 1
    ex = X.Mexc((key, sec))
    try:
        ex.sync_time()
        a = ex.account()                     # refuses keys that can withdraw
    except Exception as e:
        print("MEXC rejected the check:", str(e)[:200], "\nNothing was saved.")
        return 1
    if os.path.islink(X.CRED):
        print("Refusing: the credentials path is a link.")
        return 1
    old = os.umask(0o077)
    try:
        fd = os.open(X.CRED, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key + "\n" + sec + "\n")
        os.chmod(X.CRED, 0o600)
    finally:
        os.umask(old)
    bal = {b["asset"]: b["free"] for b in a.get("balances", []) if float(b.get("free") or 0) > 0}
    print(f"Saved privately. Can trade: {a.get('canTrade')}, can withdraw: {a.get('canWithdraw')}. Free: {bal}")
    print("Nothing trades until ~/BinanceData/mm.json has \"enabled\": true.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
