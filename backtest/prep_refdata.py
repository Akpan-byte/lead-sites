#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - One-time preprocessing: convert data.binance.vision aggTrades daily zips
#     (gdrive:trading_backtest/reference/binance/btc/spot/aggTrades/) into compact
#     per-day npz arrays (ts_ms:int64, px:float64) on
#     gdrive:trading_backtest/derived/aggtrades_npz/.
# WHY: The backtest harness needs a no-lookahead Binance reference price at any
#      timestamp. Parsing 150MB CSV zips per worker per day is wasteful; the npz
#      keeps full trade precision at ~8MB/day so workers load days lazily.
"""Convert Binance aggTrades daily zips -> per-day npz (ts_ms, px) on gdrive."""

import datetime as dt
import io
import os
import subprocess
import sys
import zipfile

import numpy as np

SRC = "gdrive:trading_backtest/reference/binance/btc/spot/aggTrades"
DST = "gdrive:trading_backtest/derived/aggtrades_npz"
TMP = "/config/backtest/_refdata_tmp"
D0 = dt.date(2026, 5, 8)
D1 = dt.date(2026, 7, 10)


def sh(args):
    return subprocess.run(args, capture_output=True, text=True)


def remote_exists(path):
    return sh(["rclone", "lsf", path]).returncode == 0 and sh(["rclone", "lsf", path]).stdout.strip() != ""


def convert_day(dstr):
    out_name = f"BTCUSDT-aggTrades-{dstr}.npz"
    if remote_exists(f"{DST}/{out_name}"):
        print(f"SKIP {dstr} (exists)", flush=True)
        return
    zip_path = os.path.join(TMP, f"{dstr}.zip")
    r = sh(["rclone", "copyto", f"{SRC}/BTCUSDT-aggTrades-{dstr}.zip", zip_path])
    if r.returncode != 0:
        print(f"FAIL fetch {dstr}: {r.stderr[-200:]}", flush=True)
        return
    with zipfile.ZipFile(zip_path) as zf:
        member = zf.namelist()[0]
        with zf.open(member) as fh:
            raw = fh.read()
    os.remove(zip_path)
    # CSV columns (data.binance.vision aggTrades): agg_trade_id,price,quantity,
    # first_trade_id,last_trade_id,timestamp,is_buyer_maker,is_best_match
    # Some files have a header row; detect and skip it.
    text = raw.decode("utf-8")
    lines = text.splitlines()
    if lines and not lines[0][0].isdigit():
        lines = lines[1:]
    n = len(lines)
    ts = np.empty(n, dtype=np.int64)
    px = np.empty(n, dtype=np.float64)
    for i, line in enumerate(lines):
        # price is field 1, timestamp field 5 — split once, cheaply
        parts = line.split(",")
        px[i] = float(parts[1])
        ts[i] = int(parts[5])
    # Binance moved aggTrades dumps to MICROseconds in 2025; store milliseconds.
    if len(ts) and ts[len(ts) // 2] > 10**14:
        ts = ts // 1000
    order = np.argsort(ts, kind="stable")
    ts = ts[order]
    px = px[order]
    out_path = os.path.join(TMP, out_name)
    np.savez_compressed(out_path, ts_ms=ts, px=px)
    r = sh(["rclone", "copyto", out_path, f"{DST}/{out_name}"])
    os.remove(out_path)
    if r.returncode != 0:
        print(f"FAIL upload {dstr}: {r.stderr[-200:]}", flush=True)
        return
    print(f"OK {dstr} rows={n} {ts[0]}..{ts[-1]}", flush=True)


def main():
    os.makedirs(TMP, exist_ok=True)
    d = D0
    while d <= D1:
        convert_day(d.isoformat())
        d += dt.timedelta(days=1)
    print("DONE prep_refdata", flush=True)


if __name__ == "__main__":
    main()
