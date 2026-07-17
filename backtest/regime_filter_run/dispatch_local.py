#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-17  kilo_regime_filter
#   - Local dispatcher for a set of global worker IDs.
#   - Launches up to --parallel run_worker.py subprocesses concurrently and waits
#     for all to finish, streaming logs to a per-worker file.
# WHY: VM (3 cores) and laptop (16 cores/threads) need a simple way to run their
#      slice of the global job queue without manually managing background jobs.
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
BACKTEST = os.path.dirname(HERE)


def run_one(worker_id: int, total_workers: int, args) -> str:
    log_dir = os.path.abspath(args.log_dir)
    log = os.path.join(log_dir, f"worker_{worker_id:02d}.log")
    os.makedirs(log_dir, exist_ok=True)
    # Use absolute paths so the worker can run from any cwd.
    registry_abs = os.path.abspath(args.registry)
    jobs_abs = os.path.abspath(args.jobs)
    partial_abs = os.path.abspath(args.partial_dir)
    cmd = [
        sys.executable,
        os.path.join(HERE, "run_worker.py"),
        "--worker-id", str(worker_id),
        "--total-workers", str(total_workers),
        "--registry", registry_abs,
        "--jobs", jobs_abs,
        "--partial-dir", partial_abs,
    ]
    with open(log, "w", encoding="utf-8") as fh:
        fh.write(f"# {' '.join(cmd)}\n")
        fh.flush()
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=BACKTEST)
        ret = proc.wait()
    status = "OK" if ret == 0 else f"EXIT:{ret}"
    return f"worker {worker_id}: {status} (log {log})"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker-ids", required=True,
                    help="comma-separated global worker IDs for this machine")
    ap.add_argument("--total-workers", type=int, required=True,
                    help="total global workers across VM+laptop+GHA")
    ap.add_argument("--parallel", type=int, default=1,
                    help="max concurrent subprocesses on this machine")
    ap.add_argument("--registry", default=os.path.join(os.path.dirname(HERE), "combined_trend_regime.json"))
    ap.add_argument("--jobs", default=os.path.join(HERE, "jobs.json"))
    ap.add_argument("--partial-dir", default=os.path.join(HERE, "partials"))
    ap.add_argument("--log-dir", default=os.path.join(HERE, "logs"))
    args = ap.parse_args()

    ids = [int(x.strip()) for x in args.worker_ids.split(",") if x.strip()]
    print(f"dispatching workers {ids} (parallel={args.parallel}) total_workers={args.total_workers}",
          flush=True)

    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futs = {ex.submit(run_one, wid, args.total_workers, args): wid for wid in ids}
        for fut in as_completed(futs):
            print(fut.result(), flush=True)
    print("dispatch_local done")


if __name__ == "__main__":
    main()
