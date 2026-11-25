#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_futures_data.py - two targeted fixes for futures_smc_scanner/data_client.py

1) A single bad or gapped candle no longer discards the whole coin.
   Only "insufficient candles" is fatal now; gaps and irregular timestamps become warnings.
2) Remove the repeated "turnover unit" warning from every coin (it is noise until verified).

Usage (from the folder that contains futures_smc_scanner/):
  python patch_futures_data.py futures_smc_scanner/data_client.py
A backup is written next to the file as data_client.py.bak.
"""
import shutil
import sys

OLD_RAISE = (
    "        if issues:\n"
    "            raise ExchangeDataError(\"; \".join(issues))\n"
    "\n"
    "        return result, issues\n"
)
NEW_RAISE = (
    "        fatal = [i for i in issues if \"insufficient\" in i.lower()]\n"
    "        if fatal:\n"
    "            raise ExchangeDataError(\"; \".join(fatal))\n"
    "\n"
    "        return result, issues\n"
)

OLD_WARN = (
    "        if hasattr(ctx, \"warnings\"):\n"
    "            if ctx.warnings is None:\n"
    "                ctx.warnings = []\n"
    "            ctx.warnings.append(warning)\n"
)
NEW_WARN = (
    "        # turnover unit warning suppressed: it repeats for every coin.\n"
)


def main(path):
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    changed = 0
    for old, new in ((OLD_RAISE, NEW_RAISE), (OLD_WARN, NEW_WARN)):
        n = src.count(old)
        if n != 1:
            print(f"ANCHOR FOUND {n} TIMES; nothing changed for this block:")
            print(old[:120])
            continue
        src = src.replace(old, new, 1)
        changed += 1

    if changed == 0:
        print("No change made. Check that the file is data_client.py from the package.")
        return 1

    shutil.copyfile(path, path + ".bak")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"Patched {changed} block(s). Backup: {path}.bak")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
