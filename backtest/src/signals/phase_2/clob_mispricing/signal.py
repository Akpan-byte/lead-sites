def clob_mispricing_signal(
    spot_price, strike, yp, np_val, rem_sec, z_score, tf_hint="5m", **kwargs
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
            "source": "CLOB_MISPRICING",
            "reason": "Time guard",
        }

    price_sum = yp + np_val
    mispricing = price_sum - 1.0

    if abs(mispricing) < 0.03:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "CLOB_MISPRICING",
            "reason": f"No mispricing ({price_sum:.3f})",
        }

    if mispricing > 0.03 and np_val <= 0.80:
        triggered = True
        direction = "NO"
        confidence = min(0.75, mispricing * 5.0)
        entry_price = np_val
        reason = f"Both sides overpriced ({price_sum:.2f}), arbitraging NO"
    elif mispricing < -0.03 and yp <= 0.80:
        triggered = True
        direction = "YES"
        confidence = min(0.75, abs(mispricing) * 5.0)
        entry_price = yp
        reason = f"Both sides underpriced ({price_sum:.2f}), arbitraging YES"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "CLOB_MISPRICING",
        "reason": reason,
    }


__all__ = ["clob_mispricing_signal"]
