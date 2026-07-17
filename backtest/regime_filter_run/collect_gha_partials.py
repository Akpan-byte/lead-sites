#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-17  kilo_regime_filter
#   - Downloads the per-worker partial artifacts from a GitHub Actions run of
#     regime_filter_is_backtest.yml into the local partials directory.
#   - Uses the gh CLI to pull every artifact matching `regime-partial-*`.
# WHY: The VM is the merge/quant host; it needs the partials produced by the
#      20 GitHub Actions workers in addition to its own and the laptop's.
from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id", help="GitHub Actions run id")
    ap.add_argument("--repo", default="Akpan-byte/lead-sites")
    ap.add_argument("--partial-dir", default=os.path.join(HERE, "partials"))
    args = ap.parse_args()

    os.makedirs(args.partial_dir, exist_ok=True)
    tmp_dir = os.path.join(HERE, "gha_artifacts")
    os.makedirs(tmp_dir, exist_ok=True)

    cmd = [
        "gh", "run", "download", args.run_id,
        "--repo", args.repo,
        "--pattern", "regime-partial-*",
        "--dir", tmp_dir,
    ]
    print(f"running: {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True)

    # Each artifact is extracted to a subdir; move the partial files up.
    moved = 0
    for root, _, files in os.walk(tmp_dir):
        for f in files:
            if f.endswith(".trades.jsonl.gz") or f.endswith(".summary.json"):
                src = os.path.join(root, f)
                dst = os.path.join(args.partial_dir, f)
                # overwrite if already present (idempotent)
                os.replace(src, dst)
                moved += 1

    print(f"collected {moved} partial files into {args.partial_dir}")


if __name__ == "__main__":
    main()
