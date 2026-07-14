# CHANGE_SUMMARY
# 2026-07-11  kimi
#   - Created harness/pool.py: in-memory mirror of engine.capital.GlobalCapitalPool.
# WHY: The live pool is Redis+Lua backed with atomic request/release. The
#      backtest runs single-process per strategy, so an in-memory pool with
#      byte-identical reserve/release semantics (including the
#      "approve whatever is left" behavior) reproduces live accounting exactly.
"""In-memory GlobalCapitalPool with identical semantics to the Redis+Lua version."""


class InMemoryPool:
    def __init__(self, total_capital: float = 200.0, namespace: str = "bt"):
        self._total_capital = float(total_capital)
        self._namespace = namespace
        self._available = 0.0
        self._committed = 0.0
        self._initialized = False

    def reconcile(self, available: float, committed: float) -> None:
        self._available = float(available)
        self._committed = float(committed)
        self._initialized = True

    def request(self, amount: float) -> float:
        """Reserve up to ``amount``; returns approved (mirrors _RESERVE_SCRIPT)."""
        if float(amount) <= 0:
            return 0.0
        if not self._initialized:
            raise RuntimeError("pool not reconciled")
        if self._available >= amount:
            self._available -= amount
            self._committed += amount
            return float(amount)
        approved = max(0.0, self._available)
        self._available = 0.0
        self._committed += approved
        return approved

    def release(self, amount: float, committed_amount: float | None = None) -> None:
        """Return funds (mirrors _RELEASE_SCRIPT incl. committed floor at 0)."""
        if float(amount) <= 0 and (committed_amount is None or float(committed_amount) <= 0):
            return
        release_committed = float(committed_amount) if committed_amount is not None else float(amount)
        self._available += float(amount)
        self._committed = max(0.0, self._committed - release_committed)

    def available(self) -> float:
        return self._available

    def committed(self) -> float:
        return self._committed


__all__ = ["InMemoryPool"]
