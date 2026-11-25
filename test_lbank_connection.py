#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_lbank_connection.py - read-only check of the LBank endpoints the scanner needs.
Run it on your own machine / Termux and send the output.
"""
import json
import sys

import requests

TESTS = [
    ("SPOT kline (api.lbank.info)", "https://api.lbank.info/v2/kline.do",
     {"symbol": "btc_usdt", "size": 5, "type": "minute15", "time": 0}),
    ("SPOT kline (api.lbkex.com)", "https://api.lbkex.com/v2/kline.do",
     {"symbol": "btc_usdt", "size": 5, "type": "minute15", "time": 0}),
    ("PERP instrument", "https://lbkperp.lbank.com/cfd/openApi/v1/pub/instrument",
     {"productGroup": "SwapU"}),
    ("PERP marketData", "https://lbkperp.lbank.com/cfd/openApi/v1/pub/marketData",
     {"productGroup": "SwapU"}),
]


def main():
    for name, url, params in TESTS:
        print("=" * 60)
        print(name)
        try:
            r = requests.get(url, params=params, timeout=15,
                             headers={"User-Agent": "lbank-test/1.0"})
            print("HTTP", r.status_code)
            text = r.text[:300].replace("\n", " ")
            print(text)
            try:
                data = r.json()
                if isinstance(data, dict):
                    print("keys:", list(data.keys())[:8])
                    rows = data.get("data") or data.get("list") or []
                    if isinstance(rows, list):
                        print("rows:", len(rows))
            except ValueError:
                print("not JSON")
        except requests.RequestException as e:
            print("ERROR:", e)
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
