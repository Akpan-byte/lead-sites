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

log = logging.getLogger("five_min_trend_breakthrough")

# Global state tracking for bars and status per (asset, strike)
# key: (asset, strike) -> dict
_STATE = {}

# Binance spot kline intervals accepted by the API.
_BINANCE_INTERVAL_MAP = {1: "1m", 5: "5m", 15: "15m", 60: "1h", 240: "4h", 1440: "1d"}

# Assets Coinbase Advanced Trade supports in USD pairs for fallback.
_COINBASE_FALLBACK_ASSETS = {"BTC", "ETH", "SOL", "XRP"}


def _fetch_binance_ohlcs(asset, timeframe_min, limit):
    """Fetch `limit` OHLC bars from Binance spot klines for asset/USDT."""
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
            # Binance indices: 2=high, 3=low, 4=close
            return [{"high": float(c[2]), "low": float(c[3]), "close": float(c[4])} for c in data]
    return []


def _fetch_coinbase_ohlcs(asset, timeframe_min, limit):
    """Fetch OHLC bars from Coinbase as a fallback for supported USD pairs."""
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
            # Coinbase indices: 2=high, 1=low, 4=close
            return [{"high": float(c[2]), "low": float(c[1]), "close": float(c[4])} for c in data]
    return []


def pre_populate_bars(asset="BTC", timeframe_min=5):
    """Bootstrap high, low, close bars for the given asset.

    Primary source: Binance spot klines (asset/USDT).
    Fallback source: Coinbase ({asset}-USD) for BTC, ETH, SOL, XRP only.
    """
    try:
        bars = _fetch_binance_ohlcs(asset, timeframe_min, limit=100)
        if bars:
            return bars
        bars = _fetch_coinbase_ohlcs(asset, timeframe_min, limit=100)
        if bars:
            log.info(f"Trend Breakthrough falling back to Coinbase for {asset}")
            return bars
    except Exception as e:
        log.warning(f"Failed to bootstrap bars for {asset}: {e}")
    return []


def calculate_ema(prices, period=50):
    """Calculate Exponential Moving Average."""
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    alpha = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * alpha + ema * (1.0 - alpha)
    return ema


def calculate_sma(values, period=21):
    """Calculate Simple Moving Average."""
    if len(values) < period:
        return values[-1] if values else 0.0
    return sum(values[-period:]) / period


def calculate_rsi(prices, period=14):
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

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_adx_di(bars, period=14):
    """Calculate Wilder's Average Directional Index (ADX) along with +DI and -DI."""
    if len(bars) < period * 2:
        return 0.0, 0.0, 0.0

    tr_list = []
    dm_plus = []
    dm_minus = []

    for i in range(1, len(bars)):
        b = bars[i]
        prev = bars[i-1]

        tr = max(b["high"] - b["low"], abs(b["high"] - prev["close"]), abs(b["low"] - prev["close"]))
        tr_list.append(tr)

        up_move = b["high"] - prev["high"]
        down_move = prev["low"] - b["low"]

        if up_move > 0 and up_move > down_move:
            dm_plus.append(up_move)
        else:
            dm_plus.append(0.0)

        if down_move > 0 and down_move > up_move:
            dm_minus.append(down_move)
        else:
            dm_minus.append(0.0)

    smoothed_tr = sum(tr_list[:period])
    smoothed_dm_plus = sum(dm_plus[:period])
    smoothed_dm_minus = sum(dm_minus[:period])

    dx_list = []

    di_plus = 100.0 * (smoothed_dm_plus / smoothed_tr) if smoothed_tr > 0 else 0.0
    di_minus = 100.0 * (smoothed_dm_minus / smoothed_tr) if smoothed_tr > 0 else 0.0
    dx = 100.0 * abs(di_plus - di_minus) / (di_plus + di_minus) if (di_plus + di_minus) > 0 else 0.0
    dx_list.append(dx)

    curr_di_plus = di_plus
    curr_di_minus = di_minus

    for i in range(period, len(tr_list)):
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr_list[i]
        smoothed_dm_plus = smoothed_dm_plus - (smoothed_dm_plus / period) + dm_plus[i]
        smoothed_dm_minus = smoothed_dm_minus - (smoothed_dm_minus / period) + dm_minus[i]

        curr_di_plus = 100.0 * (smoothed_dm_plus / smoothed_tr) if smoothed_tr > 0 else 0.0
        curr_di_minus = 100.0 * (smoothed_dm_minus / smoothed_tr) if smoothed_tr > 0 else 0.0

        dx = 100.0 * abs(curr_di_plus - curr_di_minus) / (curr_di_plus + curr_di_minus) if (curr_di_plus + curr_di_minus) > 0 else 0.0
        dx_list.append(dx)

    if len(dx_list) < period:
        return 0.0, curr_di_plus, curr_di_minus

    adx = sum(dx_list[:period]) / period
    for i in range(period, len(dx_list)):
        adx = (adx * (period - 1) + dx_list[i]) / period

    return adx, curr_di_plus, curr_di_minus


def five_min_trend_breakthrough_signal(spot_price, strike, rem_sec, yp, np_val, asset="BTC") -> dict:
    """
    Five-Minute Trend Breakthrough Signal.
    Enters trend breakout setups with high momentum and trend confirmation.

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
            log.info(f"Trend Breakthrough successfully bootstrapped with {len(closes)} bars for {asset} strike {strike}")
        state["initialized"] = True

    # Check for candle rollover
    if rem_sec > state["last_rem_sec"] + 10:
        if state["current_bar_ticks"]:
            high = max(state["current_bar_ticks"])
            low = min(state["current_bar_ticks"])
            close = state["current_bar_ticks"][-1]
            state["bars"].append({"high": high, "low": low, "close": close})
            if len(state["bars"]) > 200:
                state["bars"].pop(0)
        state["current_bar_ticks"] = []

    state["current_bar_ticks"].append(spot_price)
    state["last_rem_sec"] = rem_sec

    # Construct transient copy of bars including current tick
    bars = list(state["bars"])
    if state["current_bar_ticks"]:
        curr_high = max(state["current_bar_ticks"])
        curr_low = min(state["current_bar_ticks"])
        curr_close = spot_price
        bars.append({"high": curr_high, "low": curr_low, "close": curr_close})

    triggered = False
    direction = None
    confidence = 0.0
    signal_price = 0.0
    reason = ""

    # Needs at least 50 bars to calculate 50 EMA and 28 bars for ADX
    if len(bars) >= 50:
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]

        curr_close = spot_price

        # Calculate Channel bounds (SMA 21 High and Low)
        sma_21_high = calculate_sma(highs, 21)
        sma_21_low = calculate_sma(lows, 21)

        # Calculate Trend Filter (EMA 50)
        ema_50 = calculate_ema(closes, 50)

        # Calculate Momentum (RSI 14)
        rsi_14 = calculate_rsi(closes, 14)

        # Calculate Trend Strength (ADX 14)
        adx_14, di_plus, di_minus = calculate_adx_di(bars, 14)

        # Bull Breakthrough: spot > SMA21(High), spot > EMA50, RSI > 60, ADX > 25, +DI > -DI
        if curr_close > sma_21_high and curr_close > ema_50:
            if rsi_14 > 60.0:
                if adx_14 > 25.0 and di_plus > di_minus:
                    if yp <= 0.80:
                        triggered = True
                        direction = "YES"
                        confidence = min(1.0, (rsi_14 - 60.0) / 20.0 + (adx_14 - 25.0) / 50.0)
                        signal_price = yp
                        reason = f"Bull breakthrough triggered: spot {curr_close:.2f} > high_sma {sma_21_high:.2f}, RSI(14)={rsi_14:.2f} > 60, ADX(14)={adx_14:.2f} > 25 (+DI > -DI)"
                else:
                    reason = f"Trend strength ADX(14)={adx_14:.2f} too weak (<25) or directional bias negative (+DI {di_plus:.1f} <= -DI {di_minus:.1f})"
            else:
                reason = f"RSI(14)={rsi_14:.2f} is not above 60"
        # Bear Breakthrough: spot < SMA21(Low), spot < EMA50, RSI < 40, ADX > 25, -DI > +DI
        elif curr_close < sma_21_low and curr_close < ema_50:
            if rsi_14 < 40.0:
                if adx_14 > 25.0 and di_minus > di_plus:
                    if np_val <= 0.80:
                        triggered = True
                        direction = "NO"
                        confidence = min(1.0, (40.0 - rsi_14) / 20.0 + (adx_14 - 25.0) / 50.0)
                        signal_price = np_val
                        reason = f"Bear breakthrough triggered: spot {curr_close:.2f} < low_sma {sma_21_low:.2f}, RSI(14)={rsi_14:.2f} < 40, ADX(14)={adx_14:.2f} > 25 (-DI > +DI)"
                else:
                    reason = f"Trend strength ADX(14)={adx_14:.2f} too weak (<25) or directional bias negative (-DI {di_minus:.1f} <= +DI {di_plus:.1f})"
            else:
                reason = f"RSI(14)={rsi_14:.2f} is not below 40"
        else:
            reason = f"Spot {curr_close:.2f} inside Channel [{sma_21_low:.2f}, {sma_21_high:.2f}] or not aligned with EMA50 {ema_50:.2f}"
    else:
        reason = f"Insufficient bar history: {len(bars)}/50"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": signal_price,
        "entry_price": signal_price,
        "source": "FIVE_MIN_TREND_BREAKTHROUGH",
        "reason": reason
    }
