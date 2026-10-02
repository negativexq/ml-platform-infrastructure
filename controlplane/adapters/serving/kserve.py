"""ServingProvider backed by KServe `InferenceService`s.

Not exercised against a real KServe yet: see docs/local-verification.md.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from controlplane.adapters.kubernetes import load_api_client
from controlplane.application.providers import ServingSpec, ServingState, ServingStatus

GROUP, VERSION, PLURAL = "serving.kserve.io", "v1beta1", "inferenceservices"
ANNOTATION_REVISION = "mlp.io/revision"
SERVICE_ACCOUNT = "mlp-workload"
PREDICT_TIMEOUT_SECONDS = 10


def build_inference_service(spec: ServingSpec) -> dict[str, Any]:
    """The MLflow model server (v2 protocol) loading the revision's artifact."""
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "InferenceService",
        "metadata": {
            "name": spec.name,
            "namespace": spec.namespace,
            "labels": dict(spec.labels),
            "annotations": {ANNOTATION_REVISION: str(spec.revision)},
        },
        "spec": {
            "predictor": {
                "serviceAccountName": SERVICE_ACCOUNT,
                "model": {
                    "modelFormat": {"name": "mlflow"},
                    "protocolVersion": "v2",
                    "storageUri": spec.model_uri,
                },
            }
        },
    }


def _split(ref: str) -> tuple[str, str]:
    namespace, _, name = ref.partition("/")
    return namespace, name


class KServeServingProvider:
    def __init__(self, api_client: client.ApiClient) -> None:
        self._custom = client.CustomObjectsApi(api_client)

    @classmethod
    def from_kubeconfig(cls, path: str | None = None) -> KServeServingProvider:
        return cls(load_api_client(path))

    def deploy(self, spec: ServingSpec) -> str:
        """Create-or-update. Idempotent: applying the same spec twice changes nothing."""
        body = build_inference_service(spec)
        try:
            self._custom.create_namespaced_custom_object(
                GROUP, VERSION, spec.namespace, PLURAL, body
            )
        except ApiException as exc:
            if exc.status != 409:
                raise
            self._custom.patch_namespaced_custom_object(
                GROUP, VERSION, spec.namespace, PLURAL, spec.name, body
            )
        return f"{spec.namespace}/{spec.name}"

    def _read(self, ref: str) -> dict[str, Any] | None:
        namespace, name = _split(ref)
        try:
            service: dict[str, Any] = self._custom.get_namespaced_custom_object(
                GROUP, VERSION, namespace, PLURAL, name
            )
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        return service

    def get_status(self, ref: str) -> ServingStatus:
        service = self._read(ref)
        if service is None:
            return ServingStatus(ServingState.ABSENT)
        annotations = service.get("metadata", {}).get("annotations") or {}
        raw = annotations.get(ANNOTATION_REVISION)
        deployed = int(raw) if raw and raw.isdigit() else None
        status = service.get("status") or {}
        model_status = status.get("modelStatus") or {}

        failure = model_status.get("lastFailureInfo")
        if failure:
            return ServingStatus(
                ServingState.FAILED,
                deployed,
                reason=failure.get("message") or failure.get("reason"),
            )
        ready = any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in status.get("conditions") or []
        )
        # `UpToDate` means the model the spec asks for is the one that loaded.
        loaded = model_status.get("transitionStatus") == "UpToDate"
        if ready and loaded and deployed is not None:
            url = (status.get("address") or {}).get("url") or status.get("url")
            return ServingStatus(ServingState.READY, deployed, (deployed,), url)
        return ServingStatus(ServingState.PENDING, deployed)

    def set_traffic(self, ref: str, split: Mapping[int, int]) -> None:
        raise NotImplementedError("traffic splitting between revisions arrives with M19")

    def predict(self, ref: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        status = self.get_status(ref)
        if status.state is not ServingState.READY or not status.url:
            raise ConnectionError(f"{ref} is not serving")
        _, name = _split(ref)
        request = urllib.request.Request(
            f"{status.url.rstrip('/')}/v2/models/{name}/infer",
            data=json.dumps(dict(payload)).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=PREDICT_TIMEOUT_SECONDS) as response:  # noqa: S310
                body: Mapping[str, Any] = json.load(response)
        except urllib.error.URLError as exc:
            raise ConnectionError(f"inference request to {ref} failed: {exc}") from exc
        return body

    def delete(self, ref: str) -> None:
        namespace, name = _split(ref)
        try:
            self._custom.delete_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
        except ApiException as exc:
            if exc.status != 404:
                raise
