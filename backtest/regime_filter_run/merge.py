#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-17  kilo_regime_filter
#   - Merges per-chunk partial trade files into per-variant full-IS trade logs.
#   - Writes the merged files to results/is_taker_regime_full in the format
#     quant_suite.py expects: <variant>.trades.jsonl.gz (+ a summary JSON).
# WHY: The chunked workers produce independent partials; this step reconstructs
#      one continuous trade stream per variant for the quant battery.
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import re
from itertools import accumulate
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BACKTEST = os.path.dirname(HERE)

CAPITAL = 200.0


def _variant_chunks(partial_dir: str) -> Dict[str, List[Tuple[int, str, str]]]:
    """Map variant -> [(chunk_idx, trades_path, summary_path), ...]."""
    pattern = os.path.join(partial_dir, "*_chunk*.trades.jsonl.gz")
    out: Dict[str, List[Tuple[int, str, str]]] = {}
    for trades_path in glob.glob(pattern):
        basename = os.path.basename(trades_path).replace(".trades.jsonl.gz", "")
        m = re.match(r"^(.+)_chunk(\d+)$", basename)
        if not m:
            continue
        variant, chunk_s = m.group(1), int(m.group(2))
        summ_path = trades_path.replace(".trades.jsonl.gz", ".summary.json")
        out.setdefault(variant, []).append((chunk_s, trades_path, summ_path))
    for variant in out:
        out[variant].sort(key=lambda x: x[0])
    return out


def _load_summ(summ_path: str) -> Dict[str, Any]:
    with open(summ_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def merge_variant(variant: str, chunks: List[Tuple[int, str, str]],
                  out_dir: str) -> Dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    trades_out = os.path.join(out_dir, f"{variant}.trades.jsonl.gz")
    summ_out = os.path.join(out_dir, f"{variant}.summary.json")

    total_pnl = 0.0
    n_closed = 0
    n_markets = 0
    n_signals = 0
    n_triggered = 0

    with gzip.open(trades_out, "wt", encoding="utf-8") as out:
        for chunk_idx, trades_path, summ_path in chunks:
            if not os.path.exists(trades_path):
                raise FileNotFoundError(trades_path)
            with gzip.open(trades_path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    out.write(line + "\n")
            if os.path.exists(summ_path):
                s = _load_summ(summ_path)
                total_pnl += s.get("total_pnl", 0.0)
                n_closed += s.get("n_closed", 0)
                n_markets += s.get("n_markets", 0)
                n_signals += s.get("n_signals", 0)
                n_triggered += s.get("n_triggered", 0)

    # Recompute drawdown-agnostic summary from the merged trade stream.
    pnls: List[float] = []
    with gzip.open(trades_out, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                pnls.append(float(json.loads(line)["pnl"]))
    total_pnl = sum(pnls)
    n_closed = len(pnls)
    equity = [CAPITAL] + [CAPITAL + x for x in accumulate(pnls)]
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, peak - e)

    summary = {
        "strategy": variant,
        "n_markets": n_markets,
        "n_closed": n_closed,
        "n_signals": n_signals,
        "n_triggered": n_triggered,
        "total_pnl": round(total_pnl, 4),
        "equity": round(equity[-1], 4),
        "start_capital": CAPITAL,
        "pnl_pct": round(100 * total_pnl / CAPITAL, 2),
        "max_dd_usd": round(max_dd, 4),
        "max_dd_pct": round(100 * max_dd / CAPITAL, 2),
    }
    with open(summ_out, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--partial-dir", default=os.path.join(HERE, "partials"))
    ap.add_argument("--out-dir", default=os.path.join(BACKTEST, "results", "is_taker_regime_full"))
    ap.add_argument("--only", default="", help="comma-separated variants to merge")
    args = ap.parse_args()

    groups = _variant_chunks(args.partial_dir)
    only = set(args.only.split(",")) if args.only else None

    print(f"merging partials from {args.partial_dir}")
    summaries = []
    for variant in sorted(groups):
        if only and variant not in only:
            continue
        chunks = groups[variant]
        print(f"  {variant}: {len(chunks)} chunks", flush=True)
        summary = merge_variant(variant, chunks, args.out_dir)
        summaries.append(summary)
        print(f"    closed={summary['n_closed']} pnl={summary['total_pnl']:.2f} "
              f"dd={summary['max_dd_pct']:.1f}%", flush=True)

    print(f"merged {len(summaries)} variants -> {args.out_dir}")


if __name__ == "__main__":
    main()
