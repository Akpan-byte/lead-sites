def l2_absorption_spread_collapse_signal(
    spot_price, strike, v_t, std_v, spread, rem_sec, yp, np_val
) -> dict:
    """
    Returns signal dict with: triggered (bool), direction (YES/NO), confidence (float),
    signal_price (float), entry_price (float), source (str), reason (str)

    Trigger condition: spread <= 0.04 AND abs(v_t) > 1.2*std_v.
    Direction = velocity sign. Price <= 0.80.
    """
    triggered = False
    direction = None
    confidence = 0.0
    entry_price = 0.0
    reason = ""

    # Guard against division by zero
    if std_v <= 0:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": spot_price,
            "entry_price": 0.0,
            "source": "L2_ABSORPTION_SPREAD_COLLAPSE",
            "reason": "std_v is zero or negative",
        }

    if spread <= 0.04 and abs(v_t) > 1.2 * std_v:
        direction = "YES" if v_t > 0 else "NO"
        entry_price = yp if direction == "YES" else np_val

        if entry_price <= 0.80:
            triggered = True
            # Confidence based on velocity surge magnitude
            confidence = min(0.95, abs(v_t) / (4.0 * std_v))
            reason = f"Spread collapse ({spread:.4f}) and velocity surge ({abs(v_t) / std_v:.2f} sigma)"
        else:
            reason = f"Condition met but price {entry_price:.2f} > 0.80"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": spot_price,
        "entry_price": entry_price,
        "source": "L2_ABSORPTION_SPREAD_COLLAPSE",
        "reason": reason,
    }
