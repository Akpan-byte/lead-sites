#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - One-time converter: raw polybacktest {market}.json.gz -> compact pickle.gz
#     of parallel arrays (t_ms, btc, pu, pd, up_ask, up_bid, dn_ask, dn_bid).
#     The backtest replays need only the top book level per side (feed books are
#     stored sorted: asks asc / bids desc, verified), so the 15-level book dicts
#     collapse to 2 floats/side/snapshot. ~10x less IO and zero per-snapshot dict
#     building on the replay path (the run was memory/IO-bandwidth-bound, not
#     core-bound: 113 strats re-parsing 3GB of JSON each).
# WHY: cut the full-suite wall time from ~33h to ~10h on 4 cores.
"""Convert raw dataset -> /tmp/btc5m_compact/<market_id>.pkl.gz (parallel)."""
from __future__ import annotations

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

DST = "/tmp/btc5m_compact"


def _ts_ms(s: str) -> int:
    return int(driver._parse_ts(s).timestamp() * 1000)


def convert(path: str) -> str:
    try:
        snaps = driver.load_market_file(path)
        if not snaps:
            return f"empty {path}"
        t = []; btc = []; pu = []; pd = []; ua = []; ub = []; da = []; db = []
        for s in snaps:
            t.append(_ts_ms(s["time"]))
            btc.append(float(s.get("btc_price") or 0.0))
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
    os.makedirs(DST, exist_ok=True)
    files = sorted(glob.glob("/tmp/btc5m_all/*.json.gz"))
    # skip already-converted (resumable)
    done = {os.path.basename(f).replace(".pkl.gz", "") for f in glob.glob(os.path.join(DST, "*.pkl.gz"))}
    todo = [f for f in files if os.path.basename(f).replace(".json.gz", "") not in done]
    print(f"total {len(files)}  already {len(done)}  todo {len(todo)}", flush=True)
    n_ok = n_fail = 0
    with ProcessPoolExecutor(max_workers=3) as ex:
        for i, r in enumerate(ex.map(convert, todo, chunksize=32), 1):
            if r.startswith("ok"):
                n_ok += 1
            else:
                n_fail += 1
                if n_fail <= 5:
                    print(r, flush=True)
            if i % 2000 == 0:
                print(f"  {i}/{len(todo)} ok={n_ok} fail={n_fail}", flush=True)
    print(f"DONE ok={n_ok} fail={n_fail}", flush=True)


if __name__ == "__main__":
    main()
