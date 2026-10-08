import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from controlplane.adapters.fakes import FakeWorkflowProvider
from controlplane.api.app import create_app
from controlplane.application.model_monitoring import ModelMonitoringService
from controlplane.application.providers import ExternalState, WorkflowStatus
from controlplane.application.runs import RunService
from controlplane.domain.errors import Conflict, InvalidArgument, NotFound
from controlplane.domain.states import RunStatus
from controlplane.observability.uow import observed_uow_factory
from controlplane.reconciliation.runs import RunReconciler
from controlplane.tests.test_batch_inference import IMAGE, batch  # noqa: F401
from controlplane.tests.test_data_catalog import env  # noqa: F401


@pytest.fixture
def monitoring(request, uow_factory, clock):
    batch_service, fields, catalog, provider = request.getfixturevalue("batch")
    factory = observed_uow_factory(uow_factory)
    service = ModelMonitoringService(factory, IMAGE, provider, clock)
    fields = {
        "name": "model-quality",
        "model_version_id": fields["model_version_id"],
        "reference_dataset_id": fields["input_dataset_id"],
        "observed_dataset_id": fields["input_dataset_id"],
        "features": ["income"],
        "minimum_rows": 3,
    }
    return service, fields, provider, factory


def report(job, run, **changes):
    spec = job.monitoring_spec
    return json.dumps(
        {
            "execution": f"{run.id}/main",
            "model_version_id": spec["model_version_id"],
            "reference_dataset_id": spec["reference_dataset_id"],
            "observed_dataset_id": spec["observed_dataset_id"],
            "feedback_dataset_id": None,
            "status": "STABLE",
            "reference_rows": 5,
            "observed_rows": 5,
            "features": [
                {
                    "name": "income",
                    "dtype": "number",
                    "psi": 0.0,
                    "reference_missing_rate": 0.0,
                    "observed_missing_rate": 0.0,
                    "missing_rate_change": 0.0,
                    "drifted": False,
                    "status": "STABLE",
                }
            ],
            "performance": None,
            **changes,
        }
    )


def complete(monitoring, clock, raw=None):
    service, fields, _, factory = monitoring
    job, _ = service.create("catalog-team", **fields)
    runs, workflow = RunService(factory, clock), FakeWorkflowProvider()
    run, _ = runs.create("catalog-team", job.name)
    reconciler = RunReconciler(factory, workflow, clock)
    reconciler.reconcile(run.id)
    run = runs.get(run.id)
    workflow._status[run.external_ref] = WorkflowStatus(
        ExternalState.SUCCEEDED, results={"main": raw if raw is not None else report(job, run)}
    )
    return service, job, run, runs, reconciler


def test_immutable_monitoring_uses_secret_refs_and_argo_result(monitoring, clock):
    service, fields, _, factory = monitoring
    job, created = service.create("catalog-team", **fields)
    assert created and service.create("catalog-team", **fields) == (job, False)
    with pytest.raises(Conflict):
        service.create("catalog-team", **{**fields, "minimum_rows": 4})
    assert "never-return-this" not in json.dumps(job.monitoring_spec)
    from controlplane.adapters.workflow.argo import build_workflow
    from controlplane.application.workflow_compiler import compile_job_run

    run, _ = RunService(factory, clock).create("catalog-team", job.name)
    with factory() as uow:
        project = uow.projects.get(job.project_id)
    template = build_workflow(compile_job_run(project, job, run))["spec"]["templates"][0]
    assert template["outputs"]["parameters"][0]["valueFrom"]["path"] == "/tmp/mlp-result.json"
    assert any(
        e["name"] == "BATCH_REFERENCE_SECRET_ACCESS_KEY" and "valueFrom" in e
        for e in template["container"]["env"]
    )
    assert any(e["name"] == "MLP_MONITORING_SPEC" for e in template["container"]["env"])


def test_completion_publishes_once_and_retry_gets_a_new_report(monitoring, clock):
    service, job, run, runs, reconciler = complete(monitoring, clock)
    assert reconciler.reconcile(run.id).after == RunStatus.SUCCEEDED
    reconciler.reconcile(run.id)
    reports = service.reports("catalog-team")
    assert len(reports) == 1 and reports[0].job_run_id == run.id
    assert service.reports("catalog-team", job_run_id=run.id) == reports
    assert service.reports("catalog-team", job_run_id=uuid4()) == []
    retry, _ = runs.retry(run.id)
    assert retry.id != run.id
    assert service.reports("catalog-team", uuid4()) == []


@pytest.mark.parametrize(
    "change",
    [
        {"observed_dataset_id": str(uuid4())},
        {"execution": "forged/main"},
        {"reference_rows": True},
        {"reference_rows": 100000001},
        {"status": "DRIFTED"},
        {"features": []},
    ],
)
def test_invalid_report_fails_without_publication(monitoring, clock, change):
    service, job, run, runs, reconciler = complete(monitoring, clock)
    reconciler._workflow._status[run.external_ref] = WorkflowStatus(
        ExternalState.SUCCEEDED, results={"main": report(job, run, **change)}
    )
    assert reconciler.reconcile(run.id).after == RunStatus.FAILED
    assert not service.reports("catalog-team")


def test_precommit_crash_rolls_back_report_and_run(monitoring, clock):
    from contextlib import contextmanager

    service, job, run, runs, reconciler = complete(monitoring, clock)
    factory = monitoring[3]

    @contextmanager
    def crashing():
        with factory() as uow:

            def fail():
                raise RuntimeError("pre-commit crash")

            uow.commit = fail
            yield uow

    with pytest.raises(RuntimeError, match="pre-commit"):
        RunReconciler(crashing, reconciler._workflow, clock).reconcile(run.id)
    assert not service.reports("catalog-team")
    assert runs.get(run.id).status != RunStatus.SUCCEEDED
    reconciler.reconcile(run.id)
    assert len(service.reports("catalog-team")) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"model_version_id": uuid4()},
        {"observed_dataset_id": uuid4()},
        {"features": ["absent"]},
        {"features": ["income", "income"]},
        {"psi_threshold": float("nan")},
        {"minimum_rows": 0},
        {"feedback_dataset_id": uuid4()},
    ],
)
def test_invalid_monitoring_spec_rejected(monitoring, change):
    service, fields, _, _ = monitoring
    with pytest.raises((NotFound, InvalidArgument)):
        service.create("catalog-team", **{**fields, **change})


def test_monitoring_api_observed_uow_and_role_boundaries(monitoring, clock):
    from controlplane.api.auth import AuthConfig
    from controlplane.domain.access import Membership, Principal, ProjectRole

    service, fields, provider, factory = monitoring
    with factory() as uow:
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
            factory,
            clock,
            secrets=provider,
            batch_image=IMAGE,
            auth=AuthConfig(authenticator=Authenticator()),
        )
    )
    path = "/projects/catalog-team/model-monitoring/checks"
    body = json.loads(json.dumps(fields, default=str))
    assert (
        client.post(path, json=body, headers={"Authorization": "Bearer reader"}).status_code == 403
    )
    assert (
        client.post(path, json=body, headers={"Authorization": "Bearer publisher"}).status_code
        == 201
    )
    assert client.get(path, headers={"Authorization": "Bearer reader"}).status_code == 200
    assert client.get(path, headers={"Authorization": "Bearer outsider"}).status_code == 403
    service, job, run, runs, reconciler = complete(monitoring, clock)
    reconciler.reconcile(run.id)
    report = service.reports("catalog-team")[0]
    path = "/projects/catalog-team/model-monitoring/reports/" + str(report.id)
    assert client.get(path, headers={"Authorization": "Bearer reader"}).status_code == 200
    assert client.get(path, headers={"Authorization": "Bearer outsider"}).status_code == 403


def test_drift_is_a_successful_measurement_not_failed_execution(monitoring, clock):
    service, job, run, runs, reconciler = complete(monitoring, clock)
    value = json.loads(report(job, run))
    value["status"] = "DRIFTED"
    value["features"][0].update(psi=0.9, drifted=True, status="DRIFTED")
    reconciler._workflow._status[run.external_ref] = WorkflowStatus(
        ExternalState.SUCCEEDED, results={"main": json.dumps(value)}
    )
    assert reconciler.reconcile(run.id).after == RunStatus.SUCCEEDED
    stored = service.reports("catalog-team")[0]
    assert stored.status == "DRIFTED"
    from controlplane.application.notifications import NotificationService
    from controlplane.domain.access import Principal

    inbox = NotificationService(monitoring[3], clock)
    alerts = inbox.list(Principal(username="admin", platform_admin=True))
    assert any(
        n.resource_type == "monitoring_report"
        and n.resource_id == str(stored.id)
        and n.needs_attention
        for n in alerts
    )
    assert not inbox.list(Principal(username="other-team"))


@pytest.mark.parametrize("valid", [True, False])
def test_pipeline_monitoring_report_is_atomic_and_step_attributed(monitoring, clock, valid):
    from controlplane.application.pipeline_runs import PipelineRunService
    from controlplane.application.pipelines import CreatePipeline, PipelineService, StepInput
    from controlplane.reconciliation.pipeline_runs import PipelineRunReconciler

    service, fields, _, factory = monitoring
    job, _ = service.create("catalog-team", **fields)
    definition, _ = PipelineService(factory, clock).create(
        "catalog-team",
        CreatePipeline(name="quality-pipeline", steps=[StepInput("quality", job.name)]),
    )
    runs = PipelineRunService(factory, clock)
    view, _ = runs.create("catalog-team", definition.name)
    workflow = FakeWorkflowProvider()
    reconciler = PipelineRunReconciler(factory, workflow, clock=clock)
    reconciler.reconcile(view.run.id)
    run = runs.view(view.run.id).run
    value = json.loads(report(job, run))
    value["execution"] = str(run.id) + "/quality"
    workflow._status[run.external_ref] = WorkflowStatus(
        ExternalState.SUCCEEDED,
        {"quality": ExternalState.SUCCEEDED},
        results={"quality": json.dumps(value) if valid else "{}"},
    )
    assert reconciler.reconcile(run.id).after == (
        RunStatus.SUCCEEDED if valid else RunStatus.FAILED
    )
    reports = service.reports("catalog-team")
    if valid:
        assert len(reports) == 1 and reports[0].pipeline_run_id == run.id
        assert reports[0].job_run_id is None and reports[0].step == "quality"
    else:
        assert reports == []


def test_feedback_matching_contract_and_metrics_are_validated(monitoring, request, clock):
    from controlplane.domain.data import DatasetColumn
    from controlplane.tests.test_data_catalog import spec

    service, fields, provider, factory = monitoring
    catalog, connection, _, _ = request.getfixturevalue("env")
    observed, _ = catalog.publish_dataset(
        "catalog-team",
        **spec(
            connection,
            name="predictions",
            columns=(
                DatasetColumn("income", "number"),
                DatasetColumn("request_id", "string"),
                DatasetColumn("prediction", "number"),
            ),
            row_count=5,
        ),
    )
    truth, _ = catalog.publish_dataset(
        "catalog-team",
        **spec(
            connection,
            name="ground-truth",
            columns=(DatasetColumn("request_id", "string"), DatasetColumn("actual", "number")),
            row_count=4,
        ),
    )
    fields = {
        **fields,
        "observed_dataset_id": observed.id,
        "feedback_dataset_id": truth.id,
        "entity_key": "request_id",
    }
    job, _ = service.create("catalog-team", **fields)
    runs, workflow = RunService(factory, clock), FakeWorkflowProvider()
    run, _ = runs.create("catalog-team", job.name)
    reconciler = RunReconciler(factory, workflow, clock)
    reconciler.reconcile(run.id)
    run = runs.get(run.id)
    value = json.loads(report(job, run))
    value["feedback_dataset_id"] = str(truth.id)
    value["performance"] = {
        "task": "REGRESSION",
        "matched_rows": 3,
        "unmatched_predictions": 2,
        "unmatched_truth": 1,
        "coverage": 0.6,
        "status": "MEASURED",
        "metrics": {"mae": 0.1, "rmse": 0.2, "r2": 0.9},
    }
    workflow._status[run.external_ref] = WorkflowStatus(
        ExternalState.SUCCEEDED, results={"main": json.dumps(value)}
    )
    assert reconciler.reconcile(run.id).after == RunStatus.SUCCEEDED
    assert service.reports("catalog-team")[0].result["performance"]["coverage"] == 0.6
    retry, _ = runs.retry(run.id)
    reconciler.reconcile(retry.id)
    retry = runs.get(retry.id)
    value["execution"] = str(retry.id) + "/main"
    value["performance"]["coverage"] = 1.0
    workflow._status[retry.external_ref] = WorkflowStatus(
        ExternalState.SUCCEEDED, results={"main": json.dumps(value)}
    )
    assert reconciler.reconcile(retry.id).after == RunStatus.FAILED
    assert len(service.reports("catalog-team")) == 1


@pytest.mark.parametrize(
    "field",
    [
        "job_definition_id",
        "reference_dataset_id",
        "observed_dataset_id",
        "feedback_dataset_id",
        "job_run_id",
        "pipeline_run_id",
        "model_version_id",
        "model_id",
    ],
)
def test_postgres_refuses_cross_project_report_links(monitoring, uow_factory, clock, field):
    from sqlalchemy import insert, literal, select, update
    from sqlalchemy.exc import IntegrityError
    from controlplane.domain.entities import Project
    from controlplane.persistence.models import (
        DataConnectionRow,
        DatasetVersionRow,
        JobDefinitionRow,
        ModelRow,
        ModelVersionRow,
        MonitoringReportRow,
        RunRow,
        PipelineDefinitionRow,
        PipelineRunRow,
    )
    from controlplane.application.pipelines import PipelineService, CreatePipeline, StepInput
    from controlplane.application.pipeline_runs import PipelineRunService

    with uow_factory() as check:
        if not hasattr(check, "_session"):
            pytest.skip("PostgreSQL constraint test")
    service, job, run, _, reconciler = complete(monitoring, clock)
    reconciler.reconcile(run.id)
    report = service.reports("catalog-team")[0]
    pipeline, _ = PipelineService(uow_factory, clock).create(
        "catalog-team", CreatePipeline(name="report-pipeline", steps=[StepInput("check", job.name)])
    )
    view, _ = PipelineRunService(uow_factory, clock).create("catalog-team", pipeline.name)
    with uow_factory() as uow:
        foreign = Project(
            name="other-team", display_name="Other", created_at=clock(), updated_at=clock()
        )
        uow.projects.add(foreign)
        session = uow._session
        stored = session.get(MonitoringReportRow, report.id)
        original_dataset = session.get(DatasetVersionRow, report.reference_dataset_id)

        def clone(row, source_id, **changes):
            table = row.__table__
            new_id = uuid4()
            overrides = {"id": new_id, **changes}
            session.execute(
                insert(table).from_select(
                    [c.name for c in table.c],
                    select(
                        *[
                            literal(overrides[c.name], type_=c.type) if c.name in overrides else c
                            for c in table.c
                        ]
                    ).where(table.c.id == source_id),
                )
            )
            return new_id

        connection_id = clone(
            DataConnectionRow, original_dataset.connection_id, project_id=foreign.id
        )
        dataset_id = clone(
            DatasetVersionRow,
            original_dataset.id,
            project_id=foreign.id,
            connection_id=connection_id,
        )
        job_id = clone(JobDefinitionRow, job.id, project_id=foreign.id)
        model_id = clone(ModelRow, stored.model_id, project_id=foreign.id)
        version_id = clone(ModelVersionRow, report.model_version_id, model_id=model_id)
        run_id = clone(RunRow, run.id, project_id=foreign.id, job_definition_id=job_id)
        pipeline_id = clone(PipelineDefinitionRow, pipeline.id, project_id=foreign.id)
        pipeline_run_id = clone(
            PipelineRunRow, view.run.id, project_id=foreign.id, pipeline_definition_id=pipeline_id
        )
        foreign_ids = {
            "job_definition_id": job_id,
            "reference_dataset_id": dataset_id,
            "observed_dataset_id": dataset_id,
            "feedback_dataset_id": dataset_id,
            "job_run_id": run_id,
            "pipeline_run_id": pipeline_run_id,
            "model_version_id": version_id,
            "model_id": model_id,
        }
        changes = {field: foreign_ids[field]}
        if field == "pipeline_run_id":
            changes["job_run_id"] = None
        with pytest.raises(IntegrityError) as caught:
            session.execute(
                update(MonitoringReportRow)
                .where(MonitoringReportRow.id == report.id)
                .values(**changes)
            )
        assert caught.value.orig.sqlstate == "23503"
