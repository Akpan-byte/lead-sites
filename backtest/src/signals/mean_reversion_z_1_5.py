import math


def mean_reversion_z_1_5_signal(
    spot_price, strike, z_score, rem_sec, yp, np_val
) -> dict:
    triggered = False
    direction = None
    confidence = 0.0
    entry_price = 0.0
    reason = ""

    if rem_sec <= 5 or rem_sec >= 295:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "MEAN_REVERSION_Z_1.5",
            "reason": f"Time guard active: {rem_sec}s remaining",
        }

    if rem_sec < 45:
        reason = f"Near-expiry guard active: {rem_sec}s < 45s"
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "MEAN_REVERSION_Z_1.5",
            "reason": reason,
        }

    if z_score >= 1.5:
        if np_val <= 0.80:
            triggered = True
            direction = "NO"
            confidence = 1.0
            entry_price = np_val
            reason = f"Mean Reversion (Fade Up): z_score {z_score:.2f} >= 1.5, np_val {np_val:.2f} <= 0.80"
        else:
            reason = f"z_score {z_score:.2f} >= 1.5 but np_val {np_val:.2f} > 0.80"
    elif z_score <= -1.5:
        if yp <= 0.80:
            triggered = True
            direction = "YES"
            confidence = 1.0
            entry_price = yp
            reason = f"Mean Reversion (Fade Down): z_score {z_score:.2f} <= -1.5, yp {yp:.2f} <= 0.80"
        else:
            reason = f"z_score {z_score:.2f} <= -1.5 but yp {yp:.2f} > 0.80"
    else:
        reason = f"z_score {z_score:.2f} within bounds [-1.5, 1.5]"

    return {
        "triggered": triggered,
        "direction": direction if triggered else None,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "MEAN_REVERSION_Z_1.5",
        "reason": reason if reason else "No signal",
    }
