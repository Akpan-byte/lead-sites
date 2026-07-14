def cvd_divergence_signal(
    spot_price,
    strike,
    yp,
    np_val,
    rem_sec,
    z_score,
    tick_change,
    velocity_history,
    tf_hint="5m",
    **kwargs,
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
            "source": "CVD_DIVERGENCE",
            "reason": "Time guard",
        }

    if not velocity_history or len(velocity_history) < 20:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "CVD_DIVERGENCE",
            "reason": "Not enough velocity history",
        }

    recent_v = velocity_history[-20:]
    cvd = sum(1.0 if v > 0 else -1.0 if v < 0 else 0.0 for v in recent_v)
    cvd_z = cvd / (len(recent_v) ** 0.5 * 0.5 + 1e-10)

    if z_score > 1.5 and cvd_z < -0.5:
        if np_val <= 0.80:
            triggered = True
            direction = "NO"
            confidence = min(0.90, abs(z_score) / 4.0)
            entry_price = np_val
            reason = f"Price high but CVD fading (cvd_z={cvd_z:.2f}, z={z_score:.2f})"
    elif z_score < -1.5 and cvd_z > 0.5:
        if yp <= 0.80:
            triggered = True
            direction = "YES"
            confidence = min(0.90, abs(z_score) / 4.0)
            entry_price = yp
            reason = f"Price low but CVD rising (cvd_z={cvd_z:.2f}, z={z_score:.2f})"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "CVD_DIVERGENCE",
        "reason": reason,
    }


__all__ = ["cvd_divergence_signal"]
