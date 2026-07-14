# CHANGE_SUMMARY
# 2026-07-03  kilo
#   - Made the time guard duration-aware using tf_hint/rem_sec.
#   - Added a z_score >= |0.5| alternative trigger.
#   - Raised entry-price cap from 0.80 to 0.85.
# WHY: Same root cause as breakout_pct_003: hard-coded 5m guard and absolute
#      pct threshold left the strategy with zero live trades.

import math


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


def breakout_pct_004_signal(
    spot_price, strike, z_score, rem_sec, yp=None, np_val=None, tf_hint="5m"
) -> dict:
    val = 0.04
    threshold = val / 100.0
    upper_level = strike * (1.0 + threshold)
    lower_level = strike * (1.0 - threshold)

    triggered = False
    direction = None
    reason = "Price within bounds"

    duration = _duration_seconds(tf_hint, rem_sec)
    if rem_sec <= 5 or rem_sec >= duration - 5:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "BREAKOUT_PCT_0.04",
            "reason": f"Time guard active: {rem_sec}s remaining",
        }

    if spot_price >= upper_level or z_score >= 0.5:
        direction = "YES"
        reason = f"Spot {spot_price:.2f} >= Upper {upper_level:.2f} (strike {strike:.2f} + {val}%) or z_score={z_score:.2f}"
    elif spot_price <= lower_level or z_score <= -0.5:
        direction = "NO"
        reason = f"Spot {spot_price:.2f} <= Lower {lower_level:.2f} (strike {strike:.2f} - {val}%) or z_score={z_score:.2f}"

    if direction:
        market_price = yp if direction == "YES" else np_val
        if market_price is not None and market_price <= 0.85:
            triggered = True
            try:
                fair_p = 1.0 / (1.0 + math.exp(-2.2 * z_score))
            except OverflowError:
                fair_p = 1.0 if z_score > 0 else 0.0
            confidence = fair_p if direction == "YES" else (1.0 - fair_p)
            reason += f" | Entry at {market_price:.2f}"
        else:
            triggered = False
            reason += (
                f" | Market price {market_price if market_price else 'N/A'} > 0.85 cap"
            )
    else:
        confidence = 0.0

    return {
        "triggered": triggered,
        "direction": direction if triggered else None,
        "confidence": round(confidence, 4) if triggered else 0.0,
        "signal_price": spot_price,
        "entry_price": round(market_price, 2) if triggered else 0.0,
        "source": "BREAKOUT_PCT_0.04",
        "reason": reason if triggered or direction else "Price within bounds",
    }
