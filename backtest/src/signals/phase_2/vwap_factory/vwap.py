# CHANGE_SUMMARY
# 2026-07-15  kilo
#   - Created vwap.py: no-lookahead VWAP math utilities for the VWAP strategy factory.
#   - Supports rolling tick-VWAP, market-open anchored VWAP, std-dev bands,
#     VWAP slope, Polymarket midpoint VWAP, book VWAP, and volume profile POC.
# WHY: 70 VWAP strategies need shared, tested math; centralizing it avoids
#      copy-paste errors and guarantees no look-ahead.
"""No-lookahead VWAP and volume-profile helpers."""
from __future__ import annotations

import math
from collections import deque
from typing import Sequence


def _mean(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    return math.fsum(xs) / len(xs)


def _stdev(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (n - 1))


def rolling_vwap(prices: Sequence[float], volumes: Sequence[float] | None = None, window: int = 60) -> float:
    """Tick VWAP over the last `window` prices. Volumes default to 1.0 each."""
    if not prices:
        return 0.0
    vals = list(prices[-window:])
    vols = [1.0] * len(vals) if volumes is None else list(volumes[-window:])
    if len(vols) != len(vals):
        vols = [1.0] * len(vals)
    pv = math.fsum(p * v for p, v in zip(vals, vols))
    vsum = math.fsum(vols)
    return pv / vsum if vsum > 0 else 0.0


def anchored_vwap(prices: Sequence[float], volumes: Sequence[float] | None = None,
                  anchor_count: int | None = None) -> float:
    """VWAP anchored from the first price of the sequence (market open).
    If anchor_count is given, only use the first anchor_count ticks."""
    if not prices:
        return 0.0
    vals = list(prices[:anchor_count]) if anchor_count else list(prices)
    vols = [1.0] * len(vals) if volumes is None else list(volumes[: len(vals)])
    pv = math.fsum(p * v for p, v in zip(vals, vols))
    vsum = math.fsum(vols)
    return pv / vsum if vsum > 0 else 0.0


def vwap_std_band(vwap: float, prices: Sequence[float], n: float = 1.0) -> tuple[float, float]:
    """Return (lower, upper) band = vwap ± n * stdev(prices)."""
    if not prices:
        return vwap, vwap
    std = _stdev(prices)
    return vwap - n * std, vwap + n * std


def vwap_slope(vwap_history: Sequence[float], window: int = 5) -> float:
    """Slope of VWAP over last `window` points (rise per tick)."""
    if len(vwap_history) < 2:
        return 0.0
    recent = list(vwap_history[-window:])
    if len(recent) < 2:
        return 0.0
    # linear regression slope: sum((x-mx)*(y-my)) / sum((x-mx)^2)
    n = len(recent)
    mx = (n - 1) / 2.0
    my = _mean(recent)
    num = math.fsum((i - mx) * (y - my) for i, y in enumerate(recent))
    den = math.fsum((i - mx) ** 2 for i in range(n))
    return num / den if den != 0 else 0.0


def pm_mid_vwap(yp_history: Sequence[float], np_history: Sequence[float], window: int = 60) -> float:
    """VWAP of Polymarket midpoint (yp + np_val)/2 over last window ticks."""
    if not yp_history or not np_history:
        return 0.0
    mids = [(y + n) / 2.0 for y, n in zip(yp_history[-window:], np_history[-window:])]
    return _mean(mids) if mids else 0.0


def book_vwap(book: dict, side: str, depth: int = 5) -> float:
    """Volume-weighted average price of the top `depth` levels of a normalized book.
    book = {'asks': [[price, size], ...], 'bids': [[price, size], ...]}.
    side is 'ask' or 'bid'."""
    levels = book.get(side + "s") if side in ("ask", "bid") else book.get(side)
    if not levels:
        return 0.0
    pv = 0.0
    vsum = 0.0
    for lvl in levels[:depth]:
        if len(lvl) < 2:
            continue
        p, s = float(lvl[0]), float(lvl[1])
        if s <= 0 or p <= 0:
            continue
        pv += p * s
        vsum += s
    return pv / vsum if vsum > 0 else 0.0


def book_imbalance(book_up: dict, book_down: dict) -> float:
    """Imbalance between YES bid size and NO ask size, normalized to [-1, 1].
    Positive = more YES bid depth (bullish); negative = more NO ask depth (bearish)."""
    yes_bids = book_up.get("bids", [])[:5]
    no_asks = book_down.get("asks", [])[:5]
    yes_size = math.fsum(float(s) for _, s in yes_bids if s > 0)
    no_size = math.fsum(float(s) for _, s in no_asks if s > 0)
    total = yes_size + no_size
    if total <= 0:
        return 0.0
    return (yes_size - no_size) / total


def volume_profile_poc(prices: Sequence[float], volumes: Sequence[float] | None = None,
                       bins: int = 10) -> tuple[float, float, float]:
    """Return (POC, value_area_low, value_area_high) from tick volume profile.
    POC = price level with highest volume. Value area = ~70% of volume around POC."""
    if not prices:
        return 0.0, 0.0, 0.0
    vals = list(prices)
    vols = [1.0] * len(vals) if volumes is None else list(volumes[: len(vals)])
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return lo, lo, hi
    bucket_size = (hi - lo) / bins
    profile: dict[int, float] = {}
    for p, v in zip(vals, vols):
        b = int((p - lo) / bucket_size) if bucket_size > 0 else 0
        b = min(b, bins - 1)
        profile[b] = profile.get(b, 0.0) + v
    if not profile:
        return lo, lo, hi
    poc_bucket = max(profile, key=profile.get)
    poc = lo + (poc_bucket + 0.5) * bucket_size
    # Value area: sort buckets by volume descending, accumulate until 70% of total
    total_vol = math.fsum(profile.values())
    sorted_buckets = sorted(profile.items(), key=lambda x: -x[1])
    cum = 0.0
    va_buckets: set[int] = set()
    for b, v in sorted_buckets:
        cum += v
        va_buckets.add(b)
        if cum >= 0.70 * total_vol:
            break
    if va_buckets:
        va_low = lo + min(va_buckets) * bucket_size
        va_high = lo + (max(va_buckets) + 1) * bucket_size
    else:
        va_low, va_high = lo, hi
    return poc, va_low, va_high


def regime_slope(vwap_history: Sequence[float], window: int = 10, threshold: float = 0.0) -> str:
    """Classify VWAP slope regime as 'up', 'down', or 'flat'."""
    s = vwap_slope(vwap_history, window)
    if s > threshold:
        return "up"
    if s < -threshold:
        return "down"
    return "flat"


__all__ = [
    "rolling_vwap",
    "anchored_vwap",
    "vwap_std_band",
    "vwap_slope",
    "pm_mid_vwap",
    "book_vwap",
    "book_imbalance",
    "volume_profile_poc",
    "regime_slope",
]
