#!/usr/bin/env python3
"""Apply Phase-2 promotion criteria to per-coin Phase 1 results.

Criteria (reconstructed from BTC session + user guidance):
  1. IS taker PnL > 0
  2. OOS taker PnL > 0
  3. maxDD% < 25%
  4. n_closed trades >= 50
  5. final equity > $1 (not wiped)

Output:
  results/phase1/<coin>_promoted.json
  results/phase1/<coin>_promoted.md
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def load(coin: str, window: str) -> list[dict]:
    p = Path("results/phase1") / f"{coin}_{window}_summary.json"
    with open(p) as fh:
        return json.load(fh)


def family_of(name: str) -> str:
    if "daily_orb" in name:
        return "daily_orb"
    if "btc_orb" in name or "eth_orb" in name or "sol_orb" in name:
        return "orb"
    return "other"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", required=True, choices=["eth", "sol"])
    args = ap.parse_args()

    is_rows = {r["strategy"]: r for r in load(args.coin, "is")}
    oos_rows = {r["strategy"]: r for r in load(args.coin, "oos")}
    names = sorted(set(is_rows) & set(oos_rows))

    promoted = []
    rejected = []
    for n in names:
        is_r = is_rows[n]
        oos_r = oos_rows[n]
        reasons = []
        if is_r["total_pnl"] <= 0:
            reasons.append("IS<=0")
        if oos_r["total_pnl"] <= 0:
            reasons.append("OOS<=0")
        if is_r["max_dd_pct"] >= 25 or oos_r["max_dd_pct"] >= 25:
            reasons.append("DD>=25%")
        if is_r["n_closed"] < 50 or oos_r["n_closed"] < 50:
            reasons.append("trades<50")
        if is_r["equity"] <= 1 or oos_r["equity"] <= 1:
            reasons.append("wiped")

        if reasons:
            rejected.append({"strategy": n, "family": family_of(n), "reasons": ",".join(reasons),
                             "is_pnl": is_r["total_pnl"], "oos_pnl": oos_r["total_pnl"]})
        else:
            promoted.append({
                "strategy": n,
                "family": family_of(n),
                "is_pnl": is_r["total_pnl"],
                "is_n": is_r["n_closed"],
                "is_wr": is_r["win_rate"],
                "is_dd": is_r["max_dd_pct"],
                "oos_pnl": oos_r["total_pnl"],
                "oos_n": oos_r["n_closed"],
                "oos_wr": oos_r["win_rate"],
                "oos_dd": oos_r["max_dd_pct"],
            })

    promoted.sort(key=lambda r: (r["is_pnl"] + r["oos_pnl"]), reverse=True)

    out_dir = Path("results/phase1")
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / f"{args.coin}_promoted.json", "w") as fh:
        json.dump({"promoted": promoted, "rejected": rejected}, fh, indent=1)

    lines = [f"# Phase-2 promoted strategies — {args.coin.upper()}",
             "",
             f"Promoted: {len(promoted)} / {len(names)} evaluated",
             "",
             "| strategy | family | IS $ | IS n | IS WR% | IS DD% | OOS $ | OOS n | OOS WR% | OOS DD% |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in promoted:
        lines.append(
            f"| {r['strategy']} | {r['family']} | {r['is_pnl']:+.2f} | {r['is_n']} | "
            f"{r['is_wr']:.1f} | {r['is_dd']:.1f} | {r['oos_pnl']:+.2f} | {r['oos_n']} | "
            f"{r['oos_wr']:.1f} | {r['oos_dd']:.1f} |"
        )
    lines += ["", f"Rejected: {len(rejected)}", ""]
    lines += ["| strategy | family | reasons | IS $ | OOS $ |",
              "|---|---|---|---|---|"]
    for r in sorted(rejected, key=lambda x: x["oos_pnl"] + x["is_pnl"], reverse=True)[:30]:
        lines.append(f"| {r['strategy']} | {r['family']} | {r['reasons']} | {r['is_pnl']:+.2f} | {r['oos_pnl']:+.2f} |")

    with open(out_dir / f"{args.coin}_promoted.md", "w") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"{args.coin}: promoted={len(promoted)} rejected={len(rejected)}")
    for r in promoted:
        print(f"  {r['strategy']:50s} IS={r['is_pnl']:+8.2f} OOS={r['oos_pnl']:+8.2f} family={r['family']}")
    non_orb = [r for r in promoted if r["family"] == "other"]
    if non_orb:
        print(f"\n*** NON-ORB PROMOTED for {args.coin}: {len(non_orb)} ***")
        for r in non_orb:
            print(f"  {r['strategy']}")


if __name__ == "__main__":
    main()
