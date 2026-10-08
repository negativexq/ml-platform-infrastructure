"""Transactional dataset-triggered monitoring; no credentials enter the outbox."""

import json
from dataclasses import asdict, replace
from datetime import timedelta
from uuid import UUID, uuid4, uuid5

from controlplane.application.identity import current_actor
from controlplane.application.jobs import resolve_project
from controlplane.application.model_monitoring import ModelMonitoringService
from controlplane.application.projects import utc_now
from controlplane.application.runs import RunService
from controlplane.application.secrets import SecretInfo
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import validate_slug
from controlplane.domain.errors import Conflict, InvalidArgument, NotFound
from controlplane.domain.monitoring_automation import MonitoringExecution, MonitoringRule


class FrozenSecretMetadata:
    """Reuse API-validated secret references, like an immutable ordinary Job.

    This contains key names only. Kubernetes still resolves the actual secret at
    pod creation; forced secret deletion/rotation can fail a run independently.
    The reconciler receives no additional permission to read credentials.
    """

    def __init__(self, project_id, metadata):
        self.project_id, self.metadata = project_id, metadata

    def get(self, project, name):
        if project.id != self.project_id or name not in self.metadata:
            raise Conflict("dataset uses a connection outside the validated rule snapshot")
        return SecretInfo(**self.metadata[name])


class MonitoringAutomationService:
    def __init__(self, factory, image="", secrets=None, clock=utc_now):
        self.factory, self.image, self.secrets, self.clock = factory, image, secrets, clock

    def create(
        self,
        project_ref,
        *,
        name,
        model_version_id,
        reference_dataset_id,
        observed_dataset_name,
        feedback_dataset_name=None,
        feedback_deadline_seconds=86400,
        enabled=True,
        **options,
    ):
        for value in (name, observed_dataset_name, feedback_dataset_name):
            if value is not None:
                validate_slug(value)
        if not 60 <= feedback_deadline_seconds <= 604800:
            raise InvalidArgument("feedback deadline must be between 60 seconds and 7 days")
        with self.factory() as uow:
            project = resolve_project(uow, project_ref)
            uow.projects.lock(project.id)
            if len(uow.monitoring_automation.rules(project.id)) >= 100:
                raise Conflict("project monitoring rule limit reached")
            observed = uow.data_catalog.dataset_version(project.id, observed_dataset_name)
            feedback = (
                uow.data_catalog.dataset_version(project.id, feedback_dataset_name)
                if feedback_dataset_name
                else None
            )
            if observed is None or (feedback_dataset_name and feedback is None):
                raise InvalidArgument("rule dataset catalogs must exist before registration")
            builder = ModelMonitoringService(self.factory, self.image, self.secrets, self.clock)
            prototype = builder.build_in_uow(
                uow,
                project_ref,
                name=name,
                model_version_id=model_version_id,
                reference_dataset_id=reference_dataset_id,
                observed_dataset_id=observed.id,
                feedback_dataset_id=feedback.id if feedback else None,
                **options,
            )
            metadata = {}
            connections = {}
            for role in ("reference", "observed", "feedback"):
                dataset = prototype.monitoring_spec.get(role)
                if not dataset:
                    continue
                connection = uow.data_catalog.connection(UUID(dataset["connection_id"]))
                metadata[connection.credential_secret] = asdict(
                    self.secrets.get(project, connection.credential_secret)
                )
                connections[role] = str(connection.id)
            now = self.clock()
            rule = MonitoringRule(
                uuid4(),
                project.id,
                name,
                uow.model_versions.get(model_version_id).model_id,
                model_version_id,
                reference_dataset_id,
                observed_dataset_name,
                feedback_dataset_name,
                {"parameters": options, "secret_metadata": metadata, "connections": connections},
                self.image,
                feedback_deadline_seconds,
                enabled,
                1,
                now,
                now,
            )
            uow.monitoring_automation.add_rule(rule)
            self._audit(uow, rule, "monitoring.rule_created")
            uow.commit()
            return rule

    def list(self, project_ref):
        with self.factory() as uow:
            return uow.monitoring_automation.rules(resolve_project(uow, project_ref).id)

    def set_enabled(self, project_ref, id, *, enabled, revision):
        with self.factory() as uow:
            project = resolve_project(uow, project_ref)
            uow.projects.lock(project.id)
            rule = uow.monitoring_automation.rule(project.id, id, lock=True)
            if rule is None:
                raise NotFound("monitoring rule", id)
            if rule.revision != revision:
                raise Conflict("monitoring rule changed")
            updated = replace(rule, enabled=enabled, revision=revision + 1, updated_at=self.clock())
            uow.monitoring_automation.update_rule(updated, revision)
            self._audit(
                uow, updated, "monitoring.rule_resumed" if enabled else "monitoring.rule_paused"
            )
            uow.commit()
            return updated

    def executions(self, project_ref, id, limit=100, offset=0):
        with self.factory() as uow:
            project = resolve_project(uow, project_ref)
            if uow.monitoring_automation.rule(project.id, id) is None:
                raise NotFound("monitoring rule", id)
            return uow.monitoring_automation.executions(project.id, id, limit, offset)

    @staticmethod
    def _audit(uow, rule, action):
        uow.audit.record(
            AuditEvent(
                occurred_at=rule.updated_at,
                actor=current_actor(),
                action=action,
                entity_type="monitoring_rule",
                entity_id=rule.id,
                project_id=rule.project_id,
                payload={"name": rule.name, "revision": rule.revision},
            )
        )


class MonitoringDispatcher:
    def __init__(self, factory, clock=utc_now, heartbeat=lambda: None):
        self.factory, self.clock, self.heartbeat = factory, clock, heartbeat

    def run_once(self, limit=50):
        count = 0
        for _ in range(limit):
            self.heartbeat()
            with self.factory() as uow:
                event = uow.monitoring_automation.next_event()
                if event is None:
                    break
                uow.projects.lock(event.project_id)
                dataset = uow.data_catalog.dataset(event.dataset_id)
                now = self.clock()
                for rule in uow.monitoring_automation.rules(event.project_id):
                    if (
                        not rule.enabled
                        or rule.created_at > event.created_at
                        or rule.observed_dataset_name != dataset.name
                    ):
                        continue
                    if uow.monitoring_automation.execution(rule.id, dataset.id):
                        continue
                    execution = MonitoringExecution(
                        uuid5(rule.id, str(dataset.id)),
                        event.project_id,
                        rule.id,
                        event.id,
                        dataset.id,
                        {"rule": json.loads(json.dumps(asdict(rule), default=str))},
                        "WAITING_FEEDBACK",
                        None,
                        None,
                        None,
                        now + timedelta(seconds=rule.feedback_deadline_seconds),
                        now,
                        now,
                        now,
                    )
                    uow.monitoring_automation.add_execution(execution)
                    self._dispatch(uow, execution, rule, dataset, now)
                uow.monitoring_automation.finish_event(event.id, now)
                uow.commit()
                count += 1
        for _ in range(limit):
            self.heartbeat()
            with self.factory() as uow:
                now = self.clock()
                execution = uow.monitoring_automation.next_waiting(now)
                if execution is None:
                    break
                uow.projects.lock(execution.project_id)
                frozen = dict(execution.snapshot["rule"])
                for key in (
                    "id",
                    "project_id",
                    "model_id",
                    "model_version_id",
                    "reference_dataset_id",
                ):
                    frozen[key] = UUID(str(frozen[key]))
                rule = MonitoringRule(**frozen)
                self._dispatch(
                    uow,
                    execution,
                    rule,
                    uow.data_catalog.dataset(execution.observed_dataset_id),
                    now,
                )
                uow.commit()
        return count

    def _dispatch(self, uow, execution, rule, observed, now):
        feedback = None
        try:
            if rule.feedback_dataset_name:
                if observed.processing_date is None:
                    raise InvalidArgument(
                        "observed dataset needs processing_date for feedback selection"
                    )
                feedback = uow.data_catalog.dataset_for_date(
                    rule.project_id, rule.feedback_dataset_name, observed.processing_date
                )
                if feedback is None:
                    if now >= execution.deadline_at:
                        raise InvalidArgument("feedback deadline exceeded")
                    uow.monitoring_automation.update_execution(
                        replace(
                            execution, next_attempt_at=now + timedelta(seconds=15), updated_at=now
                        )
                    )
                    return
            for role, dataset in (("observed", observed), ("feedback", feedback)):
                if dataset and str(dataset.connection_id) != rule.options["connections"].get(role):
                    raise InvalidArgument(
                        "dataset connection differs from the validated rule snapshot"
                    )
            project = uow.projects.get(rule.project_id)
            builder = ModelMonitoringService(
                self.factory,
                rule.image,
                FrozenSecretMetadata(rule.project_id, rule.options["secret_metadata"]),
                self.clock,
            )
            job = builder.build_in_uow(
                uow,
                project.name,
                name="mon-" + execution.id.hex,
                model_version_id=rule.model_version_id,
                reference_dataset_id=rule.reference_dataset_id,
                observed_dataset_id=observed.id,
                feedback_dataset_id=feedback.id if feedback else None,
                **rule.options["parameters"],
            )
        except (InvalidArgument, Conflict, NotFound) as error:
            # Expected configuration/data failures are independent of producer success.
            updated = replace(execution, status="FAILED", reason=str(error)[:512], updated_at=now)
        else:
            uow.jobs.add(job)
            run, _ = RunService(self.factory, self.clock).create_in_uow(
                uow, project.name, job.name, "monitoring:" + str(execution.id), None, None
            )
            updated = replace(
                execution,
                status="DISPATCHED",
                job_definition_id=job.id,
                job_run_id=run.id,
                snapshot={**execution.snapshot, "monitoring_spec": job.monitoring_spec},
                updated_at=now,
            )
        uow.monitoring_automation.update_execution(updated)
        uow.audit.record(
            AuditEvent(
                occurred_at=now,
                actor=current_actor(),
                action="monitoring.execution_" + updated.status.lower(),
                entity_type="monitoring_execution",
                entity_id=execution.id,
                project_id=execution.project_id,
                payload={
                    "rule_id": str(rule.id),
                    "observed_dataset_id": str(observed.id),
                    "job_run_id": str(updated.job_run_id) if updated.job_run_id else None,
                },
            )
        )
