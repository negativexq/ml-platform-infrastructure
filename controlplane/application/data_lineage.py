"""Bounded lineage from immutable dataset identities and committed batch output audits."""

from typing import Any
from uuid import UUID

from controlplane.application.ports import UnitOfWork
from controlplane.domain.data import DatasetVersion


def lineage(uow: UnitOfWork, root: DatasetVersion) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []
    visited: set[UUID] = set()
    truncated = False

    def node(kind: str, id: UUID, name: str, **fields: Any) -> str:
        key = kind + ":" + str(id)
        nodes[key] = {"id": key, "kind": kind, "ref_id": id, "name": name, **fields}
        return key

    def edge(source: str, target: str, relation: str) -> None:
        value = {"source": source, "target": target, "relation": relation}
        if value not in edges:
            edges.append(value)

    def visit(dataset: DatasetVersion, depth: int) -> str:
        nonlocal truncated
        key = node("DATASET", dataset.id, dataset.name, version=dataset.version)
        if dataset.id in visited:
            return key
        visited.add(dataset.id)
        if depth >= 4 or len(nodes) >= 24:
            truncated = (
                bool(dataset.producer_run_id or dataset.producer_pipeline_run_id) or truncated
            )
            return key
        run = uow.runs.get(dataset.producer_run_id) if dataset.producer_run_id else None
        pipeline = (
            uow.pipeline_runs.get(dataset.producer_pipeline_run_id)
            if dataset.producer_pipeline_run_id
            else None
        )
        execution = None
        if run and run.project_id == root.project_id:
            job = uow.jobs.get(run.job_definition_id)
            execution = node(
                "JOB_RUN", run.id, job.name if job else "Job run", status=run.status.value
            )
        elif pipeline and pipeline.project_id == root.project_id:
            definition = uow.pipelines.get(pipeline.pipeline_definition_id)
            execution = node(
                "PIPELINE_RUN",
                pipeline.id,
                definition.name if definition else "Pipeline run",
                status=pipeline.status.value,
            )
        if execution:
            edge(execution, key, "OUTPUT")
        # Manual publication does not assert an input/model relationship. Only the
        # reconciler's atomic managed-output audit carries those immutable references.
        audit = uow.audit.latest(
            project_id=root.project_id,
            entity_type="dataset_version",
            entity_id=dataset.id,
            actions=["batch.output_published"],
        )
        if audit is None or execution is None:
            return key
        try:
            input_id, model_id = (
                UUID(str(audit.payload["input_dataset_id"])),
                UUID(str(audit.payload["model_version_id"])),
            )
        except (ValueError, KeyError, TypeError):
            return key
        source = uow.data_catalog.dataset(input_id)
        if source and source.project_id == root.project_id:
            edge(visit(source, depth + 1), execution, "INPUT")
        version = uow.model_versions.get(model_id)
        model = uow.models.get(version.model_id) if version else None
        if version and model and model.project_id == root.project_id:
            model_node = node(
                "MODEL_VERSION",
                version.id,
                model.name,
                version=version.version,
                status=version.status.value,
            )
            edge(model_node, execution, "MODEL")
            training = (
                uow.pipeline_runs.get(version.source_pipeline_run_id)
                if version.source_pipeline_run_id
                else None
            )
            if training and training.project_id == root.project_id:
                definition = uow.pipelines.get(training.pipeline_definition_id)
                training_node = node(
                    "PIPELINE_RUN",
                    training.id,
                    definition.name if definition else "Training pipeline",
                    status=training.status.value,
                )
                edge(training_node, model_node, "TRAINED")
        return key

    key = visit(root, 0)
    return {"root": key, "nodes": list(nodes.values()), "edges": edges, "truncated": truncated}
