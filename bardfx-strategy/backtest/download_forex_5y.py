#!/usr/bin/env python3
"""
Forex 5.4-Year 1-Minute Historical Data Downloader
==================================================
Uses the histdata package to download 5.4 years (2021 - 2026) of 1-minute historical data for:
EURUSD, USDJPY, AUDUSD, USDCAD, USDCHF, NZDUSD.
Extracts, formats, and compiles each into a clean, continuous CSV file.
Saves to `/config/bardfx-strategy/data/`
"""

import os
import sys
import time
import zipfile
import glob
import pandas as pd
from pathlib import Path
from histdata import download_hist_data as dl
from histdata.api import Platform as P, TimeFrame as TF

DATA_DIR = Path("/config/bardfx-strategy/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

PAIRS = ["eurusd", "usdjpy", "audusd", "usdcad", "usdchf", "nzdusd"]
YEARS = ["2021", "2022", "2023", "2024", "2025"]
MONTHS_2026 = ["1", "2", "3", "4", "5"]

def compile_pair(pair):
    print(f"\n=============================================================")
    print(f"COMPILING DATA FOR {pair.upper()}")
    print("=============================================================")
    
    dfs = []
    zip_paths = []
    
    # 1. Download and load yearly chunks (2021 - 2025)
    for year in YEARS:
        print(f"Downloading {pair.upper()} for year {year}...")
        try:
            # The dl function writes the zip file to the current directory (which is /config)
            zip_path = dl(year=year, month=None, pair=pair, platform=P.GENERIC_ASCII, time_frame=TF.ONE_MINUTE)
            if zip_path and os.path.exists(zip_path):
                print(f"  Successfully downloaded: {zip_path}")
                zip_paths.append(zip_path)
                df_chunk = load_zip_chunk(zip_path)
                if df_chunk is not None:
                    dfs.append(df_chunk)
            else:
                print(f"  Warning: Download failed or zip file not found for year {year}")
        except Exception as e:
            print(f"  Error downloading year {year}: {e}")
        time.sleep(2.0)  # Politeness delay to avoid rate-limiting
        
    # 2. Download and load monthly chunks for 2026 (1 - 5)
    for month in MONTHS_2026:
        print(f"Downloading {pair.upper()} for 2026-0{month}...")
        try:
            zip_path = dl(year='2026', month=month, pair=pair, platform=P.GENERIC_ASCII, time_frame=TF.ONE_MINUTE)
            if zip_path and os.path.exists(zip_path):
                print(f"  Successfully downloaded: {zip_path}")
                zip_paths.append(zip_path)
                df_chunk = load_zip_chunk(zip_path)
                if df_chunk is not None:
                    dfs.append(df_chunk)
            else:
                print(f"  Warning: Download failed or zip file not found for 2026-0{month}")
        except Exception as e:
            print(f"  Error downloading month {month}: {e}")
        time.sleep(2.0)  # Politeness delay
        
    if not dfs:
        print(f"❌ Error: No data loaded for {pair.upper()}")
        return False
        
    # 3. Concatenate and clean Master Dataset
    print(f"Combining all chunks for {pair.upper()}...")
    df_master = pd.concat(dfs, ignore_index=True)
    df_master = df_master.sort_values('timestamp').reset_index(drop=True)
    df_master = df_master.drop_duplicates(subset=['timestamp']).reset_index(drop=True)
    
    out_csv = DATA_DIR / f"{pair}_5y_1min.csv"
    print(f"Saving compiled master dataset to {out_csv}...")
    df_master.to_csv(out_csv, index=False)
    print(f"SUCCESS: Saved {len(df_master):,} rows to {out_csv}")
    
    # 4. Clean up zip files in current directory to save disk space
    print("Cleaning up temporary zip files...")
    for z_path in zip_paths:
        try:
            if os.path.exists(z_path):
                os.remove(z_path)
        except Exception as e:
            print(f"  Error removing zip file {z_path}: {e}")
            
    return True

def load_zip_chunk(zip_path):
    """Unzips the zip file, reads the internal CSV/TXT file with pandas, and returns a formatted DataFrame."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            csv_names = [name for name in z.namelist() if name.endswith('.csv') or name.endswith('.txt')]
            if not csv_names:
                print(f"    Warning: No data file found in {zip_path}")
                return None
                
            csv_name = csv_names[0]
            with z.open(csv_name) as f:
                # HistData ASCII 1-minute format:
                # Format: yyyyMMdd HHmmss;open;high;low;close;volume (no headers)
                df = pd.read_csv(
                    f, 
                    sep=';', 
                    header=None, 
                    names=['timestamp', 'open', 'high', 'low', 'close', 'volume'],
                    dtype={
                        'open': 'float32', 
                        'high': 'float32', 
                        'low': 'float32', 
                        'close': 'float32',
                        'volume': 'int32'
                    }
                )
                # Parse timestamps
                df['timestamp'] = pd.to_datetime(df['timestamp'], format='%Y%m%d %H%M%S')
                df = df.drop_duplicates(subset=['timestamp'])
                print(f"    Loaded {len(df):,} rows from {csv_name}.")
                return df
    except Exception as e:
        print(f"    Error reading zip file {zip_path}: {e}")
        return None

def main():
    print("=============================================================")
    print("FOREX 5.4-YEAR 1-MINUTE DATA ACQUISITION PIPELINE")
    print("=============================================================")
    
    start_time = time.time()
    
    for pair in PAIRS:
        compile_pair(pair)
        time.sleep(2.0)
        
    print("\n=============================================================")
    print(f"ALL FOREX PAIRS COMPILED IN {(time.time() - start_time)/60:.1f} MINUTES")
    print("=============================================================")

if __name__ == "__main__":
    main()
