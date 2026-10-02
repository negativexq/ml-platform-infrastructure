from __future__ import annotations

from fastapi.testclient import TestClient
from openapi_spec_validator import validate

from controlplane.api.app import create_app
from controlplane.persistence.memory import MemoryStore, MemoryUnitOfWork


def test_openapi_document_is_valid_and_describes_the_project_api() -> None:
    store = MemoryStore()
    client = TestClient(create_app(lambda: MemoryUnitOfWork(store)))
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    document = spec.json()
    validate(document)
    assert {"/projects", "/projects/{project_id}", "/healthz"} <= set(document["paths"])
    assert set(document["paths"]["/projects"]) == {"post", "get"}
    assert client.get("/docs").status_code == 200
