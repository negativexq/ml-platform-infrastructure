"""Lease ownership with bounded calls and fail-stop on renewal loss.

Lease expiry coordinates standby takeover. It is not a fencing token for external
systems: provider mutations must remain idempotent/CAS guarded.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from kubernetes import client
from kubernetes.client.exceptions import ApiException


class LeaseLeadership:
    def __init__(
        self,
        api: Any,
        namespace: str,
        name: str,
        *,
        duration: int = 30,
        renew_deadline: int = 15,
        retry: float = 2,
        identity: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        fatal: Callable[[], None] | None = None,
    ) -> None:
        if not 0 < retry < renew_deadline < duration:
            raise ValueError("lease requires retry < renew deadline < duration")
        self.api, self.namespace, self.name = api, namespace, name
        self.duration, self.deadline, self.retry = duration, renew_deadline, retry
        self.identity = identity or f"{os.environ.get('HOSTNAME', 'reconciler')}-{uuid4()}"
        self.clock, self.fatal = clock, fatal or (lambda: os._exit(1))
        self.active = False
        self.confirmed = 0.0
        self.observed: tuple[Any, ...] | None = None
        self.observed_at = 0.0
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None

    @classmethod
    def from_api_client(
        cls, api_client: Any, namespace: str, name: str, **kwargs: Any
    ) -> LeaseLeadership:
        return cls(client.CoordinationV1Api(api_client), namespace, name, **kwargs)

    def attempt(self) -> bool:
        now = self.clock()
        try:
            lease = self.api.read_namespaced_lease(
                self.name, self.namespace, _request_timeout=(2, 3)
            )
        except ApiException as exc:
            if exc.status != 404:
                raise
            lease = client.V1Lease(
                metadata=client.V1ObjectMeta(name=self.name, namespace=self.namespace),
                spec=client.V1LeaseSpec(),
            )
            create = True
        else:
            create = False
        spec = lease.spec
        record = (lease.metadata.resource_version, spec.holder_identity, spec.renew_time)
        if record != self.observed:
            self.observed, self.observed_at = record, now
        if spec.holder_identity and spec.holder_identity != self.identity:
            if self.active:
                self.active = False
                logging.getLogger(__name__).error("reconciler Lease ownership lost; stopping")
                self.fatal()
                return False
            if now - self.observed_at < (spec.lease_duration_seconds or self.duration):
                return False
        spec.holder_identity = self.identity
        spec.lease_duration_seconds = self.duration
        spec.renew_time = datetime.now(UTC)
        if create or not spec.acquire_time or record[1] != self.identity:
            spec.acquire_time = spec.renew_time
            spec.lease_transitions = (spec.lease_transitions or 0) + 1
        try:
            method = (
                self.api.create_namespaced_lease if create else self.api.replace_namespaced_lease
            )
            args = (self.namespace, lease) if create else (self.name, self.namespace, lease)
            method(*args, _request_timeout=(2, 3))
        except ApiException as exc:
            if exc.status == 409:
                return False
            raise
        self.confirmed, self.active = self.clock(), True
        return True

    def wait(self) -> None:
        while not self.stop.is_set():
            try:
                if self.attempt():
                    self.worker = threading.Thread(target=self._renew, daemon=True)
                    self.worker.start()
                    return
            except Exception:  # noqa: BLE001 - no work until ownership is confirmed
                pass
            self.stop.wait(self.retry)
        raise SystemExit(0)

    def _renew(self) -> None:
        while not self.stop.wait(self.retry):
            try:
                renewed = self.attempt()
            except Exception:  # noqa: BLE001 - retry within the renewal deadline
                renewed = False
            if not renewed and self.clock() - self.confirmed >= self.deadline:
                self.active = False
                logging.getLogger(__name__).error(
                    "reconciler Lease renewal deadline exceeded; stopping"
                )
                self.fatal()
                return

    def close(self) -> None:
        self.stop.set()
        if self.worker:
            self.worker.join(6)
        # Do not release early: an in-flight provider request might still complete.
        # Standby observes expiry after the process stops renewing.
        self.active = False
