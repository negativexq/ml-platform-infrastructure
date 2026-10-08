"""Managed immutable batch jobs reuse run, pipeline and scheduling contracts."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from controlplane.application.identity import current_actor
from controlplane.application.jobs import JobService, require_training_digest, resolve_project
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.application.providers import ExperimentProvider
from controlplane.application.secrets import SecretProvider, validate_refs
from controlplane.application.worker_resources import worker_resources
from controlplane.domain.audit import AuditEvent
from controlplane.domain.data import DatasetColumn, DatasetFormat, DatasetVersion
from controlplane.domain.entities import JobDefinition, validate_slug
from controlplane.domain.errors import Conflict, InvalidArgument, NotFound
from controlplane.domain.model_artifacts import validate_manifest
from controlplane.domain.secrets import SecretKeyRef, SecretRefs
from controlplane.domain.states import ModelKind, ProjectStatus


def connection_spec(connection: Any) -> dict[str, Any]:
    return {key: getattr(connection, key) for key in ("endpoint", "region", "bucket", "prefix")}


class BatchInferenceService:
    def __init__(
        self,
        factory: UnitOfWorkFactory,
        image: str = "",
        experiments: ExperimentProvider | None = None,
        secrets: SecretProvider | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.factory, self.image, self.experiments, self.secrets, self.clock = (
            factory,
            image,
            experiments,
            secrets,
            clock,
        )

    def create(
        self,
        project_ref: str,
        *,
        name: str,
        input_dataset_id: UUID,
        model_version_id: UUID,
        model_connection_id: UUID,
        output_connection_id: UUID,
        output_dataset: str,
        features: list[str],
        model_manifest: list[dict[str, Any]],
        input_selection_policy: str = "PINNED",
        output_format: DatasetFormat = DatasetFormat.PARQUET,
        prediction_dtype: str = "number",
        batch_size: int = 1000,
        max_rows: int = 10000000,
        max_bytes: int = 1073741824,
        max_model_bytes: int = 536870912,
        timeout_seconds: int = 3600,
        resources: dict[str, str] | None = None,
    ) -> tuple[JobDefinition, bool]:
        if not self.image:
            raise Conflict("batch runtime is not configured")
        require_training_digest(self.image)
        validate_slug(name, "batch name")
        validate_slug(output_dataset, "output dataset name")
        if input_selection_policy not in {"PINNED", "LATEST_AT_EXECUTION", "BY_PROCESSING_DATE"}:
            raise InvalidArgument("invalid batch input selection policy")
        if prediction_dtype not in {"number", "integer", "string", "boolean"}:
            raise InvalidArgument("unsupported prediction dtype")
        if not 1 <= batch_size <= 100000 or not 1 <= max_rows <= 100000000:
            raise InvalidArgument("invalid batch or row limit")
        # One-object conditional PUT is bounded below the 5 GiB S3 limit.
        if not 1 <= max_bytes <= 2147483648 or not 1 <= max_model_bytes <= 1073741824:
            raise InvalidArgument("invalid artifact byte limit")
        try:
            manifest = validate_manifest(model_manifest, max_model_bytes)
        except ValueError as exc:
            raise InvalidArgument(str(exc)) from None
        with self.factory() as uow:
            project = resolve_project(uow, project_ref)
            locked = uow.projects.lock(project.id)
            if locked is None or locked.status != ProjectStatus.READY:
                raise Conflict("batch inference requires a READY project")
            project = locked
            dataset = uow.data_catalog.dataset(input_dataset_id)
            version = uow.model_versions.get(model_version_id)
            model = uow.models.get(version.model_id) if version else None
            if dataset is None or dataset.project_id != project.id:
                raise NotFound("input dataset", input_dataset_id)
            if model is None or model.project_id != project.id or version is None:
                raise NotFound("model version", model_version_id)
            if model.kind != ModelKind.CLASSIC:
                raise InvalidArgument("batch inference supports classic models")
            names = [c.name for c in dataset.columns]
            if (
                not features
                or len(set(features)) != len(features)
                or not set(features) <= set(names)
                or "prediction" in names
            ):
                raise InvalidArgument("choose unique input features; prediction is reserved")
            connections = {}
            refs = {}
            for role, id in [
                ("input", dataset.connection_id),
                ("model", model_connection_id),
                ("output", output_connection_id),
            ]:
                connection = uow.data_catalog.connection(id)
                if connection is None or connection.project_id != project.id:
                    raise NotFound("data connection", id)
                connections[role] = connection
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
            # A repeat must keep the original resolved model URI, even if external metadata changed.
            existing = uow.jobs.get_by_name(project.id, name)
            uri = (
                existing.batch_spec.get("model_uri")
                if existing and existing.batch_spec
                else version.source_uri
            )
            if (
                (not uri or not str(uri).startswith("s3://"))
                and self.experiments
                and version.external_ref
            ):
                uri = self.experiments.model_artifact_uri(
                    model.registry_name(project.name), version.external_ref
                )
            parsed = urlsplit(uri or "")
            connection = connections["model"]
            prefix = connection.prefix.rstrip("/")
            key = parsed.path.lstrip("/")
            if (
                parsed.scheme != "s3"
                or parsed.netloc != connection.bucket
                or not key
                or parsed.query
                or parsed.fragment
                or (prefix and key != prefix and not key.startswith(prefix + "/"))
            ):
                raise InvalidArgument("model artifact must be an S3 prefix within its connection")
            spec = {
                "name": name,
                "input_dataset_id": str(dataset.id),
                "input_selection_policy": input_selection_policy,
                "model_version_id": str(version.id),
                "model_connection_id": str(model_connection_id),
                "output_connection_id": str(output_connection_id),
                "model_uri": uri,
                "model_manifest": manifest,
                "input": {
                    **connection_spec(connections["input"]),
                    **json.loads(json.dumps(asdict(dataset), default=str)),
                },
                "model": connection_spec(connections["model"]),
                "output": connection_spec(connections["output"]),
                "output_dataset": output_dataset,
                "features": list(features),
                "output_format": str(output_format),
                "prediction_dtype": prediction_dtype,
                "batch_size": batch_size,
                "max_rows": max_rows,
                "max_bytes": max_bytes,
                "max_model_bytes": max_model_bytes,
            }
            if len(json.dumps(spec).encode()) > 65536:
                raise InvalidArgument("batch specification too large")
            job = JobDefinition.create(
                project_id=project.id,
                name=name,
                image=self.image,
                command=("python", "-m", "batch_inference.worker"),
                env={},
                resources=worker_resources(resources, batch=spec),
                secret_refs=SecretRefs(env=refs),
                parameter_schema={
                    "type": "object",
                    "properties": {"processing_date": {"type": "string", "format": "date"}},
                    "required": ["processing_date"],
                    "additionalProperties": False,
                }
                if input_selection_policy == "BY_PROCESSING_DATE"
                else {},
                batch_spec=spec,
                now=self.clock(),
                timeout_seconds=timeout_seconds,
            )
            if existing:
                return JobService._same_or_conflict(existing, job), False
            uow.jobs.add(job)
            uow.audit.record(
                AuditEvent(
                    occurred_at=job.created_at,
                    actor=current_actor(),
                    action="batch.created",
                    entity_type="job",
                    entity_id=job.id,
                    project_id=project.id,
                    payload={
                        "name": name,
                        "input_dataset_id": str(dataset.id),
                        "model_version_id": str(version.id),
                    },
                )
            )
            uow.commit()
            return job, True


def publish_output(
    uow: UnitOfWork,
    job: JobDefinition,
    raw: str | None,
    *,
    run_id: UUID,
    pipeline: bool = False,
    step: str = "main",
    now: Any,
    snapshot: Any = None,
) -> None:
    """Caller owns the run CAS, dataset insert, audit and commit in the same transaction."""
    if not job.batch_spec:
        return
    if raw is None or len(raw.encode()) > 65536:
        raise InvalidArgument("batch result missing or oversized")
    try:
        result = json.loads(raw)
        from controlplane.application.batch_snapshots import execution_batch_spec

        spec = execution_batch_spec(job, snapshot or {})
        execution = f"{run_id}/{step}"
        suffix = "csv" if spec["output_format"] == "CSV" else "parquet"
        key = f"{spec['output']['prefix'].rstrip('/')}/{spec['name']}/{execution}.{suffix}".lstrip(
            "/"
        )
        expected_uri = f"s3://{spec['output']['bucket']}/{key}"
        columns = list(spec["input"]["columns"]) + [
            {"name": "prediction", "dtype": spec["prediction_dtype"], "nullable": False}
        ]
        if (
            result["execution"] != execution
            or result["uri"] != expected_uri
            or result["format"] != spec["output_format"]
            or result["columns"] != columns
        ):
            raise ValueError()
        if type(result["row_count"]) is not int or not 0 <= result["row_count"] <= spec["max_rows"]:
            raise ValueError()
        uow.projects.lock(job.project_id)
        connection = uow.data_catalog.connection(UUID(spec["output_connection_id"]))
        if connection is None:
            raise ValueError()
        latest = uow.data_catalog.dataset_version(job.project_id, spec["output_dataset"])
        entity = DatasetVersion(
            project_id=job.project_id,
            connection_id=connection.id,
            name=spec["output_dataset"],
            version=latest.version + 1 if latest else 1,
            uri=expected_uri,
            format=DatasetFormat(spec["output_format"]),
            columns=tuple(DatasetColumn(**c) for c in columns),
            checksum_sha256=result["checksum_sha256"],
            row_count=result["row_count"],
            processing_date=spec["input"].get("processing_date"),
            producer_run_id=None if pipeline else run_id,
            producer_pipeline_run_id=run_id if pipeline else None,
            created_at=now,
        )
        entity.validate_connection(connection)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise InvalidArgument("batch result does not match the immutable execution") from exc
    uow.data_catalog.add_dataset(entity)
    uow.audit.record(
        AuditEvent(
            occurred_at=now,
            actor="reconciler",
            action="batch.output_published",
            entity_type="dataset_version",
            entity_id=entity.id,
            project_id=job.project_id,
            payload={
                "job_id": str(job.id),
                "run_id": str(run_id),
                "step": step,
                "input_dataset_id": spec["input_dataset_id"],
                "model_version_id": spec["model_version_id"],
                "version": entity.version,
                "rows": entity.row_count,
            },
        )
    )
