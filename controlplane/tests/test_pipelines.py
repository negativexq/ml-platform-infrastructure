"""M16 skeleton tests: DAG validation, versioning, pipeline runs, steps, tracking join."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from controlplane.adapters.fakes import (
    FakeClusterProvider,
    FakeExperimentProvider,
    FakeWorkflowProvider,
)
from controlplane.api.app import create_app
from controlplane.application.jobs import CreateJob, JobService
from controlplane.application.pipeline_runs import PipelineRunService
from controlplane.application.pipelines import CreatePipeline, PipelineService, StepInput
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import CreateProject, ProjectService
from controlplane.application.providers import ExperimentRun, ExternalState
from controlplane.application.workflow_compiler import (
    TAG_PIPELINE_RUN_ID,
    TAG_STEP,
    tracking_experiment_name,
)
from controlplane.domain.entities import StepSpec, validate_dag
from controlplane.domain.errors import Conflict, InvalidArgument, NotFound
from controlplane.domain.states import RunStatus, StepStatus
from controlplane.reconciliation.pipeline_runs import PipelineRunReconciler
from controlplane.reconciliation.projects import ProjectReconciler

Factory = Callable[[], UnitOfWork]
S = ExternalState


def step(name: str, *deps: str, job: str = "job-one") -> StepSpec:
    return StepSpec(name, job, tuple(deps))


# -- DAG rules (pure) --------------------------------------------------------


def test_valid_dag_gets_a_topological_order_with_parallel_branches() -> None:
    steps = (
        step("validate"),
        step("prepare", "validate"),
        step("train", "prepare"),
        step("evaluate", "train"),
    )
    assert validate_dag(steps) == ("validate", "prepare", "train", "evaluate")
    diamond = (step("a"), step("b", "a"), step("c", "a"), step("d", "b", "c"))
    order = validate_dag(diamond)
    assert order[0] == "a" and order[-1] == "d" and set(order[1:3]) == {"b", "c"}


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ((), "at least one"),
        ((step("a"), step("a")), "duplicate"),
        ((step("a", "ghost"),), "unknown step"),
        ((step("a", "a"),), "itself"),
        ((step("a", "b"), step("b", "a")), "cycle"),
        ((step("a", "c"), step("b", "a"), step("c", "b")), "cycle"),
        ((step("a"), step("b", "a", "a")), "twice"),
        ((step("Bad Name"),), "step name"),
    ],
)
def test_invalid_dags_are_rejected(steps: tuple[StepSpec, ...], message: str) -> None:
    with pytest.raises(InvalidArgument, match=message):
        validate_dag(steps)


# -- fixtures ----------------------------------------------------------------


class Env:
    def __init__(self, factory: Factory, clock: Callable[[], Any]) -> None:
        self.factory = factory
        self.workflow = FakeWorkflowProvider()
        self.experiments = FakeExperimentProvider()
        self.pipelines = PipelineService(factory, clock)
        self.runs = PipelineRunService(factory, clock, self.experiments)
        self.reconciler = PipelineRunReconciler(
            factory, self.workflow, self.experiments, "http://mlflow:5000", clock
        )
        projects = ProjectService(factory, clock)
        project, _ = projects.create(CreateProject(name="credit-risk"))
        ProjectReconciler(factory, FakeClusterProvider(), clock).reconcile(project.id)
        self.project_id = project.id
        for name in ("job-one", "job-two"):
            JobService(factory, clock).create(
                "credit-risk", CreateJob(name=name, image=f"{name}:1", env={"USER_VAR": "x"})
            )

    def pipeline(self, name: str, *steps: StepSpec) -> Any:
        return self.pipelines.create(
            "credit-risk",
            CreatePipeline(name, [StepInput(s.name, s.job, s.depends_on) for s in steps]),
        )

    def run(self, name: str = "flow", **kw: Any) -> UUID:
        view, _ = self.runs.create("credit-risk", name, **kw)
        return view.run.id

    def ref(self, run_id: UUID) -> str:
        ref = self.runs.view(run_id).run.external_ref
        assert ref
        return ref

    def step_statuses(self, run_id: UUID) -> dict[str, StepStatus]:
        return {s.step_name: s.status for s in self.runs.view(run_id).steps}


@pytest.fixture
def env(uow_factory: Factory, clock: Callable[[], Any]) -> Env:
    return Env(uow_factory, clock)


# -- definitions -------------------------------------------------------------


def test_pipeline_versions_are_immutable_and_idempotent(env: Env) -> None:
    v1, created = env.pipeline("flow", step("a"), step("b", "a"))
    assert (v1.version, created) == (1, True)
    again, created = env.pipeline("flow", step("a"), step("b", "a"))
    assert (again.id, created) == (v1.id, False)
    v2, created = env.pipeline("flow", step("a"), step("b", "a"), step("c", "b"))
    assert (v2.version, created) == (2, True)
    assert env.pipelines.get("credit-risk", "flow", 1) == v1  # old version untouched
    assert env.pipelines.get("credit-risk", "flow").version == 2
    assert [d.name for d in env.pipelines.list("credit-risk")] == ["flow"]


def test_pipeline_rejects_cycles_and_unknown_jobs(env: Env) -> None:
    with pytest.raises(InvalidArgument, match="cycle"):
        env.pipeline("flow", step("a", "b"), step("b", "a"))
    with pytest.raises(InvalidArgument, match="unknown job"):
        env.pipeline("flow", step("a", job="no-such-job"))
    with pytest.raises(NotFound):
        env.pipelines.get("credit-risk", "flow")


# -- runs --------------------------------------------------------------------


def test_every_step_run_exists_in_the_database_from_creation(env: Env) -> None:
    env.pipeline("flow", step("validate"), step("prepare", "validate"), step("train", "prepare"))
    run_id = env.run()
    assert env.step_statuses(run_id) == {
        "validate": StepStatus.PENDING,
        "prepare": StepStatus.PENDING,
        "train": StepStatus.PENDING,
    }


def test_pipeline_runs_in_order_and_records_every_step(env: Env) -> None:
    env.pipeline(
        "flow",
        step("validate"),
        step("prepare", "validate"),
        step("train", "prepare", job="job-two"),
        step("evaluate", "train"),
    )
    run_id = env.run(commit_sha="a83d2")
    assert env.reconciler.reconcile(run_id).after is RunStatus.SUBMITTED

    ref = env.ref(run_id)
    spec = env.workflow.submitted[ref]
    assert [(s.name, s.depends_on) for s in spec.steps] == [
        ("validate", ()),
        ("prepare", ("validate",)),
        ("train", ("prepare",)),
        ("evaluate", ("train",)),
    ]
    train = next(s for s in spec.steps if s.name == "train")
    assert train.image == "job-two:1"
    assert train.env["USER_VAR"] == "x"
    assert train.env["MLP_PIPELINE_RUN_ID"] == str(run_id)
    assert train.env["MLP_STEP"] == "train" and train.env["MLP_COMMIT_SHA"] == "a83d2"
    assert train.env["MLFLOW_EXPERIMENT_NAME"] == "mlp-credit-risk"
    assert train.env["MLFLOW_TRACKING_URI"] == "http://mlflow:5000"

    env.workflow.set_steps(
        ref, {"validate": S.SUCCEEDED, "prepare": S.RUNNING}, S.RUNNING, exit_codes={"validate": 0}
    )
    assert env.reconciler.reconcile(run_id).after is RunStatus.RUNNING
    assert env.step_statuses(run_id) == {
        "validate": StepStatus.SUCCEEDED,
        "prepare": StepStatus.RUNNING,
        "train": StepStatus.PENDING,
        "evaluate": StepStatus.PENDING,
    }
    done = {n: S.SUCCEEDED for n in ("validate", "prepare", "train", "evaluate")}
    env.workflow.set_steps(ref, done, S.SUCCEEDED, exit_codes={n: 0 for n in done})
    assert env.reconciler.reconcile(run_id).after is RunStatus.SUCCEEDED

    view = env.runs.view(run_id)
    assert all(s.status is StepStatus.SUCCEEDED and s.exit_code == 0 for s in view.steps)
    assert view.run.duration_seconds is not None


def test_parallel_branches_share_only_their_real_dependencies(env: Env) -> None:
    env.pipeline("flow", step("a"), step("b", "a"), step("c", "a"), step("d", "b", "c"))
    run_id = env.run()
    env.reconciler.reconcile(run_id)
    deps = {s.name: s.depends_on for s in env.workflow.submitted[env.ref(run_id)].steps}
    assert deps["b"] == ("a",) and deps["c"] == ("a",) and set(deps["d"]) == {"b", "c"}


def test_failed_upstream_skips_downstream_and_fails_the_run(env: Env) -> None:
    env.pipeline("flow", step("a"), step("b", "a"), step("c", "b"), step("side"))
    run_id = env.run()
    env.reconciler.reconcile(run_id)
    env.workflow.set_steps(
        env.ref(run_id),
        {"a": S.FAILED, "b": S.SKIPPED, "c": S.SKIPPED, "side": S.SUCCEEDED},
        S.FAILED,
        exit_codes={"a": 3, "side": 0},
    )
    assert env.reconciler.reconcile(run_id).after is RunStatus.FAILED
    assert env.step_statuses(run_id) == {
        "a": StepStatus.FAILED,
        "b": StepStatus.SKIPPED,
        "c": StepStatus.SKIPPED,
        "side": StepStatus.SUCCEEDED,
    }
    view = env.runs.view(run_id)
    assert view.run.status_reason == "failed steps: a"
    assert next(s for s in view.steps if s.step_name == "a").exit_code == 3


def test_a_finished_run_never_leaves_steps_pending(env: Env) -> None:
    env.pipeline("flow", step("a"), step("b", "a"))
    run_id = env.run()
    env.reconciler.reconcile(run_id)
    # the workflow system reports the failure but says nothing about the downstream step
    env.workflow.set_steps(env.ref(run_id), {"a": S.FAILED}, S.FAILED)
    env.reconciler.reconcile(run_id)
    assert env.step_statuses(run_id) == {"a": StepStatus.FAILED, "b": StepStatus.SKIPPED}


def test_cancel_before_and_after_submission(env: Env) -> None:
    env.pipeline("flow", step("a"), step("b", "a"))
    early = env.run()
    view = env.runs.request_cancel(early)
    assert view.run.status is RunStatus.CANCELLED
    assert {s.status for s in view.steps} == {StepStatus.CANCELLED}
    assert not env.workflow.submitted

    late = env.run(idempotency_key="other")
    env.reconciler.reconcile(late)
    env.workflow.set_steps(env.ref(late), {"a": S.RUNNING}, S.RUNNING)
    env.reconciler.reconcile(late)
    assert env.runs.request_cancel(late).run.cancel_requested
    assert env.reconciler.reconcile(late).after is RunStatus.CANCELLED
    statuses = env.step_statuses(late)
    assert statuses["a"] is StepStatus.CANCELLED and statuses["b"] is StepStatus.CANCELLED


def test_idempotency_key_creates_one_run_and_one_workflow(env: Env) -> None:
    env.pipeline("flow", step("a"))
    first, c1 = env.runs.create("credit-risk", "flow", idempotency_key="k")
    second, c2 = env.runs.create("credit-risk", "flow", idempotency_key="k")
    assert (c1, c2) == (True, False) and first.run.id == second.run.id
    env.reconciler.reconcile(first.run.id)
    env.reconciler.reconcile(first.run.id)
    assert len(env.workflow.submitted) == 1


def test_runs_need_a_ready_project(uow_factory: Factory, clock: Callable[[], Any]) -> None:
    ProjectService(uow_factory, clock).create(CreateProject(name="cold"))
    JobService(uow_factory, clock).create("cold", CreateJob(name="job-one", image="x:1"))
    PipelineService(uow_factory, clock).create(
        "cold", CreatePipeline("flow", [StepInput("a", "job-one")])
    )
    with pytest.raises(Conflict, match="READY"):
        PipelineRunService(uow_factory, clock).create("cold", "flow")


# -- tracking join -----------------------------------------------------------


def test_platform_run_resolves_to_tracked_results_without_a_tracker_id(env: Env) -> None:
    env.pipeline("flow", step("train"))
    run_id = env.run()
    experiment = env.experiments.ensure_experiment(
        env.project_id, tracking_experiment_name(ProjectService(env.factory).get(env.project_id))
    )
    env.experiments.runs[f"{experiment}/r1"] = ExperimentRun(
        ref="r1",
        params={"alpha": "1.0"},
        metrics={"r2": 0.99},
        tags={TAG_PIPELINE_RUN_ID: str(run_id), TAG_STEP: "train"},
        artifact_uri="s3://artifacts/1/r1/artifacts",
    )
    env.experiments.runs[f"{experiment}/other"] = ExperimentRun(
        ref="other", tags={TAG_PIPELINE_RUN_ID: str(uuid4())}
    )
    tracked = env.runs.tracking(run_id)
    assert len(tracked) == 1 and tracked[0].step == "train"
    assert tracked[0].run.metrics == {"r2": 0.99}
    assert tracked[0].run.artifact_uri == "s3://artifacts/1/r1/artifacts"


# -- API ---------------------------------------------------------------------


def test_api_flow(uow_factory: Factory, clock: Callable[[], Any], env: Env) -> None:
    client = TestClient(
        create_app(uow_factory, clock, workflow=env.workflow, experiments=env.experiments)
    )
    cyclic = {
        "name": "bad",
        "steps": [
            {"name": "a", "job": "job-one", "depends_on": ["b"]},
            {"name": "b", "job": "job-one", "depends_on": ["a"]},
        ],
    }
    assert client.post("/projects/credit-risk/pipelines", json=cyclic).status_code == 422

    body = {
        "name": "flow",
        "steps": [
            {"name": "train", "job": "job-one"},
            {"name": "evaluate", "job": "job-two", "depends_on": ["train"]},
        ],
    }
    assert client.post("/projects/credit-risk/pipelines", json=body).status_code == 201
    assert client.post("/projects/credit-risk/pipelines", json=body).status_code == 200
    assert client.get("/projects/credit-risk/pipelines/flow?version=1").json()["version"] == 1

    started = client.post(
        "/projects/credit-risk/pipelines/flow/runs",
        json={"commit_sha": "abc123"},
        headers={"Idempotency-Key": "k1"},
    )
    assert started.status_code == 202
    run_id = started.json()["id"]
    replay = client.post(
        "/projects/credit-risk/pipelines/flow/runs",
        json={"commit_sha": "abc123"},
        headers={"Idempotency-Key": "k1"},
    )
    assert replay.status_code == 200 and replay.json()["id"] == run_id

    env.reconciler.reconcile(UUID(run_id))
    ref = env.ref(UUID(run_id))
    env.workflow.set_steps(ref, {"train": S.RUNNING}, S.RUNNING)
    env.reconciler.reconcile(UUID(run_id))
    env.workflow.set_logs(ref, "train", "epoch 1\n")

    got = client.get(f"/pipeline-runs/{run_id}").json()
    assert (
        got["status"] == "RUNNING" and got["pipeline"] == "flow" and got["commit_sha"] == "abc123"
    )
    assert [(s["step"], s["status"], s["depends_on"]) for s in got["steps"]] == [
        ("train", "RUNNING", []),
        ("evaluate", "PENDING", ["train"]),
    ]
    assert not {"external_ref", "workflow", "pod"} & set(got)
    assert client.get(f"/pipeline-runs/{run_id}/steps/train/logs").text == "epoch 1\n"
    assert client.get(f"/pipeline-runs/{run_id}/steps/nope/logs").status_code == 404
    assert client.get(f"/pipeline-runs/{run_id}/tracking").json()["runs"] == []
    assert len(client.get("/projects/credit-risk/pipeline-runs?pipeline=flow").json()["items"]) == 1
    assert client.post(f"/pipeline-runs/{run_id}/cancel").status_code == 202
    assert client.get(f"/pipeline-runs/{uuid4()}").status_code == 404


def test_pipeline_timeout_and_retention_keep_steps_and_definition(env: Env, clock: Any) -> None:
    from datetime import timedelta

    from controlplane.adapters.workflow.argo import build_workflow
    from controlplane.reconciliation.retention import WorkflowRetentionReconciler

    definition, _ = env.pipeline("flow", step("a"), step("b", "a"))
    run_id = env.run(timeout_seconds=90, idempotency_key="pipeline-deadline")
    env.reconciler.reconcile(run_id)
    ref = env.ref(run_id)
    assert build_workflow(env.workflow.submitted[ref])["spec"]["activeDeadlineSeconds"] == 90
    with pytest.raises(Conflict):
        env.run(timeout_seconds=91, idempotency_key="pipeline-deadline")
    env.workflow.set_steps(ref, {"a": S.SUCCEEDED, "b": S.SUCCEEDED}, S.SUCCEEDED)
    env.reconciler.reconcile(run_id)
    clock.advance(timedelta(seconds=120))
    retention = WorkflowRetentionReconciler(env.factory, env.workflow, 60, clock)
    assert retention.reconcile_all() == [run_id]
    view = env.runs.view(run_id)
    assert view.run.workflow_cleaned_at is not None
    assert view.definition.id == definition.id
    assert all(s.status is StepStatus.SUCCEEDED for s in view.steps)
    assert retention.reconcile_all() == []
