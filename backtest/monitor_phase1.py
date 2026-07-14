#!/usr/bin/env python3
"""Background progress monitor for ETH/SOL Phase 1 coarse taker screen.
Appends a snapshot every 5 minutes to /tmp/phase1_progress.log.
"""
from __future__ import annotations
import glob, json, os, time
from pathlib import Path

RESULTS = Path("/config/backtest/results")
COINS = ["eth", "sol"]
WINDOWS = ["is", "oos"]
REG = {}
for c in COINS:
    with open(f"/config/backtest/coin_full_registry_{c}.json") as fh:
        REG[c] = json.load(fh)


def snapshot():
    lines = [f"--- {time.strftime('%Y-%m-%d %H:%M:%S %Z')} ---"]
    for coin in COINS:
        for window in WINDOWS:
            d = RESULTS / f"{window}_taker_{coin}"
            summaries = sorted(d.glob("*.summary.json")) if d.exists() else []
            done = {p.stem[: -len(".summary")] for p in summaries}
            missing = [n for n in REG[coin] if n not in done]
            lines.append(f"{window}_taker_{coin}: {len(done)}/{len(REG[coin])} done, {len(missing)} missing")
            if summaries:
                rows = []
                for p in summaries:
                    s = json.load(open(p))
                    rows.append((s.get("total_pnl", 0.0), s.get("strategy"), s.get("n_closed", 0)))
                rows.sort(reverse=True)
                for pnl, name, n in rows[:5]:
                    lines.append(f"  {name:55s} pnl={pnl:+8.2f} n={n}")
    lines.append("")
    return "\n".join(lines)


def main():
    log = Path("/tmp/phase1_progress.log")
    while True:
        try:
            with open(log, "a") as fh:
                fh.write(snapshot() + "\n")
        except Exception as exc:
            with open(log, "a") as fh:
                fh.write(f"monitor error: {exc}\n")
        time.sleep(300)


if __name__ == "__main__":
    main()
