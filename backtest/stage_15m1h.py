#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-12  kimi
#   - Resumable staged downloader+compactor for the BTC 15m/1h polybacktest
#     datasets on gdrive. Streams rclone copies in ~200-file batches to a raw
#     dir under /tmp, compacts each batch to dict-of-arrays pkl.gz (reusing
#     compactify.convert with an overridden DST), then deletes the raw batch
#     before fetching the next — /config is 90% full so nothing lands there.
# WHY: btc_orb_15m/30m/1h need correctly-sized markets (15m=900s, 1h=3600s)
#      and the 5m compact set cannot serve them; raw JSON is 3.8GiB total so
#      it must never accumulate on disk.
"""Usage:
  python stage_15m1h.py 15m [--max-batches N] [--batch-size 200]
  python stage_15m1h.py 1h  [--max-batches N]
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
for p in (HERE, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

REMOTE = "gdrive:polybacktest_60d/polymarket/btc"


def remote_listing(tf: str) -> list[str]:
    out = subprocess.run(["rclone", "lsf", f"{REMOTE}/{tf}"],
                         capture_output=True, text=True, check=True)
    return sorted(l.strip() for l in out.stdout.splitlines() if l.strip())


def compact_batch(raw_dir: str, dst_dir: str, workers: int) -> tuple[int, int]:
    # Import here so the module-level `import driver` inside compactify happens
    # after sys.path is set; override its hardcoded 5m DST for this run.
    import compactify
    compactify.DST = dst_dir
    files = sorted(glob.glob(os.path.join(raw_dir, "*.json.gz")))
    if not files:
        return 0, 0
    n_ok = n_fail = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(compactify.convert, files, chunksize=16):
            if r.startswith("ok"):
                n_ok += 1
            else:
                n_fail += 1
                if n_fail <= 5:
                    print("  ", r, flush=True)
    return n_ok, n_fail


def _download(tf: str, batch: list[str], raw_dir: str, tag: str) -> None:
    """rclone-copy one batch of names from gdrive into raw_dir."""
    listf = f"/tmp/_stage_{tf}_batch_{tag}.txt"
    with open(listf, "w") as fh:
        fh.write("\n".join(batch) + "\n")
    subprocess.run(["rclone", "copy", f"{REMOTE}/{tf}", raw_dir,
                    "--files-from", listf, "--transfers", "16"],
                   check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tf", choices=["15m", "1h"])
    ap.add_argument("--batch-size", type=int, default=400)
    ap.add_argument("--max-batches", type=int, default=0,
                    help="0 = all remaining; else stop after N batches (timeout-safe reruns)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--time-budget", type=int, default=240,
                    help="stop starting new batches after this many seconds "
                         "(timeout-safe; an in-flight prefetch is left on disk "
                         "for the next invocation to compact first)")
    args = ap.parse_args()

    dst_dir = f"/tmp/btc{args.tf}_compact"
    os.makedirs(dst_dir, exist_ok=True)

    names = remote_listing(args.tf)
    done = {os.path.basename(f).replace(".pkl.gz", "")
            for f in glob.glob(os.path.join(dst_dir, "*.pkl.gz"))}
    todo = [n for n in names if n.replace(".json.gz", "") not in done]
    print(f"{args.tf}: remote={len(names)} compacted={len(done)} todo={len(todo)}",
          flush=True)
    if not todo:
        print("nothing to do", flush=True)
        return

    # Pipeline: download batch i+1 (network) while batch i compacts (CPU).
    # Two alternating raw dirs keep them disjoint; each is deleted right after
    # its compaction so raw data never accumulates. A batch whose download
    # finished but was never compacted (timeout kill / budget stop) is left in
    # its raw dir and compacted FIRST on the next invocation.
    import threading
    import time
    t_start = time.time()
    raw_a, raw_b = f"/tmp/btc{args.tf}_raw_a", f"/tmp/btc{args.tf}_raw_b"
    os.makedirs(raw_a, exist_ok=True)
    os.makedirs(raw_b, exist_ok=True)

    # Recover leftovers from a previous interrupted run (always a prefix of
    # todo, since staging order follows the sorted remote listing). Chunked and
    # budget-aware: a kill during recovery leaves the unprocessed files in
    # place for the next invocation.
    # Budget reserve scales with batch size: 1h files compact at ~1 file/s/core,
    # so a batch costs roughly batch_size wall-seconds on 4 cores.
    reserve = max(120, args.batch_size)
    for d in (raw_a, raw_b):
        tmp = d + "_chunk"
        while True:
            left = sorted(glob.glob(os.path.join(d, "*.json.gz")))
            if not left or time.time() - t_start + reserve > args.time_budget:
                break
            os.makedirs(tmp, exist_ok=True)
            for f in left[: args.batch_size]:
                os.rename(f, os.path.join(tmp, os.path.basename(f)))
            n_ok, n_fail = compact_batch(tmp, dst_dir, args.workers)
            shutil.rmtree(tmp, ignore_errors=True)
            print(f"recovered chunk from {d}: ok={n_ok} fail={n_fail}", flush=True)
        shutil.rmtree(tmp, ignore_errors=True)
    # recompute todo: recovery may have finished part (or all) of it
    done = {os.path.basename(f).replace(".pkl.gz", "")
            for f in glob.glob(os.path.join(dst_dir, "*.pkl.gz"))}
    todo = [n for n in names if n.replace(".json.gz", "") not in done]

    batches = [todo[i:i + args.batch_size]
               for i in range(0, len(todo), args.batch_size)]
    if args.max_batches:
        batches = batches[: args.max_batches]
    prefetch: threading.Thread | None = None

    for bi, batch in enumerate(batches, 1):
        # Budget check: a batch costs ~compact_time (download is overlapped),
        # so never start one that cannot finish inside the budget.
        if time.time() - t_start + reserve > args.time_budget:
            print(f"budget stop before batch {bi}/{len(batches)}", flush=True)
            break
        cur_raw = raw_a if bi % 2 == 1 else raw_b
        nxt_raw = raw_b if bi % 2 == 1 else raw_a
        if prefetch is None:                      # first batch: no prefetch yet
            _download(args.tf, batch, cur_raw, str(bi))
        else:
            prefetch.join()                       # current batch already fetched
            prefetch = None
        if bi < len(batches):                     # start fetching the next one
            nxt = batches[bi]
            prefetch = threading.Thread(
                target=_download, args=(args.tf, nxt, nxt_raw, str(bi + 1)))
            prefetch.start()
        n_ok, n_fail = compact_batch(cur_raw, dst_dir, args.workers)
        shutil.rmtree(cur_raw, ignore_errors=True)
        os.makedirs(cur_raw, exist_ok=True)
        n_done = len(glob.glob(os.path.join(dst_dir, "*.pkl.gz")))
        print(f"batch {bi}/{len(batches)}: ok={n_ok} fail={n_fail} "
              f"total_compacted={n_done}", flush=True)
    if prefetch is not None:
        prefetch.join()                           # leave its files for next run
    print("STAGE DONE" if len(glob.glob(os.path.join(dst_dir, '*.pkl.gz'))) >= len(names)
          else "STAGE PAUSED", args.tf, flush=True)


if __name__ == "__main__":
    main()
