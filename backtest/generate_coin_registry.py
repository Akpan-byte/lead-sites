#!/usr/bin/env python3
"""Generate per-coin strategy registries for ETH and SOL backtests.

Reads the base STRATEGIES dict from engine.strategy_registry, prefixes every
name with the coin (e.g. eth_breakout_pct_003), and appends the ORB entries
for that coin from coin_orb_registry.json. Output files are used via
BT_EXTRA_STRATEGIES when running driver.py / run_is.py.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from engine.strategy_registry import STRATEGIES  # noqa: E402


def build_coin_registry(coin: str) -> dict:
    coin = coin.lower()
    out: dict = {}
    for name, reg in STRATEGIES.items():
        out[f"{coin}_{name}"] = dict(reg)
    # Append ORB entries for this coin.
    orb_path = os.path.join(HERE, "coin_orb_registry.json")
    with open(orb_path, "r", encoding="utf-8") as fh:
        orb = json.load(fh)
    for name, reg in orb.items():
        if name.startswith(f"phase_2.{coin}_"):
            out[name] = dict(reg)
    return out


def main() -> None:
    for coin in ("eth", "sol"):
        reg = build_coin_registry(coin)
        out_path = os.path.join(HERE, f"coin_full_registry_{coin}.json")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(reg, fh, indent=2)
        print(f"Wrote {out_path}: {len(reg)} entries")


if __name__ == "__main__":
    main()
