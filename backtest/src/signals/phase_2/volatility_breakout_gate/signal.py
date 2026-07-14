def volatility_breakout_gate_signal(
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
            "source": "VOLATILITY_BREAKOUT_GATE",
            "reason": "Time guard",
        }

    if std_v <= 0 or not velocity_history or len(velocity_history) < 30:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "VOLATILITY_BREAKOUT_GATE",
            "reason": "Not enough data",
        }

    recent_15 = velocity_history[-15:]
    old_15 = (
        velocity_history[-30:-15]
        if len(velocity_history) >= 30
        else velocity_history[-15:]
    )
    recent_vol = sum(v * v for v in recent_15) / 15
    old_vol = sum(v * v for v in old_15) / max(len(old_15), 1)
    vol_expansion = recent_vol / (old_vol + 1e-10)

    v_sigma = abs(v_t) / std_v

    if vol_expansion > 2.0 and v_sigma > 2.0:
        if v_t > 0 and z_score > 0.5 and yp <= 0.75:
            triggered = True
            direction = "YES"
            confidence = min(0.85, vol_expansion / 4.0)
            entry_price = yp
            reason = f"Vol expansion {vol_expansion:.1f}x, breakout YES"
        elif v_t < 0 and z_score < -0.5 and np_val <= 0.75:
            triggered = True
            direction = "NO"
            confidence = min(0.85, vol_expansion / 4.0)
            entry_price = np_val
            reason = f"Vol expansion {vol_expansion:.1f}x, breakout NO"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "VOLATILITY_BREAKOUT_GATE",
        "reason": reason,
    }


__all__ = ["volatility_breakout_gate_signal"]
