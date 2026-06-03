#!/usr/bin/env python3
"""
Live Execution Broker Stress & Simulation Test
==============================================
Simulates incoming signals to verify that:
1. Routing logic correctly handles CME Futures (Tradovate) vs. CFDs (MT5).
2. The Max Trades Per Day circuit breaker triggers on consecutive signals.
3. The Daily Loss Limit gate correctly rejects trades when realized losses exceed -$600.
"""

import sys
import os
import shutil
import json
from pathlib import Path

# Add live directory to path
sys.path.append(str(Path(__file__).parent))
from live_execution_broker import LiveExecutionBroker, STATE_FILE

def reset_test_state():
    """Removes the persistent state file to ensure a clean test baseline."""
    if STATE_FILE.exists():
        os.remove(STATE_FILE)
    print("State file successfully reset.")

def run_stress_test():
    print("=" * 80)
    print("LIVE EXECUTION BROKER STRESS & CIRCUIT BREAKER TEST")
    print("=" * 80)
    
    reset_test_state()
    
    # Initialize broker in Demo/Simulation mode
    broker = LiveExecutionBroker(demo=True)
    
    # -------------------------------------------------------------------------
    # TEST CASE 1: Standard Futures & CFD Signal Routing
    # -------------------------------------------------------------------------
    print("\n--- TEST 1: Routing standard signal ---")
    futures_signal = {
        "asset": "/GC",
        "action": "Buy",
        "qty": 1,
        "order_type": "Limit",
        "price": 2350.0,
        "sl": 2340.0,
        "tp": 2380.0
    }
    # Should route (will use mock credentials if not configured)
    broker.execute_signal(futures_signal)
    
    cfd_signal = {
        "asset": "EURUSD",
        "action": "Buy",
        "qty": 0.1,
        "order_type": "Limit",
        "price": 1.0850,
        "sl": 1.0820,
        "tp": 1.0940
    }
    broker.execute_signal(cfd_signal)
    
    # -------------------------------------------------------------------------
    # TEST CASE 2: Max Trades Per Day Gating
    # -------------------------------------------------------------------------
    print("\n--- TEST 2: Gating on Max Trades Per Day limit ---")
    duplicate_signal = {
        "asset": "/GC",
        "action": "Buy",
        "qty": 1,
        "order_type": "Limit",
        "price": 2352.0,
        "sl": 2342.0,
        "tp": 2382.0
    }
    # This should be REJECTED because we already took a trade on /GC today
    broker.execute_signal(duplicate_signal)
    
    # -------------------------------------------------------------------------
    # TEST CASE 3: Daily Loss Limit Circuit Breaker
    # -------------------------------------------------------------------------
    print("\n--- TEST 3: Gating on Daily Loss Limit (-$600) ---")
    # Simulate a realized daily loss of -$800
    broker.state["realized_pnl"] = -800.0
    broker._save_state(broker.state)
    
    new_signal = {
        "asset": "USDJPY",
        "action": "Buy",
        "qty": 0.05,
        "order_type": "Market",
        "price": 155.20,
        "sl": 154.50,
        "tp": 156.50
    }
    # This should be REJECTED immediately because of the daily loss breaker
    broker.execute_signal(new_signal)
    
    print("\n" + "=" * 80)
    print("STRESS TEST COMPLETED")
    print("=" * 80)
    
    # Clean up state file to avoid impacting actual live bot runs
    reset_test_state()

if __name__ == "__main__":
    run_stress_test()
