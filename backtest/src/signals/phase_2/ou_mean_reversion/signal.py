def ou_mean_reversion_signal(
    spot_price, strike, yp, np_val, rem_sec, z_score, v_t, std_v, tf_hint="5m", **kwargs
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
            "source": "OU_MEAN_REVERSION",
            "reason": "Time guard",
        }

    if std_v <= 0:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "OU_MEAN_REVERSION",
            "reason": "Zero std_v",
        }

    ou_score = z_score * (1.0 - min(1.0, abs(v_t) / (std_v * 4.0 + 1e-10)))

    if ou_score > 1.5 and np_val <= 0.75:
        triggered = True
        direction = "NO"
        confidence = min(0.85, ou_score / 3.0)
        entry_price = np_val
        reason = f"OU z={z_score:.2f} dampened by velocity, fading NO"
    elif ou_score < -1.5 and yp <= 0.75:
        triggered = True
        direction = "YES"
        confidence = min(0.85, abs(ou_score) / 3.0)
        entry_price = yp
        reason = f"OU z={z_score:.2f} dampened by velocity, fading YES"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "OU_MEAN_REVERSION",
        "reason": reason,
    }


__all__ = ["ou_mean_reversion_signal"]
