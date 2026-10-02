"""Compiles a platform Run into a provider-neutral WorkflowSpec. Pure."""

from __future__ import annotations

from controlplane.application.namespaces import LABEL_MANAGED_BY, LABEL_PROJECT, MANAGED_BY
from controlplane.application.providers import StepSpec, WorkflowSpec
from controlplane.domain.entities import JobDefinition, Project, Run

LABEL_RUN_ID = "mlp.io/run-id"
LABEL_JOB = "mlp.io/job"
MAIN_STEP = "main"


def workflow_name(run: Run) -> str:
    """Deterministic, DNS-safe, unique per run (so a re-submit finds the same workflow)."""
    return f"run-{run.id.hex[:16]}"


def compile_job_run(project: Project, job: JobDefinition, run: Run) -> WorkflowSpec:
    return WorkflowSpec(
        name=workflow_name(run),
        namespace=project.namespace,
        steps=(
            StepSpec(
                name=MAIN_STEP,
                image=job.image,
                command=job.command,
                env=dict(job.env),
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
