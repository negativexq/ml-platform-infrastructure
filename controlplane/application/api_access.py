"""Opening endpoints to callers outside the platform: exposure, limits and API keys.

The gateway (controlplane/gateway) enforces all of this; this module is where a project's
admins change it, and every change is in the audit trail.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from controlplane.application.gateway import UNIT
from controlplane.application.identity import current_actor
from controlplane.application.jobs import resolve_project
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.application.providers import Sample, UsagePoint, UsageProvider
from controlplane.domain.api_keys import ApiKey
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import Endpoint, EndpointLimits
from controlplane.domain.errors import InvalidArgument, NotFound
from controlplane.domain.states import Exposure

MAX_USAGE_POINTS = 240


@dataclass(frozen=True, slots=True)
class EndpointAccess:
    project: str
    endpoint: Endpoint
    keys: Sequence[ApiKey]  # keys that may call this endpoint, revoked ones included


@dataclass(frozen=True, slots=True)
class CallerUsage:
    caller: str
    units: float  # in the window, about (integrated from per-minute rates)
    rejected: float
    errors: float
    points: Sequence[UsagePoint]
    prompt_tokens: float | None = None  # LLMs only
    completion_tokens: float | None = None


@dataclass(frozen=True, slots=True)
class EndpointUsage:
    available: bool
    error: str | None
    unit: str
    start: datetime
    end: datetime
    step_seconds: int
    callers: Sequence[CallerUsage]
    p95_latency_ms: Sequence[Sample]


class ApiAccessService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock = utc_now,
        usage: UsageProvider | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._usage = usage

    def access(self, project_ref: str, name: str) -> EndpointAccess:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            endpoint = uow.endpoints.get_by_name(project.id, name)
            if endpoint is None:
                raise NotFound("endpoint", name)
            keys = [k for k in uow.api_keys.list(project.id) if k.may_call(name)]
            return EndpointAccess(project.name, endpoint, keys)

    def usage(self, project_ref: str, name: str, *, minutes: int) -> EndpointUsage:
        """Calls through the gateway by caller. A metrics outage is reported, not raised."""
        access = self.access(project_ref, name)
        end = self._clock()
        start = end - timedelta(minutes=minutes)
        step = max(15, math.ceil(minutes * 60 / MAX_USAGE_POINTS))
        unit = UNIT[access.endpoint.kind]
        if self._usage is None:
            error = "usage metrics are not connected; set CP_PROMETHEUS_URL"
            return EndpointUsage(False, error, unit, start, end, step, [], [])
        try:
            series = self._usage.endpoint_usage(
                access.project, name, start=start, end=end, step_seconds=step
            )
        except (ConnectionError, ValueError) as exc:
            return EndpointUsage(False, str(exc), unit, start, end, step, [], [])
        callers = [
            CallerUsage(
                caller=caller,
                units=sum(p.units for p in points) * step / 60,
                rejected=sum(p.rejected for p in points) * step / 60,
                errors=sum(p.errors for p in points) * step / 60,
                points=points,
                prompt_tokens=series.tokens[caller][0] if caller in series.tokens else None,
                completion_tokens=series.tokens[caller][1] if caller in series.tokens else None,
            )
            for caller, points in series.callers.items()
        ]
        callers.sort(key=lambda c: c.units, reverse=True)
        return EndpointUsage(True, None, unit, start, end, step, callers, series.p95_latency_ms)

    def expose(
        self, project_ref: str, name: str, exposure: Exposure, limits: EndpointLimits
    ) -> tuple[Endpoint, bool]:
        """Set who can reach an endpoint and the limits the gateway enforces. Returns
        `(endpoint, changed)`; asking for what is already set writes nothing."""
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            endpoint = uow.endpoints.get_by_name(project.id, name)
            if endpoint is None:
                raise NotFound("endpoint", name)
            if endpoint.exposure is exposure and endpoint.limits == limits:
                return endpoint, False
            now = self._clock()
            updated = endpoint.exposed(Exposure(exposure), limits, now)
            uow.endpoints.update_access(updated)
            uow.audit.record(
                _event(
                    now,
                    "endpoint.exposure_changed",
                    "endpoint",
                    endpoint.id,
                    project.id,
                    exposure=updated.exposure.value,
                    previous=endpoint.exposure.value,
                    units_per_minute=limits.units_per_minute,
                    max_body_kb=limits.max_body_kb,
                    timeout_seconds=limits.timeout_seconds,
                )
            )
            uow.commit()
            return updated, True

    def create_key(
        self,
        project_ref: str,
        *,
        name: str,
        endpoints: Sequence[str],
        units_per_minute: int | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[ApiKey, str]:
        """A new key and its token. The token is returned this once and never again."""
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            unknown = sorted(
                e for e in set(endpoints) if not uow.endpoints.get_by_name(project.id, e)
            )
            if unknown:
                raise InvalidArgument(f"no such endpoint in this project: {', '.join(unknown)}")
            now = self._clock()
            key, token = ApiKey.issue(
                project_id=project.id,
                name=name,
                endpoints=tuple(endpoints),
                units_per_minute=units_per_minute,
                expires_at=expires_at,
                created_by=current_actor(),
                now=now,
            )
            uow.api_keys.add(key)
            uow.audit.record(
                _event(
                    now,
                    "api_key.created",
                    "api_key",
                    key.id,
                    project.id,
                    key_id=key.key_id,
                    name=key.name,
                    endpoints=list(key.endpoints),
                    units_per_minute=units_per_minute,
                    expires_at=expires_at.isoformat() if expires_at else None,
                )
            )
            uow.commit()
            return key, token

    def list_keys(self, project_ref: str) -> Sequence[ApiKey]:
        with self._uow_factory() as uow:
            return uow.api_keys.list(resolve_project(uow, project_ref).id)

    def revoke_key(self, project_ref: str, key_id: str) -> ApiKey:
        """Idempotent. The gateway stops accepting the key within its cache time."""
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            key = uow.api_keys.get(key_id)
            if key is None or key.project_id != project.id:
                raise NotFound("api key", key_id)
            if key.revoked_at is not None:
                return key
            now = self._clock()
            revoked = key.revoked(now)
            uow.api_keys.update(revoked)
            uow.audit.record(
                _event(
                    now,
                    "api_key.revoked",
                    "api_key",
                    key.id,
                    project.id,
                    key_id=key.key_id,
                    name=key.name,
                )
            )
            uow.commit()
            return revoked


def _event(
    now: datetime,
    action: str,
    entity_type: str,
    entity_id: UUID,
    project_id: UUID,
    **payload: object,
) -> AuditEvent:
    return AuditEvent(
        occurred_at=now,
        actor=current_actor(),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        project_id=project_id,
        payload=payload,
    )
