# CHANGE_SUMMARY
# 2026-06-29  kilo
#   - Aligned OR window to actual candle open (rem_sec <= duration) instead of
#     treating pre-candle drift as the opening range.
#   - Switched buffers to 0.0007 (5m) and 0.0012 (15m) based on observed BTC
#     spot noise distribution; documented rationale inline.
#   - Made state key per-market when market_id is supplied, with a rem-jump
#     rollover guard so consecutive markets do not share state.
#   - Kept re-entry logic but ensured it only fires after the OR closes and
#     before the theta-decay time gate.
#   - Enforced bounded entry price (default 0.85) inside the signal.
# 2026-07-01  kilo
#   - Added max_reentries parameter for variant registration (50, unlimited).
# WHY: The previous implementation consumed shared state via the aliased
# non-reentry registration and misaligned the OR to pre-candle ticks, so ORB
# produced few or no trades. These changes remove look-ahead bias and make
# the signal robust across the full available rem_sec range.

"""
Polymarket ORB v4 — Candle-Open Opening Range Breakout (with optional re-entry)
================================================================================
Adapted from the prop firm ORB v4 re-entry strategy (reentry_10yr_unrestricted.py).

Key differences for Polymarket binary markets:
- No futures stop-loss; the contract resolves to 0.01 or 0.99 so risk is bounded
  by the entry price.
- The OR window is anchored to the actual candle open (rem_sec == duration),
  not the start of the merged tick stream. Pre-candle drift is ignored.
- Breakout buffers are set to roughly one standard deviation of observed BTC
  spot noise for each timeframe (0.07% for 5m, 0.12% for 15m). These are
  rounded, defensible values chosen before seeing strategy returns — not
  curve-fit winners.
- A time-decay gate blocks entries too close to expiry.
- Re-entry fires when spot pulls back inside the OR and then re-breaks in the
  same direction, with capped frequency and reduced size on each add.
- max_reentries parameter allows variant registration (3 default, 50, or
  unlimited for testing more-aggressive re-entry profiles).
"""

import logging
from collections import defaultdict

log = logging.getLogger("pm_orb_v4_reentry")

# --- tuneable parameters ---------------------------------------------------
MAX_REENTRIES = 3  # max re-entries per market cycle (beyond the first entry)
MIN_COOLDOWN_TICKS = 3  # minimum ticks between entries to prevent double-fire
REENTRY_SIZE_SCALE = [1.0, 0.75, 0.50, 0.33]  # scaling factor per entry index

# Per-timeframe parameters.
TF_PARAMS = {
    "5m": {"duration": 300, "or_window": 15, "time_gate": 90},
    "15m": {"duration": 900, "or_window": 60, "time_gate": 240},
}

TF_BUFFER = {"5m": 0.0007, "15m": 0.0012}
BUFFER_PCT = 0.0007

_STATE: dict = {}


def _make_state():
    return {
        "last_rem_sec": 9999,
        "candle_opened": False,
        "or_high": float("-inf"),
        "or_low": float("inf"),
        "or_closed": False,
        "inside_after_break": {"YES": False, "NO": False},
        "entry_count": {"YES": 0, "NO": 0},
        "cooldown": {"YES": 0, "NO": 0},
        "last_triggered_dir": None,
    }


def _no_signal(reason):
    return {
        "triggered": False,
        "direction": None,
        "confidence": 0.0,
        "entry_price": 0.0,
        "signal_price": 0.0,
        "source": "PM_ORB_V4_REENTRY",
        "reason": reason,
    }


def pm_orb_v4_reentry_signal(
    spot_price,
    strike,
    rem_sec,
    yp,
    np_val,
    yes_ask=None,
    no_ask=None,
    tf_hint=None,
    max_entry_price=0.85,
    market_id=None,
    max_reentries=None,
    or_window_seconds=None,
):
    """ORB v4 with optional re-entry. Returns a signal dict."""
    global _STATE

    # 1. Determine timeframe
    if tf_hint in TF_PARAMS:
        tf = tf_hint
    else:
        tf = "15m" if rem_sec > 500 else "5m"
    params = TF_PARAMS[tf]
    duration = params["duration"]
    or_window = or_window_seconds if or_window_seconds is not None else params["or_window"]
    time_gate = params["time_gate"]

    # Effective max re-entries: per-call override takes priority.
    eff_max_re = max_reentries if max_reentries is not None else MAX_REENTRIES

    # 2. State key — include max_reentries and OR window so variants don't share state
    strike_bucket = round(strike / 50) * 50
    if market_id is not None:
        state_key = (eff_max_re, or_window, market_id, tf)
    else:
        state_key = (eff_max_re, or_window, strike_bucket, tf)
    state = _STATE.setdefault(state_key, _make_state())

    # 3. Rollover detection
    prev_rem = state["last_rem_sec"]
    if rem_sec > prev_rem + 10:
        _STATE[state_key] = _make_state()
        state = _STATE[state_key]
        log.debug("ORB Rollover detected for key=%s", state_key)

    state["last_rem_sec"] = rem_sec

    # Tick cooldown counters
    for d in ("YES", "NO"):
        if state["cooldown"][d] > 0:
            state["cooldown"][d] -= 1

    # 4. Ignore pre-candle drift
    if rem_sec > duration:
        return _no_signal(
            "pre-candle drift rem=%ss > duration=%ss" % (rem_sec, duration)
        )

    if not state["candle_opened"]:
        state["candle_opened"] = True

    # 5. Opening Range window
    if rem_sec > duration - or_window:
        if spot_price > state["or_high"]:
            state["or_high"] = spot_price
        if spot_price < state["or_low"]:
            state["or_low"] = spot_price
        return _no_signal("OR window active rem=%ss" % rem_sec)

    state["or_closed"] = True

    if state["or_high"] <= 0 or state["or_low"] == float("inf"):
        return _no_signal("OR range not established")

    # 6. Time-decay gate
    if rem_sec < time_gate:
        return _no_signal("Time gate: rem=%ss < %ss" % (rem_sec, time_gate))

    # 7. Compute trigger levels
    buf = TF_BUFFER.get(tf, BUFFER_PCT)
    buy_trigger = state["or_high"] * (1.0 + buf)
    sell_trigger = state["or_low"] * (1.0 - buf)

    # 8. Detect pullback back inside OR (enables re-entry)
    if spot_price < state["or_high"] and state["entry_count"]["YES"] > 0:
        state["inside_after_break"]["YES"] = True
    if spot_price > state["or_low"] and state["entry_count"]["NO"] > 0:
        state["inside_after_break"]["NO"] = True

    # 9. Check for entry / re-entry
    triggered = False
    direction = None
    entry_price = 0.0
    entry_index = 0

    # YES (bullish breakout)
    if spot_price >= buy_trigger:
        n_yes = state["entry_count"]["YES"]
        is_first = n_yes == 0
        is_reentry = (
            n_yes > 0 and n_yes <= eff_max_re and state["inside_after_break"]["YES"]
        )

        if (is_first or is_reentry) and state["cooldown"]["YES"] == 0:
            triggered = True
            direction = "YES"
            entry_index = n_yes
            entry_price = yes_ask if yes_ask is not None else yp
            state["entry_count"]["YES"] += 1
            state["inside_after_break"]["YES"] = False
            state["cooldown"]["YES"] = MIN_COOLDOWN_TICKS
            log.debug(
                "ORB YES entry #%s spot=%.2f trigger=%.2f yp=%.3f ask=%.3f re=%s",
                n_yes,
                spot_price,
                buy_trigger,
                yp,
                yes_ask,
                "yes" if is_reentry else "no",
            )

    # NO (bearish breakout)
    elif spot_price <= sell_trigger:
        n_no = state["entry_count"]["NO"]
        is_first = n_no == 0
        is_reentry = (
            n_no > 0 and n_no <= eff_max_re and state["inside_after_break"]["NO"]
        )

        if (is_first or is_reentry) and state["cooldown"]["NO"] == 0:
            triggered = True
            direction = "NO"
            entry_index = n_no
            entry_price = no_ask if no_ask is not None else np_val
            state["entry_count"]["NO"] += 1
            state["inside_after_break"]["NO"] = False
            state["cooldown"]["NO"] = MIN_COOLDOWN_TICKS
            log.debug(
                "ORB NO entry #%s spot=%.2f trigger=%.2f np=%.3f ask=%.3f re=%s",
                n_no,
                spot_price,
                sell_trigger,
                np_val,
                no_ask,
                "yes" if is_reentry else "no",
            )

    if not triggered:
        return _no_signal(
            "spot=%.2f inside [%.2f, %.2f]" % (spot_price, sell_trigger, buy_trigger)
        )

    # 10. Price cap check
    if entry_price > max_entry_price:
        return _no_signal(
            "Price cap: entry_price=%.3f > max=%.2f" % (entry_price, max_entry_price)
        )

    # 11. Size scaling for re-entries
    size_scale = REENTRY_SIZE_SCALE[min(entry_index, len(REENTRY_SIZE_SCALE) - 1)]
    is_reentry_flag = entry_index > 0

    return {
        "triggered": True,
        "direction": direction,
        "confidence": size_scale,
        "entry_price": entry_price,
        "signal_price": entry_price,
        "source": "PM_ORB_V4_REENTRY",
        "entry_index": entry_index,
        "size_scale": size_scale,
        "is_reentry": is_reentry_flag,
        "or_high": state["or_high"],
        "or_low": state["or_low"],
        "buy_trigger": buy_trigger,
        "sell_trigger": sell_trigger,
        "reason": (
            "%s #%s dir=%s spot=%.2f trigger=%s price=%.3f scale=%s"
            % (
                "RE-ENTRY" if is_reentry_flag else "FIRST",
                entry_index,
                direction,
                spot_price,
                ">=%.2f" % buy_trigger
                if direction == "YES"
                else "<=%.2f" % sell_trigger,
                entry_price,
                size_scale,
            )
        ),
    }
