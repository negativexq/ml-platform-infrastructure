import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from controlplane.adapters.fakes import FakeWorkflowProvider
from controlplane.adapters.workflow.argo import build_workflow
from controlplane.api.app import create_app
from controlplane.application.batch_inference import BatchInferenceService
from controlplane.application.providers import ExternalState, WorkflowStatus
from controlplane.application.runs import RunService
from controlplane.domain.entities import Model, ModelVersion
from controlplane.domain.errors import Conflict, InvalidArgument, NotFound
from controlplane.domain.states import RunStatus
from controlplane.reconciliation.runs import RunReconciler
from controlplane.tests.test_data_catalog import env, spec  # noqa: F401

IMAGE = "registry/batch@sha256:" + "a" * 64


@pytest.fixture
def batch(request, uow_factory, clock):
    catalog_env = request.getfixturevalue("env")
    catalog, connection, _, provider = catalog_env
    dataset, _ = catalog.publish_dataset("catalog-team", **spec(connection))
    with uow_factory() as uow:
        model = Model(project_id=connection.project_id, name="scorer", created_at=clock())
        version = ModelVersion(
            model_id=model.id,
            version=1,
            source_uri="s3://datasets/training/model",
            created_at=clock(),
            updated_at=clock(),
        )
        uow.models.add(model)
        uow.model_versions.add(version)
        uow.commit()
    service = BatchInferenceService(uow_factory, IMAGE, secrets=provider, clock=clock)
    fields = dict(
        name="daily-scoring",
        input_dataset_id=dataset.id,
        model_version_id=version.id,
        model_connection_id=connection.id,
        output_connection_id=connection.id,
        output_dataset="predictions",
        features=["income"],
    )
    return service, fields, catalog, provider


def result(job, run, **changes):
    return json.dumps(
        dict(
            execution=f"{run.id}/main",
            uri=f"s3://datasets/training/daily-scoring/{run.id}/main.parquet",
            format="PARQUET",
            columns=job.batch_spec["input"]["columns"]
            + [dict(name="prediction", dtype="number", nullable=False)],
            row_count=3,
            checksum_sha256="b" * 64,
            **changes,
        )
    )


def test_managed_definition_is_immutable_and_uses_secret_refs(batch, uow_factory, clock):
    service, fields, _, provider = batch
    job, created = service.create("catalog-team", **fields)
    assert created and job.batch_spec["model_version_id"] == str(fields["model_version_id"])
    assert service.create("catalog-team", **fields) == (job, False)
    with pytest.raises(Conflict):
        service.create("catalog-team", **{**fields, "batch_size": 20})
    assert "never-return-this" not in json.dumps(job.batch_spec)
    run, _ = RunService(uow_factory, clock).create("catalog-team", job.name)
    workflow = FakeWorkflowProvider()
    RunReconciler(uow_factory, workflow, clock).reconcile(run.id)
    manifest = build_workflow(next(iter(workflow.submitted.values())))
    template = manifest["spec"]["templates"][0]
    assert template["outputs"]["parameters"][0]["valueFrom"]["path"] == "/tmp/mlp-result.json"
    assert any(
        e["name"] == "BATCH_MODEL_SECRET_ACCESS_KEY" and "valueFrom" in e
        for e in template["container"]["env"]
    )
    with TestClient(create_app(uow_factory, clock, secrets=provider, batch_image=IMAGE)) as client:
        response = client.get("/projects/catalog-team/batch-inference")
        assert response.status_code == 200 and response.json()["items"][0]["id"] == str(job.id)


@pytest.mark.parametrize(
    "changed",
    [
        {"input_dataset_id": uuid4()},
        {"model_connection_id": uuid4()},
        {"features": ["missing"]},
        {"max_bytes": 0},
    ],
)
def test_rejects_invalid_scope_and_limits(batch, changed):
    service, fields, _, _ = batch
    with pytest.raises((NotFound, InvalidArgument)):
        service.create("catalog-team", **{**fields, **changed})


def test_completion_publishes_once_and_retry_gets_new_output(batch, uow_factory, clock):
    service, fields, catalog, _ = batch
    job, _ = service.create("catalog-team", **fields)
    runs = RunService(uow_factory, clock)
    workflow = FakeWorkflowProvider()
    reconciler = RunReconciler(uow_factory, workflow, clock)
    run, _ = runs.create("catalog-team", job.name, idempotency_key="one")
    reconciler.reconcile(run.id)
    run = runs.get(run.id)
    workflow._status[run.external_ref] = WorkflowStatus(
        ExternalState.SUCCEEDED, results={"main": result(job, run)}
    )
    assert reconciler.reconcile(run.id).after == RunStatus.SUCCEEDED
    reconciler.reconcile(run.id)
    output = catalog.dataset("catalog-team", "predictions")
    assert output.producer_run_id == run.id and output.row_count == 3
    assert len(catalog.datasets("catalog-team", "predictions")) == 1
    retry, _ = runs.retry(run.id)
    assert retry.id != run.id and retry.job_definition_id == job.id


@pytest.mark.parametrize("raw", [None, "{}", '"bad"', "x" * 65537])
def test_bad_result_fails_without_dataset(batch, uow_factory, clock, raw):
    service, fields, catalog, _ = batch
    job, _ = service.create("catalog-team", **fields)
    runs = RunService(uow_factory, clock)
    workflow = FakeWorkflowProvider()
    reconciler = RunReconciler(uow_factory, workflow, clock)
    run, _ = runs.create("catalog-team", job.name)
    reconciler.reconcile(run.id)
    workflow._status[runs.get(run.id).external_ref] = WorkflowStatus(
        ExternalState.SUCCEEDED, results={"main": raw} if raw else {}
    )
    assert reconciler.reconcile(run.id).after == RunStatus.FAILED
    assert catalog.datasets("catalog-team", "predictions") == []


@pytest.mark.parametrize("valid", [True, False])
def test_pipeline_batch_output_and_invalid_result_fail_pipeline(batch, uow_factory, clock, valid):
    from controlplane.application.pipeline_runs import PipelineRunService
    from controlplane.application.pipelines import CreatePipeline, PipelineService, StepInput
    from controlplane.reconciliation.pipeline_runs import PipelineRunReconciler

    service, fields, catalog, _ = batch
    job, _ = service.create("catalog-team", **fields)
    definition, _ = PipelineService(uow_factory, clock).create(
        "catalog-team", CreatePipeline(name="batch-pipeline", steps=[StepInput("score", job.name)])
    )
    runs = PipelineRunService(uow_factory, clock)
    run, _ = runs.create("catalog-team", definition.name)
    workflow = FakeWorkflowProvider()
    reconciler = PipelineRunReconciler(uow_factory, workflow, clock=clock)
    run = run.run
    reconciler.reconcile(run.id)
    run = runs.view(run.id).run
    raw = json.loads(result(job, run))
    raw["execution"] = f"{run.id}/score"
    raw["uri"] = f"s3://datasets/training/daily-scoring/{run.id}/score.parquet"
    workflow._status[run.external_ref] = WorkflowStatus(
        ExternalState.SUCCEEDED,
        {"score": ExternalState.SUCCEEDED},
        results={"score": json.dumps(raw) if valid else "{}"},
    )
    assert reconciler.reconcile(run.id).after == (
        RunStatus.SUCCEEDED if valid else RunStatus.FAILED
    )
    if valid:
        output = catalog.dataset("catalog-team", "predictions")
        assert output.producer_pipeline_run_id == run.id
        assert catalog.datasets("catalog-team", producer_pipeline_run_id=run.id) == [output]
        assert not catalog.datasets("catalog-team", producer_run_id=uuid4())
    else:
        assert not catalog.datasets("catalog-team", "predictions")


def test_output_and_run_status_roll_back_together(batch, uow_factory, clock):
    service, fields, catalog, _ = batch
    job, _ = service.create("catalog-team", **fields)
    runs, workflow = RunService(uow_factory, clock), FakeWorkflowProvider()
    run, _ = runs.create("catalog-team", job.name)
    reconciler = RunReconciler(uow_factory, workflow, clock)
    reconciler.reconcile(run.id)
    run = runs.get(run.id)
    workflow.set_state(run.external_ref, ExternalState.RUNNING)
    reconciler.reconcile(run.id)
    run = runs.get(run.id)
    workflow._status[run.external_ref] = WorkflowStatus(
        ExternalState.SUCCEEDED, results={"main": result(job, run)}
    )
    from contextlib import contextmanager

    @contextmanager
    def crashing_factory():
        with uow_factory() as uow:

            def fail():
                raise RuntimeError("simulated pre-commit crash")

            uow.commit = fail
            yield uow

    with pytest.raises(RuntimeError, match="pre-commit"):
        RunReconciler(crashing_factory, workflow, clock).reconcile(run.id)
    assert runs.get(run.id).status == RunStatus.RUNNING
    assert not catalog.datasets("catalog-team", "predictions")
    reconciler.reconcile(run.id)
    assert runs.get(run.id).status == RunStatus.SUCCEEDED
    assert len(catalog.datasets("catalog-team", "predictions")) == 1


def test_batch_api_enforces_project_roles(batch, uow_factory, clock):
    from controlplane.api.auth import AuthConfig
    from controlplane.domain.access import Membership, Principal, ProjectRole

    _, fields, _, provider = batch
    with uow_factory() as uow:
        project = uow.projects.get_by_name("catalog-team")
        for name, role in [("reader", ProjectRole.VIEWER), ("publisher", ProjectRole.OPERATOR)]:
            uow.memberships.add(
                Membership.create(
                    project_id=project.id, subject="user:" + name, role=role, now=clock()
                )
            )
        uow.commit()

    class Authenticator:
        def authenticate(self, token):
            return Principal(username=token)

    client = TestClient(
        create_app(
            uow_factory,
            clock,
            secrets=provider,
            batch_image=IMAGE,
            auth=AuthConfig(authenticator=Authenticator()),
        )
    )
    path = "/projects/catalog-team/batch-inference"
    body = json.loads(json.dumps(fields, default=str))
    assert client.get(path, headers={"Authorization": "Bearer reader"}).status_code == 200
    assert (
        client.post(path, json=body, headers={"Authorization": "Bearer reader"}).status_code == 403
    )
    assert client.get(path, headers={"Authorization": "Bearer outsider"}).status_code == 403
    assert (
        client.post(path, json=body, headers={"Authorization": "Bearer publisher"}).status_code
        == 201
    )


@pytest.mark.parametrize("telemetry", [False, True])
def test_committed_batch_lineage_pins_input_model_and_run(batch, uow_factory, clock, telemetry):
    service, fields, catalog, provider = batch
    job, _ = service.create("catalog-team", **fields)
    runs, workflow = RunService(uow_factory, clock), FakeWorkflowProvider()
    run, _ = runs.create("catalog-team", job.name)
    reconciler = RunReconciler(uow_factory, workflow, clock)
    reconciler.reconcile(run.id)
    run = runs.get(run.id)
    workflow._status[run.external_ref] = WorkflowStatus(
        ExternalState.SUCCEEDED, results={"main": result(job, run)}
    )
    reconciler.reconcile(run.id)
    if telemetry:
        from controlplane.application.data_catalog import DataCatalogService
        from controlplane.observability.uow import observed_uow_factory

        uow_factory = observed_uow_factory(uow_factory)
        catalog = DataCatalogService(uow_factory, clock, provider)
    graph = catalog.lineage("catalog-team", "predictions", 1)
    assert {node["kind"] for node in graph["nodes"]} == {"DATASET", "MODEL_VERSION", "JOB_RUN"}
    assert {edge["relation"] for edge in graph["edges"]} == {"INPUT", "OUTPUT", "MODEL"}
    assert (
        next(n for n in graph["nodes"] if n["kind"] == "MODEL_VERSION")["ref_id"]
        == fields["model_version_id"]
    )
    assert next(n for n in graph["nodes"] if n["id"] == graph["root"])["name"] == "predictions"
    client = TestClient(create_app(uow_factory, clock, secrets=provider))
    assert (
        client.get("/projects/catalog-team/datasets/predictions/lineage?version=1").status_code
        == 200
    )
