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
    published = client.post(path, json=body, headers={"Authorization": "Bearer publisher"})
    assert published.status_code == 201
    for detail in [
        f"/projects/catalog-team/data-connections/{connection.id}",
        f"/projects/catalog-team/dataset-versions/{published.json()['id']}",
        "/projects/catalog-team/datasets/customer-features/lineage?version=1",
    ]:
        assert client.get(detail, headers={"Authorization": "Bearer reader"}).status_code == 200
        assert client.get(detail, headers={"Authorization": "Bearer outsider"}).status_code == 403


def test_detail_and_lineage_endpoints_do_not_cross_project_boundaries(env, uow_factory, clock):
    service, connection, _, provider = env
    dataset, _ = service.publish_dataset("catalog-team", **spec(connection))
    other, _ = ProjectService(uow_factory, clock).create(CreateProject("other-data"))
    ProjectReconciler(uow_factory, FakeClusterProvider(), clock).reconcile(other.id)
    client = TestClient(create_app(uow_factory, clock, secrets=provider))
    for path in [f"data-connections/{connection.id}", f"dataset-versions/{dataset.id}"]:
        assert client.get(f"/projects/catalog-team/{path}").status_code == 200
        assert client.get(f"/projects/other-data/{path}").status_code == 404
    graph = client.get(f"/projects/catalog-team/datasets/{dataset.name}/lineage?version=1")
    assert graph.status_code == 200
    assert len(graph.json()["nodes"]) == 1
    assert graph.json()["edges"] == []
    assert not graph.json()["truncated"]


def test_secret_usage_includes_connections_beyond_first_page(env):
    service, connection, secrets, _ = env
    for index in range(205):
        service.create_connection(
            "catalog-team",
            name=f"source-{index:03}",
            endpoint=connection.endpoint,
            bucket=connection.bucket,
            credential_secret=connection.credential_secret,
        )
    uses = secrets.usage("catalog-team")[connection.credential_secret]
    assert len(uses) == 206
    assert all(use.kind == "data_connection" for use in uses)


@pytest.mark.parametrize("producer", ["producer_run_id", "producer_pipeline_run_id"])
def test_postgres_dataset_producer_ownership_and_existing_data_validation(
    env, uow_factory, clock, producer
):
    from sqlalchemy import insert, select, text, update
    from sqlalchemy.exc import IntegrityError

    from controlplane.application.jobs import CreateJob, JobService
    from controlplane.application.pipeline_runs import PipelineRunService
    from controlplane.application.pipelines import CreatePipeline, PipelineService, StepInput
    from controlplane.application.runs import RunService
    from controlplane.persistence.migrate import downgrade, upgrade
    from controlplane.persistence.models import DatasetVersionRow

    with uow_factory() as check:
        if not hasattr(check, "_session"):
            pytest.skip("PostgreSQL constraint test")
        engine = check._session.get_bind()
    foreign, _ = ProjectService(uow_factory, clock).create(CreateProject("foreign-producer"))
    ProjectReconciler(uow_factory, FakeClusterProvider(), clock).reconcile(foreign.id)
    producers = {}
    for project in ["catalog-team", foreign.name]:
        job, _ = JobService(uow_factory, clock).create(project, CreateJob("producer", "worker:1"))
        if producer == "producer_run_id":
            run, _ = RunService(uow_factory, clock).create(project, job.name)
            producers[project] = run.id
        else:
            pipeline, _ = PipelineService(uow_factory, clock).create(
                project, CreatePipeline("producer-pipeline", (StepInput("produce", job.name),))
            )
            view, _ = PipelineRunService(uow_factory, clock).create(project, pipeline.name)
            producers[project] = view.run.id
    service, connection, _, _ = env
    dataset, _ = service.publish_dataset("catalog-team", **spec(connection))
    # Unattributed imports and same-project producers remain valid.
    with uow_factory() as uow:
        session = uow._session
        session.execute(
            update(DatasetVersionRow)
            .where(DatasetVersionRow.id == dataset.id)
            .values(**{producer: producers["catalog-team"]})
        )
        uow.commit()
    with uow_factory() as uow:
        session = uow._session
        row = session.get(DatasetVersionRow, dataset.id)
        values = {column.name: getattr(row, column.name) for column in row.__table__.columns}
        values.update(id=uuid4(), name="foreign-output", **{producer: producers[foreign.name]})
        for statement in [
            insert(DatasetVersionRow).values(**values),
            update(DatasetVersionRow)
            .where(DatasetVersionRow.id == dataset.id)
            .values(**{producer: producers[foreign.name]}),
        ]:
            with pytest.raises(IntegrityError) as caught, session.begin_nested():
                session.execute(statement)
            assert caught.value.orig.sqlstate == "23503"
    # Upgrading an old database with a foreign producer must fail atomically,
    # preserving both its old revision and its data for explicit remediation.
    downgrade(engine, "0028")
    with engine.begin() as conn:
        conn.execute(
            update(DatasetVersionRow)
            .where(DatasetVersionRow.id == dataset.id)
            .values(**{producer: producers[foreign.name]})
        )
    with pytest.raises(IntegrityError):
        upgrade(engine)
    with engine.begin() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0028"
        assert conn.scalar(
            select(getattr(DatasetVersionRow, producer)).where(DatasetVersionRow.id == dataset.id)
        ) == producers[foreign.name]
        conn.execute(
            update(DatasetVersionRow)
            .where(DatasetVersionRow.id == dataset.id)
            .values(**{producer: producers["catalog-team"]})
        )
    upgrade(engine)
