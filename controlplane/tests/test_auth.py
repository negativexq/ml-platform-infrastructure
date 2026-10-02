"""Sign-in and project roles, against a real (in-process) OpenID Connect provider."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from controlplane.adapters.fakes import FakeClusterProvider
from controlplane.adapters.identity import OidcProvider
from controlplane.api.app import create_app
from controlplane.api.auth import POLICY, PUBLIC, SIGNED_IN, AuthConfig, project_of
from controlplane.api.session import SESSION_COOKIE, Signer
from controlplane.application.jobs import CreateJob, JobService
from controlplane.application.ports import UnitOfWork
from controlplane.reconciliation.projects import ProjectReconciler
from controlplane.tests.conftest import FakeClock
from controlplane.tests.fake_idp import API_AUDIENCE, CLIENT_ID, CLIENT_SECRET, FakeIdP

SECRET = "x" * 40


@pytest.fixture(scope="module")
def idp() -> Iterator[FakeIdP]:
    server = FakeIdP().start()
    yield server
    server.stop()


def _auth(idp: FakeIdP, *, admins: tuple[str, ...] = ("group:platform-admins",)) -> AuthConfig:
    provider = OidcProvider(
        idp.issuer,
        audience=API_AUDIENCE,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        platform_admins=admins,
    )
    return AuthConfig(
        authenticator=provider,
        login=provider,
        signer=Signer(SECRET),
        public_url="http://testserver",
        secure_cookies=False,
    )


class Env:
    def __init__(
        self, idp: FakeIdP, uow_factory: Callable[[], UnitOfWork], clock: FakeClock
    ) -> None:
        self.idp, self.uow_factory, self.clock = idp, uow_factory, clock
        self.client = TestClient(create_app(uow_factory, clock, auth=_auth(idp)))

    def as_(self, username: str, groups: list[str] | None = None) -> dict[str, str]:
        return {"authorization": f"Bearer {self.idp.token(username, groups)}"}

    def project(self, owner: str = "alice", name: str = "credit-risk") -> str:
        r = self.client.post("/projects", json={"name": name}, headers=self.as_(owner))
        assert r.status_code == 201, r.text
        ProjectReconciler(self.uow_factory, FakeClusterProvider(), self.clock).reconcile_all()
        JobService(self.uow_factory, self.clock).create(name, CreateJob(name="train", image="t:1"))
        return name

    def grant(self, project: str, subject: str, role: str, by: str = "alice") -> httpx.Response:
        response: httpx.Response = self.client.put(
            f"/projects/{project}/members/{subject}", json={"role": role}, headers=self.as_(by)
        )
        return response


@pytest.fixture
def env(idp: FakeIdP, uow_factory: Callable[[], UnitOfWork], clock: FakeClock) -> Env:
    return Env(idp, uow_factory, clock)


# -- authentication --------------------------------------------------------------------


def test_the_api_needs_a_signed_in_caller_but_probes_and_the_ui_do_not(env: Env) -> None:
    r = env.client.get("/projects")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"
    assert r.headers["www-authenticate"] == "Bearer"
    assert env.client.get("/healthz").status_code == 200
    assert env.client.get("/ui/").status_code == 200
    assert env.client.get("/projects", headers=env.as_("alice")).status_code == 200


@pytest.mark.parametrize(
    "case", ["wrong key", "expired", "wrong audience", "wrong issuer", "garbage"]
)
def test_bad_tokens_are_refused(env: Env, idp: FakeIdP, case: str) -> None:
    token: str = {  # type: ignore[no-untyped-call]
        "wrong key": lambda: idp.token(
            key=rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ),
        "expired": lambda: idp.token(ttl=-3600),
        "wrong audience": lambda: idp.token(aud="someone-else"),
        "wrong issuer": lambda: idp.token(issuer="https://evil.example.com"),
        "garbage": lambda: "not-a-jwt",
    }[case]()
    r = env.client.get("/projects", headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 401, r.text


# -- project roles -----------------------------------------------------------------------


def test_the_creator_is_admin_and_others_see_nothing_until_invited(env: Env) -> None:
    project = env.project("alice")
    assert [
        p["name"] for p in env.client.get("/projects", headers=env.as_("alice")).json()["items"]
    ] == [project]
    assert env.client.get("/projects", headers=env.as_("bob")).json()["items"] == []
    denied = env.client.get(f"/projects/{project}/summary", headers=env.as_("bob"))
    assert denied.status_code == 403 and "not a member" in denied.json()["error"]["message"]

    members = env.client.get(f"/projects/{project}/members", headers=env.as_("alice")).json()[
        "items"
    ]
    assert [(m["subject"], m["role"]) for m in members] == [("user:alice", "admin")]
    assert env.client.get("/me", headers=env.as_("alice")).json()["roles"] == {project: "admin"}


def test_viewer_reads_operator_acts_admin_governs(env: Env) -> None:
    project = env.project("alice")
    assert env.grant(project, "user:bob", "viewer").status_code == 200
    bob = env.as_("bob")
    assert env.client.get(f"/projects/{project}/runs", headers=bob).status_code == 200
    start = env.client.post(f"/projects/{project}/jobs/train/runs", headers=bob)
    assert start.status_code == 403 and "operator" in start.json()["error"]["message"]

    env.grant(project, "user:bob", "operator")
    run = env.client.post(f"/projects/{project}/jobs/train/runs", headers=bob)
    assert run.status_code == 202
    assert env.client.get(f"/runs/{run.json()['id']}", headers=bob).status_code == 200  # by id, too
    assert (
        env.client.get(f"/runs/{run.json()['id']}", headers=env.as_("mallory")).status_code == 403
    )
    assert (
        env.client.post(
            f"/projects/{project}/models", json={"name": "scorer"}, headers=bob
        ).status_code
        == 201
    )
    thresholds = env.client.put(
        f"/projects/{project}/models/scorer/thresholds",
        json={"thresholds": {"auc": {"min": 0.9}}},
        headers=bob,
    )
    assert thresholds.status_code == 403  # an admin decision
    assert env.grant(project, "user:carol", "viewer", by="bob").status_code == 403


def test_a_group_can_be_a_member(env: Env) -> None:
    project = env.project("alice")
    env.grant(project, "group:ml-team", "operator")
    carol = env.as_("carol", ["ml-team"])
    assert env.client.post(f"/projects/{project}/jobs/train/runs", headers=carol).status_code == 202
    assert env.client.get("/me", headers=carol).json()["roles"] == {project: "operator"}
    assert (
        env.client.get(
            f"/projects/{project}/runs", headers=env.as_("dave", ["other-team"])
        ).status_code
        == 403
    )


def test_platform_admins_can_do_anything(env: Env) -> None:
    project = env.project("alice")
    root = env.as_("root", ["platform-admins"])
    assert [p["name"] for p in env.client.get("/projects", headers=root).json()["items"]] == [
        project
    ]
    assert (
        env.client.put(
            f"/projects/{project}/models/x/thresholds", json={"thresholds": {}}, headers=root
        ).status_code
        == 404
    )
    assert env.client.get("/me", headers=root).json()["platform_admin"] is True


def test_a_project_keeps_an_admin(env: Env) -> None:
    project = env.project("alice")
    demote = env.grant(project, "user:alice", "operator")
    assert demote.status_code == 409 and "last admin" in demote.json()["error"]["message"]
    assert (
        env.client.delete(
            f"/projects/{project}/members/user:alice", headers=env.as_("alice")
        ).status_code
        == 409
    )
    env.grant(project, "user:bob", "admin")
    assert env.grant(project, "user:alice", "viewer").status_code == 200
    assert (
        env.client.delete(
            f"/projects/{project}/members/user:alice", headers=env.as_("bob")
        ).status_code
        == 204
    )
    assert env.grant(project, "nobody", "viewer", by="bob").status_code == 422  # not user:/group:


def test_the_audit_trail_names_the_person(env: Env) -> None:
    project = env.project("alice")
    env.grant(project, "user:bob", "operator")
    env.client.post(f"/projects/{project}/jobs/train/runs", headers=env.as_("bob"))
    events = env.client.get(f"/projects/{project}/audit?limit=50", headers=env.as_("alice")).json()[
        "items"
    ]
    by = {(e["action"], e["actor"]) for e in events}
    assert {
        ("project.created", "alice"),
        ("membership.granted", "alice"),
        ("run.created", "bob"),
    } <= by
    # Everything done through the API names a person (the job was defined directly, in setup).
    assert all(e["actor"] != "anonymous" for e in events if e["action"] != "job.created")


def test_unknown_ids_are_404_not_403(env: Env) -> None:
    env.project("alice")
    r = env.client.get("/runs/00000000-0000-0000-0000-000000000000", headers=env.as_("bob"))
    assert r.status_code == 404


# -- the policy covers every route ---------------------------------------------------------


def test_every_route_has_an_owner_in_the_policy(env: Env) -> None:
    """A route is either listed in POLICY or names the project it acts on; nothing is open by
    accident. A new route that fits neither must be added to POLICY deliberately."""
    unowned = []
    for route in env.client.app.routes:  # type: ignore[attr-defined]
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or ():
            if (method, route.path) in POLICY:
                continue
            params = {
                name: "00000000-0000-0000-0000-000000000000" for name in route.param_convertors
            }
            try:
                with env.uow_factory() as uow:
                    owned = project_of(uow, route.path, params) is not None
            except Exception:  # noqa: BLE001 - NotFound means "a project would be looked up"
                owned = True
            if not owned:
                unowned.append(f"{method} {route.path}")
    assert unowned == []
    assert POLICY[("GET", "/healthz")] == PUBLIC and POLICY[("GET", "/me")] == SIGNED_IN


# -- browser sign-in -------------------------------------------------------------------------


def _sign_in(env: Env, idp: FakeIdP, next_path: str = "/ui/#/projects") -> TestClient:
    client = env.client
    start = client.get("/auth/login", params={"next": next_path}, follow_redirects=False)
    assert start.status_code == 302
    at_idp = httpx.get(start.headers["location"], follow_redirects=False)  # the real IdP over HTTP
    assert at_idp.status_code == 302
    back = urlsplit(at_idp.headers["location"])
    callback = client.get(f"{back.path}?{back.query}", follow_redirects=False)
    assert callback.status_code == 302, callback.text
    assert callback.headers["location"] == next_path
    return client


def test_browser_sign_in_sets_a_session_and_mutations_need_the_csrf_header(
    env: Env, idp: FakeIdP
) -> None:
    idp.next_user = {"preferred_username": "alice", "groups": ["ml-team"], "name": "Alice A."}
    client = _sign_in(env, idp)
    assert SESSION_COOKIE in client.cookies
    me = client.get("/me").json()
    assert (me["username"], me["display_name"], me["groups"], me["can_sign_out"]) == (
        "alice",
        "Alice A.",
        ["ml-team"],
        True,
    )

    assert (
        client.post("/projects", json={"name": "from-browser"}).status_code == 403
    )  # no CSRF header
    made = client.post("/projects", json={"name": "from-browser"}, headers={"x-mlp-csrf": "1"})
    assert made.status_code == 201

    out = client.post("/auth/logout", headers={"x-mlp-csrf": "1"})
    assert out.status_code == 200 and out.json()["logout_url"].startswith(idp.issuer)
    client.cookies.clear()
    assert client.get("/me").status_code == 401


def test_a_tampered_session_is_ignored(env: Env, idp: FakeIdP) -> None:
    idp.next_user = {"preferred_username": "alice", "groups": []}
    client = _sign_in(env, idp)
    value = client.cookies[SESSION_COOKIE]
    payload, mac = value.split(".")
    import base64
    import json

    data: dict[str, Any] = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    data["a"] = True  # try to become a platform admin
    forged = base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    client.cookies.clear()
    assert (
        client.get("/me", headers={"cookie": f"{SESSION_COOKIE}={forged}.{mac}"}).status_code == 401
    )
    assert client.get("/me", headers={"cookie": f"{SESSION_COOKIE}={value}"}).status_code == 200


def test_sign_in_only_returns_into_the_ui(env: Env, idp: FakeIdP) -> None:
    for target in ("https://evil.example.com/", "//evil.example.com/ui", "/projects"):
        start = env.client.get("/auth/login", params={"next": target}, follow_redirects=False)
        at_idp = httpx.get(start.headers["location"], follow_redirects=False)
        back = urlsplit(at_idp.headers["location"])
        done = env.client.get(f"{back.path}?{back.query}", follow_redirects=False)
        assert done.headers["location"] == "/ui/"


def test_a_callback_that_did_not_start_here_is_refused(env: Env) -> None:
    r = env.client.get("/auth/callback", params={"code": "x", "state": "y"})
    assert r.status_code == 400 and "did not start here" in r.text
    assert "state" in parse_qs("state=1")


def test_start_up_refuses_to_run_open_by_accident() -> None:
    from controlplane.main import auth_config
    from controlplane.settings import Settings

    with pytest.raises(RuntimeError, match="CP_OIDC_ISSUER"):
        auth_config(Settings(auth_mode="oidc", oidc_issuer=""))
    with pytest.raises(RuntimeError, match="CP_SESSION_SECRET"):
        auth_config(
            Settings(oidc_issuer="https://idp", oidc_client_id="mlp-ui", session_secret="short")
        )
    assert auth_config(Settings(auth_mode="none")) is None


def test_platform_health_counts_only_the_callers_projects(env: Env) -> None:
    env.project("alice", "credit-risk")
    env.project("bob", "ranker")
    alice = env.client.get("/platform/health", headers=env.as_("alice")).json()
    assert alice["inventory"]["projects"] == 1
    admin = env.client.get("/platform/health", headers=env.as_("root", ["platform-admins"])).json()
    assert admin["inventory"]["projects"] == 2
    assert env.client.get("/platform/health").status_code == 401


def test_invokers_call_models_and_only_admins_open_them(env: Env) -> None:
    project = env.project()
    env.grant(project, "user:ivan", "invoker")
    env.grant(project, "user:olga", "operator")
    ivan, olga, alice = env.as_("ivan"), env.as_("olga"), env.as_("alice")
    base = f"/projects/{project}"
    assert env.client.get(f"{base}/runs", headers=ivan).status_code == 403  # no reading
    # may call a model (no serving here, so 409, which is past the role check)
    assert env.client.post(f"{base}/endpoints/x/predict", json={}, headers=ivan).status_code == 409
    keys = {"name": "partner-acme", "endpoints": ["x"]}
    assert env.client.post(f"{base}/api-keys", json=keys, headers=olga).status_code == 403
    assert (
        env.client.patch(
            f"{base}/endpoints/x", json={"exposure": "public"}, headers=olga
        ).status_code
        == 403
    )
    assert env.client.post(f"{base}/api-keys", json=keys, headers=alice).status_code == 422
    assert env.client.get(f"{base}/api-keys", headers=olga).status_code == 200


def test_only_platform_admins_grant_gpus(env: Env) -> None:
    project = env.project()
    own_admin = env.client.put(
        f"/projects/{project}/gpu-quota", json={"gpus": 4}, headers=env.as_("alice")
    )
    assert own_admin.status_code == 403  # alice created the project and is its admin
    root = env.client.put(
        f"/projects/{project}/gpu-quota",
        json={"gpus": 4},
        headers=env.as_("root", ["platform-admins"]),
    )
    assert root.status_code == 200 and root.json()["gpu_quota"] == 4
