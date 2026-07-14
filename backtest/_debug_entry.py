#!/usr/bin/env python3
"""Debug: tally WHY check_entry rejects triggered signals (first failing gate)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
import driver
from engine.portfolio import Portfolio, MIN_ENTRY_PRICE
from engine.execution import (position_notional, walk_book_buy, ev_slippage_allowance,
                              latency_guard, calculate_taker_fee, taker_fee_shares,
                              QUEUE_FILL_FRACTION, DEFAULT_MAX_ENTRY_PRICE)

name, path = "breakout_pct_003", "/tmp/pb_sample.json"
reg = driver.STRATEGIES[name]; fn = driver.load_signal_fn(reg)
snaps = driver.load_market_file(path)

from collections import Counter
reasons = Counter()
# replicate run_market to reach the entry point, then gate-check manually
snaps = sorted(snaps, key=lambda s: s["time"])
t_end = driver._parse_ts(snaps[-1]["time"]); strike = float(snaps[0]["btc_price"])
market = {"condition_id": str(snaps[0]["market_id"]), "token_id_yes": "yes", "token_id_no": "no",
          "end_date_iso": t_end.isoformat(), "start_date_iso": driver._parse_ts(snaps[0]["time"]).isoformat(),
          "open_oracle_price": strike, "strike": strike, "duration": "5m", "fee_schedule": None, "asset": "BTC"}
hist = []
for snap in snaps:
    btc = float(snap.get("btc_price") or 0.0)
    if btc <= 0: continue
    hist.append(btc); hist = hist[-driver.SPOT_HISTORY_MAX_LEN:]
    t = driver._parse_ts(snap["time"]); rem_sec = max(0.0, (t_end - t).total_seconds())
    up = driver.norm_book(snap.get("orderbook_up")); dn = driver.norm_book(snap.get("orderbook_down"))
    pu = float(snap.get("price_up") or 0.0); pd = float(snap.get("price_down") or 0.0)
    yp = driver.best(up, "bids", pu); yes_ask = driver.best(up, "asks", pu)
    np_val = driver.best(dn, "bids", pd); no_ask = driver.best(dn, "asks", pd)
    state = {"spot_price": btc, "spot_history": hist, "rem_sec": rem_sec, "yp": yp, "np_val": np_val,
             "yes_ask": yes_ask, "yes_bid": yp, "no_ask": no_ask, "no_bid": np_val}
    sig = fn(**driver._build_signal_kwargs(reg, market, state))
    if not (sig and sig.get("triggered")): continue
    direction = sig["direction"]; book = up if direction == "YES" else dn
    ep = float(sig.get("entry_price") or 0.0)
    if ep <= 0: reasons["entry_price<=0"] += 1; continue
    if ep < MIN_ENTRY_PRICE: reasons["below_MIN_ENTRY_PRICE"] += 1; continue
    if ep > DEFAULT_MAX_ENTRY_PRICE: reasons["above_max_entry_price"] += 1; continue
    tn = position_notional(200.0, ep, 0.005, 5)
    if tn <= 0: reasons["target_notional<=0"] += 1; continue
    if not book.get("asks"): reasons["no_book_asks"] += 1; continue
    fill = walk_book_buy(book, tn, queue_fraction=QUEUE_FILL_FRACTION, fee_schedule=None)
    af = fill["avg_fill_price"]; gs = fill["shares"]
    if gs <= 0 or af <= 0: reasons["no_liquidity"] += 1; continue
    fair = (yes_ask + yp) / 2.0 if direction == "YES" else (no_ask + np_val) / 2.0
    if not ev_slippage_allowance(sig, af, fair_price=fair): reasons["ev_slippage"] += 1; continue
    if not latency_guard(book, book, direction): reasons["latency_guard"] += 1; continue
    entry_cap = min(DEFAULT_MAX_ENTRY_PRICE, ep + 0.01)
    if af > entry_cap: reasons["avg_fill>entry_cap"] += 1; continue
    reasons["WOULD_FILL"] += 1

print("rejection tally (first failing gate) for", name, "on", os.path.basename(path))
for k, v in reasons.most_common():
    print(f"  {k}: {v}")
print("sum:", sum(reasons.values()))
