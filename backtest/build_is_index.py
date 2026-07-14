#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - One-time shared index of the local BTC-5m dataset: for every market file,
#     record (ET date of first snapshot, first-snapshot UTC ts, path). Saved
#     compressed to ./is_index.json.gz. All daily_orb strats (and OOS scoring)
#     reuse this instead of re-scanning 18k files each.
# WHY: bt_orb.run_daily_orb otherwise rebuilds this index per strategy (~25 min
#      x 39 strats of pure redundant IO).
"""Build ./is_index.json.gz from a directory of polybacktest {market}.json.gz."""
from __future__ import annotations

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
        snaps = driver.load_market_file(path)
        if not snaps:
            return None
        t = driver._parse_ts(snaps[0]["time"])
        return [t.astimezone(ET).date().isoformat(), t.timestamp(), path]
    except Exception as e:
        return ["ERR:" + str(e)[:40], 0.0, path]


def main() -> None:
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/btc5m_all"
    files = sorted(glob.glob(os.path.join(data_dir, "*.json.gz")))
    print(f"files: {len(files)}", flush=True)
    rows = []
    with ProcessPoolExecutor(max_workers=4) as ex:
        for i, r in enumerate(ex.map(probe, files, chunksize=64), 1):
            if r and not r[0].startswith("ERR:"):
                rows.append(r)
            elif r:
                print("probe err:", r[2], r[0], flush=True)
            if i % 2000 == 0:
                print(f"  {i}/{len(files)} probed", flush=True)
    rows.sort(key=lambda r: (r[0], r[1]))
    out = os.path.join(HERE, "is_index.json.gz")
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(rows, fh)
    dates = sorted({r[0] for r in rows})
    print(f"indexed {len(rows)} markets across {len(dates)} ET days "
          f"({dates[0]}..{dates[-1]}) -> {out}", flush=True)


if __name__ == "__main__":
    main()
