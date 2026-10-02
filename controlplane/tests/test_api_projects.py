from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient


def _create(client: TestClient, **body: str) -> tuple[int, dict[str, object]]:
    r = client.post("/projects", json={"name": "credit-risk", **body})
    return r.status_code, r.json()


def test_create_get_list(client: TestClient) -> None:
    code, created = _create(client, display_name="Credit Risk", description="scoring")
    assert code == 201
    assert created["status"] == "PENDING"
    assert created["display_name"] == "Credit Risk"

    got = client.get(f"/projects/{created['id']}")
    assert got.status_code == 200 and got.json() == created

    listing = client.get("/projects").json()
    assert [p["id"] for p in listing["items"]] == [created["id"]]


def test_duplicate_request_is_idempotent(client: TestClient) -> None:
    first_code, first = _create(client)
    second_code, second = _create(client)
    assert (first_code, second_code) == (201, 200)
    assert first == second
    assert len(client.get("/projects").json()["items"]) == 1


def test_same_name_different_attributes_is_409(client: TestClient) -> None:
    _create(client, description="a")
    code, body = _create(client, description="b")
    assert code == 409
    assert body["error"]["code"] == "conflict"  # type: ignore[index]


def test_invalid_name_is_422_with_a_typed_error(client: TestClient) -> None:
    r = client.post("/projects", json={"name": "Not Valid"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_argument"


def test_unknown_field_is_rejected(client: TestClient) -> None:
    assert client.post("/projects", json={"name": "abc", "bogus": 1}).status_code == 422


def test_unknown_project_is_404(client: TestClient) -> None:
    r = client.get(f"/projects/{uuid4()}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_malformed_id_is_422(client: TestClient) -> None:
    assert client.get("/projects/not-a-uuid").status_code == 422


def test_pagination_bounds(client: TestClient) -> None:
    assert client.get("/projects?limit=0").status_code == 422
    assert client.get("/projects?offset=-1").status_code == 422


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_delete_is_accepted_idempotent_and_recorded(client: TestClient) -> None:
    _, created = _create(client)
    first = client.delete(f"/projects/{created['id']}")
    assert first.status_code == 202 and first.json()["status"] == "DELETING"
    again = client.delete(f"/projects/{created['id']}")
    assert again.status_code == 202 and again.json()["status"] == "DELETING"
    assert client.delete(f"/projects/{uuid4()}").status_code == 404
