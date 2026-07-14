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

log = logging.getLogger("bbkc_squeeze")

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
            log.info(f"BBKC Squeeze falling back to Coinbase for {asset}")
            return bars
    except Exception as e:
        log.warning(f"Failed to bootstrap bars for {asset}: {e}")

    return []


def bbkc_squeeze_signal(spot_price, strike, rem_sec, yp, np_val, asset="BTC") -> dict:
    """
    Bollinger-Keltner Squeeze Breakout Signal.
    Enters breakouts after periods of compressed volatility (squeeze).

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
            log.info(f"BBKC Squeeze successfully bootstrapped with {len(closes)} bars for {asset} strike {strike}")
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

    # Need at least 25 bars to calculate 20-period indicators and check historical squeeze
    if len(bars) >= 25:
        # Calculate Bollinger and Keltner stats for the last 5 bars to check for recent squeeze
        was_squeezed = False

        # Check standard indicators on the current state (index -1)
        closes = [b["close"] for b in bars]

        # Calculate Bollinger Bands
        basis = sum(closes[-20:]) / 20
        var = sum((c - basis)**2 for c in closes[-20:]) / 19
        std_dev = math.sqrt(var) if var > 0 else 1e-10
        upper_bb = basis + 2.0 * std_dev
        lower_bb = basis - 2.0 * std_dev

        # Check squeeze state for the last 5 bars
        for idx in range(-5, 0):
            # BB basis
            sub_closes = closes[idx-20 : idx if idx < -1 else None]
            if len(sub_closes) < 20:
                continue
            sub_basis = sum(sub_closes) / 20
            sub_var = sum((c - sub_basis)**2 for c in sub_closes) / 19
            sub_std = math.sqrt(sub_var) if sub_var > 0 else 1e-10

            # Keltner ATR
            tr_list = []
            for i in range(len(bars) + idx - 20, len(bars) + idx):
                if i < 1:
                    continue
                b = bars[i]
                prev_b = bars[i-1]
                tr = max(b["high"] - b["low"], abs(b["high"] - prev_b["close"]), abs(b["low"] - prev_b["close"]))
                tr_list.append(tr)

            sub_atr = sum(tr_list) / len(tr_list) if tr_list else 1e-10

            # Squeeze definition: Bollinger Bands contract inside Keltner Channels
            if sub_std < 0.75 * sub_atr:
                was_squeezed = True
                break

        if was_squeezed:
            if spot_price >= upper_bb:
                if yp <= 0.80:
                    triggered = True
                    direction = "YES"
                    confidence = 1.0
                    signal_price = yp
                    reason = f"Volatility expansion breakout YES: spot {spot_price:.2f} >= upper_bb {upper_bb:.2f} (exiting squeeze)"
            elif spot_price <= lower_bb:
                if np_val <= 0.80:
                    triggered = True
                    direction = "NO"
                    confidence = 1.0
                    signal_price = np_val
                    reason = f"Volatility expansion breakout NO: spot {spot_price:.2f} <= lower_bb {lower_bb:.2f} (exiting squeeze)"
            else:
                reason = f"Price inside Bollinger Bands [{lower_bb:.2f}, {upper_bb:.2f}] (Squeeze active)"
        else:
            reason = f"No volatility compression (squeeze) detected in the last 5 bars"
    else:
        reason = f"Insufficient bar history: {len(bars)}/25"

    return {
        "triggered": triggered,
        "direction": direction,
        "confidence": confidence,
        "signal_price": signal_price,
        "entry_price": signal_price,
        "source": "BBKC_SQUEEZE",
        "reason": reason
    }
