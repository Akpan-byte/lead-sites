#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - Final report generator: merges IS maker/instant summaries, quant suites
#     (both bounds), and OOS scores (both bounds) into one bracketed leaderboard.
#     Every strategy gets a maker_pnl <-> instant_pnl bracket (reality lives
#     between), DSR-deflated significance, and untouched-OOS confirmation.
# WHY: one artifact the user can read to decide what goes live; every number
#      traceable to results/{is,quant,oos}_{maker,instant}.
"""Usage: python3 report_generator.py [--upload]"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
R = os.path.join(HERE, "results")


def load_summaries(d: str) -> dict:
    out = {}
    for f in glob.glob(os.path.join(R, d, "*.summary.json")):
        s = json.load(open(f))
        out[s["strategy"]] = s
    return out


def load_quant(d: str) -> dict:
    out = {}
    for f in glob.glob(os.path.join(R, d, "*.quant.json")):
        q = json.load(open(f))
        out[q["strategy"]] = q
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upload", action="store_true")
    args = ap.parse_args()

    is_m, is_i = load_summaries("is_maker"), load_summaries("is_instant")
    oos_m, oos_i = load_summaries("oos_maker"), load_summaries("oos_instant")
    q_m, q_i = load_quant("quant_maker"), load_quant("quant_instant")

    names = sorted(set(is_m) | set(is_i))
    rows = []
    for n in names:
        a, b = is_m.get(n, {}), is_i.get(n, {})
        om, oi = oos_m.get(n, {}), oos_i.get(n, {})
        qm, qi = q_m.get(n, {}), q_i.get(n, {})
        mk_pnl = a.get("total_pnl"); in_pnl = b.get("total_pnl")
        lo = min(x for x in (mk_pnl, in_pnl) if x is not None) if (mk_pnl is not None or in_pnl is not None) else None
        hi = max(x for x in (mk_pnl, in_pnl) if x is not None) if (mk_pnl is not None or in_pnl is not None) else None
        dsr = (qm.get("risk") or {}).get("dsr")
        psr = (qm.get("risk") or {}).get("psr")
        dd = (qm.get("drawdown") or {}).get("max_dd_pct")
        ruin = (qm.get("monte_carlo_50k") or {}).get("p_ruin")
        rows.append({
            "strategy": n, "family": a.get("family") or b.get("family"),
            "is_maker_pnl": mk_pnl, "is_instant_pnl": in_pnl,
            "bracket_lo": lo, "bracket_hi": hi,
            "is_maker_trades": a.get("n_closed"), "is_instant_trades": b.get("n_closed"),
            "oos_maker_pnl": om.get("total_pnl"), "oos_instant_pnl": oi.get("total_pnl"),
            "oos_maker_trades": om.get("n_closed"), "oos_instant_trades": oi.get("n_closed"),
            "psr": psr, "dsr": dsr, "maxdd_pct": dd, "p_ruin": ruin,
        })
    rows.sort(key=lambda r: (r["bracket_lo"] if r["bracket_lo"] is not None else -1e9), reverse=True)

    with open(os.path.join(R, "final_leaderboard.json"), "w") as fh:
        json.dump(rows, fh, indent=1)

    L = ["# BTC-5m final leaderboard — IS May8–Jun30 (54d), OOS Jul1–10 untouched",
         "", "$200 start/strategy, 0.5% risk, min 5 contracts, snipe exit 0.97,",
         "fees shares*0.07*p*(1-p). maker=resting-bid pessimistic, instant=optimistic.",
         "Reality lives inside [bracket].", "",
         "| # | strategy | IS maker$ | IS instant$ | bracket lo→hi | mk trd | in trd | OOS mk$ | OOS in$ | DSR | PSR | maxDD% | P(ruin) |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        f = lambda x: "—" if x is None else f"{x:.2f}"
        L.append(
            f"| {i} | {r['strategy']} | {f(r['is_maker_pnl'])} | {f(r['is_instant_pnl'])} | "
            f"{f(r['bracket_lo'])}→{f(r['bracket_hi'])} | {r['is_maker_trades'] or '—'} | {r['is_instant_trades'] or '—'} | "
            f"{f(r['oos_maker_pnl'])} | {f(r['oos_instant_pnl'])} | "
            f"{'—' if r['dsr'] is None else f'{r['dsr']:.3f}'} | {'—' if r['psr'] is None else f'{r['psr']:.3f}'} | "
            f"{'—' if r['maxdd_pct'] is None else f'{r['maxdd_pct']:.1f}'} | {'—' if r['p_ruin'] is None else f'{r['p_ruin']:.3f}'} |")
    # scale variants under taker fill (only runner honoring scale_in/max_adds).
    # In is_maker/is_instant these ran WITHOUT scale-in (engine gate) so they
    # read as plain-any; the taker numbers below are the true scale behavior.
    scale = load_summaries("is_scale")
    if scale:
        L += ["", "## Scale-in variants (taker fill = true scale_in semantics)",
              "", "| strategy | trades | pnl$ | equity$ | note |",
              "|---|---|---|---|---|"]
        for n in sorted(scale):
            s = scale[n]
            L.append(f"| {n} | {s.get('n_closed')} | {s.get('total_pnl')} | "
                     f"{s.get('equity')} | scale_in honored, max_adds enforced |")
    with open(os.path.join(R, "final_leaderboard.md"), "w") as fh:
        fh.write("\n".join(L) + "\n")
    print(f"leaderboard: {len(rows)} strategies -> results/final_leaderboard.md")
    print("\nTOP 15 by pessimistic bracket edge:")
    for r in rows[:15]:
        print(f"  {r['strategy']:45s} lo={r['bracket_lo']!s:>8} hi={r['bracket_hi']!s:>8} "
              f"oos_mk={r['oos_maker_pnl']!s:>8} dsr={r['dsr']}")
    if args.upload:
        subprocess.run(["rclone", "copyto", os.path.join(R, "final_leaderboard.md"),
                        "gdrive:trading_backtest/results/final_leaderboard.md"], capture_output=True)
        subprocess.run(["rclone", "copyto", os.path.join(R, "final_leaderboard.json"),
                        "gdrive:trading_backtest/results/final_leaderboard.json"], capture_output=True)
        print("uploaded leaderboard to gdrive")


if __name__ == "__main__":
    main()
