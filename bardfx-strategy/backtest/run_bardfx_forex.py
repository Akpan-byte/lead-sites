#!/usr/bin/env python3
"""
Bard FX "Compensation Play" Forex Intraday Backtester
====================================================
Evaluates the 100% mechanical "No-Wick Compensation Play" on:
- GBP/USD (GU 7-Day 1-Min resampled to 15-Min)
- USD/JPY (UJ 7-Day 1-Min resampled to 15-Min)

Features:
- 15-Minute candle resampling for structural setups and 50 EMA trend filters
- 1-Minute tick-resolution execution for limit order taps, fills, SL/TP exits
- Chronologically correct simulation loop eliminating look-ahead entry biases
- Decoupled pending limit and active trade state machine (no state conflicts)
- Standardized position sizing risking exactly 2% of equity based on pips risk
- Dual Session Filters: 24/5 Full Watch vs. London/NY Active Session Focus
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

# Pair configurations
PAIR_CONFIGS = {
    'gbpusd': {
        'name': 'GBP/USD (GU)',
        'csv_name': 'gbpusd_7d_1min.csv',
        'epsilon': 0.00002,     # 0.2 pips tolerance for digital candle wickless opens
        'sl_buffer': 0.0002,    # 2.0 pips stop loss safety buffer
        'slippage': 0.0001,     # 1.0 pip spread/execution friction
        'pip_value': 0.0001,
        'min_risk_pips': 5.0    # 5 pips minimum risk
    },
    'usdjpy': {
        'name': 'USD/JPY (UJ)',
        'csv_name': 'usdjpy_7d_1min.csv',
        'epsilon': 0.02,        # 0.2 pips tolerance for digital candle wickless opens
        'sl_buffer': 0.20,      # 2.0 pips stop loss safety buffer
        'slippage': 0.01,       # 1.0 pip spread/execution friction
        'pip_value': 0.01,
        'min_risk_pips': 5.0
    }
}

# ---------------------------------------------------------------------------
# Data Loader and Resampler
# ---------------------------------------------------------------------------

def load_and_prepare_forex_data(csv_path: Path, pair_key: str) -> pd.DataFrame:
    print(f"Loading data from {csv_path}...")
    df = pd.read_csv(csv_path)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    if df['timestamp'].dt.tz is None:
        df['timestamp'] = df['timestamp'].dt.tz_localize('UTC')
    else:
        df['timestamp'] = df['timestamp'].dt.tz_convert('UTC')
    df = df.sort_values('timestamp').reset_index(drop=True)
    
    df['timestamp_ms'] = (df['timestamp'].astype('int64') // 10**6)
    
    # Floor to 15-minute intervals for setups
    df['timestamp_15'] = df['timestamp'].dt.floor('15min')
    
    # Resample to 15-minute bars to calculate technicals
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
    
    # Shift 15-minute technicals by 1 bar to prevent look-ahead bias
    df_15['prev_open_15'] = df_15['open'].shift(1)
    df_15['prev_high_15'] = df_15['high'].shift(1)
    df_15['prev_low_15'] = df_15['low'].shift(1)
    df_15['prev_close_15'] = df_15['close'].shift(1)
    df_15['prev_ema50_15'] = df_15['ema50'].shift(1)
    df_15['prev_swing_low_15'] = df_15['swing_low'].shift(1)
    df_15['prev_swing_high_15'] = df_15['swing_high'].shift(1)
    
    # Merge back to 1-minute
    df_merged = pd.merge(df, df_15[[
        'timestamp_15', 
        'prev_open_15', 'prev_high_15', 'prev_low_15', 'prev_close_15',
        'prev_ema50_15', 'prev_swing_low_15', 'prev_swing_high_15'
    ]], on='timestamp_15', how='left')
    
    return df_merged

# ---------------------------------------------------------------------------
# Forex Backtest Engine
# ---------------------------------------------------------------------------

def run_forex_backtest(df: pd.DataFrame, cfg: dict, active_session_only: bool) -> dict:
    records = df[[
        'timestamp', 'timestamp_ms', 'open', 'high', 'low', 'close', 
        'prev_open_15', 'prev_high_15', 'prev_low_15', 'prev_close_15',
        'prev_ema50_15', 'prev_swing_low_15', 'prev_swing_high_15'
    ]].to_dict('records')
    
    bal_comp = 100.0  # Compounding sizing (2% active risk)
    bal_fixed = 100.0  # Fixed sizing (flat $2.00 risk)
    starting_balance = 100.0
    
    state = "IDLE"  # "IDLE", "LONG_ACTIVE", "SHORT_ACTIVE"
    
    # Limit orders (pending)
    active_buy_level = None
    active_sell_level = None
    buy_sl_level, sell_sl_level = None, None
    buy_tp_level, sell_tp_level = None, None
    size_comp_pending = 0.0
    size_fixed_pending = 0.0
    buy_zone_age_bars = 0
    sell_zone_age_bars = 0
    
    # Decoupled active trade parameters
    entry_price = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    trade_size_comp = 0.0
    trade_size_fixed = 0.0
    
    trades_comp = []
    trades_fixed = []
    active_days_set = set()
    
    epsilon = cfg['epsilon']
    sl_buffer = cfg['sl_buffer']
    slippage = cfg['slippage']
    pip_val = cfg['pip_value']
    min_risk = cfg['min_risk_pips'] * pip_val
    
    for idx, r in enumerate(records):
        if pd.isna(r['prev_ema50_15']) or pd.isna(r['prev_swing_low_15']):
            continue
            
        t_ms = r['timestamp_ms']
        dt_ny = datetime.datetime.fromtimestamp(t_ms / 1000, tz=NY_TZ)
        d = dt_ny.date()
        active_days_set.add(d)
        
        o, h, l, c = r['open'], r['high'], r['low'], r['close']
        
        # Time Filters
        # Forex closes on Friday at 5:00 PM EST (17:00 NY time).
        is_friday_close = (dt_ny.weekday() == 4 and dt_ny.hour == 16 and dt_ny.minute == 59)
        
        if active_session_only:
            # London & NY sessions: 2:00 AM EST to 5:00 PM EST
            is_active_hours = (2 <= dt_ny.hour < 17)
        else:
            # 24/5 watch
            is_active_hours = True
            
        is_new_15min_bar = (dt_ny.minute % 15 == 0)
        
        # --- 1. Increment Limit Age and Scan for New Setups at 15-min Boundaries ---
        if is_new_15min_bar:
            if active_buy_level is not None:
                buy_zone_age_bars += 1
                if buy_zone_age_bars > 4:  # Expire after 1 hour (4 bars)
                    active_buy_level = None
            if active_sell_level is not None:
                sell_zone_age_bars += 1
                if sell_zone_age_bars > 4:
                    active_sell_level = None
            
            # Setup scanner
            if state == "IDLE" and is_active_hours:
                o15, h15, l15, c15 = r['prev_open_15'], r['prev_high_15'], r['prev_low_15'], r['prev_close_15']
                ema15 = r['prev_ema50_15']
                
                uptrend = c15 > ema15
                downtrend = c15 < ema15
                
                no_bottom_wick = abs(o15 - l15) <= epsilon and c15 > o15
                no_top_wick = abs(o15 - h15) <= epsilon and c15 < o15
                
                if no_bottom_wick and uptrend:
                    active_buy_level = o15
                    buy_sl_level = r['prev_swing_low_15'] - sl_buffer
                    risk = active_buy_level - buy_sl_level
                    if risk >= min_risk:
                        buy_tp_level = active_buy_level + risk  # 1:1 RR
                        size_comp_pending = (bal_comp * 0.02) / risk
                        size_fixed_pending = 2.0 / risk
                        buy_zone_age_bars = 0
                    else:
                        active_buy_level = None
                        
                elif no_top_wick and downtrend:
                    active_sell_level = o15
                    sell_sl_level = r['prev_swing_high_15'] + sl_buffer
                    risk = sell_sl_level - active_sell_level
                    if risk >= min_risk:
                        sell_tp_level = active_sell_level - risk  # 1:1 RR
                        size_comp_pending = (bal_comp * 0.02) / risk
                        size_fixed_pending = 2.0 / risk
                        sell_zone_age_bars = 0
                    else:
                        active_sell_level = None
                        
        # --- 2. Process Exits for Active Trades ---
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
                pnl_f = (exit_val - entry_price - slippage) * trade_size_fixed
                bal_comp += pnl_c
                bal_fixed += pnl_f
                trades_comp.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c})
                trades_fixed.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f})
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
                pnl_f = (entry_price - exit_val - slippage) * trade_size_fixed
                bal_comp += pnl_c
                bal_fixed += pnl_f
                trades_comp.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c})
                trades_fixed.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f})
                state = "IDLE"
                
        if bal_comp < 1.0: bal_comp = 0.0
        if bal_fixed < 1.0: bal_fixed = 0.0
        if bal_comp == 0.0 and bal_fixed == 0.0:
            break
            
        # --- 3. Check for Pending Limit Taps (1-min Resolution) ---
        if state == "IDLE" and is_active_hours:
            if active_buy_level is not None and l <= active_buy_level <= h:
                # Tapped! Enter long
                state = "LONG_ACTIVE"
                entry_price = active_buy_level
                stop_loss = buy_sl_level
                take_profit = buy_tp_level
                trade_size_comp = size_comp_pending
                trade_size_fixed = size_fixed_pending
                
                active_buy_level = None
                active_sell_level = None
                
                # Same bar check
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
                # Tapped! Enter short
                state = "SHORT_ACTIVE"
                entry_price = active_sell_level
                stop_loss = sell_sl_level
                take_profit = sell_tp_level
                trade_size_comp = size_comp_pending
                trade_size_fixed = size_fixed_pending
                
                active_sell_level = None
                active_buy_level = None
                
                # Same bar check
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
                    
    # Summarize stats
    def summarize_suite(trades, final_bal):
        n_trades = len(trades)
        wins = [t for t in trades if t['result'] == 'TP']
        win_rate = (len(wins) / n_trades * 100.0) if n_trades > 0 else 0.0
        
        pct_returns = np.array([t['pnl'] / t['balance_before'] if t['balance_before'] > 0.0 else 0.0 for t in trades]) if n_trades > 0 else np.array([])
        
        if n_trades > 1:
            mean_pct = np.mean(pct_returns)
            std_pct = np.std(pct_returns, ddof=1)
            sharpe = float((mean_pct / std_pct) * np.sqrt(252)) if std_pct > 0.0 else 0.0
        else:
            sharpe = 0.0
            
        n_years = len(active_days_set) / 252.0
        cagr = float(((final_bal / starting_balance) ** (1.0 / n_years) - 1.0) * 100.0) if n_years > 0 and final_bal > 0 else -100.0
        
        # Max Drawdown
        peak = starting_balance
        max_dd = 0.0
        temp_bal = starting_balance
        for t in trades:
            temp_bal += t['pnl']
            if temp_bal > peak: peak = temp_bal
            dd = (peak - temp_bal) / peak if peak > 0 else 0.0
            if dd > max_dd: max_dd = dd
            
        markov = calculate_markov_transitions(trades)
        
        return {
            'n_trades': n_trades,
            'win_rate_pct': win_rate,
            'terminal_balance': final_bal,
            'cagr_pct': cagr,
            'max_dd_pct': max_dd * 100.0,
            'sharpe': sharpe,
            'active_days': len(active_days_set),
            'markov_transitions': markov,
            'trades': trades
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
    print("BARD FX FOREX MAJORS HISTORICAL QUANT STUDY (7-DAY 1-MIN)")
    print("=" * 80)
    
    results = {}
    
    for pair_key, cfg in PAIR_CONFIGS.items():
        csv_path = DATA_DIR / cfg['csv_name']
        if not csv_path.exists():
            print(f"Error: {cfg['name']} data file not found at {csv_path}.")
            continue
            
        df = load_and_prepare_forex_data(csv_path, pair_key)
        
        print(f"\nRunning Backtests on {cfg['name']}...")
        
        # 1. 24/5 watch
        res_24_5 = run_forex_backtest(df, cfg, active_session_only=False)
        # 2. London/NY active sessions focus
        res_session = run_forex_backtest(df, cfg, active_session_only=True)
        
        results[pair_key] = {
            '24_5': res_24_5,
            'session_only': res_session
        }
        
        print("\n" + "="*70)
        print(f"BARD FX PERFORMANCE RESULTS: {cfg['name']}")
        print("="*70)
        
        for name, subres in [("24/5 FULL WATCH SCANNING", res_24_5), ("LONDON/NY SESSIONS ONLY", res_session)]:
            print(f"--- {name} ---")
            for mode in ['fixed', 'compounding']:
                r = subres[mode]
                print(f"  {mode.upper()} SIZING:")
                print(f"    Trades:        {r['n_trades']}")
                print(f"    Win Rate:      {r['win_rate_pct']:.2f}%")
                print(f"    Terminal Bal:  ${r['terminal_balance']:.2f}")
                print(f"    Max Drawdown:  {r['max_dd_pct']:.2f}%")
                print(f"    Daily Sharpe:  {r['sharpe']:.4f}")
                print(f"    Markov States: P(W|W)={r['markov_transitions']['P_win_given_win']:.2f}, P(L|L)={r['markov_transitions']['P_loss_given_loss']:.2f}")
            print("-" * 70)
            
    # Save output JSON
    for pk in results.keys():
        for sf in ['24_5', 'session_only']:
            for mode in ['fixed', 'compounding']:
                if 'trades' in results[pk][sf][mode]:
                    del results[pk][sf][mode]['trades']
                    
    out_file = DATA_DIR / "bard_fx_forex_results.json"
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nCompleted all Forex quant studies! Saved summary to {out_file}")

if __name__ == "__main__":
    main()
