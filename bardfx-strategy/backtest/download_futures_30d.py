#!/usr/bin/env python3
"""
Futures 30-Day 1-Minute Downloader
==================================
Downloads the last 30 days of 1-minute historical data for CME/COMEX futures
from Yahoo Finance using yfinance. 
Saves to `/config/bardfx-strategy/data/futures_30d/`
"""

import os
import sys
import time
import datetime
from pathlib import Path
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "yfinance"])
    import yfinance as yf

DATA_DIR = Path("/config/bardfx-strategy/data/futures_30d")
DATA_DIR.mkdir(parents=True, exist_ok=True)

FUTURES = {
    'GC=F': 'gold_futures_30d_1min.csv',
    'SI=F': 'silver_futures_30d_1min.csv',
    'CL=F': 'crude_futures_30d_1min.csv',
    'NG=F': 'natgas_futures_30d_1min.csv',
    'ZC=F': 'corn_futures_30d_1min.csv',
    'ZW=F': 'wheat_futures_30d_1min.csv',
    'ZS=F': 'soybeans_futures_30d_1min.csv',
    'KC=F': 'coffee_futures_30d_1min.csv',
    'SB=F': 'sugar_futures_30d_1min.csv',
    'PL=F': 'platinum_futures_30d_1min.csv',
    'PA=F': 'palladium_futures_30d_1min.csv',
    'HG=F': 'copper_futures_30d_1min.csv',
    'HO=F': 'heatingoil_futures_30d_1min.csv',
    'RB=F': 'gasoline_futures_30d_1min.csv',
    'ES=F': 'sp500_futures_30d_1min.csv',
    'NQ=F': 'nasdaq_futures_30d_1min.csv',
    'YM=F': 'dow_futures_30d_1min.csv'
}

def main():
    print("=============================================================")
    print("FUTURES 30-DAY 1-MINUTE DATA DOWNLOADER STARTED")
    print("=============================================================")
    
    today = datetime.datetime.utcnow().date()
    
    # Define 7-day intervals to download 30 days (yfinance limits 1m to 7 days per call)
    intervals = []
    current_end = today
    for _ in range(5):
        start = current_end - datetime.timedelta(days=7)
        intervals.append((start, current_end))
        current_end = start
        
    intervals.reverse()
    
    for symbol, filename in FUTURES.items():
        print(f"\nProcessing {symbol} -> {filename}...")
        dfs = []
        for idx, (start, end) in enumerate(intervals):
            print(f"  Fetching chunk #{idx+1}: {start} to {end}...")
            try:
                df_chunk = yf.download(
                    symbol,
                    start=start.strftime('%Y-%m-%d'),
                    end=end.strftime('%Y-%m-%d'),
                    interval="1m",
                    progress=False,
                    threads=False
                )
                if not df_chunk.empty:
                    dfs.append(df_chunk)
                    print(f"    Downloaded {len(df_chunk):,} rows.")
                else:
                    print(f"    Warning: empty chunk received.")
            except Exception as e:
                print(f"    Error downloading chunk: {e}")
            time.sleep(0.5)
            
        if len(dfs) == 0:
            print(f"❌ Error: No data downloaded for {symbol}!")
            continue
            
        df_all = pd.concat(dfs)
        df_all = df_all[~df_all.index.duplicated(keep='first')]
        df_all = df_all.sort_index()
        
        if isinstance(df_all.columns, pd.MultiIndex):
            df_all.columns = df_all.columns.get_level_values(0)
            
        df_all.columns = [col.lower() for col in df_all.columns]
        
        out_file = DATA_DIR / filename
        df_all.index.name = 'timestamp'
        df_all = df_all.reset_index()
        
        df_all.to_csv(out_file, index=False)
        print(f"✅ SUCCESS: Compiled {len(df_all):,} 1-minute bars to {out_file}")
        
    print("\n=============================================================")
    print("FUTURES DOWNLOAD COMPLETE")
    print("=============================================================")

if __name__ == "__main__":
    main()
