from playwright.sync_api import expect

from controlplane.adapters.fakes import FakeSecretProvider
from controlplane.application.batch_inference import BatchInferenceService
from controlplane.application.data_catalog import DataCatalogService
from controlplane.application.secrets import ProjectSecretService
from controlplane.domain.data import DatasetColumn, DatasetFormat
from controlplane.tests.test_ui import page, server, shot  # noqa: F401


def test_create_and_start_batch_from_typed_form(page, server):  # noqa: F811
    demo = server.demo
    provider = FakeSecretProvider()
    ProjectSecretService(demo.uow_factory, provider, demo.clock).put(
        "credit-risk",
        "batch-storage",
        {"AWS_ACCESS_KEY_ID": "key", "AWS_SECRET_ACCESS_KEY": "hidden"},
        "Opaque",
    )
    catalog = DataCatalogService(demo.uow_factory, demo.clock, provider)
    connection, _ = catalog.create_connection(
        "credit-risk",
        name="batch-models",
        endpoint="http://minio:9000",
        bucket="models",
        credential_secret="batch-storage",
    )
    catalog.publish_dataset(
        "credit-risk",
        name="customers",
        connection_id=connection.id,
        uri="s3://models/input.csv",
        format=DatasetFormat.CSV,
        columns=(DatasetColumn("income", "number"),),
        checksum_sha256="a" * 64,
    )

    class Registry:
        def model_artifact_uri(self, model, version):
            return f"s3://models/{model}/{version}"

    demo.app.state.batch_inference = BatchInferenceService(
        demo.uow_factory, "registry/batch@sha256:" + "a" * 64, Registry(), provider, demo.clock
    )
    page.goto(f"{server.url}/ui/#/projects/credit-risk/batch-inference")
    page.get_by_role("button", name="Create batch inference", exact=True).click()
    page.locator("#f-name").fill("daily-score")
    page.locator("#f-output_dataset").fill("predictions")
    page.locator("#f-features").fill("income")
    page.locator("#f-memory").fill("1Gi")
    page.get_by_test_id("form-submit").click()
    expect(page.get_by_test_id("batch-definitions")).to_contain_text("daily-score")
    expect(page.get_by_test_id("batch-definitions")).to_contain_text("customers · v1")
    shot(page, "batch-definitions")
    page.get_by_role("button", name="Run now", exact=True).click()
    expect(page.get_by_role("heading", name="daily-score", exact=True)).to_be_visible()
    assert page.url.rsplit("/", 1)[-1]
