def cross_exchange_drift_signal(
    spot_price,
    strike,
    yp,
    np_val,
    rem_sec,
    v_t,
    std_v,
    z_score,
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
            "source": "CROSS_EXCHANGE_DRIFT",
            "reason": "Time guard",
        }

    if std_v <= 0 or not velocity_history or len(velocity_history) < 10:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "CROSS_EXCHANGE_DRIFT",
            "reason": "Not enough data",
        }

    rolling_mean = sum(velocity_history[-10:]) / 10
    drift = rolling_mean / (std_v + 1e-10)

    if drift > 1.5 and z_score > 1.0 and np_val <= 0.75:
        triggered = True
        direction = "NO"
        confidence = min(0.80, drift / 4.0)
        entry_price = np_val
        reason = f"Persistent upward drift ({drift:.2f}), exhaustion anticipated"
    elif drift < -1.5 and z_score < -1.0 and yp <= 0.75:
        triggered = True
        direction = "YES"
        confidence = min(0.80, abs(drift) / 4.0)
        entry_price = yp
        reason = f"Persistent downward drift ({drift:.2f}), exhaustion anticipated"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "CROSS_EXCHANGE_DRIFT",
        "reason": reason,
    }


__all__ = ["cross_exchange_drift_signal"]
