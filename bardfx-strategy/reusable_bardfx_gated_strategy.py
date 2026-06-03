#!/usr/bin/env python3
"""
Bard FX Gated Range Breakout Strategy Class
===========================================
An institutional-grade, out-of-sample quantitative implementation of the Bard FX 
Wickless Open breakout strategy (1:3 RR), with support for:
1. Standard Mode (EMA-50 Trend Alignment + Wickless Candle Setup + Limit-Order Tap)
2. Confluence Gated Mode (Standard Rules + Value Area VAH/VAL Volume Profile Filtering)

Look-Ahead Bias Immunity Audit:
--------------------------------
The strategy is 100% immune to look-ahead bias. The technical setup (wickless open,
EMA alignment, VAH/VAL) is scanned and confirmed ONLY at the close of candle T. 
The limit order is placed at the open of candle T (O_T) to be tapped strictly on subsequent 
candles (T+1 to T+4) using 1-minute tick (high/low) data. All indicators are shifted 
by 1 full block to prevent future leakage.

Rules of the Gated Strategy:
----------------------------
1. Setup Identification (Candle T - 15-Minute Structural Bar):
   - Bullish: Candle close > 50 EMA AND candle has no bottom wick (open == low within epsilon).
   - Bearish: Candle close < 50 EMA AND candle has no top wick (open == high within epsilon).
2. Confluence Filter (If enabled):
   - Bullish: Setup candle open must be strictly greater than VAH (Value Area High).
   - Bearish: Setup candle open must be strictly less than VAL (Value Area Low).
3. Limit Entry Placement (Decoupled Bracket):
   - Bullish: Place Buy Limit at O_T. Stop Loss = Recent 10-period Swing Low - SL Buffer.
   - Bearish: Place Sell Limit at O_T. Stop Loss = Recent 10-period Swing High + SL Buffer.
   - Limit order is active for 4 bars (1 hour). If unfilled, it is cancelled.
4. Profit Target (TP):
   - Strict 1:3 Reward-to-Risk ratio relative to stop distance: TP = Entry + (3 * Risk).
"""

import math
import numpy as np
import pandas as pd

class BardFXGatedStrategy:
    def __init__(self, with_confluence=True, rr=3.0, slippage=0.0001, sl_buffer=0.0002, min_risk=0.0005, tick_size=0.01):
        """
        Initializes the Bard FX Strategy Class.
        
        Parameters:
        -----------
        with_confluence : bool
            If True, gates entries strictly outside the Volume Profile Value Area (VAH/VAL).
        rr : float
            Risk-to-Reward ratio (default is 1:3).
        slippage : float
            Realistic transaction cost / spread friction.
        sl_buffer : float
            Buffer added to the swing low/high stop loss level (in raw price points).
        min_risk : float
            Minimum stop loss distance to filter out tiny noise ranges.
        tick_size : float
            Minimum tick movement (used for Volume Profile price binning).
        """
        self.with_confluence = with_confluence
        self.rr = rr
        self.slippage = slippage
        self.sl_buffer = sl_buffer
        self.min_risk = min_risk
        self.tick_size = tick_size
        
    def compute_volume_profile(self, opens, highs, lows, volumes):
        """
        Computes the Value Area High (VAH) and Value Area Low (VAL) for a given window.
        Uses the industry-standard 70% volume distribution model.
        """
        bins = {}
        total_volume = 0.0
        
        # Aggregate volume into price bins
        for o, h, l, v in zip(opens, highs, lows, volumes):
            curr = math.floor(l / self.tick_size) * self.tick_size
            levels = []
            while curr <= math.ceil(h / self.tick_size) * self.tick_size:
                levels.append(round(curr, 5))
                curr += self.tick_size
                
            if not levels:
                continue
                
            vol_per_level = v / len(levels)
            for lev in levels:
                bins[lev] = bins.get(lev, 0.0) + vol_per_level
                total_volume += vol_per_level
                
        if not bins or total_volume == 0:
            return min(lows), max(highs)
            
        poc = max(bins, key=bins.get)
        sorted_bins = sorted(bins.items(), key=lambda x: x[0])
        prices = [x[0] for x in sorted_bins]
        vols = [x[1] for x in sorted_bins]
        
        target_vol = total_volume * 0.70
        current_vol = bins[poc]
        
        poc_idx = prices.index(poc)
        low_idx = poc_idx
        high_idx = poc_idx
        
        # Expand out from POC until 70% of total volume is captured
        while current_vol < target_vol:
            prev_vol = vols[low_idx - 1] if low_idx > 0 else 0.0
            next_vol = vols[high_idx + 1] if high_idx < len(prices) - 1 else 0.0
            
            if prev_vol == 0.0 and next_vol == 0.0:
                break
                
            if prev_vol >= next_vol:
                low_idx -= 1
                current_vol += prev_vol
            else:
                high_idx += 1
                current_vol += next_vol
                
        val = prices[low_idx]
        vah = prices[high_idx]
        
        return val, vah

    def generate_signals_15m(self, df_15):
        """
        Pre-calculates 15-minute indicators out-of-sample and shifts them
        to prevent look-ahead bias in the 1-minute execution engine.
        """
        # Calculate standard 15-minute indicator indicators
        df_15['ema50'] = df_15['close'].ewm(span=50, adjust=False).mean()
        df_15['swing_low'] = df_15['low'].rolling(10).min()
        df_15['swing_high'] = df_15['high'].rolling(10).max()
        
        # Calculate rolling Volume Profile
        vals = []
        vahs = []
        opens_15 = df_15['open'].values
        highs_15 = df_15['high'].values
        lows_15 = df_15['low'].values
        vols_15 = df_15['volume'].values
        
        lookback = 10
        for idx in range(len(df_15)):
            if idx < lookback:
                vals.append(np.nan)
                vahs.append(np.nan)
            else:
                w_open = opens_15[idx - lookback : idx]
                w_high = highs_15[idx - lookback : idx]
                w_low = lows_15[idx - lookback : idx]
                w_vol = vols_15[idx - lookback : idx]
                val, vah = self.compute_volume_profile(w_open, w_high, w_low, w_vol)
                vals.append(val)
                vahs.append(vah)
                
        df_15['val'] = vals
        df_15['vah'] = vahs
        
        # Shift indicators by 1 bar to strictly immunize against look-ahead bias
        # This guarantees that at the start of minute bar T, we only use completely formed T-1 indicators.
        shifted_cols = {
            'prev_open_15': df_15['open'].shift(1),
            'prev_high_15': df_15['high'].shift(1),
            'prev_low_15': df_15['low'].shift(1),
            'prev_close_15': df_15['close'].shift(1),
            'prev_ema50_15': df_15['ema50'].shift(1),
            'prev_swing_low_15': df_15['swing_low'].shift(1),
            'prev_swing_high_15': df_15['swing_high'].shift(1),
            'prev_val_15': df_15['val'].shift(1),
            'prev_vah_15': df_15['vah'].shift(1)
        }
        for col_name, col_data in shifted_cols.items():
            df_15[col_name] = col_data
            
        return df_15

    def backtest(self, df_1m, df_15):
        """
        Runs a high-fidelity out-of-sample backtest of the strategy.
        Loads 15-minute indicators, merges them back into the 1-minute execution stream,
        and walks forward tick-by-tick with Look-Ahead Immunity.
        """
        print("Pre-processing indicators...")
        df_15_processed = self.generate_signals_15m(df_15.copy())
        
        # Merge 15-minute indicators back to 1-minute bars
        print("Merging resampled blocks back to 1-minute execution stream...")
        df_1m['timestamp_15'] = df_1m['timestamp'].dt.floor('15min')
        df_merged = pd.merge(df_1m, df_15_processed[[
            'timestamp_15', 
            'prev_open_15', 'prev_high_15', 'prev_low_15', 'prev_close_15',
            'prev_ema50_15', 'prev_swing_low_15', 'prev_swing_high_15',
            'prev_val_15', 'prev_vah_15'
        ]], on='timestamp_15', how='left')
        
        # Setup run parameters
        opens = df_merged['open'].values
        highs = df_merged['high'].values
        lows = df_merged['low'].values
        closes = df_merged['close'].values
        
        prev_opens_15 = df_merged['prev_open_15'].values
        prev_highs_15 = df_merged['prev_high_15'].values
        prev_lows_15 = df_merged['prev_low_15'].values
        prev_closes_15 = df_merged['prev_close_15'].values
        prev_ema50s_15 = df_merged['prev_ema50_15'].values
        prev_swing_lows_15 = df_merged['prev_swing_low_15'].values
        prev_swing_highs_15 = df_merged['prev_swing_high_15'].values
        prev_vals_15 = df_merged['prev_val_15'].values
        prev_vahs_15 = df_merged['prev_vah_15'].values
        
        # Pre-calculate New York timezone components
        df_merged['timestamp_ny'] = df_merged['timestamp'].dt.tz_convert('America/New_York')
        minutes = df_merged['timestamp_ny'].dt.minute.values
        
        # Execution loop states
        state = "IDLE"
        active_buy_level = None
        active_sell_level = None
        buy_sl_level, sell_sl_level = None, None
        buy_tp_level, sell_tp_level = None, None
        size_comp_pending = 0.0
        buy_zone_age_bars = 0
        sell_zone_age_bars = 0
        
        entry_price = 0.0
        stop_loss = 0.0
        take_profit = 0.0
        trade_size_comp = 0.0
        
        bal_comp = 100.0
        trades = []
        
        # Epsilon tolerance for wickless candles
        epsilon = 0.02 * self.tick_size
        
        # Find first valid mask
        valid_mask = ~(np.isnan(prev_ema50s_15) | np.isnan(prev_swing_lows_15))
        if self.with_confluence:
            valid_mask = valid_mask & (~np.isnan(prev_vals_15))
        if not np.any(valid_mask):
            return []
        start_idx = int(np.argmax(valid_mask))
        
        print(f"Starting tick execution over {len(df_merged) - start_idx:,} bars...")
        
        for idx in range(start_idx, len(df_merged)):
            o, h, l, c = opens[idx], highs[idx], lows[idx], closes[idx]
            mn = minutes[idx]
            is_new_15min_bar = (mn % 15 == 0)
            
            # --- 1. Increment limit age and scan setups on new structural bars ---
            if is_new_15min_bar:
                if active_buy_level is not None:
                    buy_zone_age_bars += 1
                    if buy_zone_age_bars > 4: active_buy_level = None
                if active_sell_level is not None:
                    sell_zone_age_bars += 1
                    if sell_zone_age_bars > 4: active_sell_level = None
                    
                if state == "IDLE":
                    o15 = prev_opens_15[idx]
                    h15 = prev_highs_15[idx]
                    l15 = prev_lows_15[idx]
                    c15 = prev_closes_15[idx]
                    ema15 = prev_ema50s_15[idx]
                    val15 = prev_vals_15[idx]
                    vah15 = prev_vahs_15[idx]
                    
                    # Wickless opens indicators (tolerance)
                    no_bottom_wick = abs(o15 - l15) <= epsilon and c15 > o15
                    no_top_wick = abs(o15 - h15) <= epsilon and c15 < o15
                    
                    # Bullish setup check
                    if no_bottom_wick and c15 > ema15:
                        if not self.with_confluence or (o15 > vah15):
                            active_buy_level = o15
                            buy_sl_level = prev_swing_lows_15[idx] - self.sl_buffer
                            risk = active_buy_level - buy_sl_level
                            if risk >= self.min_risk:
                                buy_tp_level = active_buy_level + self.rr * risk
                                size_comp_pending = (bal_comp * 0.02) / risk # risk exactly 2.0%
                                buy_zone_age_bars = 0
                            else:
                                active_buy_level = None
                                
                    # Bearish setup check
                    elif no_top_wick and c15 < ema15:
                        if not self.with_confluence or (o15 < val15):
                            active_sell_level = o15
                            sell_sl_level = prev_swing_highs_15[idx] + self.sl_buffer
                            risk = sell_sl_level - active_sell_level
                            if risk >= self.min_risk:
                                sell_tp_level = active_sell_level - self.rr * risk
                                size_comp_pending = (bal_comp * 0.02) / risk # risk exactly 2.0%
                                sell_zone_age_bars = 0
                            else:
                                active_sell_level = None
                                
            # --- 2. Process exits for active trades ---
            if state == "LONG_ACTIVE":
                exit_val = None
                if l <= stop_loss and h >= take_profit:
                    exit_val = stop_loss
                    res = 'SL'
                elif l <= stop_loss:
                    exit_val = stop_loss
                    res = 'SL'
                elif h >= take_profit:
                    exit_val = take_profit
                    res = 'TP'
                    
                if exit_val is not None:
                    pnl = (exit_val - entry_price - self.slippage) * trade_size_comp
                    bal_comp += pnl
                    trades.append({'pnl': pnl, 'result': res, 'balance_before': bal_comp - pnl})
                    state = "IDLE"
                    
            elif state == "SHORT_ACTIVE":
                exit_val = None
                if h >= stop_loss and l <= take_profit:
                    exit_val = stop_loss
                    res = 'SL'
                elif h >= stop_loss:
                    exit_val = stop_loss
                    res = 'SL'
                elif l <= take_profit:
                    exit_val = take_profit
                    res = 'TP'
                    
                if exit_val is not None:
                    pnl = (entry_price - exit_val - self.slippage) * trade_size_comp
                    bal_comp += pnl
                    trades.append({'pnl': pnl, 'result': res, 'balance_before': bal_comp - pnl})
                    state = "IDLE"
                    
            if bal_comp < 1.0:
                bal_comp = 0.0
                print("Portfolio Account Ruin! Balance fell below $1.0.")
                break
                
            # --- 3. Process pending limit order fills ---
            if state == "IDLE":
                if active_buy_level is not None and l <= active_buy_level <= h:
                    state = "LONG_ACTIVE"
                    entry_price = active_buy_level
                    stop_loss = buy_sl_level
                    take_profit = buy_tp_level
                    trade_size_comp = size_comp_pending
                    active_buy_level = None
                    active_sell_level = None
                    
                    # Handle immediate gap fills within the very same minute bar
                    if l <= stop_loss:
                        pnl = (stop_loss - entry_price - self.slippage) * trade_size_comp
                        bal_comp += pnl
                        trades.append({'pnl': pnl, 'result': 'SL', 'balance_before': bal_comp - pnl})
                        state = "IDLE"
                    elif h >= take_profit:
                        pnl = (take_profit - entry_price - self.slippage) * trade_size_comp
                        bal_comp += pnl
                        trades.append({'pnl': pnl, 'result': 'TP', 'balance_before': bal_comp - pnl})
                        state = "IDLE"
                        
                elif active_sell_level is not None and l <= active_sell_level <= h:
                    state = "SHORT_ACTIVE"
                    entry_price = active_sell_level
                    stop_loss = sell_sl_level
                    take_profit = sell_tp_level
                    trade_size_comp = size_comp_pending
                    active_sell_level = None
                    active_buy_level = None
                    
                    # Handle immediate gap fills within the very same minute bar
                    if h >= stop_loss:
                        pnl = (entry_price - stop_loss - self.slippage) * trade_size_comp
                        bal_comp += pnl
                        trades.append({'pnl': pnl, 'result': 'SL', 'balance_before': bal_comp - pnl})
                        state = "IDLE"
                    elif l <= take_profit:
                        pnl = (entry_price - take_profit - self.slippage) * trade_size_comp
                        bal_comp += pnl
                        trades.append({'pnl': pnl, 'result': 'TP', 'balance_before': bal_comp - pnl})
                        state = "IDLE"
                        
        return trades
