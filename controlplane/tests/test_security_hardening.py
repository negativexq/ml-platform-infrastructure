"""Hardening boundaries without cluster, Docker or database startup."""

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml  # type: ignore[import-untyped]

from controlplane.adapters.kubernetes.provisioner import KubernetesClusterProvider
from controlplane.adapters.serving.kserve import build_inference_service
from controlplane.adapters.workflow.argo import ArgoWorkflowProvider, build_workflow
from controlplane.api.auth import AuthConfig, AuthMiddleware
from controlplane.api.session import SESSION_COOKIE, Signer, principal_to_session
from controlplane.application.jobs import CreateJob, JobService, require_training_digest
from controlplane.application.namespaces import namespace_spec
from controlplane.application.providers import ServingSpec, StepSpec, WorkflowSpec
from controlplane.domain.access import Principal
from controlplane.domain.entities import Project
from controlplane.domain.errors import InvalidArgument
from controlplane.persistence.readiness import _runtime_role
from controlplane.persistence.security import API_TABLES, GATEWAY_TABLES, RECONCILER_TABLES
from controlplane.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
DIGEST = "registry.example.com/team/train@sha256:" + "a" * 64


def test_namespace_and_service_accounts_enforce_separate_boundaries() -> None:
    project = Project.create(name="team", display_name=None, description="", now=datetime.now(UTC))
    spec = namespace_spec(project)
    for mode in ("enforce", "warn", "audit"):
        assert spec.labels[f"pod-security.kubernetes.io/{mode}"] == "restricted"
        assert spec.labels[f"pod-security.kubernetes.io/{mode}-version"] == (
            "v1.30" if mode == "enforce" else "latest"
        )
    assert spec.quota["limits.ephemeral-storage"] == "32Gi"
    assert spec.quota["requests.ephemeral-storage"] == "16Gi"
    assert (
        "ephemeral-storage" in spec.default_limits and "ephemeral-storage" in spec.default_requests
    )
    desired = KubernetesClusterProvider(None)._desired(spec)
    training, serving = desired["serviceaccount"], desired["servingserviceaccount"]
    assert training["metadata"]["name"] == "mlp-training"
    assert training["automountServiceAccountToken"] is True
    assert serving["metadata"]["name"] == "mlp-serving"
    assert serving["automountServiceAccountToken"] is False
    assert desired["legacyserviceaccount"]["automountServiceAccountToken"] is False
    assert desired["rolebinding"]["subjects"][0]["name"] == "mlp-training"
    provider = KubernetesClusterProvider(None)
    provider._core = MagicMock()
    provider._read("servingserviceaccount", "mlp-team", "mlp-serving")
    provider._core.read_namespaced_service_account.assert_called_once_with(
        "mlp-serving", "mlp-team"
    )


def test_argo_secures_main_and_injected_executor_containers() -> None:
    spec = WorkflowSpec("train", "mlp-team", (StepSpec("main", DIGEST),), {})
    body = build_workflow(spec)["spec"]
    assert body["serviceAccountName"] == "mlp-training"
    assert body["securityContext"]["runAsNonRoot"] is True
    assert body["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    context = body["templates"][0]["container"]["securityContext"]
    assert context["allowPrivilegeEscalation"] is False
    assert context["capabilities"]["drop"] == ["ALL"]
    patch = json.loads(body["podSpecPatch"])
    assert patch["initContainers"][0]["name"] == "init"
    assert patch["containers"][0]["name"] == "wait"
    # No inherited primary identity may override an image's numeric USER (e.g. 65532).
    for field in ("runAsUser", "runAsGroup"):
        assert field not in body["securityContext"]
        assert field not in context
    assert body["securityContext"]["fsGroup"] == 1000  # shared-volume supplemental group
    for executor in (patch["initContainers"][0], patch["containers"][0]):
        assert executor["securityContext"] == {**context, "runAsUser": 1000, "runAsGroup": 1000}
    assert "USER 1000:1000" in (ROOT / "docker/training/Dockerfile").read_text()


def test_model_predictor_disables_token_mount_and_sets_restricted_context() -> None:
    spec = ServingSpec(
        name="model", namespace="mlp-team", revision=1, model_uri="s3://bucket/model"
    )
    predictor = build_inference_service(spec)["spec"]["predictor"]
    assert predictor["serviceAccountName"] == "mlp-serving"
    assert predictor["automountServiceAccountToken"] is False
    assert predictor["securityContext"]["runAsNonRoot"] is True
    assert predictor["model"]["securityContext"]["allowPrivilegeEscalation"] is False


@pytest.mark.parametrize(
    "image", ["train:v1", "train:latest", "train", "x@sha256:abc", "x@sha256:" + "A" * 64]
)
def test_runtime_training_policy_refuses_mutable_images_before_writes(image: str) -> None:
    with pytest.raises(InvalidArgument, match="digest"):
        require_training_digest(image)
    factory = MagicMock()
    with pytest.raises(InvalidArgument, match="digest"):
        JobService(factory, require_image_digest=True).create("team", CreateJob("train", image))
    factory.assert_not_called()
    provider = ArgoWorkflowProvider(None)
    provider._custom = MagicMock()
    with pytest.raises(InvalidArgument, match="digest"):
        provider.submit(WorkflowSpec("train", "mlp-team", (StepSpec("main", image),), {}), "run")
    provider._custom.create_namespaced_custom_object.assert_not_called()


def test_full_training_digest_is_accepted() -> None:
    require_training_digest(DIGEST)
    assert Settings().job_image_digest_required


def test_old_eight_hour_cookies_are_invalid_after_upgrade() -> None:
    signer = Signer("x" * 40)
    old = signer.dumps(
        principal_to_session(Principal("alice")), purpose="session", ttl_seconds=28800
    )
    middleware = AuthMiddleware(MagicMock(), AuthConfig(MagicMock(), signer=signer))
    assert middleware._from_cookie(f"{SESSION_COOKIE}={old}") is None
    with pytest.raises(ValueError):
        Settings(session_hours=8)


def test_database_manifest_does_not_grant_gateway_control_plane_writes() -> None:
    assert "audit_events" not in GATEWAY_TABLES
    assert "memberships" in GATEWAY_TABLES  # bearer invoker authorization remains supported
    assert "job_definitions" not in GATEWAY_TABLES
    assert "api_keys" not in RECONCILER_TABLES
    assert "memberships" in API_TABLES


def test_database_readiness_refuses_owner_or_missing_role() -> None:
    connection = MagicMock(dialect=SimpleNamespace(name="postgresql"))
    connection.scalar.return_value = False
    with pytest.raises(RuntimeError, match="runtime role"):
        _runtime_role(connection, "gateway")
    connection.scalar.side_effect = [True, False, False, False, True]
    with pytest.raises(RuntimeError, match="excess read"):
        _runtime_role(connection, "gateway")


@pytest.mark.skipif(not shutil.which("helm"), reason="Helm required")
def test_chart_database_secrets_and_identity_exposure() -> None:
    def render(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["helm", "template", "test", str(ROOT / "helm/controlplane"), *args],
            text=True,
            capture_output=True,
        )

    result = render("--set", "identity.existingSecret=identity")
    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    secrets = {}
    for doc in docs:
        if doc["kind"] not in {"Deployment", "Job"}:
            continue
        component = (
            "migration" if doc["kind"] == "Job" else doc["metadata"]["name"].rsplit("-", 1)[1]
        )
        container = doc["spec"]["template"]["spec"]["containers"][0]
        env = next(env for env in container["env"] if env["name"] == "CP_DATABASE_URL")
        secrets[component] = env["valueFrom"]["secretKeyRef"]["name"]
        identity = [
            entry
            for entry in container.get("envFrom", [])
            if entry.get("secretRef", {}).get("name") == "identity"
        ]
        assert bool(identity) == (component == "api")
    assert len(set(secrets.values())) == 4
    assert secrets["gateway"] == "mlp-controlplane-db-gateway"
    assert (
        render("--set", "database.gateway.existingSecret=mlp-controlplane-db-api").returncode != 0
    )


def test_gateway_bearer_auth_does_not_need_browser_session_credentials() -> None:
    from unittest.mock import patch

    from controlplane.main import auth_config

    with patch("controlplane.main.OidcProvider"):
        config = auth_config(
            Settings(oidc_issuer="https://idp.example", oidc_client_id="web", session_secret=""),
            browser_login=False,
        )
    assert config is not None and config.login is None and config.signer is None
