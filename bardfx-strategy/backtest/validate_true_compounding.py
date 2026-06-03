#!/usr/bin/env python3
"""
Batched Vectorized Compounding and Quant Suite Validation for Low Timeframes (1m & 5m)
====================================================================================
Runs the single best 5m and 1m configurations, extracts the exact trade lists,
and calculates:
- True dynamic compounding (no scaling artifacts) for 0.5%, 1.0%, and 2.0% risk.
- Robust R-multiple Sharpe Ratio (free from negative balance clamp distortion).
- Probabilistic Sharpe Ratio (PSR).
- 10,000-run batched vectorized Monte Carlo drawdowns and terminal balances.
- Markov win/loss transition states.
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
DATA_DIR = Path("/config/bardfx-strategy/data")
CSV_PATH = DATA_DIR / "gbpusd_25y_1min.csv"

CFG = {
    'sl_buffer': 0.0002,    # 2.0 pips stop loss buffer
    'slippage': 0.0001,     # 1.0 pip spread/execution friction
    'pip_value': 0.0001,
    'min_risk_pips': 3.0    # 3 pips minimum risk for lower timeframes
}

def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def calculate_psr(r_multiples: np.ndarray, benchmark_sr: float = 0.0) -> float:
    n = len(r_multiples)
    if n < 4: return 0.5
    mean_r = np.mean(r_multiples)
    std_r = np.std(r_multiples, ddof=1)
    if std_r == 0.0: return 0.5
    sr = mean_r / std_r
    diffs = r_multiples - mean_r
    skew = np.mean(diffs**3) / (std_r**3) if std_r > 0 else 0.0
    kurt = np.mean(diffs**4) / (std_r**4) if std_r > 0 else 3.0
    variance = (1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr**2) / (n - 1.0)
    if variance <= 0.0: return 0.5
    t_stat = (sr - benchmark_sr) / math.sqrt(variance)
    return normal_cdf(t_stat)

def calculate_markov(trades: list) -> dict:
    if len(trades) < 2:
        return {"P_win_win": 0.0, "P_loss_win": 0.0, "P_win_loss": 0.0, "P_loss_loss": 0.0}
    ww = wl = lw = ll = 0
    win_count = 0
    loss_count = 0
    for i in range(len(trades) - 1):
        curr = trades[i]
        nxt = trades[i+1]
        if curr >= 0.0:
            win_count += 1
            if nxt >= 0.0: ww += 1
            else: wl += 1
        else:
            loss_count += 1
            if nxt >= 0.0: lw += 1
            else: ll += 1
    return {
        "P_win_win": float(ww / win_count) if win_count > 0 else 0.0,
        "P_loss_win": float(wl / win_count) if win_count > 0 else 0.0,
        "P_win_loss": float(lw / loss_count) if loss_count > 0 else 0.0,
        "P_loss_loss": float(ll / loss_count) if loss_count > 0 else 0.0
    }

def run_backtest(tf_minutes: int, session: str, retrace: float, rr: float, wick_type: str, wick_val: float):
    print(f"\nPre-processing {tf_minutes}m data...")
    df = pd.read_csv(CSV_PATH)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    if df['timestamp'].dt.tz is None:
        df['timestamp'] = df['timestamp'].dt.tz_localize('UTC')
    else:
        df['timestamp'] = df['timestamp'].dt.tz_convert('UTC')
        
    if tf_minutes == 1:
        df_tf = df.sort_values('timestamp').reset_index(drop=True)
    else:
        df['timestamp_tf'] = df['timestamp'].dt.floor(f'{tf_minutes}min')
        df_tf = df.groupby('timestamp_tf').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).reset_index()
        df_tf = df_tf.sort_values('timestamp_tf').reset_index(drop=True)
        
    df_tf['ema50'] = df_tf['close'].ewm(span=50, adjust=False).mean()
    df_tf['swing_low'] = df_tf['low'].rolling(10).min()
    df_tf['swing_high'] = df_tf['high'].rolling(10).max()
    
    df_tf['timestamp_ny'] = df_tf['timestamp' if tf_minutes == 1 else 'timestamp_tf'].dt.tz_convert(NY_TZ)
    df_tf['dt_date'] = df_tf['timestamp_ny'].dt.date
    df_tf['dt_hour'] = df_tf['timestamp_ny'].dt.hour
    df_tf['dt_minute'] = df_tf['timestamp_ny'].dt.minute
    df_tf['dt_weekday'] = df_tf['timestamp_ny'].dt.weekday
    
    dates_arr = df_tf['dt_date'].values
    hours_arr = df_tf['dt_hour'].values
    minutes_arr = df_tf['dt_minute'].values
    weekdays_arr = df_tf['dt_weekday'].values
    opens_arr = df_tf['open'].values
    highs_arr = df_tf['high'].values
    lows_arr = df_tf['low'].values
    closes_arr = df_tf['close'].values
    ema50_arr = df_tf['ema50'].values
    swing_lows_arr = df_tf['swing_low'].values
    swing_highs_arr = df_tf['swing_high'].values
    
    valid_mask = ~(np.isnan(ema50_arr) | np.isnan(swing_lows_arr))
    start_idx = int(np.argmax(valid_mask))
    
    sl_buffer = CFG['sl_buffer']
    slippage = CFG['slippage']
    pip_val = CFG['pip_value']
    min_risk = CFG['min_risk_pips'] * pip_val
    
    state = "IDLE"
    active_buy_level = None
    active_sell_level = None
    buy_sl_level, sell_sl_level = None, None
    buy_tp_level, sell_tp_level = None, None
    buy_zone_age_bars = 0
    sell_zone_age_bars = 0
    
    entry_price = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    trade_risk_price = 0.0
    
    r_multiples = []
    
    for idx in range(start_idx, len(opens_arr)):
        hr = hours_arr[idx]
        mn = minutes_arr[idx]
        wkday = weekdays_arr[idx]
        o, h, l, c = opens_arr[idx], highs_arr[idx], lows_arr[idx], closes_arr[idx]
        
        is_friday_close = (wkday == 4 and hr == 16 and mn >= 45)
        
        # Age limit orders
        if active_buy_level is not None:
            buy_zone_age_bars += 1
            if buy_zone_age_bars > 4: active_buy_level = None
        if active_sell_level is not None:
            sell_zone_age_bars += 1
            if sell_zone_age_bars > 4: active_sell_level = None
            
        # Check fills
        if state == "IDLE":
            if active_buy_level is not None and l <= active_buy_level <= h:
                state = "LONG_ACTIVE"
                entry_price = active_buy_level
                stop_loss = buy_sl_level
                take_profit = buy_tp_level
                trade_risk_price = entry_price - stop_loss
                active_buy_level = None
                active_sell_level = None
            elif active_sell_level is not None and l <= active_sell_level <= h:
                state = "SHORT_ACTIVE"
                entry_price = active_sell_level
                stop_loss = sell_sl_level
                take_profit = sell_tp_level
                trade_risk_price = stop_loss - entry_price
                active_sell_level = None
                active_buy_level = None
                
        # Check exits
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
                res = 'CLOSE'
                
            if exit_val is not None:
                pnl = exit_val - entry_price - slippage
                r_mult = pnl / trade_risk_price
                r_multiples.append(r_mult)
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
                res = 'CLOSE'
                
            if exit_val is not None:
                pnl = entry_price - exit_val - slippage
                r_mult = pnl / trade_risk_price
                r_multiples.append(r_mult)
                state = "IDLE"
                
        # Scan setups
        if state == "IDLE" and active_buy_level is None and active_sell_level is None:
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
                        
    return np.array(r_multiples)

def analyze_r_multiples(r_mults, label: str):
    n = len(r_mults)
    if n == 0:
        print(f"No trades for {label}")
        return
        
    wins = r_mults[r_mults > 0.0]
    win_rate = len(wins) / n * 100.0
    
    mean_r = np.mean(r_mults)
    std_r = np.std(r_mults, ddof=1) if n > 1 else 0.0
    
    # Robust R-based Sharpe Ratio
    sr_r = mean_r / std_r if std_r > 0 else 0.0
    
    # True compounding simulation
    def get_comp_bal(risk):
        bal = 100.0
        for r in r_mults:
            bal *= (1.0 + risk * r)
            if bal <= 0.0: return 0.0
        return bal
        
    bal_05 = get_comp_bal(0.005)
    bal_10 = get_comp_bal(0.010)
    bal_20 = get_comp_bal(0.020)
    
    # Monte Carlo (10,000 runs) — Batched Vectorized NumPy to prevent RAM bloat
    print("Running batched vectorized Monte Carlo simulations...")
    np.random.seed(42)
    
    num_runs = 10000
    batch_size = 500
    num_batches = num_runs // batch_size
    
    mc_terminal_bals = []
    max_dds = []
    
    for b in range(num_batches):
        mc_indices = np.random.randint(0, n, size=(batch_size, n))
        samples = r_mults[mc_indices] # shape: (batch_size, n)
        
        # Compute the compound balance paths
        paths = 100.0 * np.cumprod(1.0 + 0.005 * samples, axis=1)
        
        # Append terminal balances
        mc_terminal_bals.extend(paths[:, -1].tolist())
        
        # Vectorized max drawdown:
        peaks = np.maximum.accumulate(paths, axis=1)
        drawdowns = (peaks - paths) / peaks
        batch_max_dds = np.max(drawdowns, axis=1) * 100.0
        max_dds.extend(batch_max_dds.tolist())
        
        # Clean memory explicitly
        del paths
        del peaks
        del drawdowns
        del samples
        
    p50_bal = np.percentile(mc_terminal_bals, 50)
    p95_dd = np.percentile(max_dds, 95)
    
    psr = calculate_psr(r_mults) * 100.0
    markov = calculate_markov(r_mults.tolist())
    
    print(f"\n==================================================")
    print(f"📊 Deep Quantitative Analysis: {label}")
    print(f"==================================================")
    print(f"Trades Count:       {n:,}")
    print(f"Win Rate:           {win_rate:.2f}%")
    print(f"Average R-Multiple: {mean_r:.4f} R")
    print(f"Std Dev of R:       {std_r:.4f} R")
    print(f"Robust R-Sharpe:    {sr_r:.4f}")
    print(f"Prob Sharpe (PSR):  {psr:.2f}%")
    print(f"True Compound 0.5%: ${bal_05:.2f}")
    print(f"True Compound 1.0%: ${bal_10:.2f}")
    print(f"True Compound 2.0%: ${bal_20:.2f}")
    print(f"MC Median Bal (0.5%): ${p50_bal:.2f}")
    print(f"MC 95% Max DD (0.5%): {p95_dd:.2f}%")
    print(f"Markov Probabilities:")
    print(f"  P(Win|Win):       {markov['P_win_win']*100:.2f}%")
    print(f"  P(Loss|Win):      {markov['P_loss_win']*100:.2f}%")
    print(f"  P(Win|Loss):      {markov['P_win_loss']*100:.2f}%")
    print(f"  P(Loss|Loss):     {markov['P_loss_loss']*100:.2f}%")
    
    return {
        'trades': n, 'win_rate': win_rate, 'mean_r': mean_r, 'std_r': std_r, 'sr_r': sr_r, 'psr': psr,
        'bal_05': bal_05, 'bal_10': bal_10, 'bal_20': bal_20, 'p50_bal': p50_bal, 'p95_dd': p95_dd,
        'markov': markov
    }

def main():
    # 5-Minute Best Config
    r_mults_5m = run_backtest(5, 'active', 0.50, 1.0, 'proportional', 0.10)
    stats_5m = analyze_r_multiples(r_mults_5m, "5-Minute Best WinRate Configuration")
    
    # 1-Minute Best Config
    r_mults_1m = run_backtest(1, '24/5', 0.0, 1.0, 'proportional', 0.10)
    stats_1m = analyze_r_multiples(r_mults_1m, "1-Minute Best WinRate Configuration")

if __name__ == "__main__":
    main()
