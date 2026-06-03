#!/usr/bin/env python3
"""
GBP/USD "Compensation Play" Deep Quantitative Analysis Study
============================================================
Runs the corrected 15-Minute Bard FX Compensation Play strategy on 
7 days of 1-minute GBP/USD historical data. Evaluates:
- Walk-Forward chronological fold stability (5 Splits)
- 10,000-run Monte Carlo simulation (percentiles of final balance & drawdowns)
- Complete Advanced Sharpe Metrics (PSR, DSR, EWSR, Sharpe, Sortino, Calmar)
- Markov sequence streaks and trade metrics.
"""

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
CSV_PATH = DATA_DIR / "gbpusd_7d_1min.csv"

# Pair configurations
CFG = {
    'epsilon': 0.00002,     # 0.2 pips tolerance
    'sl_buffer': 0.0002,    # 2.0 pips stop loss buffer
    'slippage': 0.0001,     # 1.0 pip spread/execution friction
    'pip_value': 0.0001,
    'min_risk_pips': 5.0
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

def load_data():
    print(f"Loading GBP/USD data from {CSV_PATH}...")
    df = pd.read_csv(CSV_PATH)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    if df['timestamp'].dt.tz is None:
        df['timestamp'] = df['timestamp'].dt.tz_localize('UTC')
    else:
        df['timestamp'] = df['timestamp'].dt.tz_convert('UTC')
    df = df.sort_values('timestamp').reset_index(drop=True)
    df['timestamp_ms'] = (df['timestamp'].astype('int64') // 10**6)
    
    # 15-Minute floor
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

def run_simulation(df):
    records = df.to_dict('records')
    
    # Sizing setups
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
    
    epsilon = CFG['epsilon']
    sl_buffer = CFG['sl_buffer']
    slippage = CFG['slippage']
    pip_val = CFG['pip_value']
    min_risk = CFG['min_risk_pips'] * pip_val
    
    for idx, r in enumerate(records):
        if pd.isna(r['prev_ema50_15']) or pd.isna(r['prev_swing_low_15']):
            continue
            
        t_ms = r['timestamp_ms']
        dt_ny = datetime.datetime.fromtimestamp(t_ms / 1000, tz=NY_TZ)
        d = dt_ny.date()
        active_days_set.add(d)
        
        o, h, l, c = r['open'], r['high'], r['low'], r['close']
        
        is_friday_close = (dt_ny.weekday() == 4 and dt_ny.hour == 16 and dt_ny.minute == 59)
        is_new_15min_bar = (dt_ny.minute % 15 == 0)
        
        # --- 1. Limit Expirations and Scanner ---
        if is_new_15min_bar:
            if active_buy_level is not None:
                buy_zone_age_bars += 1
                if buy_zone_age_bars > 4: active_buy_level = None
            if active_sell_level is not None:
                sell_zone_age_bars += 1
                if sell_zone_age_bars > 4: active_sell_level = None
                
            if state == "IDLE":
                o15, h15, l15, c15 = r['prev_open_15'], r['prev_high_15'], r['prev_low_15'], r['prev_close_15']
                ema15 = r['prev_ema50_15']
                
                no_bottom_wick = abs(o15 - l15) <= epsilon and c15 > o15
                no_top_wick = abs(o15 - h15) <= epsilon and c15 < o15
                
                if no_bottom_wick and c15 > ema15:
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
                elif no_top_wick and c15 < ema15:
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
                        
        # --- 2. Process exits ---
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
            elif is_friday_close:
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
        
        # --- 3. Check taps ---
        if state == "IDLE":
            if active_buy_level is not None and l <= active_buy_level <= h:
                state = "LONG_ACTIVE"
                entry_price = active_buy_level
                stop_loss = buy_sl_level
                take_profit = buy_tp_level
                trade_size_comp = size_comp_pending
                trade_size_fixed = size_fixed_pending
                active_buy_level = None
                active_sell_level = None
                
                if l <= stop_loss:
                    pnl_c = (stop_loss - entry_price - slippage) * trade_size_comp
                    pnl_f = (stop_loss - entry_price - slippage) * trade_size_fixed
                    bal_comp += pnl_c
                    bal_fixed += pnl_f
                    trades_comp.append({'side': 'long', 'entry': entry_price, 'exit': stop_loss, 'pnl': pnl_c, 'result': 'SL', 'balance_before': bal_comp - pnl_c, 'date': d})
                    trades_fixed.append({'side': 'long', 'entry': entry_price, 'exit': stop_loss, 'pnl': pnl_f, 'result': 'SL', 'balance_before': bal_fixed - pnl_f, 'date': d})
                    state = "IDLE"
                elif h >= take_profit:
                    pnl_c = (take_profit - entry_price - slippage) * trade_size_comp
                    pnl_f = (take_profit - entry_price - slippage) * trade_size_fixed
                    bal_comp += pnl_c
                    bal_fixed += pnl_f
                    trades_comp.append({'side': 'long', 'entry': entry_price, 'exit': take_profit, 'pnl': pnl_c, 'result': 'TP', 'balance_before': bal_comp - pnl_c, 'date': d})
                    trades_fixed.append({'side': 'long', 'entry': entry_price, 'exit': take_profit, 'pnl': pnl_f, 'result': 'TP', 'balance_before': bal_fixed - pnl_f, 'date': d})
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
                
                if h >= stop_loss:
                    pnl_c = (entry_price - stop_loss - slippage) * trade_size_comp
                    pnl_f = (entry_price - stop_loss - slippage) * trade_size_fixed
                    bal_comp += pnl_c
                    bal_fixed += pnl_f
                    trades_comp.append({'side': 'short', 'entry': entry_price, 'exit': stop_loss, 'pnl': pnl_c, 'result': 'SL', 'balance_before': bal_comp - pnl_c, 'date': d})
                    trades_fixed.append({'side': 'short', 'entry': entry_price, 'exit': stop_loss, 'pnl': pnl_f, 'result': 'SL', 'balance_before': bal_fixed - pnl_f, 'date': d})
                    state = "IDLE"
                elif l <= take_profit:
                    pnl_c = (entry_price - take_profit - slippage) * trade_size_comp
                    pnl_f = (entry_price - take_profit - slippage) * trade_size_fixed
                    bal_comp += pnl_c
                    bal_fixed += pnl_f
                    trades_comp.append({'side': 'short', 'entry': entry_price, 'exit': take_profit, 'pnl': pnl_c, 'result': 'TP', 'balance_before': bal_comp - pnl_c, 'date': d})
                    trades_fixed.append({'side': 'short', 'entry': entry_price, 'exit': take_profit, 'pnl': pnl_f, 'result': 'TP', 'balance_before': bal_fixed - pnl_f, 'date': d})
                    state = "IDLE"
                    
    return trades_comp, trades_fixed, len(active_days_set)

# ---------------------------------------------------------------------------
# Deep Quant Analytics Functions
# ---------------------------------------------------------------------------

def run_monte_carlo(trades, n_simulations: int = 10000) -> dict:
    if not trades: return {}
    
    # For Monte Carlo, we bootsrap the sequence of percentage returns
    pct_returns = np.array([t['pnl'] / t['balance_before'] if t['balance_before'] > 0.0 else 0.0 for t in trades])
    
    rng = np.random.default_rng(seed=42)
    final_balances = []
    max_drawdowns = []
    
    for _ in range(n_simulations):
        sim_returns = rng.choice(pct_returns, size=len(pct_returns), replace=True)
        
        balance = 100.0
        peak = 100.0
        max_dd = 0.0
        
        for r in sim_returns:
            pnl = balance * r
            balance += pnl
            if balance > peak: peak = balance
            dd = (peak - balance) / peak if peak > 0 else 0.0
            if dd > max_dd: max_dd = dd
            
        final_balances.append(balance)
        max_drawdowns.append(max_dd)
        
    final_balances = np.array(final_balances)
    max_drawdowns = np.array(max_drawdowns)
    
    return {
        'P50_balance': float(np.percentile(final_balances, 50)),
        'P90_balance': float(np.percentile(final_balances, 90)),
        'P10_balance': float(np.percentile(final_balances, 10)),
        'P50_drawdown': float(np.percentile(max_drawdowns, 50)) * 100.0,
        'P95_drawdown': float(np.percentile(max_drawdowns, 95)) * 100.0,
        'ruin_rate_pct': float(np.mean(final_balances < 10.0)) * 100.0
    }

def run_walk_forward_stability(trades) -> list:
    # Split the sequence of trades chronologically into 5 folds
    n = len(trades)
    if n < 5: return []
    
    fold_size = n // 5
    folds = []
    
    for i in range(5):
        start_idx = i * fold_size
        end_idx = (i + 1) * fold_size if i < 4 else n
        fold_trades = trades[start_idx:end_idx]
        
        wins = [t for t in fold_trades if t['result'] == 'TP']
        wr = len(wins) / len(fold_trades) * 100.0 if fold_trades else 0.0
        
        pct_rets = np.array([t['pnl'] / t['balance_before'] if t['balance_before'] > 0.0 else 0.0 for t in fold_trades])
        mean_r = np.mean(pct_rets)
        std_r = np.std(pct_rets, ddof=1)
        sr = (mean_r / std_r * np.sqrt(252)) if std_r > 0.0 else 0.0
        
        # Balance path in fold
        bal = 100.0
        for r in pct_rets:
            bal += bal * r
            
        folds.append({
            'fold': i + 1,
            'trades_count': len(fold_trades),
            'win_rate': wr,
            'sharpe': sr,
            'fold_return_pct': (bal - 100.0)
        })
        
    return folds

# ---------------------------------------------------------------------------
# Execution and Reporting
# ---------------------------------------------------------------------------

def main():
    print("=" * 80)
    print("GBP/USD DEEP HISTORICAL QUANT STUDY & RISK SIMULATION")
    print("=" * 80)
    
    df = load_data()
    trades_comp, trades_fixed, n_days = run_simulation(df)
    
    n_trades = len(trades_comp)
    wins = [t for t in trades_comp if t['result'] == 'TP']
    win_rate = len(wins) / n_trades * 100.0 if n_trades > 0 else 0.0
    
    # Calculate percentage returns series
    pct_returns = np.array([t['pnl'] / t['balance_before'] if t['balance_before'] > 0.0 else 0.0 for t in trades_comp])
    
    mean_pct = np.mean(pct_returns) if n_trades > 0 else 0.0
    std_pct = np.std(pct_returns, ddof=1) if n_trades > 1 else 0.0
    sharpe = (mean_pct / std_pct * np.sqrt(252)) if std_pct > 0.0 else 0.0
    
    # Sortino & Calmar
    downside_pct = np.array([r for r in pct_returns if r < 0.0])
    downside_std = np.std(downside_pct, ddof=1) if len(downside_pct) > 1 else 0.0
    sortino = (mean_pct / downside_std * np.sqrt(252)) if downside_std > 0.0 else 0.0
    
    final_bal_comp = trades_comp[-1]['balance_before'] + trades_comp[-1]['pnl'] if trades_comp else 100.0
    final_bal_fixed = trades_fixed[-1]['balance_before'] + trades_fixed[-1]['pnl'] if trades_fixed else 100.0
    
    max_dd = 0.0
    peak = 100.0
    temp_bal = 100.0
    for t in trades_comp:
        temp_bal += t['pnl']
        if temp_bal > peak: peak = temp_bal
        dd = (peak - temp_bal) / peak if peak > 0 else 0.0
        if dd > max_dd: max_dd = dd
        
    calmar = (final_bal_comp - 100.0) / (max_dd * 100.0) if max_dd > 0.0 else 0.0
    
    psr = calculate_psr(pct_returns) * 100.0
    dsr = calculate_dsr(pct_returns) * 100.0
    ewsr = calculate_ewsr(pct_returns) * np.sqrt(252)
    markov = calculate_markov_transitions(trades_comp)
    
    # Run Advanced Simulations
    mc_results = run_monte_carlo(trades_comp)
    wfo_results = run_walk_forward_stability(trades_comp)
    
    print("\n" + "="*80)
    print("DEEP QUANTITATIVE METRICS SUITE: GBP/USD (GU)")
    print("="*80)
    print(f"  Trade Count:          {n_trades}")
    print(f"  Win Rate:             {win_rate:.2f}%")
    print(f"  Terminal Bal (Comp):  ${final_bal_comp:.2f}")
    print(f"  Terminal Bal (Fixed): ${final_bal_fixed:.2f}")
    print(f"  Max Drawdown (Comp):  {max_dd * 100.0:.2f}%")
    print(f"  Daily Sharpe Ratio:   {sharpe:.4f}")
    print(f"  Daily Sortino Ratio:  {sortino:.4f}")
    print(f"  Daily Calmar Ratio:   {calmar:.4f}")
    print(f"  Exponential Sharpe:   {ewsr:.4f}")
    print(f"  Probabilistic Sharpe: {psr:.2f}%")
    print(f"  Deflated Sharpe:      {dsr:.2f}%")
    print(f"  Active Trading Days:  {n_days} days")
    print(f"  Markov Transitions:   P(W|W)={markov['P_win_given_win']:.2f}, P(L|L)={markov['P_loss_given_loss']:.2f}")
    
    print("\n" + "="*80)
    print("10,000-RUN MONTE CARLO RISK SIMULATION RESULTS")
    print("="*80)
    print(f"  P10 Balance (Bear Case):  ${mc_results['P10_balance']:.2f}")
    print(f"  P50 Balance (Base Case):  ${mc_results['P50_balance']:.2f}")
    print(f"  P90 Balance (Bull Case):  ${mc_results['P90_balance']:.2f}")
    print(f"  P50 Max Drawdown:         {mc_results['P50_drawdown']:.2f}%")
    print(f"  P95 Max Drawdown (Tail):  {mc_results['P95_drawdown']:.2f}%")
    print(f"  Ruin Rate (Bankrupt <$10): {mc_results['ruin_rate_pct']:.2f}%")
    
    print("\n" + "="*80)
    print("5-FOLD CHRONOLOGICAL WALK-FORWARD STABILITY Splits")
    print("="*80)
    for f in wfo_results:
        print(f"  Fold {f['fold']}: Trades={f['trades_count']} | Win Rate={f['win_rate']:.2f}% | Sharpe={f['sharpe']:.4f} | Return={f['fold_return_pct']:.2f}%")
    print("="*80)
    
    # Save a comprehensive JSON report for reference
    forex_deep_db = {
        'pair': 'GBPUSD',
        'metrics': {
            'n_trades': n_trades,
            'win_rate': win_rate,
            'final_balance_comp': final_bal_comp,
            'final_balance_fixed': final_bal_fixed,
            'max_dd': max_dd * 100.0,
            'sharpe': sharpe,
            'sortino': sortino,
            'calmar': calmar,
            'ew_sharpe': ewsr,
            'psr': psr,
            'dsr': dsr,
            'active_days': n_days,
            'markov': markov
        },
        'monte_carlo': mc_results,
        'walk_forward_folds': wfo_results
    }
    
    out_file = DATA_DIR / "gbpusd_deep_quant_results.json"
    with open(out_file, "w") as f:
        json.dump(forex_deep_db, f, indent=2)
    print(f"\nSaved deep quantitative report database to {out_file}")

if __name__ == "__main__":
    main()
