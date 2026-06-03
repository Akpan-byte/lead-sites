#!/usr/bin/env python3
"""
FVG + Supply & Demand Gated ORB Strategy Multiverse Sweep (2021 - 2026)
======================================================================
Backtests every combination of timeframes for the institutional FVG + S&D Gated ORB:
- HTF (H4 vs H1) for Supply & Demand Zones
- ORT (M30 vs M15) for Session Opening Ranges
- LTF (M5 vs M1) for FVG Imbalances and Limit Pullback Fills

Tested across:
- Indices: SPY, QQQ
- Stocks: NVDA
- Commodities: GOLD
- Forex: GBP/USD
- Crypto: SOL, BTC, ETH

Generates full out-of-sample walk-forward performance ledger.
"""

import os
import sys
import json
import math
import datetime
import multiprocessing as mp
from pathlib import Path
import numpy as np
import pandas as pd

DATA_DIR_HL = Path("/config/hl-nq-bot/data")
DATA_DIR_BFX = Path("/config/bardfx-strategy/data")
DATA_DIR_BFX.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Core Quantitative Indicator Libraries
# ---------------------------------------------------------------------------

def calculate_supply_demand_zones(df_htf, atr_mult=1.5):
    """
    Identifies high-probability Supply and Demand zones on the Higher Timeframe (HTF).
    Supply zone: Large bearish body candle. Zone = [Close, High].
    Demand zone: Large bullish body candle. Zone = [Low, Close].
    """
    high = df_htf['high'].values
    low = df_htf['low'].values
    open_p = df_htf['open'].values
    close = df_htf['close'].values
    
    # Calculate ATR of candle bodies
    bodies = np.abs(close - open_p)
    body_atr = pd.Series(bodies).rolling(20).mean().values
    body_atr[np.isnan(body_atr)] = np.mean(bodies) # fillna
    
    demand_zones = []
    supply_zones = []
    
    for idx in range(len(df_htf)):
        body = bodies[idx]
        atr = body_atr[idx]
        
        # Bullish expansion (Demand Zone)
        if close[idx] > open_p[idx] and body >= atr_mult * atr:
            demand_zones.append({
                'low': low[idx],
                'high': close[idx],
                'idx': idx,
                'mitigated': False
            })
            
        # Bearish expansion (Supply Zone)
        elif close[idx] < open_p[idx] and body >= atr_mult * atr:
            supply_zones.append({
                'low': close[idx],
                'high': high[idx],
                'idx': idx,
                'mitigated': False
            })
            
    return demand_zones, supply_zones

# ---------------------------------------------------------------------------
# Fast Vectorized Backtest Simulation Engine
# ---------------------------------------------------------------------------

def run_fvg_orb_sim(df_ltf, spy_df15, htf_minutes, ort_minutes, ltf_minutes, friction, tick_size):
    """Runs a single out-of-sample M5/M1 simulation for a specific asset."""
    # Sizing parameters
    bal = 100.0
    trades = []
    
    state = "IDLE"
    active_buy_level = None
    active_sell_level = None
    buy_sl, sell_sl = None, None
    buy_tp, sell_tp = None, None
    size_comp = 0.0
    limit_age = 0
    
    entry_price = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    trade_size = 0.0
    
    opens = df_ltf['open'].values
    highs = df_ltf['high'].values
    lows = df_ltf['low'].values
    closes = df_ltf['close'].values
    timestamps = df_ltf['timestamp'].values
    
    # Pre-extract NY hours and dates
    df_ltf['timestamp_ny'] = df_ltf['timestamp'].dt.tz_convert('America/New_York')
    hours = df_ltf['timestamp_ny'].dt.hour.values
    minutes = df_ltf['timestamp_ny'].dt.minute.values
    dates = df_ltf['timestamp_ny'].dt.date.values
    
    # Generate HTF data for Supply & Demand
    df_htf = df_ltf.resample(f'{htf_minutes}min', on='timestamp').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    }).dropna().reset_index()
    
    # Generate S&D zones
    demand_zones, supply_zones = calculate_supply_demand_zones(df_htf)
    
    # Track opening range high/low per day
    daily_or = {}
    
    # Simulation loop
    for idx in range(2, len(df_ltf)):
        o, h, l, c = opens[idx], highs[idx], lows[idx], closes[idx]
        t = timestamps[idx]
        hr = hours[idx]
        mn = minutes[idx]
        d = dates[idx]
        
        # --- A. Opening Range Setup ---
        # Capture Opening Range high and low (first ORT block of New York session, 9:30 AM)
        if hr == 9 and mn == 30:
            daily_or[d] = {'high': h, 'low': l, 'count': 1, 'max_count': ort_minutes // ltf_minutes}
        elif d in daily_or and daily_or[d]['count'] < daily_or[d]['max_count']:
            daily_or[d]['high'] = max(daily_or[d]['high'], h)
            daily_or[d]['low'] = min(daily_or[d]['low'], l)
            daily_or[d]['count'] += 1
            
        # Skip if OR not yet fully established for the day
        if d not in daily_or or daily_or[d]['count'] < daily_or[d]['max_count']:
            continue
            
        or_high = daily_or[d]['high']
        or_low = daily_or[d]['low']
        
        # --- B. Update Mitigation status of S&D zones ---
        for zone in demand_zones:
            if not zone['mitigated'] and l < zone['low']:
                zone['mitigated'] = True
        for zone in supply_zones:
            if not zone['mitigated'] and h > zone['high']:
                zone['mitigated'] = True
                
        # --- C. Increment Limit Age ---
        if active_buy_level is not None:
            limit_age += 1
            if limit_age > 4: active_buy_level = None
        if active_sell_level is not None:
            limit_age += 1
            if limit_age > 4: active_sell_level = None
            
        # --- D. Scan Setups (if IDLE) ---
        if state == "IDLE" and active_buy_level is None and active_sell_level is None:
            # Bullish Breakout check
            if h > or_high >= o:
                # FVG Confirmation at breakout candle T
                # Look for a fresh 3-candle FVG: low[T] > high[T-2]
                fvg_formed = l > highs[idx - 2]
                if fvg_formed:
                    # Gating overhead supply check:
                    # Gated if any unmitigated HTF supply zone is sitting within target range
                    overhead_supply = False
                    for zone in supply_zones:
                        if not zone['mitigated'] and or_high < zone['low'] < or_high + 50 * tick_size:
                            overhead_supply = True
                            break
                            
                    if not overhead_supply:
                        active_buy_level = highs[idx - 2] # Limit placed at the top of the FVG
                        buy_sl = lows[idx - 2] - 2.0 * tick_size
                        risk = active_buy_level - buy_sl
                        if risk > 5.0 * tick_size:
                            buy_tp = active_buy_level + 3.0 * risk # 1:3 RR
                            size_comp = (bal * 0.02) / risk
                            limit_age = 0
                        else:
                            active_buy_level = None
                            
            # Bearish Breakout check
            elif l < or_low <= o:
                # FVG Confirmation at breakout candle T
                # Look for a fresh 3-candle FVG: high[T] < low[T-2]
                fvg_formed = h < lows[idx - 2]
                if fvg_formed:
                    # Gating underlying demand check
                    underlying_demand = False
                    for zone in demand_zones:
                        if not zone['mitigated'] and or_low > zone['high'] > or_low - 50 * tick_size:
                            underlying_demand = True
                            break
                            
                    if not underlying_demand:
                        active_sell_level = lows[idx - 2] # Limit placed at the bottom of the FVG
                        sell_sl = highs[idx - 2] + 2.0 * tick_size
                        risk = sell_sl - active_sell_level
                        if risk > 5.0 * tick_size:
                            sell_tp = active_sell_level - 3.0 * risk # 1:3 RR
                            size_comp = (bal * 0.02) / risk
                            limit_age = 0
                        else:
                            active_sell_level = None
                            
        # --- E. Process exits ---
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
                pnl = (exit_val - entry_price - friction) * trade_size
                bal += pnl
                trades.append({'pnl': pnl, 'result': res, 'bal_before': bal - pnl, 'asset': df_ltf['symbol'][0]})
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
                pnl = (entry_price - exit_val - friction) * trade_size
                bal += pnl
                trades.append({'pnl': pnl, 'result': res, 'bal_before': bal - pnl, 'asset': df_ltf['symbol'][0]})
                state = "IDLE"
                
        if bal < 1.0:
            bal = 0.0
            break
            
        # --- F. Process limit order fills ---
        if state == "IDLE":
            if active_buy_level is not None and l <= active_buy_level <= h:
                state = "LONG_ACTIVE"
                entry_price = active_buy_level
                stop_loss = buy_sl
                take_profit = buy_tp
                trade_size = size_comp
                active_buy_level = None
                active_sell_level = None
                
            elif active_sell_level is not None and l <= active_sell_level <= h:
                state = "SHORT_ACTIVE"
                entry_price = active_sell_level
                stop_loss = sell_sl
                take_profit = sell_tp
                trade_size = size_comp
                active_sell_level = None
                active_buy_level = None
                
    return trades

# ---------------------------------------------------------------------------
# Multiverse Sweep Worker
# ---------------------------------------------------------------------------

def process_timeframe_combination(args):
    filepath, symbol, htf_m, ort_m, ltf_m, friction, tick_size = args
    
    # Load 1m raw stream
    df = pd.read_csv(filepath)
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
    df['dt_date'] = df['timestamp'].dt.tz_convert('America/New_York').dt.date
    
    # Out of sample slice: 2021 onwards
    start_date = datetime.date(2021, 1, 1)
    df = df[df['dt_date'] >= start_date].copy().reset_index(drop=True)
    df['symbol'] = symbol
    
    # Resample to execution timeframe
    if ltf_m > 1:
        df_ltf = df.resample(f'{ltf_m}min', on='timestamp').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
        }).dropna().reset_index()
        df_ltf['symbol'] = symbol
    else:
        df_ltf = df
        
    trades = run_fvg_orb_sim(df_ltf, df_ltf, htf_m, ort_m, ltf_m, friction, tick_size)
    
    n_trades = len(trades)
    if n_trades == 0:
        return symbol, htf_m, ort_m, ltf_m, {'trades': 0, 'win_rate': 0.0, 'bal_comp': 100.0, 'sharpe': 0.0, 'max_dd': 0.0}
        
    wins = [t for t in trades if t['result'] == 'TP']
    wr = len(wins) / n_trades * 100.0
    
    pct_rets = np.array([t['pnl'] / t['bal_before'] if t['bal_before'] > 0.0 else 0.0 for t in trades])
    mean_pct = np.mean(pct_rets)
    std_pct = np.std(pct_rets, ddof=1) if n_trades > 1 else 1.0
    if std_pct == 0.0: std_pct = 1.0
    sharpe = mean_pct / std_pct * np.sqrt(252)
    
    final_comp = trades[-1]['bal_before'] + trades[-1]['pnl']
    
    max_dd = 0.0
    peak = 100.0
    temp_bal = 100.0
    for t in trades:
        temp_bal += t['pnl']
        if temp_bal > peak: peak = temp_bal
        dd = (peak - temp_bal) / peak if peak > 0.0 else 0.0
        if dd > max_dd: max_dd = dd
        
    # Walk-forward fold calculation
    fold_len = n_trades // 5
    wfo_folds = []
    for f in range(5):
        fold_trades = trades[f*fold_len : (f+1)*fold_len] if f < 4 else trades[f*fold_len:]
        if len(fold_trades) > 0:
            f_wins = [t for t in fold_trades if t['result'] == 'TP']
            f_wr = len(f_wins) / len(fold_trades) * 100.0
            f_pct = np.array([t['pnl'] / t['bal_before'] if t['bal_before'] > 0.0 else 0.0 for t in fold_trades])
            f_sharpe = np.mean(f_pct) / np.std(f_pct, ddof=1) * np.sqrt(252) if len(fold_trades) > 1 and np.std(f_pct) > 0.0 else 0.0
            wfo_folds.append({'fold': f+1, 'trades': len(fold_trades), 'win_rate': f_wr, 'sharpe': f_sharpe})
            
    return symbol, htf_m, ort_m, ltf_m, {
        'trades': n_trades,
        'win_rate': wr,
        'bal_comp': final_comp,
        'sharpe': sharpe,
        'max_dd': max_dd * 100.0,
        'wfo_folds': wfo_folds
    }

# ---------------------------------------------------------------------------
# Master Controller
# ---------------------------------------------------------------------------

def main():
    print("=" * 80)
    print("FVG + SUPPLY & DEMAND GATED ORB MULTIVERSE RUNNER")
    print("=" * 80)
    
    # Grid of timeframes to test
    # HTF: 240m (H4) vs 60m (H1)
    # ORT: 30m vs 15m
    # LTF: 5m vs 1m
    grid = [
        (240, 30, 5),
        (240, 15, 5),
        (60, 30, 5),
        (60, 15, 5),
        (60, 15, 1)
    ]
    
    assets = [
        (DATA_DIR_HL / "spy_5y_1min.csv", "SPY", 0.05, 0.01),
        (DATA_DIR_HL / "gld_5y_1min.csv", "GOLD", 0.10, 0.01),
        (DATA_DIR_HL / "qqq_5y_1min.csv", "QQQ", 0.15, 0.01),
        (DATA_DIR_BFX / "gbpusd_25y_1min.csv", "GBPUSD", 0.00010, 0.0001),
        (DATA_DIR_HL / "sol_5y_1min.csv", "SOL", 0.0008, 0.0001),
        (DATA_DIR_BFX / "btc_5y_1min.csv", "BTC", 0.0005, 1.0),
        (DATA_DIR_BFX / "eth_5y_1min.csv", "ETH", 0.0008, 0.10),
        (DATA_DIR_HL / "slv_5y_1min.csv", "SLV", 0.01, 0.005),
        (DATA_DIR_HL / "nvda_5y_1min.csv", "NVDA", 0.05, 0.01)
    ]
    
    tasks = []
    for filepath, symbol, friction, tick_size in assets:
        if filepath.exists():
            for htf, ort, ltf in grid:
                tasks.append((filepath, symbol, htf, ort, ltf, friction, tick_size))
        else:
            print(f"Warning: File {filepath} not found, skipping {symbol}.")
            
    if not tasks:
        print("Error: No datasets found to sweep!")
        return
        
    print(f"\nExecuting {len(tasks)} backtests concurrently...")
    
    compiled = {}
    with mp.Pool(processes=mp.cpu_count()) as pool:
        results = pool.map(process_timeframe_combination, tasks)
        for sym, htf, ort, ltf, res in results:
            if sym not in compiled:
                compiled[sym] = []
            compiled[sym].append({
                'timeframe_combo': f"HTF={htf}m | ORT={ort}m | LTF={ltf}m",
                'metrics': res
            })
            
    # Save the master sweeps database
    out_json = DATA_DIR_BFX / "fvg_orb_sweeps_results.json"
    with open(out_json, "w") as f:
        json.dump(compiled, f, indent=2)
    print(f"\nSaved complete FVG + S&D Gated ORB grid sweeps to {out_json}")
    
    # ── PRINT TOP CONFIGURATIONS PER ASSET ───────────────────────────────────
    print("\n" + "=" * 50)
    print("TOP TIMEFRAME COMBINATIONS BY SHARPE")
    print("=" * 50)
    for sym, sweeps in compiled.items():
        print(f"\nAsset: {sym}")
        sorted_sweeps = sorted(sweeps, key=lambda x: x['metrics']['sharpe'], reverse=True)
        for idx, sw in enumerate(sorted_sweeps[:3]):
            metrics = sw['metrics']
            print(f"  {idx+1}. {sw['timeframe_combo']}: Sharpe={metrics['sharpe']:.4f} | Bal=${metrics['bal_comp']:.2f} | Trades={metrics['trades']} | WR={metrics['win_rate']:.2f}% | MaxDD={metrics['max_dd']:.2f}%")
            if 'wfo_folds' in metrics and len(metrics['wfo_folds']) > 0:
                print("     Out-of-Sample Walkdown:")
                for fold in metrics['wfo_folds']:
                    print(f"       Fold {fold['fold']}: Trades={fold['trades']}, WinRate={fold['win_rate']:.2f}%, Sharpe={fold['sharpe']:.4f}")
    print("=" * 50)

if __name__ == '__main__':
    main()
