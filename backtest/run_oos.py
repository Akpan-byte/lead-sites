#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - OOS scorer: runs every strategy ONCE over the held-out window (ET date
#     >= 2026-07-01, the last 10 days) with the same persistent-$200 wallet /
#     maker-fill semantics as the IS run. Writes results/oos/<name>.* and can
#     upload. This window is never tuned on; scored a single time at the end.
# WHY: user rule — keep last 10 days untouched for honest out-of-sample scoring.
"""OOS scorer. Usage: python3 run_oos.py --files is_files_compact.txt --workers 4 --compact [--upload]"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
for p in (HERE, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

import driver  # noqa: E402
import bt_orb  # noqa: E402
import bt_bars  # noqa: E402
import bt_reference  # noqa: E402
import run_is  # noqa: E402
from engine.portfolio import Portfolio  # noqa: E402

ET = ZoneInfo("America/New_York")
OOS_START = date(2026, 7, 1)
# env-var result dir (import-time) for forkserver safety — see run_is.py header.
RESULTS = os.path.join(HERE, "results", os.environ.get("BT_OOS_DIR", "oos"))
GDRIVE_OOS = f"gdrive:trading_backtest/results/{os.environ.get('BT_OOS_DIR', 'oos')}"


def _keep_oos_arr(arr: dict) -> bool:
    if not arr or not arr.get("t"):
        return False
    d = datetime.fromtimestamp(arr["t"][0] / 1000.0, tz=timezone.utc).astimezone(ET).date()
    return d >= OOS_START


def run_per_snapshot_oos(name: str, reg: dict, files: list[str], fill: str = "maker") -> dict:
    fn = driver.load_signal_fn(reg)
    pf = Portfolio(name=f"oos:{name}", capital=driver.CAPITAL)
    n_markets = 0
    t0 = time.time()
    if fill == "taker":
        run = driver.run_market_taker_arr
    elif fill == "instant":
        run = driver.run_market_instant_arr
    else:
        run = driver.run_market_maker_arr
    for f in files:
        arr = driver.load_compact_file(f)
        if not _keep_oos_arr(arr):
            del arr
            continue
        n_markets += 1
        run(arr, reg, fn, pf=pf)
        del arr
    closed = pf.closed_trades
    total_pnl = sum(t.pnl for t in closed)
    committed = sum(t.entry_notional for t in pf.active_trades.values())
    return {"strategy": name, "family": "per_snapshot", "n_markets": n_markets,
            "n_closed": len(closed), "n_active_left": len(pf.active_trades),
            "total_pnl": round(total_pnl, 4), "cash": round(pf.cash, 4),
            "committed": round(committed, 4), "equity": round(pf.cash + committed, 4),
            "start_capital": driver.CAPITAL,
            "pnl_pct": round(100 * total_pnl / driver.CAPITAL, 2),
            "runtime_s": round(time.time() - t0, 1),
            "trades": [t.to_dict() for t in closed]}


def run_daily_orb_oos(name: str, reg: dict, files: list[str], fill: str = "maker") -> dict:
    t0 = time.time()
    idx = bt_orb.build_index_compact(files, oos_start=None)
    idx = [r for r in idx if r[0] >= OOS_START]
    out = bt_orb.run_daily_orb(reg, files, oos_start=None, index=idx, compact=True, fill=fill)
    return {"strategy": name, "family": "daily_orb", "n_markets": None,
            "n_closed": out["n_closed"], "n_active_left": out["n_active_left"],
            "n_triggered": out["n_triggered"], "total_pnl": out["total_pnl"],
            "cash": out["cash"], "equity": out["equity"],
            "start_capital": driver.CAPITAL,
            "pnl_pct": round(100 * out["total_pnl"] / driver.CAPITAL, 2),
            "days": out["days"], "runtime_s": round(time.time() - t0, 1),
            "trades": out["trades"]}


def run_indicator_oos(name: str, reg: dict, files: list[str], fill: str = "maker") -> dict:
    bt_reference.load()
    module = reg["module"]
    mod = bt_bars._load_module_by_path(module)
    fn = getattr(mod, reg["fn"])
    clock_holder = [0]
    bt_bars._patch_bootstrap(mod, module, clock_holder)
    pf = Portfolio(name=f"oosind:{module}", capital=driver.CAPITAL)
    if fill == "taker":
        run = driver.run_market_taker_arr
    elif fill == "instant":
        run = driver.run_market_instant_arr
    else:
        run = driver.run_market_maker_arr
    n_markets = 0; n_triggered = 0; t0 = time.time()
    for f in files:
        arr = driver.load_compact_file(f)
        if not _keep_oos_arr(arr):
            del arr
            continue
        clock_holder[0] = int(arr["t"][0])
        n_markets += 1
        r = run(arr, reg, fn, pf=pf)
        n_triggered += r.get("n_triggered", 0)
        del arr
    closed = pf.closed_trades
    total_pnl = sum(t.pnl for t in closed)
    committed = sum(t.entry_notional for t in pf.active_trades.values())
    return {"strategy": name, "family": "indicator", "n_markets": n_markets,
            "n_closed": len(closed), "n_active_left": len(pf.active_trades),
            "n_triggered": n_triggered, "total_pnl": round(total_pnl, 4),
            "cash": round(pf.cash, 4), "committed": round(committed, 4),
            "equity": round(pf.cash + committed, 4), "start_capital": driver.CAPITAL,
            "pnl_pct": round(100 * total_pnl / driver.CAPITAL, 2),
            "runtime_s": round(time.time() - t0, 1),
            "trades": [t.to_dict() for t in closed]}


def run_strategy_oos(name: str, files: list[str], fill: str = "maker") -> str:
    reg = driver.STRATEGIES[name]
    fam = run_is.classify(reg)
    summ_path = os.path.join(RESULTS, f"{name}.summary.json")
    if os.path.exists(summ_path):
        return f"SKIP(exists) {name}"
    if fam == "indicator":
        res = run_indicator_oos(name, reg, files, fill)
    elif fam == "daily_orb":
        res = run_daily_orb_oos(name, reg, files, fill)
    else:
        res = run_per_snapshot_oos(name, reg, files, fill)
    res["fill_model"] = fill
    trades = res.pop("trades", [])
    with gzip.open(os.path.join(RESULTS, f"{name}.trades.jsonl.gz"), "wt", encoding="utf-8") as fh:
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
            subprocess.run(["rclone", "copyto", src, f"{GDRIVE_OOS}/{name}.{ext}"],
                           capture_output=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", default="")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--fill", choices=["maker", "taker", "instant"], default="maker",
                    help="maker = resting bid (pessimistic bound); taker = live check_entry walk-the-ask (fidelity model); instant = fill at signal price (optimistic bound)")
    args = ap.parse_args()
    want = f"oos_{args.fill}"
    if os.path.basename(RESULTS) != want:
        print(f"WARN: BT_OOS_DIR dir {RESULTS} != --fill {args.fill} ({want})", flush=True)
    os.makedirs(RESULTS, exist_ok=True)
    with open(args.files) as fh:
        files = [l.strip() for l in fh if l.strip()]
    extra_path = os.environ.get("BT_EXTRA_STRATEGIES")
    if os.environ.get("BT_ONLY_EXTRA_REGISTRY") and extra_path:
        with open(extra_path, "r", encoding="utf-8") as _fh:
            names = list(json.load(_fh).keys())
    else:
        names = list(driver.STRATEGIES.keys())
    if args.only:
        keep = set(args.only.split(","))
        names = [n for n in names if n in keep]
    _prio = {"per_snapshot": 0, "indicator": 1, "daily_orb": 2}
    names.sort(key=lambda n: (_prio.get(run_is.classify(driver.STRATEGIES[n]), 9), n))
    print(f"OOS scoring {len(names)} strategies over Jul 1-10 held-out window (fill={args.fill})", flush=True)
    bt_reference.load()
    from concurrent.futures import ProcessPoolExecutor, as_completed
    t0 = time.time()
    done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_strategy_oos, n, files, args.fill): n for n in names}
        for fut in as_completed(futs):
            n = futs[fut]
            try:
                msg = fut.result()
            except Exception as e:
                msg = f"FAIL {n}: {type(e).__name__}: {e}"
            done += 1
            print(f"[{done}/{len(names)}] {msg} ({time.time()-t0:.0f}s)", flush=True)
            if args.upload and msg.startswith("DONE"):
                upload(n)
    print(f"OOS ALL DONE in {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
