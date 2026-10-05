from collections.abc import Callable
from dataclasses import replace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from controlplane.adapters.fakes import FakeClusterProvider, FakeExperimentProvider
from controlplane.application.jobs import CreateJob, JobService
from controlplane.application.models import ModelService
from controlplane.application.pipeline_runs import PipelineRunService
from controlplane.application.pipelines import CreatePipeline, PipelineService, StepInput
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import CreateProject, ProjectService
from controlplane.application.providers import ExperimentRun, RegisteredVersion
from controlplane.application.workflow_compiler import TAG_PIPELINE_RUN_ID
from controlplane.domain.states import RunStatus
from controlplane.reconciliation.model_discovery import ModelDiscoveryReconciler
from controlplane.reconciliation.projects import ProjectReconciler


def test_scoped_discovery_retries_and_survives_reconciler_restart(
    uow_factory: Callable[[], UnitOfWork], clock: Any
) -> None:
    project, _ = ProjectService(uow_factory, clock).create(CreateProject("discovery-team"))
    ProjectReconciler(uow_factory, FakeClusterProvider(), clock).reconcile(project.id)
    JobService(uow_factory, clock).create(project.name, CreateJob("train-job", "img:1"))
    PipelineService(uow_factory, clock).create(
        project.name, CreatePipeline("train", [StepInput("train", "train-job")])
    )
    run = PipelineRunService(uow_factory, clock).create(project.name, "train")[0].run
    with uow_factory() as uow:
        uow.pipeline_runs.update(
            replace(run, status=RunStatus.SUCCEEDED, finished_at=clock()),
            expected_status=run.status,
        )
        uow.commit()
    provider = FakeExperimentProvider()
    models = ModelService(uow_factory, clock, provider)
    models.create(project.name, "scorer", {})
    registry = "discovery-team-scorer"
    discovery = ModelDiscoveryReconciler(uow_factory, provider, clock, delay_seconds=0)
    assert discovery.reconcile_all() == [0]  # registry has not published output yet
    with uow_factory() as uow:
        pending = uow.pipeline_runs.get(run.id)
        assert pending is not None and pending.models_discovered_at is None
        assert pending.model_discovery_checked_at is not None
    provider.registered[registry] = [
        RegisteredVersion("1", "tracking-one"),
        RegisteredVersion("2", "other-pipeline"),
    ]
    provider.runs["tracking-one"] = ExperimentRun(
        ref="tracking-one", metrics={}, tags={TAG_PIPELINE_RUN_ID: str(run.id)}
    )
    provider.runs["other-pipeline"] = ExperimentRun(
        ref="other-pipeline", metrics={}, tags={TAG_PIPELINE_RUN_ID: str(uuid4())}
    )
    with (
        patch.object(provider, "list_model_versions", side_effect=RuntimeError("registry down")),
        pytest.raises(RuntimeError),
    ):
        discovery.reconcile(run.id)
    with uow_factory() as uow:
        pending = uow.pipeline_runs.get(run.id)
        assert pending is not None and pending.status is RunStatus.SUCCEEDED
        assert pending.models_discovered_at is None
    assert discovery.reconcile_all() == [1]
    versions = models.get(project.name, "scorer").versions
    assert len(versions) == 1 and versions[0].source_pipeline_run_id == run.id
    assert (
        ModelDiscoveryReconciler(uow_factory, provider, clock, delay_seconds=0).reconcile_all()
        == []
    )
    with uow_factory() as uow:
        completed = uow.pipeline_runs.get(run.id)
        assert completed is not None and completed.models_discovered_at is not None

    # A retention update loaded before discovery must preserve the independent checkpoint.
    with uow_factory() as uow:
        uow.pipeline_runs.update(
            replace(pending, workflow_cleaned_at=clock()),
            expected_status=RunStatus.SUCCEEDED,
        )
        uow.commit()
    with uow_factory() as uow:
        retained = uow.pipeline_runs.get(run.id)
        assert retained is not None and retained.workflow_cleaned_at is not None
        assert retained.models_discovered_at == completed.models_discovered_at
