import os
import sys
import json
import datetime
from pathlib import Path
from dotenv import load_dotenv

# Add directory containing client wrappers to path
sys.path.append(str(Path(__file__).parent))
from tradovate_client import TradovateClient
from mt5_client import MT5Client

STATE_FILE = Path("/config/bardfx-strategy/live/static_dashboard/live_broker_state.json")

class LiveExecutionBroker:
    def __init__(self, demo=True):
        self.demo = demo
        
        # Load environment credentials
        env_path = Path("/config/hl-nq-bot/.env")
        load_dotenv(dotenv_path=env_path)
        
        # 1. Tradovate Config (Topstep Futures)
        self.tv_username = os.getenv("TRADOVATE_USERNAME", "demo_user")
        self.tv_password = os.getenv("TRADOVATE_PASSWORD", "demo_password")
        self.tv_app_id = os.getenv("TRADOVATE_APP_ID", "AppName")
        
        # 2. MT5 Config (Lucid CFD/Forex/Crypto)
        self.mt5_login = os.getenv("MT5_LOGIN_ID")
        self.mt5_password = os.getenv("MT5_PASSWORD")
        self.mt5_server = os.getenv("MT5_SERVER")
        
        # Initialize clients
        self.tv_client = TradovateClient(self.tv_username, self.tv_password, self.tv_app_id, demo=self.demo)
        self.mt5_client = MT5Client(self.mt5_login, self.mt5_password, self.mt5_server)
        
        # Risk thresholds
        self.daily_loss_limit = 600.0 # $600 max daily loss
        self.max_trades_per_day = 1 # 1 trade per asset per day
        
        self.state = self._load_state()

    def _load_state(self):
        """Loads execution state from a persistent JSON file."""
        today = str(datetime.date.today())
        default_state = {
            "date": today,
            "realized_pnl": 0.0,
            "trades_count": {},
            "active_orders": {}
        }
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r") as f:
                    state = json.load(f)
                # Reset state if it's a new day
                if state.get("date") != today:
                    print("New day detected. Resetting broker execution limits.")
                    self._save_state(default_state)
                    return default_state
                return state
            except Exception as e:
                print(f"Error reading state file, using defaults: {e}")
                return default_state
        else:
            self._save_state(default_state)
            return default_state

    def _save_state(self, state):
        """Saves current state to persistent storage."""
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=4)
        except Exception as e:
            print(f"Error saving state file: {e}")

    def execute_signal(self, signal):
        """
        Processes and routes an incoming strategy signal to the correct broker.
        
        Parameters:
        -----------
        signal : dict
            {
                "asset": str (e.g. "/GC", "GBPUSD", "SOLUSD"),
                "action": str ('Buy' or 'Sell'),
                "qty": int/float (quantity to trade),
                "order_type": str ('Limit' or 'Market'),
                "price": float (entry price),
                "sl": float (stop loss price),
                "tp": float (take profit price)
            }
        """
        asset = signal["asset"]
        action = signal["action"]
        qty = signal["qty"]
        price = signal["price"]
        sl = signal.get("sl")
        tp = signal.get("tp")
        
        print("\n" + "=" * 60)
        print(f"INCOMING SIGNAL: {action} {qty} {asset} @ {price}")
        print("=" * 60)
        
        # 1. Check Circuit Breakers
        today = str(datetime.date.today())
        if self.state["date"] != today:
            self.state = self._load_state()
            
        if self.state["realized_pnl"] <= -self.daily_loss_limit:
            print(f"🚫 SIGNAL REJECTED: Daily Loss Limit Gated (-${abs(self.state['realized_pnl'])} >= -${self.daily_loss_limit})")
            return False
            
        asset_trades = self.state["trades_count"].get(asset, 0)
        if asset_trades >= self.max_trades_per_day:
            print(f"🚫 SIGNAL REJECTED: Max Trades per Day limit reached for {asset} ({asset_trades} >= {self.max_trades_per_day})")
            return False
            
        # 2. Route Signal
        is_futures = asset.startswith("/")
        success = False
        
        if is_futures:
            # CME Futures -> Tradovate
            print(f"Routing futures signal to Tradovate: {asset}...")
            if not self.tv_client.token:
                if not self.tv_client.authenticate():
                    print("❌ Error: Tradovate client failed to authenticate.")
                    return False
                    
            # For futures, we place the entry limit order. Exits are monitored locally or placed as brackets.
            # Convert slash futures symbol (e.g. /GC) to Tradovate specification if needed
            tv_symbol = asset.lstrip("/")
            order_id = self.tv_client.place_order(
                symbol=tv_symbol,
                action=action,
                qty=qty,
                order_type=signal.get("order_type", "Limit"),
                price=price
            )
            if order_id:
                success = True
                self.state["active_orders"][order_id] = {
                    "asset": asset, "action": action, "qty": qty, "price": price, "sl": sl, "tp": tp, "broker": "tradovate"
                }
        else:
            # Forex, Crypto, CFD -> MetaTrader 5
            print(f"Routing CFD/Forex signal to MT5: {asset}...")
            # Place MT5 limit/market order with native bracket parameters
            ticket_id = self.mt5_client.place_order(
                symbol=asset,
                action=action,
                qty=qty,
                price=price,
                sl=sl,
                tp=tp,
                order_type=signal.get("order_type", "Limit").lower()
            )
            if ticket_id:
                success = True
                # MT5 places bracket order natively, so we don't need to manage brackets locally
                print(f"MT5 order filled under ticket {ticket_id} with native SL ({sl}) and TP ({tp}).")
                
        if success:
            # Update state counters
            self.state["trades_count"][asset] = asset_trades + 1
            self._save_state(self.state)
            print(f"✅ SIGNAL ROUTED SUCCESSFULLY.")
            return True
            
        print("❌ SIGNAL ROUTING FAILED.")
        return False
