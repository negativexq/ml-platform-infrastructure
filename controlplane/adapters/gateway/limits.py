"""Token buckets in memory: per endpoint (protects the model) and per caller (fair share).

One gateway replica keeps its own buckets, so with N replicas behind a load balancer the
effective limit is up to N times the configured one. A shared store (Redis) behind the same
`RateLimiter` port is the next step when that matters.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Sequence

from controlplane.application.gateway import Allowance


class TokenBucketLimiter:
    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._now = monotonic
        self._buckets: dict[str, tuple[float, float]] = {}  # name -> (tokens, at)
        self._lock = threading.Lock()

    def take(self, buckets: Sequence[tuple[str, int]], units: int) -> Allowance:
        with self._lock:
            now = self._now()
            levels = {name: self._level(name, limit, now) for name, limit in buckets}
            short = [(n, lim) for n, lim in buckets if levels[n] < units]
            if short:
                name, limit = min(short, key=lambda b: b[1])
                wait = (units - levels[name]) / (limit / 60)
                return Allowance(False, limit, 0, math.ceil(wait))
            for name, _ in buckets:
                self._buckets[name] = (levels[name] - units, now)
            name, limit = min(buckets, key=lambda b: levels[b[0]] - units)
            left = levels[name] - units
            return Allowance(True, limit, int(left), math.ceil((limit - left) / (limit / 60)))

    def admit(self, buckets: Sequence[tuple[str, int]]) -> Allowance:
        with self._lock:
            now = self._now()
            levels = {name: self._level(name, limit, now) for name, limit in buckets}
            name, limit = min(buckets, key=lambda b: levels[b[0]])
            level = levels[name]
            if level < 1:
                return Allowance(False, limit, 0, math.ceil((1 - level) / (limit / 60)))
            return Allowance(True, limit, int(level), math.ceil((limit - level) / (limit / 60)))

    def charge(self, buckets: Sequence[tuple[str, int]], units: int) -> None:
        with self._lock:
            now = self._now()
            for name, limit in buckets:
                self._buckets[name] = (self._level(name, limit, now) - units, now)

    def refund(self, buckets: Sequence[tuple[str, int]], units: int) -> None:
        with self._lock:
            now = self._now()
            for name, limit in buckets:
                self._buckets[name] = (
                    min(float(limit), self._level(name, limit, now) + units),
                    now,
                )

    def _level(self, name: str, limit: int, now: float) -> float:
        tokens, at = self._buckets.get(name, (float(limit), now))
        return min(float(limit), tokens + (now - at) * limit / 60)
