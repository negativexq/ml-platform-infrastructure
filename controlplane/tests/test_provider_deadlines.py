from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from kubernetes import client

from controlplane.adapters.kubernetes.client import REQUEST_TIMEOUT, BoundedApiClient
from controlplane.adapters.kubernetes.leadership import LeaseLeadership
from controlplane.domain.states import ModelKind
from controlplane.reconciliation.batch import reconcile_batch
from controlplane.reconciliation.model_aliases import AliasResult, ModelAliasReconciler
from controlplane.reconciliation.watchdog import ReconcileWatchdog, progress


def test_sdk_transport_bounds_all_api_groups_and_preserves_lease_timeout() -> None:
    api = BoundedApiClient()
    assert api.configuration.retries == 0
    try:
        with patch.object(client.ApiClient, "call_api") as transport:
            client.CoreV1Api(api).read_namespaced_pod_log("pod", "mlp-team")
            client.CustomObjectsApi(api).get_namespaced_custom_object(
                "argoproj.io", "v1alpha1", "mlp-team", "workflows", "job"
            )
            client.RbacAuthorizationV1Api(api).read_namespaced_role_binding("role", "mlp-team")
            client.NetworkingV1Api(api).read_namespaced_network_policy("policy", "mlp-team")
            assert all(
                c.kwargs["_request_timeout"] == REQUEST_TIMEOUT for c in transport.call_args_list
            )
            client.CoordinationV1Api(api).read_namespaced_lease(
                "lease", "system", _request_timeout=(2, 3)
            )
            assert transport.call_args.kwargs["_request_timeout"] == (2, 3)
    finally:
        api.close()


def test_progress_watchdog_stops_hung_loop_even_while_lease_renews() -> None:
    now = [0.0]
    fatal = MagicMock()
    watchdog = ReconcileWatchdog(10, clock=lambda: now[0], fatal=fatal)
    lease_api = MagicMock()
    lease_api.read_namespaced_lease.return_value = client.V1Lease(
        metadata=client.V1ObjectMeta(resource_version="1"),
        spec=client.V1LeaseSpec(holder_identity="self"),
    )
    leader = LeaseLeadership(lease_api, "system", "lease", identity="self", clock=lambda: now[0])
    assert leader.attempt()
    now[0] = 9
    assert leader.attempt() and not watchdog.check()
    now[0] = 10
    assert leader.attempt() and watchdog.check()
    fatal.assert_called_once()


def test_long_batch_heartbeats_between_entities_and_standby_has_no_watchdog() -> None:
    now = [0.0]
    fatal = MagicMock()
    watchdog = ReconcileWatchdog(10, clock=lambda: now[0], fatal=fatal)
    assert watchdog.worker is None  # main starts only after acquiring leadership
    token = progress.set(watchdog.beat)

    def reconcile(entity: object) -> object:
        now[0] += 9
        assert not watchdog.check()
        return entity

    try:
        assert len(reconcile_batch([uuid4() for _ in range(5)], reconcile, "test")) == 5
    finally:
        progress.reset(token)
    assert now[0] == 45
    fatal.assert_not_called()
    with pytest.raises(ValueError):
        ReconcileWatchdog(0)


def test_kubernetes_socket_read_timeout_interrupts_a_hung_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket
    import threading
    import time

    from urllib3.exceptions import MaxRetryError, ReadTimeoutError

    monkeypatch.setattr("controlplane.adapters.kubernetes.client.REQUEST_TIMEOUT", (0.1, 0.1))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(2)
    finish = threading.Event()

    def hang() -> None:
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(2)
            connection.recv(4096)
            finish.wait(2)  # Accept the request but never send response headers.

    worker = threading.Thread(target=hang, daemon=True)
    worker.start()
    api = BoundedApiClient()
    api.configuration.host = f"http://127.0.0.1:{listener.getsockname()[1]}"
    started = time.monotonic()
    try:
        with pytest.raises((MaxRetryError, ReadTimeoutError)):
            client.CoreV1Api(api).read_namespace("mlp-team")
        assert time.monotonic() - started < 2
    finally:
        finish.set()
        listener.close()
        worker.join(2)
        api.close()


@pytest.mark.parametrize("registry_failure", [False, True])
def test_alias_pass_heartbeats_between_models_even_after_registry_failure(
    registry_failure: bool,
) -> None:
    now = [0.0]
    fatal = MagicMock()
    watchdog = ReconcileWatchdog(300, clock=lambda: now[0], fatal=fatal)
    factory = MagicMock()
    ids = [uuid4() for _ in range(4)]
    factory.return_value.__enter__.return_value.models.list_all.return_value = [
        SimpleNamespace(id=model_id, kind=ModelKind.CLASSIC) for model_id in ids
    ]
    aliases = ModelAliasReconciler(factory, MagicMock())

    def slow_registry(model_id: UUID) -> AliasResult:
        now[0] += 120
        assert not watchdog.check()
        if registry_failure and model_id == ids[0]:
            raise ConnectionError("MLflow unavailable")
        assert model_id in ids
        return AliasResult(model_id)

    token = progress.set(watchdog.beat)
    try:
        with (
            patch.object(aliases, "reconcile", side_effect=slow_registry) as reconcile,
            patch.object(aliases, "_set_drift") as drift,
        ):
            results = aliases.reconcile_all()
            assert reconcile.call_count == 4
            if registry_failure:
                drift.assert_called_once_with(
                    ids[0], "alias sync failed: ConnectionError: MLflow unavailable"
                )
            else:
                drift.assert_not_called()
        assert [result.model_id for result in results] == ids[int(registry_failure) :]
        assert now[0] == 480  # Total pass exceeds the watchdog; each model makes progress.
        fatal.assert_not_called()
    finally:
        progress.reset(token)
