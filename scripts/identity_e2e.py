"""Sign-in against a real Keycloak, end to end: the browser flow through Keycloak's login page
for each local user, sign-out through Keycloak's end-session endpoint, and API calls with
Keycloak-issued bearer tokens.

Needs Keycloak with k8s/identity/realm-mlp.json on :8180 (scripts/identity-up.sh) and the demo
control plane on :8080 with sign-in on (the env in scripts/identity-up.sh). Run:

    python scripts/identity_e2e.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from controlplane.tests.test_ui import _launch  # noqa: E402

BASE = os.environ.get("MLP_URL", "http://localhost:8080")
KEYCLOAK = os.environ.get("KEYCLOAK_URL", "http://127.0.0.1:8180")
# who signs in -> the projects they should see (None: all, a platform admin)
EXPECTED = {
    "bob": {"credit-risk": "operator"},
    "carol": {"fraud-detection": "viewer"},
    "alice": None,
}
failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        failures.append(what)


with sync_playwright() as p:
    browser = _launch(p)
    for user, roles in EXPECTED.items():
        page = browser.new_context().new_page()
        page.goto(f"{BASE}/ui/#/projects")
        expect(page.get_by_test_id("sign-in")).to_be_visible()
        page.get_by_test_id("sign-in-button").click()
        page.wait_for_url("**/realms/mlp/**")  # Keycloak's own login page
        page.fill("#username", user)
        page.fill("#password", user)
        page.click("#kc-login")
        expect(page.get_by_test_id("account-button")).to_be_visible(timeout=15000)
        me = page.evaluate("fetch('/me').then(r => r.json())")
        if roles is None:
            check(me["platform_admin"], f"{user} is a platform admin (group platform-admins)")
        else:
            check(me["roles"] == roles, f"{user} has roles {roles} (got {me['roles']})")
        if user == "bob":
            page.get_by_test_id("account-button").click()
            page.get_by_test_id("sign-out").click()
            page.wait_for_url(f"{BASE}/ui/**", timeout=15000)
            expect(page.get_by_test_id("sign-in")).to_be_visible()
            check(True, "bob signed out through Keycloak and is back at the sign-in screen")
        page.context.close()
    browser.close()


def token(user: str) -> str:
    data = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": "mlp-cli",
            "username": user,
            "password": user,
            "scope": "openid",
        }
    ).encode()
    url = f"{KEYCLOAK}/realms/mlp/protocol/openid-connect/token"
    return str(json.load(urllib.request.urlopen(url, data))["access_token"])  # noqa: S310


def status(method: str, path: str, bearer: str) -> int:
    request = urllib.request.Request(
        BASE + path,
        method=method,
        data=b"{}" if method == "POST" else None,
        headers={"authorization": f"Bearer {bearer}", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code


bob, carol = token("bob"), token("carol")
check(
    status("POST", "/projects/credit-risk/jobs/smoke-test/runs", bob) == 202,
    "bob (operator) starts a job with a bearer token",
)
check(
    status("GET", "/projects/credit-risk/summary", carol) == 403,
    "carol cannot read a project she is not in",
)
check(status("GET", "/projects/fraud-detection/summary", carol) == 200, "carol reads her project")
check(
    status("POST", "/projects/fraud-detection/jobs/score-batch/runs", carol) == 403,
    "carol (viewer) cannot start work",
)
sys.exit(1 if failures else 0)
