# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - Created harness/recorder.py: per-strategy JSONL writers for closed trades,
#     equity points, and notable events (no_capital/no_book/errors).
# WHY: The quant suite consumes closed-trade records; equity points feed drawdown
#      analytics; events explain gaps (why signals did not convert to fills).
"""Per-strategy output writers for the replay."""

from __future__ import annotations

import json
import os


def _iso(ts: float) -> str:
    import datetime as dt
    return dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Recorder:
    def __init__(self, out_dir: str, strategy: str, fresh: bool = True):
        self.strategy = strategy
        self.dir = os.path.join(out_dir, strategy)
        os.makedirs(self.dir, exist_ok=True)
        # fresh=True truncates (new run); fresh=False appends (resume).
        mode = "w" if fresh else "a"
        self._trades = open(os.path.join(self.dir, "trades.jsonl"), mode, encoding="utf-8")
        self._equity = open(os.path.join(self.dir, "equity.jsonl"), mode, encoding="utf-8")
        self._events = open(os.path.join(self.dir, "events.jsonl"), mode, encoding="utf-8")

    def _write(self, fh, obj):
        fh.write(json.dumps(obj, separators=(",", ":"), default=str) + "\n")

    def on_trade_opened(self, ctx, trade):
        self._write(self._events, {
            "t": trade.opened_at, "iso": _iso(trade.opened_at), "kind": "entry",
            "cid": trade.condition_id, "dir": trade.direction,
            "px": round(trade.entry_price, 4), "notional": round(trade.entry_notional, 4),
            "adds": trade.adds,
        })

    def on_trade_closed(self, ctx, trade):
        eq = ctx.portfolio.equity()
        self._write(self._trades, {
            "strategy": ctx.strategy,
            "asset": trade.market.get("asset", "BTC"),
            "condition_id": trade.condition_id,
            "direction": trade.direction,
            "entry_ts": trade.opened_at,
            "entry_iso": _iso(trade.opened_at),
            "exit_ts": trade.closed_at,
            "exit_iso": _iso(trade.closed_at),
            "entry_price": round(trade.entry_price, 5),
            "exit_price": trade.exit_price,
            "shares": trade.shares,
            "fee_shares": trade.fee_shares,
            "net_shares": round(trade.net_shares(), 2),
            "entry_notional": round(trade.entry_notional, 4),
            "entry_fee": round(trade.entry_fee, 5),
            "exit_fee": round(trade.exit_fee, 5),
            "pnl": round(trade.pnl, 5),
            "exit_reason": trade.exit_reason,
            "adds": trade.adds,
            "entry_spot": trade.entry_spot,
            "open_oracle_price": trade.market.get("open_oracle_price"),
            "window_start": trade.market.get("start_date_iso"),
            "window_end": trade.market.get("end_date_iso"),
            "equity_after": round(eq, 4),
        })
        self._write(self._equity, {
            "t": trade.closed_at, "iso": _iso(trade.closed_at),
            "equity": round(eq, 4), "cash": round(ctx.portfolio.cash, 4),
            "pnl_cum": round(sum(t.pnl for t in ctx.portfolio.closed_trades), 4),
        })

    def on_event(self, ctx, kind: str, detail: str):
        self._write(self._events, {
            "t": ctx.clock(), "iso": _iso(ctx.clock()), "kind": kind,
            "detail": str(detail)[:200],
        })

    def flush(self):
        for fh in (self._trades, self._equity, self._events):
            fh.flush()
            os.fsync(fh.fileno())

    def close(self):
        for fh in (self._trades, self._equity, self._events):
            fh.close()


__all__ = ["Recorder"]
