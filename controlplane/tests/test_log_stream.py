"""Log identity, dedicated namespace RBAC and fail-closed chart configuration."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import yaml

from controlplane.adapters.kubernetes.provisioner import KubernetesClusterProvider
from controlplane.adapters.workflow.argo import ArgoWorkflowProvider
from controlplane.api.log_stream import configure
from controlplane.application.providers import NamespaceSpec
from controlplane.domain.errors import Conflict, NotFound


def test_argo_snapshot_decodes_http_bytes_without_repr_or_escaped_newlines() -> None:
    provider = ArgoWorkflowProvider(MagicMock())
    provider._custom = MagicMock()
    provider._custom.get_namespaced_custom_object.return_value = {
        "status": {"nodes": {"one": {"id": "pod", "type": "Pod", "templateName": "main"}}}
    }
    provider._core = MagicMock()
    response = MagicMock(data="işlem başladı\nsecond line\n".encode())
    provider._core.read_namespaced_pod_log.return_value = response
    assert provider.get_logs("mlp-project/workflow", "main") == "işlem başladı\nsecond line\n"
    provider._core.read_namespaced_pod_log.assert_called_once_with(
        "pod", "mlp-project", container="main", _preload_content=False
    )
    response.close.assert_called_once()
    response.release_conn.assert_called_once()


@pytest.mark.parametrize(
    "url",
    [
        "//evil/path",
        "https:",
        "http://remote/log-stream",
        "/log-stream?token=x",
        "https://user:pass@host/path",
    ],
)
def test_log_url_configuration_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ValueError):
        configure("k" * 40, url)


def test_log_ticket_configuration_requires_strong_key() -> None:
    assert configure("", "/log-stream") == ("", "/log-stream")
    with pytest.raises(ValueError):
        configure("short", "/log-stream")


@pytest.mark.parametrize("wrong", ["none", "pod_uid", "owner", "label", "container"])
def test_argo_log_target_is_bound_to_workflow_and_pod_identity(wrong: str) -> None:
    provider = ArgoWorkflowProvider(MagicMock())
    provider._custom = MagicMock()
    provider._custom.get_namespaced_custom_object.return_value = {
        "metadata": {"uid": "workflow-uid"},
        "status": {"nodes": {"one": {"id": "pod", "type": "Pod", "templateName": "main"}}},
    }
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            uid="pod-uid",
            labels={"workflows.argoproj.io/workflow": "workflow"},
            owner_references=[SimpleNamespace(kind="Workflow", uid="workflow-uid")],
        ),
        spec=SimpleNamespace(containers=[SimpleNamespace(name="main")]),
    )
    if wrong == "pod_uid":
        pod.metadata.uid = ""
    if wrong == "owner":
        pod.metadata.owner_references[0].uid = "foreign"
    if wrong == "label":
        pod.metadata.labels = {}
    if wrong == "container":
        pod.spec.containers[0].name = "wait"
    provider._core = MagicMock()
    provider._core.read_namespaced_pod.return_value = pod
    if wrong != "none":
        with pytest.raises(NotFound):
            provider.get_log_target("mlp-project/workflow", "main")
    else:
        target = provider.get_log_target("mlp-project/workflow", "main")
        assert target and target.pod_uid == "pod-uid" and target.workflow_uid == "workflow-uid"
        assert provider.get_log_target("mlp-project/workflow", "missing") is None


def test_log_binding_refuses_foreign_owner_and_preserves_cas() -> None:
    api = MagicMock()
    provider = KubernetesClusterProvider(
        api,
        api_namespace="system",
        log_stream_service_account="logs",
        log_stream_cluster_role="project-logs",
    )
    provider._rbac = MagicMock()
    spec = NamespaceSpec(uuid4(), "team", "mlp-team", {"mlp.io/project-id": "project"}, {}, {}, {})
    body = provider._desired(spec)["logrolebinding"]
    assert body["subjects"] == [{"kind": "ServiceAccount", "name": "logs", "namespace": "system"}]
    current = SimpleNamespace(metadata=SimpleNamespace(labels={"foreign": "yes"}))
    provider._rbac.read_namespaced_role_binding.return_value = current
    with pytest.raises(Conflict):
        provider._upsert("logrolebinding", spec.namespace, body)
    provider._rbac.replace_namespaced_role_binding.assert_not_called()
    current.metadata.labels = spec.labels
    current.metadata.resource_version = "12"
    current.role_ref = object()
    api.sanitize_for_serialization.return_value = body["roleRef"]
    provider._upsert("logrolebinding", spec.namespace, body)
    assert (
        provider._rbac.replace_namespaced_role_binding.call_args.args[2]["metadata"][
            "resourceVersion"
        ]
        == "12"
    )


def test_enabled_log_chart_has_no_database_or_cluster_wide_log_access() -> None:
    chart = str(Path(__file__).resolve().parents[2] / "helm/controlplane")
    command = ["helm", "template", "test", chart, "--set", "logStreaming.enabled=true"]
    assert subprocess.run(command, capture_output=True).returncode != 0
    raw = subprocess.check_output(
        command + ["--set", "logStreaming.existingSecret=log-key"], text=True
    )
    docs = [d for d in yaml.safe_load_all(raw) if d]
    deployment = next(
        d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-logs")
    )
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"].endswith("-logs")
    assert pod["volumes"] == [
        {
            "name": "signing",
            "secret": {"secretName": "log-key", "items": [{"key": "signing-key", "path": "key"}]},
        }
    ]
    assert not any(e["name"].startswith("CP_DATABASE") for e in pod["containers"][0]["env"])
    role = next(
        d
        for d in docs
        if d["kind"] == "ClusterRole" and d["metadata"]["name"].endswith("-project-logs")
    )
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["pods", "pods/log"], "verbs": ["get"]}
    ]
    assert not any(
        d["kind"] == "ClusterRoleBinding" and d["roleRef"]["name"] == role["metadata"]["name"]
        for d in docs
    )
