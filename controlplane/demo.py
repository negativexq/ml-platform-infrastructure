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

And project `customer-support`: an LLM `assistant` with three versions from the Hugging Face
Hub (v1 champion and serving on 1 of 2 GPUs, v2 candidate, v3 rejected), public through the
gateway with a `helpdesk-app` key, and a function `ticket-router` (the team's own container,
scaling to zero) behind the same key.
"""

from __future__ import annotations

import math
import os
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import FastAPI

from controlplane.adapters.fakes import (
    FakeClusterProvider,
    FakeExperimentProvider,
    FakeMetricsProvider,
    FakePlatformTelemetry,
    FakeServingProvider,
    FakeUsage,
    FakeWorkflowProvider,
)
from controlplane.adapters.gateway import ServingUpstream, TokenBucketLimiter
from controlplane.api.app import create_app
from controlplane.api.auth import AuthConfig
from controlplane.application.api_access import ApiAccessService
from controlplane.application.deployments import FUNCTION_LIMITS, DeploymentService
from controlplane.application.gateway import GatewayService
from controlplane.application.jobs import CreateJob, JobService
from controlplane.application.models import EvaluationService, ModelService, PromotionService
from controlplane.application.pipeline_runs import PipelineRunService
from controlplane.application.pipelines import CreatePipeline, PipelineService, StepInput
from controlplane.application.projects import CreateProject, ProjectService
from controlplane.application.providers import (
    ExperimentRun,
    ExternalState,
    MetricsPoint,
    PlatformSignal,
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
from controlplane.domain.entities import (
    EndpointLimits,
    FunctionServing,
    LlmServing,
    RolloutGate,
    Threshold,
)
from controlplane.domain.states import Exposure, ModelKind
from controlplane.gateway import create_gateway
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
    uow_factory: Callable[[], Any] = lambda: None  # noqa: E731 - the store behind the app
    gateway: FastAPI | None = None
    keys: dict[str, str] = field(default_factory=dict)  # demo API keys by name, full tokens

    @property
    def asgi(self) -> Any:
        """The API and, under /gateway, the inference gateway, in one process for the demo.
        In a real deployment they are separate services (see k8s/gateway)."""
        api, gateway = self.app, self.gateway

        async def app(scope: Any, receive: Any, send: Any) -> None:
            path = scope.get("path", "")
            if gateway is not None and scope["type"] == "http" and path.startswith("/gateway/"):
                scope = {**scope, "path": path.removeprefix("/gateway")}
                await gateway(scope, receive, send)
            else:
                await api(scope, receive, send)

        return app


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


def build_demo(observed: bool = False, auth: AuthConfig | None = None) -> Demo:
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

    usage = _usage_history(clock)
    keys = _open_to_partners(factory, clock)

    if observed:
        factory, fakes, rec = _observed(factory, clock, fakes)

    gateway = create_gateway(
        GatewayService(
            factory,
            ServingUpstream(fakes["serving"]),
            TokenBucketLimiter(),
            recorders=[usage],
            authenticator=auth.authenticator if auth is not None else None,
            clock=clock,
        )
    )
    app = create_app(
        factory,
        clock,
        workflow=fakes["workflow"],
        experiments=fakes["experiments"],
        serving=fakes["serving"],
        metrics=fakes["metrics"],
        auth=auth,
        platform=_platform_telemetry(),
        usage=usage,
        gateway_url=os.environ.get("CP_DEMO_GATEWAY_URL", "http://localhost:8080/gateway"),
    )
    return Demo(app, clock, rec.all, ids, factory, gateway, keys)


def _open_to_partners(factory: Callable[..., Any], clock: DemoClock) -> dict[str, str]:
    """credit-risk-prod is public with two partner keys (and one revoked); ranker-staging
    stays internal."""
    access = ApiAccessService(factory, clock)
    access.expose(
        "credit-risk",
        "credit-risk-prod",
        Exposure.PUBLIC,
        EndpointLimits(units_per_minute=600, max_body_kb=256, timeout_seconds=10),
    )
    keys = {}
    for name, per_minute in (("partner-acme", 120), ("batch-scoring", None), ("old-partner", None)):
        key, token = access.create_key(
            "credit-risk", name=name, endpoints=["credit-risk-prod"], units_per_minute=per_minute
        )
        keys[name] = token
        if name == "old-partner":
            access.revoke_key("credit-risk", key.key_id)
    access.expose(
        "customer-support",
        "assistant-prod",
        Exposure.PUBLIC,
        EndpointLimits(units_per_minute=30_000, max_body_kb=512, timeout_seconds=120),
    )
    access.expose("customer-support", "ticket-router", Exposure.PUBLIC, FUNCTION_LIMITS)
    _, keys["helpdesk-app"] = access.create_key(
        "customer-support", name="helpdesk-app", endpoints=["assistant-prod", "ticket-router"]
    )
    return keys


def _usage_history(clock: DemoClock) -> FakeUsage:
    """A day of partner traffic on credit-risk-prod, plus whatever the demo gateway serves."""
    usage = FakeUsage(clock)
    acme, batch = _wave(42, seed=61, swing=0.3), _wave(14, seed=62, swing=0.6)
    refused = _wave(0.6, seed=63, swing=0.8, noise=0.4)
    usage.series[("credit-risk", "credit-risk-prod", "partner-acme")] = lambda t: (
        acme(t) or 0.0,
        refused(t) or 0.0,
        0.0,
    )
    failing = _wave(0.08, seed=64, swing=0.9, noise=0.5)
    usage.series[("credit-risk", "credit-risk-prod", "batch-scoring")] = lambda t: (
        batch(t) or 0.0,
        0.0,
        failing(t) or 0.0,
    )
    usage.p95[("credit-risk", "credit-risk-prod")] = _wave(96, seed=65, swing=0.12)
    # the help-desk assistant: tokens, about two thirds of them prompt (context, history)
    helpdesk = _wave(9_200, seed=66, swing=0.35)
    usage.series[("customer-support", "assistant-prod", "helpdesk-app")] = lambda t: (
        helpdesk(t) or 0.0,
        0.0,
        0.0,
    )
    usage.token_split[("customer-support", "assistant-prod", "helpdesk-app")] = 0.68
    usage.p95[("customer-support", "assistant-prod")] = _wave(2_400, seed=67, swing=0.2)
    routed = _wave(18, seed=68, swing=0.5)
    usage.series[("customer-support", "ticket-router", "helpdesk-app")] = lambda t: (
        routed(t) or 0.0,
        0.0,
        0.0,
    )
    usage.p95[("customer-support", "ticket-router")] = _wave(45, seed=69, swing=0.3)
    return usage


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


def _traffic(
    *, p95: float, errors: float, rps: float, seed: int, since: datetime | None = None
) -> Callable[[datetime], MetricsPoint | None]:
    """Believable serving metrics as a function of time: a daily cycle, noise, the odd
    latency spike. Deterministic per moment, so a refresh redraws the same past."""

    def at(t: datetime) -> MetricsPoint | None:
        if since is not None and t < since:
            return None
        s = t.timestamp()
        # Smooth like a 2-minute rate window: a daily cycle, slower drifts, a little noise.
        rng = random.Random(int(s // 15) * 7919 + seed)
        day = math.sin(s / 86400 * 2 * math.pi)
        drift = 0.06 * math.sin(s / 700 + seed) + 0.04 * math.sin(s / 173 + 2 * seed)
        noise = 0.04 * (rng.random() - 0.5)
        spike = p95 * 0.6 if rng.random() < 0.004 else 0.0
        return MetricsPoint(
            at=t,
            p95_latency_ms=p95 * (1 + 0.12 * day + drift + noise) + spike,
            error_rate=max(0.0, errors * (1 + 2.5 * drift + 4 * noise)),
            requests_per_second=rps * (1 + 0.3 * day + drift / 2 + noise / 2),
        )

    return at


def _wave(
    base: float, *, seed: int, swing: float = 0.1, noise: float = 0.05, floor: float = 0.0
) -> Callable[[datetime], float | None]:
    """A smooth, deterministic series around `base` for the platform's own metrics."""

    def at(t: datetime) -> float | None:
        s = t.timestamp()
        rng = random.Random(int(s // 15) * 104729 + seed)
        drift = swing * (math.sin(s / 900 + seed) + 0.5 * math.sin(s / 241 + 3 * seed))
        return max(floor, base * (1 + drift + noise * (rng.random() - 0.5)))

    return at


def _platform_telemetry() -> FakePlatformTelemetry:
    """The control plane's own health as Prometheus would report it: busy but fine, except
    that the serving backend fails a little more often than it should (a warning)."""
    fake = FakePlatformTelemetry()
    sig = PlatformSignal
    fake.series[(sig.API_REQUESTS, "")] = _wave(4.2, seed=1, swing=0.25)
    fake.series[(sig.API_ERRORS, "")] = _wave(0.002, seed=2, swing=0.5, noise=0.15)
    fake.series[(sig.API_LATENCY, "")] = _wave(120, seed=3, swing=0.15)
    reconcilers = ("projects", "runs", "pipeline_runs", "deployments", "rollouts", "model_aliases")
    for i, name in enumerate(reconcilers):
        fake.series[(sig.RECONCILE_PASSES, name)] = _wave(5.6, seed=10 + i, swing=0.03)
        errors = 0.004 if name == "deployments" else 0.0
        fake.series[(sig.RECONCILE_ERRORS, name)] = _wave(errors, seed=20 + i, swing=0.5)
    systems = {
        "cluster": (0.0, 35),
        "experiments": (0.001, 80),
        "metrics": (0.0, 25),
        "serving": (0.024, 240),
        "workflow": (0.002, 60),
    }
    for i, (name, (errors, p95)) in enumerate(systems.items()):
        fake.series[(sig.PROVIDER_ERRORS, name)] = _wave(errors, seed=30 + i, swing=0.3)
        fake.series[(sig.PROVIDER_LATENCY, name)] = _wave(p95, seed=40 + i, swing=0.2)
    fake.series[(sig.GATEWAY_REQUESTS, "")] = _wave(0.95, seed=70, swing=0.3)
    fake.series[(sig.GATEWAY_ERRORS, "")] = _wave(0.0018, seed=71, swing=0.6, noise=0.2)
    fake.series[(sig.GATEWAY_LATENCY, "")] = _wave(118, seed=72, swing=0.12)
    fake.series[(sig.GATEWAY_TOKENS, "prompt")] = _wave(6_250, seed=73, swing=0.35)
    fake.series[(sig.GATEWAY_TOKENS, "completion")] = _wave(2_950, seed=74, swing=0.35)
    for i, (name, rate) in enumerate(
        {"run": 1.4, "pipeline_run": 0.8, "deployment": 0.3, "rollout": 0.2}.items()
    ):
        fake.series[(sig.TRANSITIONS, name)] = _wave(rate, seed=50 + i, swing=0.4)
    return fake


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
    canary_since = started.rollout.created_at
    metrics.series[(ref, 1)] = _traffic(p95=92.0, errors=0.002, rps=31.0, seed=1)
    metrics.series[(ref, 2)] = _traffic(
        p95=118.0, errors=0.004, rps=10.4, seed=2, since=canary_since
    )
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
    metrics.series[(ref2, 1)] = _traffic(p95=55.0, errors=0.001, rps=12.0, seed=3)
    metrics.series[(ref2, 2)] = _traffic(p95=61.0, errors=0.0015, rps=12.0, seed=4)
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

    # project `customer-support`: an LLM assistant ----------------------------------------
    support, _ = projects.create(
        CreateProject(
            name="customer-support",
            display_name="Customer Support",
            description="The help-desk assistant: an LLM answering customers' questions.",
        )
    )
    rec.projects.reconcile(support.id)
    projects.set_gpu_quota("customer-support", 2)
    rec.projects.reconcile(support.id)
    models.create(
        "customer-support",
        "assistant",
        {"helpfulness": Threshold(min=0.75), "toxicity": Threshold(max=0.01)},
        kind=ModelKind.LLM,
        serving=LlmServing(gpus=1, context_length=8192),
    )
    hub = (  # (source, helpfulness, toxicity) from an offline evaluation
        ("hf://Qwen/Qwen2.5-7B-Instruct@a09a354", 0.81, 0.004),
        ("hf://meta-llama/Llama-3.1-8B-Instruct@0e9e39f", 0.86, 0.003),
        ("hf://mistralai/Mistral-7B-Instruct-v0.3@e0bc86c", 0.71, 0.006),
    )
    llm_versions = []
    for source, helpful, toxic in hub:
        scores = {"helpfulness": helpful, "toxicity": toxic}
        version, _ = models.register_from_hub("customer-support", "assistant", source, scores)
        llm_versions.append(evals.evaluate(version.id).version)
        clock.advance(600)
    promotions.promote(llm_versions[0].id)
    deployments.create("customer-support", "assistant-prod")
    deployments.deploy("customer-support", "assistant-prod", "assistant", 1)
    settle()
    settle()

    # a function: the team's own container that routes tickets, scaled to zero when idle
    models.create(
        "customer-support",
        "ticket-router",
        {},
        kind=ModelKind.FUNCTION,
        function=FunctionServing(min_scale=0, max_scale=5, concurrency=20, env={"QUEUE": "tier1"}),
    )
    for tag in ("1.3.0", "1.4.2"):
        models.register_image(
            "customer-support", "ticket-router", f"ghcr.io/acme/ticket-router:{tag}"
        )
        clock.advance(300)
    deployments.create("customer-support", "ticket-router")
    deployments.deploy("customer-support", "ticket-router", "ticket-router", 2)
    settle()
    settle()

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
    auth = _auth_from_env()
    demo = build_demo(observed=telemetry.enabled, auth=auth)
    if auth is not None:
        _seed_members(demo)

    def loop() -> None:
        while True:  # lets the UI's Abort / Cancel buttons take effect
            try:
                demo.reconcile()
            except Exception as exc:  # noqa: BLE001 - a demo loop must never die
                print("reconcile error:", exc)
            time.sleep(2)

    threading.Thread(target=loop, daemon=True).start()
    print("Demo control plane (in-memory fakes) on http://localhost:8080  ->  /ui")
    print("Gateway on http://localhost:8080/gateway, try:")
    print(
        f"  curl -s -H 'Authorization: Bearer {demo.keys['partner-acme']}' "
        "-d '{\"instances\": [[0.4, 1200, 3, 0.2]]}' "
        "http://localhost:8080/gateway/v1/credit-risk/credit-risk-prod/predict"
    )
    uvicorn.run(demo.asgi, host="127.0.0.1", port=8080, log_level="warning")
    telemetry.shutdown()


def _auth_from_env() -> AuthConfig | None:
    """`CP_AUTH_MODE=oidc` plus the usual CP_OIDC_* settings turn sign-in on, e.g. against the
    local Keycloak (docs/identity.md). Without it the demo runs open, as before."""

    if os.environ.get("CP_AUTH_MODE") != "oidc":
        return None
    from controlplane.main import auth_config
    from controlplane.settings import Settings

    return auth_config(Settings())


def _seed_members(demo: Demo) -> None:
    """Roles for the local Keycloak's users: alice is a platform admin through her group;
    the ml-team group (bob) operates credit-risk; carol may look at fraud-detection."""
    from controlplane.application.identity import bind_principal, reset_principal
    from controlplane.application.members import MembershipService
    from controlplane.domain.access import Principal, ProjectRole

    token = bind_principal(Principal(username="demo-setup", platform_admin=True))
    try:
        members = MembershipService(demo.uow_factory)
        members.set_role("credit-risk", "group:ml-team", ProjectRole.OPERATOR)
        members.set_role("fraud-detection", "user:carol", ProjectRole.VIEWER)
    finally:
        reset_principal(token)


if __name__ == "__main__":
    main()
