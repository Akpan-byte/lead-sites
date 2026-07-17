#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-17  kilo_regime_filter
#   - Audits partials directory before a distributed run.
#   - Removes empty trade files and orphaned summaries.
#   - Reconstructs minimal summary.json files for trade files that are missing
#     their summary, so run_worker.py can skip them with --skip-existing.
# WHY: The laptop already produced some .trades.jsonl.gz files without
#      summaries; regenerating those summaries avoids recomputing finished
#      chunks, and deleting empty/invalid files lets workers re-run them.
from __future__ import annotations

import gzip
import json
import os
import sys
from typing import Any, Dict, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))

CAPITAL = 200.0


def _paths(job: Dict[str, Any], partial_dir: str) -> Tuple[str, str]:
    base = os.path.join(partial_dir, f"{job['variant']}_chunk{job['chunk_idx']:02d}")
    return base + ".trades.jsonl.gz", base + ".summary.json"


def _summ_from_trades(trades_path: str) -> Dict[str, Any]:
    variant = os.path.basename(trades_path).replace(".trades.jsonl.gz", "")
    # chunk idx from filename tail _chunkNN
    chunk_s = variant.rsplit("_chunk", 1)[1]
    variant = variant.rsplit("_chunk", 1)[0]
    pnls = []
    with gzip.open(trades_path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                pnls.append(float(json.loads(line).get("pnl", 0.0)))
            except Exception:
                continue
    total_pnl = sum(pnls)
    equity = CAPITAL + total_pnl
    return {
        "variant": variant,
        "chunk_idx": int(chunk_s),
        "fill": "taker",
        "n_markets": 0,
        "n_signals": 0,
        "n_triggered": 0,
        "n_closed": len(pnls),
        "n_active_left": 0,
        "total_pnl": round(total_pnl, 4),
        "cash": round(equity, 4),
        "committed": 0.0,
        "equity": round(equity, 4),
        "start_capital": CAPITAL,
        "runtime_s": 0.0,
    }


def main() -> None:
    partial_dir = os.path.join(HERE, "partials")
    jobs_path = os.path.join(HERE, "jobs.json")
    with open(jobs_path, "r", encoding="utf-8") as fh:
        jobs = json.load(fh)

    removed_empty = 0
    removed_orphan = 0
    rebuilt = 0

    for job in jobs:
        trades_path, summ_path = _paths(job, partial_dir)
        trades_exists = os.path.exists(trades_path)
        summ_exists = os.path.exists(summ_path)

        if trades_exists and os.path.getsize(trades_path) == 0:
            os.remove(trades_path)
            removed_empty += 1
            trades_exists = False
            if summ_exists:
                os.remove(summ_path)
                removed_orphan += 1
                summ_exists = False

        if summ_exists and not trades_exists:
            os.remove(summ_path)
            removed_orphan += 1
            summ_exists = False

        if trades_exists and not summ_exists:
            try:
                summ = _summ_from_trades(trades_path)
            except Exception as e:
                print(f"  corrupt trades file {trades_path}: {e}; removing", flush=True)
                os.remove(trades_path)
                removed_empty += 1
                continue
            with open(summ_path, "w", encoding="utf-8") as fh:
                json.dump(summ, fh, indent=1)
            rebuilt += 1

    print(f"audit_partials: removed {removed_empty} empty trade files, "
          f"{removed_orphan} orphaned summaries, rebuilt {rebuilt} summaries")


if __name__ == "__main__":
    main()
