"""Rate limiting and record-count caps.

The rate limiter is a simple per-instance token bucket. We deliberately do not
use a background thread or asyncio task: the MCP process is single-threaded
from the stdio loop's perspective, so a monotonic-clock check on every call is
sufficient and much easier to reason about.

Per-instance record caps come from :class:`odoo_mcp.config.InstanceConfig` and
are applied by :func:`clamp_limit`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock

from ..errors import LimitExceededError


@dataclass(slots=True)
class _Bucket:
    capacity: float
    tokens: float
    refill_per_second: float
    last_refill: float


class RateLimiter:
    """Per-instance token-bucket rate limiter.

    Capacity equals ``rate_per_minute`` tokens. Refill rate is
    ``rate_per_minute / 60`` tokens per second. One token per call.

    Thread-safe via a single lock; the MCP is single-threaded but the lock is
    cheap and makes the unit test story simpler.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._lock = Lock()

    def configure(self, instance: str, rate_per_minute: int) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        with self._lock:
            self._buckets[instance] = _Bucket(
                capacity=float(rate_per_minute),
                tokens=float(rate_per_minute),
                refill_per_second=float(rate_per_minute) / 60.0,
                last_refill=time.monotonic(),
            )

    def take(self, instance: str, now: float | None = None) -> None:
        """Consume one token or raise :class:`LimitExceededError`.

        ``now`` is injectable for deterministic unit tests.
        """
        with self._lock:
            bucket = self._buckets.get(instance)
            if bucket is None:
                raise LimitExceededError(
                    f"No rate limiter configured for instance {instance!r}."
                )
            current = now if now is not None else time.monotonic()
            elapsed = max(0.0, current - bucket.last_refill)
            bucket.tokens = min(
                bucket.capacity, bucket.tokens + elapsed * bucket.refill_per_second
            )
            bucket.last_refill = current

            if bucket.tokens < 1.0:
                needed = 1.0 - bucket.tokens
                wait = needed / bucket.refill_per_second
                raise LimitExceededError(
                    f"Rate limit exceeded for instance {instance!r}. "
                    f"Retry in ~{wait:.1f}s "
                    f"({int(bucket.capacity)} calls/min)."
                )
            bucket.tokens -= 1.0


def clamp_limit(
    requested: int | None,
    default: int,
    hard_cap: int,
) -> int:
    """Resolve a requested record limit against per-instance caps.

    * ``None`` → use the default.
    * Negative or zero → reject.
    * Above the hard cap → clamp to the hard cap (not an error; the caller's
      intent is obvious and clamping is friendlier than failing).
    """
    if requested is None:
        return default
    if not isinstance(requested, int) or isinstance(requested, bool):
        raise LimitExceededError(f"Limit must be an integer, got {type(requested).__name__}.")
    if requested <= 0:
        raise LimitExceededError(f"Limit must be positive, got {requested}.")
    return min(requested, hard_cap)
