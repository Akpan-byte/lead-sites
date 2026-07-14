#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-12  kimi
#   - 15m/1h twin of build_is_index.py: probes the compact pkl.gz sets
#     (/tmp/btc15m_compact, /tmp/btc1h_compact) for (ET date of first snapshot,
#     first-snapshot UTC ts, path), writes is_index_{15m,1h}.json.gz, then emits
#     the runner file lists is_files_{15m,1h}.txt sorted by (date, ts) — the
#     same ordering is_files_compact.txt uses for 5m. OOS (first-snapshot ET
#     date >= 2026-07-01) stays in the list; run_is._oos_skip_arr drops it at
#     replay time exactly like the 5m path.
# WHY: daily_orb-style strats consume the shared index, and the sequential
#      $200-wallet replay must walk markets in chronological order.
"""Usage: python build_is_index_15m1h.py 15m|1h"""
from __future__ import annotations

import glob
import gzip
import json
import os
import pickle
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ET = ZoneInfo("America/New_York")


def probe(path: str):
    try:
        with gzip.open(path, "rb") as fh:
            arr = pickle.load(fh)
        t = arr.get("t")
        if not t:
            return None
        dt = datetime.fromtimestamp(t[0] / 1000.0, tz=timezone.utc)
        return [dt.astimezone(ET).date().isoformat(), t[0] / 1000.0, path]
    except Exception as e:
        return ["ERR:" + str(e)[:40], 0.0, path]


def main() -> None:
    tf = sys.argv[1]
    assert tf in ("15m", "1h")
    data_dir = f"/tmp/btc{tf}_compact"
    files = sorted(glob.glob(os.path.join(data_dir, "*.pkl.gz")))
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
    out = os.path.join(HERE, f"is_index_{tf}.json.gz")
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(rows, fh)
    dates = sorted({r[0] for r in rows})
    print(f"indexed {len(rows)} markets across {len(dates)} ET days "
          f"({dates[0]}..{dates[-1]}) -> {out}", flush=True)
    list_path = os.path.join(HERE, f"is_files_{tf}.txt")
    with open(list_path, "w") as fh:
        for _, _, p in rows:
            fh.write(p + "\n")
    n_oos = sum(1 for d, _, _ in rows if d >= "2026-07-01")
    print(f"list -> {list_path} ({len(rows)} files, {n_oos} OOS skipped at runtime)",
          flush=True)


if __name__ == "__main__":
    main()
