#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - Scalable in-sample backtest runner (sandbox only). Streams every 5m BTC
#     market one at a time (memory-safe), skips the held-out OOS window by
#     date-peek (first-snapshot ET date >= OOS_START is never traded), runs one
#     persistent $200 wallet per strategy, and writes compressed per-strategy
#     trades + summary to ./results/is then uploads to gdrive.
#   - Parallel at the strategy level (each strategy is an independent process);
#     per-strategy checkpoint (existing summary.json => skip) so it is resumable.
# WHY: Run the ~109 faithful strategies over ~15.2k IS markets without holding
#      data in memory, survive long runtimes via checkpoints, and land results
#      on gdrive compressed as they complete.
"""In-sample backtest runner. Usage:
  python3 run_is.py --files is_files_all.txt --workers 4 --limit-strats 0 --only ""
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
import time
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
for p in (HERE, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

import driver  # noqa: E402
import bt_orb  # noqa: E402
import bt_bars  # noqa: E402
import bt_reference  # noqa: E402
from engine.portfolio import Portfolio  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

ET = ZoneInfo("America/New_York")
OOS_START = date(2026, 7, 1)          # last 10 days (Jul 1-10) are held out
# RESULTS/GDRIVE_IS are set in main() from --fill (maker -> is_maker, taker -> is_taker)
# Result dir comes from BT_IS_DIR env var (read at IMPORT time) so that worker
# processes get the right directory under ANY multiprocessing start method.
# Python 3.14 defaults to forkserver: children re-import this module fresh and
# would otherwise miss the main()-time override below (this bit us on the
# laptop — instant pool workers silently wrote into is_maker). The launcher
# sets BT_IS_DIR=is_maker|is_instant; --fill still selects the fill LOGIC.
RESULTS = os.path.join(HERE, "results", os.environ.get("BT_IS_DIR", "is_maker"))
GDRIVE_IS = f"gdrive:trading_backtest/results/{os.environ.get('BT_IS_DIR', 'is_maker')}"

DAILY_ORB_MODULE = "phase_2.daily_orb_v5"


def classify(reg: dict) -> str:
    mod = reg.get("module", "")
    if mod == DAILY_ORB_MODULE:
        return "daily_orb"
    if bt_bars.is_indicator(mod):
        return "indicator"
    return "per_snapshot"


def _oos_skip(snaps: list[dict]) -> bool:
    if not snaps:
        return True
    d = driver._parse_ts(snaps[0]["time"]).astimezone(ET).date()
    return d >= OOS_START


def _oos_skip_arr(arr: dict) -> bool:
    if not arr or not arr.get("t"):
        return True
    from datetime import datetime, timezone
    d = datetime.fromtimestamp(arr["t"][0] / 1000.0, tz=timezone.utc).astimezone(ET).date()
    return d >= OOS_START


def run_per_snapshot(name: str, reg: dict, files: list[str], compact: bool = False,
                     fill: str = "maker") -> dict:
    fn = driver.load_signal_fn(reg)
    pf = Portfolio(name=f"is:{name}", capital=driver.CAPITAL)
    n_markets = 0
    t0 = time.time()
    prog = os.path.join(RESULTS, f"{name}.progress")
    load = driver.load_compact_file if compact else driver.load_market_file
    skip = _oos_skip_arr if compact else _oos_skip
    if compact and fill == "taker":
        run = driver.run_market_taker_arr
    elif compact and fill == "instant":
        run = driver.run_market_instant_arr
    elif compact:
        run = driver.run_market_maker_arr
    else:
        run = driver.run_market_maker
    for i, f in enumerate(files, 1):
        # Honest early-exit: a wallet that cannot cover the 5-contract minimum at
        # the 0.05 price floor (5*0.05 = $0.25) can never trade again, and no
        # active position can settle back into it. Iterating the remaining ~18k
        # markets would be pure waste; the strategy is simply dead, exactly as a
        # live $200 account would be. Trades produced so far are unaffected.
        if pf.cash < 0.25 and not pf.active_trades:
            break
        snaps = load(f)
        if skip(snaps):
            del snaps
            continue
        n_markets += 1
        run(snaps, reg, fn, pf=pf)
        del snaps
        if i % 500 == 0:
            with open(prog, "w") as fh:
                fh.write(f"{name} {i}/{len(files)} mkts={n_markets} "
                         f"closed={len(pf.closed_trades)} cash={pf.cash:.2f} "
                         f"{time.time()-t0:.0f}s\n")
    closed = pf.closed_trades
    total_pnl = sum(t.pnl for t in closed)
    committed = sum(t.entry_notional for t in pf.active_trades.values())
    return {
        "strategy": name, "family": "per_snapshot",
        "module": reg.get("module"),
        "n_markets": n_markets, "n_closed": len(closed),
        "n_active_left": len(pf.active_trades),
        "total_pnl": round(total_pnl, 4), "cash": round(pf.cash, 4),
        "committed": round(committed, 4),
        "equity": round(pf.cash + committed, 4),
        "start_capital": driver.CAPITAL,
        "pnl_pct": round(100 * total_pnl / driver.CAPITAL, 2),
        "runtime_s": round(time.time() - t0, 1),
        "trades": [t.to_dict() for t in closed],
    }


def _shared_is_index(compact: bool = False):
    """Load the one-time shared (date, ts, file) index if built; OOS-filtered.
    Compact mode reads is_index_compact.json.gz (compact pkl.gz paths).
    BT_IS_INDEX overrides the path for per-coin runs."""
    if compact:
        path = os.environ.get("BT_IS_INDEX") or os.path.join(HERE, "is_index_compact.json.gz")
    else:
        path = os.environ.get("BT_IS_INDEX") or os.path.join(HERE, "is_index.json.gz")
    if not os.path.exists(path):
        return None
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        rows = json.load(fh)
    from datetime import date as _date
    out = []
    for d_iso, ts, f in rows:
        d = _date.fromisoformat(d_iso)
        if d >= OOS_START:
            continue
        out.append((d, ts, f))
    return out


def run_daily_orb(name: str, reg: dict, files: list[str], compact: bool = False,
                  fill: str = "maker") -> dict:
    t0 = time.time()
    idx = _shared_is_index(compact)
    out = bt_orb.run_daily_orb(reg, files, oos_start=OOS_START, index=idx,
                               compact=compact, fill=fill)
    committed_pnl = out["total_pnl"]
    return {
        "strategy": name, "family": "daily_orb",
        "module": reg.get("module"),
        "tf_hint": reg.get("tf_hint"), "max_reentries": reg.get("max_reentries"),
        "n_markets": None, "n_closed": out["n_closed"],
        "n_active_left": out["n_active_left"], "n_triggered": out["n_triggered"],
        "total_pnl": out["total_pnl"], "cash": out["cash"],
        "equity": out["equity"], "start_capital": driver.CAPITAL,
        "pnl_pct": round(100 * committed_pnl / driver.CAPITAL, 2),
        "days": out["days"], "runtime_s": round(time.time() - t0, 1),
        "trades": out["trades"],
    }


def run_indicator(name: str, reg: dict, files: list[str], compact: bool = False,
                  fill: str = "maker") -> dict:
    out = bt_bars.run_indicator(reg, files, oos_start=OOS_START, compact=compact, fill=fill)
    return {
        "strategy": name, "family": "indicator",
        "module": reg.get("module"),
        "n_markets": out["n_markets"], "n_closed": out["n_closed"],
        "n_active_left": out["n_active_left"], "n_triggered": out["n_triggered"],
        "total_pnl": out["total_pnl"], "cash": out["cash"],
        "committed": out["committed"], "equity": out["equity"],
        "start_capital": driver.CAPITAL,
        "pnl_pct": round(100 * out["total_pnl"] / driver.CAPITAL, 2),
        "runtime_s": out["runtime_s"], "trades": out["trades"],
    }


def run_strategy(name: str, files: list[str], compact: bool = False,
                 fill: str = "maker") -> str:
    reg = driver.STRATEGIES[name]
    fam = classify(reg)
    summ_path = os.path.join(RESULTS, f"{name}.summary.json")
    if os.path.exists(summ_path):
        return f"SKIP(exists) {name}"
    if fam == "indicator":
        res = run_indicator(name, reg, files, compact, fill)
    elif fam == "daily_orb":
        res = run_daily_orb(name, reg, files, compact, fill)
    else:
        res = run_per_snapshot(name, reg, files, compact, fill)
    trades = res.pop("trades", [])
    with gzip.open(os.path.join(RESULTS, f"{name}.trades.jsonl.gz"), "wt",
                   encoding="utf-8") as fh:
        for t in trades:
            fh.write(json.dumps(t, default=str) + "\n")
    with open(summ_path, "w") as fh:
        json.dump(res, fh, indent=1, default=str)
    return (f"DONE {name} closed={res.get('n_closed')} pnl={res.get('total_pnl')} "
            f"equity={res.get('equity')} rt={res.get('runtime_s')}s")


def upload(name: str) -> None:
    for ext in ("trades.jsonl.gz", "summary.json"):
        src = os.path.join(RESULTS, f"{name}.{ext}")
        if os.path.exists(src):
            subprocess.run(["rclone", "copyto", src, f"{GDRIVE_IS}/{name}.{ext}"],
                           capture_output=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", required=True, help="local file listing (one path per line)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", default="", help="comma-separated strategy names to run (subset)")
    ap.add_argument("--families", default="", help="comma list of families to include: per_snapshot,daily_orb,skip_indicator")
    ap.add_argument("--limit-strats", type=int, default=0, help="run first N strategies only")
    ap.add_argument("--upload", action="store_true", help="rclone results to gdrive as they complete")
    ap.add_argument("--compact", action="store_true", help="read compact pkl.gz arrays (files list points at /tmp/btc5m_compact)")
    ap.add_argument("--fill", choices=["maker", "taker", "instant"], default="maker",
                    help="maker = resting bid at signal price (pessimistic bound); taker = live check_entry walk-the-ask (fidelity model)")
    args = ap.parse_args()

    # output dir is fixed at import time from BT_IS_DIR (see module header);
    # --fill selects the fill model only. Sanity: warn if they disagree.
    want = f"is_{args.fill}"
    if os.path.basename(RESULTS) != want:
        print(f"WARN: BT_IS_DIR dir {RESULTS} != --fill {args.fill} ({want})",
              flush=True)
    os.makedirs(RESULTS, exist_ok=True)
    with open(args.files) as fh:
        files = [l.strip() for l in fh if l.strip()]
    print(f"files listed: {len(files)}", flush=True)

    extra_path = os.environ.get("BT_EXTRA_STRATEGIES")
    if os.environ.get("BT_ONLY_EXTRA_REGISTRY") and extra_path:
        with open(extra_path, "r", encoding="utf-8") as _fh:
            names = list(json.load(_fh).keys())
    else:
        names = list(driver.STRATEGIES.keys())
    if args.only:
        keep = set(args.only.split(","))
        names = [n for n in names if n in keep]
    if args.families:
        fams = set(args.families.split(","))
        names = [n for n in names if classify(driver.STRATEGIES[n]) in fams]
    # Fast families first so results stream in early; the any-mode ORB tail last.
    _prio = {"per_snapshot": 0, "indicator": 1, "daily_orb": 2}
    names.sort(key=lambda n: (_prio.get(classify(driver.STRATEGIES[n]), 9), n))
    if args.limit_strats:
        names = names[: args.limit_strats]
    print(f"strategies to run: {len(names)} "
          f"(per_snapshot={sum(1 for n in names if classify(driver.STRATEGIES[n])=='per_snapshot')}, "
          f"daily_orb={sum(1 for n in names if classify(driver.STRATEGIES[n])=='daily_orb')}, "
          f"indicator={sum(1 for n in names if classify(driver.STRATEGIES[n])=='indicator')})",
          flush=True)

    bt_reference.load()
    print(f"reference rows loaded: {bt_reference.coverage()}", flush=True)

    from concurrent.futures import ProcessPoolExecutor, as_completed
    t0 = time.time()
    done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_strategy, n, files, args.compact, args.fill): n for n in names}
        for fut in as_completed(futs):
            n = futs[fut]
            try:
                msg = fut.result()
            except Exception as e:
                msg = f"FAIL {n}: {type(e).__name__}: {e}"
            done += 1
            print(f"[{done}/{len(names)}] {msg} ({time.time()-t0:.0f}s elapsed)", flush=True)
            if args.upload and msg.startswith("DONE"):
                upload(n)
    print(f"ALL DONE in {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
