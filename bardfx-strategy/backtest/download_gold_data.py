#!/usr/bin/env python3
"""
High-Precision 1-Minute Gold Data Downloader (yfinance Chunk Downloader)
========================================================================
Downloads the last 30 days of historical 1-minute candle data for Gold Futures (GC=F).
Saves to `/config/bardfx-strategy/data/gold_30d_1min.csv`
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

DATA_DIR = Path("/config/bardfx-strategy/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

SYMBOL = 'GC=F'

def main():
    print("=" * 80)
    print("Gold 30-Day 1-Minute Downloader Started")
    print("=" * 80)
    
    today = datetime.datetime.utcnow().date()
    
    # Define 7-day intervals
    intervals = []
    current_end = today
    for _ in range(5): # 5 chunks of 7 days = 35 days (to safely get 30 calendar days)
        start = current_end - datetime.timedelta(days=7)
        intervals.append((start, current_end))
        current_end = start
        
    intervals.reverse()
    
    dfs = []
    for idx, (start, end) in enumerate(intervals):
        print(f"  Fetching chunk #{idx+1}: {start} to {end}...")
        try:
            df_chunk = yf.download(
                SYMBOL,
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
                print(f"    Warning: empty chunk received for {start} to {end}.")
        except Exception as e:
            print(f"    Error downloading chunk: {e}")
        time.sleep(1)
        
    if len(dfs) == 0:
        print("❌ Error: No Gold data downloaded!")
        sys.exit(1)
        
    df_all = pd.concat(dfs)
    df_all = df_all[~df_all.index.duplicated(keep='first')]
    df_all = df_all.sort_index()
    
    if isinstance(df_all.columns, pd.MultiIndex):
        df_all.columns = df_all.columns.get_level_values(0)
        
    df_all.columns = [col.lower() for col in df_all.columns]
    
    out_file = DATA_DIR / "gold_30d_1min.csv"
    df_all.index.name = 'timestamp'
    df_all = df_all.reset_index()
    
    df_all.to_csv(out_file, index=False)
    print(f"✅ Successfully compiled {len(df_all):,} 1-minute Gold bars to {out_file}")

if __name__ == "__main__":
    main()
