def absorption_reversal_signal(
    spot_price,
    strike,
    yp,
    np_val,
    rem_sec,
    v_t,
    std_v,
    a_t,
    spread,
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
            "source": "ABSORPTION_REVERSAL",
            "reason": "Time guard",
        }

    if std_v <= 0:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "ABSORPTION_REVERSAL",
            "reason": "Zero std_v",
        }

    v_sigma = abs(v_t) / std_v

    if v_sigma > 2.0 and spread < 0.06:
        if v_t > 0 and np_val <= 0.80:
            triggered = True
            direction = "NO"
            confidence = min(0.85, v_sigma / 5.0)
            entry_price = np_val
            reason = (
                f"Upward momentum absorbed (v_sigma={v_sigma:.1f}, spread={spread:.3f})"
            )
        elif v_t < 0 and yp <= 0.80:
            triggered = True
            direction = "YES"
            confidence = min(0.85, v_sigma / 5.0)
            entry_price = yp
            reason = f"Downward momentum absorbed (v_sigma={v_sigma:.1f}, spread={spread:.3f})"
    elif v_sigma > 1.5 and a_t < 0 and v_t > 0 and np_val <= 0.80:
        triggered = True
        direction = "NO"
        confidence = 0.60
        entry_price = np_val
        reason = f"Upward velocity decelerating (a_t={a_t:.2f})"
    elif v_sigma > 1.5 and a_t > 0 and v_t < 0 and yp <= 0.80:
        triggered = True
        direction = "YES"
        confidence = 0.60
        entry_price = yp
        reason = f"Downward velocity decelerating (a_t={a_t:.2f})"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "ABSORPTION_REVERSAL",
        "reason": reason,
    }


__all__ = ["absorption_reversal_signal"]
