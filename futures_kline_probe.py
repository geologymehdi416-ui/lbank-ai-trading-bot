#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
futures_kline_probe.py - finds which LBank futures kline endpoint actually answers.
It tries candidate paths and symbols, prints the HTTP status and a short raw response for each.
Run it once and send the output; the working path then goes into signal_scanner.py.
Nothing is traded; this only sends GET requests.
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://lbkperp.lbank.com"
PATHS = [
    "/cfd/openApi/v1/pub/kline",
    "/cfd/openApi/v1/pub/candle",
    "/cfd/openApi/v1/pub/klines",
    "/cfd/openApi/v1/pub/marketKline",
    "/cfd/openApi/v1/pub/marketData",
]
SYMBOLS = ["BTCUSDT", "btcusdt", "BTC_USDT", "btc_usdt"]


def get(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "probe/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # network or DNS error
        return None, str(e)


def main():
    print("BASE:", BASE)
    for path in PATHS:
        for sym in SYMBOLS:
            status, body = get(BASE + path, {"symbol": sym, "interval": "1m", "limit": 5})
            print("\n%s  symbol=%s  -> status=%s" % (path, sym, status))
            print((body or "")[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
