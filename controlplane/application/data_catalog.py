"""S3 metadata catalog; neither datasets nor secret values pass through the API."""

from collections.abc import Sequence
from dataclasses import asdict
from typing import Any
from uuid import UUID

from controlplane.application.identity import current_actor
from controlplane.application.jobs import resolve_project
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.application.secrets import SecretProvider, validate_refs
from controlplane.domain.audit import AuditEvent
from controlplane.domain.data import DataConnection, DatasetVersion
from controlplane.domain.errors import Conflict, NotFound
from controlplane.domain.secrets import SecretRefs
from controlplane.domain.states import ProjectStatus


def content(entity: DataConnection | DatasetVersion) -> dict[str, Any]:
    return {
        key: value
        for key, value in asdict(entity).items()
        if key not in {"id", "created_at", "version"}
    }


class DataCatalogService:
    def __init__(
        self,
        factory: UnitOfWorkFactory,
        clock: Clock = utc_now,
        secrets: SecretProvider | None = None,
    ) -> None:
        self.factory, self.clock, self.secrets = factory, clock, secrets

    def create_connection(self, project_ref: str, **spec: Any) -> tuple[DataConnection, bool]:
        with self.factory() as uow:
            project = resolve_project(uow, project_ref)
            locked = uow.projects.lock(project.id)
            if locked is None or locked.status != ProjectStatus.READY:
                raise Conflict("connections require a READY project")
            project = locked
            candidate = DataConnection(project_id=project.id, created_at=self.clock(), **spec)
            existing = uow.data_catalog.connection_by_name(project.id, candidate.name)
            if existing:
                if content(existing) != content(candidate):
                    raise Conflict("connections are immutable; register a new name")
                return existing, False
            validate_refs(
                self.secrets, project, SecretRefs(storage_secret=candidate.credential_secret), {}
            )
            uow.data_catalog.add_connection(candidate)
            uow.audit.record(
                AuditEvent(
                    occurred_at=candidate.created_at,
                    actor=current_actor(),
                    action="data_connection.created",
                    entity_type="data_connection",
                    entity_id=candidate.id,
                    project_id=project.id,
                    payload={"name": candidate.name, "bucket": candidate.bucket},
                )
            )
            uow.commit()
            return candidate, True

    def publish_dataset(
        self, project_ref: str, expected_latest_version: int = 0, **spec: Any
    ) -> tuple[DatasetVersion, bool]:
        with self.factory() as uow:
            project = resolve_project(uow, project_ref)
            locked = uow.projects.lock(project.id)
            if locked is None or locked.status != ProjectStatus.READY:
                raise Conflict("datasets require a READY project")
            project = locked
            connection = uow.data_catalog.connection(spec["connection_id"])
            if connection is None or connection.project_id != project.id:
                raise NotFound("data connection", spec["connection_id"])
            latest = uow.data_catalog.dataset_version(project.id, spec["name"])
            candidate = DatasetVersion(
                project_id=project.id,
                version=(latest.version + 1 if latest else 1),
                created_at=self.clock(),
                **spec,
            )
            candidate.validate_connection(connection)
            for id in [
                candidate.producer_run_id,
                candidate.producer_pipeline_run_id,
            ]:
                if id:
                    run = (
                        uow.runs.get(id)
                        if id == candidate.producer_run_id
                        else uow.pipeline_runs.get(id)
                    )
                    if run is None or run.project_id != project.id:
                        raise NotFound("producer run", id)
            if latest and content(latest) == content(candidate):
                return latest, False
            if expected_latest_version != (latest.version if latest else 0):
                raise Conflict("dataset changed; reload the latest version before publishing")
            uow.data_catalog.add_dataset(candidate)
            uow.audit.record(
                AuditEvent(
                    occurred_at=candidate.created_at,
                    actor=current_actor(),
                    action="dataset_version.created",
                    entity_type="dataset_version",
                    entity_id=candidate.id,
                    project_id=project.id,
                    payload={
                        "name": candidate.name,
                        "version": candidate.version,
                        "connection_id": str(connection.id),
                    },
                )
            )
            uow.commit()
            return candidate, True

    def connections(
        self, project_ref: str, limit: int = 100, offset: int = 0
    ) -> Sequence[DataConnection]:
        with self.factory() as uow:
            return uow.data_catalog.connections(resolve_project(uow, project_ref).id, limit, offset)

    def datasets(
        self,
        project_ref: str,
        name: str | None = None,
        limit: int = 100,
        offset: int = 0,
        producer_run_id: UUID | None = None,
        producer_pipeline_run_id: UUID | None = None,
    ) -> Sequence[DatasetVersion]:
        with self.factory() as uow:
            return uow.data_catalog.datasets(
                resolve_project(uow, project_ref).id,
                name,
                limit,
                offset,
                producer_run_id,
                producer_pipeline_run_id,
            )

    def dataset(self, project_ref: str, name: str, version: int | None = None) -> DatasetVersion:
        with self.factory() as uow:
            entity = uow.data_catalog.dataset_version(
                resolve_project(uow, project_ref).id, name, version
            )
            if entity is None:
                raise NotFound("dataset", name)
            return entity
