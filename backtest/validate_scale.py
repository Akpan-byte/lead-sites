#!/usr/bin/env python3
"""At-scale validation: parallel over markets, per strategy. Writes results to
results/validation.txt (flushed per line) so progress is visible mid-run."""
import os, sys, glob, time
from multiprocessing import Pool, cpu_count
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
import driver

DATA = os.path.join(os.path.dirname(__file__), "data", "btc5m_val")
OUT = os.path.join(os.path.dirname(__file__), "results", "validation.txt")
STRATS = ["breakout_pct_003", "mean_reversion", "mean_reversion_opposite_exit",
          "kinetic_velocity_breakout", "ofi_momentum_bo_15m"]

_SNAPS = None
def init_worker(snaps):
    global _SNAPS
    _SNAPS = snaps

def run_one(args):
    name, f = args
    reg = driver.STRATEGIES[name]; fn = driver.load_signal_fn(reg)
    r = driver.run_market(_SNAPS[f], reg, fn)
    return (name, r["n_closed"], r["n_triggered"], r["total_pnl"], r["n_active_left"])

def main():
    files = sorted(glob.glob(os.path.join(DATA, "*.json.gz")))
    snaps = {f: driver.load_market_file(f) for f in files}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as out:
        out.write(f"markets={len(files)} strats={len(STRATS)} workers={min(cpu_count(),16)}\n"); out.flush()
        for name in STRATS:
            t0 = time.time()
            tasks = [(name, f) for f in files]
            with Pool(min(cpu_count(), 16), initializer=init_worker, initargs=(snaps,)) as p:
                rows = list(p.imap_unordered(run_one, tasks))
            cl = sum(r[1] for r in rows); tr = sum(r[2] for r in rows)
            pnl = sum(r[3] for r in rows); act = sum(r[4] for r in rows)
            rate = 100.0 * cl / max(1, tr)
            out.write(f"{name:30s} closed={cl:5d} trig={tr:7d} fill_rate={rate:.3f}% pnl={pnl:10.2f} active_left={act} ({time.time()-t0:.1f}s)\n")
            out.flush()
        out.write("DONE\n"); out.flush()

if __name__ == "__main__":
    main()
