"""A personal inbox derived from lifecycle state, with durable read receipts.

Failures and completed rollouts cover the last day; unresolved serving/project issues
remain visible until they recover. Reading an incident never resolves it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID

from controlplane.application.identity import role_in, visible_project_ids
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.domain.access import Principal, ProjectRole
from controlplane.domain.entities import PipelineRun, Project, Run
from controlplane.domain.states import (
    DeploymentStatus,
    EndpointStatus,
    ProjectStatus,
    RolloutStatus,
    RunStatus,
)


@dataclass(frozen=True)
class Notification:
    id: str
    kind: str
    title: str
    project: str
    project_label: str
    resource_type: str
    resource_id: str
    resource_name: str
    status: str
    reason: str | None
    occurred_at: datetime
    needs_attention: bool
    endpoint_name: str | None = None
    read: bool = False


class NotificationService:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock = utc_now) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def list(self, principal: Principal) -> list[Notification]:
        since = self._clock() - timedelta(days=1)
        notifications: list[Notification] = []
        with self._uow_factory() as uow:
            only = visible_project_ids(uow, principal)
            projects = uow.projects.list(limit=10_000, offset=0)
            for project in projects:
                if only is not None and project.id not in only:
                    continue
                role = role_in(uow, project.id, principal)
                if role is None or not role.includes(ProjectRole.VIEWER):
                    continue

                def add(
                    kind: str,
                    title: str,
                    resource_type: str,
                    resource_id: str,
                    resource_name: str,
                    status: str,
                    at: datetime,
                    reason: str | None,
                    attention: bool = True,
                    endpoint: str | None = None,
                    context: Project = project,
                ) -> None:
                    # Terminal executions have one outcome. Stateful resources identify a
                    # transition: recovery followed by another failure is a new incident.
                    identity = f"{kind}:{resource_id}:{status}"
                    if resource_type in {"deployment", "endpoint", "project"} and kind != "rollout":
                        action = (
                            "drift_detected"
                            if status in {"DRIFTED", "DEGRADED"}
                            else status.lower()
                        )
                        event = uow.audit.latest(
                            project_id=context.id,
                            entity_type=resource_type,
                            entity_id=UUID(resource_id),
                            actions=[f"{resource_type}.{action}"],
                        )
                        # Configuration updates do not create a new incident. The immutable
                        # transition event survives them; imported state can fall back to time.
                        if event:
                            at = event.occurred_at
                        identity += f":{event.id if event else at.isoformat()}"
                    notifications.append(
                        Notification(
                            id=hashlib.sha256(identity.encode()).hexdigest(),
                            kind=kind,
                            title=title,
                            project=context.name,
                            project_label=context.display_name,
                            resource_type=resource_type,
                            resource_id=resource_id,
                            resource_name=resource_name,
                            status=status,
                            reason=reason,
                            occurred_at=at,
                            needs_attention=attention,
                            endpoint_name=endpoint,
                        )
                    )

                if project.status in {ProjectStatus.FAILED, ProjectStatus.DRIFTED}:
                    add(
                        "project",
                        f"{project.display_name} needs attention",
                        "project",
                        str(project.id),
                        project.name,
                        project.status.value,
                        project.updated_at,
                        project.status_reason,
                    )
                # Filter by finish time in storage: a run may start days before it fails.
                for kind in ("pipeline_run", "run"):
                    offset = 0
                    while True:
                        runs: Sequence[Run | PipelineRun]
                        if kind == "run":
                            runs = uow.runs.list(
                                project.id,
                                job_id=None,
                                limit=200,
                                offset=offset,
                                statuses=[RunStatus.FAILED],
                                finished_since=since,
                            )
                        else:
                            runs = uow.pipeline_runs.list(
                                project.id,
                                definition_ids=None,
                                limit=200,
                                offset=offset,
                                statuses=[RunStatus.FAILED],
                                finished_since=since,
                            )
                        for run in runs:
                            at = run.finished_at or run.updated_at
                            if at < since:
                                continue
                            if isinstance(run, Run):
                                job = uow.jobs.get(run.job_definition_id)
                                name = job.name if job else "Job run"
                            else:
                                definition = uow.pipelines.get(run.pipeline_definition_id)
                                name = definition.name if definition else "Pipeline run"
                            add(
                                kind,
                                f"{name} failed",
                                kind,
                                str(run.id),
                                name,
                                run.status.value,
                                at,
                                run.status_reason,
                            )
                        if len(runs) < 200:
                            break
                        offset += 200

                offset = 0
                while reports := uow.monitoring.list(project.id, limit=200, offset=offset):
                    for report in reports:
                        if report.created_at < since or report.status != "DRIFTED":
                            continue
                        version = uow.model_versions.get(report.model_version_id)
                        model = uow.models.get(version.model_id) if version else None
                        name = model.name if model else "Model"
                        add(
                            "model_drift",
                            f"{name} data drift detected",
                            "monitoring_report",
                            str(report.id),
                            name,
                            report.status,
                            report.created_at,
                            "Feature distributions or missing rates exceeded this check's thresholds.",
                        )
                    if len(reports) < 200 or reports[-1].created_at < since:
                        break
                    offset += 200

                for deployment in uow.deployments.list(project.id):
                    endpoint = uow.endpoints.get_by_deployment(deployment.id)
                    bad = deployment.status in {DeploymentStatus.FAILED, DeploymentStatus.DEGRADED}
                    unavailable = (
                        endpoint is not None and endpoint.status is EndpointStatus.UNAVAILABLE
                    )
                    if bad:
                        add(
                            "deployment",
                            f"{deployment.name} is {deployment.status.value.lower()}",
                            "deployment",
                            str(deployment.id),
                            deployment.name,
                            deployment.status.value,
                            deployment.updated_at,
                            deployment.status_reason,
                            endpoint=endpoint.name if unavailable and endpoint else None,
                        )
                    elif unavailable and endpoint:
                        add(
                            "endpoint",
                            f"{endpoint.name} is unavailable",
                            "endpoint",
                            str(endpoint.id),
                            endpoint.name,
                            endpoint.status.value,
                            endpoint.updated_at,
                            deployment.status_reason,
                        )
                    for rollout in uow.rollouts.list(deployment.id):
                        if rollout.status not in {
                            RolloutStatus.SUCCEEDED,
                            RolloutStatus.ROLLED_BACK,
                        }:
                            continue
                        at = rollout.finished_at or rollout.updated_at
                        if at < since:
                            continue
                        outcome = (
                            "completed"
                            if rollout.status is RolloutStatus.SUCCEEDED
                            else "rolled back"
                        )
                        add(
                            "rollout",
                            f"{deployment.name} rollout {outcome}",
                            "deployment",
                            str(rollout.id),
                            deployment.name,
                            rollout.status.value,
                            at,
                            rollout.status_reason,
                            attention=rollout.status is RolloutStatus.ROLLED_BACK,
                        )
            read = uow.notification_reads.find(
                principal.user_subject, [n.id for n in notifications]
            )
        return sorted(
            (replace(n, read=n.id in read) for n in notifications),
            key=lambda n: (n.needs_attention, n.occurred_at, n.id),
            reverse=True,
        )

    def mark_read(
        self, principal: Principal, ids: Sequence[str], *, all_notifications: bool = False
    ) -> int:
        # The caller cannot choose another user or write receipts for hidden resources.
        visible = {n.id for n in self.list(principal)}
        accepted = visible if all_notifications else visible.intersection(ids)
        with self._uow_factory() as uow:
            uow.notification_reads.mark(principal.user_subject, list(accepted), self._clock())
            uow.commit()
        return len(accepted)
