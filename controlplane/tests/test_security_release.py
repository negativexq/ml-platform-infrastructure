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
from controlplane.domain.errors import Conflict
from controlplane.persistence.migrate import _lock, upgrade

ROOT = Path(__file__).resolve().parents[2]


def test_project_secret_binding_has_only_owned_namespace_scope() -> None:
    api = MagicMock()
    provider = KubernetesClusterProvider(
        api,
        api_service_account="release-api",
        api_namespace="system",
        secret_cluster_role="release-project-secrets",
        workload_cluster_role="release-project-workload-reader",
    )
    project_id = uuid4()
    spec = NamespaceSpec(project_id, "team", "mlp-team", {}, {}, {}, {})
    binding = provider._desired(spec)["secretrolebinding"]
    assert binding["metadata"]["namespace"] == "mlp-team"
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "release-api", "namespace": "system"}
    ]
    assert binding["roleRef"]["name"] == "release-project-secrets"
    reader = provider._desired(spec)["workloadrolebinding"]
    assert reader["metadata"]["namespace"] == "mlp-team"
    assert reader["metadata"]["name"] == "mlp-api-workload-reader"
    assert reader["subjects"] == binding["subjects"]
    assert reader["roleRef"]["name"] == "release-project-workload-reader"
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
    migration = next(d for d in docs if d["kind"] == "Job")
    migration_spec = migration["spec"]["template"]["spec"]
    assert migration_spec["automountServiceAccountToken"] is False
    assert migration_spec["containers"][0]["image"] == "mlp-controlplane-migrate:dev"
    assert [env["name"] for env in migration_spec["containers"][0]["env"]] == ["CP_DATABASE_URL"]
    api_role = next(
        d for d in docs if d["kind"] == "ClusterRole" and d["metadata"]["name"].endswith("-api")
    )
    assert api_role["rules"] == [{"apiGroups": [""], "resources": ["namespaces"], "verbs": ["get"]}]
    assert not any(
        d["kind"] == "ClusterRoleBinding"
        and d["roleRef"]["name"].endswith(("-project-secrets", "-project-workload-reader"))
        for d in docs
    )
    reader_role = next(
        d
        for d in docs
        if d["kind"] == "ClusterRole" and d["metadata"]["name"].endswith("-project-workload-reader")
    )
    assert {r for rule in reader_role["rules"] for r in rule["resources"]} == {
        "pods",
        "pods/log",
        "workflows",
        "inferenceservices",
        "revisions",
    }
    assert all(set(rule["verbs"]) <= {"get", "list"} for rule in reader_role["rules"])
    deployments = {
        d["metadata"]["name"].rsplit("-", 1)[1]: d for d in docs if d["kind"] == "Deployment"
    }
    for name in ("api", "gateway", "reconciler"):
        assert deployments[name]["spec"]["replicas"] == 2
        assert deployments[name]["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
        assert deployments[name]["spec"]["template"]["spec"]["topologySpreadConstraints"]
    assert sum(d["kind"] == "PodDisruptionBudget" for d in docs) == 3
    local = render("-f", str(ROOT / "helm/controlplane/values-local.yaml"))
    assert local.returncode == 0, local.stderr
    assert not any(
        d and d["kind"] == "PodDisruptionBudget" for d in yaml.safe_load_all(local.stdout)
    )
    assert render("--set", "config.CP_GATEWAY_LIMIT_STORE=memory").returncode != 0
    assert render("-f", str(ROOT / "helm/controlplane/values-production.yaml")).returncode != 0


def test_workload_binding_repair_uses_cas_and_refuses_foreign_binding() -> None:
    api = MagicMock()
    provider = KubernetesClusterProvider(
        api,
        api_service_account="api",
        secret_cluster_role="secrets",
        workload_cluster_role="reader",
    )
    provider._rbac = MagicMock()
    spec = NamespaceSpec(uuid4(), "team", "mlp-team", {"mlp.io/project-id": "project"}, {}, {}, {})
    body = provider._desired(spec)["workloadrolebinding"]
    current = SimpleNamespace(metadata=SimpleNamespace(labels={"foreign": "yes"}))
    provider._rbac.read_namespaced_role_binding.return_value = current
    with pytest.raises(Conflict, match="not owned"):
        provider._upsert("workloadrolebinding", spec.namespace, body)
    provider._rbac.replace_namespaced_role_binding.assert_not_called()
    current.metadata.labels = spec.labels
    current.metadata.resource_version = "12"
    current.role_ref = object()
    api.sanitize_for_serialization.return_value = body["roleRef"]
    provider._upsert("workloadrolebinding", spec.namespace, body)
    assert (
        provider._rbac.replace_namespaced_role_binding.call_args.args[2]["metadata"][
            "resourceVersion"
        ]
        == "12"
    )
