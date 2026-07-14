# CHANGE_SUMMARY
# 2026-07-05  kilo
#   - Added optional `asset` parameter (defaults to "BTC") for multi-asset support.
#   - Keyed `_STATE` by `(asset, strike)` to prevent cross-asset pollution.
#   - Updated docstrings/comments from "BTC" to "asset" / "underlying".
# WHY: The system now trades BTC, ETH, SOL, BNB, XRP, HYPE; per-asset state must
#      be isolated even for signals that do not bootstrap historical bars.

import os
import time
import math
import logging

log = logging.getLogger("btc_up_coint")

# Global state tracking for log returns and tick history per (asset, strike)
# key: (asset, strike) -> dict
_STATE = {}


def normal_cdf(x):
    """Standard normal cumulative distribution function."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def update_volatility(state, spot_price):
    """Estimate rolling annualized volatility from 1-second tick log returns."""
    if "last_price" in state:
        prev = state["last_price"]
        if prev > 0 and spot_price > 0:
            log_ret = math.log(spot_price / prev)
            state["log_returns"].append(log_ret)
            if len(state["log_returns"]) > 1200: # rolling 20 minutes
                state["log_returns"].pop(0)
    state["last_price"] = spot_price

    # Calculate annualized standard deviation
    n = len(state["log_returns"])
    if n >= 30:
        mean_r = sum(state["log_returns"]) / n
        var_r = sum((r - mean_r)**2 for r in state["log_returns"]) / (n - 1)
        # Annualized volatility: std_dev_tick * sqrt(number of seconds in a year)
        vol = math.sqrt(var_r) * math.sqrt(31536000.0)
        # Bounded between 15% and 150% to prevent anomaly spikes
        return max(0.15, min(1.50, vol))
    return 0.45 # default fallback (45% annualized volatility)


def btc_up_coint_signal(spot_price, strike, rem_sec, yp, np_val, asset="BTC") -> dict:
    """
    Spot vs. Polymarket UP Cointegration Signal.
    Trades contract price deviation relative to its theoretical Black-Scholes binary option price.

    Inputs:
        spot_price: current spot asset price
        strike: the strike price of the binary contract
        rem_sec: remaining seconds in current candle/contract
        yp: YES best ask price (implied yes probability)
        np_val: NO best ask price (implied no probability)
        asset: underlying asset symbol (default "BTC" for backward compatibility)
    """
    global _STATE

    state_key = (asset, strike)

    if state_key not in _STATE:
        _STATE[state_key] = {
            "last_rem_sec": rem_sec,
            "log_returns": [],
            "last_price": spot_price
        }

    state = _STATE[state_key]

    # Detect candle rollover
    if rem_sec > state["last_rem_sec"] + 10:
        state["log_returns"] = []
        state["last_price"] = spot_price

    state["last_rem_sec"] = rem_sec

    # Update rolling volatility
    sigma = update_volatility(state, spot_price)

    # Black-Scholes Binary Option pricing parameters
    T = max(10.0, float(rem_sec)) / 31536000.0  # Time in years (capped at 10s min)

    triggered = False
    direction = None
    confidence = 0.0
    signal_price = 0.0
    reason = ""

    if spot_price > 0 and strike > 0 and sigma > 0:
        # Calculate d2: (ln(S/K) - 0.5 * sigma^2 * T) / (sigma * sqrt(T))
        try:
            ln_s_k = math.log(spot_price / strike)
            denom = sigma * math.sqrt(T)
            d2 = (ln_s_k - 0.5 * (sigma ** 2) * T) / denom

            # Theoretical probability (Black-Scholes binary call)
            theo_prob = normal_cdf(d2)

            # Live market YES price
            market_price = yp

            # Spread = Market Price - Theoretical Price
            spread = market_price - theo_prob
            threshold = 0.06 # 6% pricing discrepancy

            # Underpriced YES (Spread < -threshold) -> Buy YES
            if spread < -threshold:
                if yp <= 0.80:
                    triggered = True
                    direction = "YES"
                    confidence = min(1.0, abs(spread) / 0.20)
                    signal_price = yp
                    reason = f"Polymarket YES underpriced: spread {spread:+.3f} < {-threshold:.2f} (Theo {theo_prob:.3f}, Market {market_price:.3f}, Vol {sigma*100:.1f}%)"
            # Overpriced YES (Spread > threshold) -> Buy NO (underpriced NO)
            elif spread > threshold:
                if np_val <= 0.80:
                    triggered = True
                    direction = "NO"
                    confidence = min(1.0, abs(spread) / 0.20)
                    signal_price = np_val
                    reason = f"Polymarket YES overpriced: spread {spread:+.3f} > {threshold:.2f} (Theo {theo_prob:.3f}, Market {market_price:.3f}, Vol {sigma*100:.1f}%)"
            else:
                reason = f"Spread {spread:+.3f} is within threshold [{ -threshold :.2f}, { threshold :.2f}] (Theo {theo_prob:.3f}, Market {market_price:.3f})"
        except Exception as e:
            reason = f"Pricing error: {e}"
    else:
        reason = f"Invalid pricing inputs: spot {spot_price}, strike {strike}, vol {sigma}"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": signal_price,
        "entry_price": signal_price,
        "source": "BTC_UP_COINT",
        "reason": reason
    }
