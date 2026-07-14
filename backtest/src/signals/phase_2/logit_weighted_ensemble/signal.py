import math


def logit_weighted_ensemble_signal(
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
    tick_change,
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
            "source": "LOGIT_ENSEMBLE",
            "reason": "Time guard",
        }

    if std_v <= 0:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "LOGIT_ENSEMBLE",
            "reason": "Zero std_v",
        }

    v_sigma = v_t / std_v if std_v > 0 else 0.0

    z_feat = max(-3.0, min(3.0, z_score))
    v_feat = max(-3.0, min(3.0, v_sigma))
    spread_feat = max(-3.0, min(3.0, (spread - 0.05) * 100.0))
    a_feat = max(-3.0, min(3.0, a_t / (std_v * 2.0 + 1e-10)))

    z_w, v_w, s_w, a_w = 0.35, -0.25, -0.20, -0.20
    logit = z_w * z_feat + v_w * v_feat + s_w * spread_feat + a_w * a_feat

    prob = 1.0 / (1.0 + math.exp(-logit))

    if prob > 0.60 and yp <= 0.75:
        triggered = True
        direction = "YES"
        confidence = (prob - 0.50) * 2.0
        entry_price = yp
        reason = f"Ensemble prob={prob:.2f} (z={z_feat:.1f} v={v_feat:.1f} s={spread_feat:.1f})"
    elif prob < 0.40 and np_val <= 0.75:
        triggered = True
        direction = "NO"
        confidence = (0.50 - prob) * 2.0
        entry_price = np_val
        reason = f"Ensemble prob={prob:.2f} (z={z_feat:.1f} v={v_feat:.1f} s={spread_feat:.1f})"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "LOGIT_ENSEMBLE",
        "reason": reason,
    }


__all__ = ["logit_weighted_ensemble_signal"]
