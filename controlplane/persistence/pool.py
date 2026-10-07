"""Public QueuePool.connect timing, including queueing, connect and pre-ping.

The observer is attached by the composition root; an uninstrumented engine has
no metrics dependency. This measures acquisition, not pure queue wait.
"""

from collections.abc import Callable
from time import perf_counter
from typing import Any, cast

from sqlalchemy.pool import QueuePool


class TimedQueuePool(QueuePool):
    observe_acquire: Callable[[float, str], None] | None = None

    def __init__(
        self,
        creator: Any,
        pool_size: int = 5,
        max_overflow: int = 10,
        timeout: float = 30,
        use_lifo: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(creator, pool_size, max_overflow, timeout, use_lifo, **kwargs)
        # -1 means unlimited; applications use the bounded default (5 + 10).
        self.capacity = self.size() + max_overflow if max_overflow >= 0 and pool_size > 0 else -1

    def connect(self) -> Any:
        observer = self.observe_acquire
        if observer is None:
            return super().connect()
        started = perf_counter()
        outcome = "ok"
        try:
            return super().connect()
        except Exception:
            outcome = "error"
            raise
        finally:
            observer(perf_counter() - started, outcome)

    def recreate(self) -> Any:
        pool = cast(TimedQueuePool, super().recreate())
        pool.observe_acquire = self.observe_acquire
        return pool
