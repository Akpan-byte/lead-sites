import math

def mr_gamma_expiry_pin_signal(spot_price, strike, z_score, z_dist, rem_sec, yp, np_val) -> dict:
    """
    Returns signal dict for MR_GAMMA_EXPIRY_PIN strategy.
    
    Trigger condition: 20 <= rem_sec <= 90 AND 0.4 <= z_dist <= 1.0.
    Fade direction away from strike. Price <= 0.80.
    """
    triggered = False
    direction = None
    confidence = 0.0
    signal_price = 0.0
    entry_price = 0.0
    source = "MR_GAMMA_EXPIRY_PIN"
    reason = ""

    if 20 <= rem_sec <= 90:
        if 0.4 <= z_dist <= 1.0:
            # Fade direction away from strike:
            # If spot is above strike, bet NO (it will be <= strike).
            # If spot is below strike, bet YES (it will be > strike).
            direction = "NO" if spot_price > strike else "YES"
            
            price = yp if direction == "YES" else np_val
            
            # Price constraint
            if price <= 0.80:
                triggered = True
                signal_price = price
                entry_price = price
                confidence = 1.0
                reason = f"🛡️ [GAMMA PIN] Near expiry ({rem_sec}s remaining). Fading to {direction} at z={z_dist:.2f}..."

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": signal_price,
        "entry_price": entry_price,
        "source": source,
        "reason": reason
    }
