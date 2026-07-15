#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-15  kilo
#   - Created combine_orb_fade_full_stack.py: combine the 6 per-leg orb_fade
#     filtered trade files (produced by apply_orb_fade_filters.py) into a single
#     shared-wallet replay for the full stack, for both Hyperliquid and Binance
#     reference feeds.
#   - Reads gzipped jsonl trade files, re-sizes each trade from a shared $200
#     wallet using 0.5% risk per trade and a 5-share minimum (matching the live
#     paper trader sizing), and computes total PnL, win rate, max drawdown, etc.
#   - Writes a JSON + Markdown report to backtest/reports/orb_fade_full_stack/.
# WHY: The btc_orb_feed_compare workflow validates each leg in isolation with
#      its own $200 silo. This script gives the realistic no-silo combined
#      performance of the full stack the user intends to trade live.
"""Combine orb_fade per-leg filtered trades into a shared-wallet full-stack replay.

Usage:
    python3 combine_orb_fade_full_stack.py <artifact_dir>

Expects artifact_dir to contain subdirectories (one per downloaded artifact)
with trade files at backtest/results/is_<feed>/btc_<leg>.trades.jsonl.gz.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import sys
from pathlib import Path

CAPITAL_START = 200.0
RISK_PCT = 0.005
MIN_SHARES = 5.0

LEGS = [
    "b1m_liqpull40",
    "b1m_imbslope30",
    "b1m_chand40",
    "l5m_mom30",
    "l5m_liqpull60",
    "l3m_bbpct40",
]


def load_trades(path: Path) -> list[dict]:
    rows = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            t = json.loads(line)
            rows.append({
                "opened_at": float(t["opened_at"]),
                "closed_at": float(t.get("closed_at", t["opened_at"])),
                "entry_price": float(t["entry_price"]),
                "shares": float(t["shares"]),
                "pnl": float(t["pnl"]),
                "direction": t.get("direction", ""),
                "condition_id": t.get("condition_id", ""),
                "leg": path.stem.replace("_perf", "").replace(".trades", ""),
            })
    return rows


def replay(trades: list[dict]) -> dict:
    """Shared-wallet compound replay."""
    trades = sorted(trades, key=lambda x: x["opened_at"])
    equity = CAPITAL_START
    peak = equity
    max_dd_pct = 0.0
    total_pnl = 0.0
    wins = 0
    losses = 0
    out_trades = []

    for t in trades:
        # desired shares from shared wallet
        desired = max(MIN_SHARES, RISK_PCT * equity / t["entry_price"])
        # round to whole shares (live bot uses integer shares)
        new_shares = math.floor(desired)
        if new_shares < MIN_SHARES:
            # not enough capital for min position
            continue
        # scale net pnl linearly with share count
        per_share = t["pnl"] / t["shares"] if t["shares"] else 0.0
        pnl_eff = per_share * new_shares
        equity += pnl_eff
        total_pnl += pnl_eff
        if pnl_eff > 0:
            wins += 1
        else:
            losses += 1
        if equity > peak:
            peak = equity
        dd_pct = (peak - equity) / peak if peak else 0.0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
        out_trades.append({
            **t,
            "shares_eff": new_shares,
            "pnl_eff": pnl_eff,
            "equity_after": equity,
        })

    n = wins + losses
    return {
        "n_trades": n,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / n if n else 0.0,
        "total_pnl": total_pnl,
        "return_pct": 100.0 * total_pnl / CAPITAL_START,
        "final_equity": equity,
        "max_dd_pct": 100.0 * max_dd_pct,
        "trades": out_trades,
    }


def write_trade_file(path: Path, trades: list[dict]):
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for t in trades:
            rec = {
                "opened_at": t["opened_at"],
                "closed_at": t["closed_at"],
                "entry_price": t["entry_price"],
                "shares": t["shares_eff"],
                "pnl": t["pnl_eff"],
                "direction": t["direction"],
                "condition_id": t["condition_id"],
            }
            fh.write(json.dumps(rec) + "\n")


def main():
    artifact_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("artifacts")
    reports_dir = Path("backtest/reports/orb_fade_full_stack")
    reports_dir.mkdir(parents=True, exist_ok=True)

    report = {"feeds": {}}

    for feed in ("hl", "bn"):
        all_trades: list[dict] = []
        for leg in LEGS:
            name = f"btc_{leg}.trades.jsonl.gz"
            # search recursively under artifact_dir
            matches = list(artifact_dir.rglob(f"results/is_{feed}/{name}"))
            if not matches:
                print(f"WARN: no trades for {feed}/{leg}")
                continue
            for m in matches:
                all_trades.extend(load_trades(m))
        print(f"{feed}: loaded {len(all_trades)} filtered trades")
        if not all_trades:
            continue
        res = replay(all_trades)
        out_trades = res.pop("trades")
        report["feeds"][feed] = res

        # write combined trades so quant_suite can run on it later if desired
        results_dir = Path(f"backtest/results/is_{feed}")
        results_dir.mkdir(parents=True, exist_ok=True)
        write_trade_file(results_dir / "full_stack.trades.jsonl.gz", out_trades)

    with open(reports_dir / "full_stack.json", "w") as fh:
        json.dump(report, fh, indent=2)

    md = ["# orb_fade full-stack shared-wallet replay", ""]
    md.append(f"- start capital: ${CAPITAL_START:.0f}")
    md.append(f"- risk per trade: {RISK_PCT*100:.1f}%")
    md.append(f"- min shares: {MIN_SHARES:.0f}")
    md.append("")
    md.append("| feed | trades | win rate % | total PnL $ | return % | final equity $ | max DD % |")
    md.append("|---|---:|---:|---:|---:|---:|---:|")
    for feed, r in report["feeds"].items():
        md.append(
            f"| {feed} | {r['n_trades']} | {r['win_rate']*100:.2f} | "
            f"{r['total_pnl']:.2f} | {r['return_pct']:.2f} | "
            f"{r['final_equity']:.2f} | {r['max_dd_pct']:.2f} |"
        )
    with open(reports_dir / "full_stack.md", "w") as fh:
        fh.write("\n".join(md) + "\n")

    print("wrote", reports_dir / "full_stack.json")
    print("wrote", reports_dir / "full_stack.md")


if __name__ == "__main__":
    main()
