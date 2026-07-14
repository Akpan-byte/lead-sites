# CHANGE_SUMMARY
# 2026-07-03  kilo
#   - Added optional z_score and tf_hint parameters.
#   - Lowered the fade threshold from 0.05% to 0.03% and allowed a |z_score| >= 0.5
#     over-extension to trigger even when the absolute spot move is smaller.
#   - Raised entry-price cap from 0.80 to 0.85.
# WHY: mean_reversion had zero trades because the 0.05% fixed threshold was too
#      strict for the live spot feed; z-score confirmation provides an alternate,
#      lower-latency path while still fading over-extensions.


def mean_reversion_signal(spot_price, strike, z_score, rem_sec, yp, np_val, tf_hint="5m") -> dict:
    """
    Returns signal dict with: triggered (bool), direction (YES/NO), confidence (float),
    signal_price (float), entry_price (float), source (str), reason (str)

    Trigger: spot >= strike*1.0003 -> NO trade (fade up). spot <= strike*0.9997 -> YES trade (fade down).
    Also fires on |z_score| >= 0.5. rem_sec >= 45. Price <= 0.85.
    """
    triggered = False
    direction = None
    confidence = 0.0
    signal_price = 0.0
    reason = ""

    upper_level = strike * 1.0003
    lower_level = strike * 0.9997

    if rem_sec >= 45:
        if spot_price >= upper_level or z_score >= 0.5:
            if np_val <= 0.85:
                triggered = True
                direction = "NO"
                confidence = 1.0
                signal_price = np_val
                reason = f"Spot {spot_price:.2f} >= Upper {upper_level:.2f} or z={z_score:.2f} (Fade Up)"
            else:
                reason = f"Spot {spot_price:.2f} >= Upper {upper_level:.2f} but price {np_val:.2f} > 0.85"
        elif spot_price <= lower_level or z_score <= -0.5:
            if yp <= 0.85:
                triggered = True
                direction = "YES"
                confidence = 1.0
                signal_price = yp
                reason = f"Spot {spot_price:.2f} <= Lower {lower_level:.2f} or z={z_score:.2f} (Fade Down)"
            else:
                reason = f"Spot {spot_price:.2f} <= Lower {lower_level:.2f} but price {yp:.2f} > 0.85"
        else:
            reason = f"Spot {spot_price:.2f} within range [{lower_level:.2f}, {upper_level:.2f}]"
    else:
        reason = f"Time remaining {rem_sec}s < 45s"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": signal_price,
        "entry_price": signal_price, # In paper bot these are often same at trigger
        "source": "MEAN_REVERSION",
        "reason": reason
    }
