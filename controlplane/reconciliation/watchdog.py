"""Fail-stop a process whose main reconcile thread stops making progress."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar

progress: ContextVar[Callable[[], None] | None] = ContextVar("reconcile_progress", default=None)


def heartbeat() -> None:
    callback = progress.get()
    if callback is not None:
        callback()


class ReconcileWatchdog:
    def __init__(
        self,
        timeout: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        fatal: Callable[[], None] | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("watchdog timeout must be positive")
        self.timeout, self.clock = timeout, clock
        self.fatal = fatal or (lambda: os._exit(1))
        self.last_progress = clock()
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None
        self._lock = threading.Lock()

    def beat(self) -> None:
        with self._lock:
            self.last_progress = self.clock()

    def check(self) -> bool:
        with self._lock:
            stalled = self.clock() - self.last_progress >= self.timeout
        if stalled:
            logging.getLogger(__name__).error("reconcile progress deadline exceeded; stopping")
            self.fatal()
        return stalled

    def start(self) -> None:
        self.beat()
        self.worker = threading.Thread(target=self._watch, daemon=True)
        self.worker.start()

    def _watch(self) -> None:
        while not self.stop.wait(min(1.0, self.timeout / 4)):
            if self.check():
                return

    def close(self) -> None:
        self.stop.set()
        if self.worker:
            self.worker.join(2)
