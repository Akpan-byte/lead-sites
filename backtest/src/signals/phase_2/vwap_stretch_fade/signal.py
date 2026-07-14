def vwap_stretch_fade_signal(
    spot_price, strike, yp, np_val, rem_sec, z_score, spread, tf_hint="5m", **kwargs
) -> dict:
    triggered = False
    direction = None
    confidence = 0.0
    entry_price = 0.0
    reason = ""

    time_buf = 895 if tf_hint == "15m" else 295
    if rem_sec <= 5 or rem_sec >= time_buf:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "VWAP_STRETCH_FADE",
            "reason": "Time guard",
        }

    stretch = abs(z_score)
    if stretch > 2.0 and spread > 0.03:
        if z_score > 0 and np_val <= 0.75:
            triggered = True
            direction = "NO"
            confidence = min(0.85, stretch / 4.0)
            entry_price = np_val
            reason = f"VWAP stretch +{z_score:.1f}sigma, fading NO"
        elif z_score < 0 and yp <= 0.75:
            triggered = True
            direction = "YES"
            confidence = min(0.85, stretch / 4.0)
            entry_price = yp
            reason = f"VWAP stretch {z_score:.1f}sigma, fading YES"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "VWAP_STRETCH_FADE",
        "reason": reason,
    }


__all__ = ["vwap_stretch_fade_signal"]
