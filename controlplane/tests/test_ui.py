"""The UI: what is served, the rules it must obey, the new read APIs, and a real browser.

The browser tests drive the demo control plane (in-memory fakes, `controlplane.demo`) in
Chromium and need no other infrastructure. They are skipped when Playwright or a Chromium
binary is missing. Set CP_UI_SCREENSHOTS=<dir> to also write screenshots.
"""

from __future__ import annotations

import glob
import os
import re
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from controlplane.demo import Demo, build_demo
from controlplane.ui import CONTENT_SECURITY_POLICY, STATIC_DIR

# -- what is served -------------------------------------------------------------


@pytest.fixture
def demo() -> Demo:
    return build_demo()


@pytest.fixture
def client(demo: Demo) -> TestClient:
    return TestClient(demo.app)


def test_the_ui_is_served_with_a_strict_content_security_policy(client: TestClient) -> None:
    index = client.get("/ui/")
    assert index.status_code == 200 and "text/html" in index.headers["content-type"]
    assert index.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert "connect-src 'self'" in CONTENT_SECURITY_POLICY
    assert "'unsafe-inline'" not in CONTENT_SECURITY_POLICY
    assert index.headers["x-content-type-options"] == "nosniff"
    root = client.get("/", follow_redirects=False)
    assert root.status_code in (302, 307) and root.headers["location"] == "/ui/"
    assert "content-security-policy" not in client.get("/projects").headers  # API is untouched


def test_every_module_the_ui_imports_is_served(client: TestClient) -> None:
    for path in sorted(p for p in STATIC_DIR.rglob("*") if p.is_file()):
        url = "/ui/" + path.relative_to(STATIC_DIR).as_posix()
        assert client.get(url).status_code == 200, url


# -- the rules the UI must obey (checked on the TypeScript source) -------------------
# The served bundle is generated from controlplane/ui/web/src; the rules apply to what people
# write, not to minified output (React's own runtime legitimately contains `innerHTML`).

WEB = STATIC_DIR.parent / "web"
SOURCES = sorted(
    [
        p
        for p in (WEB / "src").rglob("*")
        if p.suffix in {".ts", ".tsx", ".css"}
        and not p.name.endswith((".d.ts", ".test.ts"))  # generated types and unit tests
    ]
    + [WEB / "index.html", STATIC_DIR / "index.html"]
)
FORBIDDEN_APIS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
    "XMLHttpRequest",
    "WebSocket",
    "EventSource",
    "sendBeacon",
    "importScripts",
    "localStorage",
    "dangerouslySetInnerHTML",
)
# The UI knows the Platform API and nothing behind it.
SUBSYSTEMS = ("mlflow", "argo", "kubernetes", "kubectl", "kserve", "knative", "prometheus", "minio")


def test_the_ui_has_sources() -> None:
    assert {p.name for p in SOURCES} >= {"index.html", "main.tsx", "client.ts", "app.css"}


def code_only(text: str) -> str:
    """Comments may *discuss* a forbidden API; only code may not use it. A `//` that
    follows `:` (as in `http://`) is part of a URL, not a comment."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(^|\s)//.*$", r"\1", text, flags=re.M)


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_ui_never_talks_to_the_subsystems(path: Path) -> None:
    text = path.read_text()
    code = code_only(text)
    lowered = code.lower()
    for word in SUBSYSTEMS:
        assert word not in lowered, f"{path.name} mentions {word!r}"
    for api in FORBIDDEN_APIS:
        if api == "localStorage" and path.name == "theme.ts":
            continue  # the one sanctioned use: the theme preference (guarded, optional)
        assert api not in code, f"{path.name} uses {api}"
    urls = re.findall(r"https?://[^\s'\"`)]+", text)
    assert [u for u in urls if u != "http://www.w3.org/2000/svg"] == [], (
        f"{path.name}: absolute URLs"
    )


def test_no_inline_style_strings() -> None:
    """The CSP drops inline style attributes silently, so none may be written."""
    for path in SOURCES:
        if path.suffix in {".ts", ".tsx"}:
            code = code_only(path.read_text())
            assert not re.search(r"style:\s*['\"`]", code), f"{path.name}: style string"
            assert not re.search(r"style=['\"]", code), f"{path.name}: style attribute string"
        if path.suffix == ".html":
            assert "style=" not in path.read_text() and "<style" not in path.read_text()


def test_only_the_api_client_uses_the_network() -> None:
    for path in SOURCES:
        if path.suffix in {".ts", ".tsx"} and path.name != "client.ts":
            assert "fetch(" not in code_only(path.read_text()), f"{path.name} calls fetch directly"


def test_api_paths_in_the_ui_exist_in_the_platform_api(demo: Demo) -> None:
    """Every literal API path the UI builds must be a real route (catches typos)."""
    known = demo.app.openapi()["paths"]  # the authoritative list of API routes
    normalised = {re.sub(r"\{[^}]+\}", "{}", p) for p in known}
    source = "\n".join(
        p.read_text()
        for p in SOURCES
        if p.suffix in {".ts", ".tsx"} and p.name != "router.tsx"  # route patterns, not API calls
    )
    for literal in re.findall(
        r"[`'\"](/(?:projects|pipeline-runs|runs|model-versions|rollouts)[^`'\"]*)[`'\"]", source
    ):
        path = re.sub(r"\$\{[^}]+\}", "{}", literal.split("?")[0])
        assert path in normalised, f"UI calls {literal!r}, which is not a platform route"


# -- the read APIs the UI added ------------------------------------------------------


def test_summary_counts(client: TestClient) -> None:
    s = client.get("/projects/credit-risk/summary").json()
    assert (s["pipeline_runs"], s["runs"], s["models"], s["champions"]) == (3, 3, 2, 2)
    assert (s["deployments"], s["deployments_ready"], s["endpoints"], s["active_rollouts"]) == (
        2,
        2,
        2,
        1,
    )
    assert client.get("/projects/ghost/summary").status_code == 404


def test_endpoint_list_and_metrics_per_revision(client: TestClient) -> None:
    endpoints = {
        e["name"]: e for e in client.get("/projects/credit-risk/endpoints").json()["items"]
    }
    assert endpoints["credit-risk-prod"]["status"] == "READY"
    m = client.get("/projects/credit-risk/endpoints/credit-risk-prod/metrics").json()
    assert m["available"] is True
    by_rev = {r["revision"]: r for r in m["revisions"]}
    assert (by_rev[1]["traffic_percent"], by_rev[2]["traffic_percent"]) == (
        75,
        25,
    )  # stable / canary
    assert by_rev[2]["p95_latency_ms"] == 118.0 and by_rev[1]["requests_per_second"] == 41.3
    assert client.get("/projects/credit-risk/endpoints/nope/metrics").status_code == 404


def test_metrics_failure_is_reported_not_raised(demo: Demo, client: TestClient) -> None:
    from controlplane.application.providers import RevisionMetrics

    class Down:
        def revision_metrics(self, *a: Any, **k: Any) -> RevisionMetrics:
            raise ConnectionError("prometheus unreachable")

    demo.app.state.overview._metrics = Down()
    body = client.get("/projects/credit-risk/endpoints/credit-risk-prod/metrics")
    assert body.status_code == 200
    assert body.json()["available"] is False and "unreachable" in body.json()["error"]


def test_audit_trail_is_newest_first_and_filterable(client: TestClient) -> None:
    deployment = client.get("/projects/credit-risk/deployments/credit-risk-prod").json()
    events = client.get(f"/projects/credit-risk/audit?entity_id={deployment['id']}").json()["items"]
    assert events and events[-1]["action"] == "deployment.created"
    stamps = [e["occurred_at"] for e in events]
    assert stamps == sorted(stamps, reverse=True)
    assert len(client.get("/projects/credit-risk/audit?limit=5").json()["items"]) == 5


def test_runs_are_listed_with_readable_names(client: TestClient) -> None:
    pruns = client.get("/projects/credit-risk/pipeline-runs").json()["items"]
    assert {r["pipeline"] for r in pruns} == {"training"} and {
        r["pipeline_version"] for r in pruns
    } == {1}
    jobs = {r["job"] for r in client.get("/projects/credit-risk/runs").json()["items"]}
    assert jobs == {"smoke-test", "evaluate-model", "train-model"}


# -- a real browser -------------------------------------------------------------------

try:
    from playwright.sync_api import Page, Playwright, expect, sync_playwright

    HAVE_PLAYWRIGHT = True
except ImportError:  # pragma: no cover
    HAVE_PLAYWRIGHT = False


def _chromium() -> str | None:
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
    return os.environ.get("CP_CHROMIUM") or (found[-1] if found else None)


def _launch(p: Playwright) -> Any:
    try:
        return p.chromium.launch()
    except Exception:  # noqa: BLE001 - pinned browser build may differ from the installed one
        path = _chromium()
        if path is None:
            pytest.skip("no Chromium available")
        return p.chromium.launch(executable_path=path, args=["--no-sandbox"])


class Server:
    def __init__(self, demo: Demo) -> None:
        import uvicorn

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.demo = demo
        self.server = uvicorn.Server(
            uvicorn.Config(demo.app, host="127.0.0.1", port=self.port, log_level="error")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("demo server did not start")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture
def server() -> Iterator[Server]:
    srv = Server(build_demo())
    yield srv
    srv.stop()


@pytest.fixture
def page(server: Server) -> Iterator[Page]:
    if not HAVE_PLAYWRIGHT:
        pytest.skip("playwright is not installed")
    with sync_playwright() as p:
        browser = _launch(p)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        requests: list[str] = []
        page.on("request", lambda r: requests.append(r.url))
        page.on("pageerror", lambda e: pytest.fail(f"uncaught page error: {e}"))
        page._requests = requests
        yield page
        # The rule that matters: every request the browser made went to the platform API.
        origin = server.url
        foreign = [u for u in requests if not u.startswith(origin) and not u.startswith("data:")]
        assert foreign == [], f"requests outside the platform API: {foreign}"
        browser.close()


def shot(page: Page, name: str) -> None:
    target = os.environ.get("CP_UI_SCREENSHOTS")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(target) / f"{name}.png"), full_page=True)


def test_projects_page(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/")  # redirects to /ui/
    expect(page.locator("h1")).to_have_text("Projects")
    cards = page.locator("[data-testid^=project-]")
    expect(cards).to_have_count(3)
    credit = page.locator("[data-testid=project-credit-risk]")
    expect(credit).to_contain_text("Credit Risk")
    expect(credit.locator("[data-status=READY]")).to_be_visible()
    expect(credit.locator("[data-count=models] b")).to_have_text("2")
    expect(credit.locator("[data-count=deployments] b")).to_have_text("2")
    expect(
        page.locator("[data-testid=project-churn-prediction] [data-status=PENDING]")
    ).to_be_visible()
    shot(page, "01-projects")


def test_project_page_lists_everything(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/projects/credit-risk")
    expect(page.locator("h1")).to_have_text("Credit Risk")
    expect(page.locator("[data-testid=pipeline-run-row]")).to_have_count(3)
    expect(page.locator("[data-testid=job-run-row]")).to_have_count(3)
    expect(page.locator("[data-testid=model-row]")).to_have_count(2)
    expect(page.locator("[data-testid=deployment-row]")).to_have_count(2)
    expect(page.locator("[data-testid=pipeline-run-row]").first).to_contain_text("training v1")
    shot(page, "02-project")


def test_pipeline_run_dag_logs_and_tracking(page: Page, server: Server) -> None:
    run = server.demo.ids["run_ok"]
    page.goto(f"{server.url}/ui/#/projects/credit-risk/pipeline-runs/{run}")
    nodes = page.locator("svg.dag g.node")
    expect(nodes).to_have_count(5)
    expect(page.locator("svg.dag g.node[data-step=train]")).to_have_class(
        re.compile("st-SUCCEEDED")
    )
    # validate>prepare, prepare>train, prepare>profile, train>evaluate
    assert page.locator("svg.dag path.edge").count() == 4
    page.locator("svg.dag g.node[data-step=evaluate]").click()
    expect(page.locator("[data-testid=logs]")).to_contain_text("holdout auc=0.941")
    expect(page.locator("[data-testid=tracking]")).to_contain_text("alpha")
    expect(page.locator("[data-testid=tracking]")).to_contain_text("0.939")
    expect(page.locator("[data-testid=artifact]")).to_contain_text("s3://mlflow/artifacts")
    expect(page.locator("[data-testid=cancel-run]")).to_have_count(0)  # finished
    shot(page, "03-pipeline-run-succeeded")


def test_failed_run_shows_the_failure_first_and_skips_the_rest(page: Page, server: Server) -> None:
    run = server.demo.ids["run_failed"]
    page.goto(f"{server.url}/ui/#/projects/credit-risk/pipeline-runs/{run}")
    expect(page.locator(".alert.bad")).to_contain_text("failed steps: prepare")
    expect(page.locator("svg.dag g.node[data-step=prepare]")).to_have_class(re.compile("st-FAILED"))
    for step in ("train", "profile", "evaluate"):
        expect(page.locator(f"svg.dag g.node[data-step={step}]")).to_have_class(
            re.compile("st-SKIPPED")
        )
    expect(page.locator("[data-testid=logs]")).to_contain_text(
        "KeyError: 'income_band'"
    )  # auto-selected
    shot(page, "04-pipeline-run-failed")


def test_running_run_can_be_cancelled(page: Page, server: Server) -> None:
    run = server.demo.ids["run_live"]
    page.goto(f"{server.url}/ui/#/projects/credit-risk/pipeline-runs/{run}")
    expect(page.locator("svg.dag g.node[data-step=train]")).to_have_class(re.compile("st-RUNNING"))
    page.locator("[data-testid=cancel-run]").click()
    shot(page, "05-cancel-confirm")
    page.locator("[data-testid=confirm-ok]").click()
    expect(page.locator(".toast")).to_contain_text("Cancellation requested")
    server.demo.reconcile()
    expect(page.locator(".page-head [data-status]").first).to_have_attribute(
        "data-status", "CANCELLED", timeout=8000
    )
    expect(page.locator("[data-testid=cancel-run]")).to_have_count(0)


def test_job_run_page(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/projects/credit-risk/runs/{server.demo.ids['job_run_ok']}")
    expect(page.locator("h1")).to_have_text("smoke-test")
    expect(page.locator("[data-testid=logs]")).to_contain_text("ok")


def test_model_versions_evaluations_and_promote(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/projects/credit-risk/models/scorer")
    rows = page.locator("[data-testid=version-row]")
    expect(rows).to_have_count(3)
    expect(
        page.locator("[data-testid=version-row][data-version='3'] [data-status=CANDIDATE]")
    ).to_be_visible()
    expect(
        page.locator("[data-testid=version-row][data-version='2'] [data-status=CHAMPION]")
    ).to_be_visible()
    expect(
        page.locator("[data-testid=version-row][data-version='1'] [data-status=REJECTED]")
    ).to_be_visible()
    expect(page.locator("[data-testid=version-row][data-version='3']")).to_contain_text("0.947")
    expect(page.locator("[data-testid=promote]")).to_have_count(1)  # only the candidate

    page.locator("[data-testid=version-row][data-version='1']").click()  # rejected: show why
    expect(page.locator("[data-testid=version-detail]")).to_contain_text("auc")
    expect(page.locator("[data-testid=version-detail] [data-status=FAILED]").first).to_be_visible()
    shot(page, "06-model")

    page.locator("[data-testid=promote]").click()
    expect(page.locator("dialog[open]")).to_contain_text("v2 (current champion) will be archived")
    page.locator("[data-testid=confirm-ok]").click()
    expect(
        page.locator("[data-testid=version-row][data-version='3'] [data-status=CHAMPION]")
    ).to_be_visible()
    expect(
        page.locator("[data-testid=version-row][data-version='2'] [data-status=ARCHIVED]")
    ).to_be_visible()
    expect(page.locator("[data-testid=promote]")).to_have_count(0)


def test_deployment_canary_metrics_and_abort(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/projects/credit-risk/deployments/credit-risk-prod")
    expect(page.locator("h1")).to_have_text("credit-risk-prod")
    expect(page.locator("[data-testid=stable-share]")).to_contain_text("75%")
    expect(page.locator("[data-testid=canary-share]")).to_contain_text("25%")
    stable = page.locator("[data-testid=revision-metrics][data-revision='1']")
    canary = page.locator("[data-testid=revision-metrics][data-revision='2']")
    expect(stable).to_contain_text("92 ms")
    expect(stable).to_contain_text("0.20%")  # 5xx rate
    expect(stable).to_contain_text("41.3")
    expect(canary).to_contain_text("canary")
    expect(canary).to_contain_text("118 ms")
    expect(page.locator("[data-testid=rollback]")).to_be_disabled()  # a rollout is live
    # The bar must actually be drawn in proportion (inline styles are blocked by the CSP).
    bar = page.locator(".bar").bounding_box()
    share = page.locator("[data-testid=canary-share]").bounding_box()
    assert bar and share and 0.20 <= share["width"] / bar["width"] <= 0.30
    shot(page, "07-deployment-canary")

    page.locator("[data-testid=abort]").click()
    page.locator("[data-testid=confirm-ok]").click()
    expect(page.locator(".toast")).to_contain_text("Abort requested")
    server.demo.reconcile()  # the reconciler returns the traffic
    expect(page.locator("[data-testid=rollout]")).to_have_count(0, timeout=8000)
    history = page.locator("[data-testid=rollout-history]")
    expect(history).to_contain_text("aborted by user")
    expect(history.locator("[data-status=ROLLED_BACK]")).to_be_visible()
    expect(page.locator("[data-testid=revision-metrics]")).to_have_count(
        1
    )  # only stable serves now
    shot(page, "08-deployment-after-abort")


def test_deployment_rollback(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/projects/credit-risk/deployments/ranker-staging")
    expect(page.locator("[data-testid=rollback]")).to_be_enabled()
    expect(page.locator("[data-testid=revisions]")).to_contain_text("ranker v2")
    page.locator("[data-testid=rollback]").click()
    expect(page.locator("dialog[open]")).to_contain_text("r2 is replaced by r1")
    shot(page, "09-rollback-confirm")
    page.locator("[data-testid=confirm-ok]").click()
    expect(page.locator(".toast")).to_contain_text("Rolling back to r1")
    server.demo.reconcile()
    expect(page.locator(".sub")).to_contain_text("r1", timeout=8000)
    model = page.request.get(f"{server.url}/projects/credit-risk/models/ranker").json()
    assert model["champion"]["version"] == 1  # the platform agrees with what is serving


def test_no_page_leaks_placeholder_text(page: Page, server: Server) -> None:
    """Regression: DOM replaceChildren(null) prints the word "null" on the page."""
    ids = server.demo.ids
    pages = [
        "/projects",
        "/projects/credit-risk",
        f"/projects/credit-risk/pipeline-runs/{ids['run_ok']}",
        f"/projects/credit-risk/pipeline-runs/{ids['run_failed']}",
        f"/projects/credit-risk/runs/{ids['job_run_ok']}",
        "/projects/credit-risk/models/scorer",
        "/projects/credit-risk/deployments/credit-risk-prod",
        "/projects/credit-risk/deployments/ranker-staging",
    ]
    for route in pages:
        page.goto(f"{server.url}/ui/#{route}")
        expect(page.locator("main h1")).to_be_visible()
        page.wait_for_timeout(300)  # let polling-driven repaints settle
        text = page.locator("main").inner_text()
        for junk in ("null", "undefined", "[object", "NaN"):
            assert junk not in text, f"{route} shows {junk!r}"


def test_the_focused_main_area_has_no_outline(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/projects")
    expect(page.locator("main h1")).to_be_visible()
    assert page.evaluate("getComputedStyle(document.querySelector('main')).outlineStyle") == "none"


def test_unknown_things_show_a_not_found_page(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/projects/ghost")
    expect(page.locator(".empty")).to_contain_text("Not found")


def test_csp_blocks_requests_to_anything_but_the_platform(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/")
    outcome = page.evaluate(
        """() => new Promise((resolve) => {
            document.addEventListener('securitypolicyviolation',
              (e) => resolve('violation:' + e.violatedDirective));
            fetch('https://example.com/steal').then(() => resolve('allowed'), () => {});
            setTimeout(() => resolve('timeout'), 3000);
        })"""
    )
    assert outcome == "violation:connect-src"


def test_dark_mode_and_a_narrow_screen_still_render(page: Page, server: Server) -> None:
    page.emulate_media(color_scheme="dark")
    page.set_viewport_size({"width": 390, "height": 800})
    page.goto(f"{server.url}/ui/#/projects/credit-risk/pipeline-runs/{server.demo.ids['run_ok']}")
    expect(page.locator("svg.dag g.node")).to_have_count(5)
    assert (
        page.evaluate("document.documentElement.scrollWidth") <= 400 + 8
    )  # no page-level overflow
    shot(page, "10-dark-mobile")


# -- UX: shell, search, forms, lists -------------------------------------------------


def test_theme_toggle_cycles_and_is_remembered(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/projects")
    html = page.locator("html")
    expect(html).not_to_have_attribute("data-theme", re.compile(".+"))  # follows the system
    page.get_by_test_id("theme-btn").click()
    expect(html).to_have_attribute("data-theme", "light")
    page.get_by_test_id("theme-btn").click()
    expect(html).to_have_attribute("data-theme", "dark")
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg == "rgb(15, 18, 24)"
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")


def test_command_palette_jumps_to_a_deployment(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/projects")
    expect(page.locator("main h1")).to_be_visible()
    page.keyboard.press("Control+k")
    box = page.get_by_test_id("palette-input")
    expect(box).to_be_focused()
    box.fill("crprod")  # a subsequence match for credit-risk-prod
    expect(page.get_by_test_id("palette-item").first).to_contain_text("credit-risk-prod")
    page.keyboard.press("Enter")
    expect(page.locator("h1")).to_have_text("credit-risk-prod")
    assert page.title().startswith("credit-risk-prod")


def test_slash_focuses_the_project_filter_and_it_filters(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/projects")
    expect(page.locator("[data-testid^=project-]").first).to_be_visible()
    page.keyboard.press("/")
    expect(page.get_by_test_id("projects-search")).to_be_focused()
    page.keyboard.type("fraud")
    expect(page.locator("[data-testid^=project-]")).to_have_count(1)
    expect(page.locator("[data-testid=project-fraud-detection]")).to_be_visible()
    page.get_by_test_id("projects-search").fill("zzz")
    expect(page.locator(".empty")).to_contain_text("No project matches")
    page.get_by_test_id("projects-search").fill("")
    page.get_by_role("button", name="Ready", exact=True).click()
    for card in page.locator("[data-testid^=project-]").all():
        expect(card.locator("[data-status=READY]")).to_be_visible()


def test_the_filter_is_not_reset_by_live_refresh(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/projects")
    page.get_by_test_id("projects-search").fill("credit")
    page.wait_for_timeout(3500)  # a project is PENDING, so the page polls
    expect(page.get_by_test_id("projects-search")).to_have_value("credit")
    expect(page.locator("[data-testid^=project-]")).to_have_count(1)


def test_create_project_validates_and_reports_server_errors(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/projects")
    page.get_by_test_id("create-project").click()
    page.locator("#f-name").fill("Not Valid")
    page.get_by_test_id("form-submit").click()
    expect(page.locator(".form-error")).to_contain_text("Lowercase")
    page.locator("#f-name").fill("credit-risk")
    page.get_by_label("Description").fill("a different description")
    page.get_by_test_id("form-submit").click()
    expect(page.locator(".form-error")).to_contain_text("credit-risk")
    page.locator("#f-name").fill("brand-new")
    page.get_by_label("Description").fill("")
    page.get_by_test_id("form-submit").click()
    expect(page.locator("h1")).to_have_text("brand-new")
    assert page.request.get(f"{server.url}/projects").json()["items"]


def test_run_a_pipeline_from_the_project_page(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/projects/credit-risk")
    page.get_by_test_id("run-pipeline").click()
    page.get_by_label("Commit").fill("beef123")
    page.get_by_test_id("form-submit").click()
    expect(page).to_have_url(re.compile(r"/pipeline-runs/[0-9a-f-]{36}$"))
    expect(page.locator(".sub")).to_contain_text("beef123")


def test_pending_project_cannot_start_work(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/projects/churn-prediction")
    expect(page.get_by_test_id("run-pipeline")).to_be_disabled()


def test_run_filters_and_show_more(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/projects/credit-risk")
    rows = page.locator("[data-testid=pipeline-run-row]")
    expect(rows).to_have_count(3)
    page.locator("[data-testid=pipeline-runs] [data-run-filter=failed]").click()
    expect(rows).to_have_count(1)
    expect(rows.first.locator("[data-status=FAILED]")).to_be_visible()
    page.locator("[data-testid=pipeline-runs] [data-run-filter=all]").click()
    expect(rows).to_have_count(3)
    expect(page.get_by_test_id("show-more")).to_have_count(0)


def test_project_activity_feed(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/projects/credit-risk")
    expect(page.locator("[data-testid=activity] li").first).to_be_visible()


def test_pipeline_run_timeline_log_tools_and_run_again(page: Page, server: Server) -> None:
    run = server.demo.ids["run_ok"]
    page.goto(f"{server.url}/ui/#/projects/credit-risk/pipeline-runs/{run}")
    expect(page.locator("[data-testid=timeline] .tl-bar")).to_have_count(5)
    page.locator("[data-testid=timeline] .tl-label", has_text="train").first.click()
    expect(page.locator("main h2", has_text="Logs: train")).to_be_visible()
    logs = page.get_by_test_id("logs")
    expect(logs).not_to_have_class(re.compile("nowrap"))
    page.get_by_test_id("log-wrap").uncheck()
    expect(logs).to_have_class(re.compile("nowrap"))
    with page.expect_download() as download:
        page.get_by_role("button", name="Download").click()
    assert download.value.suggested_filename.endswith("-train.log")
    page.get_by_test_id("rerun").click()
    expect(page).to_have_url(re.compile(r"/pipeline-runs/(?!" + str(run) + r")[0-9a-f-]{36}$"))


def test_keyboard_help_and_skip_link(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/projects")
    expect(page.locator("main h1")).to_be_visible()
    page.keyboard.press("?")
    expect(page.locator("dialog[open]")).to_contain_text("Keyboard shortcuts")
    page.keyboard.press("Escape")
    page.reload()
    expect(page.locator("main h1")).to_be_visible()
    page.locator(".skip").focus()
    box = page.locator(".skip").bounding_box()
    assert box is not None and box["x"] >= 0  # visible once focused
    page.keyboard.press("Enter")  # must not be taken for navigation by the hash router
    expect(page.locator("main")).to_be_focused()
    assert page.url.endswith("#/projects")
    shot(page, "11-ux-projects")


def test_an_unknown_route_goes_to_the_project_list(page: Page, server: Server) -> None:
    page.goto(f"{server.url}/ui/#/nonsense")
    expect(page.locator("main h1")).to_have_text("Projects")
    assert page.url.endswith("#/projects")
