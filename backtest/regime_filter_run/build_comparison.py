#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-17  kilo_regime_filter
#   - Builds the baseline-vs-regime-filter comparison table from quant_suite
#     outputs (or merged summaries as a fallback).
#   - Groups variants by underlying trend leg and highlights the best
#     regime-filtered variant per leg by total PnL.
# WHY: The user's deliverable is a side-by-side PnL/DD comparison with a clear
#      recommendation per leg.
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BACKTEST = os.path.dirname(HERE)


def _leg_key(variant: str) -> str:
    # Regime variants end with _rV[1-4]; strip that to find the baseline leg.
    if re.search(r"_rV[1-9]$", variant):
        return variant.rsplit("_rV", 1)[0]
    return variant


def _load_quant(quant_dir: str) -> Dict[str, Dict[str, Any]]:
    out = {}
    for path in glob.glob(os.path.join(quant_dir, "*.quant.json")):
        name = os.path.basename(path).replace(".quant.json", "")
        with open(path, "r", encoding="utf-8") as fh:
            out[name] = json.load(fh)
    return out


def _extract(rec: Dict[str, Any]) -> Tuple[float, float, int, float]:
    """Return (pnl, dd_pct, trades, sharpe) from a quant record or summary."""
    if "core" in rec:
        pnl = rec["core"].get("total_pnl", 0.0)
        trades = rec["core"].get("n_trades", rec.get("n_trades", 0))
        sharpe = rec["risk"].get("sharpe_per_trade", 0.0) if rec.get("risk") else 0.0
    else:
        pnl = rec.get("total_pnl", 0.0)
        trades = rec.get("n_closed", rec.get("n_trades", 0))
        sharpe = 0.0
    dd_pct = rec.get("drawdown", {}).get("max_dd_pct", rec.get("max_dd_pct", 0.0))
    return pnl, dd_pct, trades, sharpe


def build(quant_dir: str, out_path: str) -> None:
    records = _load_quant(quant_dir)
    if not records:
        raise RuntimeError(f"no quant records found in {quant_dir}")

    # Group by leg
    legs: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for name, rec in records.items():
        leg = _leg_key(name)
        legs.setdefault(leg, {})[name] = rec

    lines = [
        "# Regime-filter full-IS comparison: baseline vs trend-family variants\n",
        "_Taker-fill, $200 start, in-sample (pre-2026-07-01)._\n",
    ]

    overall_best: Dict[str, Any] = {}
    for leg in sorted(legs):
        group = legs[leg]
        baseline = group.get(leg)
        variants = {n: r for n, r in group.items() if n != leg}

        lines.append(f"\n## Leg: `{leg}`\n")
        lines.append("| config | trades | PnL $ | PnL % | maxDD % | Sharpe | note |")
        lines.append("|---|---:|---:|---:|---:|---:|---|")

        if baseline:
            pnl, dd, tr, sr = _extract(baseline)
            lines.append(
                f"| **baseline** | {tr} | {pnl:.2f} | {100*pnl/200:.1f} | {dd:.1f} | {sr:.3f} | reference |"
            )
        else:
            lines.append("| **baseline** | - | - | - | - | - | missing |")

        best_name = None
        best_pnl = -1e18
        for name in sorted(variants):
            pnl, dd, tr, sr = _extract(variants[name])
            note = ""
            if pnl > best_pnl:
                best_pnl = pnl
                best_name = name
            lines.append(
                f"| `{name}` | {tr} | {pnl:.2f} | {100*pnl/200:.1f} | {dd:.1f} | {sr:.3f} | {note} |"
            )
        if best_name:
            overall_best[leg] = best_name
            # annotate the best line
            # simple replacement
            for i, line in enumerate(lines):
                if line.startswith(f"| `{best_name}` "):
                    lines[i] = line.replace("|  |", "| **best PnL** |", 1)
                    break
        lines.append("")

    lines.append("\n## Best regime-filtered variant per leg (by total PnL)\n")
    for leg in sorted(overall_best):
        lines.append(f"- `{leg}` -> `{overall_best[leg]}`")

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"wrote comparison report to {out_path}")
    for leg, name in sorted(overall_best.items()):
        print(f"  {leg}: {name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quant-dir", default=os.path.join(BACKTEST, "results", "quant_taker_regime_full"))
    ap.add_argument("--out", default=os.path.join(BACKTEST, "reports", "regime_filter", "comparison.md"))
    args = ap.parse_args()
    build(args.quant_dir, args.out)


if __name__ == "__main__":
    main()
