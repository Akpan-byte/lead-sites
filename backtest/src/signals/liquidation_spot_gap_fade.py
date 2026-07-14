"""
LIQUIDATION_SPOT_GAP_FADE Signal Module
Extracted from /config/FINAL_GOLDEN_BOT.py run_strategy_signals()
"""

def liquidation_spot_gap_fade_signal(spot_price, strike, tick_change, rem_sec, yp, np_val) -> dict:
    """
    Returns signal dict with: 
    triggered (bool), direction (YES/NO), confidence (float), 
    signal_price (float), entry_price (float), source (str), reason (str)
    
    Trigger condition: abs(tick_change) >= 50.0 over 3s tick. 
    Direction = NO if up (tick_change > 0), YES if down (tick_change < 0). 
    Price <= 0.80.
    """
    triggered = False
    direction = None
    confidence = 0.0
    signal_price = 0.0
    entry_price = 0.0
    reason = ""

    # 1. Check trigger condition (Liquidation gap of $50+)
    if abs(tick_change) >= 50.0:
        # 2. Determine direction (Fade the move)
        # If spot moved UP (+), we bet NO (price will revert or stay below strike)
        # If spot moved DOWN (-), we bet YES (price will revert or stay above strike)
        dir_val = "NO" if tick_change > 0 else "YES"
        
        # 3. Get the market price for the chosen direction
        price = yp if dir_val == "YES" else np_val
        
        # 4. Apply price filter (Max 0.80)
        if price <= 0.80:
            triggered = True
            direction = dir_val
            # Statistical win rate from golden bot backtests: 0.616
            confidence = 0.616 
            signal_price = price
            entry_price = price
            reason = f"Liquidation gap of {tick_change:+.1f}$ detected. Fading {direction} at {price:.2f}"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": signal_price,
        "entry_price": entry_price,
        "source": "LIQUIDATION_SPOT_GAP_FADE",
        "reason": reason
    }
