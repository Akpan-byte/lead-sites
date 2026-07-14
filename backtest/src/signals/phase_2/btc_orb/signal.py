# CHANGE_SUMMARY
# 2026-07-03  kilo
#   - Added 30m and 1h to the duration map and made the map fall back to a
#     numeric tf_hint so the signal no longer treats 1h/30m markets as 5m.
#   - Tightened the OR window guard to reject only when or_window_seconds is
#     greater than or equal to the market duration.
# WHY: The 1h ORB variants were dead because or_window_seconds (900) was being
#      compared against a default 5m duration (300), triggering the
#      "or_window >= duration" rejection for every market.

"""
BTC Spot Opening Range Breakout for Polymarket UP/DOWN markets.
Tracks BTC spot price within each contract's opening window,
then trades breakouts of that range — no time gate.
"""

import logging
from collections import defaultdict

log = logging.getLogger("btc_orb")

BUFFER_PCT = 0.0005
MIN_COOLDOWN_TICKS = 3
REENTRY_SIZE_SCALE = [1.0, 0.75, 0.50, 0.33]

_STATE: dict = {}

OR_WINDOW_LABELS = {
    60: "1m",
    180: "3m",
    300: "5m",
    900: "15m",
    1800: "30m",
    3600: "1h",
}


def _make_state():
    return {
        "or_high": float("-inf"),
        "or_low": float("inf"),
        "or_closed": False,
        "entry_count": {"YES": 0, "NO": 0},
        "inside_after_break": {"YES": False, "NO": False},
        "cooldown": {"YES": 0, "NO": 0},
    }


def _no_signal(reason):
    return {
        "triggered": False,
        "direction": None,
        "confidence": 0.0,
        "entry_price": 0.0,
        "signal_price": 0.0,
        "source": "BTC_ORB",
        "reason": reason,
    }


def btc_orb_signal(
    spot_price=None,
    strike=None,
    rem_sec=0,
    yp=None,
    np_val=None,
    yes_ask=None,
    no_ask=None,
    tf_hint=None,
    market_id=None,
    or_window_seconds=60,
    max_reentries=12,
    **kwargs,
):
    if spot_price is None:
        return _no_signal("no spot")

    duration_map = {
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "4h": 14400,
        "1d": 86400,
    }
    duration = duration_map.get(tf_hint)
    if duration is None:
        try:
            duration = int(tf_hint)
        except Exception:
            duration = 300

    if or_window_seconds >= duration:
        return _no_signal("or_window >= duration")

    state_key = (or_window_seconds, max_reentries, market_id, tf_hint)
    state = _STATE.setdefault(state_key, _make_state())

    for d in ("YES", "NO"):
        if state["cooldown"][d] > 0:
            state["cooldown"][d] -= 1

    elapsed = duration - rem_sec

    if elapsed <= or_window_seconds:
        if spot_price > state["or_high"]:
            state["or_high"] = spot_price
        if spot_price < state["or_low"]:
            state["or_low"] = spot_price
        return _no_signal("OR window active")

    state["or_closed"] = True

    if state["or_high"] <= 0 or state["or_low"] == float("inf"):
        # Process started after the true opening range closed (e.g., after a
        # deploy/restart). Seed the range from the current spot so the strategy
        # can still trade breakouts going forward instead of remaining dead.
        state["or_high"] = spot_price
        state["or_low"] = spot_price
        return _no_signal("OR range seeded from current spot")

    if rem_sec < 5:
        return _no_signal("time guard")

    buf = spot_price * BUFFER_PCT
    buy_trigger = state["or_high"] + buf
    sell_trigger = state["or_low"] - buf

    if spot_price < state["or_high"] and state["entry_count"]["YES"] > 0:
        state["inside_after_break"]["YES"] = True
    if spot_price > state["or_low"] and state["entry_count"]["NO"] > 0:
        state["inside_after_break"]["NO"] = True

    triggered = False
    direction = None
    entry_price = 0.0
    entry_index = 0

    if spot_price >= buy_trigger:
        n = state["entry_count"]["YES"]
        is_first = n == 0
        is_reentry = n > 0 and n <= max_reentries and state["inside_after_break"]["YES"]
        if (is_first or is_reentry) and state["cooldown"]["YES"] == 0:
            triggered = True
            direction = "YES"
            entry_index = n
            # Use the ask price we will actually pay; fall back to bid or 0.5.
            entry_price = yes_ask if yes_ask else (yp if yp else 0.5)
            state["entry_count"]["YES"] += 1
            state["inside_after_break"]["YES"] = False
            state["cooldown"]["YES"] = MIN_COOLDOWN_TICKS

    elif spot_price <= sell_trigger:
        n = state["entry_count"]["NO"]
        is_first = n == 0
        is_reentry = n > 0 and n <= max_reentries and state["inside_after_break"]["NO"]
        if (is_first or is_reentry) and state["cooldown"]["NO"] == 0:
            triggered = True
            direction = "NO"
            entry_index = n
            # Use the ask price we will actually pay; fall back to bid or 0.5.
            entry_price = no_ask if no_ask else (np_val if np_val else 0.5)
            state["entry_count"]["NO"] += 1
            state["inside_after_break"]["NO"] = False
            state["cooldown"]["NO"] = MIN_COOLDOWN_TICKS

    if not triggered:
        return _no_signal(
            "spot=%.2f inside [%.2f, %.2f]" % (spot_price, sell_trigger, buy_trigger)
        )

    size_scale = REENTRY_SIZE_SCALE[min(entry_index, len(REENTRY_SIZE_SCALE) - 1)]

    return {
        "triggered": True,
        "direction": direction,
        "confidence": size_scale,
        "entry_price": entry_price,
        "signal_price": entry_price,
        "source": "BTC_ORB",
        "entry_index": entry_index,
        "size_scale": size_scale,
        "is_reentry": entry_index > 0,
        "or_window_seconds": or_window_seconds,
        "reason": (
            "%s #%s dir=%s spot=%.2f trigger=%s price=%.3f"
            % (
                "RE-ENTRY" if entry_index > 0 else "FIRST",
                entry_index,
                direction,
                spot_price,
                ">=%.2f" % buy_trigger
                if direction == "YES"
                else "<=%.2f" % sell_trigger,
                entry_price,
            )
        ),
    }
