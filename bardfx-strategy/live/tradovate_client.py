import os
import time
import requests
import json

class TradovateClient:
    def __init__(self, username, password, app_id, app_version="1.0", client_id=None, client_secret=None, demo=True):
        self.username = username
        self.password = password
        self.app_id = app_id
        self.app_version = app_version
        self.client_id = client_id
        self.client_secret = client_secret
        
        self.base_url = "https://demo.tradovateapi.com/v1" if demo else "https://live.tradovateapi.com/v1"
        self.token = None
        self.token_expiration = 0
        self.account_id = None
        self.account_spec = None
        self.positions = {}
        self.orders = {}
        
    def authenticate(self):
        """Authenticates with Tradovate and retrieves an access token."""
        print(f"Authenticating Tradovate user: {self.username}...")
        url = f"{self.base_url}/auth/accessTokenRequest"
        payload = {
            "name": self.username,
            "password": self.password,
            "appId": self.app_id,
            "appVersion": self.app_version
        }
        if self.client_id:
            payload["cid"] = self.client_id
        if self.client_secret:
            payload["sec"] = self.client_secret
            
        try:
            r = requests.post(url, json=payload, timeout=15)
            r.raise_for_status()
            data = r.json()
            self.token = data.get("accessToken")
            # Set expiration time (ttl is in seconds, e.g. 12 hours)
            ttl = data.get("timeToExpiration", 43200)
            self.token_expiration = time.time() + ttl - 60 # 1 minute buffer
            print("Tradovate authentication successful.")
            self._get_account_info()
            return True
        except Exception as e:
            print(f"Tradovate authentication failed: {e}")
            return False
            
    def _get_account_info(self):
        """Retrieves default account details."""
        url = f"{self.base_url}/account/list"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            r = requests.get(url, headers=headers, timeout=10)
            r.raise_for_status()
            accounts = r.json()
            if accounts:
                # Use the first active account
                self.account_id = accounts[0].get("id")
                self.account_spec = accounts[0].get("name")
                print(f"Active Account: {self.account_spec} (ID: {self.account_id})")
        except Exception as e:
            print(f"Error fetching account info: {e}")

    def place_order(self, symbol, action, qty, order_type="Limit", price=None, stop_price=None):
        """Submits an order request to Tradovate."""
        if time.time() >= self.token_expiration:
            self.authenticate()
            
        url = f"{self.base_url}/order/placeorder"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "accountSpec": self.account_spec,
            "accountId": self.account_id,
            "action": action, # 'Buy' or 'Sell'
            "symbol": symbol, # e.g. 'ESM6' or 'MGC'
            "orderQty": qty,
            "orderType": order_type, # 'Limit', 'Market', 'Stop'
            "isInteractive": True
        }
        if price:
            payload["price"] = price
        if stop_price:
            payload["stopPrice"] = stop_price
            
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=10)
            r.raise_for_status()
            res = r.json()
            order_id = res.get("orderId")
            print(f"Order Placed: {action} {qty} {symbol} ({order_type}) @ {price} | Order ID: {order_id}")
            return order_id
        except Exception as e:
            print(f"Order submission failed for {symbol}: {e}")
            return None

    def cancel_order(self, order_id):
        """Cancels an active pending order."""
        if time.time() >= self.token_expiration:
            self.authenticate()
            
        url = f"{self.base_url}/order/cancelorder"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        payload = {"orderId": order_id}
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=10)
            r.raise_for_status()
            print(f"Order Cancelled: {order_id}")
            return True
        except Exception as e:
            print(f"Order cancellation failed for {order_id}: {e}")
            return False

    def get_positions(self):
        """Fetches active open positions for the account."""
        if time.time() >= self.token_expiration:
            self.authenticate()
            
        url = f"{self.base_url}/position/list"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            r = requests.get(url, headers=headers, timeout=10)
            r.raise_for_status()
            pos_list = r.json()
            self.positions = {p['symbol']: p for p in pos_list if p.get('netQty', 0) != 0}
            return self.positions
        except Exception as e:
            print(f"Error fetching positions: {e}")
            return {}
