#!/usr/bin/env python3
"""Spawn Modal batches for the missing strategies of a coin/window.
Usage: python3 spawn_missing_modal.py <coin> <window>
Reads /tmp/missing_modal_idx_<coin>_<window>.txt and appends call IDs to
/config/backtest/modal_calls_<coin>_<window>_retry.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import modal


def main():
    coin = sys.argv[1] if len(sys.argv) > 1 else "eth"
    window = sys.argv[2] if len(sys.argv) > 2 else "is"
    fn_name = "run_batch_eth" if coin == "eth" else "run_batch_sol"
    fn = modal.Function.from_name("poly-taker-offload-coins", fn_name)

    idx_path = Path(f"/tmp/missing_modal_idx_{coin}_{window}.txt")
    indices = [int(l.strip()) for l in idx_path.read_text().splitlines() if l.strip()]
    calls = []
    for idx in indices:
        call = fn.spawn(idx, window)
        calls.append(call.object_id)
        print(f"spawned {coin}/{window} batch idx={idx} call={call.object_id}", flush=True)

    out_path = Path(f"/config/backtest/modal_calls_{coin}_{window}_retry.json")
    existing = []
    if out_path.exists():
        existing = json.loads(out_path.read_text())
    existing.extend(calls)
    out_path.write_text(json.dumps(existing, indent=1))
    print(f"wrote {len(calls)} calls to {out_path}")


if __name__ == "__main__":
    main()
