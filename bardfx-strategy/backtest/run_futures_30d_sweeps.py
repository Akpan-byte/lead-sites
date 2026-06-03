#!/usr/bin/env python3
"""
Futures 30-Day Master Sweeps Runner
==================================
Runs the 4 core strategies from unified_strategy_stack.py on the 17 CME/COMEX
futures 30-day M1 datasets, aggregates performance, and generates a report.
"""

import sys
import json
import numpy as np
import pandas as pd
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).resolve().parents[1]))
from unified_strategy_stack import (
    VolumeProfileGatedORB,
    FVGSupplyDemandGatedORB,
    InversedOCOMeanReversion,
    NQWideOCOBreakout
)

DATA_DIR = Path("/config/bardfx-strategy/data/futures_30d")
OUT_DIR = Path("/config/projects/trading/quant-suite/reports")
OUT_DIR.mkdir(parents=True, exist_ok=True)

FUTURES_SPECS = {
    'gold_futures_30d_1min.csv': {'name': 'Gold Futures (GC)', 'tick_size': 0.1, 'slippage': 0.1},
    'silver_futures_30d_1min.csv': {'name': 'Silver Futures (SI)', 'tick_size': 0.005, 'slippage': 0.005},
    'crude_futures_30d_1min.csv': {'name': 'Crude Oil (CL)', 'tick_size': 0.01, 'slippage': 0.01},
    'natgas_futures_30d_1min.csv': {'name': 'Natural Gas (NG)', 'tick_size': 0.001, 'slippage': 0.001},
    'copper_futures_30d_1min.csv': {'name': 'Copper Futures (HG)', 'tick_size': 0.0005, 'slippage': 0.0005},
    'platinum_futures_30d_1min.csv': {'name': 'Platinum Futures (PL)', 'tick_size': 0.1, 'slippage': 0.1},
    'palladium_futures_30d_1min.csv': {'name': 'Palladium Futures (PA)', 'tick_size': 0.1, 'slippage': 0.1},
    'corn_futures_30d_1min.csv': {'name': 'Corn Futures (ZC)', 'tick_size': 0.25, 'slippage': 0.25},
    'wheat_futures_30d_1min.csv': {'name': 'Wheat Futures (ZW)', 'tick_size': 0.25, 'slippage': 0.25},
    'soybeans_futures_30d_1min.csv': {'name': 'Soybean Futures (ZS)', 'tick_size': 0.25, 'slippage': 0.25},
    'coffee_futures_30d_1min.csv': {'name': 'Coffee Futures (KC)', 'tick_size': 0.05, 'slippage': 0.05},
    'sugar_futures_30d_1min.csv': {'name': 'Sugar Futures (SB)', 'tick_size': 0.01, 'slippage': 0.01},
    'heatingoil_futures_30d_1min.csv': {'name': 'Heating Oil (HO)', 'tick_size': 0.0001, 'slippage': 0.0001},
    'gasoline_futures_30d_1min.csv': {'name': 'Gasoline (RB)', 'tick_size': 0.0001, 'slippage': 0.0001},
    'sp500_futures_30d_1min.csv': {'name': 'S&P 500 Futures (ES)', 'tick_size': 0.25, 'slippage': 0.25},
    'nasdaq_futures_30d_1min.csv': {'name': 'Nasdaq Futures (NQ)', 'tick_size': 0.25, 'slippage': 0.25},
    'dow_futures_30d_1min.csv': {'name': 'Dow Futures (YM)', 'tick_size': 1.0, 'slippage': 1.0}
}

def calculate_metrics(trades, risk_per_trade=2.0, initial_bal=10000.0):
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "return_pct": 0.0, "sharpe": 0.0, "max_dd": 0.0}
        
    bal = initial_bal
    equity_curve = [bal]
    wins = 0
    pnl_pcts = []
    
    for t in trades:
        pnl_pct = t['pnl_pct']
        pnl_usd = (pnl_pct * (risk_per_trade / 100.0) * bal) if pnl_pct > -1.0 else -bal * (risk_per_trade / 100.0)
        bal += pnl_usd
        equity_curve.append(bal)
        pnl_pcts.append(pnl_pct)
        if t['result'] == 'TP':
            wins += 1
            
    win_rate = (wins / len(trades)) * 100.0
    return_pct = ((bal - initial_bal) / initial_bal) * 100.0
    
    # Calculate Sharpe
    if len(pnl_pcts) > 1:
        mean_ret = np.mean(pnl_pcts)
        std_ret = np.std(pnl_pcts, ddof=1)
        sharpe = (mean_ret / std_ret * np.sqrt(252)) if std_ret > 0 else 0.0
    else:
        sharpe = 0.0
        
    # Calculate Max Drawdown
    equity_curve = np.array(equity_curve)
    peaks = np.maximum.accumulate(equity_curve)
    drawdowns = (peaks - equity_curve) / peaks * 100.0
    max_dd = np.max(drawdowns)
    
    return {
        "trades": len(trades),
        "win_rate": round(win_rate, 2),
        "return_pct": round(return_pct, 2),
        "sharpe": round(sharpe, 4),
        "max_dd": round(max_dd, 2)
    }

def main():
    print("=" * 80)
    print("FUTURES 30-DAY मास्टर QUANT STUDY RUNNER")
    print("=" * 80)
    
    results = {}
    
    for filename, specs in FUTURES_SPECS.items():
        filepath = DATA_DIR / filename
        if not filepath.exists():
            print(f"Skipping {filename} (File not found)...")
            continue
            
        print(f"\nProcessing {specs['name']}...")
        df = pd.read_csv(filepath)
        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
        df = df.sort_values('timestamp').reset_index(drop=True)
        
        tick = specs['tick_size']
        slip = specs['slippage']
        
        # 15m Resampling for Strategy 1
        df_15 = df.resample('15min', on='timestamp').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
        }).dropna().reset_index()
        df_15.rename(columns={'timestamp': 'timestamp_15'}, inplace=True)
        
        # Instantiate Strategies
        s1 = VolumeProfileGatedORB(rr=3.0, slippage=slip, sl_buffer=2.0 * tick, tick_size=tick)
        s2 = FVGSupplyDemandGatedORB(htf_m=60, ort_m=15, ltf_m=1, rr=3.0, slippage=slip, sl_buffer=2.0 * tick, tick_size=tick)
        s3 = InversedOCOMeanReversion(ort_m=15, ltf_m=1, slippage=slip)
        s4 = NQWideOCOBreakout(lookback=5)
        
        # Run Backtests
        print("  Running Strategy 1 (VP Gated ORB)...")
        t1 = s1.backtest(df.copy(), df_15.copy())
        
        print("  Running Strategy 2 (FVG S&D Gated ORB)...")
        t2 = s2.backtest(df.copy())
        
        print("  Running Strategy 3 (Inversed OCO MR)...")
        t3 = s3.backtest(df.copy())
        
        print("  Running Strategy 4 (NQ Wide Breakout)...")
        t4 = s4.backtest(df.copy())
        
        results[specs['name']] = {
            'S1_VP_ORB': calculate_metrics(t1),
            'S2_FVG_SD_ORB': calculate_metrics(t2),
            'S3_Inversed_MR': calculate_metrics(t3),
            'S4_NQ_Wide_BO': calculate_metrics(t4)
        }
        
    # Write report
    report_path = OUT_DIR / "futures_30d_master_report.md"
    print(f"\nCompiling report to {report_path}...")
    
    with open(report_path, "w") as f:
        f.write("# 📊 Master CME/COMEX Futures 30-Day Quantitative Backtest Study\n")
        f.write("### Evaluation Period: Last 30 Days (1-Minute Tick Resolution with Look-Ahead Immunity)\n\n")
        f.write("This report presents the backtest results across 17 CME/COMEX futures contracts for the 4 core strategies. ")
        f.write("Position sizing is configured at **2.0% compounding nominal risk per trade**.\n\n")
        
        for asset, strats in results.items():
            f.write(f"## 📈 {asset}\n")
            f.write("| Strategy | Trades | Win Rate | Return % | Sharpe | Max DD |\n")
            f.write("| :--- | :---: | :---: | :---: | :---: | :---: |\n")
            for strat_name, metrics in strats.items():
                f.write(f"| {strat_name} | {metrics['trades']} | {metrics['win_rate']}% | {metrics['return_pct']}% | {metrics['sharpe']} | {metrics['max_dd']}% |\n")
            f.write("\n")
            
    print("✅ Backtests and report generation complete!")

if __name__ == "__main__":
    main()
