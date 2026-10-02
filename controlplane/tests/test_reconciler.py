"""Reconciler behaviour against the in-memory cluster. The real-cluster gate
(scripts/envtest.sh) is exercised separately."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from controlplane.adapters.fakes import FakeClusterProvider
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import CreateProject, ProjectService
from controlplane.domain.states import ProjectStatus
from controlplane.reconciliation.projects import ProjectReconciler

Factory = Callable[[], UnitOfWork]


@pytest.fixture
def cluster() -> FakeClusterProvider:
    return FakeClusterProvider()


@pytest.fixture
def svc(uow_factory: Factory, clock: Callable[[], Any]) -> ProjectService:
    return ProjectService(uow_factory, clock)


@pytest.fixture
def rec(
    uow_factory: Factory, cluster: FakeClusterProvider, clock: Callable[[], Any]
) -> ProjectReconciler:
    return ProjectReconciler(uow_factory, cluster, clock)


def _actions(uow_factory: Factory, project_id: Any) -> list[str]:
    with uow_factory() as uow:
        return [e.action for e in uow.audit.list(project_id=project_id)]


def test_pending_project_becomes_ready_with_a_namespace(
    svc: ProjectService, rec: ProjectReconciler, cluster: FakeClusterProvider, uow_factory: Factory
) -> None:
    project, _ = svc.create(CreateProject(name="credit-risk"))
    result = rec.reconcile(project.id)
    assert (result.before, result.after) == (ProjectStatus.PENDING, ProjectStatus.READY)
    assert "mlp-credit-risk" in cluster.namespaces
    assert svc.get(project.id).status is ProjectStatus.READY
    assert _actions(uow_factory, project.id) == [
        "project.created",
        "project.provisioning",
        "project.provisioned",
    ]


def test_reconcile_is_idempotent(
    svc: ProjectService, rec: ProjectReconciler, cluster: FakeClusterProvider, uow_factory: Factory
) -> None:
    project, _ = svc.create(CreateProject(name="credit-risk"))
    rec.reconcile(project.id)
    mutations, events = cluster.mutations, len(_actions(uow_factory, project.id))
    for _ in range(10):
        assert rec.reconcile(project.id).after is ProjectStatus.READY
    assert cluster.mutations == mutations
    assert len(_actions(uow_factory, project.id)) == events


def test_deleted_namespace_is_detected_and_restored(
    svc: ProjectService, rec: ProjectReconciler, cluster: FakeClusterProvider, uow_factory: Factory
) -> None:
    project, _ = svc.create(CreateProject(name="credit-risk"))
    rec.reconcile(project.id)
    cluster.remove_namespace("mlp-credit-risk")
    assert rec.reconcile(project.id).after is ProjectStatus.READY
    assert "mlp-credit-risk" in cluster.namespaces
    assert "project.drift_detected" in _actions(uow_factory, project.id)


def test_failed_provisioning_never_reaches_ready(
    svc: ProjectService, rec: ProjectReconciler, cluster: FakeClusterProvider
) -> None:
    project, _ = svc.create(CreateProject(name="credit-risk"))
    cluster.fail_apply = RuntimeError("quota admission denied")
    assert rec.reconcile(project.id).after is ProjectStatus.FAILED
    failed = svc.get(project.id)
    assert failed.status_reason and "quota admission denied" in failed.status_reason
    cluster.fail_apply = None
    assert rec.reconcile(project.id).after is ProjectStatus.READY  # retried


def test_apply_that_changes_nothing_is_not_trusted(
    svc: ProjectService, rec: ProjectReconciler, cluster: FakeClusterProvider
) -> None:
    project, _ = svc.create(CreateProject(name="credit-risk"))
    cluster.silently_incomplete = True
    assert rec.reconcile(project.id).after is ProjectStatus.FAILED


def test_delete_cleans_up_and_hides_the_project(
    svc: ProjectService, rec: ProjectReconciler, cluster: FakeClusterProvider, uow_factory: Factory
) -> None:
    project, _ = svc.create(CreateProject(name="credit-risk"))
    rec.reconcile(project.id)
    assert svc.request_delete(project.id).status is ProjectStatus.DELETING
    assert svc.request_delete(project.id).status is ProjectStatus.DELETING  # idempotent
    assert rec.reconcile(project.id).after is ProjectStatus.DELETED
    assert cluster.namespaces == {}
    assert svc.list() == []
    assert _actions(uow_factory, project.id)[-2:] == ["project.delete_requested", "project.deleted"]


def test_delete_refuses_a_namespace_the_project_does_not_own(
    svc: ProjectService, rec: ProjectReconciler, cluster: FakeClusterProvider
) -> None:
    project, _ = svc.create(CreateProject(name="credit-risk"))
    cluster.namespaces["mlp-credit-risk"] = {"namespace"}  # created by someone else
    svc.request_delete(project.id)
    assert rec.reconcile(project.id).after is ProjectStatus.DELETING
    assert "mlp-credit-risk" in cluster.namespaces
    assert "not owned" in (svc.get(project.id).status_reason or "")
