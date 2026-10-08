import json
from uuid import UUID

from playwright.sync_api import expect

from controlplane.adapters.fakes import FakeSecretProvider, FakeWorkflowProvider
from controlplane.application.data_catalog import DataCatalogService
from controlplane.application.model_monitoring import ModelMonitoringService
from controlplane.application.providers import ExternalState, WorkflowStatus
from controlplane.application.runs import RunService
from controlplane.application.secrets import ProjectSecretService
from controlplane.domain.data import DatasetColumn, DatasetFormat
from controlplane.reconciliation.runs import RunReconciler
from controlplane.tests.test_ui import page, server, shot  # noqa: F401


def test_create_monitoring_and_inspect_drift_report(page, server):  # noqa: F811
    demo = server.demo
    provider = FakeSecretProvider()
    ProjectSecretService(demo.uow_factory, provider, demo.clock).put(
        "credit-risk",
        "monitor-storage",
        {"AWS_ACCESS_KEY_ID": "key", "AWS_SECRET_ACCESS_KEY": "hidden"},
        "Opaque",
    )
    catalog = DataCatalogService(demo.uow_factory, demo.clock, provider)
    connection, _ = catalog.create_connection(
        "credit-risk",
        name="monitor-data",
        endpoint="http://minio:9000",
        bucket="datasets",
        credential_secret="monitor-storage",
    )
    datasets = []
    for name in ["reference", "observed"]:
        dataset, _ = catalog.publish_dataset(
            "credit-risk",
            name=name,
            connection_id=connection.id,
            uri=f"s3://datasets/{name}.csv",
            format=DatasetFormat.CSV,
            columns=(DatasetColumn("income", "number"),),
            checksum_sha256="a" * 64,
            row_count=150,
        )
        datasets.append(dataset)
    demo.app.state.model_monitoring = ModelMonitoringService(
        demo.uow_factory, "registry/batch@sha256:" + "a" * 64, provider, demo.clock
    )
    page.goto(f"{server.url}/ui/#/projects/credit-risk/model-monitoring")
    page.get_by_role("button", name="Create monitoring check", exact=True).click()
    page.locator("#f-name").fill("model-quality")
    page.locator("#f-features").fill("income")
    page.locator("#f-reference_dataset_id").select_option(str(datasets[0].id))
    page.locator("#f-observed_dataset_id").select_option(str(datasets[1].id))
    page.get_by_test_id("form-submit").click()
    expect(page.get_by_test_id("monitoring-checks")).to_contain_text("model-quality")
    page.get_by_role("button", name="Run now", exact=True).click()
    expect(page.get_by_role("heading", name="model-quality", exact=True)).to_be_visible()
    run_id = UUID(page.url.rsplit("/", 1)[-1])
    workflow = FakeWorkflowProvider()
    reconciler = RunReconciler(demo.uow_factory, workflow, demo.clock)
    reconciler.reconcile(run_id)
    run = RunService(demo.uow_factory, demo.clock).get(run_id)
    with demo.uow_factory() as uow:
        job = uow.jobs.get(run.job_definition_id)
    spec = job.monitoring_spec
    value = {
        "execution": str(run.id) + "/main",
        "model_version_id": spec["model_version_id"],
        "reference_dataset_id": spec["reference_dataset_id"],
        "observed_dataset_id": spec["observed_dataset_id"],
        "feedback_dataset_id": None,
        "status": "DRIFTED",
        "reference_rows": 150,
        "observed_rows": 150,
        "features": [
            {
                "name": "income",
                "dtype": "number",
                "psi": 0.9,
                "reference_missing_rate": 0.0,
                "observed_missing_rate": 0.0,
                "missing_rate_change": 0.0,
                "drifted": True,
                "status": "DRIFTED",
            }
        ],
        "performance": None,
    }
    workflow._status[run.external_ref] = WorkflowStatus(
        ExternalState.SUCCEEDED, results={"main": json.dumps(value)}
    )
    reconciler.reconcile(run.id)
    page.reload()
    page.get_by_test_id("monitoring-outputs").get_by_role("link", name="model-quality").click()
    expect(page.get_by_test_id("monitoring-features")).to_contain_text("0.900")
    expect(page.get_by_test_id("monitoring-features")).to_contain_text("drifted")
    expect(page.get_by_text("No ground truth supplied.", exact=False)).to_be_visible()
    shot(page, "model-monitoring-drift")
    report_url = page.url
    page.get_by_role(
        "link", name=f"{spec['model_name']} · v{spec['model_version']}", exact=True
    ).click()
    expect(
        page.locator(f"[data-testid='version-row'][data-version='{spec['model_version']}'].sel")
    ).to_be_visible()
    page.goto(report_url)
    page.set_viewport_size({"width": 390, "height": 844})
    shot(page, "model-monitoring-mobile")
    page.get_by_role("link", name="reference · v1", exact=True).click()
    expect(page.get_by_role("heading", name="reference", exact=True)).to_be_visible()
    # Register an automatic rule through the same product screen.
    from controlplane.application.monitoring_automation import MonitoringAutomationService

    demo.app.state.monitoring_automation = MonitoringAutomationService(
        demo.uow_factory, "registry/batch@sha256:" + "a" * 64, provider, demo.clock
    )
    page.goto(f"{server.url}/ui/#/projects/credit-risk/model-monitoring")
    page.get_by_role("button", name="Create monitoring rule", exact=True).click()
    page.locator("#f-name").fill("automatic-quality")
    page.locator("#f-features").fill("income")
    page.locator("#f-reference_dataset_id").select_option(str(datasets[0].id))
    page.locator("#f-observed_dataset_name").select_option("observed")
    page.get_by_test_id("form-submit").click()
    expect(page.get_by_test_id("monitoring-rules")).to_contain_text("automatic-quality")
    page.get_by_test_id("monitoring-rules").get_by_role("button", name="Pause", exact=True).click()
    expect(page.get_by_test_id("monitoring-rules")).to_contain_text("PAUSED", ignore_case=True)
    page.get_by_test_id("monitoring-rules").get_by_role(
        "button", name="automatic-quality", exact=True
    ).click()
    expect(page.get_by_text("Waiting for a new dataset version.", exact=True)).to_be_visible()
