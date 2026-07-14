#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-12  kimi
#   - Post-staging gap filler for the 1h compact set: diffs the gdrive remote
#     listing against /tmp/btc1h_compact, re-downloads + compacts any missing
#     files whose REMOTE size is > 0 (0-byte uploads like 2783995.json.gz are
#     data-collection artifacts and are reported, not retried).
# WHY: both staging workers saw occasional truncated downloads (EOFError /
#      JSONDecodeError on otherwise-valid remote files); this makes the merge
#      a single self-healing step instead of a manual log scrape.
"""Usage: python retry_missing_1h.py"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
for p in (HERE, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

REMOTE = "gdrive:polybacktest_60d/polymarket/btc/1h"
DST = "/tmp/btc1h_compact"
RAW = "/tmp/btc1h_raw_retry"


def main() -> None:
    out = subprocess.run(["rclone", "lsf", REMOTE, "--format", "ps"],
                         capture_output=True, text=True, check=True)
    remote = {}  # name -> size
    for line in out.stdout.splitlines():
        # format: "path;size"
        if ";" in line:
            name, _, size = line.rpartition(";")
            remote[name.strip()] = int(size or 0)
    have = {os.path.basename(f).replace(".pkl.gz", "")
            for f in glob.glob(os.path.join(DST, "*.pkl.gz"))}
    missing = [n for n in sorted(remote) if n.replace(".json.gz", "") not in have]
    empty_src = [n for n in missing if remote[n] == 0]
    retry = [n for n in missing if remote[n] > 0]
    print(f"remote={len(remote)} have={len(have)} missing={len(missing)} "
          f"(empty_on_remote={len(empty_src)} retry={len(retry)})", flush=True)
    for n in empty_src:
        print(f"  SKIP 0-byte remote: {n}", flush=True)
    if not retry:
        print("NOTHING TO RETRY", flush=True)
        return

    os.makedirs(RAW, exist_ok=True)
    listf = "/tmp/_retry_1h_missing.txt"
    with open(listf, "w") as fh:
        fh.write("\n".join(retry) + "\n")
    subprocess.run(["rclone", "copy", REMOTE, RAW,
                    "--files-from", listf, "--transfers", "8"], check=True)

    import compactify
    compactify.DST = DST
    from concurrent.futures import ProcessPoolExecutor
    files = sorted(glob.glob(os.path.join(RAW, "*.json.gz")))
    n_ok = n_fail = 0
    with ProcessPoolExecutor(max_workers=4) as ex:
        for r in ex.map(compactify.convert, files):
            if r.startswith("ok"):
                n_ok += 1
            else:
                n_fail += 1
                print("  ", r, flush=True)
    shutil.rmtree(RAW, ignore_errors=True)
    print(f"RETRY DONE: ok={n_ok} fail={n_fail}", flush=True)


if __name__ == "__main__":
    main()
