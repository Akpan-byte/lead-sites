# CHANGE_SUMMARY
# 2026-07-02  kilo
#   - Created engine/stack.py: SharedPool for combined-stack capital.
#   - Thread-safe request/release so strategies inside one process do not overcommit the wallet.
# WHY: Combined stacks must share a single $200 wallet while each strategy still tries to trade.

"""Shared capital pool used by combined-stack runners."""

from __future__ import annotations

import threading
from typing import Any

from engine.execution import TAKER_FEE


class SharedPool:
    """
    One shared wallet for a combined stack.

    Strategies call ``request`` before entering. The pool returns the approved
    notional (which may be smaller than requested if cash is low). On exit the
    runner calls ``release`` with the proceeds so the cash becomes available again.
    """

    def __init__(self, initial_capital: float = 200.0):
        self._initial = float(initial_capital)
        self._cash = float(initial_capital)
        self._committed = 0.0
        self._lock = threading.Lock()

    @property
    def available(self) -> float:
        with self._lock:
            return self._cash

    @property
    def committed(self) -> float:
        with self._lock:
            return self._committed

    @property
    def total_equity(self) -> float:
        with self._lock:
            return self._cash + self._committed

    def request(self, notional: float, fee: float = 0.0) -> float:
        """
        Attempt to reserve ``notional`` plus taker fee from the pool.

        Returns the approved notional. If funds are insufficient, returns the
        largest notional that can be covered (possibly 0.0).
        """
        total_needed = float(notional) + float(fee)
        if total_needed <= 0:
            return 0.0

        with self._lock:
            if total_needed <= self._cash:
                self._cash -= total_needed
                self._committed += float(notional)
                return float(notional)

            # Scale down to what we can afford, keeping fee coverage intact.
            if self._cash <= fee:
                return 0.0
            approved = self._cash - fee
            self._cash = 0.0
            self._committed += approved
            return approved

    def release(self, proceeds: float, committed_notional: float) -> None:
        """Return committed capital and add exit proceeds back to the pool."""
        with self._lock:
            self._cash += float(proceeds) + float(committed_notional)
            self._committed -= float(committed_notional)
            self._committed = max(0.0, self._committed)

    def update_equity(self, unrealized_value: float) -> None:
        """
        Adjust committed notional to reflect current mark-to-market value.
        Called by stacks on each loop so ``total_equity`` stays realistic.
        """
        with self._lock:
            self._committed = max(0.0, float(unrealized_value))

    def state_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "initial_capital": self._initial,
                "cash": round(self._cash, 4),
                "committed": round(self._committed, 4),
                "total_equity": round(self._cash + self._committed, 4),
            }

    def load_state(self, state: dict[str, Any]) -> None:
        """Restore pool cash/commitments from a persisted snapshot."""
        with self._lock:
            self._initial = float(state.get("initial_capital", self._initial))
            self._cash = float(state.get("cash", self._cash))
            self._committed = float(state.get("committed", self._committed))


__all__ = ["SharedPool"]
