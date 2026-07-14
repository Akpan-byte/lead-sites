def kinetic_velocity_breakout_signal(spot_price, strike, v_t, std_v, a_t, rem_sec, yp, np_val) -> dict:
    """
    Returns signal dict with: triggered (bool), direction (YES/NO), confidence (float), 
    signal_price (float), entry_price (float), source (str), reason (str)
    
    Trigger condition: v_t > 2.0*std_v AND a_t > 1.5*std_v. 
    Direction = YES if up (v_t > 0), NO if down (v_t < 0). 
    Price <= 0.80.
    """
    triggered = False
    direction = None
    confidence = 0.0
    signal_price = 0.0
    entry_price = 0.0
    reason = ""

    # UP Trigger
    if v_t > 2.0 * std_v and a_t > 1.5 * std_v:
        if yp <= 0.80:
            triggered = True
            direction = "YES"
            signal_price = yp
            entry_price = yp
            confidence = 0.75 # High confidence for kinetic breakout
            reason = f"Kinetic velocity surge detected: v_t={v_t:.2f} (>2*std), a_t={a_t:.2f} (>1.5*std)"

    # DOWN Trigger
    elif v_t < -2.0 * std_v and a_t < -1.5 * std_v:
        if np_val <= 0.80:
            triggered = True
            direction = "NO"
            signal_price = np_val
            entry_price = np_val
            confidence = 0.75
            reason = f"Kinetic velocity plunge detected: v_t={v_t:.2f} (<-2*std), a_t={a_t:.2f} (<-1.5*std)"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": signal_price,
        "entry_price": entry_price,
        "source": "KINETIC_VELOCITY_BREAKOUT",
        "reason": reason
    }
