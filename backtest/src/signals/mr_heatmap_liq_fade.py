# CHANGE_SUMMARY
# 2026-07-03  kilo
#   - Replaced the absolute $50 tick-change threshold with a relative move test
#     (>= 0.01% of strike) so it works across BTC price levels and shorter loops.
#   - Relaxed spread_val cap from 0.01 to 0.02 and raised entry-price cap from
#     0.80 to 0.85.
# WHY: mr_heatmap_liq_fade never traded because a $50 BTC spot change between
#      0.25s loop ticks almost never happened, and the spread cap was tighter
#      than typical Polymarket option spreads.


def mr_heatmap_liq_fade_signal(
    spot_price, strike, yp, np_val, rem_sec, tick_change, spread_val
) -> dict:
    signal = {
        "triggered": False,
        "direction": None,
        "confidence": 0.0,
        "signal_price": 0.0,
        "entry_price": 0.0,
        "source": "MR_HEATMAP_LIQ_FADE",
        "reason": "",
    }
    # Use a percentage-of-strike wick threshold so the signal scales with BTC price.
    move_pct = abs(tick_change) / strike if strike else 0.0
    if move_pct < 0.0001 or spread_val > 0.02:
        return signal
    direction = "NO" if tick_change > 0 else "YES"
    price = yp if direction == "YES" else np_val
    if price > 0.85:
        return signal
    signal["triggered"] = True
    signal["direction"] = direction
    signal["confidence"] = min(1.0, move_pct / 0.001)
    signal["signal_price"] = price
    signal["entry_price"] = price
    signal["reason"] = (
        f"Heatmap liquidation wick fade: tick_change={tick_change:+.1f} ({move_pct:.4%}), spread={spread_val:.3f}"
    )
    return signal
