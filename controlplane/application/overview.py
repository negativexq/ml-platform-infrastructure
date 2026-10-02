"""Read models for the UI: project summary, endpoint metrics, audit timeline.

Nothing here changes state. Provider failures never become errors: the UI gets
`available=False` and a reason, so one unreachable metrics backend cannot take a
page down.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID

from controlplane.application.deployments import gpus_in_use, serving_ref
from controlplane.application.jobs import resolve_project
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.application.providers import MetricsPoint, MetricsProvider, ServingProvider
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import Endpoint, Rollout
from controlplane.domain.errors import NotFound
from controlplane.domain.states import DeploymentStatus

MAX_AUDIT = 500


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    project_id: UUID
    jobs: int
    pipelines: int
    runs: int
    pipeline_runs: int
    models: int
    champions: int
    deployments: int
    deployments_ready: int
    endpoints: int
    active_rollouts: int
    gpu_quota: int
    gpus_in_use: int


@dataclass(frozen=True, slots=True)
class EndpointListItem:
    endpoint: Endpoint
    deployment_status: DeploymentStatus
    active_revision: int | None


@dataclass(frozen=True, slots=True)
class RevisionMetricsView:
    revision: int
    model: str
    model_version: int
    traffic_percent: int
    p95_latency_ms: float | None
    error_rate: float | None
    requests_per_second: float | None
    requests: float | None


@dataclass(frozen=True, slots=True)
class EndpointMetricsView:
    endpoint: str
    available: bool
    error: str | None
    revisions: Sequence[RevisionMetricsView]


@dataclass(frozen=True, slots=True)
class RevisionHistoryView:
    revision: int
    model: str
    model_version: int
    role: str  # "stable", "canary" or "serving"
    points: Sequence[MetricsPoint]


@dataclass(frozen=True, slots=True)
class HistoryMarker:
    at: datetime
    label: str


@dataclass(frozen=True, slots=True)
class EndpointHistoryView:
    endpoint: str
    available: bool
    error: str | None
    start: datetime
    end: datetime
    step_seconds: int
    revisions: Sequence[RevisionHistoryView]
    max_error_rate: float | None  # the live canary's gate, drawn as a reference line
    max_p95_latency_ms: float | None
    markers: Sequence[HistoryMarker]


@dataclass(frozen=True, slots=True)
class _Target:
    ref: str
    split: dict[int, int]  # revision -> percent of traffic now
    labels: dict[int, tuple[str, int]]  # revision -> (model, version)
    rollout: Rollout | None


MAX_HISTORY_POINTS = 240


class OverviewService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        serving: ServingProvider | None = None,
        metrics: MetricsProvider | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._serving = serving
        self._metrics = metrics
        self._clock = clock

    def summary(self, project_ref: str) -> ProjectSummary:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            models = uow.models.list(project.id)
            deployments = uow.deployments.list(project.id)
            champions = sum(1 for m in models if uow.model_versions.get_champion(m.id) is not None)
            return ProjectSummary(
                project_id=project.id,
                jobs=len(uow.jobs.list(project.id)),
                pipelines=len(uow.pipelines.list_latest(project.id)),
                runs=uow.runs.count(project.id),
                pipeline_runs=uow.pipeline_runs.count(project.id),
                models=len(models),
                champions=champions,
                deployments=len(deployments),
                deployments_ready=sum(1 for d in deployments if d.status is DeploymentStatus.READY),
                endpoints=sum(1 for d in deployments if uow.endpoints.get_by_deployment(d.id)),
                active_rollouts=sum(
                    1 for d in deployments if uow.rollouts.get_active(d.id) is not None
                ),
                gpu_quota=project.gpu_quota,
                gpus_in_use=gpus_in_use(uow, project.id),
            )

    def endpoints(self, project_ref: str) -> Sequence[EndpointListItem]:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            items = []
            for deployment in uow.deployments.list(project.id):
                endpoint = uow.endpoints.get_by_deployment(deployment.id)
                if endpoint is not None:
                    items.append(
                        EndpointListItem(endpoint, deployment.status, deployment.active_revision)
                    )
            return items

    def _target(self, project_ref: str, name: str) -> _Target:
        """What an endpoint serves right now: revisions, their share, their model."""
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            endpoint = uow.endpoints.get_by_name(project.id, name)
            if endpoint is None:
                raise NotFound("endpoint", name)
            deployment = uow.deployments.get(endpoint.deployment_id)
            assert deployment is not None
            rollout = uow.rollouts.get_active(deployment.id)
            if rollout is not None:
                split = {
                    rollout.from_revision: 100 - rollout.percent,
                    rollout.to_revision: rollout.percent,
                }
            elif deployment.active_revision is not None:
                split = {deployment.active_revision: 100}
            else:
                split = {}
            labels: dict[int, tuple[str, int]] = {}
            for stored in uow.revisions.list(deployment.id):
                version = uow.model_versions.get(stored.model_version_id)
                model = uow.models.get(version.model_id) if version else None
                if version and model:
                    labels[stored.revision] = (model.name, version.version)
            return _Target(serving_ref(project, deployment), split, labels, rollout)

    def endpoint_metrics(self, project_ref: str, name: str) -> EndpointMetricsView:
        """Metrics of every revision that is receiving traffic, kept apart so a canary
        can be read next to stable."""
        target = self._target(project_ref, name)
        ref, split, labels = target.ref, target.split, target.labels

        if self._serving is None or self._metrics is None:
            return EndpointMetricsView(name, False, "metrics are not configured", [])
        try:
            backend = self._serving.get_status(ref).backend_revisions
            views = []
            for revision, percent in sorted(split.items()):
                if percent <= 0:
                    continue
                m = self._metrics.revision_metrics(ref, revision, backend.get(revision))
                model_name, version_number = labels.get(revision, ("?", 0))
                views.append(
                    RevisionMetricsView(
                        revision,
                        model_name,
                        version_number,
                        percent,
                        m.p95_latency_ms,
                        m.error_rate,
                        m.requests_per_second,
                        m.requests,
                    )
                )
        except (ConnectionError, ValueError) as exc:
            return EndpointMetricsView(name, False, str(exc), [])
        return EndpointMetricsView(name, True, None, views)

    def audit(
        self, project_ref: str, *, entity_id: UUID | None = None, limit: int = 100
    ) -> Sequence[AuditEvent]:
        """Newest first."""
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            events = uow.audit.list(project_id=project.id, entity_id=entity_id)
        return list(reversed(events))[: min(limit, MAX_AUDIT)]

    def endpoint_history(self, project_ref: str, name: str, *, minutes: int) -> EndpointHistoryView:
        """The same metrics over the last `minutes`, per revision serving now, with the live
        canary's gate and when it started, so a trend can be read against the decision."""
        target = self._target(project_ref, name)
        end = self._clock()
        start = end - timedelta(minutes=minutes)
        step = max(15, -(-minutes * 60 // MAX_HISTORY_POINTS))  # ceil, at most ~240 points
        rollout = target.rollout
        markers: list[HistoryMarker] = []
        if rollout is not None and rollout.created_at >= start:
            markers.append(HistoryMarker(rollout.created_at, "canary started"))
        if rollout is not None and rollout.step_started_at and rollout.step_started_at >= start:
            markers.append(HistoryMarker(rollout.step_started_at, f"step {rollout.percent}%"))

        def role(revision: int) -> str:
            if rollout is None:
                return "serving"
            return "canary" if revision == rollout.to_revision else "stable"

        empty = EndpointHistoryView(
            name,
            False,
            None,
            start,
            end,
            step,
            [],
            rollout.gate.max_error_rate if rollout else None,
            rollout.gate.max_p95_latency_ms if rollout else None,
            markers,
        )
        if self._serving is None or self._metrics is None:
            return replace(empty, error="metrics are not configured")
        try:
            backend = self._serving.get_status(target.ref).backend_revisions
            revisions = []
            for revision in sorted(r for r, percent in target.split.items() if percent > 0):
                model_name, version_number = target.labels.get(revision, ("?", 0))
                points = self._metrics.revision_history(
                    target.ref,
                    revision,
                    backend.get(revision),
                    start=start,
                    end=end,
                    step_seconds=step,
                )
                revisions.append(
                    RevisionHistoryView(
                        revision, model_name, version_number, role(revision), points
                    )
                )
        except (ConnectionError, ValueError) as exc:
            return replace(empty, error=str(exc))
        return replace(empty, available=True, revisions=revisions)
