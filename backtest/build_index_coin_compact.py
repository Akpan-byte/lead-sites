#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-14  kimi
#   - Compact twin of build_index_coin.py: probes /tmp/<coin>5m_compact/*.pkl.gz
#     for (ET date of first snapshot, first-snapshot UTC ts, path) and writes a
#     gzipped JSON index. Uses driver.load_compact_file so it is fast and verifies
#     the compact files are readable.
# WHY: Mirror BTC's is_index_compact.json.gz for ETH/SOL daily-orb / OOS replay.
"""Build ./is_index_<coin>_compact.json.gz from compact pkl.gz files."""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
for p in (HERE, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

import driver  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

ET = ZoneInfo("America/New_York")


def probe(path: str):
    try:
        arr = driver.load_compact_file(path)
        if not arr or not arr.get("t"):
            return None
        from datetime import datetime, timezone
        t = datetime.fromtimestamp(arr["t"][0] / 1000.0, tz=timezone.utc)
        return [t.astimezone(ET).date().isoformat(), t.timestamp(), path]
    except Exception as e:
        return ["ERR:" + str(e)[:60], 0.0, path]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("coin")
    ap.add_argument("data_dir")
    ap.add_argument("out")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()
    files = sorted(glob.glob(os.path.join(args.data_dir, "*.pkl.gz")))
    print(f"[{args.coin}] files: {len(files)} in {args.data_dir}", flush=True)
    rows, errs = [], []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for i, r in enumerate(ex.map(probe, files, chunksize=64), 1):
            if r and not r[0].startswith("ERR:"):
                rows.append(r)
            elif r:
                errs.append(r)
            if i % 2000 == 0:
                print(f"[{args.coin}]   {i}/{len(files)} probed", flush=True)
    rows.sort(key=lambda r: (r[0], r[1]))
    with gzip.open(args.out, "wt", encoding="utf-8") as fh:
        json.dump(rows, fh)
    dates = sorted({r[0] for r in rows})
    span = f"{dates[0]}..{dates[-1]}" if dates else "EMPTY"
    print(f"[{args.coin}] indexed {len(rows)} markets across {len(dates)} ET days "
          f"({span}) -> {args.out}", flush=True)
    if errs:
        print(f"[{args.coin}] !!! {len(errs)} PROBE ERRORS:", flush=True)
        for e in errs[:25]:
            print("     ", e[2], "::", e[0], flush=True)


if __name__ == "__main__":
    main()
