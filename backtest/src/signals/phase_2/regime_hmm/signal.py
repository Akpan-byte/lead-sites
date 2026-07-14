def regime_hmm_signal(
    spot_price,
    strike,
    yp,
    np_val,
    rem_sec,
    z_score,
    v_t,
    std_v,
    a_t,
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
            "source": "REGIME_HMM",
            "reason": "Time guard",
        }

    if std_v <= 0 or not velocity_history or len(velocity_history) < 30:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "REGIME_HMM",
            "reason": "Not enough data",
        }

    recent_20 = velocity_history[-20:]
    older_20 = (
        velocity_history[-40:-20]
        if len(velocity_history) >= 40
        else velocity_history[-20:]
    )

    recent_var = sum(v * v for v in recent_20) / 20
    older_var = sum(v * v for v in older_20) / max(len(older_20), 1)
    vol_ratio = recent_var / (older_var + 1e-10)

    pos_count = sum(1 for v in recent_20 if v > 0)
    neg_count = sum(1 for v in recent_20 if v < 0)
    trend_strength = (pos_count - neg_count) / 20.0

    hi_vol_regime = vol_ratio > 1.5
    lo_vol_regime = vol_ratio < 0.7

    if lo_vol_regime:
        if z_score > 1.5 and np_val <= 0.75:
            triggered = True
            direction = "NO"
            confidence = 0.65
            entry_price = np_val
            reason = f"Low-vol regime ({vol_ratio:.2f}), fading z={z_score:.1f}"
        elif z_score < -1.5 and yp <= 0.75:
            triggered = True
            direction = "YES"
            confidence = 0.65
            entry_price = yp
            reason = f"Low-vol regime ({vol_ratio:.2f}), fading z={z_score:.1f}"
    elif hi_vol_regime:
        if v_t > 1.5 * std_v and trend_strength > 0.3 and yp <= 0.70:
            triggered = True
            direction = "YES"
            confidence = 0.60
            entry_price = yp
            reason = f"High-vol regime ({vol_ratio:.2f}), momentum YES"
        elif v_t < -1.5 * std_v and trend_strength < -0.3 and np_val <= 0.70:
            triggered = True
            direction = "NO"
            confidence = 0.60
            entry_price = np_val
            reason = f"High-vol regime ({vol_ratio:.2f}), momentum NO"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "REGIME_HMM",
        "reason": reason,
    }


__all__ = ["regime_hmm_signal"]
