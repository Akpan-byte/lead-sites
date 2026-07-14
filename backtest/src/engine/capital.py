# CHANGE_SUMMARY
# 2026-07-03  kilo
#   - Created engine/capital.py: GlobalCapitalPool backed by Redis.
#   - Atomic reserve/release via Lua scripts so multiple processes share one pool
#     safely.
# 2026-07-03  kilo
#   - Removed _ensure_initialized from available(); request() initializes once.
#   - reset() now accepts an optional amount so the deploy script can reconcile
#     the pool to total_capital - committed_capital.
#   - Capital logs now include the post-operation available balance.
# 2026-07-03  kilo
#   - Lua scripts now return amounts as strings (tostring).  Older Redis Lua
#     runtimes return numbers as integers, truncating fractional cents.  This
#     caused the runner to release a rounded amount while Redis had debited the
#     full fractional amount, leaking ~fee/rounding on every request cycle.
# 2026-07-05  kilo
#   - Added a second Redis key ``paper:<ns>:global:capital:committed`` so the
#     pool tracks both free and committed capital.
#   - ``request()`` now atomically moves capital from available to committed.
#   - ``release()`` now atomically moves capital from committed back to available
#     (net proceeds = entry_notional + pnl on exits, or entry_notional on cancel).
#   - Added ``reconcile(available, committed)`` to set both keys from external
#     ledger state.
#   - ``_ensure_initialized`` no longer defaults to $200; missing keys now raise
#     ``RuntimeError`` so a Redis flush cannot silently double-spend the pool.
#   - Added ``committed()`` and ``state_dict()`` accessors.
# WHY: Phase 1 accounting fix. The single ``available`` key could not distinguish
#      reserved from free capital, and the silent $200 default erased realized PnL
#      after every Redis flush or deploy restart.

"""Global Redis-backed capital pool shared by all paper-trading processes."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TOTAL_CAPITAL = 200.0
DEFAULT_NAMESPACE = "paper"
REDIS_KEY_AVAILABLE = "global:capital:available"
REDIS_KEY_COMMITTED = "global:capital:committed"


class GlobalCapitalPool:
    """
    Process-safe global capital pool backed by Redis.

    All paper-trading processes reserve entry capital from the same pool and
    release it (plus proceeds) on exit. The ``request`` and ``release`` methods
    use server-side Lua scripts so concurrent callers cannot double-spend.

    The pool now maintains two keys:
      * ``<namespace>:global:capital:available`` -- free buying power
      * ``<namespace>:global:capital:committed`` -- capital reserved by active positions

    ``available + committed`` equals the namespace's total ledger capital
    (initial capital plus realized PnL).
    """

    # Atomically reserve capital: decrement available and increment committed.
    # Returns the approved amount (may be less than requested if insufficient).
    _RESERVE_SCRIPT = """
        local available_key = KEYS[1]
        local committed_key = KEYS[2]
        local amount = tonumber(ARGV[1])
        local available = tonumber(redis.call('GET', available_key) or '0')
        if available >= amount then
            redis.call('SET', available_key, available - amount)
            local committed = tonumber(redis.call('GET', committed_key) or '0')
            redis.call('SET', committed_key, committed + amount)
            return tostring(amount)
        else
            local approved = math.max(0, available)
            redis.call('SET', available_key, 0)
            local committed = tonumber(redis.call('GET', committed_key) or '0')
            redis.call('SET', committed_key, committed + approved)
            return tostring(approved)
        end
    """

    # Atomically release capital: increment available and decrement committed.
    # The amount is net proceeds for exits (entry_notional + pnl) or the
    # original entry_notional for cancelled positions.
    _RELEASE_SCRIPT = """
        local available_key = KEYS[1]
        local committed_key = KEYS[2]
        local amount = tonumber(ARGV[1])
        local available = tonumber(redis.call('GET', available_key) or '0')
        local committed = tonumber(redis.call('GET', committed_key) or '0')
        local release_committed = tonumber(ARGV[2])
        redis.call('SET', available_key, available + amount)
        redis.call('SET', committed_key, math.max(0, committed - release_committed))
        return tostring(available + amount)
    """

    # Directly set both keys. Used by reconcile scripts, not by runners.
    _RECONCILE_SCRIPT = """
        local available_key = KEYS[1]
        local committed_key = KEYS[2]
        local available = tonumber(ARGV[1])
        local committed = tonumber(ARGV[2])
        redis.call('SET', available_key, available)
        redis.call('SET', committed_key, committed)
        return {tostring(available), tostring(committed)}
    """

    def __init__(
        self,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        total_capital: float = DEFAULT_TOTAL_CAPITAL,
        namespace: str = DEFAULT_NAMESPACE,
    ):
        import redis  # local import keeps module importable when redis is missing

        self._total_capital = float(total_capital)
        self._namespace = namespace
        self._available_key = f"{namespace}:{REDIS_KEY_AVAILABLE}"
        self._committed_key = f"{namespace}:{REDIS_KEY_COMMITTED}"
        self._redis = redis.Redis(
            host=redis_host,
            port=redis_port,
            db=0,
            decode_responses=True,
        )
        self._reserve_lua = self._redis.register_script(self._RESERVE_SCRIPT)
        self._release_lua = self._redis.register_script(self._RELEASE_SCRIPT)
        self._reconcile_lua = self._redis.register_script(self._RECONCILE_SCRIPT)
        self._initialized = False

    def _ensure_initialized(self) -> None:
        """
        Verify the pool keys exist.

        Phase 1 change: do NOT silently seed the pool to $200. A missing key
        means the pool was flushed or never reconciled; auto-initializing would
        create a double-spend risk by adding capital that is already committed
        to open positions. Callers must explicitly ``reconcile()`` from ledger
        state after a flush or before the first trade.
        """
        if self._initialized:
            return
        try:
            avail_exists = self._redis.exists(self._available_key)
            committed_exists = self._redis.exists(self._committed_key)
            if not (avail_exists and committed_exists):
                raise RuntimeError(
                    f"Capital pool keys missing for namespace {self._namespace!r}: "
                    f"available={self._available_key}, committed={self._committed_key}. "
                    "Call reconcile(available, committed) from ledger state before trading."
                )
            self._initialized = True
        except RuntimeError:
            raise
        except Exception:
            logger.exception("Failed to initialize global capital pool")
            raise

    def request(self, amount: float) -> float:
        """
        Atomically reserve up to ``amount`` from the global pool.

        Moves capital from ``available`` to ``committed``. Returns the approved
        amount (0.0 if nothing is available).
        """
        if float(amount) <= 0:
            return 0.0
        self._ensure_initialized()
        approved = self._reserve_lua(
            keys=[self._available_key, self._committed_key],
            args=[float(amount)],
        )
        approved = float(approved)
        logger.info(
            "CAPITAL_REQUEST requested=%.4f approved=%.4f available=%.4f committed=%.4f key=%s",
            float(amount), approved, self.available(), self.committed(), self._available_key,
        )
        return approved

    def release(self, amount: float, committed_amount: float | None = None) -> None:
        """
        Atomically return ``amount`` to the global pool.

        ``amount`` is the net proceeds to add back to ``available``.
        ``committed_amount`` is the capital to free from ``committed``; it
        defaults to ``amount`` for backwards compatibility. For exits the caller
        should pass net proceeds as ``amount`` and the original entry_notional
        as ``committed_amount`` so available reflects realized PnL while
        committed only drops by the reserved principal.
        """
        if float(amount) <= 0 and (committed_amount is None or float(committed_amount) <= 0):
            return
        release_committed = float(committed_amount) if committed_amount is not None else float(amount)
        new_balance = self._release_lua(
            keys=[self._available_key, self._committed_key],
            args=[float(amount), release_committed],
        )
        logger.info(
            "CAPITAL_RELEASE amount=%.4f committed_release=%.4f new_available=%.4f new_committed=%.4f key=%s",
            float(amount), release_committed, float(new_balance), self.committed(), self._available_key,
        )

    def reconcile(self, available: float, committed: float) -> None:
        """
        Set both pool keys from external ledger state.

        This is the supported way to initialize or repair a pool. It overwrites
        both ``available`` and ``committed`` atomically.
        """
        self._reconcile_lua(
            keys=[self._available_key, self._committed_key],
            args=[float(available), float(committed)],
        )
        self._initialized = True
        logger.info(
            "CAPITAL_RECONCILE available=%.4f committed=%.4f namespace=%s",
            float(available), float(committed), self._namespace,
        )

    def available(self) -> float:
        """Return current available cash in the global pool."""
        try:
            raw = self._redis.get(self._available_key)
        except Exception:
            logger.exception("Failed to read global capital pool available")
            return 0.0
        return float(raw) if raw is not None else 0.0

    def committed(self) -> float:
        """Return current committed capital in the global pool."""
        try:
            raw = self._redis.get(self._committed_key)
        except Exception:
            logger.exception("Failed to read global capital pool committed")
            return 0.0
        return float(raw) if raw is not None else 0.0

    def reset(self, amount: float | None = None) -> None:
        """Reset the available pool to ``amount`` (defaults to ``total_capital``).

        Deprecated for normal use; prefer ``reconcile()`` so committed capital is
        also tracked. Kept for compatibility with older callers.
        """
        target = float(amount) if amount is not None else self._total_capital
        self._redis.set(self._available_key, target)
        self._initialized = True
        logger.info("CAPITAL_RESET amount=%.2f key=%s", target, self._available_key)

    def state_dict(self) -> dict[str, Any]:
        return {
            "namespace": self._namespace,
            "available_key": self._available_key,
            "committed_key": self._committed_key,
            "total_capital": self._total_capital,
            "available": self.available(),
            "committed": self.committed(),
        }


__all__ = ["GlobalCapitalPool", "DEFAULT_TOTAL_CAPITAL", "DEFAULT_NAMESPACE"]
