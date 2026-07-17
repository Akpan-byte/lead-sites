#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-14  kimi
#   - Added optional BT_EXTRA_STRATEGIES env var: a JSON file merged into the
#     imported STRATEGIES dict at import time. Lets per-coin runs add ETH/SOL
#     orb entries without touching the shared strategy_registry.py.
# 2026-07-11  kimi
#   - Backtest driver (sandbox). Reuses the LIVE engine + signal code from the
#     snapshot in src/ and mirrors runners/run_breakout_pct_003.process_markets
#     per-snapshot body. Feed/book/reference are swapped for historical sources;
#     Portfolio/execution/signal logic is byte-identical to live.
# WHY: Run the real strategy code over recorded polybacktest markets + Binance
#      reference with no look-ahead, without editing the live project.
"""Feed-swap backtest driver. No live code is edited; this imports the snapshot
under ./src and replays recorded markets through the real Portfolio/execution/
signal logic.

Per-strategy model: each strategy gets its own $200 wallet (portfolio cash is the
gate; max_notional=None == no shared pool). Stacks (shared wallet) come later.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import sys
from datetime import datetime, timezone
from importlib import import_module
import importlib.util


def _install_requests_stub() -> None:
    """Offline backtest: some phase_2 signals do `import requests` at module top
    but only call it inside live-fetch helpers we never invoke (we feed data).
    If real `requests` is absent (VM is stdlib-only), inject a stub that imports
    cleanly yet raises loudly if any network path is actually hit."""
    if "requests" in sys.modules:
        return
    try:
        import requests  # noqa: F401
        return
    except Exception:
        pass
    import types

    def _blocked(*_a, **_k):
        raise RuntimeError("network disabled in offline backtest (requests stub)")

    stub = types.ModuleType("requests")
    stub.get = _blocked
    stub.post = _blocked
    stub.put = _blocked
    stub.delete = _blocked
    stub.request = _blocked
    stub.Session = type("Session", (), {"get": _blocked, "post": _blocked})
    sys.modules["requests"] = stub

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from engine.portfolio import Portfolio, EXIT_SNIPE_PRICE  # noqa: E402
from engine.execution import (  # noqa: E402
    position_notional, RISK_PCT, MIN_CONTRACTS, QUEUE_FILL_FRACTION,
)
from engine.strategy_registry import STRATEGIES  # noqa: E402

# Optional per-coin / per-task registry extension. Set BT_EXTRA_STRATEGIES to a
# JSON file mapping strategy names to registry dicts; it is merged at import time
# so both the launcher and forkserver worker processes see the same names.
_BT_EXTRA_STRATEGIES_PATH = os.environ.get("BT_EXTRA_STRATEGIES")
if _BT_EXTRA_STRATEGIES_PATH:
    try:
        with open(_BT_EXTRA_STRATEGIES_PATH, "r", encoding="utf-8") as _fh:
            _BT_EXTRA_STRATEGIES = json.load(_fh)
        STRATEGIES.update(_BT_EXTRA_STRATEGIES)
    except Exception as _e:
        print(f"WARN: BT_EXTRA_STRATEGIES={_BT_EXTRA_STRATEGIES_PATH} failed: {_e}", flush=True)

# Reuse the runner's pure feature helpers + kwargs builder (no Redis at import).
# --- Inlined from runners/run_breakout_pct_003.py (pure helpers + kwargs builder).
# Copied verbatim to avoid importing the runner (which pulls discovery->requests).
# Fidelity-critical: keep byte-identical to the live runner.  DO NOT edit live.
import statistics

CAPITAL = 200.0
SPOT_HISTORY_MAX_LEN = 1000
DEFAULT_MAX_ENTRY_PRICE = 0.85
BT_ASSET = os.environ.get("BT_ASSET", "BTC")


def _fast_stdev(vals):
    """Sample stdev via math.fsum. statistics.stdev uses exact Fraction
    arithmetic (~50x slower); fsum is correctly-rounded so the float result is
    identical to statistics' correctly-rounded result except in pathological
    cancellation (not present at BTC price scales). Validated byte-identical on
    the 458-market baseline."""
    n = len(vals)
    if n < 2:
        return 0.0
    m = math.fsum(vals) / n
    return math.sqrt(math.fsum((v - m) ** 2 for v in vals) / (n - 1))


def _compute_z_score(prices, window=20):
    if len(prices) < 2:
        return 0.0
    recent = prices[-window:] if len(prices) >= window else prices
    if len(recent) < 2:
        return 0.0
    mean = math.fsum(recent) / len(recent)
    std = _fast_stdev(recent)
    if std == 0.0:
        return 0.0
    return (prices[-1] - mean) / std


def _compute_velocity(prices):
    if len(prices) < 2:
        return 0.0, 0.0, []
    v_t = prices[-1] - prices[-2]
    history = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    std_v = _fast_stdev(history) if len(history) > 1 else 0.0
    return v_t, std_v, history


def _compute_acceleration(velocity_history):
    if len(velocity_history) < 2:
        return 0.0
    return velocity_history[-1] - velocity_history[-2]


def _compute_tick_change(prices):
    if len(prices) < 2:
        return 0.0
    return prices[-1] - prices[-2]


def _compute_spread(yes_ask, yes_bid, no_ask, no_bid):
    yes_mid = (yes_ask + yes_bid) / 2.0 if yes_ask is not None and yes_bid is not None else 0.0
    no_mid = (no_ask + no_bid) / 2.0 if no_ask is not None and no_bid is not None else 0.0
    spread = (yes_ask - yes_bid) if yes_ask is not None and yes_bid is not None else 0.0
    return yes_mid, no_mid, spread, spread


def _build_signal_kwargs(registry_entry, market, state, elapsed_sec=None,
                         duration_sec=None, orderbook_up=None, orderbook_down=None,
                         yp_history=None, np_history=None):
    params = list(registry_entry.get("params", []))
    spot_history = state["spot_history"]
    spot_price = state["spot_price"]
    strike = market.get("open_oracle_price") or market.get("strike") or spot_price
    rem_sec = state["rem_sec"]
    yp = state["yp"]
    np_val = state["np_val"]
    yes_ask = state["yes_ask"]
    yes_bid = state["yes_bid"]
    no_ask = state["no_ask"]
    no_bid = state["no_bid"]
    # Ensure elapsed_sec / duration_sec are always available for signals.
    if duration_sec is None:
        tf = registry_entry.get("tf_hint") or market.get("duration") or "5m"
        duration_sec = 300.0 if tf == "5m" else 900.0 if tf == "15m" else 1800.0 if tf == "30m" else 3600.0 if tf == "1h" else 300.0
    if elapsed_sec is None:
        elapsed_sec = max(0.0, duration_sec - rem_sec)
    # Lazy feature computation: a signal only reads what its declared `params`
    # ask for, so skip unused features (velocity/acceleration over the 1000-tick
    # history were ~90% of per-snapshot cost for strats that never read them).
    # Values, when computed, are byte-identical to the live runner's formulas.
    need = set(params)
    z_score = _compute_z_score(spot_history) if need & {"z_score", "z_dist"} else 0.0
    if need & {"v_t", "std_v", "velocity_history", "a_t"}:
        v_t, std_v, velocity_history = _compute_velocity(spot_history)
        a_t = _compute_acceleration(velocity_history) if "a_t" in need else 0.0
    else:
        v_t, std_v, a_t, velocity_history = 0.0, 0.0, 0.0, []
    tick_change = _compute_tick_change(spot_history) if "tick_change" in need else 0.0
    if need & {"spread", "spread_val"}:
        _, _, spread, spread_val = _compute_spread(yes_ask, yes_bid, no_ask, no_bid)
    else:
        spread, spread_val = 0.0, 0.0
    z_dist = abs(z_score)
    tf_hint = registry_entry.get("tf_hint") or market.get("duration")
    if tf_hint is None:
        tf_hint = "15m" if rem_sec > 500 else "5m"
    or_window_seconds = registry_entry.get("or_window_seconds")
    max_reentries = registry_entry.get("_max_reentries") or registry_entry.get("max_reentries")

    # Book imbalance for VWAP orderflow/bookmap families
    imb = 0.0
    if orderbook_up and orderbook_down:
        yes_bid_size = float(orderbook_up["bids"][0][1]) if orderbook_up.get("bids") else 0.0
        no_ask_size = float(orderbook_down["asks"][0][1]) if orderbook_down.get("asks") else 0.0
        total = yes_bid_size + no_ask_size
        if total > 0:
            imb = (yes_bid_size - no_ask_size) / total

    param_map = {
        "spot_price": spot_price, "strike": strike, "z_score": z_score,
        "rem_sec": rem_sec, "yp": yp, "np_val": np_val, "v_t": v_t, "std_v": std_v,
        "a_t": a_t, "spread": spread, "spread_val": spread_val, "tick_change": tick_change,
        "velocity_history": velocity_history, "z_dist": z_dist, "yes_ask": yes_ask,
        "no_ask": no_ask, "tf_hint": tf_hint, "market_id": market.get("condition_id"),
        "max_entry_price": DEFAULT_MAX_ENTRY_PRICE, "or_window_seconds": or_window_seconds,
        "max_reentries": max_reentries, "asset": market.get("asset") or "BTC",
        "start_date_iso": market.get("start_date_iso"),
        "resolution_source": market.get("resolution_source"),
        # Trend-family / VWAP factory extras (opt-in via params)
        "spot_history": spot_history,
        "elapsed_sec": elapsed_sec,
        "duration_sec": duration_sec,
        "orderbook_up": orderbook_up,
        "orderbook_down": orderbook_down,
        "yp_history": yp_history or [],
        "np_history": np_history or [],
        "book_imbalance_val": imb,
        "config": registry_entry,
    }
    return {p: param_map.get(p) for p in params}


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def load_market_file(path: str) -> list[dict]:
    """Load one polybacktest {market_id}.json.gz (list of snapshots)."""
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def norm_book(ob: dict | None) -> dict:
    """Snapshot orderbook_{up,down} -> walk_book_buy format (real depth)."""
    if not ob:
        return {"asks": [], "bids": [], "raw": {}}
    asks = [[float(l["price"]), float(l["size"])] for l in ob.get("asks", []) if l.get("price", 0) > 0 and l.get("size", 0) > 0]
    bids = [[float(l["price"]), float(l["size"])] for l in ob.get("bids", []) if l.get("price", 0) > 0 and l.get("size", 0) > 0]
    asks.sort(key=lambda x: x[0])          # asks ascending (best first)
    bids.sort(key=lambda x: -x[0])         # bids descending (best first)
    return {"asks": asks, "bids": bids, "raw": {}}


def best(ob_norm: dict, side: str, fallback: float) -> float:
    arr = ob_norm.get(side, [])
    return float(arr[0][0]) if arr else float(fallback)


def top_book(ob: dict | None) -> tuple[float, float]:
    """Top-of-book (best_ask, best_bid) from a raw snapshot orderbook = the first
    level with price>0 and size>0 on each side. The polybacktest feed stores asks
    ascending / bids descending (verified: 0 unsorted of 38,788 books across 8
    random markets), so the first valid level IS the best. This equals
    (best(norm_book(ob),'asks'), best(norm_book(ob),'bids')) for sorted books and
    returns (0.0, 0.0) when no valid level exists, so callers keep their pu/pd
    fallback exactly like best(). O(1) per snapshot instead of build+sort of all
    15 levels/side — pure speed win for the maker path (which only reads top)."""
    if not ob:
        return 0.0, 0.0
    ba = 0.0; bb = 0.0
    for l in ob.get("asks") or ():
        p = l.get("price", 0)
        if p > 0 and l.get("size", 0) > 0:
            ba = p
            break
    for l in ob.get("bids") or ():
        p = l.get("price", 0)
        if p > 0 and l.get("size", 0) > 0:
            bb = p
            break
    return ba, bb


def load_signal_fn(reg_entry: dict):
    """Load a strategy's signal function. Tries the normal package import first;
    if the sandbox `signals.phase_2` eager __init__ poisons the import (it pulls
    rsi2_connors -> requests on a stdlib-only VM), fall back to loading the
    submodule's signal.py directly by file path, bypassing the package __init__.
    Live code is never modified; this only changes how the sandbox imports it."""
    modname = reg_entry["module"]
    fn = reg_entry["fn"]
    try:
        return getattr(import_module(f"signals.{modname}"), fn)
    except Exception:
        pass
    _install_requests_stub()
    parts = modname.split(".")
    base = os.path.join(SRC, "signals", *parts)
    if os.path.isdir(base):
        cand = os.path.join(base, "signal.py")
    elif os.path.isfile(base + ".py"):
        cand = base + ".py"
    else:
        cand = None
    if not cand or not os.path.isfile(cand):
        raise ImportError(f"cannot resolve signal module {modname} -> {base}")
    spec = importlib.util.spec_from_file_location(
        f"_bt_sig_{modname.replace('.', '_')}", cand
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, fn)


def run_market(snapshots: list[dict], reg_entry: dict, signal_fn) -> dict:
    """Replay one market's snapshots for one strategy. Returns trades + equity."""
    if not snapshots:
        return {"trades": [], "equity": CAPITAL, "cash": CAPITAL, "pnl": 0.0, "n_signals": 0}

    snapshots = sorted(snapshots, key=lambda s: s["time"])
    t0 = _parse_ts(snapshots[0]["time"])
    t_end = _parse_ts(snapshots[-1]["time"])
    strike = float(snapshots[0].get("btc_price") or 0.0)          # window-open reference
    market_id = str(snapshots[0].get("market_id"))
    duration = reg_entry.get("tf_hint") or "5m"

    market = {
        "condition_id": market_id,
        "token_id_yes": "yes",
        "token_id_no": "no",
        "end_date_iso": t_end.isoformat(),
        "start_date_iso": t0.isoformat(),
        "open_oracle_price": strike,
        "strike": strike,
        "duration": duration,
        "fee_schedule": None,           # -> execution default crypto rate 0.07
        "asset": BT_ASSET,
    }

    pf = Portfolio(name=f"bt:{reg_entry and reg_entry.get('module')}", capital=CAPITAL)
    spot_history: list[float] = []
    n_signals = 0
    n_triggered = 0

    for snap in snapshots:
        t = _parse_ts(snap["time"])
        btc = float(snap.get("btc_price") or 0.0)
        if btc <= 0:
            continue
        spot_history.append(btc)
        if len(spot_history) > SPOT_HISTORY_MAX_LEN:
            spot_history = spot_history[-SPOT_HISTORY_MAX_LEN:]
        spot_price = btc
        rem_sec = max(0.0, (t_end - t).total_seconds())

        up = norm_book(snap.get("orderbook_up"))
        dn = norm_book(snap.get("orderbook_down"))
        pu = float(snap.get("price_up") or 0.0)
        pd = float(snap.get("price_down") or 0.0)
        yp = best(up, "bids", pu)                 # YES bid
        yes_ask = best(up, "asks", pu)            # YES ask
        np_val = best(dn, "bids", pd)             # NO bid
        no_ask = best(dn, "asks", pd)             # NO ask

        state = {
            "spot_price": spot_price, "spot_history": spot_history,
            "rem_sec": rem_sec, "yp": yp, "np_val": np_val,
            "yes_ask": yes_ask, "yes_bid": yp, "no_ask": no_ask, "no_bid": np_val,
        }

        # 1) exits first (snipe 0.97 / expiry), mirroring process_markets order.
        for closed in pf.check_exits(spot_price, yp, np_val, rem_sec, oracle_spot=spot_price):
            pass  # portfolio already updated cash/closed_trades

        # 2) signal + entry (single-strat: portfolio cash is the gate).
        kwargs = _build_signal_kwargs(reg_entry, market, state)
        sig = signal_fn(**kwargs)
        n_signals += 1
        if not (sig and sig.get("triggered")):
            continue
        n_triggered += 1
        direction = sig.get("direction")
        if direction not in ("YES", "NO"):
            continue
        book = up if direction == "YES" else dn
        if not book.get("asks"):
            continue
        fair_price = None
        if direction == "YES" and yes_ask > 0 and yp > 0:
            fair_price = (yes_ask + yp) / 2.0
        elif direction == "NO" and no_ask > 0 and np_val > 0:
            fair_price = (no_ask + np_val) / 2.0
        pf.check_entry(
            sig, market, book,
            entry_spot=spot_price, latency_book=book, fair_price=fair_price,
            queue_fraction=QUEUE_FILL_FRACTION, max_notional=None,
            allow_reentry=bool(reg_entry.get("allow_reentry", False)),
            max_reentries=reg_entry.get("_max_reentries") or reg_entry.get("max_reentries", 0),
        )

    # 3) settle any still-open trade at market expiry (resolve_dropped_markets).
    if spot_history:
        spot = spot_history[-1]
        for cid in list(pf.active_trades.keys()):
            tr = pf.active_trades[cid]
            ref = tr.market.get("open_oracle_price") or tr.entry_spot or strike
            if tr.direction == "YES":
                exit_price = 1.0 if spot >= ref else 0.0
            else:
                exit_price = 1.0 if spot < ref else 0.0
            pf.force_exit(cid, exit_price, "expiry_resolve_dropped")

    closed = pf.closed_trades
    total_pnl = sum(t.pnl for t in closed)
    active = list(pf.active_trades.values())
    return {
        "market_id": market_id,
        "trades": [t.to_dict() for t in closed],
        "n_closed": len(closed),
        "n_active_left": len(active),
        "n_signals": n_signals,
        "n_triggered": n_triggered,
        "cash": round(pf.cash, 4),
        "total_pnl": round(total_pnl, 4),
        "equity": round(CAPITAL + total_pnl, 4),   # single-strat: wallet started at CAPITAL
    }


def run_market_maker(snapshots: list[dict], reg_entry: dict, signal_fn,
                     pf: "Portfolio | None" = None) -> dict:
    """Maker-fill variant: signals return entry_price=bid, interpreted as a resting
    limit bid. It fills at entry_price when a later snapshot's best ask <= bid
    (price came to the order). This models the strategy's stated entry price and
    is reproducible from ~8Hz data (unlike fleeting taker crosses). One resting
    order per market; refreshed on each new trigger; discarded at expiry.

    pf: optional persistent Portfolio (shared wallet across markets within one
    strategy, for the $200-starting-capital compounding model). If None, a fresh
    $200 wallet is used for this market only."""
    from engine.portfolio import Trade
    from engine.execution import calculate_taker_fee, taker_fee_shares
    if not snapshots:
        return {"trades": [], "equity": CAPITAL, "cash": CAPITAL, "pnl": 0.0, "n_signals": 0}
    snapshots = sorted(snapshots, key=lambda s: s["time"])
    t0 = _parse_ts(snapshots[0]["time"]); t_end = _parse_ts(snapshots[-1]["time"])
    strike = float(snapshots[0].get("btc_price") or 0.0)
    market_id = str(snapshots[0].get("market_id"))
    duration = reg_entry.get("tf_hint") or "5m"
    market = {"condition_id": market_id, "token_id_yes": "yes", "token_id_no": "no",
              "end_date_iso": t_end.isoformat(), "start_date_iso": t0.isoformat(),
              "open_oracle_price": strike, "strike": strike, "duration": duration,
              "fee_schedule": None, "asset": BT_ASSET}
    pf = pf or Portfolio(name=f"btm:{reg_entry.get('module')}", capital=CAPITAL)
    spot_history: list[float] = []
    resting = None  # {"direction","price","entry_spot"}
    n_signals = 0; n_triggered = 0
    for snap in snapshots:
        t = _parse_ts(snap["time"]); btc = float(snap.get("btc_price") or 0.0)
        if btc <= 0:
            continue
        spot_history.append(btc); spot_history = spot_history[-SPOT_HISTORY_MAX_LEN:]
        spot_price = btc; rem_sec = max(0.0, (t_end - t).total_seconds())
        up_a, up_b = top_book(snap.get("orderbook_up"))
        dn_a, dn_b = top_book(snap.get("orderbook_down"))
        pu = float(snap.get("price_up") or 0.0); pd = float(snap.get("price_down") or 0.0)
        yp = up_b if up_b > 0 else pu; yes_ask = up_a if up_a > 0 else pu
        np_val = dn_b if dn_b > 0 else pd; no_ask = dn_a if dn_a > 0 else pd
        # 1) exits on any open trade
        for _ in pf.check_exits(spot_price, yp, np_val, rem_sec, oracle_spot=spot_price):
            pass
        # 2) fill resting maker order if price came to our bid
        if resting is not None and market_id not in pf.active_trades:
            ask = yes_ask if resting["direction"] == "YES" else no_ask
            if ask > 0 and ask <= resting["price"] and rem_sec > 0:
                ep = resting["price"]
                notional = position_notional(pf.cash, ep, RISK_PCT, MIN_CONTRACTS)
                gross_shares = int(notional / ep) if ep > 0 else 0
                gross_shares = max(gross_shares, MIN_CONTRACTS) if notional > 0 else 0
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
        # 3) new signal -> (re)post resting order (only if no open position)
        if market_id in pf.active_trades:
            continue
        state = {"spot_price": spot_price, "spot_history": spot_history, "rem_sec": rem_sec,
                 "yp": yp, "np_val": np_val, "yes_ask": yes_ask, "yes_bid": yp,
                 "no_ask": no_ask, "no_bid": np_val}
        kwargs = _build_signal_kwargs(reg_entry, market, state)
        sig = signal_fn(**kwargs); n_signals += 1
        if sig and sig.get("triggered"):
            n_triggered += 1
            d = sig.get("direction"); ep = float(sig.get("entry_price") or 0.0)
            if d in ("YES", "NO") and 0.05 <= ep <= 0.85:
                resting = {"direction": d, "price": ep, "entry_spot": spot_price}
    # expiry settlement of any open trade
    if spot_history:
        spot = spot_history[-1]
        for cid in list(pf.active_trades.keys()):
            tr = pf.active_trades[cid]
            ref = tr.market.get("open_oracle_price") or tr.entry_spot or strike
            exit_price = (1.0 if (spot >= ref) else 0.0) if tr.direction == "YES" else (1.0 if (spot < ref) else 0.0)
            pf.force_exit(cid, exit_price, "expiry_resolve_dropped")
    closed = pf.closed_trades; total_pnl = sum(t.pnl for t in closed)
    return {"market_id": market_id, "trades": [t.to_dict() for t in closed],
            "n_closed": len(closed), "n_active_left": len(pf.active_trades),
            "n_signals": n_signals, "n_triggered": n_triggered,
            "cash": round(pf.cash, 4), "total_pnl": round(total_pnl, 4),
            "equity": round(CAPITAL + total_pnl, 4)}


def load_compact_file(path: str) -> dict:
    """Load one compact /tmp/btc5m_compact/<market_id>.pkl.gz dict-of-arrays."""
    import pickle
    with gzip.open(path, "rb") as fh:
        return pickle.load(fh)


def run_market_maker_arr(arr: dict, reg_entry: dict, signal_fn,
                         pf: "Portfolio | None" = None) -> dict:
    """Array twin of run_market_maker: consumes compact dict-of-arrays instead of
    per-snapshot dicts. Semantics mirror the dict path line-for-line:
      snap['time']        -> datetime.fromtimestamp(t[i]/1000, utc)  (ms-truncated)
      snap['btc_price']   -> btc[i]          top_book(orderbook_up)   -> (ua[i], ub[i])
      snap['price_up']    -> pu[i]           top_book(orderbook_down) -> (da[i], db[i])
      snap['price_down']  -> pd[i]
    rem_sec is (t_end_ms - t[i])/1000 == (t_end_dt - t_dt).total_seconds() for the
    same ms-truncated endpoints, so float values are identical. Arrays are kept in
    file order; the dict path's sort-by-time is a no-op when files are time-ordered
    (verified in validation: monotonic t)."""
    from engine.portfolio import Trade
    from engine.execution import calculate_taker_fee, taker_fee_shares
    t_arr = arr["t"]; btc_a = arr["btc"]; pu_a = arr["pu"]; pd_a = arr["pd"]
    ua_a = arr["ua"]; ub_a = arr["ub"]; da_a = arr["da"]; db_a = arr["db"]
    n = len(t_arr)
    if n == 0:
        return {"trades": [], "equity": CAPITAL, "cash": CAPITAL, "pnl": 0.0, "n_signals": 0}
    t_end_ms = t_arr[-1]
    t0 = datetime.fromtimestamp(t_arr[0] / 1000.0, tz=timezone.utc)
    t_end = datetime.fromtimestamp(t_end_ms / 1000.0, tz=timezone.utc)
    strike = float(btc_a[0] or 0.0)
    market_id = str(arr.get("id"))
    duration = reg_entry.get("tf_hint") or "5m"
    market = {"condition_id": market_id, "token_id_yes": "yes", "token_id_no": "no",
              "end_date_iso": t_end.isoformat(), "start_date_iso": t0.isoformat(),
              "open_oracle_price": strike, "strike": strike, "duration": duration,
              "fee_schedule": None, "asset": BT_ASSET}
    pf = pf or Portfolio(name=f"btm:{reg_entry.get('module')}", capital=CAPITAL)
    spot_history: list[float] = []
    resting = None
    n_signals = 0; n_triggered = 0
    for i in range(n):
        btc = btc_a[i]
        if btc <= 0:
            continue
        spot_history.append(btc)
        if len(spot_history) > SPOT_HISTORY_MAX_LEN:
            del spot_history[:-SPOT_HISTORY_MAX_LEN]
        sim_t = t_arr[i] / 1000.0
        spot_price = btc; rem_sec = max(0.0, (t_end_ms - t_arr[i]) / 1000.0)
        pu = pu_a[i]; pd = pd_a[i]
        up_a, up_b = ua_a[i], ub_a[i]; dn_a, dn_b = da_a[i], db_a[i]
        yp = up_b if up_b > 0 else pu; yes_ask = up_a if up_a > 0 else pu
        np_val = dn_b if dn_b > 0 else pd; no_ask = dn_a if dn_a > 0 else pd
        # 1) exits on any open trade (stamp sim time: engine uses wall clock, which
        #    would corrupt hold-time/frequency metrics; PnL logic is untouched)
        for _tr in pf.check_exits(spot_price, yp, np_val, rem_sec, oracle_spot=spot_price):
            _tr.closed_at = sim_t
        # 2) fill resting maker order if price came to our bid
        if resting is not None and market_id not in pf.active_trades:
            ask = yes_ask if resting["direction"] == "YES" else no_ask
            if ask > 0 and ask <= resting["price"] and rem_sec > 0:
                ep = resting["price"]
                notional = position_notional(pf.cash, ep, RISK_PCT, MIN_CONTRACTS)
                gross_shares = int(notional / ep) if ep > 0 else 0
                gross_shares = max(gross_shares, MIN_CONTRACTS) if notional > 0 else 0
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
        # 3) new signal -> (re)post resting order (only if no open position)
        if market_id in pf.active_trades:
            continue
        state = {"spot_price": spot_price, "spot_history": spot_history, "rem_sec": rem_sec,
                 "yp": yp, "np_val": np_val, "yes_ask": yes_ask, "yes_bid": yp,
                 "no_ask": no_ask, "no_bid": np_val}
        kwargs = _build_signal_kwargs(reg_entry, market, state)
        sig = signal_fn(**kwargs); n_signals += 1
        if sig and sig.get("triggered"):
            n_triggered += 1
            d = sig.get("direction"); ep = float(sig.get("entry_price") or 0.0)
            if d in ("YES", "NO") and 0.05 <= ep <= 0.85:
                resting = {"direction": d, "price": ep, "entry_spot": spot_price}
    # expiry settlement of any open trade
    if spot_history:
        spot = spot_history[-1]
        for cid in list(pf.active_trades.keys()):
            tr = pf.active_trades[cid]
            ref = tr.market.get("open_oracle_price") or tr.entry_spot or strike
            exit_price = (1.0 if (spot >= ref) else 0.0) if tr.direction == "YES" else (1.0 if (spot < ref) else 0.0)
            pf.force_exit(cid, exit_price, "expiry_resolve_dropped")
            tr.closed_at = t_end_ms / 1000.0
    closed = pf.closed_trades; total_pnl = sum(t.pnl for t in closed)
    return {"market_id": market_id, "trades": [t.to_dict() for t in closed],
            "n_closed": len(closed), "n_active_left": len(pf.active_trades),
            "n_signals": n_signals, "n_triggered": n_triggered,
            "cash": round(pf.cash, 4), "total_pnl": round(total_pnl, 4),
            "equity": round(CAPITAL + total_pnl, 4)}


def run_market_instant_arr(arr: dict, reg_entry: dict, signal_fn,
                           pf: "Portfolio | None" = None) -> dict:
    """Instant-fill twin: on a trigger the position is opened immediately at the
    signal's entry_price (the signal-side bid for most strats, the ask for ORB
    strats), subject only to the 0.05-0.85 band and the 0.5%/min-5 sizing with
    taker fee in shares. This is the OPTIMISTIC bound: it assumes the bid is
    always available at signal time (what a naive paper fill assumes). Reality
    for a live bot sits between this and the pessimistic maker bound (resting bid
    filled only on adverse moves)."""
    from engine.portfolio import Trade
    from engine.execution import calculate_taker_fee, taker_fee_shares
    t_arr = arr["t"]; btc_a = arr["btc"]; pu_a = arr["pu"]; pd_a = arr["pd"]
    ua_a = arr["ua"]; ub_a = arr["ub"]; da_a = arr["da"]; db_a = arr["db"]
    n = len(t_arr)
    if n == 0:
        return {"trades": [], "equity": CAPITAL, "cash": CAPITAL, "pnl": 0.0, "n_signals": 0}
    t_end_ms = t_arr[-1]
    t0 = datetime.fromtimestamp(t_arr[0] / 1000.0, tz=timezone.utc)
    t_end = datetime.fromtimestamp(t_end_ms / 1000.0, tz=timezone.utc)
    strike = float(btc_a[0] or 0.0)
    market_id = str(arr.get("id"))
    duration = reg_entry.get("tf_hint") or "5m"
    market = {"condition_id": market_id, "token_id_yes": "yes", "token_id_no": "no",
              "start_date_iso": t0.isoformat(),
              "open_oracle_price": strike, "strike": strike, "duration": duration,
              "fee_schedule": None, "asset": BT_ASSET}
    pf = pf or Portfolio(name=f"bti:{reg_entry.get('module')}", capital=CAPITAL)
    spot_history: list[float] = []
    n_signals = 0; n_triggered = 0
    for i in range(n):
        btc = btc_a[i]
        if btc <= 0:
            continue
        spot_history.append(btc)
        if len(spot_history) > SPOT_HISTORY_MAX_LEN:
            del spot_history[:-SPOT_HISTORY_MAX_LEN]
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
        kwargs = _build_signal_kwargs(reg_entry, market, state)
        sig = signal_fn(**kwargs); n_signals += 1
        if sig and sig.get("triggered"):
            n_triggered += 1
            d = sig.get("direction"); ep = float(sig.get("entry_price") or 0.0)
            if d in ("YES", "NO") and 0.05 <= ep <= 0.85 and rem_sec > 0:
                notional = position_notional(pf.cash, ep, RISK_PCT, MIN_CONTRACTS)
                gross_shares = int(notional / ep) if ep > 0 else 0
                gross_shares = max(gross_shares, MIN_CONTRACTS) if notional > 0 else 0
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
    closed = pf.closed_trades; total_pnl = sum(t.pnl for t in closed)
    return {"market_id": market_id, "trades": [t.to_dict() for t in closed],
            "n_closed": len(closed), "n_active_left": len(pf.active_trades),
            "n_signals": n_signals, "n_triggered": n_triggered,
            "cash": round(pf.cash, 4), "total_pnl": round(total_pnl, 4),
            "equity": round(CAPITAL + total_pnl, 4)}


def _taker_book(ask: float, bid: float) -> dict:
    """One-level book from top-of-book. NOT synthetic-flagged (avoids the
    10-contract synthetic cap): in BTC 5m markets the best level holds hundreds
    of contracts and our 0.5%-of-$200 fills are 5-10 contracts, so walk_book_buy
    fills entirely at the top ask -> avg_fill == top ask, exactly as with the
    full 15-level book for these sizes."""
    return {"asks": [[ask, 1e6]], "bids": [[bid, 1e6]], "raw": {}}


def taker_enter(pf, sig, market, direction, yes_ask, yp, no_ask, np_val,
                spot, sim_t, reg_entry):
    """Live-taker entry. Mirrors runners/run_breakout_pct_003's entry block:
    scale_in branch for registry scale_in strats, else check_entry with the live
    kwargs (queue_fraction=0.5, fair=mid, latency_book=book). The real engine
    gates apply: price band 0.05-0.85, avg_fill <= min(0.85, signal_px+0.01)
    (i.e. spread <= 1c for bid-priced signals), 0.5% sizing w/ 5-contract min,
    taker fee in shares. New trades get stamped with simulated time."""
    if direction == "YES":
        book = _taker_book(yes_ask, yp)
        fair = (yes_ask + yp) / 2.0 if yes_ask > 0 and yp > 0 else None
    else:
        book = _taker_book(no_ask, np_val)
        fair = (no_ask + np_val) / 2.0 if no_ask > 0 and np_val > 0 else None
    cid = market["condition_id"]
    existing = pf.active_trades.get(cid)
    can_scale = (bool(reg_entry.get("scale_in", False)) and existing is not None
                 and existing.direction == direction
                 and pf.add_count(cid) < int(reg_entry.get("max_adds", 0) or 0))
    if can_scale:
        tr = pf.scale_in(sig, market, book, entry_spot=spot, latency_book=book,
                         fair_price=fair, queue_fraction=QUEUE_FILL_FRACTION,
                         max_notional=None,
                         max_adds=int(reg_entry.get("max_adds", 0) or 0))
        return tr  # existing trade; keep original opened_at
    tr = pf.check_entry(sig, market, book, entry_spot=spot, latency_book=book,
                        fair_price=fair, queue_fraction=QUEUE_FILL_FRACTION,
                        max_notional=None,
                        allow_reentry=bool(reg_entry.get("allow_reentry", False)),
                        max_reentries=reg_entry.get("_max_reentries") or reg_entry.get("max_reentries", 0))
    if tr is not None:
        tr.opened_at = sim_t
    return tr


def run_market_taker_arr(arr: dict, reg_entry: dict, signal_fn,
                         pf: "Portfolio | None" = None) -> dict:
    """Live-taker twin of run_market_maker_arr: identical replay loop, but entries
    go through the real Portfolio.check_entry against a top-of-book taker book
    (fills at the current ask immediately, subject to the live spread/price
    gates). No resting orders, no adverse-selection wait. This is the model that
    matches how the live bot actually executes (walk_book_buy taker)."""
    t_arr = arr["t"]; btc_a = arr["btc"]; pu_a = arr["pu"]; pd_a = arr["pd"]
    ua_a = arr["ua"]; ub_a = arr["ub"]; da_a = arr["da"]; db_a = arr["db"]
    n = len(t_arr)
    if n == 0:
        return {"trades": [], "equity": CAPITAL, "cash": CAPITAL, "pnl": 0.0, "n_signals": 0}
    t_end_ms = t_arr[-1]
    t0 = datetime.fromtimestamp(t_arr[0] / 1000.0, tz=timezone.utc)
    t_end = datetime.fromtimestamp(t_end_ms / 1000.0, tz=timezone.utc)
    strike = float(btc_a[0] or 0.0)
    market_id = str(arr.get("id"))
    duration = reg_entry.get("tf_hint") or "5m"
    # NOTE: end_date_iso intentionally omitted. Portfolio.check_entry rejects
    # "expired" markets via wall-clock datetime.now(); every historical market is
    # expired relative to real now, so all entries would be rejected. Entries are
    # instead gated on rem_sec > 0 below (equivalent, sim-time correct).
    market = {"condition_id": market_id, "token_id_yes": "yes", "token_id_no": "no",
              "start_date_iso": t0.isoformat(),
              "open_oracle_price": strike, "strike": strike, "duration": duration,
              "fee_schedule": None, "asset": BT_ASSET}
    pf = pf or Portfolio(name=f"btt:{reg_entry.get('module')}", capital=CAPITAL)
    spot_history: list[float] = []
    n_signals = 0; n_triggered = 0
    scale_in_on = bool(reg_entry.get("scale_in", False))
    for i in range(n):
        btc = btc_a[i]
        if btc <= 0:
            continue
        spot_history.append(btc)
        if len(spot_history) > SPOT_HISTORY_MAX_LEN:
            del spot_history[:-SPOT_HISTORY_MAX_LEN]
        sim_t = t_arr[i] / 1000.0
        spot_price = btc; rem_sec = max(0.0, (t_end_ms - t_arr[i]) / 1000.0)
        pu = pu_a[i]; pd = pd_a[i]
        up_a, up_b = ua_a[i], ub_a[i]; dn_a, dn_b = da_a[i], db_a[i]
        yp = up_b if up_b > 0 else pu; yes_ask = up_a if up_a > 0 else pu
        np_val = dn_b if dn_b > 0 else pd; no_ask = dn_a if dn_a > 0 else pd
        # 1) exits (sim-time stamped)
        for _tr in pf.check_exits(spot_price, yp, np_val, rem_sec, oracle_spot=spot_price):
            _tr.closed_at = sim_t
        # 2) signal. With a position open, non-scale strats can't add (check_entry
        #    would return None), so skip the compute exactly like the maker path.
        if market_id in pf.active_trades and not scale_in_on:
            continue
        state = {"spot_price": spot_price, "spot_history": spot_history, "rem_sec": rem_sec,
                 "yp": yp, "np_val": np_val, "yes_ask": yes_ask, "yes_bid": yp,
                 "no_ask": no_ask, "no_bid": np_val}
        kwargs = _build_signal_kwargs(reg_entry, market, state)
        sig = signal_fn(**kwargs); n_signals += 1
        if sig and sig.get("triggered"):
            n_triggered += 1
            d = sig.get("direction")
            if d in ("YES", "NO") and rem_sec > 0:
                taker_enter(pf, sig, market, d, yes_ask, yp, no_ask, np_val,
                            spot_price, sim_t, reg_entry)
    # expiry settlement of any open trade
    if spot_history:
        spot = spot_history[-1]
        for cid in list(pf.active_trades.keys()):
            tr = pf.active_trades[cid]
            ref = tr.market.get("open_oracle_price") or tr.entry_spot or strike
            exit_price = (1.0 if (spot >= ref) else 0.0) if tr.direction == "YES" else (1.0 if (spot < ref) else 0.0)
            pf.force_exit(cid, exit_price, "expiry_resolve_dropped")
            tr.closed_at = t_end_ms / 1000.0
    closed = pf.closed_trades; total_pnl = sum(t.pnl for t in closed)
    return {"market_id": market_id, "trades": [t.to_dict() for t in closed],
            "n_closed": len(closed), "n_active_left": len(pf.active_trades),
            "n_signals": n_signals, "n_triggered": n_triggered,
            "cash": round(pf.cash, 4), "total_pnl": round(total_pnl, 4),
            "equity": round(CAPITAL + total_pnl, 4)}


def cash_invariant(pf: Portfolio) -> bool:
    """Single-strat wallet identity: 200 == cash + committed(active) + realized."""
    committed = sum(t.entry_notional for t in pf.active_trades.values())
    realized = sum(t.pnl for t in pf.closed_trades)
    return abs((pf.cash + committed) - (CAPITAL + realized)) < 0.05


if __name__ == "__main__":
    import glob
    name = sys.argv[1] if len(sys.argv) > 1 else "breakout_pct_003"
    reg = STRATEGIES[name]
    fn = load_signal_fn(reg)
    paths = sys.argv[2:] or sorted(glob.glob("/tmp/pb_*.json"))
    print(f"strategy={name} params={reg.get('params')}")
    tot = {"pnl": 0.0, "closed": 0, "markets": 0, "trig": 0}
    for p in paths:
        snaps = load_market_file(p)
        r = run_market(snaps, reg, fn)
        tot["pnl"] += r["total_pnl"]; tot["closed"] += r["n_closed"]; tot["markets"] += 1; tot["trig"] += r["n_triggered"]
        print(f"{os.path.basename(p)}: closed={r['n_closed']} trig={r['n_triggered']} pnl={r['total_pnl']} equity={r['equity']} active_left={r['n_active_left']}")
    print(f"TOTAL markets={tot['markets']} closed={tot['closed']} triggered={tot['trig']} pnl={round(tot['pnl'],4)}")
