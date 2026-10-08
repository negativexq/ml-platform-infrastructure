import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from controlplane.application.pipeline_runs import PipelineRunService
from controlplane.application.pipelines import CreatePipeline, PipelineService, StepInput
from controlplane.application.runs import RunService
from controlplane.application.schedules import ScheduleDispatcher, ScheduleService
from controlplane.application.workflow_compiler import compile_job_run, compile_pipeline_run
from controlplane.domain.errors import InvalidArgument, NotFound
from controlplane.domain.schedules import TargetKind
from controlplane.domain.states import RunStatus
from controlplane.tests.test_batch_inference import batch as batch_fixture
from controlplane.tests.test_data_catalog import env, spec  # noqa: F401

batch = batch_fixture


def publish(catalog, factory, fields, version, **changes):
    with factory() as uow:
        first = uow.data_catalog.dataset(fields["input_dataset_id"])
        connection = uow.data_catalog.connection(first.connection_id)
    return catalog.publish_dataset(
        "catalog-team",
        **spec(
            connection,
            uri=f"s3://datasets/training/v{version}.csv",
            checksum_sha256=str(version) * 64,
            expected_latest_version=version - 1,
            **changes,
        ),
    )[0]


def test_latest_is_frozen_at_creation_replay_and_retry_never_move(batch, uow_factory, clock):
    service, fields, catalog, _ = batch
    job, _ = service.create("catalog-team", **fields, input_selection_policy="LATEST_AT_EXECUTION")
    latest = publish(catalog, uow_factory, fields, 2)
    runs = RunService(uow_factory, clock)
    run, _ = runs.create("catalog-team", job.name, idempotency_key="daily")
    assert run.batch_snapshot["input_dataset_id"] == str(latest.id)
    newer = publish(catalog, uow_factory, fields, 3)
    assert runs.create("catalog-team", job.name, idempotency_key="daily") == (run, False)
    with uow_factory() as uow:
        project = uow.projects.get(job.project_id)
        uow.runs.update(replace(run, status=RunStatus.SUCCEEDED), expected_status=run.status)
        uow.commit()
    retry, _ = runs.retry(run.id)
    assert retry.batch_snapshot == run.batch_snapshot
    fresh, _ = runs.create("catalog-team", job.name)
    assert fresh.batch_snapshot["input_dataset_id"] == str(newer.id)
    compiled = compile_job_run(project, job, retry)
    assert json.loads(compiled.steps[0].env["MLP_BATCH_SPEC"])["input_dataset_id"] == str(latest.id)


def test_pinned_ignores_newer_dataset_versions(batch, uow_factory, clock):
    service, fields, catalog, _ = batch
    job, _ = service.create("catalog-team", **fields)
    publish(catalog, uow_factory, fields, 2)
    run, _ = RunService(uow_factory, clock).create("catalog-team", job.name)
    assert run.batch_snapshot["input_dataset_id"] == str(fields["input_dataset_id"])


def test_date_selection_requires_exact_registered_date_and_freezes_it(batch, uow_factory, clock):
    service, fields, catalog, _ = batch
    job, _ = service.create("catalog-team", **fields, input_selection_policy="BY_PROCESSING_DATE")
    dated = publish(catalog, uow_factory, fields, 2, processing_date=date(2026, 1, 2))
    publish(catalog, uow_factory, fields, 3, processing_date=date(2026, 1, 3))
    runs = RunService(uow_factory, clock)
    run, _ = runs.create("catalog-team", job.name, parameters={"processing_date": "2026-01-02"})
    assert run.batch_snapshot["input_dataset_id"] == str(dated.id)
    assert run.batch_snapshot["input"]["processing_date"] == "2026-01-02"
    with pytest.raises(NotFound):
        runs.create("catalog-team", job.name, parameters={"processing_date": "2026-01-04"})
    with pytest.raises(InvalidArgument):
        runs.create("catalog-team", job.name)
    with pytest.raises(InvalidArgument):
        runs.create("catalog-team", job.name, parameters={"processing_date": "2026-02-30"})


def test_pipeline_freezes_each_batch_step_in_run_transaction(batch, uow_factory, clock):
    service, fields, catalog, _ = batch
    job, _ = service.create("catalog-team", **fields, input_selection_policy="LATEST_AT_EXECUTION")
    definition, _ = PipelineService(uow_factory, clock).create(
        "catalog-team", CreatePipeline(name="score-pipeline", steps=[StepInput("score", job.name)])
    )
    selected = publish(catalog, uow_factory, fields, 2)
    view, _ = PipelineRunService(uow_factory, clock).create("catalog-team", definition.name)
    publish(catalog, uow_factory, fields, 3)
    with uow_factory() as uow:
        saved = uow.pipeline_runs.get(view.run.id)
        project = uow.projects.get(job.project_id)
    assert saved.batch_snapshots["score"]["input_dataset_id"] == str(selected.id)
    compiled = compile_pipeline_run(project, definition, {job.name: job}, saved)
    assert json.loads(compiled.steps[0].env["MLP_BATCH_SPEC"])["input_dataset_id"] == str(
        selected.id
    )


def test_schedule_date_uses_planned_local_day_and_missing_data_is_recorded(
    batch, uow_factory, clock
):
    service, fields, catalog, _ = batch
    job, _ = service.create("catalog-team", **fields, input_selection_policy="BY_PROCESSING_DATE")
    selected = publish(catalog, uow_factory, fields, 2, processing_date=date(2026, 1, 2))
    now = [datetime(2026, 1, 1, 23, 59, tzinfo=UTC)]
    schedules = ScheduleService(uow_factory, lambda: now[0])
    schedule = schedules.create(
        "catalog-team",
        name="daily",
        target_kind=TargetKind.JOB,
        target_name=job.name,
        cron="0 3 * * *",
        timezone="Europe/Istanbul",
        parameter_bindings={"processing_date": "processing_date"},
    )
    dispatcher = ScheduleDispatcher(uow_factory, lambda: now[0])
    now[0] += timedelta(minutes=1)
    assert dispatcher.tick() == {"DISPATCHED": 1}
    execution = schedules.history(schedule.id)[0]
    with uow_factory() as uow:
        run = uow.runs.get(execution.job_run_id)
        assert run.parameters == {"processing_date": "2026-01-02"}
        assert run.batch_snapshot["input_dataset_id"] == str(selected.id)
        uow.runs.update(replace(run, status=RunStatus.SUCCEEDED), expected_status=run.status)
        uow.commit()
    now[0] += timedelta(days=1)
    assert dispatcher.tick() == {"MISSED": 1}
    assert schedules.history(schedule.id)[0].job_run_id is None
