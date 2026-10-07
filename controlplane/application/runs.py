from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any
from uuid import UUID

from controlplane.application.context import current_traceparent
from controlplane.application.identity import current_actor
from controlplane.application.jobs import resolve_project
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import Run, validate_timeout
from controlplane.domain.errors import AlreadyExists, Conflict, NotFound
from controlplane.domain.parameters import fingerprint, resolve
from controlplane.domain.states import ProjectStatus, RunStatus


def _audit(run: Run, action: str, **payload: object) -> AuditEvent:
    return AuditEvent(
        occurred_at=run.updated_at,
        actor=current_actor(),
        action=action,
        entity_type="run",
        entity_id=run.id,
        project_id=run.project_id,
        payload=payload,
    )


class RunService:
    """Creates and cancels runs. It never talks to the workflow system: it records
    intent in the database and the RunReconciler makes it real."""

    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock = utc_now) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def create(
        self,
        project_ref: str,
        job_name: str,
        *,
        idempotency_key: str | None = None,
        retry_of: UUID | None = None,
        timeout_seconds: int | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> tuple[Run, bool]:
        """Returns `(run, created)`. The same `idempotency_key` for the same job
        returns the original run instead of creating a second workload."""
        try:
            return self._create(
                project_ref, job_name, idempotency_key, retry_of, timeout_seconds, parameters
            )
        except AlreadyExists:  # lost a race on the idempotency key: replay the winner
            with self._uow_factory() as uow:
                project = resolve_project(uow, project_ref)
                assert idempotency_key is not None
                existing = uow.runs.get_by_idempotency_key(project.id, idempotency_key)
                job = uow.jobs.get_by_name(project.id, job_name)
            if existing is None or job is None:
                raise
            if existing.job_definition_id != job.id:
                raise Conflict("idempotency key was used for a different job") from None
            if existing.timeout_seconds != (
                job.timeout_seconds if timeout_seconds is None else timeout_seconds
            ):
                raise Conflict("idempotency key was used with a different timeout") from None
            if existing.parameters != resolve(job.parameter_schema, parameters):
                raise Conflict("idempotency key was used with different parameters") from None
            if existing.retry_of != retry_of:
                raise Conflict("idempotency key was used for a different retry parent") from None
            return existing, False

    def _create(
        self,
        project_ref: str,
        job_name: str,
        key: str | None,
        retry_of: UUID | None,
        timeout_seconds: int | None,
        parameters: Mapping[str, Any] | None = None,
    ) -> tuple[Run, bool]:
        with self._uow_factory() as uow:
            result = self.create_in_uow(
                uow, project_ref, job_name, key, retry_of, timeout_seconds, parameters
            )
            uow.commit()
            return result

    def create_in_uow(
        self,
        uow: UnitOfWork,
        project_ref: str,
        job_name: str,
        key: str | None,
        retry_of: UUID | None,
        timeout_seconds: int | None,
        parameters: Mapping[str, Any] | None = None,
    ) -> tuple[Run, bool]:
        project = resolve_project(uow, project_ref)
        job = uow.jobs.get_by_name(project.id, job_name)
        if job is None:
            raise NotFound("job", job_name)
        timeout = validate_timeout(
            job.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        resolved = resolve(job.parameter_schema, parameters)
        if key is not None:
            existing = uow.runs.get_by_idempotency_key(project.id, key)
            if existing is not None:
                if existing.job_definition_id != job.id:
                    raise Conflict(f"idempotency key {key!r} was used for a different job")
                if existing.timeout_seconds != timeout:
                    raise Conflict("idempotency key was used with a different timeout") from None
                if existing.parameters != resolved:
                    raise Conflict("idempotency key was used with different parameters")
                if existing.retry_of != retry_of:
                    raise Conflict(
                        "idempotency key was used for a different retry parent"
                    ) from None
                return existing, False
        if project.status is not ProjectStatus.READY:
            raise Conflict(
                f"project {project.name!r} is {project.status.value}; runs need a READY project"
            )
        now = self._clock()
        run = Run(
            project_id=project.id,
            job_definition_id=job.id,
            parameters=resolved,
            retry_of=retry_of,
            timeout_seconds=timeout,
            idempotency_key=key,
            traceparent=current_traceparent(),
            created_at=now,
            updated_at=now,
        )
        uow.runs.add(run)
        uow.audit.record(
            _audit(
                run,
                "run.created",
                job=job.name,
                retry_of=str(retry_of) if retry_of else None,
                parameters_sha256=fingerprint(resolved),
            )
        )
        return run, True

    def job_names(self, project_id: UUID) -> dict[UUID, str]:
        """Job definition id -> name, so a listing can show readable names."""
        with self._uow_factory() as uow:
            return {j.id: j.name for j in uow.jobs.list(project_id)}

    def get(self, run_id: UUID) -> Run:
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id)
        if run is None:
            raise NotFound("run", run_id)
        return run

    def list(
        self,
        project_ref: str,
        *,
        job_name: str | None = None,
        limit: int = 50,
        offset: int = 0,
        statuses: Collection[RunStatus] | None = None,
    ) -> Sequence[Run]:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            job_id = None
            if job_name is not None:
                job = uow.jobs.get_by_name(project.id, job_name)
                if job is None:
                    raise NotFound("job", job_name)
                job_id = job.id
            return uow.runs.list(
                project.id, job_id=job_id, limit=limit, offset=offset, statuses=statuses
            )

    def retry(self, run_id: UUID, *, idempotency_key: str | None = None) -> tuple[Run, bool]:
        """A retry is a brand-new Run. The original is never touched."""
        with self._uow_factory() as uow:
            original = uow.runs.get(run_id)
            if original is None:
                raise NotFound("run", run_id)
            if not original.is_terminal:
                raise Conflict(f"run {run_id} is still {original.status.value}; cancel it or wait")
            job = uow.jobs.get(original.job_definition_id)
            project = uow.projects.get(original.project_id)
        assert job is not None and project is not None
        return self.create(
            str(project.id),
            job.name,
            idempotency_key=idempotency_key,
            parameters=original.parameters,
            retry_of=original.id,
            timeout_seconds=original.timeout_seconds,
        )

    def request_cancel(self, run_id: UUID) -> Run:
        """Idempotent. A PENDING run has no workload yet and is cancelled at once;
        a submitted run is flagged and the reconciler cancels the workload."""
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id)
            if run is None:
                raise NotFound("run", run_id)
            if run.is_terminal or run.cancel_requested:
                return run
            now = self._clock()
            if run.status is RunStatus.PENDING:
                updated = run.transition_to(
                    RunStatus.CANCELLED, now, reason="cancelled before submission"
                )
            else:
                updated = run.with_cancel_requested(now)
            self._write(uow, run, updated, "run.cancel_requested")
            uow.commit()
            return updated

    @staticmethod
    def _write(uow: UnitOfWork, before: Run, after: Run, action: str) -> None:
        uow.runs.update(after, expected_status=before.status)
        uow.audit.record(_audit(after, action, status=after.status.value))
