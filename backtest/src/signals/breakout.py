# CHANGE_SUMMARY
# 2026-07-03  kilo
#   - Made the time guard duration-aware using tf_hint/rem_sec.
#   - Added z_score as an alternative trigger (|z| >= 0.8) so the signal fires on
#     persistent small-tick trends, not just large absolute pct moves.
#   - Raised entry-price cap from 0.80 to 0.85 to match runner default.
# WHY: breakout had zero live trades because the 0.05% fixed threshold was rarely
#      reached on the coarse spot feed, and the 295s guard ignored 15m/1h markets.


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


def breakout_signal(spot_price, strike, z_score, rem_sec, yp, np_val, tf_hint="5m") -> dict:
    signal = {
        "triggered": False,
        "direction": None,
        "confidence": 0.0,
        "signal_price": 0.0,
        "entry_price": 0.0,
        "source": "BREAKOUT",
        "reason": "",
    }

    duration = _duration_seconds(tf_hint, rem_sec)
    if rem_sec <= 5 or rem_sec >= duration - 5:
        signal["reason"] = f"Time guard active: {rem_sec}s remaining"
        return signal

    breakout_pct = 0.0005
    upper_level = strike * (1.0 + breakout_pct)
    lower_level = strike * (1.0 - breakout_pct)
    if (spot_price >= upper_level or z_score >= 0.8) and yp <= 0.85:
        signal["triggered"] = True
        signal["direction"] = "YES"
        signal["confidence"] = max(
            min(1.0, (spot_price - upper_level) / strike),
            min(1.0, abs(z_score) / 3.0),
        )
        signal["signal_price"] = spot_price
        signal["entry_price"] = yp
        signal["reason"] = (
            f"Breakout YES: price={spot_price:.2f} > upper={upper_level:.2f} or z={z_score:.2f}"
        )
    elif (spot_price <= lower_level or z_score <= -0.8) and np_val <= 0.85:
        signal["triggered"] = True
        signal["direction"] = "NO"
        signal["confidence"] = max(
            min(1.0, (lower_level - spot_price) / strike),
            min(1.0, abs(z_score) / 3.0),
        )
        signal["signal_price"] = spot_price
        signal["entry_price"] = np_val
        signal["reason"] = (
            f"Breakout NO: price={spot_price:.2f} < lower={lower_level:.2f} or z={z_score:.2f}"
        )
    return signal
