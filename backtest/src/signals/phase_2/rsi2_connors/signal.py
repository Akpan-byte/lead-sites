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

log = logging.getLogger("rsi2_connors")

# Global state for tracking bar ticks and completed bar histories per (asset, strike)
# key: (asset, strike) -> dict
_STATE = {}

# Binance spot kline intervals accepted by the API.
_BINANCE_INTERVAL_MAP = {1: "1m", 5: "5m", 15: "15m", 60: "1h", 240: "4h", 1440: "1d"}

# Assets Coinbase Advanced Trade supports in USD pairs for fallback.
_COINBASE_FALLBACK_ASSETS = {"BTC", "ETH", "SOL", "XRP"}


def calculate_rsi(prices, period=2):
    """Calculate Wilder's smoothed RSI(N) on close prices."""
    if len(prices) < period + 1:
        return 50.0

    gains = []
    losses = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))

    # Initial average gain/loss
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder's smoothing multiplier
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_sma(prices, period=200):
    """Calculate Simple Moving Average."""
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    return sum(prices[-period:]) / period


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
    """Fetch recent historical candles to bootstrap the 200 SMA.

    Primary source: Binance spot klines (asset/USDT).
    Fallback source: Coinbase ({asset}-USD) for BTC, ETH, SOL, XRP only.
    """
    try:
        closes = _fetch_binance_closes(asset, timeframe_min, limit=300)
        if closes:
            return closes
        # Fallback to Coinbase if Binance is unavailable/unsupported.
        closes = _fetch_coinbase_closes(asset, timeframe_min, limit=300)
        if closes:
            log.info(f"Connors RSI(2) falling back to Coinbase for {asset}")
            return closes
    except Exception as e:
        log.warning(f"Failed to pre-populate bars for {asset}: {e}")
    return []


def rsi2_connors_signal(spot_price, strike, rem_sec, yp, np_val, asset="BTC") -> dict:
    """
    Larry Connors RSI(2) Signal Generator.
    Fades short-term momentum extremes aligned with the long-term trend (200 SMA).

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

    # Infer timeframe (duration): if rem_sec starts > 300, it's 15m (900s), else 5m (300s)
    tf_min = 15 if rem_sec > 300 else 5

    if state_key not in _STATE:
        _STATE[state_key] = {
            "last_rem_sec": rem_sec,
            "current_bar_ticks": [spot_price],
            "bars": [],
            "initialized": False
        }

    state = _STATE[state_key]

    # On start, pre-populate bars in live/paper environments
    if not state["initialized"]:
        # Only try if we are running in real-time (not a high-speed chronological backtest)
        # Check system time compared to local variables is a good proxy, or simply execute safe try.
        closes = pre_populate_bars(asset, tf_min)
        if closes:
            state["bars"] = closes
            log.info(f"Connors RSI(2) successfully bootstrapped with {len(closes)} bars for {asset} strike {strike}")
        state["initialized"] = True

    # Check for candle rollover (rem_sec jumps up)
    if rem_sec > state["last_rem_sec"] + 10:
        if state["current_bar_ticks"]:
            close_price = state["current_bar_ticks"][-1]
            state["bars"].append(close_price)
            if len(state["bars"]) > 400:
                state["bars"].pop(0)
        state["current_bar_ticks"] = []

    state["current_bar_ticks"].append(spot_price)
    state["last_rem_sec"] = rem_sec

    # Temporary copy of bars including the ongoing tick close for indicator calculation
    bars = state["bars"] + ([state["current_bar_ticks"][-1]] if state["current_bar_ticks"] else [])

    triggered = False
    direction = None
    confidence = 0.0
    signal_price = 0.0
    reason = ""

    if len(bars) >= 200:
        sma_200 = calculate_sma(bars, 200)
        rsi_2 = calculate_rsi(bars, 2)
        curr_price = spot_price

        # Bull Trend: Spot > SMA200 and oversold (RSI2 < 10) -> Fade Down (Buy YES)
        if curr_price > sma_200:
            if rsi_2 < 10.0:
                if yp <= 0.80:
                    triggered = True
                    direction = "YES"
                    confidence = min(1.0, (10.0 - rsi_2) / 10.0)
                    signal_price = yp
                    reason = f"RSI(2)={rsi_2:.2f} < 10 in Bull Trend (Spot {curr_price:.2f} > SMA200 {sma_200:.2f})"
            else:
                reason = f"RSI(2)={rsi_2:.2f} not oversold (<10) in Bull Trend"
        # Bear Trend: Spot < SMA200 and overbought (RSI2 > 90) -> Fade Up (Buy NO)
        elif curr_price < sma_200:
            if rsi_2 > 90.0:
                if np_val <= 0.80:
                    triggered = True
                    direction = "NO"
                    confidence = min(1.0, (rsi_2 - 90.0) / 10.0)
                    signal_price = np_val
                    reason = f"RSI(2)={rsi_2:.2f} > 90 in Bear Trend (Spot {curr_price:.2f} < SMA200 {sma_200:.2f})"
            else:
                reason = f"RSI(2)={rsi_2:.2f} not overbought (>90) in Bear Trend"
        else:
            reason = f"Spot {curr_price:.2f} equals SMA200 {sma_200:.2f}"
    else:
        reason = f"Insufficient bar history: {len(bars)}/200"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": signal_price,
        "entry_price": signal_price,
        "source": "RSI2_CONNORS",
        "reason": reason
    }
