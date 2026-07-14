def ob_imbalance_fade_signal(
    spot_price, strike, yp, np_val, rem_sec, z_score, spread, tf_hint="5m", **kwargs
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
            "source": "OB_IMBALANCE_FADE",
            "reason": "Time guard",
        }

    total_price = yp + np_val
    if total_price <= 0:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "OB_IMBALANCE_FADE",
            "reason": "Zero total price",
        }

    yes_dominance = yp / total_price

    if yes_dominance > 0.65 and np_val <= 0.80:
        triggered = True
        direction = "NO"
        confidence = min(0.80, (yes_dominance - 0.50) * 3.0)
        entry_price = np_val
        reason = f"YES dominance {yes_dominance:.2f} fading to NO"
    elif (1.0 - yes_dominance) > 0.65 and yp <= 0.80:
        triggered = True
        direction = "YES"
        confidence = min(0.80, (1.0 - yes_dominance - 0.50) * 3.0)
        entry_price = yp
        reason = f"NO dominance {1.0 - yes_dominance:.2f} fading to YES"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "OB_IMBALANCE_FADE",
        "reason": reason,
    }


__all__ = ["ob_imbalance_fade_signal"]
