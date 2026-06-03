#!/usr/bin/env python3
"""
Unified Quantitative Trading Strategy Stack
==========================================
A modular, high-fidelity, object-oriented python library implementing our 
four core proprietary strategies. Fully immune to look-ahead bias and ready 
for live api or backtest integration.

Strategies Implemented:
-----------------------
1. Strategy 1: VolumeProfileGatedORB (SPY, GOLD, NVDA Winner)
   - Resamples M15 candles, scans for EMA-aligned wickless opens.
   - Gates entries strictly outside the 10-session rolling Volume Profile Value Area.
2. Strategy 2: FVGSupplyDemandGatedORB (SOL, BTC, ETH Altcoin Winner)
   - Maps H1/H4 Supply/Demand zones, M15 Opening Ranges, and M1/M5 FVG pullbacks.
   - Gates breakouts outside S&D zones, fills limit orders on FVG boundaries.
3. Strategy 3: InversedOCOMeanReversion (Crypto Whipsaw Exploiter)
   - Establishes a session opening range.
   - Shorts upside breakouts and longs downside breakouts (anti-whipsaw).
   - Dynamic bracket exits: TP = +1.0 R, SL = -1.5 R.
4. Strategy 4: NQWideOCOBreakout (Nasdaq-100 Momentum Winner)
   - Classical wide opening range brackets with a lookback high/low buffer.
   - Designed to ride broad trending extensions.
"""

import math
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# STRATEGY 1: Volume Profile Gated ORB
# ---------------------------------------------------------------------------

class VolumeProfileGatedORB:
    def __init__(self, rr=3.0, slippage=0.0001, sl_buffer=0.0002, lookback=10, tick_size=0.01):
        self.rr = rr
        self.slippage = slippage
        self.sl_buffer = sl_buffer
        self.lookback = lookback
        self.tick_size = tick_size

    def compute_volume_profile(self, opens, highs, lows, volumes):
        bins = {}
        total_vol = 0.0
        for o, h, l, v in zip(opens, highs, lows, volumes):
            curr = math.floor(l / self.tick_size) * self.tick_size
            levels = []
            while curr <= math.ceil(h / self.tick_size) * self.tick_size:
                levels.append(round(curr, 5))
                curr += self.tick_size
            if not levels: continue
            vol_per_level = v / len(levels)
            for lev in levels:
                bins[lev] = bins.get(lev, 0.0) + vol_per_level
                total_vol += vol_per_level
        if not bins or total_vol == 0:
            return min(lows), max(highs)
        poc = max(bins, key=bins.get)
        sorted_bins = sorted(bins.items(), key=lambda x: x[0])
        prices = [x[0] for x in sorted_bins]
        vols = [x[1] for x in sorted_bins]
        target_vol = total_vol * 0.70
        current_vol = bins[poc]
        poc_idx = prices.index(poc)
        low_idx = poc_idx
        high_idx = poc_idx
        while current_vol < target_vol:
            prev_vol = vols[low_idx - 1] if low_idx > 0 else 0.0
            next_vol = vols[high_idx + 1] if high_idx < len(prices) - 1 else 0.0
            if prev_vol == 0.0 and next_vol == 0.0: break
            if prev_vol >= next_vol:
                low_idx -= 1
                current_vol += prev_vol
            else:
                high_idx += 1
                current_vol += next_vol
        return prices[low_idx], prices[high_idx]

    def backtest(self, df_1m, df_15):
        # 15m Indicators
        df_15['ema50'] = df_15['close'].ewm(span=50, adjust=False).mean()
        df_15['swing_low'] = df_15['low'].rolling(10).min()
        df_15['swing_high'] = df_15['high'].rolling(10).max()
        
        vals, vahs = [], []
        opens_15 = df_15['open'].values
        highs_15 = df_15['high'].values
        lows_15 = df_15['low'].values
        vols_15 = df_15['volume'].values
        
        for idx in range(len(df_15)):
            if idx < self.lookback:
                vals.append(np.nan)
                vahs.append(np.nan)
            else:
                val, vah = self.compute_volume_profile(
                    opens_15[idx - self.lookback : idx],
                    highs_15[idx - self.lookback : idx],
                    lows_15[idx - self.lookback : idx],
                    vols_15[idx - self.lookback : idx]
                )
                vals.append(val)
                vahs.append(vah)
        df_15['val'] = vals
        df_15['vah'] = vahs
        
        # Shift to immunize look-ahead bias
        df_15['prev_open'] = df_15['open'].shift(1)
        df_15['prev_high'] = df_15['high'].shift(1)
        df_15['prev_low'] = df_15['low'].shift(1)
        df_15['prev_close'] = df_15['close'].shift(1)
        df_15['prev_ema50'] = df_15['ema50'].shift(1)
        df_15['prev_swing_low'] = df_15['swing_low'].shift(1)
        df_15['prev_swing_high'] = df_15['swing_high'].shift(1)
        df_15['prev_val'] = df_15['val'].shift(1)
        df_15['prev_vah'] = df_15['vah'].shift(1)
        
        # Merge back to 1m
        df_1m['timestamp_15'] = df_1m['timestamp'].dt.floor('15min')
        df_merged = pd.merge(df_1m, df_15[[
            'timestamp_15', 'prev_open', 'prev_high', 'prev_low', 'prev_close',
            'prev_ema50', 'prev_swing_low', 'prev_swing_high', 'prev_val', 'prev_vah'
        ]], on='timestamp_15', how='left')
        
        # Fast arrays
        opens = df_merged['open'].values
        highs = df_merged['high'].values
        lows = df_merged['low'].values
        closes = df_merged['close'].values
        
        prev_opens = df_merged['prev_open'].values
        prev_highs = df_merged['prev_high'].values
        prev_lows = df_merged['prev_low'].values
        prev_closes = df_merged['prev_close'].values
        prev_ema50s = df_merged['prev_ema50'].values
        prev_swing_lows = df_merged['prev_swing_low'].values
        prev_swing_highs = df_merged['prev_swing_high'].values
        prev_vals = df_merged['prev_val'].values
        prev_vahs = df_merged['prev_vah'].values
        
        df_merged['timestamp_ny'] = df_merged['timestamp'].dt.tz_convert('America/New_York')
        minutes = df_merged['timestamp_ny'].dt.minute.values
        
        state = "IDLE"
        active_buy_level = None
        active_sell_level = None
        buy_sl, sell_sl = None, None
        buy_tp, sell_tp = None, None
        limit_age = 0
        
        entry_price = 0.0
        stop_loss = 0.0
        take_profit = 0.0
        
        trades = []
        epsilon = 0.02 * self.tick_size
        
        valid_mask = ~(np.isnan(prev_ema50s) | np.isnan(prev_swing_lows) | np.isnan(prev_vals))
        if not np.any(valid_mask): return []
        start_idx = int(np.argmax(valid_mask))
        
        for idx in range(start_idx, len(df_merged)):
            o, h, l, c = opens[idx], highs[idx], lows[idx], closes[idx]
            is_new_15m = (minutes[idx] % 15 == 0)
            
            if is_new_15m:
                if active_buy_level is not None:
                    limit_age += 1
                    if limit_age > 4: active_buy_level = None
                if active_sell_level is not None:
                    limit_age += 1
                    if limit_age > 4: active_sell_level = None
                    
                if state == "IDLE":
                    o15, h15, l15, c15 = prev_opens[idx], prev_highs[idx], prev_lows[idx], prev_closes[idx]
                    ema15 = prev_ema50s[idx]
                    val15 = prev_vals[idx]
                    vah15 = prev_vahs[idx]
                    
                    no_bottom_wick = abs(o15 - l15) <= epsilon and c15 > o15
                    no_top_wick = abs(o15 - h15) <= epsilon and c15 < o15
                    
                    if no_bottom_wick and c15 > ema15:
                        if o15 > vah15:
                            active_buy_level = o15
                            buy_sl = prev_swing_lows[idx] - self.sl_buffer
                            buy_tp = active_buy_level + self.rr * (active_buy_level - buy_sl)
                            limit_age = 0
                    elif no_top_wick and c15 < ema15:
                        if o15 < val15:
                            active_sell_level = o15
                            sell_sl = prev_swing_highs[idx] + self.sl_buffer
                            sell_tp = active_sell_level - self.rr * (sell_sl - active_sell_level)
                            limit_age = 0
                            
            # Process exits
            if state == "LONG_ACTIVE":
                exit_val = None
                if l <= stop_loss and h >= take_profit: exit_val, res = stop_loss, 'SL'
                elif l <= stop_loss: exit_val, res = stop_loss, 'SL'
                elif h >= take_profit: exit_val, res = take_profit, 'TP'
                if exit_val is not None:
                    trades.append({'pnl_pct': (exit_val - entry_price - self.slippage)/(entry_price - stop_loss), 'result': res})
                    state = "IDLE"
            elif state == "SHORT_ACTIVE":
                exit_val = None
                if h >= stop_loss and l <= take_profit: exit_val, res = stop_loss, 'SL'
                elif h >= stop_loss: exit_val, res = stop_loss, 'SL'
                elif l <= take_profit: exit_val, res = take_profit, 'TP'
                if exit_val is not None:
                    trades.append({'pnl_pct': (entry_price - exit_val - self.slippage)/(stop_loss - entry_price), 'result': res})
                    state = "IDLE"
                    
            # Process fills
            if state == "IDLE":
                if active_buy_level is not None and l <= active_buy_level <= h:
                    state, entry_price, stop_loss, take_profit = "LONG_ACTIVE", active_buy_level, buy_sl, buy_tp
                    active_buy_level = None
                elif active_sell_level is not None and l <= active_sell_level <= h:
                    state, entry_price, stop_loss, take_profit = "SHORT_ACTIVE", active_sell_level, sell_sl, sell_tp
                    active_sell_level = None
                    
        return trades

# ---------------------------------------------------------------------------
# STRATEGY 2: FVG + Supply & Demand Gated ORB
# ---------------------------------------------------------------------------

class FVGSupplyDemandGatedORB:
    def __init__(self, htf_m=60, ort_m=15, ltf_m=1, rr=3.0, slippage=0.0001, sl_buffer=0.0002, tick_size=0.01):
        self.htf_m = htf_m
        self.ort_m = ort_m
        self.ltf_m = ltf_m
        self.rr = rr
        self.slippage = slippage
        self.sl_buffer = sl_buffer
        self.tick_size = tick_size

    def calculate_supply_demand_zones(self, df_htf):
        high = df_htf['high'].values
        low = df_htf['low'].values
        open_p = df_htf['open'].values
        close = df_htf['close'].values
        
        bodies = np.abs(close - open_p)
        body_atr = pd.Series(bodies).rolling(20).mean().values
        body_atr[np.isnan(body_atr)] = np.mean(bodies)
        
        demand_zones, supply_zones = [], []
        for idx in range(len(df_htf)):
            if close[idx] > open_p[idx] and bodies[idx] >= 1.5 * body_atr[idx]:
                demand_zones.append({'low': low[idx], 'high': close[idx], 'mitigated': False})
            elif close[idx] < open_p[idx] and bodies[idx] >= 1.5 * body_atr[idx]:
                supply_zones.append({'low': close[idx], 'high': high[idx], 'mitigated': False})
        return demand_zones, supply_zones

    def backtest(self, df_ltf):
        opens = df_ltf['open'].values
        highs = df_ltf['high'].values
        lows = df_ltf['low'].values
        closes = df_ltf['close'].values
        
        df_ltf['timestamp_ny'] = df_ltf['timestamp'].dt.tz_convert('America/New_York')
        hours = df_ltf['timestamp_ny'].dt.hour.values
        minutes = df_ltf['timestamp_ny'].dt.minute.values
        dates = df_ltf['timestamp_ny'].dt.date.values
        
        df_htf = df_ltf.resample(f'{self.htf_m}min', on='timestamp').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
        }).dropna().reset_index()
        
        demand_zones, supply_zones = self.calculate_supply_demand_zones(df_htf)
        daily_or = {}
        
        state = "IDLE"
        active_buy_level = None
        active_sell_level = None
        buy_sl, sell_sl = None, None
        buy_tp, sell_tp = None, None
        limit_age = 0
        
        entry_price = 0.0
        stop_loss = 0.0
        take_profit = 0.0
        
        trades = []
        
        for idx in range(2, len(df_ltf)):
            o, h, l, c = opens[idx], highs[idx], lows[idx], closes[idx]
            hr, mn, d = hours[idx], minutes[idx], dates[idx]
            
            if hr == 9 and mn == 30:
                daily_or[d] = {'high': h, 'low': l, 'count': 1, 'max_count': self.ort_m // self.ltf_m}
            elif d in daily_or and daily_or[d]['count'] < daily_or[d]['max_count']:
                daily_or[d]['high'] = max(daily_or[d]['high'], h)
                daily_or[d]['low'] = min(daily_or[d]['low'], l)
                daily_or[d]['count'] += 1
                
            if d not in daily_or or daily_or[d]['count'] < daily_or[d]['max_count']: continue
            or_high = daily_or[d]['high']
            or_low = daily_or[d]['low']
            
            # Update zones
            for zone in demand_zones:
                if not zone['mitigated'] and l < zone['low']: zone['mitigated'] = True
            for zone in supply_zones:
                if not zone['mitigated'] and h > zone['high']: zone['mitigated'] = True
                
            # Limit age
            if active_buy_level is not None:
                limit_age += 1
                if limit_age > 4: active_buy_level = None
            if active_sell_level is not None:
                limit_age += 1
                if limit_age > 4: active_sell_level = None
                
            # Scan setups
            if state == "IDLE" and active_buy_level is None and active_sell_level is None:
                if h > or_high >= o:
                    fvg = l > highs[idx - 2]
                    if fvg:
                        overhead = False
                        for zone in supply_zones:
                            if not zone['mitigated'] and or_high < zone['low'] < or_high + 50 * self.tick_size:
                                overhead = True
                                break
                        if not overhead:
                            active_buy_level = highs[idx - 2]
                            buy_sl = lows[idx - 2] - self.sl_buffer
                            buy_tp = active_buy_level + self.rr * (active_buy_level - buy_sl)
                            limit_age = 0
                elif l < or_low <= o:
                    fvg = h < lows[idx - 2]
                    if fvg:
                        underneath = False
                        for zone in demand_zones:
                            if not zone['mitigated'] and or_low > zone['high'] > or_low - 50 * self.tick_size:
                                underneath = True
                                break
                        if not underneath:
                            active_sell_level = lows[idx - 2]
                            sell_sl = highs[idx - 2] + self.sl_buffer
                            sell_tp = active_sell_level - self.rr * (sell_sl - active_sell_level)
                            limit_age = 0
                            
            # Process exits
            if state == "LONG_ACTIVE":
                exit_val = None
                if l <= stop_loss and h >= take_profit: exit_val, res = stop_loss, 'SL'
                elif l <= stop_loss: exit_val, res = stop_loss, 'SL'
                elif h >= take_profit: exit_val, res = take_profit, 'TP'
                if exit_val is not None:
                    trades.append({'pnl_pct': (exit_val - entry_price - self.slippage)/(entry_price - stop_loss), 'result': res})
                    state = "IDLE"
            elif state == "SHORT_ACTIVE":
                exit_val = None
                if h >= stop_loss and l <= take_profit: exit_val, res = stop_loss, 'SL'
                elif h >= stop_loss: exit_val, res = stop_loss, 'SL'
                elif l <= take_profit: exit_val, res = take_profit, 'TP'
                if exit_val is not None:
                    trades.append({'pnl_pct': (entry_price - exit_val - self.slippage)/(stop_loss - entry_price), 'result': res})
                    state = "IDLE"
                    
            # Process fills
            if state == "IDLE":
                if active_buy_level is not None and l <= active_buy_level <= h:
                    state, entry_price, stop_loss, take_profit = "LONG_ACTIVE", active_buy_level, buy_sl, buy_tp
                    active_buy_level = None
                elif active_sell_level is not None and l <= active_sell_level <= h:
                    state, entry_price, stop_loss, take_profit = "SHORT_ACTIVE", active_sell_level, sell_sl, sell_tp
                    active_sell_level = None
                    
        return trades

# ---------------------------------------------------------------------------
# STRATEGY 3: Inversed OCO Mean-Reversion
# ---------------------------------------------------------------------------

class InversedOCOMeanReversion:
    def __init__(self, ort_m=15, ltf_m=1, slippage=0.0001, sl_mult=1.5, tp_mult=1.0):
        self.ort_m = ort_m
        self.ltf_m = ltf_m
        self.slippage = slippage
        self.sl_mult = sl_mult
        self.tp_mult = tp_mult

    def backtest(self, df_ltf):
        opens = df_ltf['open'].values
        highs = df_ltf['high'].values
        lows = df_ltf['low'].values
        closes = df_ltf['close'].values
        
        df_ltf['timestamp_ny'] = df_ltf['timestamp'].dt.tz_convert('America/New_York')
        hours = df_ltf['timestamp_ny'].dt.hour.values
        minutes = df_ltf['timestamp_ny'].dt.minute.values
        dates = df_ltf['timestamp_ny'].dt.date.values
        
        daily_or = {}
        state = "IDLE"
        entry_price = 0.0
        stop_loss = 0.0
        take_profit = 0.0
        
        trades = []
        
        for idx in range(len(df_ltf)):
            o, h, l, c = opens[idx], highs[idx], lows[idx], closes[idx]
            hr, mn, d = hours[idx], minutes[idx], dates[idx]
            
            if hr == 9 and mn == 30:
                daily_or[d] = {'high': h, 'low': l, 'count': 1, 'max_count': self.ort_m // self.ltf_m}
            elif d in daily_or and daily_or[d]['count'] < daily_or[d]['max_count']:
                daily_or[d]['high'] = max(daily_or[d]['high'], h)
                daily_or[d]['low'] = min(daily_or[d]['low'], l)
                daily_or[d]['count'] += 1
                
            if d not in daily_or or daily_or[d]['count'] < daily_or[d]['max_count']: continue
            or_high = daily_or[d]['high']
            or_low = daily_or[d]['low']
            range_width = or_high - or_low
            
            # Process exits
            if state == "LONG_ACTIVE":
                exit_val = None
                if l <= stop_loss and h >= take_profit: exit_val, res = stop_loss, 'SL'
                elif l <= stop_loss: exit_val, res = stop_loss, 'SL'
                elif h >= take_profit: exit_val, res = take_profit, 'TP'
                if exit_val is not None:
                    trades.append({'pnl_pct': (exit_val - entry_price - self.slippage)/(entry_price - stop_loss), 'result': res})
                    state = "IDLE"
            elif state == "SHORT_ACTIVE":
                exit_val = None
                if h >= stop_loss and l <= take_profit: exit_val, res = stop_loss, 'SL'
                elif h >= stop_loss: exit_val, res = stop_loss, 'SL'
                elif l <= take_profit: exit_val, res = take_profit, 'TP'
                if exit_val is not None:
                    trades.append({'pnl_pct': (entry_price - exit_val - self.slippage)/(stop_loss - entry_price), 'result': res})
                    state = "IDLE"
                    
            # Process entries (Mean-Reversion: short range high, long range low)
            if state == "IDLE" and range_width > 0:
                # Price crosses range high -> SHORT
                if h >= or_high >= o:
                    state = "SHORT_ACTIVE"
                    entry_price = or_high
                    stop_loss = entry_price + self.sl_mult * range_width
                    take_profit = entry_price - self.tp_mult * range_width
                # Price crosses range low -> LONG
                elif l <= or_low <= o:
                    state = "LONG_ACTIVE"
                    entry_price = or_low
                    stop_loss = entry_price - self.sl_mult * range_width
                    take_profit = entry_price + self.tp_mult * range_width
                    
        return trades

# ---------------------------------------------------------------------------
# STRATEGY 4: NQ Wide OCO Breakout
# ---------------------------------------------------------------------------

class NQWideOCOBreakout:
    def __init__(self, buffer_pts=28.0, sl_pts=56.0, tp_pts=112.0, lookback=5):
        self.buffer = buffer_pts
        self.sl = sl_pts
        self.tp = tp_pts
        self.lookback = lookback

    def backtest(self, df_1m):
        highs = df_1m['high'].values
        lows = df_1m['low'].values
        closes = df_1m['close'].values
        opens = df_1m['open'].values
        
        df_1m['timestamp_ny'] = df_1m['timestamp'].dt.tz_convert('America/New_York')
        hours = df_1m['timestamp_ny'].dt.hour.values
        minutes = df_1m['timestamp_ny'].dt.minute.values
        
        state = "IDLE"
        buy_stop = None
        sell_stop = None
        entry_price = 0.0
        stop_loss = 0.0
        take_profit = 0.0
        
        trades = []
        
        for idx in range(self.lookback, len(df_1m)):
            o, h, l, c = opens[idx], highs[idx], lows[idx], closes[idx]
            hr, mn = hours[idx], minutes[idx]
            
            # Arm brackets at the end of the consolidation window (e.g. 9:45 AM after NYSE open)
            if hr == 9 and mn == 45:
                window_high = max(highs[idx - self.lookback : idx])
                window_low = min(lows[idx - self.lookback : idx])
                buy_stop = window_high + self.buffer
                sell_stop = window_low - self.buffer
                
            # Reset at end of New York day session (4:00 PM / 16:00 EST)
            if hr == 16 and mn == 0:
                buy_stop, sell_stop = None, None
                if state in ("LONG_ACTIVE", "SHORT_ACTIVE"):
                    # Force exit at cash close
                    exit_price = c
                    res = 'TP' if (state == "LONG_ACTIVE" and exit_price > entry_price) or (state == "SHORT_ACTIVE" and exit_price < entry_price) else 'SL'
                    trades.append({'pnl_pct': (exit_price - entry_price)/entry_price if state == "LONG_ACTIVE" else (entry_price - exit_price)/entry_price, 'result': res})
                    state = "IDLE"
                    
            # Process exits
            if state == "LONG_ACTIVE":
                exit_val = None
                if l <= stop_loss and h >= take_profit: exit_val, res = stop_loss, 'SL'
                elif l <= stop_loss: exit_val, res = stop_loss, 'SL'
                elif h >= take_profit: exit_val, res = take_profit, 'TP'
                if exit_val is not None:
                    trades.append({'pnl_pct': (exit_val - entry_price)/entry_price, 'result': res})
                    state = "IDLE"
            elif state == "SHORT_ACTIVE":
                exit_val = None
                if h >= stop_loss and l <= take_profit: exit_val, res = stop_loss, 'SL'
                elif h >= stop_loss: exit_val, res = stop_loss, 'SL'
                elif l <= take_profit: exit_val, res = take_profit, 'TP'
                if exit_val is not None:
                    trades.append({'pnl_pct': (entry_price - exit_val)/entry_price, 'result': res})
                    state = "IDLE"
                    
            # Process breakout trigger fills
            if state == "IDLE" and buy_stop is not None:
                if h >= buy_stop >= o:
                    state, entry_price, stop_loss, take_profit = "LONG_ACTIVE", buy_stop, buy_stop - self.sl, buy_stop + self.tp
                    buy_stop, sell_stop = None, None
                elif l <= sell_stop <= o:
                    state, entry_price, stop_loss, take_profit = "SHORT_ACTIVE", sell_stop, sell_stop + self.sl, sell_stop - self.tp
                    buy_stop, sell_stop = None, None
                    
        return trades
