# CHANGE_SUMMARY
# 2026-07-05  kilo
#   - Added optional `asset` parameter (defaults to "BTC") for multi-asset support.
#   - Replaced hard-coded Coinbase BTC-USD bootstrap with Binance klines
#     (symbol={ASSET}USDT) and a Coinbase fallback for BTC/ETH/SOL/XRP.
#   - Keyed `_STATE` by `(asset, strike)` to prevent cross-asset pollution.
#   - Updated docstrings/comments from "BTC" to "asset".
# WHY: The system now trades BTC, ETH, SOL, BNB, XRP, HYPE; signals must fetch
#      the correct asset's candles and keep per-asset state isolated.

import os
import time
import math
import requests
import logging

log = logging.getLogger("ou_zscore_mr")

# Global state tracking for price bar history per (asset, strike)
# key: (asset, strike) -> dict
_STATE = {}

# Binance spot kline intervals accepted by the API.
_BINANCE_INTERVAL_MAP = {1: "1m", 5: "5m", 15: "15m", 60: "1h", 240: "4h", 1440: "1d"}

# Assets Coinbase Advanced Trade supports in USD pairs for fallback.
_COINBASE_FALLBACK_ASSETS = {"BTC", "ETH", "SOL", "XRP"}


def _fetch_binance_closes(asset, timeframe_min, limit):
    """Fetch `limit` closes from Binance spot klines for asset/USDT."""
    interval = _BINANCE_INTERVAL_MAP.get(timeframe_min, "5m")
    url = (
        "https://api.binance.com/api/v3/klines"
        f"?symbol={asset}USDT&interval={interval}&limit={limit}"
    )
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
    if r.status_code == 200:
        data = r.json()
        if isinstance(data, list) and len(data) > 0:
            data = sorted(data, key=lambda x: x[0])
            return [float(candle[4]) for candle in data]
    return []


def _fetch_coinbase_closes(asset, timeframe_min, limit):
    """Fetch closes from Coinbase as a fallback for supported USD pairs."""
    if asset not in _COINBASE_FALLBACK_ASSETS:
        return []
    granularity = timeframe_min * 60
    url = (
        "https://api.exchange.coinbase.com/products/"
        f"{asset}-USD/candles?granularity={granularity}&limit={limit}"
    )
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
    if r.status_code == 200:
        data = r.json()
        if isinstance(data, list) and len(data) > 0:
            data = sorted(data, key=lambda x: x[0])
            return [float(candle[4]) for candle in data]
    return []


def pre_populate_bars(asset="BTC", timeframe_min=5):
    """Bootstrap historical close prices for the given asset.

    Primary source: Binance spot klines (asset/USDT).
    Fallback source: Coinbase ({asset}-USD) for BTC, ETH, SOL, XRP only.
    """
    try:
        closes = _fetch_binance_closes(asset, timeframe_min, limit=100)
        if closes:
            return closes
        closes = _fetch_coinbase_closes(asset, timeframe_min, limit=100)
        if closes:
            log.info(f"OU Z-Score falling back to Coinbase for {asset}")
            return closes
    except Exception as e:
        log.warning(f"Failed to bootstrap bars for {asset}: {e}")
    return []


def calibrate_ou_parameters(closes, strike, lookback=40):
    """
    Calibrate Ornstein-Uhlenbeck parameters using OLS on price deviations.
    Returns: (a, b, var_eps, theta, mu, std_stationary)
    """
    # Calculate deviations from strike
    x = [c - strike for c in closes[-lookback:]]

    # Lagged deviation series
    X = x[:-1]
    Y = x[1:]

    N = len(X)
    mean_X = sum(X) / N
    mean_Y = sum(Y) / N

    cov_XY = sum((X[i] - mean_X) * (Y[i] - mean_Y) for i in range(N))
    var_X = sum((X[i] - mean_X)**2 for i in range(N))

    if var_X == 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 1.0

    # OLS coefficients: Y = a*X + b + eps
    a = cov_XY / var_X
    b = mean_Y - a * mean_X

    # Residual Variance (var_eps)
    residuals_sum_sq = sum((Y[i] - (a * X[i] + b))**2 for i in range(N))
    var_eps = residuals_sum_sq / (N - 2) if N > 2 else 1e-10

    # Solve for continuous parameters if process is stationary (0 < a < 1)
    if 0.0 < a < 0.999:
        theta = -math.log(a)
        mu = b / (1.0 - a)
        std_stationary = math.sqrt(var_eps / (1.0 - a**2))
        return a, b, var_eps, theta, mu, std_stationary

    return a, b, var_eps, 0.0, 0.0, 1.0


def ou_zscore_mr_signal(spot_price, strike, rem_sec, yp, np_val, asset="BTC") -> dict:
    """
    Ornstein-Uhlenbeck Z-Score Mean Reversion Signal.
    Calibrates a mean-reverting OU process to deviations from strike and trades statistical extremes.

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

    tf_min = 15 if rem_sec > 300 else 5
    lookback = 40

    if state_key not in _STATE:
        _STATE[state_key] = {
            "last_rem_sec": rem_sec,
            "current_bar_ticks": [spot_price],
            "bars": [],
            "initialized": False
        }

    state = _STATE[state_key]

    if not state["initialized"]:
        closes = pre_populate_bars(asset, tf_min)
        if closes:
            state["bars"] = closes
            log.info(f"OU Z-Score successfully bootstrapped with {len(closes)} bars for {asset} strike {strike}")
        state["initialized"] = True

    # Check for candle rollover
    if rem_sec > state["last_rem_sec"] + 10:
        if state["current_bar_ticks"]:
            close = state["current_bar_ticks"][-1]
            state["bars"].append(close)
            if len(state["bars"]) > 200:
                state["bars"].pop(0)
        state["current_bar_ticks"] = []

    state["current_bar_ticks"].append(spot_price)
    state["last_rem_sec"] = rem_sec

    # Construct transient copy of bars including current tick
    bars = list(state["bars"])
    if state["current_bar_ticks"]:
        bars.append(spot_price)

    triggered = False
    direction = None
    confidence = 0.0
    signal_price = 0.0
    reason = ""

    # Need at least lookback + 5 bars for reliable calibration
    if len(bars) >= lookback + 5:
        a, b, var_eps, theta, mu, std_stationary = calibrate_ou_parameters(bars, strike, lookback)

        if 0.0 < a < 0.995:
            # Current price deviation
            x_curr = spot_price - strike

            # Compute standardized Z-score
            z_score = (x_curr - mu) / std_stationary

            # Z < -2.0 -> Underpriced (Fade Down) -> Buy YES
            if z_score < -2.0:
                if yp <= 0.80:
                    triggered = True
                    direction = "YES"
                    confidence = min(1.0, abs(z_score) / 4.0)
                    signal_price = yp
                    reason = f"OU Mean Reversion YES: Z-score {z_score:.2f} < -2.0 (a={a:.3f}, theta={theta:.3f}, mu={mu:+.2f}, std_stat={std_stationary:.2f})"
            # Z > 2.0 -> Overpriced (Fade Up) -> Buy NO
            elif z_score > 2.0:
                if np_val <= 0.80:
                    triggered = True
                    direction = "NO"
                    confidence = min(1.0, abs(z_score) / 4.0)
                    signal_price = np_val
                    reason = f"OU Mean Reversion NO: Z-score {z_score:.2f} > 2.0 (a={a:.3f}, theta={theta:.3f}, mu={mu:+.2f}, std_stat={std_stationary:.2f})"
            else:
                reason = f"Z-score {z_score:.2f} is inside threshold [-2.0, 2.0] (a={a:.3f}, theta={theta:.3f}, mu={mu:+.2f})"
        else:
            reason = f"Process is not mean-reverting: OLS coefficient a={a:.3f} is not in (0.0, 0.995)"
    else:
        reason = f"Insufficient bar history: {len(bars)}/{lookback + 5}"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": signal_price,
        "entry_price": signal_price,
        "source": "OU_ZSCORE_MR",
        "reason": reason
    }
