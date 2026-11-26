#!/usr/bin/env python3
"""Binance USDT-margined futures connectivity + account check (read-only).
Reads key/secret from ~/.binance/credentials (mode 600). Never prints secrets.
"""
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

BASE = "https://fapi.binance.com"
CREDS = os.path.expanduser("~/.binance/credentials")


def load_creds():
    creds = {}
    with open(CREDS) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip()
    return creds["BINANCE_API_KEY"], creds["BINANCE_API_SECRET"]


def public(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read())


def signed(key, secret, path, params=None):
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE}{path}?{qs}&signature={sig}"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": key})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main():
    key, secret = load_creds()
    print("ping:", public("/fapi/v1/ping"))
    server = public("/fapi/v1/time")
    print("serverTime:", server, "| local skew ms:",
          int(time.time() * 1000) - server["serverTime"])
    try:
        acct = signed(key, secret, "/fapi/v2/account")
    except urllib.error.HTTPError as e:
        print("ACCOUNT ERROR", e.code, e.read().decode())
        return
    print("canTrade:", acct.get("canTrade"),
          "| canDeposit:", acct.get("canDeposit"),
          "| canWithdraw:", acct.get("canWithdraw"))
    print("totalWalletBalance:", acct.get("totalWalletBalance"),
          "| totalMarginBalance:", acct.get("totalMarginBalance"),
          "| availableBalance:", acct.get("totalAvailableBalance"))
    for a in acct.get("assets", []):
        if float(a.get("walletBalance", 0)) != 0:
            print("asset:", a["asset"], "wallet:", a["walletBalance"],
                  "available:", a["availableBalance"],
                  "unrealizedPNL:", a.get("unrealizedProfit"))
    open_pos = [p for p in acct.get("positions", [])
                if float(p.get("positionAmt", 0)) != 0]
    print("open positions:", len(open_pos))
    for p in open_pos:
        print(" ", p["symbol"], p["positionAmt"], "entry:", p["entryPrice"],
              "unrealized:", p["unrealizedProfit"], "lev:", p["leverage"])
    try:
        bal = signed(key, secret, "/fapi/v2/balance")
        usdt = [b for b in bal if b["asset"] == "USDT"]
        print("USDT futures balance:", usdt)
    except urllib.error.HTTPError as e:
        print("BALANCE ERROR", e.code, e.read().decode())


if __name__ == "__main__":
    main()
