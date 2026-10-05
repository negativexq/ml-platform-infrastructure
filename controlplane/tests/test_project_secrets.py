from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from controlplane.adapters.fakes import FakeClusterProvider, FakeSecretProvider
from controlplane.adapters.serving.kserve import build_inference_service
from controlplane.adapters.workflow.argo import build_workflow
from controlplane.api.app import create_app
from controlplane.application.jobs import CreateJob, JobService
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import CreateProject, ProjectService
from controlplane.application.providers import ServingSpec
from controlplane.application.runs import RunService
from controlplane.application.secrets import ProjectSecretService
from controlplane.application.workflow_compiler import compile_job_run
from controlplane.domain.errors import Conflict, InvalidArgument, NotFound
from controlplane.domain.secrets import SecretKeyRef, SecretRefs
from controlplane.reconciliation.projects import ProjectReconciler


def setup(
    factory: Callable[[], UnitOfWork], clock: Any
) -> tuple[Any, FakeSecretProvider, ProjectSecretService]:
    project, _ = ProjectService(factory, clock).create(CreateProject("secret-team"))
    ProjectReconciler(factory, FakeClusterProvider(), clock).reconcile(project.id)
    return project, FakeSecretProvider(), ProjectSecretService(factory, None, clock)


def test_write_only_api_cas_rotation_validation_and_audit(
    uow_factory: Callable[[], UnitOfWork], clock: Any
) -> None:
    _, provider, _ = setup(uow_factory, clock)
    client = TestClient(create_app(uow_factory, clock, secrets=provider))
    path = "/projects/secret-team/secrets/credentials"
    password = "must-not-be-returned-123"
    created = client.post(path, json={"values": {"password": password}})
    assert created.status_code == 201 and password not in created.text
    version = created.json()["version"]
    assert password not in client.get("/projects/secret-team/secrets").text
    assert (
        client.put(
            path, json={"values": {"password": "new-value"}, "expected_version": "stale"}
        ).status_code
        == 409
    )
    rotated = client.put(
        path, json={"values": {"password": "new-value"}, "expected_version": version}
    )
    assert rotated.status_code == 200 and "new-value" not in rotated.text
    invalid = client.post(path, json={"values": {"password": password}, "extra": password})
    assert invalid.status_code == 422 and password not in invalid.text
    with uow_factory() as uow:
        events = uow.audit.list(project_id=setup_project_id(uow))
    assert password not in repr([e.payload for e in events]) and "new-value" not in repr(events)
    assert client.delete(path, params={"expected_version": version}).status_code == 409
    assert (
        client.delete(path, params={"expected_version": rotated.json()["version"]}).status_code
        == 204
    )


def setup_project_id(uow: UnitOfWork) -> Any:
    project = uow.projects.get_by_name("secret-team")
    assert project is not None
    return project.id


def test_refs_compile_without_values_and_deletion_is_protected(
    uow_factory: Callable[[], UnitOfWork], clock: Any
) -> None:
    project, provider, _ = setup(uow_factory, clock)
    secrets = ProjectSecretService(uow_factory, provider, clock)
    info = secrets.put("secret-team", "credentials", {"password": "hidden-value"}, "Opaque")
    refs = SecretRefs({"DB_PASSWORD": SecretKeyRef("credentials", "password")})
    jobs = JobService(uow_factory, clock, provider)
    job, _ = jobs.create("secret-team", CreateJob("train-job", "img:1", secret_refs=refs))
    run, _ = RunService(uow_factory, clock).create("secret-team", job.name)
    manifest = build_workflow(compile_job_run(project, job, run))
    env = manifest["spec"]["templates"][0]["container"]["env"]
    assert env == [
        {
            "name": "DB_PASSWORD",
            "valueFrom": {"secretKeyRef": {"name": "credentials", "key": "password"}},
        }
    ]
    assert "hidden-value" not in repr(manifest)
    predictor = build_inference_service(
        ServingSpec("fn", project.namespace, "image:1", 1, runtime="container", secret_refs=refs)
    )["spec"]["predictor"]
    assert predictor["containers"][0]["env"] == env
    with pytest.raises(Conflict, match="referenced"):
        secrets.delete("secret-team", "credentials", info.version)
    with pytest.raises(Conflict):
        secrets.put("secret-team", "credentials", {"other": "x"}, "Opaque", info.version)
    with pytest.raises(NotFound):
        jobs.create(
            "secret-team",
            CreateJob(
                "other-job", "img:1", secret_refs=SecretRefs({"X": SecretKeyRef("missing", "key")})
            ),
        )
    secrets.delete("secret-team", "credentials", info.version, force=True)
    with pytest.raises(NotFound):
        provider.get(project, "credentials")
    with uow_factory() as uow:
        assert uow.jobs.get_by_name(project.id, job.name) is not None


def test_secret_management_requires_admin_and_cannot_cross_projects(
    uow_factory: Callable[[], UnitOfWork], clock: Any
) -> None:
    from unittest.mock import Mock

    from controlplane.api.auth import AuthConfig
    from controlplane.domain.access import Membership, Principal, ProjectRole

    project, provider, _ = setup(uow_factory, clock)
    secrets = ProjectSecretService(uow_factory, provider, clock)
    secrets.put("secret-team", "credentials", {"password": "hidden"}, "Opaque")
    with uow_factory() as uow:
        uow.memberships.add(
            Membership(
                project_id=project.id,
                subject="user:operator",
                role=ProjectRole.OPERATOR,
                created_at=clock(),
                updated_at=clock(),
            )
        )
        uow.commit()
    auth = Mock()
    auth.authenticate.side_effect = lambda token: Principal(
        username=token, platform_admin=token == "admin"
    )
    client = TestClient(create_app(uow_factory, clock, secrets=provider, auth=AuthConfig(auth)))
    base = "/projects/secret-team/secrets"
    for token in ("operator", "stranger"):
        headers = {"authorization": f"Bearer {token}"}
        assert client.get(base, headers=headers).status_code == 403
        assert (
            client.post(
                base + "/other", headers=headers, json={"values": {"key": "hidden"}}
            ).status_code
            == 403
        )
        assert (
            client.put(
                base + "/credentials",
                headers=headers,
                json={"values": {"password": "replacement"}, "expected_version": "1"},
            ).status_code
            == 403
        )
        assert (
            client.delete(
                base + "/credentials",
                headers=headers,
                params={"expected_version": "1", "force": True},
            ).status_code
            == 403
        )
    assert client.get(base, headers={"authorization": "Bearer admin"}).status_code == 200
    catalog = client.get(
        "/projects/secret-team/secret-references", headers={"authorization": "Bearer operator"}
    )
    assert catalog.status_code == 200 and "hidden" not in catalog.text
    assert client.get(base).status_code == 401
    with uow_factory() as uow:
        uow.memberships.add(
            Membership(
                project_id=project.id,
                subject="user:project-admin",
                role=ProjectRole.ADMIN,
                created_at=clock(),
                updated_at=clock(),
            )
        )
        uow.commit()
    assert client.get(base, headers={"authorization": "Bearer project-admin"}).status_code == 200
    other, _ = ProjectService(uow_factory, clock).create(CreateProject("another-team"))
    ProjectReconciler(uow_factory, FakeClusterProvider(), clock).reconcile(other.id)
    with pytest.raises(NotFound):
        provider.get(other, "credentials")


def test_kubernetes_adapter_refuses_foreign_namespace_and_redacts_backend_errors() -> None:
    from unittest.mock import Mock

    from kubernetes.client.exceptions import ApiException

    from controlplane.adapters.kubernetes.secrets import KubernetesSecretProvider
    from controlplane.domain.entities import Project
    from controlplane.tests.conftest import FakeClock

    clock = FakeClock()
    project = Project.create(name="secret-team", display_name=None, description="", now=clock())
    adapter = KubernetesSecretProvider(Mock())
    adapter._core = Mock()
    adapter._core.read_namespace.return_value.metadata.labels = {}
    with pytest.raises(Conflict, match="own"):
        adapter.put(project, "credentials", {"password": "hidden"}, "Opaque", None)
    adapter._core.create_namespaced_secret.assert_not_called()
    adapter._core.read_namespace.side_effect = ApiException(status=500, reason="password=hidden")
    with pytest.raises(Conflict) as error:
        adapter.list(project)
    assert "hidden" not in str(error.value)


def test_registry_refs_and_model_refs_are_validated_and_names_only(
    uow_factory: Callable[[], UnitOfWork], clock: Any
) -> None:
    project, provider, _ = setup(uow_factory, clock)
    client = TestClient(create_app(uow_factory, clock, secrets=provider))
    base = "/projects/secret-team"
    registry = client.post(
        base + "/secrets/registry",
        json={
            "kind": "kubernetes.io/dockerconfigjson",
            "values": {
                ".dockerconfigjson": '{"auths":{"registry.example":{"auth":"private-token"}}}'
            },
        },
    )
    assert registry.status_code == 201 and "private-token" not in registry.text
    client.post(base + "/secrets/credentials", json={"values": {"password": "private-value"}})
    refs = {
        "env": {"PASSWORD": {"name": "credentials", "key": "password"}},
        "image_pull_secrets": ["registry"],
    }
    created = client.post(
        base + "/models",
        json={
            "name": "secure-function",
            "kind": "function",
            "secret_refs": refs,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["secret_refs"] == refs
    listed = client.get(base + "/secrets").json()["items"]
    registry_uses = next(s for s in listed if s["name"] == "registry")["used_by"]
    assert registry_uses == [{"kind": "model", "name": "secure-function", "revision": None}]
    assert "private-value" not in created.text
    assert (
        client.delete(
            base + "/secrets/registry", params={"expected_version": registry.json()["version"]}
        ).status_code
        == 409
    )
    conflict = client.post(
        base + "/models",
        json={
            "name": "conflicting-env",
            "kind": "function",
            "secret_refs": refs,
            "function": {"env": {"PASSWORD": "plain"}},
        },
    )
    assert conflict.status_code == 422
    refs["image_pull_secrets"] = ["credentials"]
    assert (
        client.post(
            base + "/models",
            json={"name": "wrong-registry", "kind": "function", "secret_refs": refs},
        ).status_code
        == 422
    )
    job, _ = JobService(uow_factory, clock, provider).create(
        "secret-team",
        CreateJob(
            "private-job",
            "registry.example/image:1",
            secret_refs=SecretRefs(image_pull_secrets=("registry",)),
        ),
    )
    run, _ = RunService(uow_factory, clock).create("secret-team", job.name)
    assert build_workflow(compile_job_run(project, job, run))["spec"]["imagePullSecrets"] == [
        {"name": "registry"}
    ]


def test_kubernetes_rotation_and_delete_send_resource_version_preconditions() -> None:
    from unittest.mock import Mock

    from kubernetes import client
    from kubernetes.client.exceptions import ApiException

    from controlplane.adapters.kubernetes.secrets import KubernetesSecretProvider
    from controlplane.application.namespaces import LABEL_MANAGED_BY, LABEL_PROJECT_ID, MANAGED_BY
    from controlplane.domain.entities import Project
    from controlplane.tests.conftest import FakeClock

    project = Project.create(
        name="secret-team", display_name=None, description="", now=FakeClock()()
    )
    labels = {LABEL_MANAGED_BY: MANAGED_BY, LABEL_PROJECT_ID: str(project.id)}
    adapter = KubernetesSecretProvider(Mock())
    adapter._core = Mock()
    adapter._core.read_namespace.return_value.metadata.labels = labels
    existing = client.V1Secret(
        metadata=client.V1ObjectMeta(name="credentials", labels=labels, resource_version="12"),
        type="Opaque",
        data={"password": "base64-private-value"},
    )
    adapter._core.read_namespaced_secret.return_value = existing
    adapter._core.replace_namespaced_secret.return_value = existing
    info = adapter.put(project, "credentials", {"password": "replacement"}, "Opaque", "12")
    assert info.keys == ("password",) and "replacement" not in repr(info)
    sent = adapter._core.replace_namespaced_secret.call_args.args[2]
    assert sent.metadata.resource_version == "12"
    assert sent.string_data == {"password": "replacement"}
    adapter.delete(project, "credentials", "12")
    sent_delete = adapter._core.delete_namespaced_secret.call_args.kwargs["body"]
    assert sent_delete.preconditions.resource_version == "12"
    adapter._core.replace_namespaced_secret.side_effect = ApiException(
        status=409, reason="private-value"
    )
    with pytest.raises(Conflict) as error:
        adapter.put(project, "credentials", {"password": "replacement"}, "Opaque", "12")
    assert "private-value" not in str(error.value)


def test_deployment_snapshots_references_without_secret_values(
    uow_factory: Callable[[], UnitOfWork], clock: Any
) -> None:
    from controlplane.adapters.fakes import FakeServingProvider
    from controlplane.application.deployments import DeploymentService
    from controlplane.application.models import ModelService
    from controlplane.domain.states import ModelKind
    from controlplane.reconciliation.deployments import DeploymentReconciler

    project, provider, _ = setup(uow_factory, clock)
    secrets = ProjectSecretService(uow_factory, provider, clock)
    info = secrets.put("secret-team", "credentials", {"password": "old-private"}, "Opaque")
    refs = SecretRefs({"PASSWORD": SecretKeyRef("credentials", "password")})
    models = ModelService(uow_factory, clock, secrets=provider)
    models.create("secret-team", "secure-function", {}, kind=ModelKind.FUNCTION, secret_refs=refs)
    models.register_image(
        "secret-team", "secure-function", "registry.example/fn@sha256:" + "a" * 64
    )
    serving = FakeServingProvider()
    deployments = DeploymentService(uow_factory, clock, serving=serving)
    deployments.create("secret-team", "secure-function")
    deployments.deploy("secret-team", "secure-function", "secure-function", 1)
    DeploymentReconciler(uow_factory, serving, clock).reconcile_all()
    view = deployments.get("secret-team", "secure-function")
    assert view.revisions[0].revision.secret_refs == refs
    assert "old-private" not in repr(view)
    secrets.put("secret-team", "credentials", {"password": "new-private"}, "Opaque", info.version)
    assert (
        deployments.get("secret-team", "secure-function").revisions[0].revision.secret_refs == refs
    )
    with uow_factory() as uow:
        stored = uow.models.get_by_name(project.id, "secure-function")
        assert stored is not None and stored.secret_refs == refs


@pytest.mark.parametrize("name", ["", "a..b", "a.-b", "a-.b", "UPPER", "a" * 254])
def test_secret_names_reject_invalid_dns_subdomains(name: str) -> None:
    with pytest.raises(InvalidArgument):
        SecretKeyRef(name, "key")
