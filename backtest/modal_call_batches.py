#!/usr/bin/env python3
"""Spawn all Phase 1 backtest batches for a coin/window on Modal.

Usage:
  python3 modal_call_batches.py eth is
  python3 modal_call_batches.py eth oos
  python3 modal_call_batches.py sol is
  python3 modal_call_batches.py sol oos
"""
from __future__ import annotations

import json
import sys

import modal


def main():
    coin = sys.argv[1] if len(sys.argv) > 1 else "eth"
    window = sys.argv[2] if len(sys.argv) > 2 else "is"
    fn_name = "run_batch_eth" if coin == "eth" else "run_batch_sol"
    fn = modal.Function.from_name("poly-taker-offload-coins", fn_name)

    batches = json.load(open(f"/config/backtest/modal_batches_{coin}.json"))
    calls = []
    for idx in range(len(batches)):
        call = fn.spawn(idx, window)
        calls.append(call.object_id)
    out_path = f"/config/backtest/modal_calls_{coin}_{window}.json"
    with open(out_path, "w") as fh:
        json.dump(calls, fh, indent=1)
    print(f"spawned {len(calls)} {coin}/{window} batches -> {out_path}")


if __name__ == "__main__":
    main()
