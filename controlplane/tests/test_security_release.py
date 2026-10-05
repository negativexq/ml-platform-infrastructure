"""Security and availability contracts checked without running a cluster/database."""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import yaml  # type: ignore[import-untyped]

from controlplane.adapters.kubernetes.provisioner import KubernetesClusterProvider
from controlplane.application.providers import NamespaceSpec
from controlplane.persistence.migrate import _lock, upgrade

ROOT = Path(__file__).resolve().parents[2]


def test_project_secret_binding_has_only_owned_namespace_scope() -> None:
    api = MagicMock()
    provider = KubernetesClusterProvider(
        api,
        api_service_account="release-api",
        api_namespace="system",
        secret_cluster_role="release-project-secrets",
    )
    project_id = uuid4()
    spec = NamespaceSpec(project_id, "team", "mlp-team", {}, {}, {}, {})
    binding = provider._desired(spec)["secretrolebinding"]
    assert binding["metadata"]["namespace"] == "mlp-team"
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "release-api", "namespace": "system"}
    ]
    assert binding["roleRef"]["name"] == "release-project-secrets"
    provider._rbac = MagicMock()
    provider._read("secretrolebinding", "mlp-team", "mlp-api-secrets")
    provider._rbac.read_namespaced_role_binding.assert_called_once_with(
        "mlp-api-secrets", "mlp-team"
    )


def test_migration_refuses_second_owner_before_alembic() -> None:
    connection = MagicMock(dialect=SimpleNamespace(name="postgresql"))
    connection.scalar.return_value = False
    engine = MagicMock()
    engine.begin.return_value.__enter__.return_value = connection
    with patch("controlplane.persistence.migrate.command.upgrade") as migrate:
        with pytest.raises(RuntimeError, match="owns"):
            upgrade(engine)
        migrate.assert_not_called()
    connection.scalar.return_value = True
    _lock(connection)


@pytest.mark.skipif(shutil.which("helm") is None, reason="Helm is required")
def test_chart_rbac_ha_and_production_fail_closed() -> None:
    def render(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["helm", "template", "test", str(ROOT / "helm/controlplane"), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    result = render()
    assert result.returncode == 0, result.stderr
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    api_role = next(
        d for d in docs if d["kind"] == "ClusterRole" and d["metadata"]["name"].endswith("-api")
    )
    assert all("secrets" not in rule["resources"] for rule in api_role["rules"])
    assert not any(
        d["kind"] == "ClusterRoleBinding" and d["roleRef"]["name"].endswith("-project-secrets")
        for d in docs
    )
    deployments = {
        d["metadata"]["name"].rsplit("-", 1)[1]: d for d in docs if d["kind"] == "Deployment"
    }
    for name in ("api", "gateway"):
        assert deployments[name]["spec"]["replicas"] == 2
        assert deployments[name]["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
        assert deployments[name]["spec"]["template"]["spec"]["topologySpreadConstraints"]
    assert sum(d["kind"] == "PodDisruptionBudget" for d in docs) == 2
    assert render("--set", "config.CP_GATEWAY_LIMIT_STORE=memory").returncode != 0
    assert render("-f", str(ROOT / "helm/controlplane/values-production.yaml")).returncode != 0
