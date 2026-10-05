"""Notification scope, incident identity and durable read receipts."""

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from controlplane.application.notifications import NotificationService
from controlplane.application.ports import UnitOfWork
from controlplane.domain.access import Membership, Principal, ProjectRole
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import JobDefinition, Run
from controlplane.domain.states import DeploymentStatus, EndpointStatus, ProjectStatus, RunStatus
from controlplane.persistence.models import NotificationReadRow
from controlplane.persistence.sql import SqlNotificationReads
from controlplane.tests.conftest import FakeClock


def fail_project(
    client: TestClient,
    factory: Callable[[], UnitOfWork],
    clock: FakeClock,
    name: str = "test-project",
) -> UUID:
    response = client.post("/projects", json={"name": name})
    assert response.status_code == 201
    project_id = UUID(response.json()["id"])
    with factory() as uow:
        project = uow.projects.get(project_id)
        assert project
        uow.projects.update(
            replace(project, status=ProjectStatus.FAILED, updated_at=clock()),
            expected_status=project.status,
        )
        uow.audit.record(
            AuditEvent(
                occurred_at=clock(),
                actor="test",
                action="project.failed",
                entity_type="project",
                entity_id=project_id,
                project_id=project_id,
            )
        )
        uow.commit()
    return project_id


def test_polling_keeps_incident_ids_and_read_state(
    client: TestClient, uow_factory: Callable[[], UnitOfWork], clock: FakeClock
) -> None:
    fail_project(client, uow_factory, clock)
    first = client.get("/me/notifications").json()
    assert len(first["items"]) == first["unread_count"] == first["attention_count"] == 1
    key = first["items"][0]["id"]
    assert client.post("/me/notifications/read", json={"ids": [key, key]}).json()["marked"] == 1
    clock.advance(timedelta(minutes=5))
    with uow_factory() as uow:
        project = uow.projects.list(limit=1, offset=0)[0]
        uow.projects.update(project.with_gpu_quota(2, clock()), expected_status=project.status)
        uow.commit()
    refreshed = client.get("/me/notifications").json()
    assert refreshed["items"][0]["id"] == key
    assert refreshed["items"][0]["read"] and refreshed["unread_count"] == 0
    assert refreshed["attention_count"] == 1  # reading never resolves a problem


def test_read_receipts_are_personal_and_hidden_resources_cannot_be_marked(
    client: TestClient, uow_factory: Callable[[], UnitOfWork], clock: FakeClock
) -> None:
    project = fail_project(client, uow_factory, clock)
    service = NotificationService(uow_factory, clock)
    with uow_factory() as uow:
        uow.memberships.add(
            Membership.create(
                project_id=project, subject="user:alice", role=ProjectRole.VIEWER, now=clock()
            )
        )
        uow.memberships.add(
            Membership.create(
                project_id=project, subject="user:caller", role=ProjectRole.INVOKER, now=clock()
            )
        )
        uow.commit()
    alice = Principal(username="alice")
    hidden = Principal(username="bob")
    admin = Principal(username="root", platform_admin=True)
    key = service.list(alice)[0].id
    assert service.list(hidden) == service.list(Principal(username="caller")) == []
    assert service.mark_read(hidden, [key]) == 0
    assert service.mark_read(alice, [key]) == 1
    assert service.list(alice)[0].read and not service.list(admin)[0].read
    assert service.mark_read(admin, [], all_notifications=True) == 1
    with uow_factory() as uow:
        uow.memberships.remove(project, "user:alice")
        uow.commit()
    assert service.list(alice) == []  # persisted receipts never expose revoked resources


def test_recent_failure_uses_finish_time_and_recovery_starts_new_incident(
    client: TestClient, uow_factory: Callable[[], UnitOfWork], clock: FakeClock
) -> None:
    project_id = fail_project(client, uow_factory, clock)
    old = clock() - timedelta(days=2)
    now = clock()
    with uow_factory() as uow:
        job = JobDefinition.create(
            project_id=project_id,
            name="long-training",
            image="train:v1",
            command=(),
            resources={},
            env={},
            now=old,
        )
        uow.jobs.add(job)
        uow.runs.add(
            Run(
                project_id=project_id,
                job_definition_id=job.id,
                status=RunStatus.FAILED,
                created_at=old,
                started_at=old,
                updated_at=now,
                finished_at=now,
            )
        )
        uow.commit()
    feed = client.get("/me/notifications").json()
    assert any(n["title"] == "long-training failed" for n in feed["items"])
    client.post("/me/notifications/read", json={"all": True})
    with uow_factory() as uow:
        project = uow.projects.get(project_id)
        assert project
        uow.projects.update(
            replace(project, status=ProjectStatus.READY, updated_at=clock()),
            expected_status=project.status,
        )
        uow.commit()
    assert all(n["kind"] != "project" for n in client.get("/me/notifications").json()["items"])
    with uow_factory() as uow:
        project = uow.projects.get(project_id)
        assert project
        uow.projects.update(
            replace(project, status=ProjectStatus.FAILED, updated_at=clock()),
            expected_status=project.status,
        )
        uow.audit.record(
            AuditEvent(
                occurred_at=clock(),
                actor="test",
                action="project.failed",
                entity_type="project",
                entity_id=project_id,
                project_id=project_id,
            )
        )
        uow.commit()
    new = client.get("/me/notifications").json()
    assert new["unread_count"] == 1
    clock.advance(timedelta(days=2))
    assert all(n["kind"] != "run" for n in client.get("/me/notifications").json()["items"])


def test_bad_deployment_and_endpoint_are_one_actionable_incident(
    client: TestClient, uow_factory: Callable[[], UnitOfWork], clock: FakeClock
) -> None:
    project = client.post("/projects", json={"name": "serving-project"}).json()
    with uow_factory() as uow:
        ready = uow.projects.get(UUID(project["id"]))
        assert ready
        uow.projects.update(
            replace(ready, status=ProjectStatus.READY), expected_status=ready.status
        )
        uow.commit()
    deployed = client.post("/projects/serving-project/deployments", json={"name": "test-prod"})
    assert deployed.status_code == 201
    with uow_factory() as uow:
        deployment = uow.deployments.get(UUID(deployed.json()["id"]))
        assert deployment
        endpoint = uow.endpoints.get_by_deployment(deployment.id)
        assert endpoint
        uow.deployments.update(
            replace(deployment, status=DeploymentStatus.FAILED, updated_at=clock()),
            expected_status=deployment.status,
        )
        uow.endpoints.update(
            replace(endpoint, status=EndpointStatus.UNAVAILABLE, updated_at=clock()),
            expected_status=endpoint.status,
        )
        uow.commit()
    items = client.get("/me/notifications").json()["items"]
    assert len(items) == 1 and items[0]["kind"] == "deployment"
    assert items[0]["endpoint_name"] == "test-prod" and items[0]["project"] == project["name"]
    with uow_factory() as uow:
        uow.deployments.update(
            replace(deployment, status=DeploymentStatus.READY, updated_at=clock()),
            expected_status=DeploymentStatus.FAILED,
        )
        uow.commit()
    assert client.get("/me/notifications").json()["items"][0]["kind"] == "endpoint"


def test_read_payload_cannot_select_another_user(client: TestClient) -> None:
    assert (
        client.post("/me/notifications/read", json={"username": "someone", "all": True}).status_code
        == 422
    )
    assert client.post("/me/notifications/read", json={"ids": ["invalid"]}).status_code == 422


def test_sql_receipts_are_idempotent_durable_and_transactional() -> None:
    # This port only needs portable scalar columns. Exercise real SQL even when the
    # optional PostgreSQL integration fixture is not installed.
    engine = create_engine("sqlite://")
    NotificationReadRow.metadata.tables["notification_reads"].create(engine)
    sessions = sessionmaker(engine)
    at = FakeClock()()
    with sessions.begin() as session:
        reads = SqlNotificationReads(session)
        reads.mark("alice", ["a" * 64, "a" * 64], at)
        reads.mark("alice", ["a" * 64], at)
    with sessions() as session:
        reads = SqlNotificationReads(session)
        assert reads.find("alice", ["a" * 64]) == {"a" * 64}
        assert reads.find("bob", ["a" * 64]) == set()
        reads.mark("alice", ["b" * 64], at)
        session.rollback()
    with sessions() as session:
        assert SqlNotificationReads(session).find("alice", ["b" * 64]) == set()
    engine.dispose()
