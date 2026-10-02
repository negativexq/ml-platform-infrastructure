"""A control plane filled with believable data, running on in-memory fakes.

    python -m controlplane.demo            # http://localhost:8080  (UI at /ui)

No PostgreSQL, Kubernetes, Argo, MLflow or KServe is involved: every provider is the
in-memory fake from `controlplane.adapters.fakes`. It exists so the UI can be developed,
tested in a real browser and looked at without any infrastructure.

The seeded story (project `credit-risk`):
  * pipeline `training` (validate -> prepare -> train | profile -> evaluate) with a
    succeeded run, a failed run (prepare exits 3, the rest are skipped) and a run in flight
  * model `scorer`: v1 REJECTED by evaluation, v2 CHAMPION, v3 CANDIDATE
  * deployment `credit-risk-prod` serving v2 with a canary of v3 at 25%
  * model `ranker` / deployment `ranker-staging` after a finished rollout, so Rollback works
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import FastAPI

from controlplane.adapters.fakes import (
    FakeClusterProvider,
    FakeExperimentProvider,
    FakeMetricsProvider,
    FakeServingProvider,
    FakeWorkflowProvider,
)
from controlplane.api.app import create_app
from controlplane.application.deployments import DeploymentService
from controlplane.application.jobs import CreateJob, JobService
from controlplane.application.models import EvaluationService, ModelService, PromotionService
from controlplane.application.pipeline_runs import PipelineRunService
from controlplane.application.pipelines import CreatePipeline, PipelineService, StepInput
from controlplane.application.projects import CreateProject, ProjectService
from controlplane.application.providers import (
    ExperimentRun,
    ExternalState,
    RegisteredVersion,
    RevisionMetrics,
)
from controlplane.application.rollouts import RolloutService
from controlplane.application.runs import RunService
from controlplane.application.workflow_compiler import (
    TAG_COMMIT_SHA,
    TAG_PIPELINE_RUN_ID,
    TAG_STEP,
    tracking_experiment_name,
)
from controlplane.domain.entities import RolloutGate, Threshold
from controlplane.persistence.memory import MemoryStore, MemoryUnitOfWork
from controlplane.reconciliation.deployments import DeploymentReconciler
from controlplane.reconciliation.model_aliases import ModelAliasReconciler
from controlplane.reconciliation.pipeline_runs import PipelineRunReconciler
from controlplane.reconciliation.projects import ProjectReconciler
from controlplane.reconciliation.rollouts import RolloutReconciler
from controlplane.reconciliation.runs import RunReconciler

S = ExternalState


class DemoClock:
    """Starts in the past so history has believable timestamps, then goes live."""

    def __init__(self, hours_ago: float = 3.0) -> None:
        self._now = datetime.now(UTC) - timedelta(hours=hours_ago)
        self._live = False

    def __call__(self) -> datetime:
        if self._live:
            self._now = max(self._now + timedelta(milliseconds=1), datetime.now(UTC))
        else:
            self._now += timedelta(milliseconds=1)
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def go_live(self) -> None:
        assert self._now <= datetime.now(UTC), "seeded history must end in the past"
        self._live = True


@dataclass
class Demo:
    app: FastAPI
    clock: DemoClock
    reconcile: Callable[[], None]
    ids: dict[str, UUID]


class _Reconcilers:
    def __init__(
        self, factory: Callable[..., Any], clock: DemoClock, fakes: dict[str, Any]
    ) -> None:
        self.projects = ProjectReconciler(factory, FakeClusterProvider(), clock)
        self.runs = RunReconciler(factory, fakes["workflow"], clock)
        self.pipeline_runs = PipelineRunReconciler(
            factory, fakes["workflow"], fakes["experiments"], "http://mlflow:5000", clock
        )
        self.deployments = DeploymentReconciler(factory, fakes["serving"], clock)
        self.rollouts = RolloutReconciler(factory, fakes["serving"], fakes["metrics"], clock)
        self.aliases = ModelAliasReconciler(factory, fakes["experiments"], clock)

    def all(self) -> None:
        self.projects.reconcile_all()
        self.runs.reconcile_all()
        self.pipeline_runs.reconcile_all()
        self.rollouts.reconcile_all()
        self.deployments.reconcile_all()
        self.aliases.reconcile_all()


def build_demo(observed: bool = False) -> Demo:
    """`observed` wires the same telemetry the real deployment has (traces, metrics, trace
    context across the reconciler) around the fakes, *after* the history is seeded."""
    store = MemoryStore()

    def factory() -> MemoryUnitOfWork:
        return MemoryUnitOfWork(store)

    clock = DemoClock()
    fakes: dict[str, Any] = {
        "workflow": FakeWorkflowProvider(),
        "experiments": FakeExperimentProvider(),
        "serving": FakeServingProvider(),
        "metrics": FakeMetricsProvider(),
    }
    rec = _Reconcilers(factory, clock, fakes)
    ids = _seed(factory, clock, fakes, rec)
    clock.go_live()

    if observed:
        factory, fakes, rec = _observed(factory, clock, fakes)

    app = create_app(
        factory,
        clock,
        workflow=fakes["workflow"],
        experiments=fakes["experiments"],
        serving=fakes["serving"],
        metrics=fakes["metrics"],
    )
    return Demo(app, clock, rec.all, ids)


def _observed(
    factory: Callable[..., Any], clock: DemoClock, fakes: dict[str, Any]
) -> tuple[Callable[..., Any], dict[str, Any], _Reconcilers]:
    from controlplane import observability as obs

    uow = obs.observed_uow_factory(factory)
    wrapped = {
        "workflow": obs.observe(fakes["workflow"], "workflow", obs.WORKFLOW_MUTATIONS),
        "experiments": obs.observe(fakes["experiments"], "experiments", obs.EXPERIMENT_MUTATIONS),
        "serving": obs.observe(fakes["serving"], "serving", obs.SERVING_MUTATIONS),
        "metrics": obs.observe(fakes["metrics"], "metrics", obs.NO_MUTATIONS),
    }
    rec = _Reconcilers(uow, clock, wrapped)

    def origin(getter: str) -> Callable[[Any, UUID], str | None]:
        def read(u: Any, entity_id: UUID) -> str | None:
            entity = getattr(u, getter).get(entity_id)
            return None if entity is None else entity.traceparent

        return read

    for name, reconciler, getter in (
        ("projects", rec.projects, "projects"),
        ("runs", rec.runs, "runs"),
        ("pipeline_runs", rec.pipeline_runs, "pipeline_runs"),
        ("deployments", rec.deployments, "deployments"),
        ("rollouts", rec.rollouts, "rollouts"),
        ("model_aliases", rec.aliases, None),
    ):
        obs.instrument_reconciler(reconciler, name, uow, origin(getter) if getter else None)
    return uow, wrapped, rec


# -- seeding ------------------------------------------------------------------


def _seed(
    factory: Callable[..., Any], clock: DemoClock, fakes: dict[str, Any], rec: _Reconcilers
) -> dict[str, UUID]:
    workflow: FakeWorkflowProvider = fakes["workflow"]
    experiments: FakeExperimentProvider = fakes["experiments"]
    serving: FakeServingProvider = fakes["serving"]
    metrics: FakeMetricsProvider = fakes["metrics"]

    projects = ProjectService(factory, clock)
    jobs = JobService(factory, clock)
    pipelines = PipelineService(factory, clock)
    pipeline_runs = PipelineRunService(factory, clock, experiments)
    runs = RunService(factory, clock)
    models = ModelService(factory, clock, experiments)
    evals = EvaluationService(factory, experiments, clock)
    promotions = PromotionService(factory, clock)
    deployments = DeploymentService(factory, clock, experiments, serving)
    rollouts = RolloutService(factory, deployments, clock)

    def settle() -> None:
        clock.advance(2)
        rec.all()

    # projects ---------------------------------------------------------------
    credit, _ = projects.create(
        CreateProject(
            name="credit-risk",
            display_name="Credit Risk",
            description="Application scoring: training, the scorer model, its serving.",
        )
    )
    projects.create(
        CreateProject(
            name="fraud-detection",
            display_name="Fraud Detection",
            description="Transaction anomaly models.",
        )
    )
    rec.projects.reconcile(credit.id)
    for p in projects.list():
        if p.name == "fraud-detection":
            rec.projects.reconcile(p.id)
    jobs.create("fraud-detection", CreateJob(name="score-batch", image="fraud:1"))

    # jobs and the training pipeline ------------------------------------------
    for name in ("validate-data", "prepare-data", "train-model", "profile-data", "evaluate-model"):
        jobs.create(
            "credit-risk",
            CreateJob(
                name=name,
                image=f"credit-risk/{name}:sha-a83d2c1",
                command=("python", "-m", name.replace("-", "_")),
                resources={"cpu": "2", "memory": "4Gi"},
            ),
        )
    jobs.create(
        "credit-risk",
        CreateJob(name="smoke-test", image="busybox:1.36", command=("sh", "-c", "echo ok")),
    )
    pipelines.create(
        "credit-risk",
        CreatePipeline(
            "training",
            [
                StepInput("validate", "validate-data"),
                StepInput("prepare", "prepare-data", ("validate",)),
                StepInput("train", "train-model", ("prepare",)),
                StepInput("profile", "profile-data", ("prepare",)),
                StepInput("evaluate", "evaluate-model", ("train",)),
            ],
        ),
    )

    experiment = experiments.ensure_experiment(credit.id, tracking_experiment_name(credit))

    def drive(
        run_id: UUID,
        stages: list[tuple[float, dict[str, ExternalState], ExternalState, dict[str, int]]],
    ) -> str:
        view = pipeline_runs.view(run_id)
        rec.pipeline_runs.reconcile(run_id)
        ref = pipeline_runs.view(run_id).run.external_ref
        assert ref
        for seconds, steps, overall, codes in stages:
            clock.advance(seconds)
            workflow.set_steps(ref, steps, overall, exit_codes=codes)
            rec.pipeline_runs.reconcile(run_id)
        _ = view
        return ref

    # run A: everything succeeds -------------------------------------------------
    a_view, _ = pipeline_runs.create(
        "credit-risk", "training", commit_sha="a83d2c1", idempotency_key="demo-a"
    )
    a = a_view.run.id
    ref_a = drive(
        a,
        [
            (4, {"validate": S.RUNNING}, S.RUNNING, {}),
            (21, {"validate": S.SUCCEEDED, "prepare": S.RUNNING}, S.RUNNING, {"validate": 0}),
            (
                63,
                {
                    "validate": S.SUCCEEDED,
                    "prepare": S.SUCCEEDED,
                    "train": S.RUNNING,
                    "profile": S.RUNNING,
                },
                S.RUNNING,
                {"validate": 0, "prepare": 0},
            ),
            (
                118,
                {
                    "validate": S.SUCCEEDED,
                    "prepare": S.SUCCEEDED,
                    "train": S.SUCCEEDED,
                    "profile": S.SUCCEEDED,
                    "evaluate": S.RUNNING,
                },
                S.RUNNING,
                {"validate": 0, "prepare": 0, "train": 0, "profile": 0},
            ),
            (
                35,
                {n: S.SUCCEEDED for n in ("validate", "prepare", "train", "profile", "evaluate")},
                S.SUCCEEDED,
                dict.fromkeys(("validate", "prepare", "train", "profile", "evaluate"), 0),
            ),
        ],
    )
    logs = {
        "validate": "checked 48,210 rows, 0 schema violations\n",
        "prepare": "imputed 312 missing values\nfeature matrix: (48210, 37)\n",
        "train": "fit Ridge(alpha=1.0)\nauc=0.939 f1=0.880\nlogged model to registry\n",
        "profile": "wrote profile report (37 columns)\n",
        "evaluate": "holdout auc=0.941, f1=0.882\n",
    }
    for step, text in logs.items():
        workflow.set_logs(ref_a, step, text)
    experiments.runs[f"{experiment}/mlrun-a"] = ExperimentRun(
        ref="mlrun-a",
        params={"model_type": "Ridge", "alpha": "1.0", "samples": "48210"},
        metrics={"auc": 0.939, "f1": 0.88, "rmse": 0.31},
        tags={TAG_PIPELINE_RUN_ID: str(a), TAG_STEP: "train", TAG_COMMIT_SHA: "a83d2c1"},
        artifact_uri="s3://mlflow/artifacts/1/9f1c0e/artifacts",
    )

    # run B: prepare fails, the rest is skipped -------------------------------------
    clock.advance(600)
    b_view, _ = pipeline_runs.create(
        "credit-risk", "training", commit_sha="b17e90d", idempotency_key="demo-b"
    )
    b = b_view.run.id
    ref_b = drive(
        b,
        [
            (3, {"validate": S.SUCCEEDED, "prepare": S.RUNNING}, S.RUNNING, {"validate": 0}),
            (
                40,
                {
                    "validate": S.SUCCEEDED,
                    "prepare": S.FAILED,
                    "train": S.SKIPPED,
                    "profile": S.SKIPPED,
                    "evaluate": S.SKIPPED,
                },
                S.FAILED,
                {"validate": 0, "prepare": 3},
            ),
        ],
    )
    workflow.set_logs(
        ref_b,
        "prepare",
        "loading features\nTraceback (most recent call last):\n"
        '  File "prepare.py", line 41, in main\n'
        "KeyError: 'income_band'\nexit status 3\n",
    )

    # run C: still running -------------------------------------------------------------
    clock.advance(900)
    c_view, _ = pipeline_runs.create(
        "credit-risk", "training", commit_sha="c4d2f55", idempotency_key="demo-c"
    )
    c = c_view.run.id
    ref_c = drive(
        c,
        [
            (3, {"validate": S.RUNNING}, S.RUNNING, {}),
            (
                25,
                {
                    "validate": S.SUCCEEDED,
                    "prepare": S.SUCCEEDED,
                    "train": S.RUNNING,
                    "profile": S.SUCCEEDED,
                },
                S.RUNNING,
                {"validate": 0, "prepare": 0, "profile": 0},
            ),
        ],
    )
    workflow.set_logs(ref_c, "train", "fit Ridge(alpha=0.5)\nepoch 7/20 ...\n")

    # single job runs -------------------------------------------------------------------
    ok, _ = runs.create("credit-risk", "smoke-test", idempotency_key="demo-ok")
    rec.runs.reconcile(ok.id)
    ref_ok = runs.get(ok.id).external_ref
    assert ref_ok
    clock.advance(6)
    workflow.set_state(ref_ok, S.SUCCEEDED, exit_code=0)
    workflow.set_logs(ref_ok, "main", "ok\n")
    rec.runs.reconcile(ok.id)
    bad, _ = runs.create("credit-risk", "evaluate-model", idempotency_key="demo-bad")
    rec.runs.reconcile(bad.id)
    ref_bad = runs.get(bad.id).external_ref
    assert ref_bad
    clock.advance(12)
    workflow.set_state(ref_bad, S.FAILED, reason="ImagePullBackOff", exit_code=None)
    rec.runs.reconcile(bad.id)
    live, _ = runs.create("credit-risk", "train-model", idempotency_key="demo-live")
    rec.runs.reconcile(live.id)
    ref_live = runs.get(live.id).external_ref
    assert ref_live
    workflow.set_state(ref_live, S.RUNNING)
    workflow.set_logs(ref_live, "main", "warming up\n")
    rec.runs.reconcile(live.id)

    # model `scorer` ---------------------------------------------------------------------
    models.create("credit-risk", "scorer", {"auc": Threshold(min=0.9), "f1": Threshold(min=0.85)})
    registry = "credit-risk-scorer"
    results = {"1": (0.81, 0.79), "2": (0.939, 0.88), "3": (0.947, 0.89)}
    for ref, (auc, f1) in results.items():
        tags = {TAG_PIPELINE_RUN_ID: str(a)} if ref == "2" else {}
        experiments.runs[f"run-{ref}"] = ExperimentRun(
            ref=f"run-{ref}", metrics={"auc": auc, "f1": f1}, tags=tags
        )
        experiments.registered.setdefault(registry, []).append(RegisteredVersion(ref, f"run-{ref}"))
        clock.advance(300)
    models.discover("credit-risk", "scorer")
    versions = {v.version: v.id for v in models.get("credit-risk", "scorer").versions}
    for number in (1, 2, 3):
        evals.evaluate(versions[number])
        clock.advance(30)
    promotions.promote(versions[2])

    # deployment `credit-risk-prod`: v2 stable, v3 canary ----------------------------------
    deployments.create("credit-risk", "credit-risk-prod")
    deployments.deploy("credit-risk", "credit-risk-prod", "scorer", 2)
    settle()
    ref = "mlp-credit-risk/credit-risk-prod"
    metrics.by_revision[(ref, 1)] = RevisionMetrics(92.0, 0.002, 41.3, 5120)
    metrics.by_revision[(ref, 2)] = RevisionMetrics(118.0, 0.004, 10.4, 380)
    started = rollouts.start(
        "credit-risk",
        "credit-risk-prod",
        "scorer",
        3,
        gate=RolloutGate(step_seconds=3600, min_requests=50),
    )
    rid = started.rollout.id
    rec.rollouts.reconcile(rid)  # 10%
    rec.rollouts.reconcile(rid)  # observing
    # an earlier step already passed its gate: show it at 25%
    from dataclasses import replace

    with factory() as uow:
        current = uow.rollouts.get(rid)
        assert current is not None
        uow.rollouts.update(
            replace(current, gate=RolloutGate(step_seconds=60, min_requests=50)),
            expected_status=current.status,
        )
        uow.commit()
    clock.advance(61)
    rec.rollouts.reconcile(rid)  # passes the 10% gate -> 25%
    rec.rollouts.reconcile(rid)  # observing
    with factory() as uow:
        current = uow.rollouts.get(rid)
        assert current is not None
        uow.rollouts.update(
            replace(current, gate=RolloutGate(step_seconds=3600, min_requests=50)),
            expected_status=current.status,
        )
        uow.commit()

    # project `credit-risk`: model `ranker` + a finished rollout so Rollback is available ----
    models.create("credit-risk", "ranker", {"ndcg": Threshold(min=0.8)})
    ranker_registry = "credit-risk-ranker"
    for ref, ndcg in (("1", 0.84), ("2", 0.88)):
        experiments.runs[f"rrun-{ref}"] = ExperimentRun(ref=f"rrun-{ref}", metrics={"ndcg": ndcg})
        experiments.registered.setdefault(ranker_registry, []).append(
            RegisteredVersion(ref, f"rrun-{ref}")
        )
    models.discover("credit-risk", "ranker")
    ranker = {v.version: v.id for v in models.get("credit-risk", "ranker").versions}
    for number in (1, 2):
        evals.evaluate(ranker[number])
    promotions.promote(ranker[1])
    deployments.create("credit-risk", "ranker-staging")
    deployments.deploy("credit-risk", "ranker-staging", "ranker", 1)
    settle()
    ref2 = "mlp-credit-risk/ranker-staging"
    metrics.by_revision[(ref2, 1)] = RevisionMetrics(55.0, 0.001, 12.0, 900)
    metrics.by_revision[(ref2, 2)] = RevisionMetrics(61.0, 0.002, 12.0, 900)
    done = rollouts.start(
        "credit-risk",
        "ranker-staging",
        "ranker",
        2,
        steps=[50, 100],
        gate=RolloutGate(step_seconds=30, min_requests=10),
    )
    rec.rollouts.reconcile(done.rollout.id)
    for _ in range(2):
        rec.rollouts.reconcile(done.rollout.id)
        clock.advance(31)
        rec.rollouts.reconcile(done.rollout.id)
    rec.deployments.reconcile_all()
    rec.aliases.reconcile_all()
    clock.advance(5)

    # Created last and never reconciled, so the project list shows a PENDING project.
    projects.create(CreateProject(name="churn-prediction", display_name="Churn Prediction"))
    return {
        "project": credit.id,
        "run_ok": a,
        "run_failed": b,
        "run_live": c,
        "rollout": rid,
        "job_run_ok": ok.id,
        "job_run_failed": bad.id,
        "job_run_live": live.id,
    }


def main() -> None:
    import uvicorn

    from controlplane import observability

    telemetry = observability.configure("mlp-controlplane-demo", json_logs=False)
    demo = build_demo(observed=telemetry.enabled)

    def loop() -> None:
        while True:  # lets the UI's Abort / Cancel buttons take effect
            try:
                demo.reconcile()
            except Exception as exc:  # noqa: BLE001 - a demo loop must never die
                print("reconcile error:", exc)
            time.sleep(2)

    threading.Thread(target=loop, daemon=True).start()
    print("Demo control plane (in-memory fakes) on http://localhost:8080  ->  /ui")
    uvicorn.run(demo.app, host="127.0.0.1", port=8080, log_level="warning")
    telemetry.shutdown()


if __name__ == "__main__":
    main()
