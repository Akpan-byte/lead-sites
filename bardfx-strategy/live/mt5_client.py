import time

try:
    import MetaTrader5 as mt5
except ImportError:
    # MT5 is only natively installable on Windows, but on Linux we can import it 
    # and mock it for testing purposes if the binary package is not available.
    mt5 = None

class MT5Client:
    def __init__(self, login_id, password, server, magic_number=9999):
        try:
            self.login_id = int(login_id) if login_id else None
        except (ValueError, TypeError):
            print(f"Warning: MT5 login_id '{login_id}' is invalid or not configured. Using None.")
            self.login_id = None
        self.password = password
        self.server = server
        self.magic_number = magic_number
        self.connected = False
        
    def authenticate(self):
        """Initializes and logs into the MetaTrader 5 terminal."""
        if mt5 is None:
            print("MetaTrader5 python module is not available on this platform (requires Windows or Wine). Mocking MT5 connection.")
            self.connected = True
            return True
            
        print(f"Connecting to MT5 Server: {self.server} for User: {self.login_id}...")
        if not mt5.initialize():
            print(f"MT5 initialize() failed: {mt5.last_error()}")
            return False
            
        authorized = mt5.login(login=self.login_id, password=self.password, server=self.server)
        if not authorized:
            print(f"MT5 login failed for user {self.login_id}: {mt5.last_error()}")
            mt5.shutdown()
            return False
            
        print("MT5 login successful.")
        self.connected = True
        return True

    def place_order(self, symbol, action, qty, price=None, sl=None, tp=None, order_type="Market"):
        """Places a trade or pending limit order on MT5 with optional bracket protection (SL/TP)."""
        if not self.connected:
            if not self.authenticate():
                print("Cannot place order. MT5 not connected.")
                return None
                
        if mt5 is None:
            print(f"[MOCK MT5] Placed Order: {action} {qty} {symbol} ({order_type}) @ {price} | SL: {sl} | TP: {tp}")
            return 123456 # Mock Ticket ID
            
        # Determine order type constants
        if order_type.lower() == "market":
            trade_type = mt5.ORDER_TYPE_BUY if action.lower() == "buy" else mt5.ORDER_TYPE_SELL
            action_type = mt5.TRADE_ACTION_DEAL
            fill_price = price if price else mt5.symbol_info_tick(symbol).ask if action.lower() == "buy" else mt5.symbol_info_tick(symbol).bid
        elif order_type.lower() == "limit":
            trade_type = mt5.ORDER_TYPE_BUY_LIMIT if action.lower() == "buy" else mt5.ORDER_TYPE_SELL_LIMIT
            action_type = mt5.TRADE_ACTION_PENDING
            fill_price = price
        else:
            print(f"Unsupported MT5 order type: {order_type}")
            return None

        # Build trade request
        request = {
            "action": action_type,
            "symbol": symbol,
            "volume": float(qty),
            "type": trade_type,
            "price": float(fill_price),
            "magic": self.magic_number,
            "comment": "Bard FX Algorithmic Execution",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if sl:
            request["sl"] = float(sl)
        if tp:
            request["tp"] = float(tp)
            
        # Send request
        print(f"Sending order to MT5: {action} {qty} {symbol} @ {fill_price}...")
        result = mt5.order_send(request)
        if result is None:
            print(f"Order send returned None. Last error: {mt5.last_error()}")
            return None
            
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            print(f"Order rejected by server! Retcode: {result.retcode} | Comment: {result.comment}")
            return None
            
        print(f"Order execution successful! Ticket ID: {result.order}")
        return result.order

    def cancel_order(self, ticket_id):
        """Deletes a pending limit/stop order."""
        if mt5 is None:
            print(f"[MOCK MT5] Cancelled Order Ticket: {ticket_id}")
            return True
            
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": int(ticket_id)
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"Pending order {ticket_id} cancelled.")
            return True
        print(f"Failed to cancel pending order {ticket_id}: {result.comment if result else 'No response'}")
        return False

    def shutdown(self):
        if mt5 is not None:
            mt5.shutdown()
            print("MT5 connection closed.")
