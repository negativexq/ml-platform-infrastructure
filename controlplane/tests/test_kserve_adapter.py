"""Delayed controller snapshots must not change backend revision identity."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from controlplane.adapters.serving.kserve import (
    ANNOTATION_PREVIOUS,
    ANNOTATION_REVISION,
    GROUP,
    KServeServingProvider,
    build_inference_service,
)
from controlplane.application.providers import ServingSpec, ServingState

REF = "mlp-test/function"


def service() -> dict[str, Any]:
    return {
        "metadata": {"annotations": {ANNOTATION_REVISION: "2", ANNOTATION_PREVIOUS: "1"}},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "modelStatus": {"transitionStatus": "UpToDate"},
            "components": {
                "predictor": {
                    "latestCreatedRevision": "candidate",
                    "latestReadyRevision": "candidate",
                    "previousRolledoutRevision": "stable",
                }
            },
            "address": {"url": "http://function.example"},
        },
    }


def revision(number: int, *, ready: bool = True) -> dict[str, Any]:
    return {
        "metadata": {
            "annotations": {ANNOTATION_REVISION: str(number)},
            "labels": {"serving.kserve.io/inferenceservice": "function"},
        },
        "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "Unknown"}]},
    }


def provider(snapshot: dict[str, Any], backends: dict[str, Any]) -> KServeServingProvider:
    def read(group: str, version: str, namespace: str, plural: str, name: str) -> Any:
        assert namespace == "mlp-test"
        if group == GROUP:
            return snapshot
        assert (group, version, plural) == ("serving.knative.dev", "v1", "revisions")
        if name not in backends:
            raise ApiException(status=404)
        value = backends[name]
        if isinstance(value, Exception):
            raise value
        return value

    with patch("controlplane.adapters.serving.kserve.client.CustomObjectsApi") as api:
        api.return_value.get_namespaced_custom_object.side_effect = read
        return KServeServingProvider(Mock())


@pytest.mark.parametrize("runtime", ["mlflow", "huggingface", "container"])
def test_revision_identity_is_in_the_predictor_template(runtime: str) -> None:
    body = build_inference_service(
        ServingSpec(
            name="function", namespace="mlp-test", model_uri="image:1", revision=2, runtime=runtime
        )
    )
    assert body["spec"]["predictor"]["annotations"][ANNOTATION_REVISION] == "2"


def test_old_ready_backend_cannot_be_relabelled_as_candidate() -> None:
    snapshot = service()
    predictor = snapshot["status"]["components"]["predictor"]
    predictor.update(latestCreatedRevision="stable", latestReadyRevision="stable")
    status = provider(snapshot, {"stable": revision(1)}).get_status(REF)
    assert status.state is ServingState.PENDING
    assert status.deployed_revision == 2
    assert status.ready_revisions == () and not status.backend_revisions


@pytest.mark.parametrize("change", ["missing", "unmarked", "foreign", "not-ready", "not-latest"])
def test_unproven_candidate_stays_pending(change: str) -> None:
    snapshot = service()
    candidate = revision(2)
    backends = {"candidate": candidate, "stable": revision(1)}
    if change == "missing":
        del backends["candidate"]
    elif change == "unmarked":
        candidate["metadata"]["annotations"] = {}
    elif change == "foreign":
        candidate["metadata"]["labels"]["serving.kserve.io/inferenceservice"] = "another"
    elif change == "not-ready":
        backends["candidate"] = revision(2, ready=False)
    else:
        snapshot["status"]["components"]["predictor"]["latestReadyRevision"] = "stable"
    assert provider(snapshot, backends).get_status(REF).state is ServingState.PENDING


def test_candidate_and_stable_metrics_use_verified_backend_identities() -> None:
    status = provider(service(), {"candidate": revision(2), "stable": revision(1)}).get_status(REF)
    assert status.state is ServingState.READY
    assert status.ready_revisions == (1, 2)
    assert status.backend_revisions == {1: "stable", 2: "candidate"}


@pytest.mark.parametrize("stable", [revision(3), revision(1, ready=False), None])
def test_unproven_previous_backend_is_not_used_for_metrics(stable: Any) -> None:
    backends = {"candidate": revision(2)}
    if stable is not None:
        backends["stable"] = stable
    status = provider(service(), backends).get_status(REF)
    assert status.ready_revisions == (2,)
    assert status.backend_revisions == {2: "candidate"}


@pytest.mark.parametrize("model_status", [None, {"transitionStatus": "InProgress"}])
def test_custom_containers_and_idle_services_are_callable(model_status: Any) -> None:
    snapshot = service()
    if model_status is None:
        del snapshot["status"]["modelStatus"]
    else:
        snapshot["status"]["modelStatus"] = model_status
    serving = provider(snapshot, {"candidate": revision(2), "stable": revision(1)})
    with patch("urllib.request.urlopen") as request:
        request.return_value.__enter__.return_value.read.return_value = b'{"answer": 42}'
        assert serving.invoke(REF, {"input": 1}) == {"answer": 42}
        assert request.call_args.args[0].full_url == "http://function.example/"


def test_historical_failure_does_not_override_ready_backend() -> None:
    snapshot = service()
    snapshot["status"]["modelStatus"]["lastFailureInfo"] = {"message": "old failure"}
    assert (
        provider(snapshot, {"candidate": revision(2)}).get_status(REF).state is ServingState.READY
    )


def test_failure_of_matching_backend_is_reported() -> None:
    snapshot = service()
    snapshot["status"]["modelStatus"] = {
        "transitionStatus": "BlockedByFailedLoad",
        "lastFailureInfo": {"message": "load failed", "modelRevisionName": "candidate"},
    }
    status = provider(snapshot, {"candidate": revision(2, ready=False)}).get_status(REF)
    assert status.state is ServingState.FAILED and status.reason == "load failed"


@pytest.mark.parametrize("failed_backend", [None, "stable"])
def test_unattributed_failure_cannot_fail_a_new_backend(failed_backend: str | None) -> None:
    snapshot = service()
    snapshot["status"]["modelStatus"] = {
        "transitionStatus": "BlockedByFailedLoad",
        "lastFailureInfo": {"message": "load failed", "modelRevisionName": failed_backend},
    }
    status = provider(snapshot, {"candidate": revision(2, ready=False)}).get_status(REF)
    assert status.state is ServingState.PENDING


def test_a_ready_backend_overrides_a_lingering_failure_state() -> None:
    snapshot = service()
    snapshot["status"]["modelStatus"] = {
        "transitionStatus": "BlockedByFailedLoad",
        "lastFailureInfo": {"message": "old failure", "modelRevisionName": "candidate"},
    }
    assert (
        provider(snapshot, {"candidate": revision(2)}).get_status(REF).state is ServingState.READY
    )


def test_old_failure_is_not_attributed_to_new_revision() -> None:
    snapshot = service()
    snapshot["status"]["modelStatus"] = {
        "transitionStatus": "BlockedByFailedLoad",
        "lastFailureInfo": {"message": "load failed"},
    }
    assert (
        provider(snapshot, {"candidate": revision(1)}).get_status(REF).state is ServingState.PENDING
    )


def test_revision_read_errors_are_not_silently_treated_as_ready() -> None:
    with pytest.raises(ApiException) as error:
        provider(service(), {"candidate": ApiException(status=403)}).get_status(REF)
    assert error.value.status == 403


def test_repaired_same_platform_revision_waits_for_new_apply_identity() -> None:
    from controlplane.adapters.serving.kserve import ANNOTATION_APPLY

    snapshot = service()
    snapshot["spec"] = {"predictor": {"annotations": {ANNOTATION_APPLY: "repair"}}}
    old = revision(2)
    old["metadata"]["annotations"][ANNOTATION_APPLY] = "old-apply"
    adapter = provider(snapshot, {"candidate": old})
    assert adapter.get_status(REF).state is ServingState.PENDING
    old["metadata"]["annotations"][ANNOTATION_APPLY] = "repair"
    assert adapter.get_status(REF).state is ServingState.READY


def test_owned_spec_detects_removed_or_injected_env_but_allows_defaults() -> None:
    from copy import deepcopy

    from controlplane.adapters.serving.kserve import _owned_matches

    spec = ServingSpec("function", "mlp-test", "image:1", 2, runtime="container")
    desired = build_inference_service(spec)["spec"]["predictor"]
    actual = deepcopy(desired)
    actual["containers"][0]["terminationMessagePolicy"] = "File"
    assert _owned_matches(actual, desired)
    actual["containers"][0]["env"] = [{"name": "INJECTED", "value": "override"}]
    assert not _owned_matches(actual, desired)


def test_drift_repair_replaces_predictor_with_cas_and_then_is_idempotent() -> None:
    from copy import deepcopy

    from controlplane.adapters.serving.kserve import ANNOTATION_APPLY

    spec = ServingSpec("function", "mlp-test", "image:1", 2, runtime="container")
    actual = deepcopy(build_inference_service(spec))
    actual["metadata"]["resourceVersion"] = "42"
    actual["metadata"]["annotations"]["external.example/note"] = "keep"
    actual["spec"]["predictor"]["containers"][0]["env"] = [
        {"name": "INJECTED", "value": "override"}
    ]
    adapter = provider(actual, {})
    assert not adapter.matches(spec)
    assert adapter.deploy(spec) == REF
    call = adapter._custom.patch_namespaced_custom_object.call_args
    assert call.kwargs["_content_type"] == "application/json-patch+json"
    patches = call.args[5]
    assert patches[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "42"}
    for operation in patches[1:]:
        if operation["path"] == "/spec/predictor":
            actual["spec"]["predictor"] = operation["value"]
        else:
            actual["metadata"][operation["path"].split("/")[-1]] = operation["value"]
    assert actual["spec"]["predictor"]["containers"][0]["env"] == []
    assert actual["spec"]["predictor"]["annotations"][ANNOTATION_APPLY]
    assert actual["metadata"]["annotations"]["external.example/note"] == "keep"
    assert adapter.matches(spec)
    adapter.deploy(spec)
    adapter._custom.patch_namespaced_custom_object.assert_called_once()


def test_storage_account_is_revision_scoped_and_never_grants_workload_rbac() -> None:
    from kubernetes import client

    from controlplane.adapters.serving.kserve import storage_account_name
    from controlplane.domain.errors import Conflict
    from controlplane.domain.secrets import SecretRefs

    spec = ServingSpec(
        "private",
        "mlp-test",
        "s3://models/v1",
        1,
        secret_refs=SecretRefs(storage_secret="s3-creds"),
    )
    adapter = KServeServingProvider(Mock())
    adapter._core = Mock()
    adapter._core.read_namespaced_service_account.side_effect = ApiException(status=404)
    assert adapter._storage_account(spec, write=True)
    body = adapter._core.create_namespaced_service_account.call_args.args[1]
    assert body.automount_service_account_token is False
    assert [s.name for s in body.secrets] == ["s3-creds"]
    assert build_inference_service(spec)["spec"]["predictor"][
        "serviceAccountName"
    ] == storage_account_name(spec)
    from dataclasses import replace

    assert storage_account_name(replace(spec, revision=2)) != storage_account_name(spec)
    adapter._core.read_namespaced_service_account.side_effect = None
    adapter._core.read_namespaced_service_account.return_value = client.V1ServiceAccount(
        metadata=client.V1ObjectMeta(name=body.metadata.name, labels={"foreign": "yes"})
    )
    with pytest.raises(Conflict, match="not owned"):
        adapter._storage_account(spec, write=True)


def test_delete_preserves_storage_accounts_until_service_is_absent() -> None:
    adapter = KServeServingProvider(Mock())
    adapter._custom = Mock()
    adapter._core = Mock()
    adapter._custom.get_namespaced_custom_object.return_value = {
        "metadata": {"deletionTimestamp": "now"}
    }
    adapter.delete(REF)
    adapter._core.list_namespaced_service_account.assert_not_called()
    adapter._core.read_namespace.assert_not_called()


def test_delete_cleans_all_owned_revisions_with_preconditions_and_skips_foreign() -> None:
    from kubernetes import client

    from controlplane.adapters.serving.kserve import storage_account_name
    from controlplane.application.namespaces import LABEL_MANAGED_BY, MANAGED_BY

    adapter = KServeServingProvider(Mock())
    adapter._custom = Mock()
    adapter._custom.delete_namespaced_custom_object.side_effect = ApiException(status=404)
    adapter._custom.get_namespaced_custom_object.side_effect = ApiException(status=404)
    adapter._core = Mock()
    adapter._core.read_namespace.return_value = client.V1Namespace(
        metadata=client.V1ObjectMeta(
            labels={LABEL_MANAGED_BY: MANAGED_BY, "mlp.io/project-id": "project"}
        )
    )

    def account(number: int, *, owner: str = "function") -> Any:
        spec = ServingSpec(owner, "mlp-test", "s3://model", number)
        return client.V1ServiceAccount(
            metadata=client.V1ObjectMeta(
                name=storage_account_name(spec),
                uid=f"uid-{number}",
                resource_version="12",
                labels={
                    "mlp.io/project-id": "project",
                    "mlp.io/deployment": owner,
                    "mlp.io/storage-owner": owner,
                    "mlp.io/storage-revision": str(number),
                },
            )
        )

    first, second, foreign = account(1), account(2), account(3, owner="other")
    forged = account(4)
    forged.metadata.name = "mlp-workload"
    adapter._core.list_namespaced_service_account.side_effect = [
        client.V1ServiceAccountList(
            items=[first, foreign, forged], metadata=client.V1ListMeta(_continue="next")
        ),
        client.V1ServiceAccountList(items=[second], metadata=client.V1ListMeta()),
    ]
    adapter.delete(REF)
    deletes = adapter._core.delete_namespaced_service_account.call_args_list
    assert [c.args[0] for c in deletes] == [first.metadata.name, second.metadata.name]
    assert deletes[0].kwargs["body"].preconditions.uid == "uid-1"
    assert deletes[0].kwargs["body"].preconditions.resource_version == "12"
    assert adapter._core.list_namespaced_service_account.call_args.kwargs["_continue"] == "next"
    adapter._core.list_namespaced_service_account.side_effect = None
    adapter._core.list_namespaced_service_account.return_value = client.V1ServiceAccountList(
        items=[first], metadata=client.V1ListMeta()
    )
    adapter._core.delete_namespaced_service_account.side_effect = ApiException(status=409)
    with pytest.raises(ApiException) as conflict:
        adapter.delete(REF)
    assert conflict.value.status == 409
