#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-17  kilo_regime_filter
#   - Worker script for one global worker in the chunked regime-filter backtest.
#   - Reads jobs.json, filters to jobs where job_idx % total_workers == worker_id,
#     runs each assigned chunk independently, and writes compressed partial trades.
# WHY: Lets VM, laptop, and GitHub Actions share one deterministic job queue so
#      every core/thread across every target stays busy.
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
import traceback
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BACKTEST = os.path.dirname(HERE)
SRC = os.path.join(BACKTEST, "src")
for p in (BACKTEST, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

import driver  # noqa: E402
from engine.portfolio import Portfolio  # noqa: E402


def _load_registry(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        reg = json.load(fh)
    driver.STRATEGIES.update(reg)
    return reg


def _run_fill(runner_name: str):
    if runner_name == "taker":
        return driver.run_market_taker_arr
    if runner_name == "instant":
        return driver.run_market_instant_arr
    if runner_name == "maker":
        return driver.run_market_maker_arr
    raise ValueError(f"unknown fill {runner_name}")


def run_job(job: Dict[str, Any], registry: Dict[str, Any], partial_dir: str) -> str:
    variant = job["variant"]
    chunk_idx = job["chunk_idx"]
    chunk_path = job["chunk_path"]
    fill = job.get("fill", "taker")

    reg_entry = registry[variant]
    signal_fn = driver.load_signal_fn(reg_entry)

    with open(chunk_path, "r", encoding="utf-8") as fh:
        files = [l.strip() for l in fh if l.strip()]

    pf = Portfolio(name=f"is:{variant}:c{chunk_idx}", capital=driver.CAPITAL)
    runner = _run_fill(fill)

    partial_base = os.path.join(partial_dir, f"{variant}_chunk{chunk_idx:02d}")
    trades_path = partial_base + ".trades.jsonl.gz"
    summ_path = partial_base + ".summary.json"
    os.makedirs(partial_dir, exist_ok=True)

    t0 = time.time()
    n_markets = 0
    n_signals = 0
    n_triggered = 0
    n_closed = 0

    with gzip.open(trades_path, "wt", encoding="utf-8") as out:
        for i, f in enumerate(files, 1):
            # Honest early-exit mirror of run_is.py: a dead wallet cannot trade.
            if pf.cash < 0.25 and not pf.active_trades:
                print(f"  [{variant} c{chunk_idx}] wallet depleted at market {i}, exiting chunk early",
                      flush=True)
                break
            try:
                arr = driver.load_compact_file(f)
            except Exception as e:
                print(f"  [{variant} c{chunk_idx}] SKIP load {f}: {e}", flush=True)
                continue
            if not arr or not arr.get("t"):
                continue
            n_markets += 1
            res = runner(arr, reg_entry, signal_fn, pf=pf)
            n_signals += res.get("n_signals", 0)
            n_triggered += res.get("n_triggered", 0)
            n_closed += res.get("n_closed", 0)
            for tr in res.get("trades", []):
                out.write(json.dumps(tr, default=str) + "\n")

            if i % 500 == 0:
                print(f"  [{variant} c{chunk_idx}] {i}/{len(files)} "
                      f"closed={n_closed} cash={pf.cash:.2f} "
                      f"{time.time()-t0:.0f}s", flush=True)

    committed = sum(t.entry_notional for t in pf.active_trades.values())
    total_pnl = sum(t.pnl for t in pf.closed_trades)
    summary = {
        "variant": variant,
        "chunk_idx": chunk_idx,
        "fill": fill,
        "n_markets": n_markets,
        "n_signals": n_signals,
        "n_triggered": n_triggered,
        "n_closed": n_closed,
        "n_active_left": len(pf.active_trades),
        "total_pnl": round(total_pnl, 4),
        "cash": round(pf.cash, 4),
        "committed": round(committed, 4),
        "equity": round(pf.cash + committed, 4),
        "start_capital": driver.CAPITAL,
        "runtime_s": round(time.time() - t0, 1),
    }
    with open(summ_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
    return (f"DONE {variant} chunk {chunk_idx}: markets={n_markets} "
            f"closed={n_closed} pnl={summary['total_pnl']:.2f} "
            f"rt={summary['runtime_s']}s")


def _partial_paths(job: Dict[str, Any], partial_dir: str) -> Tuple[str, str]:
    partial_base = os.path.join(
        partial_dir, f"{job['variant']}_chunk{job['chunk_idx']:02d}"
    )
    return partial_base + ".trades.jsonl.gz", partial_base + ".summary.json"


def _already_done(job: Dict[str, Any], partial_dir: str) -> bool:
    trades_path, summ_path = _partial_paths(job, partial_dir)
    return (
        os.path.exists(summ_path)
        and os.path.exists(trades_path)
        and os.path.getsize(trades_path) > 0
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker-id", type=int, required=True)
    ap.add_argument("--total-workers", type=int, required=True)
    ap.add_argument("--registry", default=os.path.join(BACKTEST, "combined_trend_regime.json"))
    ap.add_argument("--jobs", default=os.path.join(HERE, "jobs.json"))
    ap.add_argument("--partial-dir", default=os.path.join(HERE, "partials"))
    ap.add_argument("--limit", type=int, default=0, help="run first N matching jobs only")
    ap.add_argument("--skip-existing", action="store_true", default=True,
                    help="skip jobs whose non-empty partial files already exist")
    ap.add_argument("--no-skip-existing", action="store_false", dest="skip_existing",
                    help="re-run jobs even if partial files exist")
    args = ap.parse_args()

    with open(args.jobs, "r", encoding="utf-8") as fh:
        jobs = json.load(fh)

    registry = _load_registry(args.registry)

    todo = [j for j in jobs if j["job_idx"] % args.total_workers == args.worker_id]
    if args.limit:
        todo = todo[:args.limit]

    print(f"worker {args.worker_id}/{args.total_workers}: {len(todo)} jobs", flush=True)
    t0 = time.time()
    for i, job in enumerate(todo, 1):
        if args.skip_existing and _already_done(job, args.partial_dir):
            print(f"SKIP-EXISTING {job['variant']} chunk {job['chunk_idx']} [{i}/{len(todo)}]", flush=True)
            continue
        msg = f"START {job['variant']} chunk {job['chunk_idx']} [{i}/{len(todo)}]"
        print(msg, flush=True)
        try:
            res = run_job(job, registry, args.partial_dir)
        except Exception as e:
            traceback.print_exc()
            res = f"FAIL {job['variant']} chunk {job['chunk_idx']}: {type(e).__name__}: {e}"
        print(f"[{i}/{len(todo)}] {res} ({time.time()-t0:.0f}s elapsed)", flush=True)
    print(f"worker {args.worker_id} finished in {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
