"""Dependency isolation: the domain and application layers are pure, and the whole
control plane runs with MLflow, Argo, Kubernetes and KServe unavailable."""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

EXTERNAL = {"mlflow", "kubernetes", "argo_workflows", "hera", "kserve", "boto3", "requests", "jwt"}
FRAMEWORKS = {"sqlalchemy", "alembic", "psycopg", "fastapi", "starlette", "pydantic_settings"}

# layer -> top-level modules it must never import
FORBIDDEN: dict[str, set[str]] = {
    "domain": EXTERNAL | FRAMEWORKS | {"pydantic"},
    "application": EXTERNAL | FRAMEWORKS | {"pydantic"},
    "api": EXTERNAL | {"sqlalchemy", "alembic", "psycopg"},
}
FORBIDDEN_INTERNAL: dict[str, set[str]] = {
    "domain": {"application", "api", "persistence", "adapters", "reconciliation"},
    "application": {"api", "persistence", "adapters", "reconciliation"},
    "api": {"persistence", "adapters"},
}


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layer_import_rules(layer: str) -> None:
    files = sorted((ROOT / layer).rglob("*.py"))
    assert files, layer
    for path in files:
        for module in _imports(path):
            top = module.split(".")[0]
            assert top not in FORBIDDEN[layer], f"{path.name} imports {module}"
            if top == "controlplane":
                inner = module.removeprefix("controlplane.")
                for banned in FORBIDDEN_INTERNAL[layer]:
                    assert not (inner == banned or inner.startswith(banned + ".")), (
                        f"{layer}/{path.name} imports {module}"
                    )


def test_only_adapters_may_touch_external_systems() -> None:
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in {"adapters", "tests"}:
            continue
        for module in _imports(path):
            assert module.split(".")[0] not in EXTERNAL, f"{rel} imports {module}"


def test_control_plane_runs_with_every_external_system_unavailable() -> None:
    """Block the client libraries outright, then run a real use case end to end
    through the API. Also proves the pure layers never pulled in a database driver."""
    program = textwrap.dedent(
        """
        import sys
        for name in ("mlflow", "kubernetes", "argo_workflows", "hera", "kserve", "boto3"):
            sys.modules[name] = None  # any import now raises ImportError

        from fastapi.testclient import TestClient
        from controlplane.adapters import fakes  # noqa: F401
        from controlplane.api.app import create_app
        from controlplane.persistence.memory import MemoryStore, MemoryUnitOfWork

        store = MemoryStore()
        client = TestClient(create_app(lambda: MemoryUnitOfWork(store)))
        assert client.post("/projects", json={"name": "credit-risk"}).status_code == 201
        assert len(client.get("/projects").json()["items"]) == 1
        for heavy in ("sqlalchemy", "psycopg", "alembic"):
            assert heavy not in sys.modules, heavy
        print("isolated-ok")
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, cwd=ROOT.parent
    )
    assert out.returncode == 0, out.stderr
    assert "isolated-ok" in out.stdout


def test_only_composition_roots_import_observability() -> None:
    roots = {"main.py", "reconciler_main.py", "demo.py"}
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in {"observability", "tests"} or rel.name in roots:
            continue
        for module in _imports(path):
            assert not module.startswith("controlplane.observability"), f"{rel} imports {module}"
            assert module.split(".")[0] != "opentelemetry" or rel.parts[0] in {"api"}, (
                f"{rel} imports {module}"
            )
