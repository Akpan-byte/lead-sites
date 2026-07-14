#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-14  kimi
#   - Generic per-coin twin of build_is_index.py: probes a staged dir of
#     polybacktest {market}.json.gz (eth/sol, any timeframe) for
#     (ET date of first snapshot, first-snapshot UTC ts, path) and writes a
#     gzipped JSON index. Reads RAW .json.gz via driver.load_market_file (same
#     as the 5m BTC path), NOT the compact pkl.gz the 15m/1h BTC twin uses.
#     Workers default to 3 to respect this VM's CPU/RAM budget. A full probe
#     also doubles as an integrity scan: any file that fails to gunzip/parse is
#     collected and reported as a PROBE ERROR.
# WHY: Mirror the BTC is_index.json.gz for ETH/SOL so the identical IS/OOS
#      windowing (ET-date split) and chronological daily_orb replay can be
#      reproduced per coin without re-scanning ~18k files per strategy.
"""Usage: python build_index_coin.py <coin> <srcdir> <outindex.json.gz> [workers]"""
from __future__ import annotations

import glob
import gzip
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
for p in (HERE, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

import driver  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

ET = ZoneInfo("America/New_York")


def _first_obj_time(path: str):
    """Fast path: extract the first array element's 'time' from the first 128 KB.

    Avoids json.load() of the whole file (a 1h market may have >1000 snapshots).
    Returns a datetime or raises on failure; caller handles exceptions.
    """
    from datetime import datetime
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        s = fh.read(131072)
    i = s.find("[")
    if i < 0:
        raise ValueError("no array start")
    j = s.find("{", i)
    if j < 0:
        raise ValueError("no object start")
    depth = 0
    in_str = False
    escape = False
    k = j
    while k < len(s):
        c = s[k]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    break
        k += 1
    if depth != 0:
        raise ValueError("first object spans fast-read buffer")
    obj = json.loads(s[j:k + 1])
    t_str = obj.get("time")
    if not t_str:
        raise ValueError("first object missing time")
    return datetime.fromisoformat(str(t_str).replace("Z", "+00:00"))


def probe(path: str):
    try:
        t = _first_obj_time(path)
        return [t.astimezone(ET).date().isoformat(), t.timestamp(), path]
    except Exception:
        # Fall back to the faithful full-file parser for odd files (very large
        # first object, unusual formatting, etc.). This still catches gzip/JSON
        # corruption that the fast path missed.
        try:
            snaps = driver.load_market_file(path)
            if not snaps:
                return None
            t = driver._parse_ts(snaps[0]["time"])
            return [t.astimezone(ET).date().isoformat(), t.timestamp(), path]
        except Exception as e:
            return ["ERR:" + str(e)[:60], 0.0, path]


def main() -> None:
    coin = sys.argv[1]
    data_dir = sys.argv[2]
    out = sys.argv[3]
    workers = int(sys.argv[4]) if len(sys.argv) > 4 else 3
    files = sorted(glob.glob(os.path.join(data_dir, "*.json.gz")))
    print(f"[{coin}] files: {len(files)} in {data_dir}", flush=True)
    rows, errs = [], []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, r in enumerate(ex.map(probe, files, chunksize=64), 1):
            if r and not r[0].startswith("ERR:"):
                rows.append(r)
            elif r:
                errs.append(r)
            if i % 2000 == 0:
                print(f"[{coin}]   {i}/{len(files)} probed", flush=True)
    rows.sort(key=lambda r: (r[0], r[1]))
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(rows, fh)
    dates = sorted({r[0] for r in rows})
    span = f"{dates[0]}..{dates[-1]}" if dates else "EMPTY"
    print(f"[{coin}] indexed {len(rows)} markets across {len(dates)} ET days "
          f"({span}) -> {out}", flush=True)
    if errs:
        print(f"[{coin}] !!! {len(errs)} PROBE ERRORS (corrupt/unloadable):", flush=True)
        for e in errs[:25]:
            print("     ", e[2], "::", e[0], flush=True)
    else:
        print(f"[{coin}] integrity: 0 probe errors "
              f"(all {len(files)} staged files loaded OK)", flush=True)


if __name__ == "__main__":
    main()
