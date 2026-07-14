# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - Created harness/refdata.py: BinanceReference — no-lookahead BTC reference
#     price from per-day aggTrades npz files (ts_ms, px) produced by
#     /config/backtest/prep_refdata.py on gdrive:trading_backtest/derived/aggtrades_npz.
# WHY: The user mandated Binance as the backtest reference price (entries/exits/
#      ORB ranges). price_at(t) returns the last trade with timestamp <= t
#      (bisect right - 1), which is strictly causal. Days are loaded lazily and
#      only the current + previous day are kept in RAM.
"""No-lookahead Binance BTC reference price series."""

from __future__ import annotations

import bisect
import datetime as dt
import os
import subprocess

import numpy as np

NPZ_REMOTE = "gdrive:trading_backtest/derived/aggtrades_npz"
KLINES_REMOTE = "gdrive:trading_backtest/reference/binance/btc/spot/klines_1m"
_TMP = "/config/backtest/_refdata_cache"


class BinanceReference:
    """price_at(t) = last Binance aggTrade with timestamp <= t. Strictly causal."""

    def __init__(self, local_dir: str | None = None):
        self._local_dir = local_dir  # if set, read npz from here instead of gdrive
        self._days: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._order: list[str] = []
        os.makedirs(_TMP, exist_ok=True)

    @staticmethod
    def _day_key(ts: float) -> str:
        return dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")

    def _npz_path(self, day: str) -> str | None:
        name = f"BTCUSDT-aggTrades-{day}.npz"
        if self._local_dir:
            p = os.path.join(self._local_dir, name)
            return p if os.path.exists(p) else None
        p = os.path.join(_TMP, name)
        if not os.path.exists(p):
            r = subprocess.run(
                ["rclone", "copyto", f"{NPZ_REMOTE}/BTCUSDT-aggTrades-{day}.npz", p],
                capture_output=True,
            )
            if r.returncode != 0:
                return None
        return p

    def _ensure(self, day: str) -> None:
        if day in self._days:
            return
        path = self._npz_path(day)
        if path is None:
            self._days[day] = (np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64))
        else:
            with np.load(path) as z:
                ts, px = z["ts_ms"], z["px"]
            # Binance moved aggTrades dumps to MICROseconds in 2025; normalize
            # to milliseconds so bisect against ts*1000 works.
            if len(ts) and ts[len(ts) // 2] > 10**14:
                ts = ts // 1000
            self._days[day] = (ts, px)
        self._order.append(day)
        # Keep at most 3 days in RAM (current, previous, and one spare).
        while len(self._order) > 3:
            old = self._order.pop(0)
            self._days.pop(old, None)

    def price_at(self, ts: float) -> float | None:
        """Last Binance trade price with trade_ts <= ts (unix seconds)."""
        day = self._day_key(ts)
        self._ensure(day)
        ts_ms = int(ts * 1000)
        arr_ts, arr_px = self._days[day]
        i = bisect.bisect_right(arr_ts, ts_ms) - 1
        if i >= 0:
            return float(arr_px[i])
        # Before this day's first trade — use the previous day's last trade.
        prev_day = self._day_key(ts - 86400)
        self._ensure(prev_day)
        pts, ppx = self._days[prev_day]
        if len(ppx):
            return float(ppx[-1])
        return None


__all__ = ["BinanceReference"]
