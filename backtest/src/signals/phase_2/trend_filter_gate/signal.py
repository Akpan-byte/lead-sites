def trend_filter_gate_signal(
    spot_price,
    strike,
    yp,
    np_val,
    rem_sec,
    z_score,
    v_t,
    std_v,
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
            "source": "TREND_FILTER_GATE",
            "reason": "Time guard",
        }

    if std_v <= 0 or not velocity_history or len(velocity_history) < 30:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "TREND_FILTER_GATE",
            "reason": "Not enough data",
        }

    recent_30 = velocity_history[-30:]
    pos_net = sum(1 for v in recent_30 if v > 0)
    neg_net = sum(1 for v in recent_30 if v < 0)
    trend = (pos_net - neg_net) / 30.0

    uptrend = trend > 0.2
    downtrend = trend < -0.2
    v_sigma = abs(v_t) / std_v

    if uptrend and v_sigma > 1.5 and v_t > 0 and yp <= 0.75:
        triggered = True
        direction = "YES"
        confidence = min(0.80, trend + 0.5)
        entry_price = yp
        reason = f"Uptrend confirmed ({trend:.2f}), momentum YES"
    elif downtrend and v_sigma > 1.5 and v_t < 0 and np_val <= 0.75:
        triggered = True
        direction = "NO"
        confidence = min(0.80, abs(trend) + 0.5)
        entry_price = np_val
        reason = f"Downtrend confirmed ({trend:.2f}), momentum NO"

    if not uptrend and not downtrend:
        if z_score > 2.0 and np_val <= 0.75:
            triggered = True
            direction = "NO"
            confidence = 0.55
            entry_price = np_val
            reason = f"No trend, fading extreme z={z_score:.1f}"
        elif z_score < -2.0 and yp <= 0.75:
            triggered = True
            direction = "YES"
            confidence = 0.55
            entry_price = yp
            reason = f"No trend, fading extreme z={z_score:.1f}"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "TREND_FILTER_GATE",
        "reason": reason,
    }


__all__ = ["trend_filter_gate_signal"]
