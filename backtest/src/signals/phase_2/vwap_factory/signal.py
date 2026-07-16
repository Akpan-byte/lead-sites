# CHANGE_SUMMARY
# 2026-07-15  kilo
#   - Created vwap_factory/signal.py: one configurable signal function powering
#     70 distinct VWAP-based strategies.
#   - Maintains per-(market_id, strategy_name) state for histories, VWAPs,
#     entry counters, and cooldowns.
#   - Supports fade, trend, breakout, anchored, PM, orderbook, OFI, ladder,
#     orb, combo, time-slice, and regime-flip modes.
# WHY: Centralized factory lets us generate 70 strategies from registry config
#      alone, keeps them live-compatible, and makes iteration/sweeping easy.
"""Configurable VWAP signal factory."""
from __future__ import annotations

import math
from typing import Any

from .vwap import (
    anchored_vwap,
    book_imbalance,
    book_vwap,
    pm_mid_vwap,
    regime_slope,
    rolling_vwap,
    vwap_slope,
    vwap_std_band,
    volume_profile_poc,
)

MAX_HISTORY = 600
MAX_ENTRY_PRICE = 0.85
MIN_ENTRY_PRICE = 0.05

# Module state dict deliberately NOT named `_STATE`. harness/run.py detects
# `_STATE` on signal modules and swaps/resets it to an empty dict between calls,
# which would destroy our accumulated VWAP histories. Using a different name
# keeps the harness's hands off our state.
_VWAP_FACTORY_STATE: dict[tuple[str, str], dict[str, Any]] = {}


def _key(market_id: str | None, strategy_name: str) -> tuple[str, str]:
    return (market_id or "unknown", strategy_name)


def _make_state() -> dict[str, Any]:
    return {
        "spot_history": [],
        "yp_history": [],
        "np_history": [],
        "vwap_history": [],
        "entry_count": 0,
        "cooldown": 0,
        "last_direction": None,
        "regime": "flat",
        "first_sec": None,
        "last_imbalance": 0.0,
    }


def _get_state(market_id: str | None, strategy_name: str) -> dict[str, Any]:
    return _VWAP_FACTORY_STATE.setdefault(_key(market_id, strategy_name), _make_state())


def _no_signal(reason: str) -> dict[str, Any]:
    return {
        "triggered": False,
        "direction": None,
        "confidence": 0.0,
        "entry_price": 0.0,
        "signal_price": 0.0,
        "source": "VWAP_FACTORY",
        "reason": reason,
    }


def _update_histories(state: dict[str, Any], spot_price: float, yp: float, np_val: float,
                      vwap: float) -> None:
    for k, v in (("spot_history", spot_price), ("yp_history", yp),
                 ("np_history", np_val), ("vwap_history", vwap)):
        state[k].append(v)
        if len(state[k]) > MAX_HISTORY:
            state[k] = state[k][-MAX_HISTORY:]


def _consecutive_moves(prices: list[float], direction: str, min_ticks: int) -> bool:
    if len(prices) < min_ticks + 1:
        return False
    recent = prices[-(min_ticks + 1):]
    for i in range(1, len(recent)):
        if direction == "up" and recent[i] <= recent[i - 1]:
            return False
        if direction == "down" and recent[i] >= recent[i - 1]:
            return False
    return True


def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 1.0 if x > 0 else 0.0


def vwap_factory_signal(
    spot_price: float,
    yp: float,
    np_val: float,
    rem_sec: float,
    elapsed_sec: float | None = None,
    duration_sec: float | None = None,
    z_score: float = 0.0,
    spread: float = 0.0,
    orderbook_up: dict | None = None,
    orderbook_down: dict | None = None,
    yp_history: list[float] | None = None,
    np_history: list[float] | None = None,
    book_imbalance_val: float = 0.0,
    config: dict[str, Any] | None = None,
    market_id: str | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Main VWAP factory signal. `config` is the registry entry dict."""
    config = config or {}
    strategy_name = config.get("name", "vwap_unknown")
    mode = config.get("mode", "btc_vwap_fade")
    lookback = int(config.get("lookback", 60))
    threshold = float(config.get("threshold", 2.0))
    entry_max = float(config.get("entry_max", MAX_ENTRY_PRICE))
    cooldown_ticks = int(config.get("cooldown_ticks", 3))
    confirmation = int(config.get("confirmation", 2))
    band_n = float(config.get("band_n", 1.0))
    anchor = int(config.get("anchor", 0))  # 0 = rolling, else anchored first N ticks
    use_pm = bool(config.get("use_pm", False))
    use_book = bool(config.get("use_book", False))
    flip_threshold = float(config.get("flip_threshold", 0.0))
    time_slice = config.get("time_slice")  # None or (first_lookback, second_lookback)

    if rem_sec <= 5:
        return _no_signal("too close to expiry")

    # Normalize prices
    yp = float(yp) if yp is not None else 0.0
    np_val = float(np_val) if np_val is not None else 0.0
    spot_price = float(spot_price) if spot_price is not None else 0.0
    if spot_price <= 0 or (yp <= 0 and np_val <= 0):
        return _no_signal("bad prices")

    state = _get_state(market_id, strategy_name)
    if state["first_sec"] is None and elapsed_sec is not None:
        state["first_sec"] = elapsed_sec

    # Use provided histories if available, else our own state
    spots = list(state["spot_history"])
    yps = list(state["yp_history"])
    nps = list(state["np_history"])
    spots.append(spot_price)
    yps.append(yp)
    nps.append(np_val)

    # Effective lookback (time_slice support)
    eff_lookback = lookback
    if time_slice and duration_sec and duration_sec > 0:
        if elapsed_sec is not None and elapsed_sec > duration_sec / 2:
            eff_lookback = int(time_slice[1])
        else:
            eff_lookback = int(time_slice[0])

    # Compute relevant VWAP
    if use_pm:
        vwap = pm_mid_vwap(yps, nps, eff_lookback)
    elif use_book and orderbook_up and orderbook_down:
        # VWAP of YES bid + NO ask midpoint of book VWAPs
        yes_bvwap = book_vwap(orderbook_up, "bid", 5)
        no_avwap = book_vwap(orderbook_down, "ask", 5)
        if yes_bvwap > 0 and no_avwap > 0:
            vwap = (yes_bvwap + (1.0 - no_avwap)) / 2.0
        else:
            vwap = rolling_vwap(spots, None, eff_lookback)
    else:
        if anchor > 0:
            vwap = anchored_vwap(spots, None, anchor)
        else:
            vwap = rolling_vwap(spots, None, eff_lookback)

    if vwap <= 0:
        _update_histories(state, spot_price, yp, np_val, vwap)
        return _no_signal("vwap not ready")

    _update_histories(state, spot_price, yp, np_val, vwap)

    if state["cooldown"] > 0:
        state["cooldown"] -= 1
        return _no_signal("cooldown")

    # Deviation and slope
    dev = (spot_price - vwap) / vwap if vwap != 0 else 0.0
    slope = vwap_slope(state["vwap_history"], max(5, lookback // 10))
    regime = regime_slope(state["vwap_history"], max(5, lookback // 10), flip_threshold)

    triggered = False
    direction = None
    confidence = 0.0
    reason = ""

    # -------------------- MODE DISPATCH --------------------
    if mode == "btc_vwap_fade":
        # Fade spot deviation from VWAP
        if abs(dev) * 100 >= threshold:
            if dev > 0 and np_val <= entry_max:
                triggered, direction, reason = True, "NO", f"spot {dev*100:.2f}% above VWAP, fade NO"
            elif dev < 0 and yp <= entry_max:
                triggered, direction, reason = True, "YES", f"spot {dev*100:.2f}% below VWAP, fade YES"
        confidence = min(0.9, abs(dev) * 100 / (threshold + 1.0))

    elif mode == "btc_vwap_trend":
        # Follow spot crossing VWAP with slope confirmation
        cross_up = spot_price > vwap and spots[-2] <= vwap if len(spots) >= 2 else False
        cross_down = spot_price < vwap and spots[-2] >= vwap if len(spots) >= 2 else False
        if cross_up and slope > 0 and yp <= entry_max:
            if _consecutive_moves(spots, "up", confirmation):
                triggered, direction, reason = True, "YES", "spot crossed above VWAP, trend YES"
        elif cross_down and slope < 0 and np_val <= entry_max:
            if _consecutive_moves(spots, "down", confirmation):
                triggered, direction, reason = True, "NO", "spot crossed below VWAP, trend NO"
        confidence = min(0.85, _sigmoid(abs(slope) * 100))

    elif mode == "btc_vwap_breakout":
        # Breakout of VWAP ± N stdev band
        _, _, prices = spots, None, spots
        lower, upper = vwap_std_band(vwap, spots[-lookback:], band_n)
        if spot_price >= upper and yp <= entry_max:
            triggered, direction, reason = True, "YES", f"spot broke upper VWAP+{band_n}σ band"
        elif spot_price <= lower and np_val <= entry_max:
            triggered, direction, reason = True, "NO", f"spot broke lower VWAP-{band_n}σ band"
        confidence = min(0.85, abs(dev) * 50)

    elif mode == "pm_vwap_fade":
        pm_mid = (yp + np_val) / 2.0 if yp > 0 and np_val > 0 else 0.0
        if vwap > 0 and pm_mid > 0:
            pm_dev = (pm_mid - vwap) / vwap
            if abs(pm_dev) * 100 >= threshold:
                if pm_dev > 0 and np_val <= entry_max:
                    triggered, direction, reason = True, "NO", f"PM mid {pm_dev*100:.2f}% above VWAP, fade NO"
                elif pm_dev < 0 and yp <= entry_max:
                    triggered, direction, reason = True, "YES", f"PM mid {pm_dev*100:.2f}% below VWAP, fade YES"
            confidence = min(0.9, abs(pm_dev) * 100 / (threshold + 1.0))

    elif mode == "pm_vwap_trend":
        pm_mid = (yp + np_val) / 2.0 if yp > 0 and np_val > 0 else 0.0
        if vwap > 0 and pm_mid > 0:
            if pm_mid > vwap and slope > 0 and yp <= entry_max:
                triggered, direction, reason = True, "YES", "PM mid above VWAP, trend YES"
            elif pm_mid < vwap and slope < 0 and np_val <= entry_max:
                triggered, direction, reason = True, "NO", "PM mid below VWAP, trend NO"
            confidence = min(0.85, _sigmoid(abs(slope) * 100))

    elif mode == "pm_vwap_divergence":
        # Trade BTC spot vs PM midpoint VWAP divergence
        pm_mid = (yp + np_val) / 2.0 if yp > 0 and np_val > 0 else 0.0
        # Normalize both to %-deviation from their VWAPs
        spot_vwap = rolling_vwap(spots, None, eff_lookback)
        pm_dev = (pm_mid - vwap) / vwap if vwap > 0 else 0.0
        spot_dev = (spot_price - spot_vwap) / spot_vwap if spot_vwap > 0 else 0.0
        diff = spot_dev - pm_dev  # positive = spot more bullish than PM
        if abs(diff) * 100 >= threshold:
            if diff > 0 and np_val <= entry_max:
                triggered, direction, reason = True, "NO", "spot premium vs PM VWAP, fade NO"
            elif diff < 0 and yp <= entry_max:
                triggered, direction, reason = True, "YES", "spot discount vs PM VWAP, fade YES"
        confidence = min(0.85, abs(diff) * 100 / (threshold + 1.0))

    elif mode == "book_vwap_fade":
        if orderbook_up and orderbook_down:
            yes_bvwap = book_vwap(orderbook_up, "bid", 5)
            no_avwap = book_vwap(orderbook_down, "ask", 5)
            if yes_bvwap > 0 and no_avwap > 0:
                # implied fair from book: YES bid should be ~1-NO ask
                book_fair = (yes_bvwap + (1.0 - no_avwap)) / 2.0
                book_dev = (book_fair - vwap) / vwap if vwap > 0 else 0.0
                if abs(book_dev) * 100 >= threshold:
                    if book_dev > 0 and np_val <= entry_max:
                        triggered, direction, reason = True, "NO", "book fair above VWAP, fade NO"
                    elif book_dev < 0 and yp <= entry_max:
                        triggered, direction, reason = True, "YES", "book fair below VWAP, fade YES"
                confidence = min(0.85, abs(book_dev) * 100 / (threshold + 1.0))

    elif mode == "book_imbalance_vwap":
        imb = book_imbalance_val if book_imbalance_val != 0 else (
            book_imbalance(orderbook_up, orderbook_down) if orderbook_up and orderbook_down else 0.0
        )
        if abs(imb) >= threshold / 10.0:  # threshold in tenths
            if imb > 0 and yp <= entry_max:
                triggered, direction, reason = True, "YES", f"book imbalance {imb:.2f}, YES"
            elif imb < 0 and np_val <= entry_max:
                triggered, direction, reason = True, "NO", f"book imbalance {imb:.2f}, NO"
        confidence = min(0.85, abs(imb))

    elif mode == "vwap_ofi":
        # Order-flow imbalance: recent change in book imbalance
        hist_imb = state.get("last_imbalance", 0.0)
        imb = book_imbalance_val if book_imbalance_val != 0 else (
            book_imbalance(orderbook_up, orderbook_down) if orderbook_up and orderbook_down else 0.0
        )
        delta = imb - hist_imb
        state["last_imbalance"] = imb
        if abs(delta) * 100 >= threshold:
            if delta > 0 and yp <= entry_max:
                triggered, direction, reason = True, "YES", f"OFI delta +{delta*100:.2f}, YES"
            elif delta < 0 and np_val <= entry_max:
                triggered, direction, reason = True, "NO", f"OFI delta {delta*100:.2f}, NO"
        confidence = min(0.85, abs(delta) * 50)

    elif mode == "vwap_ladder":
        # Ladder re-tests: price returns to VWAP from outside, enter in direction of original break
        if len(spots) >= lookback + 2:
            recent = spots[-lookback:]
            prev = spots[-lookback - 1]
            inside = any(p > vwap for p in recent) and any(p < vwap for p in recent)
            if inside:
                if prev > vwap and spot_price <= vwap and np_val <= entry_max:
                    triggered, direction, reason = True, "NO", "ladder retest VWAP from above, NO"
                elif prev < vwap and spot_price >= vwap and yp <= entry_max:
                    triggered, direction, reason = True, "YES", "ladder retest VWAP from below, YES"
        confidence = min(0.8, abs(dev) * 50)

    elif mode == "vwap_orb":
        # ORB breakout only if VWAP slope confirms
        orb_window = int(config.get("orb_window", 60))
        if elapsed_sec is not None and elapsed_sec > orb_window:
            # Build OR from first orb_window ticks
            or_prices = spots[:orb_window] if len(spots) > orb_window else spots
            if or_prices:
                or_high, or_low = max(or_prices), min(or_prices)
                if spot_price > or_high and slope > 0 and yp <= entry_max:
                    triggered, direction, reason = True, "YES", "ORB breakout with VWAP slope up"
                elif spot_price < or_low and slope < 0 and np_val <= entry_max:
                    triggered, direction, reason = True, "NO", "ORB breakdown with VWAP slope down"
        confidence = min(0.85, _sigmoid(abs(slope) * 100))

    elif mode == "vwap_mr_combo":
        # Combine z-score fade with VWAP location
        if abs(z_score) >= threshold and abs(dev) * 100 >= threshold / 2.0:
            if z_score > 0 and dev > 0 and np_val <= entry_max:
                triggered, direction, reason = True, "NO", f"z={z_score:.1f} and spot above VWAP, fade NO"
            elif z_score < 0 and dev < 0 and yp <= entry_max:
                triggered, direction, reason = True, "YES", f"z={z_score:.1f} and spot below VWAP, fade YES"
        confidence = min(0.9, abs(z_score) / (threshold + 1.0))

    elif mode == "vwap_regime_flip":
        # Switch between fade and trend based on VWAP slope regime
        if regime == "up":
            # trending up -> follow breakouts / fade breakdowns less
            if spot_price > vwap and yp <= entry_max:
                triggered, direction, reason = True, "YES", "regime up, trend YES"
        elif regime == "down":
            if spot_price < vwap and np_val <= entry_max:
                triggered, direction, reason = True, "NO", "regime down, trend NO"
        else:
            # flat -> mean-revert
            if dev > threshold / 100.0 and np_val <= entry_max:
                triggered, direction, reason = True, "NO", "flat regime, fade NO"
            elif dev < -threshold / 100.0 and yp <= entry_max:
                triggered, direction, reason = True, "YES", "flat regime, fade YES"
        confidence = min(0.8, _sigmoid(abs(slope) * 100))

    elif mode == "vwap_time_slice":
        # Use different lookback (handled by eff_lookback) for fade logic
        if abs(dev) * 100 >= threshold:
            if dev > 0 and np_val <= entry_max:
                triggered, direction, reason = True, "NO", f"time-slice fade NO (lb={eff_lookback})"
            elif dev < 0 and yp <= entry_max:
                triggered, direction, reason = True, "YES", f"time-slice fade YES (lb={eff_lookback})"
        confidence = min(0.85, abs(dev) * 50)

    elif mode == "pm_vwap_momentum":
        if len(yps) >= 2 and len(nps) >= 2:
            pm_mid = (yp + np_val) / 2.0
            pm_prev = (yps[-2] + nps[-2]) / 2.0
            pm_vel = (pm_mid - pm_prev) * 100  # cents per tick
            if abs(pm_vel) >= threshold / 10.0:
                if pm_vel > 0 and slope > 0 and yp <= entry_max:
                    triggered, direction, reason = True, "YES", f"PM momentum +{pm_vel:.2f}, YES"
                elif pm_vel < 0 and slope < 0 and np_val <= entry_max:
                    triggered, direction, reason = True, "NO", f"PM momentum {pm_vel:.2f}, NO"
            confidence = min(0.85, abs(pm_vel) / (threshold + 1.0))

    else:
        return _no_signal(f"unknown mode {mode}")

    if not triggered or direction not in ("YES", "NO"):
        return _no_signal(reason or "no trigger")

    # Apply entry price band and spread guard
    # Use the ask we will actually pay for taker entries (mirrors ORB runners).
    yes_ask = kwargs.get("yes_ask")
    no_ask = kwargs.get("no_ask")
    if direction == "YES":
        entry_price = yes_ask if yes_ask and yes_ask > 0 else yp
    else:
        entry_price = no_ask if no_ask and no_ask > 0 else np_val
    if entry_price < MIN_ENTRY_PRICE or entry_price > entry_max:
        return _no_signal(f"entry price {entry_price:.3f} outside band")
    if spread > 0.04:
        return _no_signal(f"spread {spread:.3f} too wide")

    state["cooldown"] = cooldown_ticks
    state["entry_count"] += 1
    state["last_direction"] = direction

    return {
        "triggered": True,
        "direction": direction,
        "confidence": float(confidence),
        "entry_price": float(entry_price),
        "signal_price": float(entry_price),
        "source": "VWAP_FACTORY",
        "reason": reason,
        "vwap": float(vwap),
        "dev_pct": float(dev * 100),
        "regime": regime,
    }


__all__ = ["vwap_factory_signal"]
