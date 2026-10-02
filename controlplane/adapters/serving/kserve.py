"""ServingProvider backed by KServe `InferenceService`s.

Canary traffic uses KServe's native split: the newest revision takes
`canaryTrafficPercent` and the revision before it keeps the rest. That needs
KServe's Serverless (Knative) mode; RawDeployment cannot split.

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
ANNOTATION_PREVIOUS = "mlp.io/previous-revision"
SERVICE_ACCOUNT = "mlp-workload"
PREDICT_TIMEOUT_SECONDS = 10


def build_inference_service(spec: ServingSpec) -> dict[str, Any]:
    """The MLflow model server (v2 protocol) loading the revision's artifact.

    `canaryTrafficPercent` is always present: in a merge patch `null` removes the
    field, which is how "this revision takes all traffic" is expressed.
    """
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
                "canaryTrafficPercent": spec.canary_percent,
            }
        },
    }


def _int(raw: str | None) -> int | None:
    return int(raw) if raw and raw.isdigit() else None


def _split(ref: str) -> tuple[str, str]:
    namespace, _, name = ref.partition("/")
    return namespace, name


class KServeServingProvider:
    def __init__(self, api_client: client.ApiClient) -> None:
        self._custom = client.CustomObjectsApi(api_client)

    @classmethod
    def from_kubeconfig(cls, path: str | None = None) -> KServeServingProvider:
        return cls(load_api_client(path))

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

    def deploy(self, spec: ServingSpec) -> str:
        """Create-or-update. Idempotent: applying the same spec twice changes nothing."""
        ref = f"{spec.namespace}/{spec.name}"
        body = build_inference_service(spec)
        existing = self._read(ref)
        if existing is None:
            if spec.canary_percent is None:
                del body["spec"]["predictor"]["canaryTrafficPercent"]
            try:
                self._custom.create_namespaced_custom_object(
                    GROUP, VERSION, spec.namespace, PLURAL, body
                )
                return ref
            except ApiException as exc:
                if exc.status != 409:  # 409: created concurrently, fall through to patch
                    raise
        else:
            # Remember what was serving before, so a canary knows what receives the rest
            # of the traffic and which backend revision to label its metrics with.
            annotations = existing.get("metadata", {}).get("annotations") or {}
            current = annotations.get(ANNOTATION_REVISION)
            previous = (
                current
                if current and current != str(spec.revision)
                else annotations.get(ANNOTATION_PREVIOUS)
            )
            if previous:
                body["metadata"]["annotations"][ANNOTATION_PREVIOUS] = previous
        self._custom.patch_namespaced_custom_object(
            GROUP, VERSION, spec.namespace, PLURAL, spec.name, body
        )
        return ref

    def get_status(self, ref: str) -> ServingStatus:
        service = self._read(ref)
        if service is None:
            return ServingStatus(ServingState.ABSENT)
        annotations = service.get("metadata", {}).get("annotations") or {}
        deployed = _int(annotations.get(ANNOTATION_REVISION))
        previous = _int(annotations.get(ANNOTATION_PREVIOUS))
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
        if not (ready and loaded and deployed is not None):
            return ServingStatus(ServingState.PENDING, deployed)

        predictor = (status.get("components") or {}).get("predictor") or {}
        backend: dict[int, str] = {}
        if predictor.get("latestCreatedRevision"):
            backend[deployed] = predictor["latestCreatedRevision"]
        ready_revisions = [deployed]
        if previous is not None and predictor.get("previousRolledoutRevision"):
            backend[previous] = predictor["previousRolledoutRevision"]
            ready_revisions.append(previous)  # still serving the rest of the traffic
        url = (status.get("address") or {}).get("url") or status.get("url")
        return ServingStatus(
            ServingState.READY,
            deployed,
            ready_revisions=tuple(sorted(ready_revisions)),
            backend_revisions=backend,
            url=url,
        )

    def set_traffic(self, ref: str, split: Mapping[int, int]) -> None:
        """KServe splits between the newest revision and the one before it, so a split
        is expressed as the newest revision's share."""
        namespace, name = _split(ref)
        service = self._read(ref)
        if service is None:
            raise ConnectionError(f"{ref} does not exist")
        annotations = service.get("metadata", {}).get("annotations") or {}
        deployed = _int(annotations.get(ANNOTATION_REVISION))
        allowed = {deployed, _int(annotations.get(ANNOTATION_PREVIOUS))}
        if sum(split.values()) != 100 or set(split) - allowed:
            raise ValueError(f"cannot express split {dict(split)} on {ref}")
        share = split.get(deployed, 0) if deployed is not None else 0
        self._custom.patch_namespaced_custom_object(
            GROUP,
            VERSION,
            namespace,
            PLURAL,
            name,
            {"spec": {"predictor": {"canaryTrafficPercent": None if share >= 100 else share}}},
        )

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
