"""Parameter contracts through persistence, retries, scheduling and compilation."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from controlplane.application.jobs import CreateJob, JobService
from controlplane.application.pipelines import CreatePipeline, StepInput
from controlplane.application.runs import RunService
from controlplane.application.schedules import ScheduleDispatcher, ScheduleService
from controlplane.application.workflow_compiler import compile_job_run, compile_pipeline_run
from controlplane.domain.errors import Conflict, InvalidArgument
from controlplane.domain.parameters import resolve, validate_schema
from controlplane.domain.schedules import ConcurrencyPolicy, TargetKind, VersionPolicy
from controlplane.domain.states import RunStatus
from controlplane.tests.test_pipelines import Env

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "processing_date": {"type": "string", "format": "date"},
        "batch_size": {"type": "integer", "minimum": 1, "default": 100},
    },
    "required": ["processing_date"],
}


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"processing_date": "wrong"},
        {"processing_date": "2026-01-01", "batch_size": True},
        {"processing_date": "2026-01-01", "extra": "secret-value"},
    ],
)
def test_invalid_values_are_rejected_without_echo(values):
    with pytest.raises(InvalidArgument) as error:
        resolve(SCHEMA, values)
    assert "secret-value" not in str(error.value)


def test_defaults_copy_and_reference_size_guards():
    assert resolve(SCHEMA, {"processing_date": "2026-01-01"})["batch_size"] == 100
    for schema in [
        {**SCHEMA, "$ref": "https://example.com/schema"},
        {**SCHEMA, "properties": {"x": {"type": "integer", "default": "bad"}}},
    ]:
        with pytest.raises(InvalidArgument):
            validate_schema(schema)
    with pytest.raises(InvalidArgument):
        resolve(SCHEMA, {"processing_date": "x" * 65536})
    for fmt, value in [
        ("date-time", "not-a-date"),
        ("uri", "relative/path"),
        ("uri", "https://user:password@example.com"),
    ]:
        with pytest.raises(InvalidArgument):
            resolve(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"x": {"type": "string", "format": fmt}},
                },
                {"x": value},
            )


def setup(factory):
    now = datetime(2026, 1, 1, 20, 59, tzinfo=UTC)
    env = Env(factory, lambda: now)
    job, _ = JobService(factory).create(
        "credit-risk",
        CreateJob(
            name="parameter-job",
            image="test:1",
            command=("echo", "literal"),
            env={"MLP_PARAMETERS": "spoof"},
            parameter_schema=SCHEMA,
        ),
    )
    pipeline, _ = env.pipelines.create(
        "credit-risk",
        CreatePipeline(
            name="parameter-pipeline", steps=[StepInput("train", job.name)], parameter_schema=SCHEMA
        ),
    )
    return env, job, pipeline, now


def test_job_snapshot_idempotency_retry_and_literal_container_env(uow_factory):
    env, job, _, _ = setup(uow_factory)
    service = RunService(uow_factory)
    values = {"processing_date": "2026-01-01"}
    run, _ = service.create("credit-risk", job.name, idempotency_key="same", parameters=values)
    values["processing_date"] = "2026-01-02"
    assert service.get(run.id).parameters == {"processing_date": "2026-01-01", "batch_size": 100}
    assert not service.create(
        "credit-risk",
        job.name,
        idempotency_key="same",
        parameters={"processing_date": "2026-01-01", "batch_size": 100},
    )[1]
    with pytest.raises(Conflict, match="parameters"):
        service.create("credit-risk", job.name, idempotency_key="same", parameters=values)
    with uow_factory() as uow:
        project = uow.projects.get(env.project_id)
        uow.runs.update(replace(run, status=RunStatus.FAILED), expected_status=run.status)
        uow.commit()
    retry, _ = service.retry(run.id)
    assert retry.parameters == run.parameters
    compiled = compile_job_run(project, job, retry).steps[0]
    assert compiled.command == job.command
    assert json.loads(compiled.env["MLP_PARAMETERS"]) == run.parameters


def test_pipeline_parameter_projection_and_idempotency(uow_factory):
    env, job, definition, _ = setup(uow_factory)
    run, _ = env.runs.create(
        "credit-risk",
        definition.name,
        parameters={"processing_date": "2026-01-01"},
        idempotency_key="pipeline",
    )
    assert env.runs.view(run.run.id).run.parameters["batch_size"] == 100
    with pytest.raises(Conflict, match="parameters"):
        env.runs.create(
            "credit-risk",
            definition.name,
            parameters={"processing_date": "2026-01-02"},
            idempotency_key="pipeline",
        )
    with uow_factory() as uow:
        project = uow.projects.get(env.project_id)
    spec = compile_pipeline_run(project, definition, {job.name: job}, run.run)
    assert json.loads(spec.steps[0].env["MLP_PARAMETERS"]) == run.run.parameters


def test_queued_schedule_freezes_local_date_and_defaults(uow_factory):
    env, _, definition, start = setup(uow_factory)
    now = [start]
    service = ScheduleService(uow_factory, lambda: now[0])
    dispatcher = ScheduleDispatcher(uow_factory, lambda: now[0])
    schedule = service.create(
        "credit-risk",
        name="parameter-schedule",
        target_kind=TargetKind.PIPELINE,
        target_name=definition.name,
        version=1,
        cron="* * * * *",
        timezone="Europe/Istanbul",
        concurrency_policy=ConcurrencyPolicy.QUEUE,
        parameter_bindings={"processing_date": "processing_date"},
    )
    now[0] += timedelta(minutes=1)
    dispatcher.tick()
    first = service.history(schedule.id)[0]
    now[0] += timedelta(minutes=1)
    dispatcher.tick()
    queued = service.history(schedule.id)[0]
    assert queued.pipeline_run_id is None
    assert queued.parameters == {"processing_date": "2026-01-02", "batch_size": 100}
    with uow_factory() as uow:
        original = uow.pipeline_runs.get(first.pipeline_run_id)
        uow.pipeline_runs.update(
            replace(original, status=RunStatus.SUCCEEDED), expected_status=original.status
        )
        uow.commit()
    service.update(schedule.id, schedule.revision, {"parameters": {"batch_size": 200}})
    now[0] += timedelta(hours=1)
    dispatcher.tick(occurrence_limit=1)
    result = next(x for x in service.history(schedule.id) if x.id == queued.id)
    assert env.runs.view(result.pipeline_run_id).run.parameters == queued.parameters


def test_api_validates_before_intent_and_reports_resolved_snapshot(uow_factory):
    from fastapi.testclient import TestClient

    from controlplane.api.app import create_app

    env, job, definition, _ = setup(uow_factory)
    client = TestClient(create_app(uow_factory))
    endpoint = f"/projects/credit-risk/jobs/{job.name}/runs"
    assert (
        client.post(endpoint, json={"parameters": {"processing_date": "invalid"}}).status_code
        == 422
    )
    with uow_factory() as uow:
        assert uow.runs.count(env.project_id) == 0
    response = client.post(endpoint, json={"parameters": {"processing_date": "2026-01-01"}})
    assert response.status_code == 202
    assert response.json()["parameters"]["batch_size"] == 100
    assert client.get(f"/projects/credit-risk/jobs/{job.name}").json()["parameter_schema"] == SCHEMA
    response = client.post(
        f"/projects/credit-risk/pipelines/{definition.name}/runs",
        json={"parameters": {"processing_date": "2026-01-01"}},
    )
    assert response.status_code == 202
    assert response.json()["parameters"]["batch_size"] == 100


def test_schedule_schema_change_records_missed_instead_of_blocking_dispatcher(uow_factory):
    env, _, definition, start = setup(uow_factory)
    now = [start]
    service = ScheduleService(uow_factory, lambda: now[0])
    schedule = service.create(
        "credit-risk",
        name="latest-parameters",
        target_kind=TargetKind.PIPELINE,
        target_name=definition.name,
        version_policy=VersionPolicy.LATEST,
        cron="* * * * *",
        timezone="UTC",
        parameter_bindings={"processing_date": "processing_date"},
    )
    env.pipelines.create(
        "credit-risk", CreatePipeline(name=definition.name, steps=[StepInput("train", "job-one")])
    )
    now[0] += timedelta(minutes=1)
    assert ScheduleDispatcher(uow_factory, lambda: now[0]).tick() == {"MISSED": 1}
    assert (
        service.history(schedule.id)[0].reason
        == "parameter validation failed for resolved definition"
    )


def test_schedule_audit_does_not_copy_parameter_values(uow_factory):
    env, _, definition, start = setup(uow_factory)
    service = ScheduleService(uow_factory, lambda: start)
    schedule = service.create(
        "credit-risk",
        name="audit-parameters",
        target_kind=TargetKind.PIPELINE,
        target_name=definition.name,
        version=1,
        cron="* * * * *",
        timezone="UTC",
        parameters={"processing_date": "2026-01-01"},
    )
    service.update(schedule.id, 1, {"parameters": {"processing_date": "2026-01-02"}})
    with uow_factory() as uow:
        events = [
            e for e in uow.audit.list(project_id=env.project_id) if e.entity_id == schedule.id
        ]
    assert "2026-01-01" not in json.dumps([e.payload for e in events])
    assert "2026-01-02" not in json.dumps([e.payload for e in events])
