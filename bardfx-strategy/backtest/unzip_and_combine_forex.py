#!/usr/bin/env python3
"""
HistData ZIP Unzipper & Master Forex Dataset Compiler
=====================================================
Sequentially unzips all GBP/USD 1-minute ZIP archives in /config/Desktop/,
parses their semicolon-separated ASCII fields, sorts them chronologically,
and compiles a clean, continuous 25-year master 1-minute historical CSV.
"""

import os
import glob
import zipfile
import io
import pandas as pd
from pathlib import Path

DESKTOP_DIR = Path("/config/Desktop")
OUTPUT_DIR = Path("/config/bardfx-strategy/data")
OUTPUT_DIR.mkdir(exist_ok=True)

def main():
    print("=" * 80)
    # Search for all GBP/USD zip files
    zip_files = sorted(glob.glob(str(DESKTOP_DIR / "*GBPUSD*.zip")))
    if not zip_files:
        print("Error: No GBPUSD ZIP files found in /config/Desktop/!")
        return
        
    print(f"Found {len(zip_files)} ZIP archives to process.")
    print("=" * 80)
    
    dfs = []
    
    for idx, z_path in enumerate(zip_files):
        filename = os.path.basename(z_path)
        print(f"[{idx+1}/{len(zip_files)}] Unzipping & loading {filename}...")
        
        try:
            with zipfile.ZipFile(z_path) as z:
                # Find the .csv file in the zip namelist
                csv_names = [name for name in z.namelist() if name.endswith('.csv')]
                if not csv_names:
                    print(f"  Warning: No CSV file found in {filename}, skipping.")
                    continue
                    
                csv_name = csv_names[0]
                with z.open(csv_name) as f:
                    # Read CSV (semicolon separated, no header)
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
                    
                    # Convert timestamp string to datetime quickly
                    df['timestamp'] = pd.to_datetime(df['timestamp'], format='%Y%m%d %H%M%S')
                    
                    # Drop duplicates
                    df = df.drop_duplicates(subset=['timestamp'])
                    
                    dfs.append(df)
                    print(f"  Successfully loaded {len(df):,} rows.")
        except Exception as e:
            print(f"  Error processing {filename}: {e}")
            
    if not dfs:
        print("Error: No data successfully loaded.")
        return
        
    print("\n" + "="*80)
    print("CONCATENATING AND CLEANING MASTER 25-YEAR FOREX DATASET...")
    print("="*80)
    
    df_master = pd.concat(dfs, ignore_index=True)
    print(f"Unsorted master rows: {len(df_master):,}")
    
    # Sort chronologically and drop any overlapping duplicates
    df_master = df_master.sort_values('timestamp').reset_index(drop=True)
    df_master = df_master.drop_duplicates(subset=['timestamp']).reset_index(drop=True)
    
    print(f"Pruned & sorted master rows: {len(df_master):,}")
    
    out_csv = OUTPUT_DIR / "gbpusd_25y_1min.csv"
    print(f"Saving compiled master dataset to {out_csv}...")
    df_master.to_csv(out_csv, index=False)
    print(f"Success! Master 25-year GBP/USD 1-minute historical dataset saved. File size: {out_csv.stat().st_size / (1024*1024):.2f} MB")
    print("=" * 80)

if __name__ == "__main__":
    main()
