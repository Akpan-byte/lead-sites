# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - Created harness/driver.py: the feed-swap replay driver. Mirrors
#     runners/run_phase_2_*.py AssetRunner.process_markets line-for-line
#     (feature computation, signal kwargs, sizing, pool accounting, scale-in,
#     BOOK_WAIT re-poll, check_exits, expiry resolution) but driven by replayed
#     polybacktest snapshots on a 0.25s grid instead of the Redis/live loop.
# WHY: The backtest must reproduce live fills tick-for-tick. The ONLY swapped
#      parts are the feed (snapshots instead of Redis) and the clock (replay
#      time). Signal code, Portfolio, execution math, pool semantics are the
#      live ones imported unchanged from /config/backtest_repo.
"""Replay driver: runs live strategy code over historical snapshots."""

from __future__ import annotations

import json
import math
import statistics
import sys
from typing import Any

sys.path.insert(0, "/config/backtest_repo")

from engine.execution import (  # noqa: E402
    MIN_CONTRACTS,
    QUEUE_FILL_FRACTION,
    RISK_PCT,
    position_notional,
)
from engine.portfolio import Portfolio  # noqa: E402

from harness.feed import BacktestFeed, normalize_snapshot_book  # noqa: E402
from harness.pool import InMemoryPool  # noqa: E402

# Runner constants (from run_phase_2_*.py).
CAPITAL = 200.0
DEFAULT_MAX_ENTRY_PRICE = 0.85
SPOT_HISTORY_MAX_LEN = 1000
BOOK_WAIT_SEC = 2.0
TICK_SECONDS = 0.25  # live LOOP_SLEEP_SECONDS cadence


# ---------------------------------------------------------------------------
# Feature helpers — copies from the generated runners with one deviation:
# statistics.mean/stdev are replaced by math.fsum-based equivalents. Python 3.12's
# statistics module computes stdev through exact Fraction ratios (49M
# as_integer_ratio calls per day here = 71% of runtime); the fsum path is
# correctly-rounded double precision (equal to ~1 ulp) and ~100x faster. Verified
# by harness/equiv_test.py: trades stay byte-identical to the live runner.
# ---------------------------------------------------------------------------
def _fast_mean(xs):
    return math.fsum(xs) / len(xs)


def _fast_stdev(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = math.fsum(xs) / n
    return math.sqrt(math.fsum((x - m) * (x - m) for x in xs) / (n - 1))


def _compute_z_score(prices, window=20):
    if len(prices) < 2:
        return 0.0
    recent = prices[-window:] if len(prices) >= window else prices
    mean = _fast_mean(recent)
    if len(recent) < 2:
        return 0.0
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


# ---------------------------------------------------------------------------
# Per-strategy replay context (the backtest twin of AssetRunner).
# ---------------------------------------------------------------------------
class StrategyContext:
    def __init__(self, strategy_name, registry_entry, signal_fn, signal_module,
                 feed: BacktestFeed, clock, recorder):
        self.strategy = strategy_name
        self.entry = registry_entry
        self.signal_fn = signal_fn
        self.signal_module = signal_module
        self.feed = feed
        self.clock = clock
        self.recorder = recorder
        self.portfolio = Portfolio(name=strategy_name, capital=CAPITAL)
        self.pool = InMemoryPool(total_capital=CAPITAL, namespace=f"bt:{strategy_name}")
        self.pool.reconcile(CAPITAL, 0.0)
        self.spot_history: list[float] = []
        self.yp_history: list[float] = []
        self.np_history: list[float] = []
        self.oracle_spot: float | None = None
        # Stateful signal modules (daily_orb_v5, pm_orb_v4, ...) keep per-strategy
        # module-global state; we swap it in around every signal call so strategies
        # sharing a worker process stay isolated exactly like separate live runners.
        self._module_state = None
        self.diag = {k: 0 for k in (
            "signals_seen", "signals_triggered", "fills_attempted", "fills_succeeded",
            "trades_opened", "errors", "no_book", "scale_ins")}

    def swap_in(self):
        if self._module_state is not None:
            self.signal_module._STATE = self._module_state

    def swap_out(self):
        if self._module_state is not None:
            self._module_state = self.signal_module._STATE

    def update_spot(self, price):
        if price and price > 0:
            self.spot_history.append(float(price))
            if len(self.spot_history) > SPOT_HISTORY_MAX_LEN:
                self.spot_history = self.spot_history[-SPOT_HISTORY_MAX_LEN:]

    def update_prices(self, yp, np_val):
        if yp is not None and yp > 0:
            self.yp_history.append(float(yp))
            if len(self.yp_history) > SPOT_HISTORY_MAX_LEN:
                self.yp_history = self.yp_history[-SPOT_HISTORY_MAX_LEN:]
        if np_val is not None and np_val > 0:
            self.np_history.append(float(np_val))
            if len(self.np_history) > SPOT_HISTORY_MAX_LEN:
                self.np_history = self.np_history[-SPOT_HISTORY_MAX_LEN:]

    def update_oracle(self, price):
        if price and price > 0:
            self.oracle_spot = float(price)

    # -- exact copy of runner._build_signal_kwargs + VWAP extras -------------
    def _build_signal_kwargs(self, market, state, elapsed_sec=None, duration_sec=None,
                             orderbook_up=None, orderbook_down=None):
        params = list(self.entry.get("params", []))
        spot_history = self.spot_history
        spot_price = state["spot_price"]
        strike = market.get("open_oracle_price") or market.get("strike") or spot_price
        rem_sec = state["rem_sec"]
        yp = state["yp"]
        np_val = state["np_val"]
        yes_ask = state["yes_ask"]
        no_ask = state["no_ask"]

        z_score = _compute_z_score(spot_history)
        v_t, std_v, velocity_history = _compute_velocity(spot_history)
        a_t = _compute_acceleration(velocity_history)
        tick_change = _compute_tick_change(spot_history)
        _, _, spread, spread_val = _compute_spread(yes_ask, yp, no_ask, np_val)
        z_dist = abs(z_score)

        tf_hint = self.entry.get("tf_hint") or market.get("duration")
        if tf_hint is None:
            tf_hint = "15m" if rem_sec > 500 else "5m"

        or_window_seconds = self.entry.get("or_window_seconds")
        max_reentries = self.entry.get("_max_reentries") or self.entry.get("max_reentries")

        # Book imbalance for VWAP orderflow/bookmap families
        imb = 0.0
        if orderbook_up and orderbook_down:
            yes_bid_size = float(orderbook_up["bids"][0][1]) if orderbook_up.get("bids") else 0.0
            no_ask_size = float(orderbook_down["asks"][0][1]) if orderbook_down.get("asks") else 0.0
            total = yes_bid_size + no_ask_size
            if total > 0:
                imb = (yes_bid_size - no_ask_size) / total

        param_map = {
            "spot_price": spot_price,
            "strike": strike,
            "z_score": z_score,
            "rem_sec": rem_sec,
            "yp": yp,
            "np_val": np_val,
            "v_t": v_t,
            "std_v": std_v,
            "a_t": a_t,
            "spread": spread,
            "spread_val": spread_val,
            "tick_change": tick_change,
            "velocity_history": velocity_history,
            "z_dist": z_dist,
            "yes_ask": yes_ask,
            "no_ask": no_ask,
            "tf_hint": tf_hint,
            "market_id": market.get("condition_id"),
            "max_entry_price": DEFAULT_MAX_ENTRY_PRICE,
            "or_window_seconds": or_window_seconds,
            "max_reentries": max_reentries,
            "asset": market.get("asset") or "BTC",
            "start_date_iso": market.get("start_date_iso"),
            "resolution_source": market.get("resolution_source"),
            # VWAP factory extras (opt-in via params)
            "elapsed_sec": elapsed_sec,
            "duration_sec": duration_sec,
            "orderbook_up": orderbook_up,
            "orderbook_down": orderbook_down,
            "yp_history": self.yp_history,
            "np_history": self.np_history,
            "book_imbalance_val": imb,
            "config": self.entry,
        }
        return {p: param_map.get(p) for p in params}

    # -- mirror of AssetRunner.process_markets for one market at one tick -----
    def process_market(self, mkt, snap, rem_sec, spot_price):
        token_yes = mkt.market["token_id_yes"]
        token_no = mkt.market["token_id_no"]
        # Install this tick's books (shared per tick across strategies).
        yes_prices = self.feed.latest_prices(token_yes)
        no_prices = self.feed.latest_prices(token_no)
        yp = (yes_prices.get("bid") or 0.0) if yes_prices else 0.0
        yes_ask = (yes_prices.get("ask") or 1.0) if yes_prices else 1.0
        np_val = (no_prices.get("bid") or 0.0) if no_prices else 0.0
        no_ask = (no_prices.get("ask") or 1.0) if no_prices else 1.0
        self.update_prices(yp, np_val)

        duration_sec = mkt.window_end - mkt.window_start
        elapsed_sec = duration_sec - rem_sec

        # Normalized books for VWAP factory
        orderbook_up = self.feed.latest_book_real(token_yes)
        orderbook_down = self.feed.latest_book_real(token_no)

        state = {
            "spot_price": spot_price,
            "spot_history": self.spot_history,
            "rem_sec": rem_sec,
            "yp": yp,
            "np_val": np_val,
            "yes_ask": yes_ask,
            "yes_bid": yp,
            "no_ask": no_ask,
            "no_bid": np_val,
        }

        # 1) exits first (same order as the live loop)
        try:
            closed_trades = self.portfolio.check_exits(
                spot_price, yp, np_val, rem_sec, oracle_spot=self.oracle_spot
            )
            for closed in closed_trades:
                net_proceeds = closed.exit_price * closed.net_shares() - closed.exit_fee
                self.pool.release(net_proceeds)
                self.recorder.on_trade_closed(self, closed)
        except Exception as exc:
            self.diag["errors"] += 1
            self.recorder.on_event(self, "check_exits_error", str(exc))

        # 2) signal
        kwargs = self._build_signal_kwargs(
            mkt.market, state,
            elapsed_sec=elapsed_sec,
            duration_sec=duration_sec,
            orderbook_up=orderbook_up,
            orderbook_down=orderbook_down,
        )
        self.swap_in()
        try:
            sig = self.signal_fn(**kwargs)
        except Exception as exc:
            self.diag["errors"] += 1
            self.recorder.on_event(self, "signal_error", str(exc))
            self.swap_out()
            return
        self.swap_out()
        self.diag["signals_seen"] += 1
        if not (sig and sig.get("triggered")):
            return
        self.diag["signals_triggered"] += 1

        direction = sig.get("direction")
        if direction not in ("YES", "NO"):
            return
        entry_price = float(sig.get("entry_price") or 0.0)
        target_notional = position_notional(
            capital=self.pool.available(),
            entry_price=entry_price,
            risk_pct=RISK_PCT,
            min_contracts=MIN_CONTRACTS,
        )
        if target_notional <= 0:
            return
        requested = target_notional
        approved = self.pool.request(requested)
        if approved <= 0:
            self.recorder.on_event(self, "signal_no_capital", direction)
            return

        trade = None
        tick_ts = self.clock()  # restore point after any BOOK_WAIT time jump
        try:
            token = token_yes if direction == "YES" else token_no
            book = self.feed.latest_book_real(token)
            latency_book = self.feed.latency_book(token)
            # BOOK_WAIT emulation: live re-polls for up to 2s; we scan forward
            # snapshots within (t, t+2s] and execute at that later time.
            if (not book or not book.get("asks")) and snap is not None:
                later = mkt.snapshot_after(tick_ts, BOOK_WAIT_SEC, token)
                if later is not None:
                    book = later
                    latency_book = later
                    self.clock.set(later["_exec_ts"])
            if not book or not book.get("asks"):
                self.diag["no_book"] += 1
                self.recorder.on_event(self, "signal_no_book", direction)
                return

            fair_price = None
            if yes_ask is not None and yp is not None and yes_ask > 0 and yp > 0:
                fair_price = (yes_ask + yp) / 2.0
            if direction == "NO" and no_ask is not None and np_val is not None and no_ask > 0 and np_val > 0:
                fair_price = (no_ask + np_val) / 2.0

            self.diag["fills_attempted"] += 1
            cid = mkt.market["condition_id"]
            existing = self.portfolio.active_trades.get(cid)
            scale_in = bool(self.entry.get("scale_in", False))
            max_adds = int(self.entry.get("max_adds", 0) or 0)
            can_scale = (
                scale_in and existing is not None
                and existing.direction == direction
                and self.portfolio.add_count(cid) < max_adds
            )
            pre_notional = 0.0
            if can_scale:
                pre_notional = existing.entry_notional
                trade = self.portfolio.scale_in(
                    sig, mkt.market, book, entry_spot=spot_price,
                    latency_book=latency_book, fair_price=fair_price,
                    queue_fraction=QUEUE_FILL_FRACTION, max_notional=approved,
                    max_adds=max_adds,
                )
            else:
                trade = self.portfolio.check_entry(
                    sig, mkt.market, book, entry_spot=spot_price,
                    latency_book=latency_book, fair_price=fair_price,
                    queue_fraction=QUEUE_FILL_FRACTION, max_notional=approved,
                    allow_reentry=bool(self.entry.get("allow_reentry", False)),
                    max_reentries=self.entry.get("_max_reentries") or self.entry.get("max_reentries", 0),
                )
            if trade is None:
                return
        except Exception as exc:
            self.diag["errors"] += 1
            self.recorder.on_event(self, "check_entry_error", str(exc))
            return
        finally:
            self.clock.set(tick_ts)  # undo any BOOK_WAIT time jump
            if trade is None and approved > 0:
                self.pool.release(approved)

        if can_scale:
            actual_cost = max(0.0, trade.entry_notional - pre_notional)
        else:
            actual_cost = trade.entry_notional
        if approved > actual_cost:
            self.pool.release(approved - actual_cost)

        self.diag["fills_succeeded"] += 1
        if can_scale:
            self.diag["scale_ins"] += 1
        else:
            self.diag["trades_opened"] += 1
        self.recorder.on_trade_opened(self, trade)


def replay_day(ctxs, day_markets, ref, clock, feed):
    """Replay one UTC day for all strategy contexts on a shared 0.25s grid.

    Mirrors the live runner main loop: each tick updates spot/oracle, then every
    active market is processed (exits first, then signal/entry). At t == window_end
    rem_sec hits 0 and check_exits resolves the position against the oracle,
    exactly like the live expiry path.
    """
    from harness.feed import normalize_snapshot_book

    if not day_markets:
        return
    day_markets.sort(key=lambda m: m.window_start)
    for m in day_markets:
        m.reset()

    t_ms = int(min(m.window_start for m in day_markets)) * 1000
    end_ms = int(max(m.window_end for m in day_markets)) * 1000
    step_ms = int(TICK_SECONDS * 1000)

    lo = 0  # index of first market whose window_end >= t (markets sorted by start)
    n = len(day_markets)
    while t_ms <= end_ms:
        t = t_ms / 1000.0
        clock.set(t)
        # Drop markets fully in the past.
        while lo < n and day_markets[lo].window_end < t:
            lo += 1
        # Install this tick's books for every active market (shared by all ctxs).
        active = []
        j = lo
        while j < n and day_markets[j].window_start <= t:
            m = day_markets[j]
            snap = m.advance(t)
            feed.set_book(m.market["token_id_yes"],
                          normalize_snapshot_book(snap.get("orderbook_up")) if snap else None)
            feed.set_book(m.market["token_id_no"],
                          normalize_snapshot_book(snap.get("orderbook_down")) if snap else None)
            active.append((m, snap))
            j += 1

        spot = ref.price_at(t)
        for ctx in ctxs:
            ctx.update_spot(spot)
            ctx.update_oracle(spot)  # oracle == Binance reference in the backtest
            if not ctx.spot_history:
                continue
            for m, snap in active:
                rem = max(0.0, m.window_end - t)
                ctx.process_market(m, snap, rem, spot)
        t_ms += step_ms


__all__ = ["StrategyContext", "replay_day", "TICK_SECONDS", "CAPITAL"]
