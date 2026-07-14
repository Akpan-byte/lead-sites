#!/usr/bin/env python3
"""Call the deployed build_compact function and print the call ID.

Usage:
  python3 modal_call_compact.py eth
  python3 modal_call_compact.py sol
"""
from __future__ import annotations

import sys

import modal

fn = modal.Function.from_name("build-compact-coins", "build_compact")

coin = sys.argv[1] if len(sys.argv) > 1 else "eth"
call = fn.spawn(coin)
print(f"spawned {coin}: {call.object_id}")
