"""M15 skeleton tests: job definitions, runs, compile, reconcile. Real-Argo gates
are listed in docs/local-verification.md."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from controlplane.adapters.fakes import FakeClusterProvider, FakeWorkflowProvider
from controlplane.adapters.workflow.argo import build_workflow
from controlplane.api.app import create_app
from controlplane.application.jobs import CreateJob, JobService
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import CreateProject, ProjectService
from controlplane.application.providers import ExternalState, StepSpec, WorkflowSpec
from controlplane.application.runs import RunService
from controlplane.domain.errors import Conflict, InvalidArgument, NotFound
from controlplane.domain.states import RunStatus
from controlplane.reconciliation.projects import ProjectReconciler
from controlplane.reconciliation.runs import RunReconciler

Factory = Callable[[], UnitOfWork]
JOB = CreateJob(
    name="train-credit-risk",
    image="train-credit-risk:sha-a83d2",
    command=("python", "train.py"),
    resources={"cpu": "2", "memory": "4Gi"},
    env={"MODEL_NAME": "credit-risk"},
)


class Env:
    def __init__(self, factory: Factory, clock: Callable[[], Any]) -> None:
        self.workflow = FakeWorkflowProvider()
        self.jobs = JobService(factory, clock)
        self.runs = RunService(factory, clock)
        self.reconciler = RunReconciler(factory, self.workflow, clock)
        self.factory = factory
        ProjectService(factory, clock).create(CreateProject(name="credit-risk"))
        project = ProjectService(factory, clock).list()[0]
        ProjectReconciler(factory, FakeClusterProvider(), clock).reconcile(project.id)

    def audit(self, run_id: Any) -> list[str]:
        with self.factory() as uow:
            return [e.action for e in uow.audit.list(entity_id=run_id)]


@pytest.fixture
def env(uow_factory: Factory, clock: Callable[[], Any]) -> Env:
    e = Env(uow_factory, clock)
    e.jobs.create("credit-risk", JOB)
    return e


def test_job_definition_validation_and_idempotency(env: Env) -> None:
    again, created = env.jobs.create("credit-risk", JOB)
    assert not created and again.name == JOB.name
    with pytest.raises(Conflict):  # definitions are immutable
        env.jobs.create("credit-risk", CreateJob(name=JOB.name, image="other:1"))
    for bad in (
        CreateJob(name="Bad Name", image="x:1"),
        CreateJob(name="ok-name", image="has space"),
        CreateJob(name="ok-name", image="x:1", resources={"cpu": "lots"}),
        CreateJob(name="ok-name", image="x:1", resources={"disk": "1"}),
        CreateJob(name="ok-name", image="x:1", env={"1BAD": "v"}),
    ):
        with pytest.raises(InvalidArgument):
            env.jobs.create("credit-risk", bad)


def test_run_happy_path_records_everything(env: Env) -> None:
    run, created = env.runs.create("credit-risk", JOB.name)
    assert created and run.status is RunStatus.PENDING

    assert env.reconciler.reconcile(run.id).after is RunStatus.SUBMITTED
    submitted = env.runs.get(run.id)
    assert submitted.external_ref is not None
    spec = env.workflow.submitted[submitted.external_ref]
    assert spec.namespace == "mlp-credit-risk" and spec.steps[0].image == JOB.image

    env.workflow.set_state(submitted.external_ref, ExternalState.RUNNING)
    assert env.reconciler.reconcile(run.id).after is RunStatus.RUNNING
    env.workflow.set_state(submitted.external_ref, ExternalState.SUCCEEDED, exit_code=0)
    assert env.reconciler.reconcile(run.id).after is RunStatus.SUCCEEDED

    done = env.runs.get(run.id)
    assert done.exit_code == 0 and done.duration_seconds is not None and done.duration_seconds > 0
    assert env.audit(run.id) == ["run.created", "run.submitted", "run.running", "run.succeeded"]


def test_fast_job_passes_through_running(env: Env) -> None:
    run, _ = env.runs.create("credit-risk", JOB.name)
    env.reconciler.reconcile(run.id)
    ref = env.runs.get(run.id).external_ref
    assert ref
    env.workflow.set_state(ref, ExternalState.SUCCEEDED, exit_code=0)
    assert env.reconciler.reconcile(run.id).after is RunStatus.SUCCEEDED
    assert "run.running" in env.audit(run.id)


def test_failed_workload_keeps_reason_and_exit_code(env: Env) -> None:
    run, _ = env.runs.create("credit-risk", JOB.name)
    env.reconciler.reconcile(run.id)
    ref = env.runs.get(run.id).external_ref
    assert ref
    env.workflow.set_state(ref, ExternalState.FAILED, reason="ImagePullBackOff", exit_code=None)
    env.reconciler.reconcile(run.id)
    failed = env.runs.get(run.id)
    assert failed.status is RunStatus.FAILED and failed.status_reason == "ImagePullBackOff"
    env.workflow.set_state(ref, ExternalState.FAILED, reason="OOMKilled", exit_code=137)
    assert env.reconciler.reconcile(run.id).after is RunStatus.FAILED  # terminal: untouched
    assert env.runs.get(run.id).status_reason == "ImagePullBackOff"


def test_runs_can_be_listed_by_status(env: Env) -> None:
    """Server-side, so "show me every failed run" is not limited to the page in view."""
    failed, _ = env.runs.create("credit-risk", JOB.name, idempotency_key="a")
    pending, _ = env.runs.create("credit-risk", JOB.name, idempotency_key="b")
    env.reconciler.reconcile(failed.id)
    ref = env.runs.get(failed.id).external_ref
    assert ref
    env.workflow.set_state(ref, ExternalState.FAILED, reason="boom", exit_code=1)
    env.reconciler.reconcile(failed.id)

    only_failed = env.runs.list("credit-risk", statuses={RunStatus.FAILED})
    assert [r.id for r in only_failed] == [failed.id]
    both = env.runs.list("credit-risk", statuses={RunStatus.FAILED, RunStatus.PENDING})
    assert {r.id for r in both} == {failed.id, pending.id}
    assert len(env.runs.list("credit-risk", statuses=None)) == 2


def test_cancel_before_and_after_submission(env: Env) -> None:
    early, _ = env.runs.create("credit-risk", JOB.name)
    assert env.runs.request_cancel(early.id).status is RunStatus.CANCELLED
    assert not env.workflow.submitted  # never became a workload

    late, _ = env.runs.create("credit-risk", JOB.name)
    env.reconciler.reconcile(late.id)
    assert env.runs.request_cancel(late.id).cancel_requested
    assert env.reconciler.reconcile(late.id).after is RunStatus.CANCELLED
    assert env.runs.request_cancel(late.id).status is RunStatus.CANCELLED  # idempotent


def test_same_idempotency_key_never_creates_a_second_run_or_workload(env: Env) -> None:
    first, c1 = env.runs.create("credit-risk", JOB.name, idempotency_key="k1")
    second, c2 = env.runs.create("credit-risk", JOB.name, idempotency_key="k1")
    assert (c1, c2) == (True, False) and first.id == second.id
    env.reconciler.reconcile(first.id)
    env.reconciler.reconcile(first.id)
    assert len(env.workflow.submitted) == 1


def test_resubmitting_after_a_crash_reuses_the_workload(env: Env) -> None:
    run, _ = env.runs.create("credit-risk", JOB.name)
    spec_ref = env.workflow.submit(
        WorkflowSpec(name="x", namespace="mlp-credit-risk", steps=()), str(run.id)
    )  # workload submitted, but the DB write never happened
    env.reconciler.reconcile(run.id)
    assert env.runs.get(run.id).external_ref == spec_ref
    assert len(env.workflow.submitted) == 1


def test_retry_creates_a_new_run_and_leaves_history_alone(env: Env) -> None:
    run, _ = env.runs.create("credit-risk", JOB.name)
    with pytest.raises(Conflict):
        env.runs.retry(run.id)  # still running
    env.reconciler.reconcile(run.id)
    ref = env.runs.get(run.id).external_ref
    assert ref
    env.workflow.set_state(ref, ExternalState.FAILED, reason="boom", exit_code=1)
    env.reconciler.reconcile(run.id)
    before = env.runs.get(run.id)

    retry, created = env.runs.retry(run.id)
    assert created and retry.id != run.id and retry.retry_of == run.id
    assert retry.status is RunStatus.PENDING
    assert env.runs.get(run.id) == before


def test_runs_need_a_ready_project_and_a_known_job(
    uow_factory: Factory, clock: Callable[[], Any]
) -> None:
    ProjectService(uow_factory, clock).create(CreateProject(name="not-ready"))
    JobService(uow_factory, clock).create("not-ready", JOB)
    runs = RunService(uow_factory, clock)
    with pytest.raises(Conflict, match="READY"):
        runs.create("not-ready", JOB.name)
    with pytest.raises(NotFound):
        runs.create("not-ready", "no-such-job")


def test_disappeared_workload_fails_the_run(env: Env) -> None:
    run, _ = env.runs.create("credit-risk", JOB.name)
    env.reconciler.reconcile(run.id)
    env.workflow._status.clear()  # the workflow vanished from the cluster
    assert env.reconciler.reconcile(run.id).after is RunStatus.FAILED


def test_api_flow_hides_workflow_internals(
    uow_factory: Factory, clock: Callable[[], Any], env: Env
) -> None:
    client = TestClient(create_app(uow_factory, clock, workflow=env.workflow))
    body = {"name": "second-job", "image": "img:1", "command": ["echo", "hi"]}
    assert client.post("/projects/credit-risk/jobs", json=body).status_code == 201
    assert client.post("/projects/credit-risk/jobs", json=body).status_code == 200

    started = client.post(
        "/projects/credit-risk/jobs/second-job/runs", headers={"Idempotency-Key": "abc"}
    )
    assert started.status_code == 202
    replay = client.post(
        "/projects/credit-risk/jobs/second-job/runs", headers={"Idempotency-Key": "abc"}
    )
    assert replay.status_code == 200 and replay.json()["id"] == started.json()["id"]

    run_id = started.json()["id"]
    env.reconciler.reconcile(UUID(run_id))
    got = client.get(f"/runs/{run_id}").json()
    assert got["status"] == "SUBMITTED"
    assert not {"external_ref", "workflow", "pod", "namespace"} & set(got)

    ref = env.runs.get(UUID(run_id)).external_ref
    assert ref
    env.workflow.set_logs(ref, "main", "hello\n")
    assert client.get(f"/runs/{run_id}/logs").text == "hello\n"
    assert client.post(f"/runs/{run_id}/cancel").status_code == 202
    assert client.get(f"/runs/{uuid4()}").status_code == 404
    assert len(client.get("/projects/credit-risk/runs").json()["items"]) == 1


def test_argo_manifest_single_step_and_dag() -> None:
    one = build_workflow(
        WorkflowSpec(
            name="run-1",
            namespace="mlp-a",
            labels={"k": "v"},
            steps=(
                StepSpec(
                    name="main",
                    image="img:1",
                    command=("python", "t.py"),
                    env={"A": "1"},
                    resources={"cpu": "2"},
                ),
            ),
        )
    )
    assert one["spec"]["entrypoint"] == "main"
    container = one["spec"]["templates"][0]["container"]
    assert container["command"] == ["python", "t.py"]
    assert container["resources"] == {"requests": {"cpu": "2"}, "limits": {"cpu": "2"}}

    dag = build_workflow(
        WorkflowSpec(
            name="run-2",
            namespace="mlp-a",
            steps=(StepSpec("a", "i"), StepSpec("b", "i", depends_on=("a",))),
        )
    )
    tasks = dag["spec"]["templates"][-1]["dag"]["tasks"]
    assert [(t["name"], t["dependencies"]) for t in tasks] == [("a", []), ("b", ["a"])]
