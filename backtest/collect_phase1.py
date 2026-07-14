#!/usr/bin/env python3
"""Collect Phase 1 per-coin backtest summaries into a promotion table.

Usage:
  python3 collect_phase1.py --coin eth --window is --dir results/is_taker_eth
  python3 collect_phase1.py --coin eth --window oos --dir results/oos_taker_eth
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path


def load_summaries(d: str) -> dict:
    out = {}
    for f in glob.glob(os.path.join(d, "*.summary.json")):
        s = json.load(open(f))
        out[s["strategy"]] = s
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", required=True, choices=["eth", "sol"])
    ap.add_argument("--window", required=True, choices=["is", "oos"])
    ap.add_argument("--dir", required=True)
    args = ap.parse_args()

    sums = load_summaries(args.dir)
    rows = []
    for name, s in sorted(sums.items()):
        rows.append({
            "strategy": name,
            "family": s.get("family", "unknown"),
            "n_markets": s.get("n_markets", s.get("n_closed", 0)),
            "n_closed": s.get("n_closed", 0),
            "total_pnl": s.get("total_pnl", 0.0),
            "win_rate": s.get("win_rate", 0.0),
            "max_dd_pct": s.get("max_dd_pct", 0.0),
            "equity": s.get("equity", 200.0),
        })

    out_dir = Path("results/phase1")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.coin}_{args.window}_summary.json"
    with open(out_path, "w") as fh:
        json.dump(rows, fh, indent=1)

    print(f"coin={args.coin} window={args.window} dir={args.dir} strategies={len(rows)}")
    print(f"wrote {out_path}")
    print(f"{'strategy':50s} {'n':>6} {'pnl':>9} {'wr%':>6} {'dd%':>6} {'equity':>8}")
    for r in sorted(rows, key=lambda x: x["total_pnl"], reverse=True)[:20]:
        print(f"{r['strategy']:50s} {r['n_closed']:6d} {r['total_pnl']:+9.2f} {r['win_rate']:6.1f} {r['max_dd_pct']:6.1f} {r['equity']:8.2f}")


if __name__ == "__main__":
    main()
