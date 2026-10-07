from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from controlplane.adapters.fakes import FakeClusterProvider, FakeSecretProvider
from controlplane.api.app import create_app
from controlplane.application.data_catalog import DataCatalogService
from controlplane.application.projects import CreateProject, ProjectService
from controlplane.application.secrets import ProjectSecretService
from controlplane.domain.data import DatasetColumn, DatasetFormat
from controlplane.domain.errors import Conflict, InvalidArgument, NotFound
from controlplane.reconciliation.projects import ProjectReconciler


@pytest.fixture
def env(uow_factory, clock):
    project, _ = ProjectService(uow_factory, clock).create(CreateProject("catalog-team"))
    ProjectReconciler(uow_factory, FakeClusterProvider(), clock).reconcile(project.id)
    provider = FakeSecretProvider()
    secrets = ProjectSecretService(uow_factory, provider, clock)
    secrets.put(
        "catalog-team",
        "data-credentials",
        {"AWS_ACCESS_KEY_ID": "key", "AWS_SECRET_ACCESS_KEY": "never-return-this"},
        "Opaque",
    )
    service = DataCatalogService(uow_factory, clock, provider)
    connection, _ = service.create_connection(
        "catalog-team",
        name="training-data",
        endpoint="http://minio:9000",
        bucket="datasets",
        prefix="training",
        credential_secret="data-credentials",
    )
    return service, connection, secrets, provider


def spec(connection, **changes):
    return {
        "name": "customer-features",
        "connection_id": connection.id,
        "uri": "s3://datasets/training/features.csv",
        "format": DatasetFormat.CSV,
        "columns": (DatasetColumn("income", "number"),),
        "checksum_sha256": "a" * 64,
        **changes,
    }


def test_connections_are_immutable_and_protect_secret_deletion(env):
    service, connection, secrets, _ = env
    again, created = service.create_connection(
        "catalog-team",
        **{
            k: v
            for k, v in connection.__dict__.items()
            if k not in {"id", "project_id", "created_at"}
        },
    )
    assert not created and again.id == connection.id
    with pytest.raises(Conflict, match="immutable"):
        service.create_connection(
            "catalog-team",
            name=connection.name,
            endpoint="https://changed.example.com",
            bucket=connection.bucket,
            prefix=connection.prefix,
            credential_secret=connection.credential_secret,
        )
    with pytest.raises(Conflict, match="referenced"):
        secrets.delete("catalog-team", connection.credential_secret, "1")


def test_dataset_versions_freeze_schema_integrity_and_use_cas(env):
    service, connection, _, _ = env
    first, created = service.publish_dataset("catalog-team", **spec(connection))
    assert created and first.version == 1
    assert service.publish_dataset("catalog-team", **spec(connection)) == (first, False)
    with pytest.raises(Conflict, match="changed"):
        service.publish_dataset("catalog-team", **spec(connection, checksum_sha256="b" * 64))
    second, _ = service.publish_dataset(
        "catalog-team", expected_latest_version=1, **spec(connection, checksum_sha256="b" * 64)
    )
    assert second.version == 2
    assert service.dataset("catalog-team", first.name, 1) == first
    assert service.dataset("catalog-team", first.name) == second


@pytest.mark.parametrize(
    "changes",
    [
        {"checksum_sha256": None},
        {"uri": "s3://other/training/data.csv"},
        {"uri": "s3://datasets/unscoped.csv"},
        {"columns": ()},
        {"object_version_id": "null", "checksum_sha256": None},
        {"columns": (DatasetColumn("a", "number"), DatasetColumn("a", "number"))},
    ],
)
def test_invalid_dataset_is_rejected_before_persistence(env, changes):
    service, connection, _, _ = env
    with pytest.raises(InvalidArgument):
        service.publish_dataset("catalog-team", **spec(connection, **changes))
    assert service.datasets("catalog-team") == []


def test_api_returns_metadata_without_credentials_and_checks_project_scope(env, uow_factory, clock):
    service, connection, _, provider = env
    client = TestClient(create_app(uow_factory, clock, secrets=provider))
    response = client.get("/projects/catalog-team/data-connections")
    assert response.status_code == 200
    assert "never-return-this" not in response.text
    value = spec(connection)
    value["connection_id"] = str(connection.id)
    value["columns"] = [{"name": "income", "dtype": "number"}]
    response = client.post("/projects/catalog-team/datasets", json=value)
    assert response.status_code == 201
    assert response.json()["version"] == 1
    assert client.post("/projects/catalog-team/datasets", json=value).status_code == 200
    with pytest.raises(NotFound):
        service.publish_dataset("catalog-team", **spec(replace(connection, id=uuid4())))


def test_two_publishers_cannot_allocate_the_same_dataset_version(pg_engine, clock):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from controlplane.persistence.migrate import upgrade
    from controlplane.persistence.sql import SqlUnitOfWork, sql_uow_factory

    upgrade(pg_engine)
    sessions = sql_uow_factory(pg_engine)

    def factory():
        return SqlUnitOfWork(sessions)

    project, _ = ProjectService(factory, clock).create(CreateProject("race-catalog"))
    ProjectReconciler(factory, FakeClusterProvider(), clock).reconcile(project.id)
    provider = FakeSecretProvider()
    ProjectSecretService(factory, provider, clock).put(
        "race-catalog",
        "credentials",
        {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"},
        "Opaque",
    )
    service = DataCatalogService(factory, clock, provider)
    connection, _ = service.create_connection(
        "race-catalog",
        name="source-data",
        endpoint="http://minio:9000",
        bucket="datasets",
        prefix="training",
        credential_secret="credentials",
    )
    barrier = Barrier(2)

    def publish(digest):
        barrier.wait()
        try:
            return service.publish_dataset(
                "race-catalog", **spec(connection, checksum_sha256=digest * 64)
            )[0]
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, ["a", "b"]))
    assert sum(result is not None for result in results) == 1
    assert len(service.datasets("race-catalog")) == 1


def test_catalog_viewer_operator_and_other_project_boundaries(env, uow_factory, clock):
    from controlplane.api.auth import AuthConfig
    from controlplane.domain.access import Membership, Principal, ProjectRole

    service, connection, _, provider = env

    class Authenticator:
        def authenticate(self, token):
            return Principal(username=token)

    with uow_factory() as uow:
        for name, role in [("reader", ProjectRole.VIEWER), ("publisher", ProjectRole.OPERATOR)]:
            uow.memberships.add(
                Membership.create(
                    project_id=connection.project_id, subject="user:" + name, role=role, now=clock()
                )
            )
        uow.commit()
    client = TestClient(
        create_app(
            uow_factory, clock, secrets=provider, auth=AuthConfig(authenticator=Authenticator())
        )
    )
    body = spec(connection)
    body["connection_id"] = str(connection.id)
    body["columns"] = [{"name": "income", "dtype": "number"}]
    path = "/projects/catalog-team/datasets"
    assert client.get(path, headers={"Authorization": "Bearer reader"}).status_code == 200
    assert (
        client.post(path, json=body, headers={"Authorization": "Bearer reader"}).status_code == 403
    )
    assert client.get(path, headers={"Authorization": "Bearer outsider"}).status_code == 403
    assert (
        client.post(path, json=body, headers={"Authorization": "Bearer publisher"}).status_code
        == 201
    )
