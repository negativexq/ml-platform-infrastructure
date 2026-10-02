"""Sign-in and roles in a real browser: the demo control plane with sign-in on, against an
in-process OpenID Connect provider."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest

from controlplane.adapters.identity import OidcProvider
from controlplane.api.auth import AuthConfig
from controlplane.api.session import Signer
from controlplane.application.identity import bind_principal, reset_principal
from controlplane.application.members import MembershipService
from controlplane.demo import build_demo
from controlplane.domain.access import Principal, ProjectRole
from controlplane.tests.fake_idp import API_AUDIENCE, CLIENT_ID, CLIENT_SECRET, FakeIdP
from controlplane.tests.test_ui import HAVE_PLAYWRIGHT, Server, _launch, shot

if HAVE_PLAYWRIGHT:
    from playwright.sync_api import Page, expect, sync_playwright


@pytest.fixture(scope="module")
def idp() -> Iterator[FakeIdP]:
    server = FakeIdP().start()
    yield server
    server.stop()


@pytest.fixture
def server(idp: FakeIdP) -> Iterator[Server]:
    provider = OidcProvider(
        idp.issuer,
        audience=API_AUDIENCE,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        platform_admins=["user:root"],
    )
    demo = build_demo(
        auth=AuthConfig(
            authenticator=provider, login=provider, signer=Signer("k" * 40), secure_cookies=False
        )
    )
    token = bind_principal(Principal(username="setup", platform_admin=True))
    try:
        members = MembershipService(demo.uow_factory)
        members.set_role("credit-risk", "user:alice", ProjectRole.ADMIN)
        members.set_role("credit-risk", "user:bob", ProjectRole.VIEWER)
    finally:
        reset_principal(token)
    srv = Server(demo)
    yield srv
    srv.stop()


@pytest.fixture
def page(server: Server) -> Iterator[Any]:
    if not HAVE_PLAYWRIGHT:
        pytest.skip("playwright is not installed")
    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        calls: list[str] = []
        page.on(
            "request",
            lambda r: calls.append(r.url) if r.resource_type in ("fetch", "xhr") else None,
        )
        page.on("pageerror", lambda e: pytest.fail(f"uncaught page error: {e}"))
        yield page
        # Sign-in is a top-level navigation to the identity provider; the page itself still
        # only ever calls the platform API.
        assert [u for u in calls if not u.startswith(server.url)] == []
        browser.close()


def sign_in(
    page: Page, server: Server, idp: FakeIdP, user: str, route: str = "/projects", **claims: Any
) -> None:
    idp.next_user = {"preferred_username": user, "groups": [], **claims}
    page.goto(f"{server.url}/ui/#{route}")
    expect(page.get_by_test_id("sign-in")).to_be_visible()
    page.get_by_test_id("sign-in-button").click()
    expect(page.get_by_test_id("account-button")).to_be_visible()


def test_sign_in_and_come_back_to_the_page_asked_for(
    page: Page, server: Server, idp: FakeIdP
) -> None:
    page.goto(f"{server.url}/ui/#/projects/credit-risk/runs?status=failed")
    expect(page.get_by_test_id("sign-in")).to_contain_text("Sign in to ML Platform")
    shot(page, "20-sign-in")
    idp.next_user = {
        "preferred_username": "alice",
        "groups": [],
        "name": "Alice Admin",
        "email": "alice@example.com",
    }
    page.get_by_test_id("sign-in-button").click()
    expect(page.locator("main h1")).to_have_text("Runs")
    assert page.url.endswith("#/projects/credit-risk/runs?status=failed")
    page.get_by_test_id("account-button").click()
    expect(page.get_by_test_id("account-menu")).to_contain_text("Alice Admin")
    expect(page.get_by_test_id("account-menu")).to_contain_text("alice@example.com")


def test_a_viewer_sees_but_cannot_act(page: Page, server: Server, idp: FakeIdP) -> None:
    sign_in(page, server, idp, "bob")
    expect(
        page.locator(
            "[data-testid^=project-]:not([data-testid=projects-list]):not([data-testid=projects-search])"
        )
    ).to_have_count(1)
    page.goto(f"{server.url}/ui/#/projects/credit-risk")
    run = page.get_by_test_id("run-pipeline")
    expect(run).to_be_disabled()
    expect(run).to_have_attribute("title", re.compile("Needs the operator role.*you are viewer"))
    page.goto(f"{server.url}/ui/#/projects/credit-risk/deployments/credit-risk-prod")
    expect(page.get_by_test_id("rollback")).to_be_disabled()
    expect(page.get_by_test_id("abort")).to_be_disabled()
    expect(page.get_by_test_id("try-send")).to_be_disabled()
    page.goto(f"{server.url}/ui/#/projects/credit-risk/settings")
    expect(page.get_by_test_id("member-row")).to_have_count(2)
    expect(page.get_by_test_id("add-member")).to_be_disabled()
    expect(page.get_by_test_id("delete-project")).to_be_disabled()
    shot(page, "21-viewer-settings")


def test_a_non_member_is_told_so(page: Page, server: Server, idp: FakeIdP) -> None:
    sign_in(page, server, idp, "mallory")
    expect(page.get_by_test_id("projects-list")).to_contain_text("No projects yet")
    page.goto(f"{server.url}/ui/#/projects/credit-risk")
    expect(page.locator("main")).to_contain_text("not a member of this project")


def test_an_admin_manages_members(page: Page, server: Server, idp: FakeIdP) -> None:
    sign_in(page, server, idp, "alice", route="/projects/credit-risk/settings")
    rows = page.get_by_test_id("member-row")
    expect(rows).to_have_count(2)
    page.get_by_test_id("add-member").click()
    page.locator("#f-kind").select_option("group")
    page.locator("#f-name").fill("ml-team")
    page.locator("#f-role").select_option("operator")
    page.get_by_test_id("form-submit").click()
    expect(rows).to_have_count(3)
    bob = page.locator("[data-testid=member-row][data-subject='user:bob']")
    bob.get_by_test_id("member-role").select_option("operator")
    expect(bob.get_by_test_id("member-role")).to_have_value("operator")
    alice = page.locator("[data-testid=member-row][data-subject='user:alice']")
    alice.get_by_test_id("member-role").select_option("viewer")  # the last admin: refused
    expect(page.locator(".toast.bad")).to_contain_text("last admin")
    bob.get_by_test_id("remove-member").click()
    page.get_by_test_id("confirm-ok").click()
    expect(rows).to_have_count(2)
    shot(page, "22-members")

    page.goto(f"{server.url}/ui/#/projects/credit-risk/activity")
    expect(page.locator("[data-testid=activity-list]").first).to_contain_text("alice")


def test_sign_out(page: Page, server: Server, idp: FakeIdP) -> None:
    sign_in(page, server, idp, "alice")
    page.get_by_test_id("account-button").click()
    page.get_by_test_id("sign-out").click()
    page.wait_for_url(re.compile(r"/logout"))  # the identity provider's end-session endpoint
    page.goto(f"{server.url}/ui/#/projects")
    expect(page.get_by_test_id("sign-in")).to_be_visible()
