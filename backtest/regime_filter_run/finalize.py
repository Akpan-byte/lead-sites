#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-17  kilo_regime_filter
#   - Finalization script: run quant_suite.py over the merged trade logs and
#     then build the baseline-vs-regime-filter comparison report.
# WHY: After VM/laptop/GitHub partials are merged, one command produces the
#      quant battery and the requested comparison table.
from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKTEST = os.path.dirname(HERE)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--fill", default="taker")
    args = ap.parse_args()

    is_dir = f"is_{args.fill}_regime_full"
    q_dir = f"quant_{args.fill}_regime_full"
    env = os.environ.copy()
    env["BT_IS_DIR"] = is_dir
    env["BT_Q_DIR"] = q_dir

    cmd = [sys.executable, os.path.join(BACKTEST, "quant_suite.py"),
           "--fill", args.fill, "--workers", str(args.workers)]
    print(f"running: {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=BACKTEST, env=env, check=True)

    cmd2 = [sys.executable, os.path.join(HERE, "build_comparison.py"),
            "--quant-dir", os.path.join(BACKTEST, "results", q_dir)]
    print(f"running: {' '.join(cmd2)}", flush=True)
    subprocess.run(cmd2, cwd=BACKTEST, check=True)


if __name__ == "__main__":
    main()
