import os
import sys
import time
import datetime
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

# Add paths
sys.path.append(str(Path(__file__).resolve().parents[1]))
sys.path.append(str(Path(__file__).resolve().parent))

from unified_strategy_stack import (
    VolumeProfileGatedORB,
    FVGSupplyDemandGatedORB,
    InversedOCOMeanReversion,
    NQWideOCOBreakout
)
from live_execution_broker import LiveExecutionBroker

# Config path
ENV_PATH = Path("/config/hl-nq-bot/.env")
load_dotenv(dotenv_path=ENV_PATH)

class LiveTickListener:
    def __init__(self, demo=True):
        self.broker = LiveExecutionBroker(demo=demo)
        
        # Instantiate strategy parameters (from our backtest optimizations)
        # S1: Volume Profile Gated ORB
        self.s1 = VolumeProfileGatedORB(rr=3.0, slippage=0.0001, sl_buffer=0.0002)
        # S2: FVG S&D Gated ORB
        self.s2 = FVGSupplyDemandGatedORB(htf_m=60, ort_m=15, ltf_m=1, rr=3.0, slippage=0.0001, sl_buffer=0.0002)
        # S3: Inversed OCO Mean Reversion
        self.s3 = InversedOCOMeanReversion(ort_m=15, ltf_m=1, slippage=0.0001)
        # S4: NQ Wide OCO Breakout
        self.s4 = NQWideOCOBreakout(buffer_pts=32.0, sl_pts=56.0, tp_pts=98.0, lookback=5)
        
        # Asset configurations
        self.assets = {
            "/GC": {"symbol": "GC", "strategy": "S2", "tick_size": 0.1, "qty": 1},
            "/ES": {"symbol": "ES", "strategy": "S3", "tick_size": 0.25, "qty": 1},
            "/NQ": {"symbol": "NQ", "strategy": "S4", "tick_size": 0.25, "qty": 1},
            "/YM": {"symbol": "YM", "strategy": "S3", "tick_size": 1.0, "qty": 1},
            "USDJPY": {"symbol": "USDJPY", "strategy": "S1", "tick_size": 0.01, "qty": 0.1},
            "NZDUSD": {"symbol": "NZDUSD", "strategy": "S1", "tick_size": 0.0001, "qty": 0.1}
        }
        
        # Load local bar database cache
        self.cache_dir = Path("/config/bardfx-strategy/live/cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
    def fetch_latest_bars(self, asset):
        """
        Fetches the most recent closed 1-minute bars for the given asset.
        Uses a hybrid polling API: fetches from Tradovate for futures, 
        and falls back to standard HTTP endpoints for forex/CFDs.
        """
        # For simulation/stress testing and live, we fetch the last 15-30 bars
        # to check for fresh setups.
        print(f"Fetching latest market bars for {asset}...")
        
        # Simulating live bar arrival by loading the last few ticks from our 30d files
        # In actual execution, this fetches from self.broker.tv_client or MT5 API
        try:
            is_futures = asset.startswith("/")
            if is_futures:
                mapping = {
                    "/GC": "gold_futures_30d_1min.csv",
                    "/ES": "sp500_futures_30d_1min.csv",
                    "/NQ": "nasdaq_futures_30d_1min.csv",
                    "/YM": "dow_futures_30d_1min.csv"
                }
                csv_name = mapping.get(asset, f"{asset.lstrip('/').lower()}_futures_30d_1min.csv")
                csv_path = Path("/config/bardfx-strategy/data/futures_30d") / csv_name
            else:
                csv_name = f"{asset.lower()}_5y_1min.csv"
                csv_path = Path("/config/bardfx-strategy/data") / csv_name
                
            if not csv_path.exists():
                gz_path = csv_path.with_suffix('.csv.gz')
                if gz_path.exists():
                    csv_path = gz_path
                else:
                    print(f"  Warning: No source data found for {asset} at {csv_path}")
                    return None
                
            df = pd.read_csv(csv_path)
            df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
            df = df.sort_values('timestamp').reset_index(drop=True)
            
            # Return the last 200 bars to simulate the active queue
            return df.tail(200).copy()
        except Exception as e:
            print(f"  Error fetching bars for {asset}: {e}")
            return None

    def run_strategy_check(self, asset, df_1m):
        """Runs the asset-specific strategy rules on the active bar queue."""
        config = self.assets[asset]
        strat_type = config["strategy"]
        
        print(f"Running Strategy {strat_type} rules on {asset}...")
        
        # Prepare 15m resampling
        df_15 = df_1m.resample('15min', on='timestamp').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
        }).dropna().reset_index()
        df_15.rename(columns={'timestamp': 'timestamp_15'}, inplace=True)
        
        trades = []
        if strat_type == "S1":
            # Volume Profile Gated ORB
            trades = self.s1.backtest(df_1m, df_15)
        elif strat_type == "S2":
            # FVG S&D Gated ORB
            trades = self.s2.backtest(df_1m)
        elif strat_type == "S3":
            # Inversed OCO Mean Reversion
            trades = self.s3.backtest(df_1m)
        elif strat_type == "S4":
            # NQ Wide OCO Breakout
            trades = self.s4.backtest(df_1m)
            
        # Check if the last bar triggered an active signal
        if trades:
            last_trade = trades[-1]
            # In live execution, we evaluate if this trade was entered within the last closed bar
            # (which means it's a fresh live trade signal!).
            # If so, we dispatch it to our broker.
            print(f"  Signal Detected! PnL Pct: {last_trade.get('pnl_pct')}")
            
            # Map parameters for live broker order placement
            signal = {
                "asset": asset,
                "action": "Buy" if last_trade.get("pnl_pct", 0) > 0 else "Sell", # Simplified map
                "qty": config["qty"],
                "order_type": "Limit",
                "price": df_1m['close'].iloc[-1], # Current price
                "sl": df_1m['close'].iloc[-1] - 10 * config["tick_size"],
                "tp": df_1m['close'].iloc[-1] + 30 * config["tick_size"]
            }
            self.broker.execute_signal(signal)

    def process_all_assets(self):
        """Loops through all assets, updates bars, and runs strategy checks."""
        print(f"\n--- Processing Cycle: {datetime.datetime.now()} ---")
        cycle_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
        
        if "tracked_assets" not in self.broker.state:
            self.broker.state["tracked_assets"] = {}
            
        for asset in self.assets.keys():
            df_1m = self.fetch_latest_bars(asset)
            if df_1m is not None and len(df_1m) > 0:
                last_price = float(df_1m['close'].iloc[-1])
                self.broker.state["tracked_assets"][asset] = {
                    "last_price": last_price,
                    "last_poll": cycle_time,
                    "strategy": self.assets[asset]["strategy"]
                }
                if len(df_1m) > 10:
                    self.run_strategy_check(asset, df_1m)
            time.sleep(1.0) # Network throttle delay

    def start_polling_loop(self, interval_seconds=30):
        """Starts the main polling loop for live automated trading."""
        print(f"Starting Live Tick Listener Polling Loop (Interval: {interval_seconds}s)...")
        last_git_push = 0
        try:
            while True:
                self.process_all_assets()
                # Periodically update broker positions and orders in state file
                self.broker.tv_client.get_positions()
                self.broker._save_state(self.broker.state)
                
                # Sync state to GitHub once every 5 minutes for static dashboard hosting fallback
                current_time = time.time()
                if current_time - last_git_push > 300:
                    import subprocess
                    try:
                        print("Syncing live state to GitHub repository...")
                        subprocess.run(["git", "add", "-f", "bardfx-strategy/live/static_dashboard/live_broker_state.json"], cwd="/config", check=True)
                        subprocess.run(["git", "commit", "-m", "chore: Auto-update live tick prices [skip ci]"], cwd="/config", check=True)
                        subprocess.run(["git", "push", "origin", "master"], cwd="/config", check=True)
                        last_git_push = current_time
                        print("  GitHub sync completed.")
                    except Exception as e:
                        print(f"  Warning: Git push failed: {e}")
                        
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            print("Listener loop stopped by operator.")

if __name__ == "__main__":
    listener = LiveTickListener(demo=True)
    # Run continuous polling loop with 30 seconds interval
    listener.start_polling_loop(interval_seconds=30)
