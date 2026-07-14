# CHANGE_SUMMARY
# 2026-07-03  kilo
#   - Made the time guard duration-aware using tf_hint/rem_sec, so 15m/1h markets
#     are not rejected after the first 5 minutes.
#   - Added z_score as an alternative trigger (|z| >= 0.5).
#   - Lowered the breakout threshold from 0.05% to 0.03% and raised the price cap
#     from 0.80 to 0.85.
# WHY: mean_reversion_opposite_exit had zero trades because it hard-coded a 5m
#      guard and an absolute 0.05% threshold that the live spot feed rarely met.


def _duration_seconds(tf_hint, rem_sec):
    if tf_hint == "5m":
        return 300
    if tf_hint == "15m":
        return 900
    if tf_hint == "30m":
        return 1800
    if tf_hint == "1h":
        return 3600
    if tf_hint == "4h":
        return 14400
    if tf_hint == "1d":
        return 86400
    if rem_sec > 1800:
        return 3600
    if rem_sec > 600:
        return 1800
    if rem_sec > 300:
        return 900
    return 300


def mean_reversion_opposite_exit_signal(
    spot_price, strike, z_score, rem_sec, yp, np_val, tf_hint="5m"
) -> dict:
    """
    Enters OPPOSITE direction (breakout-following) trades.
    Despite the name, this is NOT an exit strategy — it enters new breakout trades.
    Renamed source to BREAKOUT_OPPOSITE for regime shield compatibility.
    """
    triggered = False
    direction = None
    confidence = 0.0
    entry_price = 0.0
    reason = ""

    breakout_pct = 0.0003
    upper_level = strike * (1.0 + breakout_pct)
    lower_level = strike * (1.0 - breakout_pct)

    duration = _duration_seconds(tf_hint, rem_sec)
    if rem_sec <= 5 or rem_sec >= duration - 5:
        reason = f"Time guard active: {rem_sec}s remaining"
        direction = None
    elif rem_sec >= 45:
        if (spot_price >= upper_level or z_score >= 0.5) and yp <= 0.85:
            triggered = True
            direction = "YES"
            confidence = 1.0
            entry_price = yp
            reason = f"Breakout UP: spot {spot_price:.2f} >= {upper_level:.2f} or z={z_score:.2f} (strike {strike})"

        elif (spot_price <= lower_level or z_score <= -0.5) and np_val <= 0.85:
            triggered = True
            direction = "NO"
            confidence = 1.0
            entry_price = np_val
            reason = f"Breakout DOWN: spot {spot_price:.2f} <= {lower_level:.2f} or z={z_score:.2f} (strike {strike})"

    return {
        "triggered": triggered,
        "direction": direction if triggered else None,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "MEAN_REVERSION_OPPOSITE_EXIT",
        "reason": reason,
    }
