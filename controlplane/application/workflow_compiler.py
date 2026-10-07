"""Compiles a platform Run into a provider-neutral WorkflowSpec. Pure."""

from __future__ import annotations

from collections.abc import Mapping

from controlplane.application.namespaces import LABEL_MANAGED_BY, LABEL_PROJECT, MANAGED_BY
from controlplane.application.providers import StepSpec, WorkflowSpec
from controlplane.domain.entities import (
    JobDefinition,
    PipelineDefinition,
    PipelineRun,
    Project,
    Run,
)
from controlplane.domain.parameters import encode, project_values

LABEL_RUN_ID = "mlp.io/run-id"
LABEL_JOB = "mlp.io/job"
LABEL_PIPELINE_RUN_ID = "mlp.io/pipeline-run-id"
MAIN_STEP = "main"


def workflow_name(run: Run) -> str:
    """Deterministic, DNS-safe, unique per run (so a re-submit finds the same workflow)."""
    return f"run-{run.id.hex[:16]}"


def compile_job_run(project: Project, job: JobDefinition, run: Run) -> WorkflowSpec:
    return WorkflowSpec(
        name=workflow_name(run),
        namespace=project.namespace,
        timeout_seconds=run.timeout_seconds,
        image_pull_secrets=job.secret_refs.image_pull_secrets,
        steps=(
            StepSpec(
                name=MAIN_STEP,
                image=job.image,
                command=job.command,
                env={
                    **job.env,
                    **(
                        {"MLP_PARAMETERS": encode(dict(run.parameters))}
                        if job.parameter_schema
                        else {}
                    ),
                },
                secret_refs=job.secret_refs,
                resources=dict(job.resources),
            ),
        ),
        labels={
            LABEL_MANAGED_BY: MANAGED_BY,
            LABEL_PROJECT: project.name,
            LABEL_JOB: job.name,
            LABEL_RUN_ID: str(run.id),
        },
    )


# --- pipelines ---------------------------------------------------------------
# Tracker tags the platform expects a step's tracking run to carry. The step code
# (see scripts/train.py) reads the MLP_* env vars below and writes them as tags;
# the platform then finds a run's tracked results by these tags alone.
TAG_PROJECT_ID = "platform_project_id"
TAG_PIPELINE_RUN_ID = "platform_pipeline_run_id"
TAG_STEP = "platform_step"
TAG_COMMIT_SHA = "commit_sha"
TAG_IMAGE = "image"


def tracking_experiment_name(project: Project) -> str:
    """Deterministic experiment name for a project (names are unique and never reused)."""
    return f"mlp-{project.name}"


def pipeline_workflow_name(run: PipelineRun) -> str:
    return f"pr-{run.id.hex[:16]}"


def compile_pipeline_run(
    project: Project,
    definition: PipelineDefinition,
    jobs: Mapping[str, JobDefinition],
    run: PipelineRun,
    *,
    tracking_uri: str | None = None,
) -> WorkflowSpec:
    """One workflow for the whole pipeline: a step per node, `depends_on` per edge.
    The platform-owned environment is applied last so a job cannot spoof it."""
    base = {
        "MLP_PROJECT_ID": str(project.id),
        "MLP_PROJECT": project.name,
        "MLP_PIPELINE": definition.name,
        "MLP_PIPELINE_VERSION": str(definition.version),
        "MLP_PIPELINE_RUN_ID": str(run.id),
        "MLFLOW_EXPERIMENT_NAME": tracking_experiment_name(project),
    }
    if tracking_uri:
        base["MLFLOW_TRACKING_URI"] = tracking_uri
    if run.commit_sha:
        base["MLP_COMMIT_SHA"] = run.commit_sha

    steps = []
    for name in definition.execution_order:
        spec = next(s for s in definition.steps if s.name == name)
        job = jobs[spec.job]
        steps.append(
            StepSpec(
                name=name,
                image=job.image,
                command=job.command,
                env={
                    **job.env,
                    **base,
                    "MLP_STEP": name,
                    "MLP_IMAGE": job.image,
                    "MLP_PARAMETERS": encode(project_values(job.parameter_schema, run.parameters)),
                },
                resources=dict(job.resources),
                secret_refs=job.secret_refs,
                depends_on=spec.depends_on,
            )
        )
    return WorkflowSpec(
        name=pipeline_workflow_name(run),
        namespace=project.namespace,
        timeout_seconds=run.timeout_seconds,
        image_pull_secrets=tuple(
            sorted({name for job in jobs.values() for name in job.secret_refs.image_pull_secrets})
        ),
        steps=tuple(steps),
        labels={
            LABEL_MANAGED_BY: MANAGED_BY,
            LABEL_PROJECT: project.name,
            LABEL_PIPELINE_RUN_ID: str(run.id),
        },
    )
