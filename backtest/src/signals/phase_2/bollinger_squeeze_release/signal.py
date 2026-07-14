def bollinger_squeeze_release_signal(
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
            "source": "BOLLINGER_SQUEEZE_RELEASE",
            "reason": "Time guard",
        }

    if std_v <= 0:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "BOLLINGER_SQUEEZE_RELEASE",
            "reason": "Zero std_v",
        }

    is_squeezed = abs(z_score) < 0.5

    if is_squeezed:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "BOLLINGER_SQUEEZE_RELEASE",
            "reason": f"Squeezed (z={z_score:.2f}), waiting for release",
        }

    if abs(z_score) > 2.0:
        if z_score > 0 and v_t > 0 and yp <= 0.75:
            triggered = True
            direction = "YES"
            confidence = min(0.80, abs(z_score) / 4.0)
            entry_price = yp
            reason = f"Squeeze release up (z={z_score:.1f}, v={v_t:.2f})"
        elif z_score < 0 and v_t < 0 and np_val <= 0.75:
            triggered = True
            direction = "NO"
            confidence = min(0.80, abs(z_score) / 4.0)
            entry_price = np_val
            reason = f"Squeeze release down (z={z_score:.1f}, v={v_t:.2f})"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "BOLLINGER_SQUEEZE_RELEASE",
        "reason": reason,
    }


__all__ = ["bollinger_squeeze_release_signal"]
