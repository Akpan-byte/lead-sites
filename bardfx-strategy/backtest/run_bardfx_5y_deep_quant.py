#!/usr/bin/env python3
"""
Bard FX 5.4-Year Deep Quantitative Study & Walk-Forward Suite
============================================================
Runs the corrected 15-Minute Bard FX Compensation Play strategy on 
5.4 years of 1-minute historical S&P 500 (SPY) and Nasdaq-100 (QQQ) data.

Computes:
- Chronological 5-Fold Walk-Forward chronological splits
- 10,000-run vectorized Monte Carlo simulations
- Advanced Sharpe metrics: Sharpe, Sortino, Calmar, EWSR, PSR, DSR
- Markov win/loss transition states
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

# Configurations
PAIR_CONFIGS = {
    'spy': {
        'name': 'S&P 500 (SPX)',
        'csv_name': 'spy_5y_1min.csv',
        'epsilon': 0.1,         # 10 cents on 29k scale
        'sl_buffer': 2.0,       # 2 points buffer
        'slippage': 0.5,        # 0.5 points slippage
        'min_risk': 5.0
    },
    'qqq': {
        'name': 'Nasdaq-100 (NQ)',
        'csv_name': 'qqq_5y_1min.csv',
        'epsilon': 0.1,
        'sl_buffer': 2.0,
        'slippage': 0.5,
        'min_risk': 5.0
    }
}

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

def calculate_ewsr(returns: np.ndarray, decay_factor: float = 0.95) -> float:
    n = len(returns)
    if n == 0: return 0.0
    weights = np.array([decay_factor**(n - 1 - i) for i in range(n)])
    sum_weights = np.sum(weights)
    if sum_weights == 0.0: return 0.0
    ew_mean = np.sum(weights * returns) / sum_weights
    ew_var = np.sum(weights * (returns - ew_mean)**2) / sum_weights
    ew_std = math.sqrt(ew_var)
    if ew_std == 0.0: return 0.0
    return ew_mean / ew_std

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

def load_and_prepare_data(csv_path: Path) -> pd.DataFrame:
    print(f"Loading 1-minute data from {csv_path}...")
    df = pd.read_csv(csv_path)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    if df['timestamp'].dt.tz is None:
        df['timestamp'] = df['timestamp'].dt.tz_localize('UTC')
    else:
        df['timestamp'] = df['timestamp'].dt.tz_convert('UTC')
    df = df.sort_values('timestamp').reset_index(drop=True)
    
    # Scale price series to 29174.0 scale
    latest_close = df['close'].iloc[-1]
    scale_factor = 29174.0 / latest_close
    for col in ['open', 'high', 'low', 'close']:
        df[col] = df[col] * scale_factor
        
    df['timestamp_ms'] = (df['timestamp'].astype('int64') // 10**6)
    df['timestamp_15'] = df['timestamp'].dt.floor('15min')
    
    # Resample
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
    
    # Shift to prevent look-ahead
    df_15['prev_open_15'] = df_15['open'].shift(1)
    df_15['prev_high_15'] = df_15['high'].shift(1)
    df_15['prev_low_15'] = df_15['low'].shift(1)
    df_15['prev_close_15'] = df_15['close'].shift(1)
    df_15['prev_ema50_15'] = df_15['ema50'].shift(1)
    df_15['prev_swing_low_15'] = df_15['swing_low'].shift(1)
    df_15['prev_swing_high_15'] = df_15['swing_high'].shift(1)
    
    df_merged = pd.merge(df, df_15[[
        'timestamp_15', 
        'prev_open_15', 'prev_high_15', 'prev_low_15', 'prev_close_15',
        'prev_ema50_15', 'prev_swing_low_15', 'prev_swing_high_15'
    ]], on='timestamp_15', how='left')
    
    return df_merged

# ---------------------------------------------------------------------------
# Backtest Engine
# ---------------------------------------------------------------------------

def run_backtest(df: pd.DataFrame, cfg: dict, apply_slippage: bool) -> dict:
    records = df[[
        'timestamp', 'timestamp_ms', 'open', 'high', 'low', 'close', 
        'prev_open_15', 'prev_high_15', 'prev_low_15', 'prev_close_15',
        'prev_ema50_15', 'prev_swing_low_15', 'prev_swing_high_15'
    ]].to_dict('records')
    
    bal_comp = 100.0
    bal_fixed = 100.0
    starting_balance = 100.0
    
    state = "IDLE"
    active_buy_level = None
    active_sell_level = None
    buy_sl_level, sell_sl_level = None, None
    buy_tp_level, sell_tp_level = None, None
    size_comp_pending = 0.0
    size_fixed_pending = 0.0
    buy_zone_age_bars = 0
    sell_zone_age_bars = 0
    
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
    slippage = cfg['slippage'] if apply_slippage else 0.0
    min_risk = cfg['min_risk']
    
    for idx, r in enumerate(records):
        if pd.isna(r['prev_ema50_15']) or pd.isna(r['prev_swing_low_15']):
            continue
            
        t_ms = r['timestamp_ms']
        dt_ny = datetime.datetime.fromtimestamp(t_ms / 1000, tz=NY_TZ)
        d = dt_ny.date()
        active_days_set.add(d)
        
        o, h, l, c = r['open'], r['high'], r['low'], r['close']
        
        is_trading_hours = (dt_ny.hour == 9 and dt_ny.minute >= 30) or (10 <= dt_ny.hour < 16)
        is_close_candle = (dt_ny.hour == 15 and dt_ny.minute == 59)
        is_new_15min_bar = (dt_ny.minute % 15 == 0)
        
        # --- 1. Increment Limit Age and Scanner ---
        if is_new_15min_bar:
            if active_buy_level is not None:
                buy_zone_age_bars += 1
                if buy_zone_age_bars > 4: active_buy_level = None
            if active_sell_level is not None:
                sell_zone_age_bars += 1
                if sell_zone_age_bars > 4: active_sell_level = None
                
            if state == "IDLE" and is_trading_hours:
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
                        buy_tp_level = active_buy_level + risk
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
                        sell_tp_level = active_sell_level - risk
                        size_comp_pending = (bal_comp * 0.02) / risk
                        size_fixed_pending = 2.0 / risk
                        sell_zone_age_bars = 0
                    else:
                        active_sell_level = None
                        
        # --- 2. Process Exits ---
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
            elif is_close_candle:
                exit_val = c
                res = 'TP' if c > entry_price else 'SL'
                
            if exit_val is not None:
                pnl_c = (exit_val - entry_price - slippage) * trade_size_comp
                pnl_f = (exit_val - entry_price - slippage) * trade_size_fixed
                bal_comp += pnl_c
                bal_fixed += pnl_f
                trades_comp.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c, 'date': d})
                trades_fixed.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f, 'date': d})
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
            elif is_close_candle:
                exit_val = c
                res = 'TP' if c < entry_price else 'SL'
                
            if exit_val is not None:
                pnl_c = (entry_price - exit_val - slippage) * trade_size_comp
                pnl_f = (entry_price - exit_val - slippage) * trade_size_fixed
                bal_comp += pnl_c
                bal_fixed += pnl_f
                trades_comp.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c, 'date': d})
                trades_fixed.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f, 'date': d})
                state = "IDLE"
                
        if bal_comp < 1.0: bal_comp = 0.0
        if bal_fixed < 1.0: bal_fixed = 0.0
        if bal_comp == 0.0 and bal_fixed == 0.0:
            break
            
        # --- 3. Check Pending Limit Taps ---
        if state == "IDLE" and is_trading_hours:
            if active_buy_level is not None and l <= active_buy_level <= h:
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
                    trades_comp.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c, 'date': d})
                    trades_fixed.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f, 'date': d})
                    state = "IDLE"
                elif h >= take_profit:
                    exit_val = take_profit
                    res = 'TP'
                    pnl_c = (exit_val - entry_price - slippage) * trade_size_comp
                    pnl_f = (exit_val - entry_price - slippage) * trade_size_fixed
                    bal_comp += pnl_c
                    bal_fixed += pnl_f
                    trades_comp.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c, 'date': d})
                    trades_fixed.append({'side': 'long', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f, 'date': d})
                    state = "IDLE"
                    
            elif active_sell_level is not None and l <= active_sell_level <= h:
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
                    trades_comp.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c, 'date': d})
                    trades_fixed.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f, 'date': d})
                    state = "IDLE"
                elif l <= take_profit:
                    exit_val = take_profit
                    res = 'TP'
                    pnl_c = (entry_price - exit_val - slippage) * trade_size_comp
                    pnl_f = (entry_price - exit_val - slippage) * trade_size_fixed
                    bal_comp += pnl_c
                    bal_fixed += pnl_f
                    trades_comp.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_c, 'result': res, 'balance_before': bal_comp - pnl_c, 'date': d})
                    trades_fixed.append({'side': 'short', 'entry': entry_price, 'exit': exit_val, 'pnl': pnl_f, 'result': res, 'balance_before': bal_fixed - pnl_f, 'date': d})
                    state = "IDLE"
                    
    # Generate stats summary
    def summarize_suite(trades, final_bal):
        n_trades = len(trades)
        wins = [t for t in trades if t['result'] == 'TP']
        win_rate = (len(wins) / n_trades * 100.0) if n_trades > 0 else 0.0
        
        pct_returns = np.array([t['pnl'] / t['balance_before'] if t['balance_before'] > 0.0 else 0.0 for t in trades]) if n_trades > 0 else np.array([])
        
        if n_trades > 1:
            mean_pct = np.mean(pct_returns)
            std_pct = np.std(pct_returns, ddof=1)
            sharpe = float((mean_pct / std_pct) * np.sqrt(252)) if std_pct > 0.0 else 0.0
            
            downside_rets = np.array([r for r in pct_returns if r < 0.0])
            downside_std = np.std(downside_rets, ddof=1) if len(downside_rets) > 1 else 0.0
            sortino = float((mean_pct / downside_std) * np.sqrt(252)) if downside_std > 0.0 else 0.0
        else:
            sharpe = 0.0
            sortino = 0.0
            
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
            
        calmar = cagr / (max_dd * 100.0) if max_dd > 0.0 else 0.0
        
        psr = calculate_psr(pct_returns) if n_trades > 0 else 0.5
        dsr = calculate_dsr(pct_returns) if n_trades > 0 else 0.5
        ewsr = calculate_ewsr(pct_returns) * np.sqrt(252) if n_trades > 0 else 0.0
        markov = calculate_markov_transitions(trades)
        
        # Vectorized 10,000 Monte Carlo Simulation
        mc_results = {}
        if n_trades > 0:
            rng = np.random.default_rng(seed=42)
            sim_returns = rng.choice(pct_returns, size=(10000, n_trades), replace=True)
            growth_factors = 1.0 + sim_returns
            balance_paths = 100.0 * np.cumprod(growth_factors, axis=1)
            balance_paths = np.hstack([np.ones((10000, 1)) * 100.0, balance_paths])
            
            final_balances = balance_paths[:, -1]
            peaks = np.maximum.accumulate(balance_paths, axis=1)
            dds = (peaks - balance_paths) / peaks
            max_dds = np.max(dds, axis=1)
            
            mc_results = {
                'P10_balance': float(np.percentile(final_balances, 10)),
                'P50_balance': float(np.percentile(final_balances, 50)),
                'P90_balance': float(np.percentile(final_balances, 90)),
                'P50_drawdown': float(np.percentile(max_dds, 50)) * 100.0,
                'P95_drawdown': float(np.percentile(max_dds, 95)) * 100.0,
                'ruin_rate_pct': float(np.mean(final_balances < 10.0)) * 100.0
            }
            
        # Chronological 5-Fold Walk-Forward splits
        wfo_results = []
        if n_trades >= 5:
            fold_size = n_trades // 5
            for i in range(5):
                start_idx = i * fold_size
                end_idx = (i + 1) * fold_size if i < 4 else n_trades
                fold_t = trades[start_idx:end_idx]
                
                f_wins = [t for t in fold_t if t['result'] == 'TP']
                f_wr = len(f_wins) / len(fold_t) * 100.0 if fold_t else 0.0
                
                f_pct_rets = np.array([t['pnl'] / t['balance_before'] if t['balance_before'] > 0.0 else 0.0 for t in fold_t])
                f_mean = np.mean(f_pct_rets)
                f_std = np.std(f_pct_rets, ddof=1) if len(fold_t) > 1 else 0.0
                f_sr = (f_mean / f_std * np.sqrt(252)) if f_std > 0.0 else 0.0
                
                f_bal = 100.0
                for r_val in f_pct_rets:
                    f_bal += f_bal * r_val
                    
                wfo_results.append({
                    'fold': i + 1,
                    'trades': len(fold_t),
                    'win_rate': f_wr,
                    'sharpe': f_sr,
                    'return_pct': (f_bal - 100.0)
                })
                
        return {
            'n_trades': n_trades,
            'win_rate_pct': win_rate,
            'terminal_balance': final_bal,
            'cagr_pct': cagr,
            'max_dd_pct': max_dd * 100.0,
            'sharpe': sharpe,
            'sortino': sortino,
            'calmar': calmar,
            'psr_pct': psr * 100.0,
            'dsr_pct': dsr * 100.0,
            'ewsr': ewsr,
            'active_days': len(active_days_set),
            'markov_transitions': markov,
            'monte_carlo': mc_results,
            'wfo_folds': wfo_results
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
    print("BARD FX 5.4-YEAR HISTORICAL DEEP QUANTITATIVE STUDY ENGINE")
    print("=" * 80)
    
    results = {}
    
    for pair_key, cfg in PAIR_CONFIGS.items():
        csv_path = DATA_DIR / cfg['csv_name']
        if not csv_path.exists():
            print(f"Error: {cfg['name']} data file not found at {csv_path}.")
            continue
            
        df = load_and_prepare_data(csv_path)
        
        print(f"\nRunning 5.4-Year Backtests on {cfg['name']}...")
        
        res_ideal = run_backtest(df, cfg, apply_slippage=False)
        res_slip = run_backtest(df, cfg, apply_slippage=True)
        
        results[pair_key] = {
            'ideal': res_ideal,
            'slippage': res_slip
        }
        
        # Print results beautifully
        print("\n" + "="*80)
        print(f"5.4-YEAR DEEP QUANT RESULTS: {cfg['name']}")
        print("="*80)
        
        for name, subres in [("IDEAL FILLS (Zero Slippage)", res_ideal), ("WITH SLIPPAGE (0.5 pts)", res_slip)]:
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
                print(f"    Sortino Ratio: {r['sortino']:.4f}")
                print(f"    Calmar Ratio:  {r['calmar']:.4f}")
                print(f"    PSR / DSR:     {r['psr_pct']:.2f}% / {r['dsr_pct']:.2f}%")
                print(f"    Markov States: P(W|W)={r['markov_transitions']['P_win_given_win']:.2f}, P(L|L)={r['markov_transitions']['P_loss_given_loss']:.2f}")
                
                # Monte Carlo
                if r['monte_carlo']:
                    mc = r['monte_carlo']
                    print(f"    Monte Carlo (Compounding Sizing):")
                    print(f"      P10 Balance (Bear Case):  ${mc['P10_balance']:.2f}")
                    print(f"      P50 Balance (Base Case):  ${mc['P50_balance']:.2f}")
                    print(f"      P90 Balance (Bull Case):  ${mc['P90_balance']:.2f}")
                    print(f"      P50 / P95 Drawdown:       {mc['P50_drawdown']:.2f}% / {mc['P95_drawdown']:.2f}%")
                    print(f"      Ruin Rate (<$10):         {mc['ruin_rate_pct']:.2f}%")
                
                # Walk Forward
                if r['wfo_folds']:
                    print(f"    5-Fold Walk Forward Stability chronological splits:")
                    for f in r['wfo_folds']:
                        print(f"      Fold {f['fold']}: Trades={f['trades']} | WR={f['win_rate']:.2f}% | Sharpe={f['sharpe']:.4f} | Ret={f['return_pct']:.2f}%")
            print("-" * 80)
            
    # Save a clean results JSON for reference
    out_file = DATA_DIR / "bard_fx_5y_deep_quant_results.json"
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nAll 5.4-year historical deep quant studies completed! Saved database to {out_file}")

if __name__ == "__main__":
    main()
