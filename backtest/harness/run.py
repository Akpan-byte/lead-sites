# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - Created harness/run.py: orchestrates the feed-swap backtest. Builds a
#     market index (market_id -> window_start), shards the 113 registry
#     strategies across worker processes, replays day-by-day with checkpoints,
#     and syncs results to gdrive:trading_backtest/results/<tag>/.
# WHY: One entry point for the IS run (May 8 - Jun 30), the sealed OOS score
#      (Jul 1 - Jul 10), and small validation slices. Crash-resumable per day.
"""Backtest orchestrator: shard strategies, replay days, sync results."""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import gzip
import json
import os
import pickle
import subprocess
import sys
import time

REPO = "/config/backtest_repo"
BT = "/config/backtest"
sys.path.insert(0, REPO)
sys.path.insert(0, BT)


# ---------------------------------------------------------------------------
# Market index: market_id -> (window_start epoch, path). Cached to disk.
# ---------------------------------------------------------------------------
def build_index(data_dir: str, cache_path: str) -> list[dict]:
    if os.path.exists(cache_path):
        with open(cache_path) as fh:
            return json.load(fh)
    import re

    from harness.markets import WINDOW_SECONDS

    time_re = re.compile(r'"time"\s*:\s*"(\d{4}-\d{2}-\d{2}T[\d:.]+Z)"')
    entries = []
    files = sorted(f for f in os.listdir(data_dir) if f.endswith(".json.gz"))
    for i, f in enumerate(files):
        path = os.path.join(data_dir, f)
        try:
            # Stream-decompress just the head of the file; the first snapshot's
            # "time" is all we need for window assignment.
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                head = fh.read(8192)
            t0 = time_re.search(head).group(1)
        except Exception as exc:
            print(f"index skip {f}: {exc}", flush=True)
            continue
        ts = dt.datetime.strptime(t0, "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()
        ws = int(ts // WINDOW_SECONDS) * WINDOW_SECONDS
        entries.append({"market_id": f.split(".")[0], "window_start": ws, "path": path})
        if (i + 1) % 4000 == 0:
            print(f"indexed {i + 1}/{len(files)}", flush=True)
    entries.sort(key=lambda e: e["window_start"])
    with open(cache_path, "w") as fh:
        json.dump(entries, fh)
    return entries


def group_by_day(entries, start_date, end_date):
    """Return {date_iso: [entries]} for window_start within [start, end] inclusive."""
    d0 = dt.date.fromisoformat(start_date)
    d1 = dt.date.fromisoformat(end_date)
    days = {}
    for e in entries:
        d = dt.datetime.utcfromtimestamp(e["window_start"]).date()
        if d0 <= d <= d1:
            days.setdefault(d.isoformat(), []).append(e)
    return days


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
def _resolve_signal(module_name, fn_name):
    import importlib
    mod = importlib.import_module(f"signals.{module_name}")
    return getattr(mod, fn_name), mod


def _worker(payload):
    (strategies, day_list, days_map, out_dir, tag, worker_id, resume) = payload
    from harness import btclock
    btclock.install()
    clock = btclock.Clock()

    from engine.strategy_registry import STRATEGIES
    from harness.feed import BacktestFeed
    from harness.markets import load_market_file
    from harness.recorder import Recorder
    from harness.refdata import BinanceReference
    from harness.driver import StrategyContext, replay_day

    ref = BinanceReference()
    feed = BacktestFeed(ref, clock)
    log_path = os.path.join(out_dir, f"worker_{worker_id}.log")

    def log(msg):
        line = f"{btclock.real_now_iso()} [w{worker_id}] {msg}"
        with open(log_path, "a") as fh:
            fh.write(line + "\n")
        print(line, flush=True)

    ctxs = []
    for name in strategies:
        entry = STRATEGIES[name]
        fn, mod = _resolve_signal(entry["module"], entry["fn"])
        # Isolate module-global state per strategy (swap around each call) and
        # disable on-disk persistence — restart recovery is meaningless in a
        # continuous replay; our own checkpoints handle resume.
        mod_state = None
        if hasattr(mod, "_STATE"):
            mod_state = {}
            if hasattr(mod, "_save_key"):
                mod._save_key = lambda *a, **k: None
            if hasattr(mod, "_load_key"):
                mod._load_key = lambda *a, **k: None
        rec = Recorder(out_dir, name, fresh=not resume)
        ctx = StrategyContext(name, entry, fn, mod, feed, clock, rec)
        ctx._module_state = mod_state
        ctxs.append(ctx)

    ckpt_path = os.path.join(out_dir, f"worker_{worker_id}.checkpoint.pkl")
    done_days = set()
    if resume and os.path.exists(ckpt_path):
        with open(ckpt_path, "rb") as fh:
            ck = pickle.load(fh)
        done_days = set(ck.get("done_days", []))
        for ctx in ctxs:
            st = ck["strategies"].get(ctx.strategy)
            if not st:
                continue
            ctx.portfolio.load_state(st["portfolio"])
            ctx.spot_history = st["spot_history"]
            ctx.oracle_spot = st["oracle_spot"]
            ctx.diag.update(st["diag"])
            ctx._module_state = st["module_state"]
        log(f"resumed checkpoint with {len(done_days)} days done")

    for day in day_list:
        if day in done_days:
            continue
        t0 = time.monotonic()  # monotonic: the fake clock patches time.time
        entries = days_map.get(day, [])
        markets = []
        for e in entries:
            try:
                m = load_market_file(e["path"], ref)
                if m is not None:
                    markets.append(m)
            except Exception as exc:
                log(f"market load error {e['market_id']}: {exc}")
        replay_day(ctxs, markets, ref, clock, feed)
        for ctx in ctxs:
            ctx.recorder.flush()
        # checkpoint
        with open(ckpt_path, "wb") as fh:
            pickle.dump({
                "done_days": sorted(done_days | {day}),
                "strategies": {
                    c.strategy: {
                        "portfolio": c.portfolio.state_dict(),
                        "spot_history": c.spot_history,
                        "oracle_spot": c.oracle_spot,
                        "diag": c.diag,
                        "module_state": c._module_state,
                    } for c in ctxs
                },
            }, fh)
        done_days.add(day)
        elapsed = time.monotonic() - t0
        summary = "; ".join(
            f"{c.strategy.split('.')[-1]}: tr={c.diag['trades_opened']} pnl={c.portfolio.perf()['total_pnl']:+.2f}"
            for c in ctxs[:6]
        )
        log(f"day {day} done markets={len(markets)} {elapsed:.0f}s | {summary}")
        # Sync results to gdrive after each day (small jsonl files).
        subprocess.run(
            ["rclone", "copy", out_dir, f"gdrive:trading_backtest/results/{tag}/",
             "--exclude", "*.checkpoint.pkl", "--transfers", "8", "--quiet"],
            capture_output=True,
        )
    for ctx in ctxs:
        ctx.recorder.close()
    log(f"worker done: {len(done_days)} days")
    return worker_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="/config/bt_data/5m")
    ap.add_argument("--out-dir", default="/config/backtest/out")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--tag", required=True, help="gdrive results folder (is/oos/val)")
    ap.add_argument("--procs", type=int, default=4)
    ap.add_argument("--strategies", default="", help="comma subset; default=all 113")
    ap.add_argument("--limit-markets", type=int, default=0)
    ap.add_argument("--resume", action="store_true", help="resume from per-worker checkpoints")
    args = ap.parse_args()

    from engine.strategy_registry import STRATEGIES
    names = sorted(STRATEGIES)
    if args.strategies:
        keep = set(args.strategies.split(","))
        names = [n for n in names if n in keep]
    print(f"strategies: {len(names)}", flush=True)

    cache_name = f"index_{os.path.basename(args.data_dir.rstrip('/'))}.json"
    index = build_index(args.data_dir, os.path.join(BT, cache_name))
    if args.limit_markets:
        index = index[: args.limit_markets]
    days_map = group_by_day(index, args.start, args.end)
    day_list = sorted(days_map)
    total_markets = sum(len(v) for v in days_map.values())
    print(f"days: {len(day_list)} markets: {total_markets} range {day_list[0] if day_list else '-'}..{day_list[-1] if day_list else '-'}", flush=True)

    os.makedirs(args.out_dir, exist_ok=True)
    # Shard strategies contiguously across procs.
    k = max(1, args.procs)
    shards = [names[i::k] for i in range(k)]
    payloads = [
        (shards[i], day_list, days_map, args.out_dir, args.tag, i, args.resume)
        for i in range(k) if shards[i]
    ]
    with cf.ProcessPoolExecutor(max_workers=k) as ex:
        for wid in ex.map(_worker, payloads):
            print(f"worker {wid} finished", flush=True)
    print("ALL WORKERS DONE", flush=True)


if __name__ == "__main__":
    main()
