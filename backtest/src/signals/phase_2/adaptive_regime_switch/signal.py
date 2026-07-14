def adaptive_regime_switch_signal(
    spot_price,
    strike,
    yp,
    np_val,
    rem_sec,
    z_score,
    v_t,
    std_v,
    a_t,
    spread,
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
            "source": "ADAPTIVE_REGIME_SWITCH",
            "reason": "Time guard",
        }

    if std_v <= 0 or not velocity_history or len(velocity_history) < 30:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "ADAPTIVE_REGIME_SWITCH",
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
    vol_regime = recent_vol / (old_vol + 1e-10)

    pos_net = sum(1 for v in recent_15 if v > 0)
    neg_net = sum(1 for v in recent_15 if v < 0)
    trend_strength = (pos_net - neg_net) / 15.0

    v_sigma = v_t / std_v if std_v > 0 else 0.0

    mean_reversion_mode = vol_regime < 1.2 and abs(trend_strength) < 0.3
    momentum_mode = vol_regime > 1.5 or abs(trend_strength) > 0.4

    if mean_reversion_mode:
        if z_score > 1.5 and np_val <= 0.75:
            triggered = True
            direction = "NO"
            confidence = 0.65
            entry_price = np_val
            reason = f"MR mode (vol={vol_regime:.1f}), fading z={z_score:.1f}"
        elif z_score < -1.5 and yp <= 0.75:
            triggered = True
            direction = "YES"
            confidence = 0.65
            entry_price = yp
            reason = f"MR mode (vol={vol_regime:.1f}), fading z={z_score:.1f}"
    elif momentum_mode:
        if v_sigma > 1.5 and trend_strength > 0.2 and yp <= 0.70:
            triggered = True
            direction = "YES"
            confidence = 0.60
            entry_price = yp
            reason = f"Mom mode (vol={vol_regime:.1f}), trend={trend_strength:.2f}"
        elif v_sigma > 1.5 and trend_strength < -0.2 and np_val <= 0.70:
            triggered = True
            direction = "NO"
            confidence = 0.60
            entry_price = np_val
            reason = f"Mom mode (vol={vol_regime:.1f}), trend={trend_strength:.2f}"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "ADAPTIVE_REGIME_SWITCH",
        "reason": reason,
    }


__all__ = ["adaptive_regime_switch_signal"]
