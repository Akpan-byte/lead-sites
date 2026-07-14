#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-12  kimi
#   - Parallel tail-worker for the 1h staging: stage_15m1h.py works the sorted
#     remote listing FRONT-to-back; this script works the same todo list
#     BACK-to-front into the SAME dst dir. Outputs are filename-disjoint pkl.gz
#     so there are no write collisions, and the main stager's done-set check
#     auto-skips anything this worker finishes (and vice versa).
# WHY: 1h staging (1,468 files) was the last open track and ETA was slipping;
#      a second downloader+compactor roughly halves the wall time.
"""Usage: python stage_1h_tail.py [--batch-size 150] [--workers 2] [--max-files 600]"""
from __future__ import annotations

import argparse
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

REMOTE = "gdrive:polybacktest_60d/polymarket/btc"
TF = "1h"
DST = "/tmp/btc1h_compact"
RAW = "/tmp/btc1h_raw_tail"


def remote_listing() -> list[str]:
    out = subprocess.run(["rclone", "lsf", f"{REMOTE}/{TF}"],
                         capture_output=True, text=True, check=True)
    return sorted(l.strip() for l in out.stdout.splitlines() if l.strip())


def compact_batch(raw_dir: str, workers: int) -> tuple[int, int]:
    import compactify
    compactify.DST = DST
    files = sorted(glob.glob(os.path.join(raw_dir, "*.json.gz")))
    if not files:
        return 0, 0
    n_ok = n_fail = 0
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(compactify.convert, files, chunksize=16):
            if r.startswith("ok"):
                n_ok += 1
            else:
                n_fail += 1
                if n_fail <= 5:
                    print("  ", r, flush=True)
    return n_ok, n_fail


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-size", type=int, default=150)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--max-files", type=int, default=600,
                    help="only take this many files from the TAIL of the todo list "
                         "(keeps this worker disjoint from the front worker)")
    args = ap.parse_args()

    os.makedirs(DST, exist_ok=True)
    os.makedirs(RAW, exist_ok=True)

    names = remote_listing()
    done = {os.path.basename(f).replace(".pkl.gz", "")
            for f in glob.glob(os.path.join(DST, "*.pkl.gz"))}
    todo = [n for n in names if n.replace(".json.gz", "") not in done]
    # Tail slice, processed in REVERSE order so we stay maximally far from the
    # front worker's current position for as long as possible.
    mine = list(reversed(todo[-args.max_files:]))
    print(f"tail-worker: remote={len(names)} done={len(done)} todo={len(todo)} "
          f"mine={len(mine)}", flush=True)

    batches = [mine[i:i + args.batch_size] for i in range(0, len(mine), args.batch_size)]
    for bi, batch in enumerate(batches, 1):
        # Recompute done each batch: if the front worker already got here
        # (shouldn't happen — opposite directions), skip finished files.
        done = {os.path.basename(f).replace(".pkl.gz", "")
                for f in glob.glob(os.path.join(DST, "*.pkl.gz"))}
        batch = [n for n in batch if n.replace(".json.gz", "") not in done]
        if not batch:
            print(f"batch {bi}: fully done by front worker, skipping", flush=True)
            continue
        listf = f"/tmp/_stage_1h_tail_batch_{bi}.txt"
        with open(listf, "w") as fh:
            fh.write("\n".join(batch) + "\n")
        subprocess.run(["rclone", "copy", f"{REMOTE}/{TF}", RAW,
                        "--files-from", listf, "--transfers", "16"], check=True)
        n_ok, n_fail = compact_batch(RAW, args.workers)
        shutil.rmtree(RAW, ignore_errors=True)
        os.makedirs(RAW, exist_ok=True)
        n_done = len(glob.glob(os.path.join(DST, "*.pkl.gz")))
        print(f"tail batch {bi}/{len(batches)}: ok={n_ok} fail={n_fail} "
              f"total_compacted={n_done}", flush=True)
    shutil.rmtree(RAW, ignore_errors=True)
    print("TAIL WORKER DONE", flush=True)


if __name__ == "__main__":
    main()
