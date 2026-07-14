# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - Day-anchored ORB backtest harness for daily_orb_v5 (sandbox only).
#   - Seeds the 09:30 ET opening range from the causal Binance 1m feed
#     (bt_reference) and drives the live signal with a simulated clock, so the
#     wall-clock-based daily_orb_v5_signal sees historical time. Seeding the OR
#     to the true window max/min makes the signal's own in-window accumulation a
#     no-op (no spot tick can exceed the window's own high/low), so live breakout
#     / re-entry / time-gate logic is reused unchanged.
# WHY: daily_orb_v5 reads _now_et() (wall clock) and a Hyperliquid trade archive
#      for its OR; neither exists in backtest. We swap both at runtime (no live
#      file edits) and replay whole days in time order with one persistent wallet.
"""Day-anchored ORB (daily_orb_v5) backtest driver. Sandbox-only; reuses live
signal + Portfolio/execution logic, swaps only the clock and the OR data source.
"""
from __future__ import annotations

import glob
import os
import sys
import tempfile
from datetime import datetime, date
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))           # .../backtest/src
_ROOT = os.path.dirname(_HERE)                               # .../backtest
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import driver
import bt_reference
from engine.portfolio import Portfolio
from engine.execution import calculate_taker_fee, taker_fee_shares, position_notional

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

_TF_PARAMS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
}


def _active_tfs(tf_mode: str) -> list[str]:
    tf_mode = (tf_mode or "any").lower()
    if tf_mode in ("any", "anyscale"):
        return list(_TF_PARAMS.keys())
    if tf_mode in _TF_PARAMS:
        return [tf_mode]
    return ["5m"]


def _load_orb_module(reg_entry: dict):
    """Load the daily_orb_v5 signal.py by file path (bypass package __init__)."""
    import importlib.util
    from pathlib import Path
    driver._install_requests_stub()
    base = os.path.join(driver.SRC, "signals", "phase_2", "daily_orb_v5", "signal.py")
    spec = importlib.util.spec_from_file_location("_bt_daily_orb_v5", base)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Keep on-disk persistence out of the project tree during backtest (Path: _save_key calls .mkdir).
    mod._STATE_DIR = Path(tempfile.mkdtemp(prefix="bt_orb_state_"))
    return mod


def _date_et_of_file(path: str) -> date:
    snaps = driver.load_market_file(path)
    if not snaps:
        return None
    t = driver._parse_ts(snaps[0]["time"])  # UTC-aware
    return t.astimezone(ET).date(), snaps


def seed_orb(mod, reg_entry: dict, d: date) -> None:
    """Seed the 09:30-ET OR for (asset, d, tf_mode, max_re) from Binance 1m.

    Finalized (or_closed=True). Because the seeded high/low equal the window's
    true max/min, the signal's later in-window spot accumulation cannot change
    them (low <= spot <= high), so the OR stays exactly the Binance range."""
    asset = (reg_entry.get("asset") or "BTC").upper()
    tf_mode = (reg_entry.get("tf_hint") or "any").lower()
    max_re = reg_entry.get("_max_reentries") or reg_entry.get("max_reentries", 3)
    key = (asset, d, tf_mode, max_re)
    state = mod._make_state()
    state["today"] = d
    for tf in _active_tfs(tf_mode):
        or_sec = _TF_PARAMS[tf]
        rng = bt_reference.opening_range(asset, d, or_sec)
        if rng is None:
            # Data gap: leave unfinalized; signal will just not trade this tf.
            state["or_high"][tf] = float("-inf")
            state["or_low"][tf] = float("inf")
            state["or_closed"][tf] = False
            continue
        hi, lo = rng
        state["or_high"][tf] = hi
        state["or_low"][tf] = lo
        state["or_closed"][tf] = True
    mod._STATE[key] = state


def run_market_clocked(snaps: list[dict], reg_entry: dict, fn, mod, pf: Portfolio,
                       clock_holder: list) -> dict:
    """run_market_maker variant that drives the signal with a simulated clock.

    clock_holder[0] is updated to each snapshot's ET time before the signal runs,
    and mod._now_et is rebound to read it, so daily_orb_v5 sees historical time.
    """
    if not snaps:
        return {"n_closed": 0, "n_active_left": 0, "n_triggered": 0, "total_pnl": 0.0,
                "trades": [], "n_signals": 0}
    from engine.portfolio import Trade
    snaps = sorted(snaps, key=lambda s: s["time"])
    t0 = driver._parse_ts(snaps[0]["time"]); t_end = driver._parse_ts(snaps[-1]["time"])
    strike = float(snaps[0].get("btc_price") or 0.0)
    market_id = str(snaps[0].get("market_id"))
    duration = reg_entry.get("tf_hint") or "5m"
    market = {"condition_id": market_id, "token_id_yes": "yes", "token_id_no": "no",
              "end_date_iso": t_end.isoformat(), "start_date_iso": t0.isoformat(),
              "open_oracle_price": strike, "strike": strike, "duration": duration,
              "fee_schedule": None, "asset": reg_entry.get("asset") or "BTC"}
    spot_history: list[float] = []
    resting = None
    n_signals = 0; n_triggered = 0
    for snap in snaps:
        t = driver._parse_ts(snap["time"]); btc = float(snap.get("btc_price") or 0.0)
        if btc <= 0:
            continue
        # advance simulated clock to this snapshot's ET time
        clock_holder[0] = t.astimezone(ET)
        spot_history.append(btc); spot_history = spot_history[-driver.SPOT_HISTORY_MAX_LEN:]
        spot_price = btc; rem_sec = max(0.0, (t_end - t).total_seconds())
        up_a, up_b = driver.top_book(snap.get("orderbook_up"))
        dn_a, dn_b = driver.top_book(snap.get("orderbook_down"))
        pu = float(snap.get("price_up") or 0.0); pd = float(snap.get("price_down") or 0.0)
        yp = up_b if up_b > 0 else pu; yes_ask = up_a if up_a > 0 else pu
        np_val = dn_b if dn_b > 0 else pd; no_ask = dn_a if dn_a > 0 else pd
        for _ in pf.check_exits(spot_price, yp, np_val, rem_sec, oracle_spot=spot_price):
            pass
        if resting is not None and market_id not in pf.active_trades:
            ask = yes_ask if resting["direction"] == "YES" else no_ask
            if ask > 0 and ask <= resting["price"] and rem_sec > 0:
                ep = resting["price"]
                notional = position_notional(pf.cash, ep, driver.RISK_PCT, driver.MIN_CONTRACTS)
                gross_shares = int(notional / ep) if ep > 0 else 0
                gross_shares = max(gross_shares, driver.MIN_CONTRACTS) if notional > 0 else 0
                if gross_shares > 0 and pf.cash >= ep * gross_shares:
                    gross_notional = ep * gross_shares
                    entry_fee = calculate_taker_fee(gross_shares, ep, None)
                    fs = round(taker_fee_shares(gross_shares, ep, None), 2)
                    net_shares = round(max(0.0, gross_shares - fs), 2)
                    trade = Trade(condition_id=market_id, direction=resting["direction"],
                                  entry_price=ep, shares=float(gross_shares),
                                  entry_notional=gross_notional, entry_fee=entry_fee,
                                  fee_shares=fs, market=market, entry_spot=resting["entry_spot"])
                    pf._cash -= gross_notional
                    pf.active_trades[market_id] = trade
                    pf._entry_count[market_id] = pf._entry_count.get(market_id, 0) + 1
                resting = None
        if market_id in pf.active_trades:
            continue
        state = {"spot_price": spot_price, "spot_history": spot_history, "rem_sec": rem_sec,
                 "yp": yp, "np_val": np_val, "yes_ask": yes_ask, "yes_bid": yp,
                 "no_ask": no_ask, "no_bid": np_val}
        kwargs = driver._build_signal_kwargs(reg_entry, market, state)
        sig = fn(**kwargs); n_signals += 1
        if sig and sig.get("triggered"):
            n_triggered += 1
            d = sig.get("direction"); ep = float(sig.get("entry_price") or 0.0)
            if d in ("YES", "NO") and 0.05 <= ep <= 0.85:
                resting = {"direction": d, "price": ep, "entry_spot": spot_price}
    if spot_history:
        spot = spot_history[-1]
        for cid in list(pf.active_trades.keys()):
            tr = pf.active_trades[cid]
            ref = tr.market.get("open_oracle_price") or tr.entry_spot or strike
            exit_price = (1.0 if (spot >= ref) else 0.0) if tr.direction == "YES" else (1.0 if (spot < ref) else 0.0)
            pf.force_exit(cid, exit_price, "expiry_resolve_dropped")
    closed = pf.closed_trades
    return {"n_closed": 0, "n_active_left": len(pf.active_trades),
            "n_triggered": n_triggered, "n_signals": n_signals,
            "trades": [], "total_pnl": 0.0}


def run_market_clocked_arr(arr: dict, reg_entry: dict, fn, mod, pf: Portfolio,
                           clock_holder: list) -> dict:
    """Array twin of run_market_clocked (compact pkl.gz input). Mirrors the dict
    path line-for-line; see driver.run_market_maker_arr for the field mapping and
    the ms-truncation note."""
    from engine.portfolio import Trade
    t_arr = arr["t"]; btc_a = arr["btc"]; pu_a = arr["pu"]; pd_a = arr["pd"]
    ua_a = arr["ua"]; ub_a = arr["ub"]; da_a = arr["da"]; db_a = arr["db"]
    n = len(t_arr)
    if n == 0:
        return {"n_closed": 0, "n_active_left": 0, "n_triggered": 0, "total_pnl": 0.0,
                "trades": [], "n_signals": 0}
    t_end_ms = t_arr[-1]
    t0 = datetime.fromtimestamp(t_arr[0] / 1000.0, tz=UTC)
    t_end = datetime.fromtimestamp(t_end_ms / 1000.0, tz=UTC)
    strike = float(btc_a[0] or 0.0)
    market_id = str(arr.get("id"))
    duration = reg_entry.get("tf_hint") or "5m"
    market = {"condition_id": market_id, "token_id_yes": "yes", "token_id_no": "no",
              "end_date_iso": t_end.isoformat(), "start_date_iso": t0.isoformat(),
              "open_oracle_price": strike, "strike": strike, "duration": duration,
              "fee_schedule": None, "asset": reg_entry.get("asset") or "BTC"}
    spot_history: list[float] = []
    resting = None
    n_signals = 0; n_triggered = 0
    for i in range(n):
        btc = btc_a[i]
        if btc <= 0:
            continue
        # advance simulated clock to this snapshot's ET time
        clock_holder[0] = datetime.fromtimestamp(t_arr[i] / 1000.0, tz=UTC).astimezone(ET)
        spot_history.append(btc)
        if len(spot_history) > driver.SPOT_HISTORY_MAX_LEN:
            del spot_history[:-driver.SPOT_HISTORY_MAX_LEN]
        spot_price = btc; rem_sec = max(0.0, (t_end_ms - t_arr[i]) / 1000.0)
        pu = pu_a[i]; pd = pd_a[i]
        up_a, up_b = ua_a[i], ub_a[i]; dn_a, dn_b = da_a[i], db_a[i]
        yp = up_b if up_b > 0 else pu; yes_ask = up_a if up_a > 0 else pu
        np_val = dn_b if dn_b > 0 else pd; no_ask = dn_a if dn_a > 0 else pd
        sim_t = t_arr[i] / 1000.0
        for _tr in pf.check_exits(spot_price, yp, np_val, rem_sec, oracle_spot=spot_price):
            _tr.closed_at = sim_t
        if resting is not None and market_id not in pf.active_trades:
            ask = yes_ask if resting["direction"] == "YES" else no_ask
            if ask > 0 and ask <= resting["price"] and rem_sec > 0:
                ep = resting["price"]
                notional = position_notional(pf.cash, ep, driver.RISK_PCT, driver.MIN_CONTRACTS)
                gross_shares = int(notional / ep) if ep > 0 else 0
                gross_shares = max(gross_shares, driver.MIN_CONTRACTS) if notional > 0 else 0
                if gross_shares > 0 and pf.cash >= ep * gross_shares:
                    gross_notional = ep * gross_shares
                    entry_fee = calculate_taker_fee(gross_shares, ep, None)
                    fs = round(taker_fee_shares(gross_shares, ep, None), 2)
                    net_shares = round(max(0.0, gross_shares - fs), 2)
                    trade = Trade(condition_id=market_id, direction=resting["direction"],
                                  entry_price=ep, shares=float(gross_shares),
                                  entry_notional=gross_notional, entry_fee=entry_fee,
                                  fee_shares=fs, market=market, entry_spot=resting["entry_spot"])
                    trade.opened_at = sim_t
                    pf._cash -= gross_notional
                    pf.active_trades[market_id] = trade
                    pf._entry_count[market_id] = pf._entry_count.get(market_id, 0) + 1
                resting = None
        if market_id in pf.active_trades:
            continue
        state = {"spot_price": spot_price, "spot_history": spot_history, "rem_sec": rem_sec,
                 "yp": yp, "np_val": np_val, "yes_ask": yes_ask, "yes_bid": yp,
                 "no_ask": no_ask, "no_bid": np_val}
        kwargs = driver._build_signal_kwargs(reg_entry, market, state)
        sig = fn(**kwargs); n_signals += 1
        if sig and sig.get("triggered"):
            n_triggered += 1
            d = sig.get("direction"); ep = float(sig.get("entry_price") or 0.0)
            if d in ("YES", "NO") and 0.05 <= ep <= 0.85:
                resting = {"direction": d, "price": ep, "entry_spot": spot_price}
    if spot_history:
        spot = spot_history[-1]
        for cid in list(pf.active_trades.keys()):
            tr = pf.active_trades[cid]
            ref = tr.market.get("open_oracle_price") or tr.entry_spot or strike
            exit_price = (1.0 if (spot >= ref) else 0.0) if tr.direction == "YES" else (1.0 if (spot < ref) else 0.0)
            pf.force_exit(cid, exit_price, "expiry_resolve_dropped")
            tr.closed_at = t_end_ms / 1000.0
    closed = pf.closed_trades
    return {"n_closed": 0, "n_active_left": len(pf.active_trades),
            "n_triggered": n_triggered, "n_signals": n_signals,
            "trades": [], "total_pnl": 0.0}


def run_market_clocked_instant_arr(arr: dict, reg_entry: dict, fn, mod, pf: Portfolio,
                                   clock_holder: list) -> dict:
    """Instant-fill twin of run_market_clocked_arr (optimistic bound)."""
    from engine.portfolio import Trade
    from engine.execution import calculate_taker_fee, taker_fee_shares
    t_arr = arr["t"]; btc_a = arr["btc"]; pu_a = arr["pu"]; pd_a = arr["pd"]
    ua_a = arr["ua"]; ub_a = arr["ub"]; da_a = arr["da"]; db_a = arr["db"]
    n = len(t_arr)
    if n == 0:
        return {"n_closed": 0, "n_active_left": 0, "n_triggered": 0, "total_pnl": 0.0,
                "trades": [], "n_signals": 0}
    t_end_ms = t_arr[-1]
    t0 = datetime.fromtimestamp(t_arr[0] / 1000.0, tz=UTC)
    t_end = datetime.fromtimestamp(t_end_ms / 1000.0, tz=UTC)
    strike = float(btc_a[0] or 0.0)
    market_id = str(arr.get("id"))
    duration = reg_entry.get("tf_hint") or "5m"
    market = {"condition_id": market_id, "token_id_yes": "yes", "token_id_no": "no",
              "start_date_iso": t0.isoformat(),
              "open_oracle_price": strike, "strike": strike, "duration": duration,
              "fee_schedule": None, "asset": reg_entry.get("asset") or "BTC"}
    spot_history: list[float] = []
    n_signals = 0; n_triggered = 0
    for i in range(n):
        btc = btc_a[i]
        if btc <= 0:
            continue
        clock_holder[0] = datetime.fromtimestamp(t_arr[i] / 1000.0, tz=UTC).astimezone(ET)
        spot_history.append(btc)
        if len(spot_history) > driver.SPOT_HISTORY_MAX_LEN:
            del spot_history[:-driver.SPOT_HISTORY_MAX_LEN]
        sim_t = t_arr[i] / 1000.0
        spot_price = btc; rem_sec = max(0.0, (t_end_ms - t_arr[i]) / 1000.0)
        pu = pu_a[i]; pd = pd_a[i]
        up_a, up_b = ua_a[i], ub_a[i]; dn_a, dn_b = da_a[i], db_a[i]
        yp = up_b if up_b > 0 else pu; yes_ask = up_a if up_a > 0 else pu
        np_val = dn_b if dn_b > 0 else pd; no_ask = dn_a if dn_a > 0 else pd
        for _tr in pf.check_exits(spot_price, yp, np_val, rem_sec, oracle_spot=spot_price):
            _tr.closed_at = sim_t
        if market_id in pf.active_trades:
            continue
        state = {"spot_price": spot_price, "spot_history": spot_history, "rem_sec": rem_sec,
                 "yp": yp, "np_val": np_val, "yes_ask": yes_ask, "yes_bid": yp,
                 "no_ask": no_ask, "no_bid": np_val}
        kwargs = driver._build_signal_kwargs(reg_entry, market, state)
        sig = fn(**kwargs); n_signals += 1
        if sig and sig.get("triggered"):
            n_triggered += 1
            d = sig.get("direction"); ep = float(sig.get("entry_price") or 0.0)
            if d in ("YES", "NO") and 0.05 <= ep <= 0.85 and rem_sec > 0:
                notional = position_notional(pf.cash, ep, driver.RISK_PCT, driver.MIN_CONTRACTS)
                gross_shares = int(notional / ep) if ep > 0 else 0
                gross_shares = max(gross_shares, driver.MIN_CONTRACTS) if notional > 0 else 0
                if gross_shares > 0 and pf.cash >= ep * gross_shares:
                    gross_notional = ep * gross_shares
                    entry_fee = calculate_taker_fee(gross_shares, ep, None)
                    fs = round(taker_fee_shares(gross_shares, ep, None), 2)
                    trade = Trade(condition_id=market_id, direction=d,
                                  entry_price=ep, shares=float(gross_shares),
                                  entry_notional=gross_notional, entry_fee=entry_fee,
                                  fee_shares=fs, market=market, entry_spot=spot_price)
                    trade.opened_at = sim_t
                    pf._cash -= gross_notional
                    pf.active_trades[market_id] = trade
                    pf._entry_count[market_id] = pf._entry_count.get(market_id, 0) + 1
    if spot_history:
        spot = spot_history[-1]
        for cid in list(pf.active_trades.keys()):
            tr = pf.active_trades[cid]
            ref = tr.market.get("open_oracle_price") or tr.entry_spot or strike
            exit_price = (1.0 if (spot >= ref) else 0.0) if tr.direction == "YES" else (1.0 if (spot < ref) else 0.0)
            pf.force_exit(cid, exit_price, "expiry_resolve_dropped")
            tr.closed_at = t_end_ms / 1000.0
    closed = pf.closed_trades
    return {"n_closed": 0, "n_active_left": len(pf.active_trades),
            "n_triggered": n_triggered, "n_signals": n_signals,
            "trades": [], "total_pnl": 0.0}


def run_market_clocked_taker_arr(arr: dict, reg_entry: dict, fn, mod, pf: Portfolio,
                                 clock_holder: list) -> dict:
    """Live-taker twin of run_market_clocked_arr: same day-clock replay, but
    entries go through the real Portfolio.check_entry (taker book at top of
    book). Mirrors drivers' run_market_taker_arr; see it for the gate list."""
    t_arr = arr["t"]; btc_a = arr["btc"]; pu_a = arr["pu"]; pd_a = arr["pd"]
    ua_a = arr["ua"]; ub_a = arr["ub"]; da_a = arr["da"]; db_a = arr["db"]
    n = len(t_arr)
    if n == 0:
        return {"n_closed": 0, "n_active_left": 0, "n_triggered": 0, "total_pnl": 0.0,
                "trades": [], "n_signals": 0}
    t_end_ms = t_arr[-1]
    t0 = datetime.fromtimestamp(t_arr[0] / 1000.0, tz=UTC)
    t_end = datetime.fromtimestamp(t_end_ms / 1000.0, tz=UTC)
    strike = float(btc_a[0] or 0.0)
    market_id = str(arr.get("id"))
    duration = reg_entry.get("tf_hint") or "5m"
    market = {"condition_id": market_id, "token_id_yes": "yes", "token_id_no": "no",
              "start_date_iso": t0.isoformat(),
              "open_oracle_price": strike, "strike": strike, "duration": duration,
              "fee_schedule": None, "asset": reg_entry.get("asset") or "BTC"}
    spot_history: list[float] = []
    n_signals = 0; n_triggered = 0
    scale_in_on = bool(reg_entry.get("scale_in", False))
    for i in range(n):
        btc = btc_a[i]
        if btc <= 0:
            continue
        clock_holder[0] = datetime.fromtimestamp(t_arr[i] / 1000.0, tz=UTC).astimezone(ET)
        spot_history.append(btc)
        if len(spot_history) > driver.SPOT_HISTORY_MAX_LEN:
            del spot_history[:-driver.SPOT_HISTORY_MAX_LEN]
        sim_t = t_arr[i] / 1000.0
        spot_price = btc; rem_sec = max(0.0, (t_end_ms - t_arr[i]) / 1000.0)
        pu = pu_a[i]; pd = pd_a[i]
        up_a, up_b = ua_a[i], ub_a[i]; dn_a, dn_b = da_a[i], db_a[i]
        yp = up_b if up_b > 0 else pu; yes_ask = up_a if up_a > 0 else pu
        np_val = dn_b if dn_b > 0 else pd; no_ask = dn_a if dn_a > 0 else pd
        for _tr in pf.check_exits(spot_price, yp, np_val, rem_sec, oracle_spot=spot_price):
            _tr.closed_at = sim_t
        if market_id in pf.active_trades and not scale_in_on:
            continue
        state = {"spot_price": spot_price, "spot_history": spot_history, "rem_sec": rem_sec,
                 "yp": yp, "np_val": np_val, "yes_ask": yes_ask, "yes_bid": yp,
                 "no_ask": no_ask, "no_bid": np_val}
        kwargs = driver._build_signal_kwargs(reg_entry, market, state)
        sig = fn(**kwargs); n_signals += 1
        if sig and sig.get("triggered"):
            n_triggered += 1
            d = sig.get("direction")
            if d in ("YES", "NO") and rem_sec > 0:
                driver.taker_enter(pf, sig, market, d, yes_ask, yp, no_ask, np_val,
                                   spot_price, sim_t, reg_entry)
    if spot_history:
        spot = spot_history[-1]
        for cid in list(pf.active_trades.keys()):
            tr = pf.active_trades[cid]
            ref = tr.market.get("open_oracle_price") or tr.entry_spot or strike
            exit_price = (1.0 if (spot >= ref) else 0.0) if tr.direction == "YES" else (1.0 if (spot < ref) else 0.0)
            pf.force_exit(cid, exit_price, "expiry_resolve_dropped")
            tr.closed_at = t_end_ms / 1000.0
    closed = pf.closed_trades
    return {"n_closed": 0, "n_active_left": len(pf.active_trades),
            "n_triggered": n_triggered, "n_signals": n_signals,
            "trades": [], "total_pnl": 0.0}


def build_index_compact(files: list[str], oos_start: "date | None" = None):
    """(date_ET, first_ts_epoch, path) index from compact pkl.gz files (fast:
    pickle load, first element only)."""
    index = []
    for f in files:
        arr = driver.load_compact_file(f)
        if not arr or not arr.get("t"):
            continue
        t_first = datetime.fromtimestamp(arr["t"][0] / 1000.0, tz=UTC)
        d = t_first.astimezone(ET).date()
        if oos_start is not None and d >= oos_start:
            continue
        index.append((d, arr["t"][0] / 1000.0, f))
    index.sort(key=lambda x: (x[0].isoformat(), x[1]))
    return index


def run_daily_orb(reg_entry: dict, files: list[str], oos_start: "date | None" = None,
                  index: "list[tuple[date, float, str]] | None" = None,
                  compact: bool = False, fill: str = "maker") -> dict:
    """Replay one daily_orb_v5 strategy over many market files, day-grouped,
    with one persistent $200 wallet. Streams one market at a time (memory-safe);
    only a single market's snapshots are resident at once. Returns aggregate
    trades + equity.

    oos_start: if set, markets whose first-snapshot ET date >= oos_start are
    skipped (held-out OOS window, never traded/scored in-sample)."""
    bt_reference.load()
    mod = _load_orb_module(reg_entry)
    fn = getattr(mod, reg_entry["fn"])
    clock_holder = [datetime.now(ET)]
    mod._now_et = lambda: clock_holder[0]
    pf = Portfolio(name=f"btorb:{reg_entry.get('tf_hint')}:{reg_entry.get('max_reentries')}",
                   capital=driver.CAPITAL)
    # pass 1: (date, first_time, file) index. A precomputed index may be passed in
    # (shared across all daily_orb strats so the whole dataset is not re-scanned
    # 39 times); it must already be OOS-filtered and time-sorted. Otherwise build
    # it here, one file resident at a time.
    if index is None:
        if compact:
            index = build_index_compact(files, oos_start)
        else:
            index = []
            for f in files:
                snaps = driver.load_market_file(f)
                if not snaps:
                    continue
                t_first = driver._parse_ts(snaps[0]["time"])
                d = t_first.astimezone(ET).date()
                if oos_start is not None and d >= oos_start:
                    del snaps
                    continue
                index.append((d, t_first.timestamp(), f))
                del snaps
            index.sort(key=lambda x: (x[0].isoformat(), x[1]))
    n_triggered = 0; n_signals = 0; cur_day = None; day_mkts = 0
    for d, _ft, f in index:
        if d != cur_day:
            if cur_day is not None:
                print(f"  day {cur_day} mkts={day_mkts} trig={n_triggered} "
                      f"closed={len(pf.closed_trades)} cash={pf.cash:.2f}", flush=True)
            cur_day = d; day_mkts = 0
            seed_orb(mod, reg_entry, d)
        day_mkts += 1
        if compact:
            data = driver.load_compact_file(f)
            if fill == "taker":
                r = run_market_clocked_taker_arr(data, reg_entry, fn, mod, pf, clock_holder)
            elif fill == "instant":
                r = run_market_clocked_instant_arr(data, reg_entry, fn, mod, pf, clock_holder)
            else:
                r = run_market_clocked_arr(data, reg_entry, fn, mod, pf, clock_holder)
        else:
            data = driver.load_market_file(f)
            r = run_market_clocked(data, reg_entry, fn, mod, pf, clock_holder)
        n_triggered += r["n_triggered"]; n_signals += r["n_signals"]
        del data
    if cur_day is not None:
        print(f"  day {cur_day} mkts={day_mkts} trig={n_triggered} "
              f"closed={len(pf.closed_trades)} cash={pf.cash:.2f}", flush=True)
    closed = pf.closed_trades
    total_pnl = sum(t.pnl for t in closed)
    return {"trades": [t.to_dict() for t in closed], "n_closed": len(closed),
            "n_active_left": len(pf.active_trades), "n_triggered": n_triggered,
            "n_signals": n_signals, "cash": round(pf.cash, 4),
            "total_pnl": round(total_pnl, 4),
            "equity": round(driver.CAPITAL + total_pnl, 4), "days": len({d for d, _, _ in index})}


if __name__ == "__main__":
    # Smoke test: run one daily_orb_v5 strat over a staged set.
    import sys, glob
    sys.path.insert(0, "/config/backtest"); sys.path.insert(0, "/config/backtest/src")
    files = sorted(glob.glob(sys.argv[1] if len(sys.argv) > 1 else "/tmp/orb_test/*.json.gz"))
    print("files:", len(files))
    # pick the first daily_orb_v5 strategy from the registry
    orb_regs = [(n, r) for n, r in driver.STRATEGIES.items()
                if r.get("module") == "phase_2.daily_orb_v5"]
    print("daily_orb_v5 strats in registry:", len(orb_regs))
    name, reg = orb_regs[0]
    print("running:", name, {k: reg.get(k) for k in ("tf_hint", "max_reentries", "asset")})
    out = run_daily_orb(reg, files)
    print({k: v for k, v in out.items() if k != "trades"})
