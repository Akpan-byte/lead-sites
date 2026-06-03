#!/usr/bin/env python3
"""
GBP/USD 25-Year Compounding Risk Study for Top Multiverse Configurations
========================================================================
Runs the 25-year backtest at 15-minute resolution for:
1. Rank 1 Sharpe (1:4.5 RR, digital 5 pip wick, 24/5)
2. Top Win Rate (1:1 RR, proportional 10% wick, active session, 50% body retrace)
Using a 2.0% compounding risk model.
"""

import os
import sys
import json
import math
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd

NY_TZ = ZoneInfo("America/New_York")
DATA_DIR = Path("/config/bardfx-strategy/data")
CSV_PATH = DATA_DIR / "gbpusd_25y_1min.csv"

CFG = {
    'sl_buffer': 0.0002,    # 2.0 pips stop loss buffer
    'slippage': 0.0001,     # 1.0 pip spread/execution friction
    'pip_value': 0.0001,
    'min_risk_pips': 5.0    # 5 pips minimum risk
}

def run_compounding_backtest(session, retrace, rr, wick_type, wick_val, dates_arr, hours_arr, minutes_arr, weekdays_arr, opens_arr, highs_arr, lows_arr, closes_arr, ema50_arr, swing_lows_arr, swing_highs_arr, start_idx):
    sl_buffer = CFG['sl_buffer']
    slippage = CFG['slippage']
    pip_val = CFG['pip_value']
    min_risk = CFG['min_risk_pips'] * pip_val
    
    bal_comp = 100.0
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
    
    trades = []
    active_days_set = set()
    total_len = len(opens_arr)
    
    for idx in range(start_idx, total_len):
        d = dates_arr[idx]
        hr = hours_arr[idx]
        mn = minutes_arr[idx]
        wkday = weekdays_arr[idx]
        active_days_set.add(d)
        
        o, h, l, c = opens_arr[idx], highs_arr[idx], lows_arr[idx], closes_arr[idx]
        
        is_friday_close = (wkday == 4 and hr == 16 and mn == 45)
        
        # Increment Limit Age
        if active_buy_level is not None:
            buy_zone_age_bars += 1
            if buy_zone_age_bars > 4: active_buy_level = None
        if active_sell_level is not None:
            sell_zone_age_bars += 1
            if sell_zone_age_bars > 4: active_sell_level = None
            
        # Check Limit Order Fills
        if state == "IDLE" and bal_comp > 1.0:
            if active_buy_level is not None and l <= active_buy_level <= h:
                state = "LONG_ACTIVE"
                entry_price = active_buy_level
                stop_loss = buy_sl_level
                take_profit = buy_tp_level
                trade_size_comp = (bal_comp * 0.02) / (entry_price - stop_loss)
                active_buy_level = None
                active_sell_level = None
                
            elif active_sell_level is not None and l <= active_sell_level <= h:
                state = "SHORT_ACTIVE"
                entry_price = active_sell_level
                stop_loss = sell_sl_level
                take_profit = sell_tp_level
                trade_size_comp = (bal_comp * 0.02) / (stop_loss - entry_price)
                active_sell_level = None
                active_buy_level = None
                
        # Process Exits
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
            elif is_friday_close:
                exit_val = c
                res = 'TP' if c > entry_price else 'SL'
                
            if exit_val is not None:
                pnl_c = (exit_val - entry_price - slippage) * trade_size_comp
                bal_comp += pnl_c
                trades.append({'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c})
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
            elif is_friday_close:
                exit_val = c
                res = 'TP' if c < entry_price else 'SL'
                
            if exit_val is not None:
                pnl_c = (entry_price - exit_val - slippage) * trade_size_comp
                bal_comp += pnl_c
                trades.append({'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c})
                state = "IDLE"
                
        if bal_comp < 1.0: 
            bal_comp = 0.0
            
        # Scan setups for next bar
        if state == "IDLE" and active_buy_level is None and active_sell_level is None and bal_comp > 1.0:
            in_session = True
            if session == 'active':
                in_session = (3 <= hr <= 12)
                
            if in_session:
                if wick_type == 'digital':
                    epsilon = wick_val * pip_val
                else:
                    epsilon = (h - l) * wick_val
                    
                no_bottom_wick = abs(o - l) <= epsilon and c > o
                no_top_wick = abs(o - h) <= epsilon and c < o
                
                ema15 = ema50_arr[idx]
                
                if no_bottom_wick and c > ema15:
                    candle_body = c - o
                    active_buy_level = o - retrace * candle_body
                    buy_sl_level = swing_lows_arr[idx] - sl_buffer
                    risk = active_buy_level - buy_sl_level
                    if risk >= min_risk:
                        buy_tp_level = active_buy_level + rr * risk
                        buy_zone_age_bars = 0
                    else:
                        active_buy_level = None
                        
                elif no_top_wick and c < ema15:
                    candle_body = o - c
                    active_sell_level = o + retrace * candle_body
                    sell_sl_level = swing_highs_arr[idx] + sl_buffer
                    risk = sell_sl_level - active_sell_level
                    if risk >= min_risk:
                        sell_tp_level = active_sell_level - rr * risk
                        sell_zone_age_bars = 0
                    else:
                        active_sell_level = None
                        
    n_trades = len(trades)
    n_days = len(active_days_set)
    if n_trades == 0:
        return {'trades': 0, 'final_balance': 100.0, 'max_dd': 0.0, 'cagr': 0.0, 'win_rate': 0.0}
        
    wins = [t for t in trades if t['result'] == 'TP']
    win_rate = len(wins) / n_trades * 100.0
    
    max_dd = 0.0
    peak = 100.0
    temp_bal = 100.0
    for t in trades:
        temp_bal += t['pnl']
        if temp_bal > peak: peak = temp_bal
        dd = (peak - temp_bal) / peak if peak > 0 else 0.0
        if dd > max_dd: max_dd = dd
        
    cagr = ((bal_comp / 100.0) ** (1.0 / (n_days / 252.0)) - 1.0) * 100.0 if bal_comp > 0.0 else -100.0
    
    return {
        'trades': n_trades,
        'final_balance': bal_comp,
        'max_dd': max_dd * 100.0,
        'cagr': cagr,
        'win_rate': win_rate
    }

def main():
    print("Loading 1-minute dataset and resampling...")
    df = pd.read_csv(CSV_PATH)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    if df['timestamp'].dt.tz is None:
        df['timestamp'] = df['timestamp'].dt.tz_localize('UTC')
    else:
        df['timestamp'] = df['timestamp'].dt.tz_convert('UTC')
        
    df['timestamp_15'] = df['timestamp'].dt.floor('15min')
    
    df_15 = df.groupby('timestamp_15').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).reset_index()
    
    df_15 = df_15.sort_values('timestamp_15').reset_index(drop=True)
    df_15['ema50'] = df_15['close'].ewm(span=50, adjust=False).mean()
    df_15['swing_low'] = df_15['low'].rolling(10).min()
    df_15['swing_high'] = df_15['high'].rolling(10).max()
    
    df_15['timestamp_ny'] = df_15['timestamp_15'].dt.tz_convert(NY_TZ)
    df_15['dt_date'] = df_15['timestamp_ny'].dt.date
    df_15['dt_hour'] = df_15['timestamp_ny'].dt.hour
    df_15['dt_minute'] = df_15['timestamp_ny'].dt.minute
    df_15['dt_weekday'] = df_15['timestamp_ny'].dt.weekday
    
    dates_arr = df_15['dt_date'].values
    hours_arr = df_15['dt_hour'].values
    minutes_arr = df_15['dt_minute'].values
    weekdays_arr = df_15['dt_weekday'].values
    opens_arr = df_15['open'].values
    highs_arr = df_15['high'].values
    lows_arr = df_15['low'].values
    closes_arr = df_15['close'].values
    ema50_arr = df_15['ema50'].values
    swing_lows_arr = df_15['swing_low'].values
    swing_highs_arr = df_15['swing_high'].values
    
    valid_mask = ~(np.isnan(ema50_arr) | np.isnan(swing_lows_arr))
    start_idx = int(np.argmax(valid_mask))
    
    # -----------------------------------------------------------------------
    # Run Configuration 1: Rank 1 Sharpe
    # -----------------------------------------------------------------------
    print("\nRunning Rank 1 Sharpe (1:4.5 RR, digital 5 pip wick) under 2% Compounding Risk...")
    res1 = run_compounding_backtest('24/5', 0.0, 4.5, 'digital', 5.0, dates_arr, hours_arr, minutes_arr, weekdays_arr, opens_arr, highs_arr, lows_arr, closes_arr, ema50_arr, swing_lows_arr, swing_highs_arr, start_idx)
    
    # -----------------------------------------------------------------------
    # Run Configuration 2: Top Win Rate
    # -----------------------------------------------------------------------
    print("Running Rank 2 Sharpe/Top Win Rate (1:1.0 RR, proportional 10% wick, active session, 50% body retrace) under 2% Compounding Risk...")
    res2 = run_compounding_backtest('active', 0.50, 1.0, 'proportional', 0.10, dates_arr, hours_arr, minutes_arr, weekdays_arr, opens_arr, highs_arr, lows_arr, closes_arr, ema50_arr, swing_lows_arr, swing_highs_arr, start_idx)
    
    print("\n" + "="*80)
    print("COMPOUNDING STUDY RESULTS (2.0% RISK PER TRADE)")
    print("="*80)
    print("Model 1: Rank 1 Sharpe (1:4.5 RR, digital 5 pip wick, 24/5)")
    print(f"  Total Trades:     {res1['trades']:,}")
    print(f"  Win Rate:         {res1['win_rate']:.2f}%")
    print(f"  Terminal Balance: ${res1['final_balance']:.2f}")
    print(f"  Max Drawdown:     {res1['max_dd']:.2f}%")
    print(f"  CAGR:             {res1['cagr']:.2f}%")
    print("-" * 80)
    print("Model 2: Top Win Rate (1:1.0 RR, proportional 10% wick, active, 50% retrace)")
    print(f"  Total Trades:     {res2['trades']:,}")
    print(f"  Win Rate:         {res2['win_rate']:.2f}%")
    print(f"  Terminal Balance: ${res2['final_balance']:.2f}")
    print(f"  Max Drawdown:     {res2['max_dd']:.2f}%")
    print(f"  CAGR:             {res2['cagr']:.2f}%")
    print("="*80)

if __name__ == "__main__":
    main()
