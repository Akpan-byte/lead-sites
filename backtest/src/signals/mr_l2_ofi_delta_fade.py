def mr_l2_ofi_delta_fade_signal(
    spot_price,
    strike,
    v_t,
    std_v,
    spread,
    rem_sec,
    yp,
    np_val,
) -> dict:
    """
    Returns signal dict with: triggered (bool), direction (YES/NO), confidence (float),
    signal_price (float), entry_price (float), source (str), reason (str)

    Trigger: v_t > 1.5*std_v AND spread > 0.03 (weak bid) -> fade to NO.
    OR v_t < -1.5*std_v AND spread > 0.03 (weak ask) -> fade to YES.
    Price <= 0.80.
    """
    triggered = False
    direction = None
    confidence = 0.0
    signal_price = 0.0
    entry_price = 0.0
    reason = ""

    # Guard against division by zero
    if std_v <= 0:
        return {
            "triggered": False,
            "direction": None,
            "confidence": 0.0,
            "signal_price": 0.0,
            "entry_price": 0.0,
            "source": "MR_L2_OFI_DELTA_FADE",
            "reason": "std_v is zero or negative",
        }

    # v_t > 1.5*std_v AND spread > 0.03 (weak bid) -> fade to NO
    if v_t > 1.5 * std_v and spread > 0.03:
        if np_val <= 0.80:
            triggered = True
            direction = "NO"
            confidence = 0.65  # Base confidence for OFI fade
            signal_price = np_val
            entry_price = np_val
            reason = f"Spot wicking up ({v_t / std_v:.2f} sigma) but bid support faded (spread={spread:.3f})"
        else:
            reason = f"Condition met but NO price {np_val:.2f} > 0.80"

    # v_t < -1.5*std_v AND spread > 0.03 (weak ask) -> fade to YES
    elif v_t < -1.5 * std_v and spread > 0.03:
        if yp <= 0.80:
            triggered = True
            direction = "YES"
            confidence = 0.65
            signal_price = yp
            entry_price = yp
            reason = f"Spot wicking down ({v_t / std_v:.2f} sigma) but ask resistance faded (spread={spread:.3f})"
        else:
            reason = f"Condition met but YES price {yp:.2f} > 0.80"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": signal_price,
        "entry_price": entry_price,
        "source": "MR_L2_OFI_DELTA_FADE",
        "reason": reason,
    }
