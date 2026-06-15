#!/usr/bin/env python3
import os
import sys
import json
import time
import math
import logging
import csv
import socket
import urllib3
import requests
from datetime import datetime, timezone, timedelta
from collections import deque, defaultdict
from typing import Dict, List, Optional, Tuple
import threading
import redis
import subprocess

# Global Path Initializers (Will be updated in __init__ silo)
DATA_DIR = ""
LOG_FILE = ""
TRADES_JSON = ""
TRADES_CSV = ""
TICK_LOG_CSV = ""
ORDERBOOK_LOG_CSV = ""

# API Endpoints
GAMMA_API = "https://gamma-api.polymarket.com/markets"
CLOB_API = "https://clob.polymarket.com"
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"
COINBASE_SPOT = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
DAEMON_URL = "http://127.0.0.1:5001"

# Polymarket taker fee
POLYMARKET_TAKER_FEE = 0.02

log = logging.getLogger("shadow_paper_bot")

class ShadowPaperTrader:
    def __init__(self):
        # --- 1. MODE DETECTION ---
        self.live_mode = False
        for i in range(len(sys.argv)):
            if sys.argv[i] == "--live": self.live_mode = True
        if os.getenv("LIVE_TRADING") == "true": self.live_mode = True

        # --- 2. PORTFOLIO & ASSET ---
        self.portfolio = "all"
        for i in range(len(sys.argv) - 1):
            if sys.argv[i] == "--portfolio":
                self.portfolio = sys.argv[i + 1]
                if self.portfolio == "elite_16": self.portfolio = "elite_17"
        
        self.asset = "BTC"
        for i in range(len(sys.argv) - 1):
            if sys.argv[i] == "--asset": self.asset = sys.argv[i + 1].upper()

        # --- 3. PHYSICAL DATA SILO ISOLATION ---
        global DATA_DIR, LOG_FILE, TRADES_JSON, TRADES_CSV, TICK_LOG_CSV, ORDERBOOK_LOG_CSV
        
        BASE_ROOT = "/root/polymarket-bot/data" if os.path.exists("/root/polymarket-bot/data") else "/config/projects/trading/data/poly-data"
        MODE_ROOT = "live" if self.live_mode else "paper"
        path_suffix = f"_{self.portfolio}" if self.portfolio != "all" else ""
        asset_suffix = f"_{self.asset.lower()}" if self.asset != "BTC" else ""
        
        DATA_DIR = f"{BASE_ROOT}/{MODE_ROOT}/poly_data{path_suffix}{asset_suffix}"
        LOG_FILE = f"{DATA_DIR}/shadow_paper_bot.log"
        TRADES_JSON = f"{DATA_DIR}/trades.json"
        TRADES_CSV = f"{DATA_DIR}/trades.csv"
        TICK_LOG_CSV = f"{DATA_DIR}/{self.asset.lower()}_ticks.csv"
        ORDERBOOK_LOG_CSV = f"{DATA_DIR}/orderbook_snapshots.csv"

        os.makedirs(DATA_DIR, exist_ok=True)

        # --- 4. LOGGER RECONFIGURATION ---
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]: root_logger.removeHandler(handler)
        
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
        )
        log.info(f"📁 DATA SILO: {MODE_ROOT.upper()} Dir: {DATA_DIR}")

        # --- 5. COMPONENT INITIALIZATION ---
        self.session = requests.Session()
        self.session.mount("https://", requests.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=10))
        
        try:
            self.redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
            self.redis_client.ping()
        except: self.redis_client = None

        self.active_trades = {}
        self.completed_trades = []
        self.traded_condition_ids = set()
        self.pending_pullbacks = {}
        self.spot_history = deque(maxlen=1200)
        self.last_socket_swap = time.time()
        self.tick_count = 0

        self.balances = {}
        self.peak_balances = {}
        self.balance_floors = {}
        for strat in self.get_all_strategies():
            self.balances[strat] = 100.0
            self.peak_balances[strat] = 100.0
            self.balance_floors[strat] = 80.0

        # Maintenance Timers
        self.last_backup = time.time()
        self.last_redemption = time.time()
        self.last_stale_reap = time.time()

        # Launch Threads
        self.fast_poll_thread = threading.Thread(target=self.fast_poll_loop, daemon=True)
        self.fast_poll_thread.start()

        # Regime Shield: DISABLED for elite stacks by default
        if self.portfolio in ("elite_17", "elite_7"):
            self.regime_shield_active = False
        else:
            self.regime_shield_active = True

        for i in range(len(sys.argv) - 1):
            if sys.argv[i] == "--no-regime-shield": self.regime_shield_active = False
            elif sys.argv[i] == "--enable-regime-shield": self.regime_shield_active = True

        self.load_state()
        self.save_state()
        self.init_csv_files()

    def get_all_strategies(self) -> List[str]:
        if self.portfolio == "elite_7":
            return ["BREAKOUT", "BREAKOUT_PCT_0.04", "BREAKOUT_PCT_0.08", "BREAKOUT_Z_1.6", "OFI_MOMENTUM_BO_15M", "HEATMAP_EXPIRY_DRIFT_15M"]
        if self.portfolio == "elite_17":
            return ["MEAN_REVERSION", "MEAN_REVERSION_OPPOSITE_EXIT", "MEAN_REVERSION_Z_1.5", "MR_GAMMA_EXPIRY_PIN", "MR_HEATMAP_LIQ_FADE", "SNIPE", "BREAKOUT_PCT_0.04", "BREAKOUT_PCT_0.08", "BREAKOUT_Z_1.6", "KINETIC_VELOCITY_BREAKOUT", "L2_ABSORPTION_SPREAD_COLLAPSE", "LIQUIDATION_SPOT_GAP_FADE", "MR_L2_OFI_DELTA_FADE"]
        return ["SNIPE", "BREAKOUT_PCT_0.04"]

    def load_state(self):
        if os.path.exists(TRADES_JSON):
            try:
                with open(TRADES_JSON, "r") as f:
                    state = json.load(f)
                self.balances = state.get("balances", self.balances)
                self.active_trades = state.get("active_trades", {})
                self.completed_trades = state.get("completed_trades", [])
                self.traded_condition_ids = set(state.get("traded_condition_ids", []))
            except Exception as e: log.error(f"Load error: {e}")

    def save_state(self):
        state = {
            "balances": self.balances,
            "active_trades": self.active_trades,
            "completed_trades": self.completed_trades,
            "traded_condition_ids": list(self.traded_condition_ids),
        }
        try:
            with open(TRADES_JSON, "w") as f: json.dump(state, f, indent=4)
        except Exception as e: log.error(f"Save error: {e}")

    def place_live_clob_order(self, token_id: str, price: float, size: float, side: str, market: dict, order_type: str = "FOK", trade_id: str = None):
        if not self.live_mode: return
        
        # 1.62s Safety Lock check for SELL orders
        if side == "SELL" and trade_id and trade_id in self.active_trades:
            elapsed = time.perf_counter() - self.active_trades[trade_id].get("entry_timestamp", 0)
            if elapsed < 1.62:
                wait = 1.62 - elapsed
                log.info(f"⏳ [SAFETY] Waiting {wait*1000:.1f}ms for Indexer...")
                time.sleep(wait)

        # Physical Rule: Max 2 decimals for makingAmount
        size = round(float(size), 2)
        if size < 5.0: size = 5.0

        payload = {"token_id": token_id, "price": price, "size": size, "side": side, "type": order_type}
        try:
            r = self.session.post(f"{DAEMON_URL}/order", json=payload, timeout=5)
            if r.status_code == 200:
                resp = r.json()
                if side == "BUY" and trade_id:
                    # REAL-FILL VERIFICATION
                    actual_shares = float(resp.get("makingAmount", size))
                    self.active_trades[trade_id]["shares"] = actual_shares
                    self.active_trades[trade_id]["entry_order_id"] = resp.get("orderID")
                    self.active_trades[trade_id]["entry_timestamp"] = time.perf_counter()
                    log.info(f"✅ [REAL-FILL] Bought {actual_shares:.2f} shares at ${price}. ID: {resp.get('orderID')}")
                    
                    # LAYER 1: Immediate Resting Limit Order at $0.985
                    if actual_shares >= 5.0:
                        self.place_resting_win_limit(token_id, actual_shares, trade_id)
                else:
                    log.info(f"✅ [SUCCESS] {side} {size} shares.")
                return resp
            else: log.error(f"❌ [API ERROR] {r.text}")
        except Exception as e: log.error(f"Order failed: {e}")
        return None

    def place_resting_win_limit(self, token_id, shares, trade_id):
        payload = {"token_id": token_id, "price": 0.985, "size": shares, "side": "SELL", "type": "LIMIT"}
        try:
            r = self.session.post(f"{DAEMON_URL}/order", json=payload, timeout=5)
            if r.status_code == 200:
                oid = r.json().get("orderID")
                self.active_trades[trade_id]["resting_order_id"] = oid
                log.info(f"💎 [LAYER 1] Resting Limit SELL at $0.985 placed. ID: {oid}")
        except: pass

    def cancel_order(self, order_id):
        try:
            self.session.post(f"{DAEMON_URL}/cancel", json={"order_id": order_id}, timeout=3)
        except: pass

    def fast_poll_loop(self):
        log.info("⚡ [FAST-POLL] Dual-Layer Exit Engine Active.")
        while True:
            try:
                if not self.active_trades:
                    time.sleep(0.5)
                    continue
                
                now_utc = datetime.now(timezone.utc)
                for tid, trade in list(self.active_trades.items()):
                    # LAYER 2: TERMINAL BYPASS (T-1.1s)
                    try:
                        end_dt = datetime.fromisoformat(trade["end_date"].replace("Z", "+00:00"))
                        rem = (end_dt - now_utc).total_seconds()
                        
                        if rem <= 1.1:
                            # 1. Cancel resting order
                            if "resting_order_id" in trade:
                                self.cancel_order(trade["resting_order_id"])
                            
                            # 2. Fire high-speed FAK sweep
                            log.info(f"🛡️ [LAYER 2] Terminal Sweep at T-{rem:.2f}s for {tid[:8]}.")
                            self.execute_paper_exit(tid, 0.50, status="EARLY_EXPIRY_EXIT") # execute_paper_exit handles logic
                            continue
                    except: pass
                time.sleep(0.05)
            except: time.sleep(1)

    def execute_paper_exit(self, trade_id, exit_price, status="WIN"):
        if trade_id not in self.active_trades: return
        trade = self.active_trades.pop(trade_id)
        
        # Physical Execution if live
        if self.live_mode:
            self.place_live_clob_order(trade["token_id"], exit_price, trade["shares"], "SELL", {}, "FAK", trade_id)
            
        trade.update({
            "exit_time": datetime.now(timezone.utc).isoformat(),
            "exit_contract_payout": exit_price,
            "status": status,
            "pnl": (exit_price - trade["entry_contract_ask"]) * trade["shares"]
        })
        self.completed_trades.append(trade)
        self.balances[trade["strategy"]] += trade["pnl"]
        self.save_state()

    # (Rest of market discovery/tick/logic functions follow original pattern...)
    def tick(self):
        self.tick_count += 1
        # ... logic for signal generation ...
        pass

    def init_csv_files(self):
        # ... CSV initialization ...
        pass
    
    def backfill_spot_history(self): pass
    def get_hyperliquid_perp_price(self): return 64000.0 # Mock for example
    def simulate_mock_trade_if_needed(self): pass

if __name__ == "__main__":
    bot = ShadowPaperTrader()
    while True:
        try: bot.tick()
        except: time.sleep(1)
        time.sleep(3)
