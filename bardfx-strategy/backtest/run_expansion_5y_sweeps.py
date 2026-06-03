#!/usr/bin/env python3
"""
Multi-Asset 5.4-Year Master Expansion Backtest sweeps
=====================================================
Runs the core strategies from unified_strategy_stack.py on the 5.4-year M1 stock,
ETF, and forex datasets, aggregates metrics, and compiles a master quant report.
"""

import os
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
    InversedOCOMeanReversion
)

DATA_DIR_BFX = Path("/config/bardfx-strategy/data")
DATA_DIR_HL = Path("/config/hl-nq-bot/data")
OUT_DIR = Path("/config/projects/trading/quant-suite/reports")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# List of assets, their file locations, and specs
ASSETS = {
    # Forex (5.4 Years / 25 Years)
    'GBPUSD': {'path': DATA_DIR_BFX / 'gbpusd_25y_1min.csv', 'tick_size': 0.0001, 'slippage': 0.0001, 'type': 'forex'},
    'EURUSD': {'path': DATA_DIR_BFX / 'eurusd_5y_1min.csv', 'tick_size': 0.0001, 'slippage': 0.0001, 'type': 'forex'},
    'USDJPY': {'path': DATA_DIR_BFX / 'usdjpy_5y_1min.csv', 'tick_size': 0.01, 'slippage': 0.01, 'type': 'forex'},
    'AUDUSD': {'path': DATA_DIR_BFX / 'audusd_5y_1min.csv', 'tick_size': 0.0001, 'slippage': 0.0001, 'type': 'forex'},
    'USDCAD': {'path': DATA_DIR_BFX / 'usdcad_5y_1min.csv', 'tick_size': 0.0001, 'slippage': 0.0001, 'type': 'forex'},
    'USDCHF': {'path': DATA_DIR_BFX / 'usdchf_5y_1min.csv', 'tick_size': 0.0001, 'slippage': 0.0001, 'type': 'forex'},
    'NZDUSD': {'path': DATA_DIR_BFX / 'nzdusd_5y_1min.csv', 'tick_size': 0.0001, 'slippage': 0.0001, 'type': 'forex'},
    
    # Stocks (5.4 Years)
    'TSLA': {'path': DATA_DIR_BFX / 'tsla_5y_1min.csv', 'tick_size': 0.01, 'slippage': 0.01, 'type': 'stock'},
    'GOOGL': {'path': DATA_DIR_BFX / 'googl_5y_1min.csv', 'tick_size': 0.01, 'slippage': 0.01, 'type': 'stock'},
    'META': {'path': DATA_DIR_BFX / 'meta_5y_1min.csv', 'tick_size': 0.01, 'slippage': 0.01, 'type': 'stock'},
    'MSFT': {'path': DATA_DIR_BFX / 'msft_5y_1min.csv', 'tick_size': 0.01, 'slippage': 0.01, 'type': 'stock'},
    'AAPL': {'path': DATA_DIR_BFX / 'aapl_5y_1min.csv', 'tick_size': 0.01, 'slippage': 0.01, 'type': 'stock'},
    
    # Indices (5.4 Years)
    'DIA': {'path': DATA_DIR_BFX / 'dia_5y_1min.csv', 'tick_size': 0.01, 'slippage': 0.01, 'type': 'stock'},
    
    # Commodities & Precious Metals ETFs (5.4 Years)
    'USO': {'path': DATA_DIR_BFX / 'uso_5y_1min.csv', 'tick_size': 0.01, 'slippage': 0.01, 'type': 'stock'},
    'UNG': {'path': DATA_DIR_BFX / 'ung_5y_1min.csv', 'tick_size': 0.01, 'slippage': 0.01, 'type': 'stock'},
    'CORN': {'path': DATA_DIR_BFX / 'corn_5y_1min.csv', 'tick_size': 0.01, 'slippage': 0.01, 'type': 'stock'},
    'WEAT': {'path': DATA_DIR_BFX / 'weat_5y_1min.csv', 'tick_size': 0.01, 'slippage': 0.01, 'type': 'stock'},
    'GLD': {'path': DATA_DIR_HL / 'gld_5y_1min.csv', 'tick_size': 0.01, 'slippage': 0.01, 'type': 'stock'},
    'SLV': {'path': DATA_DIR_HL / 'slv_5y_1min.csv', 'tick_size': 0.01, 'slippage': 0.01, 'type': 'stock'},
    'URA': {'path': DATA_DIR_BFX / 'ura_5y_1min.csv', 'tick_size': 0.01, 'slippage': 0.01, 'type': 'stock'}
}

def calculate_metrics(trades, risk_per_trade=2.0, initial_bal=100.0):
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
    print("MULTI-ASSET 5.4-YEAR MASTER EXPANSION sweeps")
    print("=" * 80)
    
    results = {}
    
    for name, specs in ASSETS.items():
        filepath = specs['path']
        if not filepath.exists():
            print(f"Skipping {name} (File {filepath} not found)...")
            continue
            
        print(f"\nProcessing {name} ({filepath.name})...")
        try:
            df = pd.read_csv(filepath)
            df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
            df = df.sort_values('timestamp').reset_index(drop=True)
            
            # Slice to 2021 onwards to keep identical out-of-sample window
            df['dt_date'] = df['timestamp'].dt.tz_convert('America/New_York').dt.date
            df = df[df['dt_date'] >= pd.Timestamp('2021-01-01').date()].copy().reset_index(drop=True)
            
            if len(df) == 0:
                print(f"  Warning: Empty data after 2021-01-01 filter!")
                continue
                
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
            
            # Run Backtests
            print("  Running Strategy 1 (VP Gated ORB)...")
            t1 = s1.backtest(df.copy(), df_15.copy())
            
            print("  Running Strategy 2 (FVG S&D Gated ORB)...")
            t2 = s2.backtest(df.copy())
            
            print("  Running Strategy 3 (Inversed OCO MR)...")
            t3 = s3.backtest(df.copy())
            
            results[name] = {
                'S1_VP_ORB': calculate_metrics(t1),
                'S2_FVG_SD_ORB': calculate_metrics(t2),
                'S3_Inversed_MR': calculate_metrics(t3)
            }
            
        except Exception as e:
            print(f"  Error backtesting {name}: {e}")
            
    # Write report
    report_path = OUT_DIR / "expansion_5y_master_report.md"
    print(f"\nCompiling report to {report_path}...")
    
    with open(report_path, "w") as f:
        f.write("# 📊 Master 5.4-Year Multi-Asset Expansion Quant Study\n")
        f.write("### Evaluation Period: 2021 – 2026 (1-Minute Tick Resolution with Look-Ahead Immunity)\n\n")
        f.write("This report presents the out-of-sample backtest results for the newly expanded stock, ETF, and forex datasets ")
        f.write("under the Volume Profile Gated ORB (S1), FVG + S&D Gated ORB (S2), and Inversed OCO Mean Reversion (S3) strategies. ")
        f.write("Position sizing is configured at **2.0% compounding risk per trade**.\n\n")
        
        for asset, strats in results.items():
            f.write(f"## 📈 {asset}\n")
            f.write("| Strategy | Trades | Win Rate | Return % | Sharpe | Max DD |\n")
            f.write("| :--- | :---: | :---: | :---: | :---: | :---: |\n")
            for strat_name, metrics in strats.items():
                f.write(f"| {strat_name} | {metrics['trades']} | {metrics['win_rate']}% | {metrics['return_pct']}% | {metrics['sharpe']} | {metrics['max_dd']}% |\n")
            f.write("\n")
            
    print("✅ 5.4-Year Backtests and report generation complete!")

if __name__ == "__main__":
    main()
