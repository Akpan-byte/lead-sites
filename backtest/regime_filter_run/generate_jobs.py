#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-17  kilo_regime_filter
#   - Generates deterministic date-range chunk file lists and a global job queue
#     for the regime-filter full-IS backtest.
#   - Chunks are built from is_index_compact.json.gz (sorted by market open time).
#   - One job = one registry variant on one chunk. Jobs are ordered so that the
#     global job index is stable across VM, laptop, and GitHub Actions workers.
# WHY: Enables maximum parallelization while keeping per-job memory/runtime bounded
#      and making partial results trivial to merge.
import argparse
import gzip
import json
import os
from typing import List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BACKTEST = os.path.dirname(HERE)


def load_index(path: str) -> List[Tuple[str, float, str]]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        rows = json.load(fh)
    # rows: [date_iso, ts, file_path]
    rows.sort(key=lambda r: r[1])
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default=os.path.join(BACKTEST, "combined_trend_regime.json"))
    ap.add_argument("--index", default=os.path.join(BACKTEST, "is_index_compact.json.gz"))
    ap.add_argument("--chunks", type=int, default=5, help="number of date-range chunks")
    ap.add_argument("--out-dir", default=HERE)
    ap.add_argument("--fill", choices=["maker", "taker", "instant"], default="taker")
    args = ap.parse_args()

    rows = load_index(args.index)
    n = len(rows)
    chunk_size = (n + args.chunks - 1) // args.chunks
    chunk_files = []
    for i in range(args.chunks):
        start = i * chunk_size
        end = min((i + 1) * chunk_size, n)
        chunk_rows = rows[start:end]
        chunk_path = os.path.join(args.out_dir, "chunks", f"chunk_{i:02d}.txt")
        os.makedirs(os.path.dirname(chunk_path), exist_ok=True)
        with open(chunk_path, "w", encoding="utf-8") as fh:
            for _, _, f in chunk_rows:
                fh.write(f + "\n")
        # Store path relative to backtest root so jobs are portable across VM,
        # laptop, and GitHub Actions runners.
        chunk_files.append(os.path.relpath(chunk_path, BACKTEST))
        print(f"chunk {i}: {chunk_rows[0][0]} -> {chunk_rows[-1][0]}  markets={len(chunk_rows)}")

    with open(args.registry, "r", encoding="utf-8") as fh:
        registry = json.load(fh)

    variants = sorted(registry.keys())
    jobs = []
    for variant in variants:
        for chunk_idx, chunk_path in enumerate(chunk_files):
            jobs.append({
                "job_idx": len(jobs),
                "variant": variant,
                "chunk_idx": chunk_idx,
                "chunk_path": chunk_path,
                "fill": args.fill,
            })

    jobs_path = os.path.join(args.out_dir, "jobs.json")
    with open(jobs_path, "w", encoding="utf-8") as fh:
        json.dump(jobs, fh, indent=1)
    print(f"wrote {len(jobs)} jobs to {jobs_path}")

    # Also emit a flat list of variant names for convenience.
    variants_path = os.path.join(args.out_dir, "variants.txt")
    with open(variants_path, "w", encoding="utf-8") as fh:
        for v in variants:
            fh.write(v + "\n")


if __name__ == "__main__":
    main()
