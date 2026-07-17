# CHANGE_SUMMARY
# 2026-07-17  kilo_regime_filter
#   - Created trend_family_regime wrapper around phase_2_new.trend_family_sweep.
#   - Adds three regime gates before allowing the base trend-deviation signal:
#       1. IND_vwapside_8h: skip when trailing trade-pct of 8h VWAP side is
#          against the trade direction and >= threshold.
#       2. Volatility regime: skip when ATR% from spot_history is below
#          min_atr_pct (dead trend) or above max_atr_pct (chop).
#       3. Time-of-day: skip during configured low-volume UTC hours.
#   - Loads the 8h VWAP side series from filters_v2/gate_today/btc_8h_deep.json
#     once per worker process; builds a trailing percentile over recent history.
#   - Hardened _trailing_pct to O(1) per tick using deque + running +/- counters,
#     and fixed the percentile direction so it measures the strength of the
#     current VWAP side (matching %) rather than the asymmetric "less than"
#     rank that always returned 0 for the bearish (-1) side.
# WHY: Test whether regime filters on the top trend-family legs can cut
#      drawdown while preserving PnL; the original list-comprehension
#      implementation was too slow for a full 18k-market IS run and the
#      percentile was directionally broken for one side.

from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Lazy load of the base trend_family_sweep signal (no __init__.py in the
# phase_2_new package, so we use importlib.util exactly like the driver).
# ---------------------------------------------------------------------------
_BASE_FN: Any = None


def _base_signal():
    global _BASE_FN
    if _BASE_FN is not None:
        return _BASE_FN
    here = os.path.dirname(os.path.abspath(__file__))
    base_path = os.path.join(here, "..", "trend_family_sweep", "signal.py")
    base_path = os.path.normpath(base_path)
    if not os.path.isfile(base_path):
        raise RuntimeError(f"trend_family_regime cannot find base signal at {base_path}")
    spec = importlib.util.spec_from_file_location(
        "_trend_family_sweep_base", base_path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _BASE_FN = mod.trend_family_signal
    return _BASE_FN


# ---------------------------------------------------------------------------
# 8h VWAP side state (one series per process)
# ---------------------------------------------------------------------------
_VWAP: Dict[str, Any] = {"loaded": False, "publish": None, "side": None}
_TRAIL: Deque[Tuple[float, float]] = deque()
_TRAIL_COUNTS: Dict[str, int] = {"pos": 0, "neg": 0}


def _load_vwapside() -> None:
    if _VWAP["loaded"]:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "..", "..", "..", "filters_v2", "gate_today", "btc_8h_deep.json")
    path = os.path.normpath(path)
    if not os.path.isfile(path):
        raise RuntimeError(f"trend_family_regime cannot find 8h klines at {path}")

    raw = json.load(open(path, "r", encoding="utf-8"))
    # Binance kline format: [open_ms, open, high, low, close, volume, close_ms, ...]
    opens_ms = np.array([r[0] for r in raw], dtype=float)
    high = np.array([float(r[2]) for r in raw], dtype=float)
    low = np.array([float(r[3]) for r in raw], dtype=float)
    close = np.array([float(r[4]) for r in raw], dtype=float)
    volume = np.array([float(r[5]) for r in raw], dtype=float)

    tp = (high + low + close) / 3.0
    dt = np.array([datetime.fromtimestamp(t / 1000.0, tz=timezone.utc) for t in opens_ms])
    day = np.array([d.date().isoformat() for d in dt])
    # daily-anchored cumulative VWAP (faithful to features_indicators.py)
    cum_pv = np.zeros_like(volume)
    cum_v = np.zeros_like(volume)
    running_pv = 0.0
    running_v = 0.0
    last_day = ""
    for i in range(len(day)):
        if day[i] != last_day:
            running_pv = 0.0
            running_v = 0.0
            last_day = day[i]
        running_pv += tp[i] * volume[i]
        running_v += volume[i]
        cum_pv[i] = running_pv
        cum_v[i] = running_v
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap = cum_pv / cum_v
    side = np.where(close >= vwap, 1.0, -1.0).astype(float)
    side[cum_v == 0] = np.nan
    # published at the 8h bar close (open + 8h)
    publish_s = opens_ms / 1000.0 + 8.0 * 3600.0
    # drop NaN warm-up rows
    valid = ~np.isnan(side)
    _VWAP["publish"] = publish_s[valid]
    _VWAP["side"] = side[valid]
    _VWAP["loaded"] = True


def _vwapside_at(ts_s: float) -> float:
    _load_vwapside()
    arr = _VWAP["publish"]
    idx = int(np.searchsorted(arr, ts_s, side="right")) - 1
    if idx < 0:
        return math.nan
    return float(_VWAP["side"][idx])


def _trailing_pct(current_side: float, ts_s: float,
                  window_s: float, min_count: int) -> float:
    """Trailing percentage of recent VWAP sides matching the current side.

    Maintains a chronological trail and O(1) counters for +1/-1 observations.
    Returns the percent of *valid* (non-NaN) sides that equal ``current_side``.
    This is the regime-strength metric used to block trades that fight a
    persistent VWAP-side regime.
    """
    global _TRAIL
    counts = _TRAIL_COUNTS
    cutoff = ts_s - window_s
    # Trim old entries and update counters; NaN entries are kept for
    # chronological correctness but never counted.
    while _TRAIL and _TRAIL[0][0] < cutoff:
        _, old_side = _TRAIL.popleft()
        if old_side > 0:
            counts["pos"] -= 1
        elif old_side < 0:
            counts["neg"] -= 1
    _TRAIL.append((ts_s, current_side))
    if current_side > 0:
        counts["pos"] += 1
    elif current_side < 0:
        counts["neg"] += 1
    if math.isnan(current_side):
        return math.nan
    total = counts["pos"] + counts["neg"]
    if total < min_count:
        return math.nan
    matching = counts["pos"] if current_side > 0 else counts["neg"]
    return 100.0 * matching / total


# ---------------------------------------------------------------------------
# Volatility and time helpers
# ---------------------------------------------------------------------------
def _atr_pct(spot_history: List[float], spot_price: float, lookback: int) -> float:
    if len(spot_history) < 2 or spot_price <= 0:
        return 0.0
    arr = spot_history[-lookback:]
    if len(arr) < 2:
        return 0.0
    diffs = [abs(arr[i] - arr[i - 1]) for i in range(1, len(arr))]
    atr = sum(diffs) / len(diffs)
    return 100.0 * atr / spot_price


def _utc_hour(start_date_iso: str, elapsed_sec: float) -> int:
    try:
        start = datetime.fromisoformat(start_date_iso)
    except Exception:
        return -1
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    cur = start + timedelta(seconds=elapsed_sec)
    return cur.astimezone(timezone.utc).hour


# ---------------------------------------------------------------------------
# Regime wrapper entry point
# ---------------------------------------------------------------------------
def trend_family_regime_signal(**kwargs: Any) -> Dict[str, Any]:
    cfg = kwargs.get("config") or {}

    # Read regime parameters from the registry entry (config).
    use_vwapside = bool(cfg.get("use_vwapside", True))
    vwap_window_s = float(cfg.get("vwap_window_s", 7.0 * 86400.0))
    vwap_min_count = int(cfg.get("vwap_min_count", 20))
    vwap_pct_thr = float(cfg.get("vwap_pct_thr", 50.0))

    use_vol = bool(cfg.get("use_vol", True))
    atr_lookback = int(cfg.get("atr_lookback", 50))
    min_atr_pct = float(cfg.get("min_atr_pct", 0.0))
    max_atr_pct = float(cfg.get("max_atr_pct", 999.0))

    use_tod = bool(cfg.get("use_tod", True))
    blocked_hours = list(cfg.get("blocked_hours", [0, 1, 2, 3, 4]))

    # Common kwargs we need for regime math.
    spot_history = list(kwargs.get("spot_history", []))
    spot_price = float(kwargs.get("spot_price", 0.0))
    start_date_iso = kwargs.get("start_date_iso", "")
    elapsed_sec = float(kwargs.get("elapsed_sec", 0.0))

    neutral = {
        "triggered": False,
        "direction": None,
        "confidence": 0.0,
        "signal_price": spot_price,
        "entry_price": 0.0,
        "source": "TREND_FAMILY_REGIME",
        "reason": "no signal",
    }

    # ---- 1. time-of-day gate (cheapest) ----
    if use_tod and start_date_iso:
        hour = _utc_hour(start_date_iso, elapsed_sec)
        if hour in blocked_hours:
            neutral["reason"] = f"blocked UTC hour {hour}"
            return neutral

    # ---- 2. volatility gate ----
    if use_vol and spot_price > 0:
        atr_pct = _atr_pct(spot_history, spot_price, atr_lookback)
        if atr_pct < min_atr_pct:
            neutral["reason"] = f"vol too low atr_pct={atr_pct:.4f}"
            return neutral
        if atr_pct > max_atr_pct:
            neutral["reason"] = f"vol too high atr_pct={atr_pct:.4f}"
            return neutral

    # ---- 3. IND_vwapside_8h gate ----
    vwap_side = math.nan
    vwap_pct = math.nan
    if use_vwapside and start_date_iso:
        try:
            start = datetime.fromisoformat(start_date_iso)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            ts_s = start.timestamp() + elapsed_sec
        except Exception:
            ts_s = 0.0
        if ts_s > 0:
            vwap_side = _vwapside_at(ts_s)
            vwap_pct = _trailing_pct(vwap_side, ts_s, vwap_window_s, vwap_min_count)

    # Run the base trend-family signal.
    try:
        sig = _base_signal()(**kwargs)
    except Exception as e:
        neutral["reason"] = f"base signal error: {e}"
        return neutral

    if not (sig and sig.get("triggered")):
        return sig if sig else neutral

    direction = sig.get("direction")

    # VWAP side: +1 = price above VWAP (bullish), -1 = below (bearish).
    # For a YES trade we want +1; for a NO trade we want -1.
    if use_vwapside and not math.isnan(vwap_side) and not math.isnan(vwap_pct):
        aligned = (direction == "YES" and vwap_side > 0) or (direction == "NO" and vwap_side < 0)
        if not aligned and vwap_pct >= vwap_pct_thr:
            neutral["reason"] = (
                f"vwapside_8h against {direction} "
                f"side={int(vwap_side)} pct={vwap_pct:.1f}"
            )
            return neutral

    # Attach regime diagnostics to the returned signal for debugging.
    sig["source"] = "TREND_FAMILY_REGIME"
    sig["regime"] = {
        "vwap_side": None if math.isnan(vwap_side) else int(vwap_side),
        "vwap_pct": None if math.isnan(vwap_pct) else round(vwap_pct, 2),
        "atr_pct": round(_atr_pct(spot_history, spot_price, atr_lookback), 4) if use_vol else None,
        "utc_hour": _utc_hour(start_date_iso, elapsed_sec) if start_date_iso else None,
    }
    return sig
