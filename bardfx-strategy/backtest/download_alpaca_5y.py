#!/usr/bin/env python3
"""
Alpaca Stock & ETF 5.4-Year 1-Minute Downloader
===============================================
Fetches 5.4 years of 1-minute historical OHLCV data for stocks and commodity ETFs
using Alpaca's Stock Bars API (v2) with IEX feed.
Saves to `/config/bardfx-strategy/data/`
"""

import os
import sys
import time
import requests
import datetime
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

DATA_DIR = Path("/config/bardfx-strategy/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

SYMBOLS = [
    "TSLA", "GOOGL", "META", "MSFT", "AAPL", "DIA", "URA",
    "USO", "UNG", "UHN", "UGA", "CPER", "PPLT", "PALL", 
    "CORN", "SOYB", "WEAT", "JO", "CANE"
]

def download_symbol(symbol, headers, endpoint):
    output_path = DATA_DIR / f"{symbol.lower()}_5y_1min.csv"
    print(f"\nDownloading {symbol} -> {output_path}...")
    
    start_time = "2021-01-01T00:00:00Z"
    end_time = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    params = {
        "symbols": symbol,
        "timeframe": "1Min",
        "start": start_time,
        "end": end_time,
        "limit": 10000,
        "feed": "iex"
    }
    
    all_bars = []
    page_token = None
    request_count = 0
    start_perf = time.time()
    
    try:
        while True:
            if page_token:
                params["page_token"] = page_token
                
            request_count += 1
            response = requests.get(endpoint, headers=headers, params=params, timeout=30)
            
            if response.status_code == 429:
                print("    Rate limit hit. Sleeping for 5s...")
                time.sleep(5)
                continue
                
            response.raise_for_status()
            data = response.json()
            
            bars_dict = data.get("bars", {})
            symbol_bars = bars_dict.get(symbol, []) if bars_dict else []
            
            if not symbol_bars:
                break
                
            all_bars.extend(symbol_bars)
            page_token = data.get("next_page_token")
            
            if not page_token:
                break
                
            # Sleep to avoid hard rate limit (free tier allows 200 requests/minute)
            time.sleep(0.05)
            
        if not all_bars:
            print(f"    Warning: No data returned for {symbol}")
            return False
            
        df = pd.DataFrame(all_bars)
        df = df.rename(columns={
            "t": "timestamp",
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "volume"
        })
        df = df[["timestamp", "open", "high", "low", "close", "volume"]]
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        
        df.to_csv(output_path, index=False)
        print(f"    SUCCESS: Saved {len(df):,} bars to {output_path} in {time.time()-start_perf:.1f}s ({request_count} requests)")
        return True
        
    except Exception as e:
        print(f"    Error downloading {symbol}: {e}")
        return False

def main():
    env_path = Path("/config/hl-nq-bot/.env")
    if not env_path.exists():
        print(f"Error: Environment file not found at {env_path}")
        sys.exit(1)
        
    load_dotenv(dotenv_path=env_path)
    api_key = os.getenv("ALPACA_API_KEY_ID")
    api_secret = os.getenv("ALPACA_API_SECRET_KEY")
    
    if not api_key or not api_secret:
        print("Error: ALPACA credentials are missing!")
        sys.exit(1)
        
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
        "accept": "application/json"
    }
    
    endpoint = "https://data.alpaca.markets/v2/stocks/bars"
    
    print("=============================================================")
    print("ALPACA STOCK & ETF 5.4-YEAR 1-MINUTE DOWNLOADER")
    print("=============================================================")
    
    for symbol in SYMBOLS:
        download_symbol(symbol, headers, endpoint)
        time.sleep(0.5)
        
    print("\n=============================================================")
    print("ALL ALPACA DOWNLOADS COMPLETED")
    print("=============================================================")

if __name__ == "__main__":
    main()
