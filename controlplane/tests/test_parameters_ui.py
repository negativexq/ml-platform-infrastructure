import re

from playwright.sync_api import expect

from controlplane.tests.test_parameters import SCHEMA
from controlplane.tests.test_ui import page, server  # noqa: F401


def test_typed_job_parameters_and_retry_snapshot(page, server):  # noqa: F811
    response = page.request.post(
        f"{server.url}/projects/credit-risk/jobs",
        data={"name": "parameter-browser", "image": "test:1", "parameter_schema": SCHEMA},
    )
    assert response.status == 201
    page.goto(f"{server.url}/ui/#/projects/credit-risk/jobs/parameter-browser")
    page.get_by_role("button", name="Start job", exact=True).click()
    page.get_by_label("processing_date", exact=False).fill("2026-02-03")
    page.get_by_label("batch_size", exact=True).fill("42")
    page.get_by_test_id("form-submit").click()
    expect(page).to_have_url(re.compile(r"/runs/[0-9a-f-]{36}$"))
    expect(page.get_by_test_id("execution-parameters")).to_contain_text("2026-02-03")
    id = page.url.rsplit("/", 1)[-1]
    assert page.request.get(f"{server.url}/runs/{id}").json()["parameters"] == {
        "processing_date": "2026-02-03",
        "batch_size": 42,
    }
