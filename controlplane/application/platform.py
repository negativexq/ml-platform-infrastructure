"""The control plane's own health, for the Monitor page: is the platform doing its job?

Each check is a measure, its current value, the threshold it is judged against and its
trend over the window. The thresholds match the alert rules in
observability/controlplane-alert-rules.yaml, so the page and the pager agree.

Inventory comes from the database, scoped to the projects the caller can see. Telemetry
failures never become errors: the page shows what it has and says what is missing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.application.providers import PlatformSignal, PlatformTelemetry, Sample
from controlplane.domain.entities import Deployment
from controlplane.domain.states import DeploymentStatus, ProjectStatus, RunStatus

MAX_POINTS = 240
MAX_FAILED_LOOKUP = 500


class Health(StrEnum):
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"
    NO_DATA = "no_data"


_SEVERITY = {Health.OK: 0, Health.NO_DATA: 1, Health.WARNING: 2, Health.CRITICAL: 3}


@dataclass(frozen=True, slots=True)
class SignalSpec:
    signal: PlatformSignal
    title: str
    unit: str  # "ratio", "ms", "req/s", "/min"
    group: str | None  # what one series is: "reconciler", "system", "entity"
    warn: float | None = None
    critical: float | None = None
    lower_is_worse: bool = False
    silence_is_critical: bool = False  # a heartbeat: no recent data means it stopped
    help: str = ""


SIGNALS: tuple[SignalSpec, ...] = (
    SignalSpec(
        PlatformSignal.RECONCILE_PASSES,
        "Reconciler heartbeat",
        "/min",
        "reconciler",
        warn=1.0,
        critical=0.1,
        lower_is_worse=True,
        silence_is_critical=True,
        help="Completed passes per minute. Zero means desired state is no longer being "
        "applied: runs stay pending, deployments stop converging.",
    ),
    SignalSpec(
        PlatformSignal.RECONCILE_ERRORS,
        "Reconcile errors",
        "ratio",
        "reconciler",
        warn=0.01,
        critical=0.1,
        help="Share of reconciliations that raised. Open the failing trace in Tempo.",
    ),
    SignalSpec(
        PlatformSignal.API_ERRORS,
        "API error rate",
        "ratio",
        None,
        warn=0.01,
        critical=0.05,
        help="Share of API responses that were 5xx.",
    ),
    SignalSpec(
        PlatformSignal.API_LATENCY,
        "API latency p95",
        "ms",
        None,
        warn=500.0,
        critical=1000.0,
        help="95th percentile of API response time.",
    ),
    SignalSpec(PlatformSignal.API_REQUESTS, "API traffic", "req/s", None, help="Requests served."),
    SignalSpec(
        PlatformSignal.PROVIDER_ERRORS,
        "External system errors",
        "ratio",
        "system",
        warn=0.01,
        critical=0.1,
        help="Share of calls to workflow, serving, tracking, cluster and metrics backends "
        "that failed.",
    ),
    SignalSpec(
        PlatformSignal.PROVIDER_LATENCY,
        "External system latency p95",
        "ms",
        "system",
        warn=1000.0,
        critical=5000.0,
        help="95th percentile of a call to each backend.",
    ),
    SignalSpec(
        PlatformSignal.TRANSITIONS,
        "State changes",
        "/min",
        "entity",
        help="Runs, deployments, rollouts and models changing state.",
    ),
)


@dataclass(frozen=True, slots=True)
class SeriesHealth:
    name: str  # the group ("runs", "serving"...), or "" for an ungrouped signal
    points: Sequence[Sample]
    current: float | None  # the latest value, if recent enough to speak for now
    status: Health


@dataclass(frozen=True, slots=True)
class SignalHealth:
    spec: SignalSpec
    status: Health
    series: Sequence[SeriesHealth]
    error: str | None


@dataclass(frozen=True, slots=True)
class Inventory:
    projects: int
    projects_not_ready: int
    runs_active: int
    runs_waiting: int  # accepted but not started yet
    oldest_waiting_seconds: float | None
    runs_failed_24h: int
    deployments: int
    deployments_ready: int
    deployments_failed: int
    rollouts_active: int


@dataclass(frozen=True, slots=True)
class PlatformHealth:
    generated_at: datetime
    start: datetime
    end: datetime
    step_seconds: int
    available: bool
    error: str | None
    status: Health
    signals: Sequence[SignalHealth]
    inventory: Inventory


NOT_CONNECTED = "platform metrics are not connected; set CP_PROMETHEUS_URL"


class PlatformService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        telemetry: PlatformTelemetry | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._telemetry = telemetry
        self._clock = clock

    def health(self, *, minutes: int, only: Sequence[UUID] | None = None) -> PlatformHealth:
        end = self._clock()
        start = end - timedelta(minutes=minutes)
        step = max(15, math.ceil(minutes * 60 / MAX_POINTS))
        signals = [self._signal(spec, start, end, step) for spec in SIGNALS]
        errors = [s.error for s in signals if s.error]
        available = self._telemetry is not None and len(errors) < len(signals)
        error = NOT_CONNECTED if self._telemetry is None else (errors[0] if errors else None)
        judged = [s.status for s in signals if s.status is not Health.NO_DATA]
        status = max(judged, key=_SEVERITY.__getitem__) if judged else Health.NO_DATA
        return PlatformHealth(
            generated_at=end,
            start=start,
            end=end,
            step_seconds=step,
            available=available,
            error=error,
            status=status,
            signals=signals,
            inventory=self._inventory(end, only),
        )

    def _signal(self, spec: SignalSpec, start: datetime, end: datetime, step: int) -> SignalHealth:
        if self._telemetry is None:
            return SignalHealth(spec, Health.NO_DATA, [], NOT_CONNECTED)
        try:
            raw = self._telemetry.platform_series(
                spec.signal, start=start, end=end, step_seconds=step
            )
        except ConnectionError as exc:
            return SignalHealth(spec, Health.NO_DATA, [], str(exc))
        fresh_after = end - timedelta(seconds=max(3 * step, 180))
        series = []
        for name, points in raw.items():
            current = points[-1].value if points and points[-1].at >= fresh_after else None
            series.append(SeriesHealth(name, points, current, judge(spec, current)))
        if not series:
            # A heartbeat with no series at all: nothing is reporting.
            status = Health.CRITICAL if spec.silence_is_critical else Health.NO_DATA
        else:
            status = max((s.status for s in series), key=_SEVERITY.__getitem__)
        return SignalHealth(spec, status, series, None)

    def _inventory(self, now: datetime, only: Sequence[UUID] | None) -> Inventory:
        visible = None if only is None else set(only)

        def seen(project_id: UUID) -> bool:
            return visible is None or project_id in visible

        since = now - timedelta(hours=24)
        with self._uow_factory() as uow:
            projects = [p for p in uow.projects.list(limit=10_000, offset=0) if seen(p.id)]
            active = [r for r in uow.runs.list_active() if seen(r.project_id)] + [
                r for r in uow.pipeline_runs.list_active() if seen(r.project_id)
            ]
            waiting = [r for r in active if r.status in (RunStatus.PENDING, RunStatus.SUBMITTED)]
            failed = 0
            deployments: list[Deployment] = []
            for project in projects:
                for run in uow.runs.list(
                    project.id,
                    job_id=None,
                    limit=MAX_FAILED_LOOKUP,
                    offset=0,
                    statuses={RunStatus.FAILED},
                ):
                    failed += run.updated_at >= since
                for prun in uow.pipeline_runs.list(
                    project.id,
                    definition_ids=None,
                    limit=MAX_FAILED_LOOKUP,
                    offset=0,
                    statuses={RunStatus.FAILED},
                ):
                    failed += prun.updated_at >= since
                deployments.extend(uow.deployments.list(project.id))
            rollouts = sum(1 for d in deployments if uow.rollouts.get_active(d.id) is not None)
        oldest = min((r.created_at for r in waiting), default=None)
        return Inventory(
            projects=len(projects),
            projects_not_ready=sum(1 for p in projects if p.status is not ProjectStatus.READY),
            runs_active=len(active),
            runs_waiting=len(waiting),
            oldest_waiting_seconds=(now - oldest).total_seconds() if oldest else None,
            runs_failed_24h=failed,
            deployments=len(deployments),
            deployments_ready=sum(1 for d in deployments if d.status is DeploymentStatus.READY),
            deployments_failed=sum(1 for d in deployments if d.status is DeploymentStatus.FAILED),
            rollouts_active=rollouts,
        )


def judge(spec: SignalSpec, value: float | None) -> Health:
    if value is None:
        return Health.CRITICAL if spec.silence_is_critical else Health.NO_DATA

    def crossed(threshold: float | None) -> bool:
        if threshold is None:
            return False
        return value < threshold if spec.lower_is_worse else value > threshold

    if crossed(spec.critical):
        return Health.CRITICAL
    if crossed(spec.warn):
        return Health.WARNING
    return Health.OK
