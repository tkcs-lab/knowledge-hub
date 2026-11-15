"""In-process token bucket per caller ``sub`` (plan 3.1 ``KH_RATE_LIMIT_PER_MIN``)."""

from __future__ import annotations

import math
import threading

_PRUNE_ABOVE = 10_000


class RateLimiter:
    """Capacity ``per_min`` tokens, refilled continuously at ``per_min / 60`` per second.

    ``now`` is passed in (seconds, any monotonic origin) so tests control the clock.
    """

    def __init__(self, per_min: int) -> None:
        self.capacity = float(max(per_min, 1))
        self.rate = self.capacity / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}  # sub -> (tokens, last)
        self._lock = threading.Lock()

    def allow(self, sub: str, now: float) -> tuple[bool, int]:
        """Take one token. Returns ``(True, 0)`` or ``(False, whole seconds to wait, at least 1)``."""
        with self._lock:
            tokens, last = self._buckets.get(sub, (self.capacity, now))
            tokens = min(self.capacity, tokens + max(0.0, now - last) * self.rate)
            if tokens >= 1.0:
                self._buckets[sub] = (tokens - 1.0, now)
                allowed, retry = True, 0
            else:
                self._buckets[sub] = (tokens, now)
                allowed, retry = False, max(1, math.ceil((1.0 - tokens) / self.rate))
            if len(self._buckets) > _PRUNE_ABOVE:
                self._prune(now)
            return allowed, retry

    def _prune(self, now: float) -> None:
        """Drop buckets that have refilled completely: they equal a fresh one."""
        full = [
            s
            for s, (t, last) in self._buckets.items()
            if t + max(0.0, now - last) * self.rate >= self.capacity
        ]
        for s in full:
            del self._buckets[s]
