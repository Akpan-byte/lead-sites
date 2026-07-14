#!/usr/bin/env python3
"""Fire-and-forget launcher for modal_build_compact.build_compact.

Usage:
  python3 modal_spawn_compact.py eth
  python3 modal_spawn_compact.py sol
"""
from __future__ import annotations

import sys

from modal_build_compact import app, build_compact


def main():
    coin = sys.argv[1] if len(sys.argv) > 1 else "eth"
    with app.run():
        call = build_compact.spawn(coin)
        print(f"spawned {coin} compact build: {call.object_id}")


if __name__ == "__main__":
    main()
