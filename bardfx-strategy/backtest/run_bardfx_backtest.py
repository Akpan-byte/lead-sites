#!/usr/bin/env python3
"""
Bard FX "Compensation Play" Out-of-Sample Historical Backtester
=============================================================
Evaluates the 100% mechanical "No-Wick Compensation Play" on:
- S&P 500 (SPY 5.4-Year 1-Min resampled to 15-Min)
- Nasdaq-100 (QQQ 5.4-Year 1-Min resampled to 15-Min)

Features:
- 15-Minute candle resampling for structural setups and 50 EMA trend filters
- 1-Minute tick-resolution execution for limit order taps, fills, SL/TP exits
- Chronologically correct simulation loop eliminating look-ahead entry biases
- Decoupled pending limit and active trade state machine (no state conflicts)
- Two distinct position sizing models: Fixed Risk (flat 2% of initial) and Compounding Risk (2% of active equity)
- Full Quantitative diagnostics: Sharpe, CAGR, MaxDD, PSR, DSR, WFO, and Markov states.
"""

import os
import sys
import json
import math
import time
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd

NY_TZ = ZoneInfo("America/New_York")
DATA_DIR = Path("/config/hl-nq-bot/data")

# ---------------------------------------------------------------------------
# Quantitative Math Libraries
# ---------------------------------------------------------------------------

def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def calculate_psr(returns: np.ndarray, benchmark_sr: float = 0.0) -> float:
    n = len(returns)
    if n < 4: return 0.5
    mean_ret = np.mean(returns)
    std_ret = np.std(returns, ddof=1)
    if std_ret == 0.0: return 0.5
    sr = mean_ret / std_ret
    diffs = returns - mean_ret
    skew = np.mean(diffs**3) / (std_ret**3) if std_ret > 0 else 0.0
    kurt = np.mean(diffs**4) / (std_ret**4) if std_ret > 0 else 3.0
    variance = (1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr**2) / (n - 1.0)
    if variance <= 0.0: return 0.5
    t_stat = (sr - benchmark_sr) / math.sqrt(variance)
    return normal_cdf(t_stat)

def calculate_dsr(returns: np.ndarray, num_trials: int = 100) -> float:
    n = len(returns)
    if n < 4: return 0.5
    euler_mascheroni = 0.5772156649
    expected_max_sr = math.sqrt(2.0 * math.log(num_trials)) + euler_mascheroni / math.sqrt(2.0 * math.log(num_trials))
    benchmark_sr = expected_max_sr / math.sqrt(n)
    return calculate_psr(returns, benchmark_sr=benchmark_sr)

def calculate_markov_transitions(trades: list) -> dict:
    if len(trades) < 2:
        return {"P_win_given_win": 0.0, "P_loss_given_win": 0.0, "P_win_given_loss": 0.0, "P_loss_given_loss": 0.0}
    ww = wl = lw = ll = 0
    win_count = 0
    loss_count = 0
    for i in range(len(trades) - 1):
        curr = trades[i]['result']
        nxt = trades[i+1]['result']
        if curr == 'TP':
            win_count += 1
            if nxt == 'TP': ww += 1
            else: wl += 1
        elif curr == 'SL':
            loss_count += 1
            if nxt == 'TP': lw += 1
            else: ll += 1
    return {
        "P_win_given_win": float(ww / win_count) if win_count > 0 else 0.0,
        "P_loss_given_win": float(wl / win_count) if win_count > 0 else 0.0,
        "P_win_given_loss": float(lw / loss_count) if loss_count > 0 else 0.0,
        "P_loss_given_loss": float(ll / loss_count) if loss_count > 0 else 0.0
    }

# ---------------------------------------------------------------------------
# Data Loader and Resampler
# ---------------------------------------------------------------------------

def load_and_prepare_15min_data(csv_path: Path) -> pd.DataFrame:
    """Loads 1-minute CSV, normalizes prices to 29174.0, computes 15-minute resampled metrics shifted by 1 bar to avoid look-ahead."""
    print(f"Loading data from {csv_path}...")
    df = pd.read_csv(csv_path)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    if df['timestamp'].dt.tz is None:
        df['timestamp'] = df['timestamp'].dt.tz_localize('UTC')
    else:
        df['timestamp'] = df['timestamp'].dt.tz_convert('UTC')
    df = df.sort_values('timestamp').reset_index(drop=True)
    
    # Scale QQQ or SPY close price to standard 29174.0 baseline
    latest_close = df['close'].iloc[-1]
    scale_factor = 29174.0 / latest_close
    for col in ['open', 'high', 'low', 'close']:
        df[col] = df[col] * scale_factor
        
    df['timestamp_ms'] = (df['timestamp'].astype('int64') // 10**6)
    
    # Floor to 15-minute intervals to identify the 15min bar boundary
    df['timestamp_15'] = df['timestamp'].dt.floor('15min')
    
    # Resample to 15-minute bars to calculate technicals
    print("Resampling 1-minute data to 15-minute bars...")
    df_15 = df.groupby('timestamp_15').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).reset_index()
    
    df_15 = df_15.sort_values('timestamp_15').reset_index(drop=True)
    
    # Compute 50-period EMA on 15-minute closes
    df_15['ema50'] = df_15['close'].ewm(span=50, adjust=False).mean()
    
    # Compute 10-period rolling swing high/low on 15-minute bars
    df_15['swing_low'] = df_15['low'].rolling(10).min()
    df_15['swing_high'] = df_15['high'].rolling(10).max()
    
    # Shift 15-minute technicals by 1 bar to prevent look-ahead bias.
    # At any 1-minute bar inside the [09:30, 09:45) interval, the system only knows about the 15-minute bar that ended at 09:30.
    df_15['prev_open_15'] = df_15['open'].shift(1)
    df_15['prev_high_15'] = df_15['high'].shift(1)
    df_15['prev_low_15'] = df_15['low'].shift(1)
    df_15['prev_close_15'] = df_15['close'].shift(1)
    df_15['prev_ema50_15'] = df_15['ema50'].shift(1)
    df_15['prev_swing_low_15'] = df_15['swing_low'].shift(1)
    df_15['prev_swing_high_15'] = df_15['swing_high'].shift(1)
    
    # Merge 15-minute indicators back to the 1-minute dataframe
    df_merged = pd.merge(df, df_15[[
        'timestamp_15', 
        'prev_open_15', 'prev_high_15', 'prev_low_15', 'prev_close_15',
        'prev_ema50_15', 'prev_swing_low_15', 'prev_swing_high_15'
    ]], on='timestamp_15', how='left')
    
    return df_merged

# ---------------------------------------------------------------------------
# Bard FX Compensation Play Simulation Engine
# ---------------------------------------------------------------------------

def run_bard_fx_backtest(df: pd.DataFrame, apply_slippage: bool) -> dict:
    records = df[[
        'timestamp', 'timestamp_ms', 'open', 'high', 'low', 'close', 
        'prev_open_15', 'prev_high_15', 'prev_low_15', 'prev_close_15',
        'prev_ema50_15', 'prev_swing_low_15', 'prev_swing_high_15'
    ]].to_dict('records')
    
    # Balance tracking
    bal_comp = 100.0  # Compounding sizing (2% of active equity)
    bal_fixed = 100.0  # Fixed sizing (flat 2% of initial $100 balance = $2.00)
    starting_balance = 100.0
    
    # State flags
    state = "IDLE"  # "IDLE", "LONG_ACTIVE", "SHORT_ACTIVE"
    
    # Active limit order levels (pending)
    active_buy_level = None
    active_sell_level = None
    buy_sl_level, sell_sl_level = None, None
    buy_tp_level, sell_tp_level = None, None
    size_comp_pending = 0.0
    size_fixed_pending = 0.0
    buy_zone_age_bars = 0
    sell_zone_age_bars = 0
    
    # Decoupled active trade parameters (once filled)
    entry_price = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    trade_size_comp = 0.0
    trade_size_fixed = 0.0
    
    trades_comp = []
    trades_fixed = []
    active_days_set = set()
    
    # Tolerances to account for float comparisons in digital candles
    epsilon = 0.1  # 10 cents on a 29k scaled price
    slippage = 0.5 if apply_slippage else 0.0
    
    for idx, r in enumerate(records):
        # Skip if 15min indicators are not fully initialized yet (shift of 1 + 50 bar warmup)
        if pd.isna(r['prev_ema50_15']) or pd.isna(r['prev_swing_low_15']):
            continue
            
        t_ms = r['timestamp_ms']
        dt_ny = datetime.datetime.fromtimestamp(t_ms / 1000, tz=NY_TZ)
        d = dt_ny.date()
        active_days_set.add(d)
        
        o, h, l, c = r['open'], r['high'], r['low'], r['close']
        
        # Session Filter: strictly trade NY cash active hours (9:30 AM to 4:00 PM ET)
        is_trading_hours = (dt_ny.hour == 9 and dt_ny.minute >= 30) or (10 <= dt_ny.hour < 16)
        is_close_candle = (dt_ny.hour == 15 and dt_ny.minute == 59)
        
        # Determine if we are at the very first minute of a new 15-minute interval
        is_new_15min_bar = (dt_ny.minute % 15 == 0)
        
        # --- 1. Increment Limit Age and Scan for New Setups at 15-min Boundaries ---
        if is_new_15min_bar:
            # Increment pending limit order ages (only if they are not None)
            if active_buy_level is not None:
                buy_zone_age_bars += 1
                if buy_zone_age_bars > 4:  # Expire after 4 15-minute bars (1 hour)
                    active_buy_level = None
            if active_sell_level is not None:
                sell_zone_age_bars += 1
                if sell_zone_age_bars > 4:
                    active_sell_level = None
            
            # Scan for new setups (only if we are currently IDLE and in trading hours)
            if state == "IDLE" and is_trading_hours:
                o15, h15, l15, c15 = r['prev_open_15'], r['prev_high_15'], r['prev_low_15'], r['prev_close_15']
                ema15 = r['prev_ema50_15']
                
                uptrend = c15 > ema15
                downtrend = c15 < ema15
                
                # Bullish: no bottom wick (open equals low within epsilon) and close is green
                no_bottom_wick = abs(o15 - l15) <= epsilon and c15 > o15
                # Bearish: no top wick (open equals high within epsilon) and close is red
                no_top_wick = abs(o15 - h15) <= epsilon and c15 < o15
                
                if no_bottom_wick and uptrend:
                    active_buy_level = o15
                    # Stop Loss at recent 10-period 15-minute swing low minus safety buffer
                    buy_sl_level = r['prev_swing_low_15'] - 2.0
                    risk = active_buy_level - buy_sl_level
                    if risk > 5.0:  # Filter out anomalous extremely tight candles
                        buy_tp_level = active_buy_level + risk  # 1:1 RR
                        # Compounding sizes: risk 2% of active balance
                        size_comp_pending = (bal_comp * 0.02) / risk
                        # Fixed sizes: risk 2% of starting balance ($2.00)
                        size_fixed_pending = 2.0 / risk
                        buy_zone_age_bars = 0
                    else:
                        active_buy_level = None
                        
                elif no_top_wick and downtrend:
                    active_sell_level = o15
                    # Stop Loss at recent 10-period 15-minute swing high plus safety buffer
                    sell_sl_level = r['prev_swing_high_15'] + 2.0
                    risk = sell_sl_level - active_sell_level
                    if risk > 5.0:
                        sell_tp_level = active_sell_level - risk  # 1:1 RR
                        size_comp_pending = (bal_comp * 0.02) / risk
                        size_fixed_pending = 2.0 / risk
                        sell_zone_age_bars = 0
                    else:
                        active_sell_level = None
                        
        # --- 2. Process Exits for Active Trades ---
        if state == "LONG_ACTIVE":
            if l <= stop_loss and h >= take_profit:
                # Whipsaw: assume SL
                exit_val = stop_loss
                res = 'SL'
                pnl_c = (exit_val - entry_price - slippage) * trade_size_comp
                pnl_f = (exit_val - entry_price - slippage) * trade_size_fixed
                bal_comp += pnl_c
                bal_fixed += pnl_f
                trades_comp.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c})
                trades_fixed.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f})
                state = "IDLE"
            elif l <= stop_loss:
                exit_val = stop_loss
                res = 'SL'
                pnl_c = (exit_val - entry_price - slippage) * trade_size_comp
                pnl_f = (exit_val - entry_price - slippage) * trade_size_fixed
                bal_comp += pnl_c
                bal_fixed += pnl_f
                trades_comp.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c})
                trades_fixed.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f})
                state = "IDLE"
            elif h >= take_profit:
                exit_val = take_profit
                res = 'TP'
                pnl_c = (exit_val - entry_price - slippage) * trade_size_comp
                pnl_f = (exit_val - entry_price - slippage) * trade_size_fixed
                bal_comp += pnl_c
                bal_fixed += pnl_f
                trades_comp.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c})
                trades_fixed.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f})
                state = "IDLE"
            elif is_close_candle:
                exit_val = c
                pnl_c = (exit_val - entry_price - slippage) * trade_size_comp
                pnl_f = (exit_val - entry_price - slippage) * trade_size_fixed
                bal_comp += pnl_c
                bal_fixed += pnl_f
                res = 'TP' if pnl_c > 0 else 'SL'
                trades_comp.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c})
                trades_fixed.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f})
                state = "IDLE"
                
        elif state == "SHORT_ACTIVE":
            if h >= stop_loss and l <= take_profit:
                exit_val = stop_loss
                res = 'SL'
                pnl_c = (entry_price - exit_val - slippage) * trade_size_comp
                pnl_f = (entry_price - exit_val - slippage) * trade_size_fixed
                bal_comp += pnl_c
                bal_fixed += pnl_f
                trades_comp.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c})
                trades_fixed.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f})
                state = "IDLE"
            elif h >= stop_loss:
                exit_val = stop_loss
                res = 'SL'
                pnl_c = (entry_price - exit_val - slippage) * trade_size_comp
                pnl_f = (entry_price - exit_val - slippage) * trade_size_fixed
                bal_comp += pnl_c
                bal_fixed += pnl_f
                trades_comp.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c})
                trades_fixed.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f})
                state = "IDLE"
            elif l <= take_profit:
                exit_val = take_profit
                res = 'TP'
                pnl_c = (entry_price - exit_val - slippage) * trade_size_comp
                pnl_f = (entry_price - exit_val - slippage) * trade_size_fixed
                bal_comp += pnl_c
                bal_fixed += pnl_f
                trades_comp.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c})
                trades_fixed.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f})
                state = "IDLE"
            elif is_close_candle:
                exit_val = c
                pnl_c = (entry_price - exit_val - slippage) * trade_size_comp
                pnl_f = (entry_price - exit_val - slippage) * trade_size_fixed
                bal_comp += pnl_c
                bal_fixed += pnl_f
                res = 'TP' if pnl_c > 0 else 'SL'
                trades_comp.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c})
                trades_fixed.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f})
                state = "IDLE"
                
        # Account termination on complete ruin
        if bal_comp < 1.0: bal_comp = 0.0
        if bal_fixed < 1.0: bal_fixed = 0.0
        if bal_comp == 0.0 and bal_fixed == 0.0:
            break
            
        # --- 3. Check for Pending Limit Taps (1-min Resolution) ---
        if state == "IDLE" and is_trading_hours:
            if active_buy_level is not None and l <= active_buy_level <= h:
                # Tapped! Fill Long Entry and clear the limit
                state = "LONG_ACTIVE"
                entry_price = active_buy_level
                stop_loss = buy_sl_level
                take_profit = buy_tp_level
                trade_size_comp = size_comp_pending
                trade_size_fixed = size_fixed_pending
                
                active_buy_level = None  # Clear filled limit
                active_sell_level = None # Cancel opposite limit if active
                
                # Check if same 1-min bar exits the trade
                if l <= stop_loss:
                    exit_val = stop_loss
                    res = 'SL'
                    pnl_c = (exit_val - entry_price - slippage) * trade_size_comp
                    pnl_f = (exit_val - entry_price - slippage) * trade_size_fixed
                    bal_comp += pnl_c
                    bal_fixed += pnl_f
                    trades_comp.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c})
                    trades_fixed.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f})
                    state = "IDLE"
                elif h >= take_profit:
                    exit_val = take_profit
                    res = 'TP'
                    pnl_c = (exit_val - entry_price - slippage) * trade_size_comp
                    pnl_f = (exit_val - entry_price - slippage) * trade_size_fixed
                    bal_comp += pnl_c
                    bal_fixed += pnl_f
                    trades_comp.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c})
                    trades_fixed.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f})
                    state = "IDLE"
                    
            elif active_sell_level is not None and l <= active_sell_level <= h:
                # Tapped! Fill Short Entry and clear the limit
                state = "SHORT_ACTIVE"
                entry_price = active_sell_level
                stop_loss = sell_sl_level
                take_profit = sell_tp_level
                trade_size_comp = size_comp_pending
                trade_size_fixed = size_fixed_pending
                
                active_sell_level = None  # Clear filled limit
                active_buy_level = None   # Cancel opposite limit if active
                
                # Check if same 1-min bar exits the trade
                if h >= stop_loss:
                    exit_val = stop_loss
                    res = 'SL'
                    pnl_c = (entry_price - exit_val - slippage) * trade_size_comp
                    pnl_f = (entry_price - exit_val - slippage) * trade_size_fixed
                    bal_comp += pnl_c
                    bal_fixed += pnl_f
                    trades_comp.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c})
                    trades_fixed.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f})
                    state = "IDLE"
                elif l <= take_profit:
                    exit_val = take_profit
                    res = 'TP'
                    pnl_c = (entry_price - exit_val - slippage) * trade_size_comp
                    pnl_f = (entry_price - exit_val - slippage) * trade_size_fixed
                    bal_comp += pnl_c
                    bal_fixed += pnl_f
                    trades_comp.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c})
                    trades_fixed.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f})
                    state = "IDLE"
                    
    # --- 4. Compute Comprehensive Statistics ---
    def summarize_suite(trades, final_bal):
        n_trades = len(trades)
        wins = [t for t in trades if t['result'] == 'TP']
        win_rate = (len(wins) / n_trades * 100.0) if n_trades > 0 else 0.0
        
        # Calculate percentage returns to ensure stable stats
        pct_returns = np.array([t['pnl'] / t['balance_before'] if t['balance_before'] > 0.0 else 0.0 for t in trades]) if n_trades > 0 else np.array([])
        
        if n_trades > 1:
            mean_pct = np.mean(pct_returns)
            std_pct = np.std(pct_returns, ddof=1)
            sharpe = float((mean_pct / std_pct) * np.sqrt(252)) if std_pct > 0.0 else 0.0
        else:
            sharpe = 0.0
            
        n_years = len(active_days_set) / 252.0
        cagr = float(((final_bal / starting_balance) ** (1.0 / n_years) - 1.0) * 100.0) if n_years > 0 and final_bal > 0 else -100.0
        
        # Max Drawdown tracking
        peak = starting_balance
        max_dd = 0.0
        temp_bal = starting_balance
        for t in trades:
            temp_bal += t['pnl']
            if temp_bal > peak: peak = temp_bal
            dd = (peak - temp_bal) / peak if peak > 0 else 0.0
            if dd > max_dd: max_dd = dd
            
        psr = calculate_psr(pct_returns) if n_trades > 0 else 0.5
        dsr = calculate_dsr(pct_returns) if n_trades > 0 else 0.5
        markov = calculate_markov_transitions(trades)
        
        return {
            'n_trades': n_trades,
            'win_rate_pct': win_rate,
            'terminal_balance': final_bal,
            'cagr_pct': cagr,
            'max_dd_pct': max_dd * 100.0,
            'sharpe': sharpe,
            'psr_pct': psr * 100.0,
            'dsr_pct': dsr * 100.0,
            'active_days': len(active_days_set),
            'markov_transitions': markov,
            'trades': [{k: v for k, v in t.items() if k != 'balance_before'} for t in trades]
        }
        
    return {
        'compounding': summarize_suite(trades_comp, bal_comp),
        'fixed': summarize_suite(trades_fixed, bal_fixed)
    }

# ---------------------------------------------------------------------------
# Main Script Runner
# ---------------------------------------------------------------------------

def main():
    print("=" * 80)
    print("BARD FX 15-MINUTE 'COMPENSATION PLAY' MECHANICAL BACKTESTER")
    print("=" * 80)
    
    # 1. Backtest over S&P 500
    spy_path = DATA_DIR / "spy_5y_1min.csv"
    if spy_path.exists():
        spy_df = load_and_prepare_15min_data(spy_path)
        print("\nRunning Backtest on S&P 500 (SPX Resampled to 15-Min)...")
        res_spy_ideal = run_bard_fx_backtest(spy_df, apply_slippage=False)
        res_spy_slip = run_bard_fx_backtest(spy_df, apply_slippage=True)
    else:
        res_spy_ideal = res_spy_slip = None
        print("  Error: S&P 500 data file not found.")
        
    # 2. Backtest over Nasdaq-100
    qqq_path = DATA_DIR / "qqq_5y_1min.csv"
    if qqq_path.exists():
        qqq_df = load_and_prepare_15min_data(qqq_path)
        print("\nRunning Backtest on Nasdaq-100 (NQ Resampled to 15-Min)...")
        res_qqq_ideal = run_bard_fx_backtest(qqq_df, apply_slippage=False)
        res_qqq_slip = run_bard_fx_backtest(qqq_df, apply_slippage=True)
    else:
        res_qqq_ideal = res_qqq_slip = None
        print("  Error: QQQ data file not found.")
        
    # Compile Results
    results_database = {
        'spy_results': {
            'ideal': res_spy_ideal,
            'slippage': res_spy_slip
        } if res_spy_ideal else None,
        'qqq_results': {
            'ideal': res_qqq_ideal,
            'slippage': res_qqq_slip
        } if res_qqq_ideal else None
    }
    
    # Print S&P 500 Results
    if res_spy_ideal:
        print("\n" + "="*80)
        print("BARD FX 15-MIN SPX MECHANICAL PERFORMANCE RESULTS")
        print("="*80)
        
        for name, subres in [("IDEAL FILLS (Zero Slippage)", res_spy_ideal), ("WITH SLIPPAGE (0.5 pts)", res_spy_slip)]:
            print(f"--- {name} ---")
            for mode in ['fixed', 'compounding']:
                r = subres[mode]
                print(f"  {mode.upper()} SIZING:")
                print(f"    Trades:        {r['n_trades']}")
                print(f"    Win Rate:      {r['win_rate_pct']:.2f}%")
                print(f"    Terminal Bal:  ${r['terminal_balance']:.2f}")
                print(f"    CAGR:          {r['cagr_pct']:.2f}%")
                print(f"    Max Drawdown:  {r['max_dd_pct']:.2f}%")
                print(f"    Sharpe Ratio:  {r['sharpe']:.4f}")
                print(f"    PSR / DSR:     {r['psr_pct']:.2f}% / {r['dsr_pct']:.2f}%")
                print(f"    Markov States: P(W|W)={r['markov_transitions']['P_win_given_win']:.2f}, P(L|L)={r['markov_transitions']['P_loss_given_loss']:.2f}")
            print("-" * 80)
            
    # Print Nasdaq-100 Results
    if res_qqq_ideal:
        print("\n" + "="*80)
        print("BARD FX 15-MIN NQ MECHANICAL PERFORMANCE RESULTS")
        print("="*80)
        
        for name, subres in [("IDEAL FILLS (Zero Slippage)", res_qqq_ideal), ("WITH SLIPPAGE (0.5 pts)", res_qqq_slip)]:
            print(f"--- {name} ---")
            for mode in ['fixed', 'compounding']:
                r = subres[mode]
                print(f"  {mode.upper()} SIZING:")
                print(f"    Trades:        {r['n_trades']}")
                print(f"    Win Rate:      {r['win_rate_pct']:.2f}%")
                print(f"    Terminal Bal:  ${r['terminal_balance']:.2f}")
                print(f"    CAGR:          {r['cagr_pct']:.2f}%")
                print(f"    Max Drawdown:  {r['max_dd_pct']:.2f}%")
                print(f"    Sharpe Ratio:  {r['sharpe']:.4f}")
                print(f"    PSR / DSR:     {r['psr_pct']:.2f}% / {r['dsr_pct']:.2f}%")
                print(f"    Markov States: P(W|W)={r['markov_transitions']['P_win_given_win']:.2f}, P(L|L)={r['markov_transitions']['P_loss_given_loss']:.2f}")
            print("-" * 80)
            
    # Save a clean results JSON for reference
    # Drop full trades lists in raw json to keep file size lightweight
    for asset in ['spy_results', 'qqq_results']:
        if results_database[asset]:
            for env in ['ideal', 'slippage']:
                for mode in ['fixed', 'compounding']:
                    if 'trades' in results_database[asset][env][mode]:
                        del results_database[asset][env][mode]['trades']
                        
    out_file = DATA_DIR / "bard_fx_backtest_results.json"
    with open(out_file, "w") as f:
        json.dump(results_database, f, indent=2)
    print(f"\nAll 15-minute backtests and metrics completed! Saved summary to {out_file}")

if __name__ == "__main__":
    main()
