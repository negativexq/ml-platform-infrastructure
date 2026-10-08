"""Resolve input metadata once, inside the run creator's database transaction."""

import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import date
from typing import Any

from controlplane.application.batch_inference import connection_spec
from controlplane.application.ports import UnitOfWork
from controlplane.domain.entities import JobDefinition
from controlplane.domain.errors import InvalidArgument, NotFound


def resolve_batch_snapshot(
    uow: UnitOfWork, job: JobDefinition, parameters: Mapping[str, Any]
) -> dict[str, Any]:
    if not job.batch_spec:
        return {}
    spec = json.loads(json.dumps(dict(job.batch_spec)))
    policy = spec.get("input_selection_policy", "PINNED")
    if policy == "PINNED":
        return spec
    uow.projects.lock(job.project_id)  # coordinates with dataset publication
    name = spec["input"]["name"]
    if policy == "LATEST_AT_EXECUTION":
        dataset = uow.data_catalog.dataset_version(job.project_id, name)
    elif policy == "BY_PROCESSING_DATE":
        try:
            processing_date = date.fromisoformat(parameters["processing_date"])
        except (KeyError, TypeError, ValueError):
            raise InvalidArgument("batch requires an ISO processing_date parameter") from None
        dataset = uow.data_catalog.dataset_for_date(job.project_id, name, processing_date)
    else:
        raise InvalidArgument("invalid batch input selection policy")
    if dataset is None:
        raise NotFound("batch input dataset for execution", name)
    connection = uow.data_catalog.connection(dataset.connection_id)
    template = spec["input"]
    # Credential refs were frozen in the definition. A changed connection requires
    # a new definition rather than injecting unrelated credentials at execution.
    if (
        str(dataset.connection_id) != template["connection_id"]
        or connection is None
        or connection.project_id != job.project_id
        or str(dataset.format) != template["format"]
        or json.loads(json.dumps([asdict(c) for c in dataset.columns])) != template["columns"]
    ):
        raise InvalidArgument("selected dataset differs from the batch input schema or connection")
    if dataset.row_count is not None and dataset.row_count > spec["max_rows"]:
        raise InvalidArgument("selected dataset exceeds batch row limit")
    dataset.validate_connection(connection)
    spec["input_dataset_id"] = str(dataset.id)
    spec["input"] = {
        **connection_spec(connection),
        **json.loads(json.dumps(asdict(dataset), default=str)),
    }
    return spec


def execution_batch_spec(job: JobDefinition, snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    if snapshot:
        return snapshot
    if job.batch_spec.get("input_selection_policy", "PINNED") != "PINNED":
        raise InvalidArgument("dynamic batch execution snapshot missing")
    return job.batch_spec  # pre-migration pinned executions retain their definition


def snapshot_inputs(snapshots: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        step: {
            "dataset_id": spec["input_dataset_id"],
            "name": spec["input"]["name"],
            "version": spec["input"]["version"],
            "processing_date": spec["input"].get("processing_date"),
            "selection_policy": spec.get("input_selection_policy", "PINNED"),
        }
        for step, spec in snapshots.items()
        if spec
    }
