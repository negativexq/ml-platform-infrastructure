"""Read models for the UI: project summary, endpoint metrics, audit timeline.

Nothing here changes state. Provider failures never become errors: the UI gets
`available=False` and a reason, so one unreachable metrics backend cannot take a
page down.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from controlplane.application.deployments import serving_ref
from controlplane.application.jobs import resolve_project
from controlplane.application.projects import UnitOfWorkFactory
from controlplane.application.providers import MetricsProvider, ServingProvider
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import Endpoint
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


class OverviewService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        serving: ServingProvider | None = None,
        metrics: MetricsProvider | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._serving = serving
        self._metrics = metrics

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

    def endpoint_metrics(self, project_ref: str, name: str) -> EndpointMetricsView:
        """Metrics of every revision that is receiving traffic, kept apart so a canary
        can be read next to stable."""
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
            ref = serving_ref(project, deployment)

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
