#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-14  kimi
#   - Parametric twin of compactify.py for ETH/SOL (or any coin).
#     Reads raw polybacktest {market}.json.gz, writes compact {market}.pkl.gz
#     with parallel arrays. The coin's spot price field (eth_price / sol_price)
#     is stored under the key "btc" so the existing driver/bt_orb array paths
#     keep working unchanged.
# WHY: Reuse the BTC backtest harness for other coins with minimal code change.
"""Convert raw coin dataset -> /tmp/<coin>5m_compact/<market_id>.pkl.gz (parallel)."""
from __future__ import annotations

import argparse
import glob
import gzip
import os
import pickle
import sys
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
for p in (HERE, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

import driver  # noqa: E402


def _ts_ms(s: str) -> int:
    return int(driver._parse_ts(s).timestamp() * 1000)


PRICE_FIELD: str = "btc_price"


def convert(path: str) -> str:
    try:
        snaps = driver.load_market_file(path)
        if not snaps:
            return f"empty {path}"
        t = []; btc = []; pu = []; pd = []; ua = []; ub = []; da = []; db = []
        for s in snaps:
            t.append(_ts_ms(s["time"]))
            # Use the coin's spot field but keep the array key "btc" for
            # compatibility with the existing driver array paths.
            btc.append(float(s.get(PRICE_FIELD) or 0.0))
            pu.append(float(s.get("price_up") or 0.0))
            pd.append(float(s.get("price_down") or 0.0))
            a, b = driver.top_book(s.get("orderbook_up")); ua.append(a); ub.append(b)
            a2, b2 = driver.top_book(s.get("orderbook_down")); da.append(a2); db.append(b2)
        mid = str(snaps[0].get("market_id") or os.path.basename(path).split(".")[0])
        out = {"t": t, "btc": btc, "pu": pu, "pd": pd,
               "ua": ua, "ub": ub, "da": da, "db": db, "id": mid}
        dst = os.path.join(DST, os.path.basename(path).replace(".json.gz", ".pkl.gz"))
        with gzip.open(dst, "wb", compresslevel=3) as fh:
            pickle.dump(out, fh, protocol=5)
        return f"ok {mid}"
    except Exception as e:
        return f"FAIL {path}: {type(e).__name__}: {e}"


def main() -> None:
    global DST
    ap = argparse.ArgumentParser()
    ap.add_argument("coin", help="coin label (eth, sol, ...)")
    ap.add_argument("srcdir", help="source dir with *.json.gz")
    ap.add_argument("dstdir", help="destination dir for *.pkl.gz")
    ap.add_argument("price_field", help="raw snapshot field holding the spot price")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    global PRICE_FIELD
    PRICE_FIELD = args.price_field
    DST = args.dstdir
    os.makedirs(DST, exist_ok=True)
    files = sorted(glob.glob(os.path.join(args.srcdir, "*.json.gz")))
    done = {os.path.basename(f).replace(".pkl.gz", "") for f in glob.glob(os.path.join(DST, "*.pkl.gz"))}
    todo = [f for f in files if os.path.basename(f).replace(".json.gz", "") not in done]
    print(f"[{args.coin}] total {len(files)}  already {len(done)}  todo {len(todo)}", flush=True)
    n_ok = n_fail = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for i, r in enumerate(ex.map(convert, todo, chunksize=32), 1):
            if r.startswith("ok"):
                n_ok += 1
            else:
                n_fail += 1
                if n_fail <= 5:
                    print(r, flush=True)
            if i % 2000 == 0:
                print(f"[{args.coin}]   {i}/{len(todo)} ok={n_ok} fail={n_fail}", flush=True)
    print(f"[{args.coin}] DONE ok={n_ok} fail={n_fail}", flush=True)


if __name__ == "__main__":
    main()
