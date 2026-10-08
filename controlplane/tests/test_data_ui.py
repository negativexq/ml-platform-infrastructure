from playwright.sync_api import expect

from controlplane.adapters.fakes import FakeSecretProvider
from controlplane.application.data_catalog import DataCatalogService
from controlplane.application.secrets import ProjectSecretService
from controlplane.tests.test_ui import page, server, shot  # noqa: F401


def test_register_connection_and_immutable_dataset_versions(page, server):  # noqa: F811
    demo = server.demo
    provider = FakeSecretProvider()
    secrets = ProjectSecretService(demo.uow_factory, provider, demo.clock)
    secrets.put(
        "credit-risk",
        "data-storage",
        {"AWS_ACCESS_KEY_ID": "key", "AWS_SECRET_ACCESS_KEY": "hidden"},
        "Opaque",
    )
    demo.app.state.secrets = secrets
    demo.app.state.data_catalog = DataCatalogService(demo.uow_factory, demo.clock, provider)
    page.goto(f"{server.url}/ui/#/projects/credit-risk/connections")
    page.get_by_role("button", name="Register connection", exact=True).click()
    page.locator("#f-name").fill("customer-data")
    page.locator("#f-endpoint").fill("http://minio:9000")
    page.locator("#f-bucket").fill("datasets")
    page.get_by_test_id("form-submit").click()
    expect(page.get_by_role("heading", name="customer-data", exact=True)).to_be_visible()
    page.get_by_role("link", name="Browse datasets", exact=True).click()
    page.get_by_role("button", name="Register dataset", exact=True).click()
    page.get_by_label("Dataset name", exact=True).fill("customers")
    page.get_by_label("S3 object URI", exact=True).fill("s3://datasets/customers-v1.parquet")
    page.get_by_label("SHA-256 checksum", exact=True).fill("a" * 64)
    page.get_by_label("Column name 1", exact=True).fill("income")
    page.get_by_label("Row count", exact=True).fill("500")
    page.get_by_test_id("register-dataset-submit").click()
    expect(page.get_by_role("heading", name="customers", exact=True)).to_be_visible()
    expect(page.get_by_test_id("dataset-schema")).to_contain_text("income")
    expect(page.get_by_test_id("dataset-lineage")).to_contain_text("No producer")
    version_one_url = page.url
    page.get_by_role("button", name="Register new version", exact=True).click()
    page.get_by_label("S3 object URI", exact=True).fill("s3://datasets/customers-v2.parquet")
    page.get_by_label("SHA-256 checksum", exact=True).fill("b" * 64)
    page.get_by_label("Column name 1", exact=True).fill("salary")
    page.get_by_test_id("register-dataset-submit").click()
    expect(page.get_by_test_id("dataset-schema")).to_contain_text("salary")
    expect(page.get_by_label("Dataset version", exact=True)).to_have_value("2")
    shot(page, "dataset-version-schema-lineage")
    page.goto(version_one_url)
    expect(page.get_by_test_id("dataset-schema")).to_contain_text("income")
    expect(page.get_by_label("Dataset version", exact=True)).to_have_value("1")
    page.goto(f"{server.url}/ui/#/datasets?project=credit-risk")
    expect(page.get_by_test_id("datasets")).to_contain_text("customers")
    expect(page.get_by_test_id("datasets").locator("tbody tr")).to_have_count(2)


def test_batch_output_graph_opens_pinned_input(page, server):  # noqa: F811
    from controlplane.adapters.fakes import FakeWorkflowProvider
    from controlplane.application.batch_inference import BatchInferenceService
    from controlplane.application.providers import ExternalState, WorkflowStatus
    from controlplane.application.runs import RunService
    from controlplane.domain.data import DatasetColumn, DatasetFormat
    from controlplane.reconciliation.runs import RunReconciler
    from controlplane.tests.test_batch_inference import result

    demo = server.demo
    provider = FakeSecretProvider()
    ProjectSecretService(demo.uow_factory, provider, demo.clock).put(
        "credit-risk",
        "graph-storage",
        {"AWS_ACCESS_KEY_ID": "key", "AWS_SECRET_ACCESS_KEY": "hidden"},
        "Opaque",
    )
    catalog = DataCatalogService(demo.uow_factory, demo.clock, provider)
    connection, _ = catalog.create_connection(
        "credit-risk",
        name="graph-data",
        endpoint="http://minio:9000",
        bucket="datasets",
        prefix="training",
        credential_secret="graph-storage",
    )
    dataset, _ = catalog.publish_dataset(
        "credit-risk",
        name="customers",
        connection_id=connection.id,
        uri="s3://datasets/training/customers.csv",
        format=DatasetFormat.CSV,
        columns=(DatasetColumn("income", "number"),),
        checksum_sha256="a" * 64,
    )
    with demo.uow_factory() as uow:
        project = uow.projects.get_by_name("credit-risk")
        model = next(
            model for model in uow.models.list(project.id) if model.kind.value == "classic"
        )
        version = uow.model_versions.list(model.id)[0]

    class Registry:
        def model_artifact_uri(self, model, version):
            return "s3://datasets/training/model"

    service = BatchInferenceService(
        demo.uow_factory, "registry/batch@sha256:" + "a" * 64, Registry(), provider, demo.clock
    )
    job, _ = service.create(
        "credit-risk",
        name="daily-scoring",
        input_dataset_id=dataset.id,
        model_version_id=version.id,
        model_connection_id=connection.id,
        output_connection_id=connection.id,
        output_dataset="predictions",
        features=["income"],
    )
    runs, workflow = RunService(demo.uow_factory, demo.clock), FakeWorkflowProvider()
    run, _ = runs.create("credit-risk", job.name)
    reconciler = RunReconciler(demo.uow_factory, workflow, demo.clock)
    reconciler.reconcile(run.id)
    run = runs.get(run.id)
    workflow._status[run.external_ref] = WorkflowStatus(
        ExternalState.SUCCEEDED, results={"main": result(job, run)}
    )
    reconciler.reconcile(run.id)
    page.goto(f"{server.url}/ui/#/projects/credit-risk/datasets/predictions?version=1")
    graph = page.get_by_test_id("dataset-lineage")
    expect(graph.get_by_role("button", name="daily-scoring: succeeded")).to_be_visible()
    expect(graph.get_by_role("button", name="customers: registered")).to_be_visible()
    shot(page, "dataset-batch-lineage")
    page.set_viewport_size({"width": 390, "height": 844})
    shot(page, "dataset-lineage-mobile")
    graph.get_by_role("button", name="customers: registered").click()
    expect(page.get_by_role("heading", name="customers", exact=True)).to_be_visible()
    expect(page.get_by_label("Dataset version", exact=True)).to_have_value("1")
