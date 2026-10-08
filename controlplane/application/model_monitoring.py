"""Managed immutable monitoring checks and atomic result publication."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import UUID

from controlplane.application.batch_inference import connection_spec
from controlplane.application.identity import current_actor
from controlplane.application.jobs import JobService, require_training_digest, resolve_project
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.application.secrets import SecretProvider, validate_refs
from controlplane.application.worker_resources import worker_resources
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import JobDefinition
from controlplane.domain.errors import Conflict, InvalidArgument, NotFound
from controlplane.domain.model_monitoring import MonitoringReport, MonitoringResult
from controlplane.domain.secrets import SecretKeyRef, SecretRefs
from controlplane.domain.states import ModelKind, ProjectStatus


class ModelMonitoringService:
    def __init__(
        self,
        factory: UnitOfWorkFactory,
        image: str = "",
        secrets: SecretProvider | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.factory, self.image, self.secrets, self.clock = factory, image, secrets, clock

    def create(self, project_ref: str, **options: Any) -> tuple[JobDefinition, bool]:
        with self.factory() as uow:
            result = self.create_in_uow(uow, project_ref, **options)
            uow.commit()
            return result

    def create_in_uow(
        self, uow: UnitOfWork, project_ref: str, **options: Any
    ) -> tuple[JobDefinition, bool]:
        """Caller owns definition, run, automation intent and audit transaction."""
        job = self.build_in_uow(uow, project_ref, **options)
        existing = uow.jobs.get_by_name(job.project_id, job.name)
        if existing:
            return JobService._same_or_conflict(existing, job), False
        uow.jobs.add(job)
        uow.audit.record(
            AuditEvent(
                occurred_at=job.created_at,
                actor=current_actor(),
                action="monitoring.created",
                entity_type="job",
                entity_id=job.id,
                project_id=job.project_id,
                payload={
                    "name": job.name,
                    "model_version_id": job.monitoring_spec["model_version_id"],
                },
            )
        )
        return job, True

    def build_in_uow(
        self,
        uow: UnitOfWork,
        project_ref: str,
        *,
        name: str,
        model_version_id: UUID,
        reference_dataset_id: UUID,
        observed_dataset_id: UUID,
        features: list[str],
        feedback_dataset_id: UUID | None = None,
        task: str = "REGRESSION",
        entity_key: str = "",
        prediction_column: str = "prediction",
        label_column: str = "actual",
        psi_threshold: float = 0.2,
        missing_rate_threshold: float = 0.1,
        minimum_rows: int = 100,
        batch_size: int = 1000,
        max_rows: int = 10000000,
        max_bytes: int = 1073741824,
        max_join_bytes: int = 1073741824,
        timeout_seconds: int = 3600,
        resources: dict[str, str] | None = None,
    ) -> JobDefinition:
        if not self.image:
            raise Conflict("scientific runtime is not configured")
        require_training_digest(self.image)
        if task not in {"REGRESSION", "CLASSIFICATION"}:
            raise InvalidArgument("unsupported monitoring task")
        if (
            not math.isfinite(psi_threshold)
            or not 0 < psi_threshold <= 100
            or not math.isfinite(missing_rate_threshold)
            or not 0 < missing_rate_threshold <= 1
        ):
            raise InvalidArgument("invalid drift threshold")
        if not 1 <= minimum_rows <= max_rows <= 100000000 or not 1 <= batch_size <= 100000:
            raise InvalidArgument("invalid monitoring row or batch limit")
        if not 1 <= max_bytes <= 2147483648 or not 1 <= max_join_bytes <= 2147483648:
            raise InvalidArgument("invalid monitoring byte limit")
        project = resolve_project(uow, project_ref)
        locked = uow.projects.lock(project.id)
        if locked is None or locked.status != ProjectStatus.READY:
            raise Conflict("monitoring requires a READY project")
        project = locked
        version = uow.model_versions.get(model_version_id)
        model = uow.models.get(version.model_id) if version else None
        if model is None or version is None or model.project_id != project.id:
            raise NotFound("model version", model_version_id)
        if model.kind != ModelKind.CLASSIC:
            raise InvalidArgument("monitoring supports classic models")
        datasets = {}
        refs = {}
        for role, id in [
            ("reference", reference_dataset_id),
            ("observed", observed_dataset_id),
            ("feedback", feedback_dataset_id),
        ]:
            if id is None:
                continue
            dataset = uow.data_catalog.dataset(id)
            if dataset is None or dataset.project_id != project.id:
                raise NotFound("dataset version", id)
            connection = uow.data_catalog.connection(dataset.connection_id)
            if connection is None or connection.project_id != project.id:
                raise NotFound("data connection", dataset.connection_id)
            validate_refs(
                self.secrets,
                project,
                SecretRefs(storage_secret=connection.credential_secret),
                {},
            )
            for key in ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "SESSION_TOKEN"):
                if key != "SESSION_TOKEN" or (
                    self.secrets
                    and "AWS_SESSION_TOKEN"
                    in self.secrets.get(project, connection.credential_secret).keys
                ):
                    refs[f"BATCH_{role.upper()}_{key}"] = SecretKeyRef(
                        connection.credential_secret, f"AWS_{key}"
                    )
            datasets[role] = {
                **connection_spec(connection),
                **json.loads(json.dumps(asdict(dataset), default=str)),
            }
        schemas = {
            role: {column["name"]: column["dtype"] for column in dataset["columns"]}
            for role, dataset in datasets.items()
        }
        if (
            not 1 <= len(features) <= 32
            or len(set(features)) != len(features)
            or any(
                name not in schemas["reference"]
                or schemas["reference"].get(name) != schemas["observed"].get(name)
                for name in features
            )
        ):
            raise InvalidArgument("choose 1-32 unique features with matching dataset types")
        attribution = uow.audit.latest(
            project_id=project.id,
            entity_type="dataset_version",
            entity_id=observed_dataset_id,
            actions=["batch.output_published"],
        )
        if attribution and attribution.payload.get("model_version_id") != str(model_version_id):
            raise InvalidArgument("observed batch output belongs to another model version")
        if feedback_dataset_id:
            if (
                not entity_key
                or entity_key not in schemas["observed"]
                or schemas["observed"].get(entity_key) != schemas["feedback"].get(entity_key)
            ):
                raise InvalidArgument("feedback needs a shared entity key with matching types")
            if (
                prediction_column not in schemas["observed"]
                or label_column not in schemas["feedback"]
            ):
                raise InvalidArgument("feedback needs prediction and label columns")
            prediction_type, label_type = (
                schemas["observed"][prediction_column],
                schemas["feedback"][label_column],
            )
            if task == "REGRESSION" and (
                prediction_type not in {"number", "integer"}
                or label_type not in {"number", "integer"}
            ):
                raise InvalidArgument("regression predictions and labels must be numeric")
            if task == "CLASSIFICATION" and prediction_type != label_type:
                raise InvalidArgument("classification predictions and labels need matching types")
        spec = {
            "name": name,
            "model_version_id": str(model_version_id),
            "model_name": model.name,
            "model_version": version.version,
            "reference_dataset_id": str(reference_dataset_id),
            "observed_dataset_id": str(observed_dataset_id),
            "feedback_dataset_id": str(feedback_dataset_id) if feedback_dataset_id else None,
            **datasets,
            "features": list(features),
            "task": task,
            "entity_key": entity_key,
            "prediction_column": prediction_column,
            "label_column": label_column,
            "psi_threshold": psi_threshold,
            "missing_rate_threshold": missing_rate_threshold,
            "minimum_rows": minimum_rows,
            "batch_size": batch_size,
            "max_rows": max_rows,
            "max_bytes": max_bytes,
            "max_join_bytes": max_join_bytes,
        }
        if len(json.dumps(spec).encode()) > 65536:
            raise InvalidArgument("monitoring specification exceeds limit")
        job = JobDefinition.create(
            project_id=project.id,
            name=name,
            image=self.image,
            command=("python", "-m", "batch_inference.monitoring_worker"),
            env={},
            resources=worker_resources(resources, monitoring=spec),
            secret_refs=SecretRefs(env=refs),
            monitoring_spec=spec,
            now=self.clock(),
            timeout_seconds=timeout_seconds,
        )
        return job

    def reports(
        self,
        project_ref: str,
        model_version_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
        job_run_id: UUID | None = None,
        pipeline_run_id: UUID | None = None,
    ) -> Sequence[MonitoringReport]:
        with self.factory() as uow:
            return uow.monitoring.list(
                resolve_project(uow, project_ref).id,
                model_version_id=model_version_id,
                limit=limit,
                offset=offset,
                job_run_id=job_run_id,
                pipeline_run_id=pipeline_run_id,
            )

    def report(self, project_ref: str, id: UUID) -> MonitoringReport:
        with self.factory() as uow:
            project = resolve_project(uow, project_ref)
            report = uow.monitoring.get(id)
            if report is None or report.project_id != project.id:
                raise NotFound("monitoring report", id)
            return report


def publish_report(
    uow: UnitOfWork,
    job: JobDefinition,
    raw: str | None,
    *,
    run_id: UUID,
    now: datetime,
    pipeline: bool = False,
    step: str = "main",
) -> None:
    """Caller owns result insert, run/step CAS, audit and commit."""
    if not job.monitoring_spec:
        return
    if raw is None or len(raw.encode()) > 32768:
        raise InvalidArgument("monitoring result missing or oversized")
    try:
        result = MonitoringResult.from_json(raw)
        spec = job.monitoring_spec
        if result.execution != f"{run_id}/{step}" or any(
            str(getattr(result, field)) != str(spec.get(field))
            for field in [
                "model_version_id",
                "reference_dataset_id",
                "observed_dataset_id",
                "feedback_dataset_id",
            ]
        ):
            raise ValueError()
        if (
            not 0 <= result.reference_rows <= spec["max_rows"]
            or not 0 <= result.observed_rows <= spec["max_rows"]
        ):
            raise ValueError()
        for role, count in [
            ("reference", result.reference_rows),
            ("observed", result.observed_rows),
        ]:
            expected_count = spec[role].get("row_count")
            if expected_count is not None and expected_count != count:
                raise ValueError()
        if [feature.name for feature in result.features] != spec["features"]:
            raise ValueError()
        types = {c["name"]: c["dtype"] for c in spec["reference"]["columns"]}
        enough = min(result.reference_rows, result.observed_rows) >= spec["minimum_rows"]
        for feature in result.features:
            a, b = feature.reference_missing_rate, feature.observed_missing_rate
            if (a is None) != (result.reference_rows == 0) or (b is None) != (
                result.observed_rows == 0
            ):
                raise ValueError()
            delta = abs(a - b) if a is not None and b is not None else None
            if feature.missing_rate_change != delta or (not enough and feature.psi is not None):
                raise ValueError()
            flagged = enough and (
                (feature.psi is not None and feature.psi >= spec["psi_threshold"])
                or (
                    feature.missing_rate_change is not None
                    and feature.missing_rate_change >= spec["missing_rate_threshold"]
                )
            )
            expected = (
                "DRIFTED"
                if flagged
                else "STABLE"
                if enough and feature.psi is not None
                else "INSUFFICIENT_DATA"
            )
            if (
                feature.dtype != types[feature.name]
                or feature.drifted != flagged
                or feature.status != expected
            ):
                raise ValueError()
        status = (
            "DRIFTED"
            if any(f.drifted for f in result.features)
            else "STABLE"
            if all(f.status == "STABLE" for f in result.features)
            else "INSUFFICIENT_DATA"
        )
        if result.status != status or bool(result.performance) != bool(
            spec.get("feedback_dataset_id")
        ):
            raise ValueError()
        if result.performance:
            p = result.performance
            expected_keys = (
                {"mae", "rmse", "r2"} if spec["task"] == "REGRESSION" else {"accuracy", "macro_f1"}
            )
            coverage = p.matched_rows / result.observed_rows if result.observed_rows else None
            if (
                p.task != spec["task"]
                or set(p.metrics) != expected_keys
                or p.matched_rows + p.unmatched_predictions != result.observed_rows
                or p.matched_rows + p.unmatched_truth > spec["max_rows"]
                or p.coverage != coverage
            ):
                raise ValueError()
            if p.status != (
                "MEASURED" if p.matched_rows >= spec["minimum_rows"] else "INSUFFICIENT_DATA"
            ):
                raise ValueError()
            if (
                spec["feedback"].get("row_count") is not None
                and p.matched_rows + p.unmatched_truth != spec["feedback"]["row_count"]
            ):
                raise ValueError()
            for metric, value in p.metrics.items():
                if value is None:
                    if p.status == "MEASURED" and metric != "r2":
                        raise ValueError()
                elif (
                    p.status != "MEASURED"
                    or (metric in {"mae", "rmse"} and value < 0)
                    or (metric in {"accuracy", "macro_f1"} and not 0 <= value <= 1)
                    or (metric == "r2" and value > 1)
                ):
                    raise ValueError()
        report = MonitoringReport(
            project_id=job.project_id,
            job_definition_id=job.id,
            model_version_id=result.model_version_id,
            model_name=spec["model_name"],
            model_version=spec["model_version"],
            check_name=job.name,
            reference_dataset_id=result.reference_dataset_id,
            observed_dataset_id=result.observed_dataset_id,
            feedback_dataset_id=result.feedback_dataset_id,
            job_run_id=None if pipeline else run_id,
            pipeline_run_id=run_id if pipeline else None,
            step=step,
            status=result.status,
            result=result.as_json(),
            created_at=now,
        )
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        raise InvalidArgument("monitoring result differs from immutable execution") from error
    uow.monitoring.add(report)
    uow.audit.record(
        AuditEvent(
            occurred_at=now,
            actor="reconciler",
            action="monitoring.completed",
            entity_type="monitoring_report",
            entity_id=report.id,
            project_id=job.project_id,
            payload={
                "model_version_id": str(report.model_version_id),
                "run_id": str(run_id),
                "status": report.status,
                "step": step,
            },
        )
    )
