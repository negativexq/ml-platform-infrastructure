from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from controlplane.api.app import create_app
from controlplane.application.schedule_calendar import next_occurrence
from controlplane.application.schedules import ScheduleDispatcher, ScheduleService
from controlplane.domain.errors import Conflict
from controlplane.domain.schedules import (
    ConcurrencyPolicy,
    ConcurrencyScope,
    ExecutionStatus,
    MissedRunPolicy,
    TargetKind,
    VersionPolicy,
)
from controlplane.domain.states import RunStatus
from controlplane.persistence.models import ScheduleExecutionRow
from controlplane.tests.test_pipelines import Env, step


class ScheduleEnv:
    def __init__(self, factory):
        self.factory = factory
        self.now = datetime(2026, 1, 1, tzinfo=UTC)
        self.env = Env(factory, lambda: self.now)
        self.definition, _ = self.env.pipeline("daily-model", step("train"))
        self.service = ScheduleService(factory, lambda: self.now)
        self.dispatcher = ScheduleDispatcher(factory, lambda: self.now)

    def create(self, name="daily-score", **options):
        return self.service.create(
            "credit-risk",
            name=name,
            target_kind=TargetKind.PIPELINE,
            target_name="daily-model",
            cron="* * * * *",
            timezone="UTC",
            version=1,
            **options,
        )

    def finish(self, execution):
        with self.factory() as uow:
            run = uow.pipeline_runs.get(execution.pipeline_run_id)
            uow.pipeline_runs.update(
                replace(run, status=RunStatus.SUCCEEDED), expected_status=run.status
            )
            uow.commit()


@pytest.fixture
def env(uow_factory):
    return ScheduleEnv(uow_factory)


def test_occurrence_replay_keeps_one_execution_one_run(env):
    schedule = env.create()
    env.now += timedelta(minutes=1)
    assert env.dispatcher.tick() == {"DISPATCHED": 1}
    assert env.dispatcher.tick() == {}
    history = env.service.history(schedule.id)
    assert len(history) == 1
    with env.factory() as uow:
        assert uow.pipeline_runs.count(env.env.project_id) == 1
        assert uow.schedules.for_run(history[0].pipeline_run_id, TargetKind.PIPELINE) == history[0]
        events = uow.audit.list(project_id=env.env.project_id)
        assert any(
            e.actor == "schedule-dispatcher" and e.action == "pipeline_run.created" for e in events
        )


def test_target_forbid_coordinates_distinct_schedules_and_pending_runs(env):
    first, second = env.create(), env.create("second-score")
    env.now += timedelta(minutes=1)
    assert env.dispatcher.tick() == {"DISPATCHED": 1, "SKIPPED": 1}
    outcomes = [env.service.history(s.id)[0] for s in [first, second]]
    assert sorted(e.status for e in outcomes) == [
        ExecutionStatus.DISPATCHED,
        ExecutionStatus.SKIPPED,
    ]
    assert next(e for e in outcomes if e.status == ExecutionStatus.SKIPPED).pipeline_run_id is None


def test_schedule_scope_allows_different_schedules_and_manual_runs_are_excluded(env):
    env.env.runs.create("credit-risk", "daily-model")
    env.create(concurrency_scope=ConcurrencyScope.SCHEDULE)
    env.create("second-score", concurrency_scope=ConcurrencyScope.SCHEDULE)
    env.now += timedelta(minutes=1)
    assert env.dispatcher.tick() == {"DISPATCHED": 2}


def test_queue_has_no_run_until_capacity_opens_and_freezes_latest(env):
    schedule = env.create(concurrency_policy=ConcurrencyPolicy.QUEUE)
    env.now += timedelta(minutes=1)
    env.dispatcher.tick()
    first = env.service.history(schedule.id)[0]
    schedule = env.service.update(
        schedule.id, schedule.revision, {"version_policy": VersionPolicy.LATEST, "version": None}
    )
    env.now += timedelta(minutes=1)
    assert env.dispatcher.tick() == {"QUEUED": 1}
    queued = env.service.history(schedule.id)[0]
    assert queued.pipeline_run_id is None and queued.resolved_definition_id == env.definition.id
    newer, _ = env.env.pipeline("daily-model", step("train"), step("verify", "train"))
    assert newer.version == 2
    env.finish(first)
    assert env.dispatcher.tick() == {"DISPATCHED": 1}
    queued = env.service.history(schedule.id)[0]
    with env.factory() as uow:
        assert (
            uow.pipeline_runs.get(queued.pipeline_run_id).pipeline_definition_id
            == env.definition.id
        )
        assert uow.pipeline_runs.count(env.env.project_id) == 2


def test_pause_holds_queue_and_does_not_cancel_started_run(env):
    schedule = env.create(concurrency_policy=ConcurrencyPolicy.QUEUE)
    env.now += timedelta(minutes=1)
    env.dispatcher.tick()
    first = env.service.history(schedule.id)[0]
    env.now += timedelta(minutes=1)
    env.dispatcher.tick()
    schedule = env.service.update(schedule.id, 1, {"paused": True})
    env.finish(first)
    env.now += timedelta(minutes=1)
    assert env.dispatcher.tick() == {}
    assert env.service.history(schedule.id)[0].status == ExecutionStatus.QUEUED
    env.service.update(schedule.id, schedule.revision, {"paused": False})
    env.dispatcher.tick()
    assert env.service.history(schedule.id)[1].status == ExecutionStatus.DISPATCHED


def test_queue_overflow_expiry_and_config_revision(env):
    schedule = env.create(
        concurrency_policy=ConcurrencyPolicy.QUEUE, max_queue_size=1, queue_ttl_seconds=90
    )
    env.now += timedelta(minutes=1)
    env.dispatcher.tick()
    env.now += timedelta(minutes=1)
    env.dispatcher.tick()
    env.now += timedelta(minutes=1)
    env.dispatcher.tick()
    assert env.service.history(schedule.id)[0].reason == "queue capacity exceeded"
    env.now += timedelta(seconds=31)
    env.dispatcher.tick()
    assert env.service.history(schedule.id)[1].status == ExecutionStatus.MISSED
    updated = env.service.update(
        schedule.id, 1, {"cron": "0 3 * * *", "timezone": "Europe/Istanbul"}
    )
    assert updated.revision == 2
    with pytest.raises(Conflict):
        env.service.update(schedule.id, 1, {"paused": True})


@pytest.mark.parametrize(
    "policy, dispatched", [(MissedRunPolicy.SKIP, 1), (MissedRunPolicy.CATCH_UP, 5)]
)
def test_half_hour_outage_is_bounded_and_obeys_missed_policy(env, policy, dispatched):
    schedule = env.create(
        missed_run_policy=policy, deadline_seconds=299, concurrency_policy=ConcurrencyPolicy.ALLOW
    )
    env.now += timedelta(minutes=30)
    env.dispatcher.tick(occurrence_limit=40)
    history = env.service.history(schedule.id)
    assert len(history) == 30
    assert sum(e.status == ExecutionStatus.DISPATCHED for e in history) == dispatched
    assert sum(e.status == ExecutionStatus.MISSED for e in history) == 30 - dispatched


def test_transaction_crash_after_run_creation_rolls_back_every_record(env):
    schedule = env.create()
    env.now += timedelta(minutes=1)
    with patch.object(env.dispatcher, "_dispatch", wraps=env.dispatcher._dispatch) as dispatch:
        original = dispatch._mock_wraps

        def crash(*args):
            original(*args)
            raise RuntimeError("process died before commit")

        dispatch.side_effect = crash
        with pytest.raises(RuntimeError):
            env.dispatcher.tick()
    assert env.service.history(schedule.id) == []
    assert env.service.get(schedule.id).next_run_at == env.now
    with env.factory() as uow:
        assert uow.pipeline_runs.count(env.env.project_id) == 0
    assert env.dispatcher.tick() == {"DISPATCHED": 1}
    assert env.dispatcher.tick() == {}


def test_job_scheduling_and_http_crud_preview_history(env):
    client = TestClient(create_app(env.factory, lambda: env.now))
    response = client.post(
        "/projects/credit-risk/schedules",
        json={
            "name": "job-score",
            "target_kind": "JOB",
            "target_name": "job-one",
            "cron": "* * * * *",
        },
    )
    assert response.status_code == 201, response.text
    id = response.json()["id"]
    env.now += timedelta(minutes=1)
    env.dispatcher.tick()
    execution = client.get(f"/schedules/{id}/executions").json()["items"][0]
    assert execution["job_run_id"] and not execution["pipeline_run_id"]
    assert client.get(f"/runs/{execution['job_run_id']}/schedule").json()["schedule_id"] == id
    assert client.get("/schedules").json()["items"][0]["last_execution"]["run_status"] == "PENDING"
    assert (
        client.patch(f"/schedules/{id}", json={"expected_revision": 1, "paused": True}).status_code
        == 200
    )
    assert (
        client.patch(f"/schedules/{id}", json={"expected_revision": 1, "paused": False}).status_code
        == 409
    )
    assert (
        client.post("/schedule-preview", json={"cron": "bad", "timezone": "UTC"}).status_code == 422
    )


@pytest.mark.parametrize(
    "zone,cron,start,expected",
    [
        ("Europe/Istanbul", "0 3 * * *", "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
        (
            "America/New_York",
            "30 2 * * *",
            "2026-03-08T05:00:00+00:00",
            "2026-03-09T06:30:00+00:00",
        ),
        (
            "America/New_York",
            "30 1 * * *",
            "2026-11-01T04:00:00+00:00",
            "2026-11-01T05:30:00+00:00",
        ),
        (
            "America/New_York",
            "30 1 * * *",
            "2026-11-01T05:30:00+00:00",
            "2026-11-02T06:30:00+00:00",
        ),
        (
            "America/New_York",
            "30 1 * * *",
            "2026-11-01T06:15:00+00:00",
            "2026-11-02T06:30:00+00:00",
        ),
    ],
)
def test_timezone_and_dst_once(zone, cron, start, expected):
    assert next_occurrence(cron, zone, datetime.fromisoformat(start)) == datetime.fromisoformat(
        expected
    )


def test_real_postgres_two_dispatchers_coordinate_same_target(pg_engine):
    from controlplane.persistence.migrate import upgrade
    from controlplane.persistence.sql import SqlUnitOfWork, sql_uow_factory

    upgrade(pg_engine)
    sessions = sql_uow_factory(pg_engine)

    def factory():
        return SqlUnitOfWork(sessions)

    env = ScheduleEnv(factory)
    env.create()
    env.create("second-score")
    env.now += timedelta(minutes=1)
    barrier = Barrier(2)

    def dispatch():
        barrier.wait(timeout=10)
        return ScheduleDispatcher(factory, lambda: env.now).tick()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(dispatch) for _ in range(2)]
        for future in futures:
            future.result(timeout=20)
    env.dispatcher.tick()  # a worker may skip a lock; the next pass repairs it
    with sessions() as session:
        executions = list(session.scalars(select(ScheduleExecutionRow)))
    assert len(executions) == 2
    assert sum(e.status == "DISPATCHED" for e in executions) == 1
    assert sum(e.status == "SKIPPED" for e in executions) == 1


@pytest.mark.parametrize("expression", ["R * * * *", "H * * * *", "0 3 L * *", "0 3 * * MON#2"])
def test_randomized_and_extended_cron_are_rejected(expression):
    from controlplane.domain.errors import InvalidArgument

    with pytest.raises(InvalidArgument):
        next_occurrence(expression, "UTC", datetime(2026, 1, 1, tzinfo=UTC))


def test_observed_transaction_wrapper_dispatches_normally(env):
    from controlplane.observability.uow import observed_uow_factory

    schedule = env.create()
    env.now += timedelta(minutes=1)
    wrapped = observed_uow_factory(env.factory)
    assert ScheduleDispatcher(wrapped, lambda: env.now).tick() == {"DISPATCHED": 1}
    assert len(env.service.history(schedule.id)) == 1


def test_queue_heads_do_not_starve_other_schedules(env):
    first = env.create(concurrency_policy=ConcurrencyPolicy.QUEUE)
    second = env.create("other-score", concurrency_policy=ConcurrencyPolicy.QUEUE)
    env.now += timedelta(minutes=1)
    env.dispatcher.tick()
    for _ in range(4):
        env.now += timedelta(minutes=1)
        env.dispatcher.tick()
    with env.factory() as uow:
        heads = uow.schedules.queued(2)
    assert {item.schedule_id for item in heads} == {first.id, second.id}
    assert all(item.pipeline_run_id is None for item in heads)
