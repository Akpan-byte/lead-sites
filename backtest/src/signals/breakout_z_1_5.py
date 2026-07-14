def breakout_z_1_5_signal(
    spot_price, strike, z_score, rem_sec, yp=None, np_val=None
) -> dict:
    z_threshold = 1.5
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
            "source": "BREAKOUT_Z_1.5",
            "reason": f"Time guard active: {rem_sec}s remaining",
        }

    if z_score >= z_threshold and yp is not None and yp <= 0.80:
        triggered = True
        direction = "YES"
        entry_price = yp
        confidence = min(1.0, abs(z_score) / 3.0)
        reason = f"Breakout Z-score {z_score:.2f} >= {z_threshold}"
    elif z_score <= -z_threshold and np_val is not None and np_val <= 0.80:
        triggered = True
        direction = "NO"
        entry_price = np_val
        confidence = min(1.0, abs(z_score) / 3.0)
        reason = f"Breakout Z-score {z_score:.2f} <= -{z_threshold}"

    return {
        "triggered": triggered,
        "direction": direction if triggered else None,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "BREAKOUT_Z_1.5",
        "reason": reason,
    }
