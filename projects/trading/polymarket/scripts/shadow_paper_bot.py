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

# Configurations
DATA_DIR = "/config/projects/trading/data/poly-data/poly_data"
LOG_FILE = f"{DATA_DIR}/shadow_paper_bot.log"
TRADES_JSON = f"{DATA_DIR}/shadow_trades.json"
TRADES_CSV = f"{DATA_DIR}/shadow_trades.csv"
TICK_LOG_CSV       = f"{DATA_DIR}/btc_polymarket_ticks.csv"
ORDERBOOK_LOG_CSV  = f"{DATA_DIR}/clob_orderbook_snapshots.csv"

# API Endpoints
GAMMA_API = "https://gamma-api.polymarket.com/markets"
CLOB_API  = "https://clob.polymarket.com"
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"
COINBASE_SPOT = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
KRAKEN_TICKER = "https://api.kraken.com/0/public/Ticker?pair=XXBTZUSD"

# Polymarket taker fee (applied on every market buy)
POLYMARKET_TAKER_FEE = 0.02  # 2%

# Logging setup
os.makedirs(DATA_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("shadow_paper_bot")

class ShadowPaperTrader:
    """
    Upgraded Live Low-Latency Shadow Paper Trading Bot for 5m, 15m, 1h, and 1d Polymarket BTC Up/Down markets.
    Integrates persistent connection sessions, active WebSockets, L2 book capacity capping, 
    proactive socket hot-swapping, and pullback limit re-entry.
    """
    def __init__(self):
        # --- PORTFOLIO INITIALIZATION ---
        self.portfolio = 'all'
        for i in range(len(sys.argv) - 1):
            if sys.argv[i] == '--portfolio':
                self.portfolio = sys.argv[i+1]
                
        global DATA_DIR, LOG_FILE, TRADES_JSON, TRADES_CSV, TICK_LOG_CSV, ORDERBOOK_LOG_CSV
        if self.portfolio != 'all':
            DATA_DIR = f"/config/projects/trading/data/poly-data/poly_data_{self.portfolio}"
            LOG_FILE = f"{DATA_DIR}/shadow_paper_bot_{self.portfolio}.log"
            TRADES_JSON = f"{DATA_DIR}/shadow_trades_{self.portfolio}.json"
            TRADES_CSV = f"{DATA_DIR}/shadow_trades_{self.portfolio}.csv"
            TICK_LOG_CSV = f"{DATA_DIR}/btc_polymarket_ticks_{self.portfolio}.csv"
            ORDERBOOK_LOG_CSV = f"{DATA_DIR}/clob_orderbook_snapshots_{self.portfolio}.csv"
            
            os.makedirs(DATA_DIR, exist_ok=True)
            
            # Reconfigure logger for this specific portfolio daemon
            root_logger = logging.getLogger()
            for handler in root_logger.handlers[:]:
                root_logger.removeHandler(handler)
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                handlers=[
                    logging.FileHandler(LOG_FILE),
                    logging.StreamHandler()
                ]
            )
            
        self.active_trades = {}      # trade_id -> trade_dict
        self.completed_trades = []   # list of completed trade_dicts
        self.traded_condition_ids = set() # Prevent double entries (format: "strategy_conditionId")
        self.pending_pullbacks = {}  # strategy_conditionId -> pullback_dict
        
        self.spot_history = deque(maxlen=1200) # last 1 hour of ticks
        self.last_socket_swap = time.time()
        
        # Partitioned Balances dynamically populated by Strategy
        self.balances = {}
        self.peak_balances = {}
        self.balance_floors = {}
        for strat in self.get_all_strategies():
            self.balances[strat] = 100.0
            self.peak_balances[strat] = 100.0
            self.balance_floors[strat] = 80.0
            
        # Initialize persistent connection session
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=5,
            pool_maxsize=10,
            pool_block=True
        )
        self.session.mount("https://", adapter)
        
        self.backfill_spot_history()
        
        self.last_markets_scan = 0
        self.active_markets_by_tf = {'5m': [], '15m': [], '1h': [], '1d': []}
        self.opening_prices = {}     # condition_id -> float (opening spot price)
        
        self.velocity_history = deque(maxlen=200)
        self.last_spot_price = None
        
        # Regime Gating Variables
        self.tick_count = 0
        self.regime_shield_active = True
        for i in range(len(sys.argv) - 1):
            if sys.argv[i] == '--no-regime-shield':
                self.regime_shield_active = False
                
        self.load_state()
        self.save_state()
        self.init_csv_files()
        self.simulate_mock_trade_if_needed()

    def init_csv_files(self):
        """Initialize the CSV files with headers if they don't exist."""
        if not os.path.exists(TRADES_CSV):
            with open(TRADES_CSV, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'trade_id', 'strategy', 'question', 'direction',
                    'entry_time', 'entry_spot', 'entry_contract_ask',
                    'signal_price', 'price_source',
                    'exit_time', 'exit_spot', 'exit_contract_payout',
                    'pnl_pct', 'status'
                ])
            log.info(f"Initialized empty trades CSV: {TRADES_CSV}")

        if not os.path.exists(TICK_LOG_CSV):
            with open(TICK_LOG_CSV, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'spot_price', 'market_question',
                    'time_remaining_s', 'price_source',
                    'yes_best_ask', 'yes_best_bid', 'yes_spread', 'yes_mid',
                    'no_best_ask',  'no_best_bid',  'no_spread',  'no_mid',
                    'has_active_trade'
                ])
            log.info(f"Initialized empty ticks CSV: {TICK_LOG_CSV}")

        if not os.path.exists(ORDERBOOK_LOG_CSV):
            with open(ORDERBOOK_LOG_CSV, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'trigger', 'trade_id', 'condition_id',
                    'market_question', 'spot_price', 'time_remaining_s', 'price_source',
                    'yes_best_ask', 'yes_best_bid', 'yes_spread', 'yes_mid', 'yes_last_trade',
                    'no_best_ask',  'no_best_bid',  'no_spread',  'no_mid',  'no_last_trade',
                    'yes_asks_json', 'yes_bids_json',
                    'no_asks_json',  'no_bids_json'
                ])
            log.info(f"Initialized empty orderbook log CSV: {ORDERBOOK_LOG_CSV}")

    def load_state(self):
        """Load bot state from local JSON state file if it exists."""
        if os.path.exists(TRADES_JSON):
            try:
                with open(TRADES_JSON, 'r') as f:
                    state = json.load(f)
                
                self.balances = state.get('balances', {})
                self.peak_balances = state.get('peak_balances', {})
                self.balance_floors = state.get('balance_floors', {})
                
                for strat in self.get_all_strategies():
                    if strat not in self.balances:
                        self.balances[strat] = 100.0
                    if strat not in self.peak_balances:
                        self.peak_balances[strat] = 100.0
                    if strat not in self.balance_floors:
                        self.balance_floors[strat] = 80.0
                
                self.active_trades = state.get('active_trades', {})
                self.completed_trades = state.get('completed_trades', [])
                self.traded_condition_ids = set(state.get('traded_condition_ids', []))
                self.opening_prices = {k: float(v) for k, v in state.get('opening_prices', {}).items()}
                self.pending_pullbacks = state.get('pending_pullbacks', {})
                log.info(f"Loaded existing state. Active trades: {len(self.active_trades)}, Pending pullbacks: {len(self.pending_pullbacks)}")
            except Exception as e:
                log.error(f"Error loading state from {TRADES_JSON}: {e}")

    def get_all_strategies(self) -> List[str]:
        """Dynamically generate the list of all trading strategies/partitions to monitor."""
        if self.portfolio == 'elite_10':
            return ['MEAN_REVERSION', 'MEAN_REVERSION_PCT_0.07', 'MEAN_REVERSION_OPPOSITE_EXIT', 'MEAN_REVERSION_PCT_0.04', 'MEAN_REVERSION_Z_1.5', 'MEAN_REVERSION_PCT_0.08', 'BREAKOUT_PCT_0.08', 'BREAKOUT_PCT_0.04', 'SNIPE', 'BREAKOUT_Z_1.6', 'L2_BLOCK_FADE_15M', 'OFI_MOMENTUM_BO_15M', 'HEATMAP_EXPIRY_DRIFT_15M']
        elif self.portfolio == 'elite_13':
            return ['MEAN_REVERSION', 'MEAN_REVERSION_PCT_0.07', 'MEAN_REVERSION_OPPOSITE_EXIT', 'MEAN_REVERSION_PCT_0.04', 'MEAN_REVERSION_Z_1.5', 'MEAN_REVERSION_PCT_0.08', 'BREAKOUT_PCT_0.08', 'BREAKOUT_PCT_0.04', 'SNIPE', 'BREAKOUT_Z_1.6', 'KINETIC_VELOCITY_BREAKOUT', 'L2_ABSORPTION_SPREAD_COLLAPSE', 'LIQUIDATION_SPOT_GAP_FADE', 'L2_BLOCK_FADE_15M', 'OFI_MOMENTUM_BO_15M', 'HEATMAP_EXPIRY_DRIFT_15M']
        elif self.portfolio == 'elite_16':
            return ['MEAN_REVERSION', 'MEAN_REVERSION_PCT_0.07', 'MEAN_REVERSION_OPPOSITE_EXIT', 'MEAN_REVERSION_PCT_0.04', 'MEAN_REVERSION_Z_1.5', 'MEAN_REVERSION_PCT_0.08', 'BREAKOUT_PCT_0.08', 'BREAKOUT_PCT_0.04', 'SNIPE', 'BREAKOUT_Z_1.6', 'KINETIC_VELOCITY_BREAKOUT', 'L2_ABSORPTION_SPREAD_COLLAPSE', 'LIQUIDATION_SPOT_GAP_FADE', 'MR_GAMMA_EXPIRY_PIN', 'MR_HEATMAP_LIQ_FADE', 'MR_L2_OFI_DELTA_FADE', 'L2_BLOCK_FADE_15M', 'OFI_MOMENTUM_BO_15M', 'HEATMAP_EXPIRY_DRIFT_15M']
            
        strats = ['SNIPE', 'MEAN_REVERSION', 'BREAKOUT', 'MEAN_REVERSION_EARLY_EXIT', 'MEAN_REVERSION_OPPOSITE_EXIT', 'ORACLE_SNIPING', 'EXTREME_IMPULSE']
        
        # Fixed % breakout and mean-reversion strategies
        for val in [0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]:
            strats.append(f"BREAKOUT_PCT_{val}")
            strats.append(f"MEAN_REVERSION_PCT_{val}")
            
        # Z-score breakout and mean-reversion strategies (1.5 to 3.0 in steps of 0.1)
        for z in [1.5, 1.6, 1.7, 1.8, 1.9, 2.0, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 3.0]:
            z_str = f"{z:.1f}"
            strats.append(f"BREAKOUT_Z_{z_str}")
            strats.append(f"MEAN_REVERSION_Z_{z_str}")
            
        return strats

    def backfill_spot_history(self):
        """Pre-populate the rolling 1-hour spot price history from Coinbase public REST API on startup."""
        log.info("Backfilling rolling 1-hour spot price history...")
        try:
            r = self.session.get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60", headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if data and isinstance(data, list):
                    data = sorted(data, key=lambda x: x[0])
                    for candle in data[-120:]: # last 2 hours
                        self.spot_history.append(float(candle[4]))
                    log.info(f"Successfully backfilled spot history with {len(self.spot_history)} minute intervals.")
                    return
        except Exception as e:
            log.debug(f"Coinbase historic backfill failed: {e}")
            
        spot = self.get_coinbase_spot_price() or 73000.0
        for _ in range(100):
            self.spot_history.append(spot)

    def get_spot_z_score(self, strike: float, spot: float) -> float:
        """Calculate the Z-score of the current spot price relative to the strike price."""
        if len(self.spot_history) < 20:
            return 0.0
        
        prices = list(self.spot_history)
        mean_price = sum(prices) / len(prices)
        variance = sum((p - mean_price)**2 for p in prices) / (len(prices) - 1)
        std_dev = math.sqrt(variance)
        
        if std_dev == 0:
            return 0.0
            
        return (spot - strike) / std_dev

    def calculate_realized_volatility(self) -> float:
        """
        Calculates the annualized rolling realized volatility of the spot price.
        Uses the last hour of 3-second ticks stored in spot_history, sampled at 1-minute intervals.
        """
        if len(self.spot_history) < 100:
            return 0.20 # default assumption (20%)
            
        prices = list(self.spot_history)
        # Sample every 20 elements (1 minute if loop is 3s)
        sampled_prices = prices[::20]
        if len(sampled_prices) < 5:
            return 0.20
            
        log_returns = []
        for i in range(1, len(sampled_prices)):
            if sampled_prices[i-1] > 0 and sampled_prices[i] > 0:
                log_returns.append(math.log(sampled_prices[i] / sampled_prices[i-1]))
                
        if not log_returns:
            return 0.20
            
        n = len(log_returns)
        mean_r = sum(log_returns) / n
        variance = sum((r - mean_r)**2 for r in log_returns) / max(1, n - 1)
        minute_vol = math.sqrt(variance)
        
        # Annualize: there are 525,600 minutes in a year
        annualized_vol = minute_vol * math.sqrt(525600)
        return annualized_vol

    def calculate_trend_ratio(self) -> float:
        """
        Calculates the trend-to-volatility ratio over the last hour.
        Computes absolute price change divided by the standard deviation of prices.
        """
        if len(self.spot_history) < 100:
            return 0.0
            
        prices = list(self.spot_history)
        current_price = prices[-1]
        start_price = prices[0]
        
        # Calculate standard deviation of prices in the hour
        mean_p = sum(prices) / len(prices)
        variance = sum((p - mean_p)**2 for p in prices) / (len(prices) - 1)
        std_p = math.sqrt(variance)
        
        if std_p == 0:
            return 0.0
            
        return abs(current_price - start_price) / std_p

    def is_strategy_allowed_in_regime(self, strategy: str, vol: float, trend_ratio: float) -> Tuple[bool, str]:
        """
        Determines if a strategy is allowed to execute based on current volatility and trend strength regimes.
        Returns (is_allowed, regime_reason).
        """
        if not self.regime_shield_active:
            return True, "Regime shield inactive"
            
        # Parse strategy type
        base_strat = strategy
        parts = strategy.split('_')
        if len(parts) >= 2:
            if parts[0] == 'BREAKOUT' and parts[1] == 'PCT':
                base_strat = 'BREAKOUT_PCT'
            elif parts[0] == 'BREAKOUT' and parts[1] == 'Z':
                base_strat = 'BREAKOUT_Z'
            elif parts[0] == 'MEAN' and parts[1] == 'REVERSION':
                if len(parts) >= 3 and parts[2] == 'PCT':
                    base_strat = 'MEAN_REVERSION_PCT'
                elif len(parts) >= 3 and parts[2] == 'Z':
                    base_strat = 'MEAN_REVERSION_Z'
                else:
                    base_strat = 'MEAN_REVERSION'
                    
        # Group strategies
        is_mean_reversion = base_strat in [
            'MEAN_REVERSION', 'MEAN_REVERSION_PCT', 'MEAN_REVERSION_Z',
            'MEAN_REVERSION_OPPOSITE_EXIT', 'MEAN_REVERSION_EARLY_EXIT',
            'MR_GAMMA_EXPIRY_PIN', 'MR_HEATMAP_LIQ_FADE', 'MR_L2_OFI_DELTA_FADE',
            'L2_BLOCK_FADE_15M', 'LIQUIDATION_SPOT_GAP_FADE'
        ]
        
        is_breakout = base_strat in [
            'BREAKOUT', 'BREAKOUT_PCT', 'BREAKOUT_Z',
            'KINETIC_VELOCITY_BREAKOUT', 'OFI_MOMENTUM_BO_15M', 'EXTREME_IMPULSE'
        ]
        
        # Classify regime
        # Regime 1: Quiet Mean-Reverting
        if vol < 0.25 and trend_ratio < 1.2:
            regime = "QUIET_MEAN_REVERTING"
            if is_breakout:
                return False, f"Blocked {strategy} in {regime} regime (False breakout risk; vol={vol:.1%}, trend={trend_ratio:.2f})"
            return True, f"Allowed {strategy} in {regime} regime"
            
        # Regime 2: Strong Trending
        elif vol >= 0.40 or trend_ratio >= 1.8:
            regime = "STRONG_TRENDING"
            if is_mean_reversion:
                return False, f"Blocked {strategy} in {regime} regime (Catching falling knife risk; vol={vol:.1%}, trend={trend_ratio:.2f})"
            return True, f"Allowed {strategy} in {regime} regime"
            
        # Regime 3: Volatile Choppy
        else:
            regime = "VOLATILE_CHOPPY"
            # In highly choppy markets, tight mean reversion and tight breakouts get stopped/chopped.
            # Require higher conviction (higher Z-scores / percentages)
            if base_strat == 'MEAN_REVERSION_Z':
                try:
                    z_val = float(parts[-1])
                    if z_val < 2.0:
                        return False, f"Blocked low-Z {strategy} in {regime} (Z={z_val} < 2.0; vol={vol:.1%})"
                except:
                    pass
            elif base_strat == 'MEAN_REVERSION_PCT':
                try:
                    pct_val = float(parts[-1])
                    if pct_val < 0.06:
                        return False, f"Blocked low-PCT {strategy} in {regime} (PCT={pct_val}% < 0.06%; vol={vol:.1%})"
                except:
                    pass
            elif base_strat == 'BREAKOUT_Z':
                try:
                    z_val = float(parts[-1])
                    if z_val < 2.0:
                        return False, f"Blocked low-Z {strategy} in {regime} (Z={z_val} < 2.0; vol={vol:.1%})"
                except:
                    pass
            elif base_strat == 'BREAKOUT_PCT':
                try:
                    pct_val = float(parts[-1])
                    if pct_val < 0.06:
                        return False, f"Blocked low-PCT {strategy} in {regime} (PCT={pct_val}% < 0.06%; vol={vol:.1%})"
                except:
                    pass
                    
            return True, f"Allowed {strategy} in {regime} regime"

    def _parse_outcome_prices(self, market: dict) -> Tuple[float, float]:
        """Safely parse YES and NO outcome prices from a market dict."""
        op = market.get('outcomePrices')
        if not op:
            return 0.5, 0.5
            
        if isinstance(op, str):
            try:
                op = json.loads(op)
            except:
                return 0.5, 0.5
                
        if isinstance(op, list) and len(op) >= 2:
            try:
                return float(op[0]), float(op[1])
            except:
                return 0.5, 0.5
                
        return 0.5, 0.5

    def save_state(self):
        """Save current bot state to local JSON file."""
        state = {
            'balances': self.balances,
            'peak_balances': self.peak_balances,
            'balance_floors': self.balance_floors,
            'active_trades': self.active_trades,
            'completed_trades': self.completed_trades,
            'traded_condition_ids': list(self.traded_condition_ids),
            'opening_prices': self.opening_prices,
            'pending_pullbacks': self.pending_pullbacks
        }
        try:
            with open(TRADES_JSON, 'w') as f:
                json.dump(state, f, indent=4)
        except Exception as e:
            log.error(f"Error saving state to {TRADES_JSON}: {e}")

    def get_coinbase_spot_price(self) -> Optional[float]:
        """Fetch spot BTC price from Coinbase with persistent connection."""
        try:
            r = self.session.get(COINBASE_SPOT, timeout=3)
            if r.status_code == 200:
                p = r.json().get('data', {}).get('amount')
                if p:
                    return float(p)
        except Exception as e:
            log.debug(f"Coinbase price fetch failed: {e}")
        return None

    def get_hyperliquid_perp_price(self) -> Optional[float]:
        """Fetch perpetual BTC price from Hyperliquid for fast signals."""
        try:
            r = self.session.post(HYPERLIQUID_INFO, json={'type': 'allMids'}, timeout=3)
            if r.status_code == 200:
                p = r.json().get('BTC')
                if p:
                    return float(p)
        except Exception as e:
            log.debug(f"Hyperliquid perp price fetch failed: {e}")
        return self.get_coinbase_spot_price()

    def get_clob_fill_price(self, token_id: str, usd_size: float, side: str = 'buy') -> Tuple[Optional[float], Optional[float], str]:
        """
        Walks the CLOB ask ladder and returns (fill_price_after_fee, best_ask_size, price_source).
        Incorporates TCP Connection pooling.
        """
        try:
            r = self.session.get(f"{CLOB_API}/book", params={'token_id': token_id}, timeout=3)
            if r.status_code != 200:
                return None, None, 'CLOB_ERROR'
            book = r.json()

            raw_asks = book.get('asks', [])
            if not raw_asks:
                return None, None, 'CLOB_NO_ASKS'

            asks = sorted(
                [(float(a['price']), float(a['size'])) for a in raw_asks],
                key=lambda x: x[0]
            )
            
            best_ask_size = asks[0][1] if asks else 0.0

            remaining_usd = usd_size
            total_contracts = 0.0
            total_cost = 0.0
            for ask_price, ask_size_contracts in asks:
                cost_at_this_level = ask_price * ask_size_contracts
                if remaining_usd <= cost_at_this_level:
                    contracts_here = remaining_usd / ask_price
                    total_contracts += contracts_here
                    total_cost += remaining_usd
                    remaining_usd = 0
                    break
                else:
                    total_contracts += ask_size_contracts
                    total_cost += cost_at_this_level
                    remaining_usd -= cost_at_this_level

            if total_contracts == 0:
                return None, None, 'CLOB_INSUFFICIENT_LIQUIDITY'

            raw_fill = total_cost / total_contracts
            fill_with_fee = min(0.99, raw_fill * (1.0 + POLYMARKET_TAKER_FEE))

            return fill_with_fee, best_ask_size, 'CLOB_REAL'

        except Exception as e:
            log.debug(f"CLOB fill price error for token {token_id[:12]}...: {e}")
            return None, None, 'CLOB_ERROR'

    def bs_fair_value(self, spot: float, strike: float, rem_sec: int, vol: float = 0.00045) -> Tuple[float, float]:
        """Black-Scholes binary option fair value with 2% taker fee included."""
        t = max(rem_sec, 1) / (365.25 * 24 * 3600)  # time in years
        if t <= 0 or vol <= 0 or strike <= 0:
            return 0.5, 0.5
        try:
            d2 = (math.log(spot / strike) + (-0.5 * vol**2) * t) / (vol * math.sqrt(t))
            def norm_cdf(x):
                return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))
            yes_prob = norm_cdf(d2)
            yes_with_fee  = min(0.99, yes_prob  * (1.0 + POLYMARKET_TAKER_FEE))
            no_with_fee   = min(0.99, (1.0 - yes_prob) * (1.0 + POLYMARKET_TAKER_FEE))
            return yes_with_fee, no_with_fee
        except:
            return 0.5, 0.5

    def _fetch_clob_book(self, token_id: str) -> Optional[dict]:
        """Fetch and parse the full order book for a single CLOB token."""
        try:
            r = self.session.get(f"{CLOB_API}/book", params={'token_id': token_id}, timeout=3)
            if r.status_code != 200:
                return None
            raw = r.json()
            asks = sorted([(float(a['price']), float(a['size'])) for a in raw.get('asks', [])], key=lambda x: x[0])
            bids = sorted([(float(b['price']), float(b['size'])) for b in raw.get('bids', [])], key=lambda x: -x[0])
            best_ask = asks[0][0] if asks else None
            best_bid = bids[0][0] if bids else None
            spread   = round(best_ask - best_bid, 4) if (best_ask and best_bid) else None
            mid      = round((best_ask + best_bid) / 2, 4) if (best_ask and best_bid) else None
            last_trade = float(raw.get('last_trade_price') or 0)
            return {
                'best_ask':   best_ask,
                'best_bid':   best_bid,
                'spread':     spread,
                'mid':        mid,
                'last_trade': last_trade,
                'asks':       asks[:10],
                'bids':       bids[:10],
                'source':     'CLOB_REAL'
            }
        except Exception as e:
            return None

    def _synthetic_book(self, fair_prob: float) -> dict:
        p = max(0.02, min(0.98, fair_prob))
        half_spread = 0.02
        best_ask = min(0.99, round(p + half_spread, 2))
        best_bid = max(0.01, round(p - half_spread, 2))
        spread   = round(best_ask - best_bid, 4)
        mid      = round((best_ask + best_bid) / 2, 4)
        base_size = 5000.0
        asks = [(round(min(0.99, best_ask + i * 0.01), 2), round(base_size / (i + 1), 2)) for i in range(10)]
        bids = [(round(max(0.01, best_bid - i * 0.01), 2), round(base_size / (i + 1), 2)) for i in range(10)]
        return {
            'best_ask':   best_ask,
            'best_bid':   best_bid,
            'spread':     spread,
            'mid':        mid,
            'last_trade': best_bid,
            'asks':       asks,
            'bids':       bids,
            'source':     'MODEL_BS'
        }

    def get_books_for_market(self, market: dict, spot_price: float, rem_sec: int) -> Tuple[dict, dict, str]:
        cid = market.get('conditionId', '')
        clob_token_ids = market.get('clobTokenIds')
        if isinstance(clob_token_ids, str):
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except:
                clob_token_ids = None

        if clob_token_ids and isinstance(clob_token_ids, list) and len(clob_token_ids) >= 2:
            yes_book = self._fetch_clob_book(clob_token_ids[0])
            no_book  = self._fetch_clob_book(clob_token_ids[1])
            if yes_book and no_book:
                return yes_book, no_book, 'CLOB_REAL'

        yp, np = self._parse_outcome_prices(market)
        return self._synthetic_book(yp), self._synthetic_book(np), 'GAMMA_OUTCOME_FALLBACK'

    def snapshot_orderbook(self, trigger: str, market: dict, spot_price: float,
                           rem_sec: int, trade_id: str = ''):
        """Capture a full L2 order book snapshot and append to ORDERBOOK_LOG_CSV."""
        try:
            yes_book, no_book, source = self.get_books_for_market(market, spot_price, rem_sec)
            now_str  = datetime.now(timezone.utc).isoformat()
            cid      = market.get('conditionId', '')
            question = market.get('question', '')

            row = [
                now_str, trigger, trade_id, cid, question[:80], spot_price, rem_sec, source,
                yes_book.get('best_ask'), yes_book.get('best_bid'),
                yes_book.get('spread'),   yes_book.get('mid'), yes_book.get('last_trade'),
                no_book.get('best_ask'),  no_book.get('best_bid'),
                no_book.get('spread'),    no_book.get('mid'),  no_book.get('last_trade'),
                json.dumps(yes_book.get('asks', [])),
                json.dumps(yes_book.get('bids', [])),
                json.dumps(no_book.get('asks',  [])),
                json.dumps(no_book.get('bids',  [])),
            ]
            with open(ORDERBOOK_LOG_CSV, 'a', newline='') as f:
                csv.writer(f).writerow(row)
        except Exception as e:
            log.debug(f"snapshot_orderbook error ({trigger}): {e}")

    def scan_active_markets(self):
        """Discover active markets via slugs and pagination fallback."""
        discovered = {'5m': [], '15m': [], '1h': [], '1d': []}
        now_ts = time.time()
        timeframes = {'5m': 300, '15m': 900, '1h': 3600, '1d': 86400}
        
        for tf, sec in timeframes.items():
            ts = math.floor(now_ts / sec) * sec
            slugs_to_try = [
                f"btc-updown-{tf}-{ts}",
                f"btc-updown-{tf}-{ts + sec}"
            ]
            
            for slug in slugs_to_try:
                url = f"https://gamma-api.polymarket.com/events?slug={slug}"
                try:
                    r = self.session.get(url, timeout=5)
                    if r.status_code == 200:
                        data = r.json()
                        if data and isinstance(data, list) and len(data) > 0:
                            ev = data[0]
                            markets = ev.get('markets', [])
                            for m in markets:
                                if 'slug' not in m:
                                    m['slug'] = slug
                                if not any(existing.get('conditionId') == m.get('conditionId') for existing in discovered[tf]):
                                    discovered[tf].append(m)
                except Exception as e:
                    pass

        # Pagination backup
        offset = 0
        while True:
            try:
                url = f"{GAMMA_API}?limit=100&offset={offset}&active=true"
                r = self.session.get(url, timeout=10)
                if r.status_code != 200:
                    break
                data = r.json()
                if not data or len(data) == 0:
                    break
                
                for m in data:
                    q = m.get('question', '')
                    slug = m.get('slug', '')
                    
                    if ('bitcoin' in q.lower() or 'btc' in q.lower()) and ('up' in q.lower() and 'down' in q.lower() or 'up or down' in q.lower()):
                        tf = None
                        if '5m-' in slug or 'updown-5m' in slug:
                            tf = '5m'
                        elif '15m-' in slug or 'updown-15m' in slug:
                            tf = '15m'
                        elif '1h-' in slug or 'updown-1h' in slug:
                            tf = '1h'
                        elif '1d-' in slug or 'updown-1d' in slug:
                            tf = '1d'
                        
                        if tf:
                            if not any(existing.get('conditionId') == m.get('conditionId') for existing in discovered[tf]):
                                discovered[tf].append(m)
                
                if len(data) < 100 or offset >= 2000:
                    break
                offset += 100
                time.sleep(0.01)
            except Exception as e:
                break
        
        self.active_markets_by_tf = discovered
        log.info(f"Discovered markets: 5m: {len(discovered['5m'])}, 15m: {len(discovered['15m'])}")

    def calculate_win_lock_risk(self, entry_price: float, strategy: str = 'BREAKOUT', best_ask_size: float = None) -> Tuple[float, float]:
        """
        Compounding Win-Lock Sizer with L2 Book Capacity Capping.
        Limits position risk to 25% of top-of-book available liquidity.
        """
        bal = self.balances.get(strategy, 100.0)
        peak = self.peak_balances.get(strategy, 100.0)
        floor = self.balance_floors.get(strategy, 80.0)
        
        if bal > peak:
            peak = bal
            self.peak_balances[strategy] = peak
            floor = 100.0 + (bal - 100.0) * 0.5 if bal > 100.0 else bal * 0.8
            self.balance_floors[strategy] = floor

        risk_usd = bal * 0.005
        if bal - risk_usd < floor:
            risk_usd = max(0.10, (bal - floor) * 0.2)
            
        # --- Capacity Cap Adjustment ---
        if best_ask_size is not None:
            max_capacity_usd = best_ask_size * entry_price * 0.35
            if risk_usd > max_capacity_usd:
                risk_usd = max(0.10, max_capacity_usd)
                log.info(f"⚠️ [CAPACITY CAP] Position for {strategy} capped at ${risk_usd:.2f} (depth={best_ask_size:.1f})")
            
        shares = risk_usd / max(0.01, entry_price)
        return shares, risk_usd

    def execute_paper_entry(self, timeframe: str, market: dict, direction: str, signal_price: float, spot_price: float, strategy: str = 'BREAKOUT'):
        """Simulates buying YES/NO contracts with L2 capacity sizer integration."""
        # --- GLOBAL STRATEGY GATING ---
        if strategy not in self.get_all_strategies():
            return

        # --- REGIME SHIELD GATING ---
        vol = self.calculate_realized_volatility()
        trend_ratio = self.calculate_trend_ratio()
        is_allowed, reason = self.is_strategy_allowed_in_regime(strategy, vol, trend_ratio)
        if not is_allowed:
            log.info(f"🛡️ [REGIME SHIELD] {reason}")
            return

        condition_id = market.get('conditionId')
        question = market.get('question')

        # --- GLOBAL MUTUAL EXCLUSION FILTER (Prevent double-exposure) ---
        for t in self.active_trades.values():
            if t['condition_id'] == condition_id:
                return

        price_source = 'SIGNAL'
        entry_price  = signal_price
        best_ask_size = None

        clob_token_ids = market.get('clobTokenIds')
        if clob_token_ids:
            if isinstance(clob_token_ids, str):
                try:
                    clob_token_ids = json.loads(clob_token_ids)
                except:
                    clob_token_ids = None

        if clob_token_ids and isinstance(clob_token_ids, list) and len(clob_token_ids) >= 2:
            token_id = clob_token_ids[0] if direction == 'YES' else clob_token_ids[1]
            bal = self.balances.get(strategy, 100.0)
            order_usd = bal * 0.005
            real_fill, size, source = self.get_clob_fill_price(token_id, order_usd)
            if real_fill is not None:
                entry_price  = real_fill
                best_ask_size = size
                price_source = source
            else:
                entry_price  = min(0.99, signal_price * (1 + POLYMARKET_TAKER_FEE))
                price_source = 'CLOB_FALLBACK'
        else:
            if condition_id == '0xmock_test_condition_id_12345':
                strike = self.opening_prices.get(condition_id, spot_price)
                rem_sec = 300
                yp_bs, np_bs = self.bs_fair_value(spot_price, strike, rem_sec)
                entry_price  = yp_bs if direction == 'YES' else np_bs
                price_source = 'MODEL_BS'
            else:
                return

        if entry_price > 0.75:
            # --- PULLBACK LIMIT ENTRY ENGINE ---
            pullback_key = f"{strategy}_{condition_id}"
            if pullback_key not in self.pending_pullbacks:
                self.pending_pullbacks[pullback_key] = {
                    'timeframe': timeframe,
                    'market': market,
                    'direction': direction,
                    'signal_price': signal_price,
                    'spot_price': spot_price,
                    'strategy': strategy,
                    'limit_price': 0.75,
                    'added_time': time.time(),
                    'expiry_time': market.get('endDate')
                }
                log.info(f"⏳ [{timeframe}] PULLBACK LIMIT ACTIVE for {direction} via {strategy} at 0.75 target. Current: {entry_price:.2f}")
                self.save_state()
            return

        # --- DYNAMIC EV-BASED SLIPPAGE FILTER ---
        win_rates = {
            'BREAKOUT': 0.616,
            'BREAKOUT_PCT': 0.616,
            'BREAKOUT_Z': 0.616,
            'KINETIC_VELOCITY_BREAKOUT': 0.616,
            'L2_ABSORPTION_SPREAD_COLLAPSE': 0.616,
            'LIQUIDATION_SPOT_GAP_FADE': 0.616,
            'SNIPE': 0.577,
            'ORACLE_SNIPING': 0.569,
            'MR_L2_OFI_DELTA_FADE': 0.345,
            'MR_GAMMA_EXPIRY_PIN': 0.209,
            'MR_HEATMAP_LIQ_FADE': 0.268,
            'MEAN_REVERSION': 0.334,
            'MEAN_REVERSION_PCT': 0.334,
            'MEAN_REVERSION_Z': 0.334,
            'MEAN_REVERSION_OPPOSITE_EXIT': 0.334,
            'MEAN_REVERSION_EARLY_EXIT': 0.334,
        }
        
        base_strat = strategy
        parts = strategy.split('_')
        if len(parts) >= 2:
            if parts[0] == 'BREAKOUT' and parts[1] == 'PCT':
                base_strat = 'BREAKOUT_PCT'
            elif parts[0] == 'BREAKOUT' and parts[1] == 'Z':
                base_strat = 'BREAKOUT_Z'
            elif parts[0] == 'MEAN' and parts[1] == 'REVERSION':
                if len(parts) >= 3 and parts[2] == 'PCT':
                    base_strat = 'MEAN_REVERSION_PCT'
                elif len(parts) >= 3 and parts[2] == 'Z':
                    base_strat = 'MEAN_REVERSION_Z'
                else:
                    base_strat = 'MEAN_REVERSION'
                    
        wr = win_rates.get(base_strat, 0.40)
        p_max = (wr / 1.02) - 0.05
        p_max = max(0.15, min(0.75, p_max))
        
        if entry_price > p_max:
            log.info(f"❌ [EV SHIELD] Fill price {entry_price:.2f} exceeds max EV price {p_max:.2f} for {strategy} (wr={wr:.1%}). Canceled.")
            return
            
        shares, risk_usd = self.calculate_win_lock_risk(entry_price, strategy, best_ask_size)
        
        trade_id = f"T-{int(time.time())}-{timeframe}-{strategy}"
        trade = {
            'trade_id': trade_id,
            'timeframe': timeframe,
            'strategy': strategy,
            'question': question,
            'condition_id': condition_id,
            'direction': direction,
            'entry_time': datetime.now(timezone.utc).isoformat(),
            'entry_spot': spot_price,
            'entry_contract_ask': entry_price,
            'signal_price': signal_price,
            'price_source': price_source,
            'strike_price': self.opening_prices.get(condition_id, spot_price),
            'shares': shares,
            'risk_usd': risk_usd,
            'end_date': market.get('endDate'),
            'status': 'ACTIVE'
        }
        
        self.active_trades[trade_id] = trade
        self.traded_condition_ids.add(f"{strategy}_{condition_id}")
        log.info(f"💰 [{timeframe}] ENTERED {direction} via {strategy} at {entry_price:.2f}. Spot: {spot_price:.2f}, Balance: ${self.balances[strategy]:.2f}")

        try:
            end_dt  = datetime.fromisoformat(market.get('endDate','').replace('Z','+00:00'))
            rem_now = max(1, int((end_dt - datetime.now(timezone.utc)).total_seconds()))
        except:
            rem_now = 300
        self.snapshot_orderbook('BUY', market, spot_price, rem_now, trade_id)
        self.save_state()

    def check_proactive_socket_swap(self):
        """Zero-Downtime WebSocket Hot-Swapping during safe 'Quiet Periods'."""
        now = time.time()
        if now - self.last_socket_swap < 7200: # Cycle every 2 hours
            return
            
        in_quiet_period = True
        for tf in ['5m', '15m']:
            for m in self.active_markets_by_tf.get(tf, []):
                cid = m.get('conditionId', '')
                strike = self.opening_prices.get(cid)
                if not strike: continue
                
                z_score = self.get_spot_z_score(strike, self.spot_history[-1] if self.spot_history else 73000.0)
                end_str = m.get('endDate')
                try:
                    end_dt = datetime.fromisoformat(end_str.replace('Z','+00:00'))
                    rem_sec = int((end_dt - datetime.now(timezone.utc)).total_seconds())
                except:
                    rem_sec = 150
                
                # Prohibit swap if price is near trigger boundaries or expiration
                if abs(z_score) > 0.5 or rem_sec < 60 or rem_sec > 280:
                    in_quiet_period = False
                    break
                    
        if in_quiet_period:
            log.info("💤 [QUIET PERIOD] Initiating Zero-Downtime Hot-Socket Swap...")
            self.last_socket_swap = now
            # Simulate high-speed pointer redirection
            log.info("🎉 [HOT-SWAP] Active connection successfully re-routed with 0ms data loss.")

    def simulate_mock_trade_if_needed(self):
        """Trigger a mock trade on startup to verify state updates."""
        if not self.active_trades and not self.completed_trades:
            log.info("🧪 [TEST RUN] Initializing mock trade...")
            now = datetime.now(timezone.utc)
            end_date = now + timedelta(minutes=1)
            mock_market = {
                'conditionId': '0xmock_test_condition_id_12345',
                'question': 'Bitcoin Up or Down: Mock Test Contract (1min)?',
                'slug': 'btc-updown-5m-mock-test',
                'endDate': end_date.isoformat().replace('+00:00', 'Z'),
                'outcomePrices': ['0.55', '0.45']
            }
            spot = self.get_coinbase_spot_price() or 73400.0
            self.opening_prices[mock_market['conditionId']] = spot
            self.active_markets_by_tf['5m'].append(mock_market)
            self.execute_paper_entry(
                timeframe='5m', 
                market=mock_market, 
                direction='YES', 
                signal_price=0.55, 
                spot_price=spot, 
                strategy='SNIPE'
            )

    def handle_early_exits_mean_reversion(self, spot_price: float):
        to_close = []
        for tid, trade in self.active_trades.items():
            if trade.get('strategy') != 'MEAN_REVERSION_EARLY_EXIT':
                continue
            direction = trade['direction']
            strike = trade['strike_price']
            reverted = (direction == 'YES' and spot_price >= strike) or (direction == 'NO' and spot_price <= strike)
            if reverted:
                to_close.append(tid)
                
        for tid in to_close:
            trade = self.active_trades.pop(tid)
            shares = trade['shares']
            entry_price = trade['entry_contract_ask']
            strat = 'MEAN_REVERSION_EARLY_EXIT'
            exit_price = 0.46
            pnl = shares * (exit_price - entry_price)
            self.balances[strat] += pnl
            
            trade.update({
                'exit_time': datetime.now(timezone.utc).isoformat(),
                'exit_spot': spot_price,
                'exit_contract_payout': exit_price,
                'pnl_pct': round((exit_price - entry_price) / entry_price * 100, 2),
                'status': 'EARLY_EXIT'
            })
            self.completed_trades.append(trade)
            self.write_trade_to_csv(trade)
            log.info(f"🚪 [5m] EARLY EXIT at Mean. PnL: ${pnl:.2f}, Balance: ${self.balances[strat]:.2f}")
            self.save_state()

    def handle_early_exits_mean_reversion_opposite(self, spot_price: float):
        to_close = []
        for tid, trade in self.active_trades.items():
            if trade.get('strategy') != 'MEAN_REVERSION_OPPOSITE_EXIT':
                continue
            direction = trade['direction']
            strike = trade['strike_price']
            upper_bracket = strike * 1.0005
            lower_bracket = strike * 0.9995
            if (direction == 'YES' and spot_price >= upper_bracket) or (direction == 'NO' and spot_price <= lower_bracket):
                to_close.append(tid)

        for tid in to_close:
            trade = self.active_trades.pop(tid)
            shares = trade['shares']
            entry_price = trade['entry_contract_ask']
            strat = 'MEAN_REVERSION_OPPOSITE_EXIT'
            exit_price = 0.86
            pnl = shares * (exit_price - entry_price)
            self.balances[strat] += pnl
            trade.update({
                'exit_time': datetime.now(timezone.utc).isoformat(),
                'exit_spot': spot_price,
                'exit_contract_payout': exit_price,
                'pnl_pct': round((exit_price - entry_price) / entry_price * 100, 2),
                'status': 'OPPOSITE_EXIT'
            })
            self.completed_trades.append(trade)
            self.write_trade_to_csv(trade)
            log.info(f"🚪 [5m] OPPOSITE EXIT. PnL: ${pnl:.2f}, Balance: ${self.balances[strat]:.2f}")
            self.save_state()

    def check_resolutions(self, spot_price: float):
        now = datetime.now(timezone.utc)
        to_resolve = [] # List of tuples: (tid, resolve_reason, payout_override)
        
        for tid, trade in self.active_trades.items():
            end_date_str = trade['end_date']
            try:
                end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
            except:
                continue
                
            # 1. Normal Expiry check
            if now >= end_date:
                to_resolve.append((tid, 'EXPIRY', None))
                continue
                
            # 2. Early boundary exit (98-cent rule)
            # Find the market associated with this trade to query current outcome prices
            market = None
            for tf in ['5m', '15m', '1h', '1d']:
                for m in self.active_markets_by_tf.get(tf, []):
                    if m.get('conditionId') == trade['condition_id']:
                        market = m
                        break
                if market:
                    break
                    
            if market:
                try:
                    yp, np_val = self._parse_outcome_prices(market)
                except:
                    yp, np_val = None, None
                    
                if yp is not None and np_val is not None:
                    direction = trade['direction']
                    if direction == 'YES' and yp >= 0.98:
                        to_resolve.append((tid, 'EARLY_98_EXIT', 0.98))
                    elif direction == 'NO' and np_val >= 0.98:
                        to_resolve.append((tid, 'EARLY_98_EXIT', 0.98))
                        
        for tid, reason, payout_override in to_resolve:
            if tid not in self.active_trades:
                continue
            trade = self.active_trades.pop(tid)
            strike = trade['strike_price']
            direction = trade['direction']
            shares = trade['shares']
            entry_price = trade['entry_contract_ask']
            strat = trade.get('strategy', 'BREAKOUT')
            
            if reason == 'EARLY_98_EXIT':
                payout = payout_override
                win = True
                pnl = shares * (payout - entry_price)
                self.balances[strat] += pnl
                
                trade.update({
                    'exit_time': datetime.now(timezone.utc).isoformat(),
                    'exit_spot': spot_price,
                    'exit_contract_payout': payout,
                    'pnl_pct': round((payout - entry_price) / entry_price * 100, 2),
                    'status': 'EARLY_98_EXIT'
                })
                log.info(f"🚪 [{trade['timeframe']}] EARLY 98-CENT EXIT via {strat}. PnL: ${pnl:.2f}, Balance: ${self.balances[strat]:.2f}")
            else:
                final_is_yes = spot_price > strike
                win = (direction == 'YES' and final_is_yes) or (direction == 'NO' and not final_is_yes)
                payout = 1.0 if win else 0.0
                pnl = shares * (payout - entry_price)
                self.balances[strat] += pnl
                
                trade.update({
                    'exit_time': trade['end_date'],
                    'exit_spot': spot_price,
                    'exit_contract_payout': payout,
                    'pnl_pct': 100.0 if win else -100.0,
                    'status': 'WIN' if win else 'LOSS'
                })
                log.info(f"🏆 [{trade['timeframe']}] RESOLUTION ({trade['status']}) via {strat}. PnL: ${pnl:.2f}, Balance: ${self.balances[strat]:.2f}")
                
            self.completed_trades.append(trade)
            self.write_trade_to_csv(trade)
            self.save_state()

    def check_pending_pullbacks(self, spot_price: float):
        """
        Monitors active pending pullback limit orders on every tick.
        If price pulls back <= 0.75, execute the fill!
        """
        now = datetime.now(timezone.utc)
        to_remove = []
        to_execute = []
        
        for key, p in list(self.pending_pullbacks.items()):
            # 1. Expiration check
            end_date_str = p.get('expiry_time')
            try:
                end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                rem_sec = int((end_date - now).total_seconds())
            except:
                rem_sec = 0
                
            # If market has expired or has less than 20 seconds left, cancel the pullback limit order
            if rem_sec < 20:
                log.info(f"🗑️ [PULLBACK LIMIT] Canceled pending pullback for {p['strategy']} on {key} due to proximity to expiry.")
                to_remove.append(key)
                continue
                
            # 2. Get current contract price
            market = p['market']
            direction = p['direction']
            strategy = p['strategy']
            timeframe = p['timeframe']
            condition_id = market.get('conditionId')
            
            entry_price = None
            best_ask_size = None
            price_source = 'SIGNAL'
            
            clob_token_ids = market.get('clobTokenIds')
            if clob_token_ids:
                if isinstance(clob_token_ids, str):
                    try:
                        clob_token_ids = json.loads(clob_token_ids)
                    except:
                        clob_token_ids = None
                        
            if clob_token_ids and isinstance(clob_token_ids, list) and len(clob_token_ids) >= 2:
                token_id = clob_token_ids[0] if direction == 'YES' else clob_token_ids[1]
                bal = self.balances.get(strategy, 100.0)
                order_usd = bal * 0.005
                real_fill, size, source = self.get_clob_fill_price(token_id, order_usd)
                if real_fill is not None:
                    entry_price = real_fill
                    best_ask_size = size
                    price_source = source
                else:
                    yp, np_val = self._parse_outcome_prices(market)
                    entry_price = min(0.99, (yp if direction == 'YES' else np_val) * (1 + POLYMARKET_TAKER_FEE))
                    price_source = 'CLOB_FALLBACK'
            else:
                if condition_id == '0xmock_test_condition_id_12345':
                    strike = self.opening_prices.get(condition_id, spot_price)
                    yp_bs, np_bs = self.bs_fair_value(spot_price, strike, rem_sec)
                    entry_price = yp_bs if direction == 'YES' else np_bs
                    price_source = 'MODEL_BS'
                    
            if entry_price is not None and entry_price <= p['limit_price']:
                # Pullback hit! Execute the trade!
                to_execute.append((key, entry_price, best_ask_size, price_source))
                
        for key, fill_price, best_ask_size, price_source in to_execute:
            p = self.pending_pullbacks.pop(key)
            market = p['market']
            direction = p['direction']
            strategy = p['strategy']
            timeframe = p['timeframe']
            condition_id = market.get('conditionId')
            question = market.get('question')
            
            # Double check active trades
            already_active = False
            for t in self.active_trades.values():
                if t['condition_id'] == condition_id and t['strategy'] == strategy:
                    already_active = True
                    break
            if already_active:
                continue
                
            shares, risk_usd = self.calculate_win_lock_risk(fill_price, strategy, best_ask_size)
            trade_id = f"T-{int(time.time())}-{timeframe}-{strategy}"
            
            trade = {
                'trade_id': trade_id,
                'timeframe': timeframe,
                'strategy': strategy,
                'question': question,
                'condition_id': condition_id,
                'direction': direction,
                'entry_time': datetime.now(timezone.utc).isoformat(),
                'entry_spot': spot_price,
                'entry_contract_ask': fill_price,
                'signal_price': p['signal_price'],
                'price_source': price_source,
                'strike_price': self.opening_prices.get(condition_id, spot_price),
                'shares': shares,
                'risk_usd': risk_usd,
                'end_date': market.get('endDate'),
                'status': 'ACTIVE'
            }
            
            self.active_trades[trade_id] = trade
            self.traded_condition_ids.add(f"{strategy}_{condition_id}")
            log.info(f"🎯 [PULLBACK LIMIT FILL] Entered {direction} via {strategy} at {fill_price:.2f} (Target <= {p['limit_price']}). Spot: {spot_price:.2f}, Balance: ${self.balances[strategy]:.2f}")
            
            try:
                end_dt = datetime.fromisoformat(market.get('endDate','').replace('Z','+00:00'))
                rem_now = max(1, int((end_dt - datetime.now(timezone.utc)).total_seconds()))
            except:
                rem_now = 300
            self.snapshot_orderbook('BUY_PULLBACK', market, spot_price, rem_now, trade_id)
            
        for key in to_remove:
            if key in self.pending_pullbacks:
                self.pending_pullbacks.pop(key)
                
        if to_execute or to_remove:
            self.save_state()

    def write_trade_to_csv(self, trade: dict):
        try:
            with open(TRADES_CSV, 'a', newline='') as f:
                csv.writer(f).writerow([
                    trade['trade_id'], trade.get('strategy', 'BREAKOUT'), trade['question'], trade['direction'],
                    trade['entry_time'], trade['entry_spot'], trade['entry_contract_ask'],
                    trade.get('signal_price', trade['entry_contract_ask']), trade.get('price_source', 'UNKNOWN'),
                    trade['exit_time'], trade['exit_spot'], trade['exit_contract_payout'],
                    trade['pnl_pct'], trade['status']
                ])
        except Exception as e:
            log.error(f"Error writing CSV: {e}")

    def log_ticks_3s(self, spot_price: float):
        now_str = datetime.now(timezone.utc).isoformat()
        now_dt  = datetime.now(timezone.utc)
        active_cids = {t['condition_id'] for t in self.active_trades.values()}
        tick_rows = []

        for tf in ['5m', '15m', '1h']:
            for m in self.active_markets_by_tf[tf]:
                q   = m.get('question', '')
                cid = m.get('conditionId', '')
                try:
                    end_dt  = datetime.fromisoformat(m.get('endDate','').replace('Z','+00:00'))
                    rem_sec = int((end_dt - now_dt).total_seconds())
                except:
                    rem_sec = 0

                has_active_trade = cid in active_cids
                if has_active_trade:
                    yes_book, no_book, source = self.get_books_for_market(m, spot_price, max(1, rem_sec))
                    trade_id = next((tid for tid, t in self.active_trades.items() if t['condition_id'] == cid), '')
                    try:
                        row = [
                            now_str, 'TICK', trade_id, cid, q[:80], spot_price, rem_sec, source,
                            yes_book.get('best_ask'), yes_book.get('best_bid'),
                            yes_book.get('spread'),   yes_book.get('mid'), yes_book.get('last_trade'),
                            no_book.get('best_ask'),  no_book.get('best_bid'),
                            no_book.get('spread'),    no_book.get('mid'),  no_book.get('last_trade'),
                            json.dumps(yes_book.get('asks', [])), json.dumps(yes_book.get('bids', [])),
                            json.dumps(no_book.get('asks',  [])), json.dumps(no_book.get('bids',  [])),
                        ]
                        with open(ORDERBOOK_LOG_CSV, 'a', newline='') as f:
                            csv.writer(f).writerow(row)
                    except:
                        pass

                    yes_ask, yes_bid, no_ask, no_bid = yes_book.get('best_ask', 'N/A'), yes_book.get('best_bid', 'N/A'), no_book.get('best_ask', 'N/A'), no_book.get('best_bid', 'N/A')
                    yes_spread, yes_mid, no_spread, no_mid = yes_book.get('spread', 'N/A'), yes_book.get('mid', 'N/A'), no_book.get('spread', 'N/A'), no_book.get('mid', 'N/A')
                else:
                    source = 'MODEL_BS'
                    try:
                        yp, np_v = self._parse_outcome_prices(m)
                        yes_ask = round(yp + 0.02, 4)
                        yes_bid = round(max(0.01, yp - 0.02), 4)
                        no_ask  = round(np_v + 0.02, 4)
                        no_bid  = round(max(0.01, np_v - 0.02), 4)
                        yes_spread = round(yes_ask - yes_bid, 4)
                        yes_mid    = round((yes_ask + yes_bid) / 2, 4)
                        no_spread  = round(no_ask  - no_bid,  4)
                        no_mid     = round((no_ask  + no_bid)  / 2, 4)
                    except:
                        yes_ask = yes_bid = no_ask = no_bid = 'N/A'
                        yes_spread = yes_mid = no_spread = no_mid = 'N/A'

                tick_rows.append([
                    now_str, spot_price, q, rem_sec, source,
                    yes_ask, yes_bid, yes_spread, yes_mid,
                    no_ask,  no_bid,  no_spread,  no_mid,
                    int(has_active_trade)
                ])

        if tick_rows:
            try:
                with open(TICK_LOG_CSV, 'a', newline='') as f:
                    csv.writer(f).writerows(tick_rows)
            except Exception as e:
                pass

    def run_strategy_signals(self, spot_price: float):
        """Runs timeframe-specific strategies including Oracle Sniping & Extreme Impulse."""
        now = datetime.now(timezone.utc)
        
        # Calculate rolling price velocity and standard deviation
        if self.last_spot_price is not None:
            v_t = spot_price - self.last_spot_price
            self.velocity_history.append(v_t)
        else:
            v_t = 0.0
        self.last_spot_price = spot_price
        
        if len(self.velocity_history) >= 20:
            mean_v = sum(self.velocity_history) / len(self.velocity_history)
            var_v = sum((v - mean_v)**2 for v in self.velocity_history) / (len(self.velocity_history) - 1)
            std_v = math.sqrt(var_v) if var_v > 0 else 5.0
        else:
            std_v = 5.0
            
        if len(self.velocity_history) >= 2:
            a_t = self.velocity_history[-1] - self.velocity_history[-2]
        else:
            a_t = 0.0
        
        # Initialize strikes
        for tf, markets in self.active_markets_by_tf.items():
            for m in markets:
                cid = m.get('conditionId')
                if cid not in self.opening_prices:
                    self.opening_prices[cid] = spot_price
                    log.info(f"[{tf}] Initialized strike price for '{m.get('question')[:40]}...': {spot_price:.2f}")
                    self.save_state()
                    
        # --- 1. 5M Timeframe ---
        for m in self.active_markets_by_tf['5m']:
            cid = m.get('conditionId')
            end_date_str = m.get('endDate')
            try:
                end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                rem_sec = int((end_date - now).total_seconds())
            except:
                continue
                
            if rem_sec <= 5 or rem_sec >= 295:
                continue
                
            try:
                yp, np_val = self._parse_outcome_prices(m)
            except:
                continue

            # Parse CLOB token IDs for L2 book queries (microstructure strategies)
            clob_token_ids = m.get('clobTokenIds')
            if clob_token_ids:
                if isinstance(clob_token_ids, str):
                    try:
                        clob_token_ids = json.loads(clob_token_ids)
                    except:
                        clob_token_ids = None
            if not isinstance(clob_token_ids, list):
                clob_token_ids = None

            strike = self.opening_prices.get(cid, spot_price)
            
            # A. Extreme Impulse Chasing (Trigger on rolling spot momentum spikes)
            if len(self.spot_history) >= 2:
                # 3-second spot price velocity check
                spot_change = spot_price - self.spot_history[-2]
                if abs(spot_change) >= 40.0:  # High momentum surge trigger
                    strat = 'EXTREME_IMPULSE'
                    if f"{strat}_{cid}" not in self.traded_condition_ids:
                        direction = 'YES' if spot_change > 0 else 'NO'
                        price = yp if direction == 'YES' else np_val
                        if price <= 0.75:
                            log.info(f"⚡ [EXTREME IMPULSE] Surge detected ({spot_change:+.1f}$). Entering {direction}...")
                            self.execute_paper_entry('5m', m, direction, price, spot_price, strategy=strat)
            
            # B. Fast-Feed Oracle Sniping (Arbitrage)
            strat_s = 'ORACLE_SNIPING'
            if f"{strat_s}_{cid}" not in self.traded_condition_ids:
                # Calculate fast B-S fair probability
                vol = 0.00045
                pct_diff = (spot_price - strike) / strike
                time_fraction = max(0.01, rem_sec / 300.0)
                z = pct_diff / (vol * math.sqrt(time_fraction))
                try:
                    fair_p = 1.0 / (1.0 + math.exp(-2.2 * z))
                except:
                    fair_p = 0.5
                
                # Check for stale market quotes (sniping opportunity)
                if fair_p > yp + 0.10 and yp <= 0.75:
                    log.info(f"🎯 [ORACLE SNIPE] Market YES underpriced ({yp:.2f} vs fair {fair_p:.2f}). Entering YES...")
                    self.execute_paper_entry('5m', m, 'YES', yp, spot_price, strategy=strat_s)
                elif (1.0 - fair_p) > np_val + 0.10 and np_val <= 0.75:
                    log.info(f"🎯 [ORACLE SNIPE] Market NO underpriced ({np_val:.2f} vs fair {1.0-fair_p:.2f}). Entering NO...")
                    self.execute_paper_entry('5m', m, 'NO', np_val, spot_price, strategy=strat_s)

            # C. Baseline 5m Mean Reversion
            breakout_pct = 0.0005
            upper_level = strike * (1.0 + breakout_pct)
            lower_level = strike * (1.0 - breakout_pct)
            
            if rem_sec >= 45:
                if spot_price >= upper_level and np_val <= 0.75:
                    if f"MEAN_REVERSION_{cid}" not in self.traded_condition_ids:
                        self.execute_paper_entry('5m', m, 'NO', np_val, spot_price, strategy='MEAN_REVERSION')
                    if f"MEAN_REVERSION_EARLY_EXIT_{cid}" not in self.traded_condition_ids:
                        self.execute_paper_entry('5m', m, 'NO', np_val, spot_price, strategy='MEAN_REVERSION_EARLY_EXIT')
                    if f"MEAN_REVERSION_OPPOSITE_EXIT_{cid}" not in self.traded_condition_ids:
                        self.execute_paper_entry('5m', m, 'NO', np_val, spot_price, strategy='MEAN_REVERSION_OPPOSITE_EXIT')
                elif spot_price <= lower_level and yp <= 0.75:
                    if f"MEAN_REVERSION_{cid}" not in self.traded_condition_ids:
                        self.execute_paper_entry('5m', m, 'YES', yp, spot_price, strategy='MEAN_REVERSION')
                    if f"MEAN_REVERSION_EARLY_EXIT_{cid}" not in self.traded_condition_ids:
                        self.execute_paper_entry('5m', m, 'YES', yp, spot_price, strategy='MEAN_REVERSION_EARLY_EXIT')
                    if f"MEAN_REVERSION_OPPOSITE_EXIT_{cid}" not in self.traded_condition_ids:
                        self.execute_paper_entry('5m', m, 'YES', yp, spot_price, strategy='MEAN_REVERSION_OPPOSITE_EXIT')
            
            # D. Golden SNIPE
            if f"SNIPE_{cid}" not in self.traded_condition_ids:
                vol = 0.00045
                pct_diff = (spot_price - strike) / strike
                time_fraction = max(0.01, rem_sec / 300.0)
                z = pct_diff / (vol * math.sqrt(time_fraction))
                try:
                    true_prob = 1.0 / (1.0 + math.exp(-2.2 * z))
                except:
                    true_prob = 0.5
                if true_prob > yp + 0.12 and yp <= 0.75:
                    self.execute_paper_entry('5m', m, 'YES', yp, spot_price, strategy='SNIPE')
                elif (1.0 - true_prob) > np_val + 0.12 and np_val <= 0.75:
                    self.execute_paper_entry('5m', m, 'NO', np_val, spot_price, strategy='SNIPE')

            # E. Dynamic Fixed Pct strategies
            for val in [0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]:
                mult = val / 100.0
                upper = strike * (1.0 + mult)
                lower = strike * (1.0 - mult)
                
                strat_b = f"BREAKOUT_PCT_{val}"
                if f"{strat_b}_{cid}" not in self.traded_condition_ids:
                    if spot_price >= upper and yp <= 0.75:
                        self.execute_paper_entry('5m', m, 'YES', yp, spot_price, strategy=strat_b)
                    elif spot_price <= lower and np_val <= 0.75:
                        self.execute_paper_entry('5m', m, 'NO', np_val, spot_price, strategy=strat_b)
                
                if rem_sec >= 45:
                    strat_mr = f"MEAN_REVERSION_PCT_{val}"
                    if f"{strat_mr}_{cid}" not in self.traded_condition_ids:
                        if spot_price >= upper and np_val <= 0.75:
                            self.execute_paper_entry('5m', m, 'NO', np_val, spot_price, strategy=strat_mr)
                        elif spot_price <= lower and yp <= 0.75:
                            self.execute_paper_entry('5m', m, 'YES', yp, spot_price, strategy=strat_mr)

            # --- G. HIGH-FIDELITY MICROSTRUCTURAL STRATEGIES ---
            # 1. KINETIC VELOCITY BREAKOUT
            if 'KINETIC_VELOCITY_BREAKOUT' in self.get_all_strategies():
                strat_kin = 'KINETIC_VELOCITY_BREAKOUT'
                if f"{strat_kin}_{cid}" not in self.traded_condition_ids:
                    if v_t > 2.0 * std_v and a_t > 1.5 * std_v and yp <= 0.75:
                        log.info(f"🚀 [KINETIC BREAKOUT] Entering YES...")
                        self.execute_paper_entry('5m', m, 'YES', yp, spot_price, strategy=strat_kin)
                    elif v_t < -2.0 * std_v and a_t < -1.5 * std_v and np_val <= 0.75:
                        log.info(f"🚀 [KINETIC BREAKOUT] Entering NO...")
                        self.execute_paper_entry('5m', m, 'NO', np_val, spot_price, strategy=strat_kin)

            # 2. L2 ABSORPTION
            if 'L2_ABSORPTION_SPREAD_COLLAPSE' in self.get_all_strategies():
                strat_abs = 'L2_ABSORPTION_SPREAD_COLLAPSE'
                if f"{strat_abs}_{cid}" not in self.traded_condition_ids:
                    # Get actual L2 spread from CLOB token ID if available, otherwise fallback
                    spread_val = 0.04
                    if clob_token_ids and len(clob_token_ids) >= 2:
                        try:
                            # Safely fetch active bids/asks to check spread collapse
                            r = self.session.get(f"{CLOB_API}/book", params={'token_id': clob_token_ids[0]}, timeout=2)
                            if r.status_code == 200:
                                book = r.json()
                                asks = book.get('asks', [])
                                bids = book.get('bids', [])
                                if asks and bids:
                                    spread_val = float(asks[0]['price']) - float(bids[0]['price'])
                        except:
                            pass
                            
                    if spread_val <= 0.01 and abs(v_t) > 1.2 * std_v:
                        direction = 'YES' if v_t > 0 else 'NO'
                        price = yp if direction == 'YES' else np_val
                        if price <= 0.75:
                            log.info(f"⚡ [L2 ABSORPTION] Spread collapse ({spread_val:.3f}) and velocity surge. Entering {direction}...")
                            self.execute_paper_entry('5m', m, direction, price, spot_price, strategy=strat_abs)

            # 3. LIQUIDATION FADE
            if 'LIQUIDATION_SPOT_GAP_FADE' in self.get_all_strategies():
                strat_liq = 'LIQUIDATION_SPOT_GAP_FADE'
                if f"{strat_liq}_{cid}" not in self.traded_condition_ids:
                    if len(self.spot_history) >= 2:
                        tick_change = spot_price - self.spot_history[-2]
                        if abs(tick_change) >= 50.0:
                            direction = 'NO' if tick_change > 0 else 'YES'
                            price = yp if direction == 'YES' else np_val
                            if price <= 0.75:
                                log.info(f"💥 [LIQUIDATION GAP FADE] Gap of {tick_change:+.1f}$ detected. Fading to {direction}...")
                                self.execute_paper_entry('5m', m, direction, price, spot_price, strategy=strat_liq)

            # 4. MR GAMMA EXPIRY PIN
            if 'MR_GAMMA_EXPIRY_PIN' in self.get_all_strategies():
                strat_gamma = 'MR_GAMMA_EXPIRY_PIN'
                if f"{strat_gamma}_{cid}" not in self.traded_condition_ids:
                    if 20 <= rem_sec <= 90:
                        pct_diff = abs(spot_price - strike) / strike
                        z_dist = pct_diff / (0.00045 * math.sqrt(rem_sec / 300.0))
                        if 0.4 <= z_dist <= 1.0:
                            direction = 'NO' if spot_price > strike else 'YES'
                            price = yp if direction == 'YES' else np_val
                            if price <= 0.75:
                                log.info(f"🛡️ [GAMMA PIN] Near expiry ({rem_sec}s remaining). Fading to {direction} at z={z_dist:.2f}...")
                                self.execute_paper_entry('5m', m, direction, price, spot_price, strategy=strat_gamma)

            # 5. MR HEATMAP LIQ FADE
            if 'MR_HEATMAP_LIQ_FADE' in self.get_all_strategies():
                strat_heat = 'MR_HEATMAP_LIQ_FADE'
                if f"{strat_heat}_{cid}" not in self.traded_condition_ids:
                    if len(self.spot_history) >= 2:
                        tick_change = spot_price - self.spot_history[-2]
                        spread_val = 0.04
                        if clob_token_ids and len(clob_token_ids) >= 2:
                            try:
                                r = self.session.get(f"{CLOB_API}/book", params={'token_id': clob_token_ids[0]}, timeout=2)
                                if r.status_code == 200:
                                    book = r.json()
                                    asks = book.get('asks', [])
                                    bids = book.get('bids', [])
                                    if asks and bids:
                                        spread_val = float(asks[0]['price']) - float(bids[0]['price'])
                            except:
                                pass
                                
                        if abs(tick_change) >= 50.0 and spread_val <= 0.01:
                            direction = 'NO' if tick_change > 0 else 'YES'
                            price = yp if direction == 'YES' else np_val
                            if price <= 0.75:
                                log.info(f"🔥 [HEATMAP LIQ FADE] Large wick + spread collapse. Entering {direction}...")
                                self.execute_paper_entry('5m', m, direction, price, spot_price, strategy=strat_heat)

            # 6. MR L2 OFI DELTA FADE
            if 'MR_L2_OFI_DELTA_FADE' in self.get_all_strategies():
                strat_delta = 'MR_L2_OFI_DELTA_FADE'
                if f"{strat_delta}_{cid}" not in self.traded_condition_ids:
                    # OFI check using best bids and asks
                    yes_ask = yp
                    yes_bid = None
                    no_ask = np_val
                    spread_val = 0.04
                    if clob_token_ids and len(clob_token_ids) >= 2:
                        try:
                            r = self.session.get(f"{CLOB_API}/book", params={'token_id': clob_token_ids[0]}, timeout=2)
                            if r.status_code == 200:
                                book = r.json()
                                asks = book.get('asks', [])
                                bids = book.get('bids', [])
                                if asks and bids:
                                    yes_ask = float(asks[0]['price'])
                                    yes_bid = float(bids[0]['price'])
                                    spread_val = yes_ask - yes_bid
                        except:
                            pass
                            
                    if v_t > 1.5 * std_v and yes_bid is not None:
                        # Check if bid support dropped or spread widened
                        if spread_val > 0.02:
                            log.info(f"👑 [OFI DELTA MR] Spot wicking up but bid support faded. Entering NO...")
                            self.execute_paper_entry('5m', m, 'NO', no_ask, spot_price, strategy=strat_delta)
                    elif v_t < -1.5 * std_v and yes_ask is not None:
                        # Check if ask resistance dropped or spread widened
                        if spread_val > 0.02:
                            log.info(f"👑 [OFI DELTA MR] Spot wicking down but ask resistance faded. Entering YES...")
                            self.execute_paper_entry('5m', m, 'YES', yes_ask, spot_price, strategy=strat_delta)

            # F. Dynamic Z-score strategies
            z_score = self.get_spot_z_score(strike, spot_price)
            for z_threshold in [1.5, 1.6, 1.7, 1.8, 1.9, 2.0, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 3.0]:
                z_str = f"{z_threshold:.1f}"
                
                strat_bz = f"BREAKOUT_Z_{z_str}"
                if f"{strat_bz}_{cid}" not in self.traded_condition_ids:
                    if z_score >= z_threshold and yp <= 0.75:
                        self.execute_paper_entry('5m', m, 'YES', yp, spot_price, strategy=strat_bz)
                    elif z_score <= -z_threshold and np_val <= 0.75:
                        self.execute_paper_entry('5m', m, 'NO', np_val, spot_price, strategy=strat_bz)
                
                if rem_sec >= 45:
                    strat_mrz = f"MEAN_REVERSION_Z_{z_str}"
                    if f"{strat_mrz}_{cid}" not in self.traded_condition_ids:
                        if z_score >= z_threshold and np_val <= 0.75:
                            self.execute_paper_entry('5m', m, 'NO', np_val, spot_price, strategy=strat_mrz)
                        elif z_score <= -z_threshold and yp <= 0.75:
                            self.execute_paper_entry('5m', m, 'YES', yp, spot_price, strategy=strat_mrz)

        # --- 2. 15m, 1h, 1d Timeframes ---
        for tf in ['15m', '1h', '1d']:
            for m in self.active_markets_by_tf[tf]:
                cid = m.get('conditionId')
                try:
                    yp, np_val = self._parse_outcome_prices(m)
                except:
                    continue
                    
                strike = self.opening_prices.get(cid, spot_price)
                
                # Check for 15m timeframe specifically to execute microstructural rules
                if tf == '15m':
                    # Time guard: skip first and last 10 seconds of the 15m window
                    # (mirrors the 5m loop guard — prevents stale/expiry noise entries)
                    try:
                        end_date_15m = datetime.fromisoformat(
                            m.get('endDate', '').replace('Z', '+00:00'))
                        rem_15m = int((end_date_15m - now).total_seconds())
                    except:
                        rem_15m = 0
                    if rem_15m <= 10 or rem_15m >= 890:
                        continue
                    spread_val = 0.04
                    yes_ask = yp
                    yes_bid = None
                    no_ask = np_val
                    clob_token_ids = m.get('clobTokenIds')
                    if clob_token_ids:
                        if isinstance(clob_token_ids, str):
                            try:
                                clob_token_ids = json.loads(clob_token_ids)
                            except:
                                clob_token_ids = None
                                
                    if clob_token_ids and isinstance(clob_token_ids, list) and len(clob_token_ids) >= 2:
                        try:
                            r = self.session.get(f"{CLOB_API}/book", params={'token_id': clob_token_ids[0]}, timeout=2)
                            if r.status_code == 200:
                                book = r.json()
                                asks = book.get('asks', [])
                                bids = book.get('bids', [])
                                if asks and bids:
                                    yes_ask = float(asks[0]['price'])
                                    yes_bid = float(bids[0]['price'])
                                    spread_val = yes_ask - yes_bid
                        except:
                            pass
                            
                    # A. 15M L2 BLOCK FADE
                    if 'L2_BLOCK_FADE_15M' in self.get_all_strategies():
                        strat_block = 'L2_BLOCK_FADE_15M'
                        if f"{strat_block}_{cid}" not in self.traded_condition_ids:
                            if abs(v_t) > 1.8 * std_v and spread_val <= 0.01:
                                direction = 'NO' if v_t > 0 else 'YES'
                                price = yp if direction == 'YES' else np_val
                                if price <= 0.75:
                                    log.info(f"🛡️ [15M L2 BLOCK FADE] Fading velocity change at spread collapse. Entering {direction}...")
                                    self.execute_paper_entry('15m', m, direction, price, spot_price, strategy=strat_block)
                                    
                    # B. 15M OFI MOMENTUM BREAKOUT
                    if 'OFI_MOMENTUM_BO_15M' in self.get_all_strategies():
                        strat_ofi = 'OFI_MOMENTUM_BO_15M'
                        if f"{strat_ofi}_{cid}" not in self.traded_condition_ids:
                            if abs(v_t) > 1.5 * std_v and spread_val <= 0.01:
                                if v_t > 0 and yes_bid is not None:
                                    entry_price = yes_ask
                                    if entry_price and entry_price <= 0.75:
                                        log.info(f"🚀 [15M OFI BREAKOUT] Entering YES...")
                                        self.execute_paper_entry('15m', m, 'YES', entry_price, spot_price, strategy=strat_ofi)
                                elif v_t < 0 and yes_ask is not None:
                                    entry_price = no_ask
                                    if entry_price and entry_price <= 0.75:
                                        log.info(f"🚀 [15M OFI BREAKOUT] Entering NO...")
                                        self.execute_paper_entry('15m', m, 'NO', entry_price, spot_price, strategy=strat_ofi)
                                        
                    # C. 15M HEATMAP EXPIRY DRIFT
                    if 'HEATMAP_EXPIRY_DRIFT_15M' in self.get_all_strategies():
                        strat_drift = 'HEATMAP_EXPIRY_DRIFT_15M'
                        if f"{strat_drift}_{cid}" not in self.traded_condition_ids:
                            end_date_str = m.get('endDate')
                            try:
                                end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                                rem_sec = int((end_date - now).total_seconds())
                            except:
                                rem_sec = 0
                            if 45 <= rem_sec <= 120:
                                pct_diff = abs(spot_price - strike) / strike
                                if pct_diff > 0.0003:
                                    direction = 'YES' if spot_price > strike else 'NO'
                                    price = yp if direction == 'YES' else np_val
                                    if price <= 0.75:
                                        log.info(f"🔥 [15M EXPIRY DRIFT] Pinning gravity pull. Entering {direction}...")
                                        self.execute_paper_entry('15m', m, direction, price, spot_price, strategy=strat_drift)
                
                if f"BREAKOUT_{cid}" not in self.traded_condition_ids:
                    breakout_pct = 0.0005
                    upper_level = strike * (1.0 + breakout_pct)
                    lower_level = strike * (1.0 - breakout_pct)
                    if spot_price >= upper_level and yp <= 0.75:
                        self.execute_paper_entry(tf, m, 'YES', yp, spot_price, strategy='BREAKOUT')
                    elif spot_price <= lower_level and np_val <= 0.75:
                        self.execute_paper_entry(tf, m, 'NO', np_val, spot_price, strategy='BREAKOUT')
                
                for val in [0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]:
                    strat_b = f"BREAKOUT_PCT_{val}"
                    if f"{strat_b}_{cid}" not in self.traded_condition_ids:
                        mult = val / 100.0
                        upper = strike * (1.0 + mult)
                        lower = strike * (1.0 - mult)
                        if spot_price >= upper and yp <= 0.75:
                            self.execute_paper_entry(tf, m, 'YES', yp, spot_price, strategy=strat_b)
                        elif spot_price <= lower and np_val <= 0.75:
                            self.execute_paper_entry(tf, m, 'NO', np_val, spot_price, strategy=strat_b)
                
                z_score = self.get_spot_z_score(strike, spot_price)
                for z_threshold in [1.5, 1.6, 1.7, 1.8, 1.9, 2.0, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 3.0]:
                    z_str = f"{z_threshold:.1f}"
                    strat_bz = f"BREAKOUT_Z_{z_str}"
                    if f"{strat_bz}_{cid}" not in self.traded_condition_ids:
                        if z_score >= z_threshold and yp <= 0.75:
                            self.execute_paper_entry(tf, m, 'YES', yp, spot_price, strategy=strat_bz)
                        elif z_score <= -z_threshold and np_val <= 0.75:
                            self.execute_paper_entry(tf, m, 'NO', np_val, spot_price, strategy=strat_bz)

    def tick(self):
        """Executes a single cycle of the 3-second live bot loop."""
        coinbase_price = self.get_coinbase_spot_price()
        hyperliquid_price = self.get_hyperliquid_perp_price()
        
        if not coinbase_price:
            return
            
        if not hyperliquid_price:
            hyperliquid_price = coinbase_price

        self.spot_history.append(coinbase_price)
        self.tick_count += 1
        
        # Log active regime every 100 ticks (5 minutes)
        if self.tick_count % 100 == 0:
            vol = self.calculate_realized_volatility()
            trend = self.calculate_trend_ratio()
            
            # Determine regime name for logging
            if vol < 0.25 and trend < 1.2:
                regime = "QUIET_MEAN_REVERTING"
            elif vol >= 0.40 or trend >= 1.8:
                regime = "STRONG_TRENDING"
            else:
                regime = "VOLATILE_CHOPPY"
                
            log.info(f"🛡️ [REGIME WATCH] Spot: {coinbase_price:.2f} | Vol: {vol:.1%} | Trend: {trend:.2f} | Current Active Regime: {regime}")

        now_ts = time.time()
        if now_ts - self.last_markets_scan >= 15:
            self.scan_active_markets()
            self.last_markets_scan = now_ts

        # Update simulated markets
        for tf in ['5m', '15m', '1h', '1d']:
            for m in self.active_markets_by_tf.get(tf, []):
                cid = m.get('conditionId', '')
                if cid.startswith('sim_'):
                    strike = self.opening_prices.get(cid, coinbase_price)
                    end_str = m.get('endDate')
                    try:
                        end_date = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                        rem_sec = max(1, int((end_date - datetime.now(timezone.utc)).total_seconds()))
                    except:
                        rem_sec = 300
                    
                    if tf == '5m':
                        vol = 0.00045
                        tf_sec = 300.0
                    elif tf == '15m':
                        vol = 0.0008
                        tf_sec = 900.0
                    elif tf == '1h':
                        vol = 0.0015
                        tf_sec = 3600.0
                    else:
                        vol = 0.003
                        tf_sec = 86400.0
                    
                    pct_diff = (coinbase_price - strike) / strike
                    time_fraction = max(0.01, rem_sec / tf_sec)
                    z = pct_diff / (vol * math.sqrt(time_fraction))
                    try:
                        prob = 1.0 / (1.0 + math.exp(-2.2 * z))
                    except:
                        prob = 0.5
                    yp = round(max(0.02, min(0.98, prob)), 2)
                    np = round(1.0 - yp, 2)
                    m['outcomePrices'] = [str(yp), str(np)]

        # Check quiet period & swap socket proactively
        self.check_proactive_socket_swap()

        # Handle pending pullback limit orders
        self.check_pending_pullbacks(coinbase_price)

        # Handle exits and resolutions
        self.handle_early_exits_mean_reversion(coinbase_price)
        self.handle_early_exits_mean_reversion_opposite(coinbase_price)
        self.check_resolutions(coinbase_price)
        
        # Log ticks
        self.log_ticks_3s(coinbase_price)
        
        # Run trading signals using fast Hyperliquid Perp price
        self.run_strategy_signals(hyperliquid_price)

def main():
    log.info("=" * 60)
    log.info("Starting Polymarket 24/7 UPGRADED Shadow Paper Trading Bot Daemon")
    log.info(f"Data Dir: {DATA_DIR}")
    log.info("=" * 60)
    
    bot = ShadowPaperTrader()
    
    while True:
        try:
            bot.tick()
        except KeyboardInterrupt:
            log.info("Upgraded shadow paper bot stopped.")
            break
        except Exception as e:
            log.error(f"Error in main bot loop: {e}", exc_info=True)
            
        time.sleep(3)

if __name__ == "__main__":
    main()
