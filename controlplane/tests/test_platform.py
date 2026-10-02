"""The Monitor page's read model: checks judged against thresholds, silence, inventory."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from controlplane.adapters.fakes import FakeClusterProvider, FakePlatformTelemetry
from controlplane.api.app import create_app
from controlplane.application.jobs import CreateJob, JobService
from controlplane.application.platform import SIGNALS, Health, PlatformService, judge
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import CreateProject, ProjectService
from controlplane.application.providers import PlatformSignal
from controlplane.application.runs import RunService
from controlplane.reconciliation.projects import ProjectReconciler

Factory = Callable[[], UnitOfWork]
SPEC = {s.signal: s for s in SIGNALS}


def steady(value: float) -> Callable[[datetime], float]:
    return lambda _t: value


def test_thresholds_follow_the_direction_of_bad() -> None:
    errors = SPEC[PlatformSignal.API_ERRORS]
    assert judge(errors, 0.001) is Health.OK
    assert judge(errors, 0.02) is Health.WARNING
    assert judge(errors, 0.2) is Health.CRITICAL
    assert judge(errors, None) is Health.NO_DATA
    heartbeat = SPEC[PlatformSignal.RECONCILE_PASSES]
    assert judge(heartbeat, 6) is Health.OK
    assert judge(heartbeat, 0.5) is Health.WARNING
    assert judge(heartbeat, 0) is Health.CRITICAL
    assert judge(heartbeat, None) is Health.CRITICAL  # silence from a heartbeat is an outage


def test_a_reconciler_that_went_quiet_is_critical(uow_factory: Factory, clock: Any) -> None:
    now = clock()
    telemetry = FakePlatformTelemetry()
    telemetry.series[(PlatformSignal.RECONCILE_PASSES, "runs")] = steady(6)
    cutoff = now - timedelta(minutes=20)
    telemetry.series[(PlatformSignal.RECONCILE_PASSES, "rollouts")] = lambda t: (
        6.0 if t < cutoff else None
    )
    telemetry.series[(PlatformSignal.API_ERRORS, "")] = steady(0.0)
    health = PlatformService(uow_factory, telemetry, lambda: now).health(minutes=60)
    passes = next(s for s in health.signals if s.spec.signal is PlatformSignal.RECONCILE_PASSES)
    assert {s.name: (s.status, s.current) for s in passes.series} == {
        "rollouts": (Health.CRITICAL, None),
        "runs": (Health.OK, 6),
    }
    assert health.status is Health.CRITICAL and health.available
    assert health.step_seconds == 15 and len(passes.series[1].points) == 240


def test_without_telemetry_the_page_still_has_the_inventory(
    uow_factory: Factory, clock: Any
) -> None:
    ProjectService(uow_factory, clock).create(CreateProject(name="credit-risk"))
    ProjectReconciler(uow_factory, FakeClusterProvider(), clock).reconcile_all()
    JobService(uow_factory, clock).create("credit-risk", CreateJob(name="train", image="t:1"))
    RunService(uow_factory, clock).create("credit-risk", "train")

    health = PlatformService(uow_factory, None, clock).health(minutes=60)
    assert not health.available and "CP_PROMETHEUS_URL" in (health.error or "")
    assert health.status is Health.NO_DATA
    inv = health.inventory
    assert (inv.projects, inv.projects_not_ready, inv.runs_active, inv.runs_waiting) == (1, 0, 1, 1)
    assert inv.oldest_waiting_seconds is not None


def test_an_unreachable_backend_is_reported_not_raised(uow_factory: Factory, clock: Any) -> None:
    telemetry = FakePlatformTelemetry()
    telemetry.fail = "prometheus query failed: connection refused"
    client = TestClient(create_app(uow_factory, clock, platform=telemetry))
    body = client.get("/platform/health?minutes=15").json()
    assert body["available"] is False and "connection refused" in body["error"]
    assert {s["status"] for s in body["signals"]} == {"no_data"}
    assert client.get("/platform/health?minutes=1").status_code == 422
