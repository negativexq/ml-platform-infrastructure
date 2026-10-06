"""ExperimentProvider backed by an MLflow tracking server (or a file store, for tests)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from uuid import UUID

from mlflow import MlflowClient
from mlflow.entities import Run as MlflowRun
from mlflow.exceptions import MlflowException
from mlflow.protos.databricks_pb2 import (
    INVALID_PARAMETER_VALUE,
    RESOURCE_ALREADY_EXISTS,
    RESOURCE_DOES_NOT_EXIST,
    ErrorCode,
)

from controlplane.application.providers import ExperimentRun, RegisteredVersion
from controlplane.domain.errors import NotFound

_MAX_RUNS = 1000


def _run(run: MlflowRun) -> ExperimentRun:
    return ExperimentRun(
        ref=run.info.run_id,
        params=dict(run.data.params),
        metrics=dict(run.data.metrics),
        # `mlflow.*` tags are the tracker's own bookkeeping, not ours.
        tags={k: v for k, v in run.data.tags.items() if not k.startswith("mlflow.")},
        artifact_uri=run.info.artifact_uri,
    )


def _is(exc: MlflowException, code: int) -> bool:
    """`MlflowException.error_code` is the enum *name*, not its number."""
    return bool(exc.error_code == ErrorCode.Name(code))


def _quote(value: str) -> str:
    return "'" + value.replace("'", "\\'") + "'"


class MlflowExperimentProvider:
    def __init__(self, tracking_uri: str) -> None:
        self._client = MlflowClient(tracking_uri=tracking_uri)

    def ensure_experiment(self, project_id: UUID, name: str) -> str:
        """Create-or-get. Deterministic: the same name always maps to the same experiment."""
        existing = self._client.get_experiment_by_name(name)
        if existing is not None:
            return str(existing.experiment_id)
        try:
            return str(
                self._client.create_experiment(name, tags={"platform_project_id": str(project_id)})
            )
        except MlflowException as exc:
            if not _is(exc, RESOURCE_ALREADY_EXISTS):
                raise
        raced = self._client.get_experiment_by_name(name)  # created by a concurrent caller
        if raced is None:
            raise NotFound("experiment", name)
        return str(raced.experiment_id)

    def get_run(self, ref: str) -> ExperimentRun:
        try:
            return _run(self._client.get_run(ref))
        except MlflowException as exc:
            if _is(exc, RESOURCE_DOES_NOT_EXIST):
                raise NotFound("experiment run", ref) from None
            raise

    def find_runs(self, experiment_ref: str, tags: Mapping[str, str]) -> Sequence[ExperimentRun]:
        clauses = [f"tags.`{key}` = {_quote(value)}" for key, value in tags.items()]
        runs = self._client.search_runs(
            [experiment_ref],
            filter_string=" and ".join(clauses),
            max_results=_MAX_RUNS,
            order_by=["attributes.start_time ASC"],
        )
        return [_run(r) for r in runs]

    def model_artifact_uri(self, model: str, version_ref: str) -> str | None:
        try:
            version = self._client.get_model_version(model, version_ref)
        except MlflowException as exc:
            if _is(exc, RESOURCE_DOES_NOT_EXIST):
                return None
            raise
        # In MLflow 3 a version created from a logged model has a `models:/m-...` source
        # that a serving runtime cannot read; the logged model knows the real location.
        model_id = version.model_id
        if not model_id and version.source:
            logged_source = re.fullmatch(r"models:/(m-[0-9a-f]{32})", version.source)
            if logged_source:
                model_id = logged_source.group(1)
        if model_id:
            location = self._client.get_logged_model(model_id).artifact_location
            if location:
                return str(location)
        return version.source or None

    def list_model_versions(self, model: str) -> Sequence[RegisteredVersion]:
        versions = self._client.search_model_versions(f"name = {_quote(model)}")
        return sorted(
            (RegisteredVersion(ref=str(v.version), run_ref=v.run_id or None) for v in versions),
            key=lambda v: int(v.ref),
        )

    def set_model_alias(self, model: str, alias: str, version_ref: str) -> None:
        self._client.set_registered_model_alias(model, alias, version_ref)

    def delete_model_alias(self, model: str, alias: str) -> None:
        try:
            self._client.delete_registered_model_alias(model, alias)
        except MlflowException as exc:
            if not _is(exc, RESOURCE_DOES_NOT_EXIST):
                raise

    def get_model_alias(self, model: str, alias: str) -> str | None:
        try:
            return str(self._client.get_model_version_by_alias(model, alias).version)
        except MlflowException as exc:
            # An unknown model is RESOURCE_DOES_NOT_EXIST, but a missing *alias* on a
            # known model comes back as INVALID_PARAMETER_VALUE ("... not found").
            missing_alias = _is(exc, INVALID_PARAMETER_VALUE) and "not found" in str(exc)
            if _is(exc, RESOURCE_DOES_NOT_EXIST) or missing_alias:
                return None
            raise
