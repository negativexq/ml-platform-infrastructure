"""ServingProvider backed by KServe `InferenceService`s.

Canary traffic uses KServe's native split: the newest revision takes
`canaryTrafficPercent` and the revision before it keeps the rest. That needs
KServe's Serverless (Knative) mode; RawDeployment cannot split.

Not exercised against a real KServe yet: see docs/local-verification.md.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.utils.quantity import parse_quantity

from controlplane.adapters.kubernetes import load_api_client
from controlplane.adapters.kubernetes.security import (
    SERVING_ACCOUNT,
    container_security,
    pod_security,
)
from controlplane.application.context import current_traceparent
from controlplane.application.namespaces import LABEL_MANAGED_BY, LABEL_PROJECT_ID, MANAGED_BY
from controlplane.application.providers import ServingSpec, ServingState, ServingStatus
from controlplane.domain.entities import FunctionServing
from controlplane.domain.errors import Conflict

GROUP, VERSION, PLURAL = "serving.kserve.io", "v1beta1", "inferenceservices"
KNATIVE_GROUP, KNATIVE_VERSION = "serving.knative.dev", "v1"
ANNOTATION_REVISION = "mlp.io/revision"
ANNOTATION_PREVIOUS = "mlp.io/previous-revision"
ANNOTATION_APPLY = "mlp.io/apply-id"

SERVICE_ACCOUNT = SERVING_ACCOUNT
PREDICT_TIMEOUT_SECONDS = 10
CHAT_TIMEOUT_SECONDS = 120


# Per GPU an LLM replica gets this much CPU and memory (weights load through host memory).
LLM_CPU_PER_GPU = ("4", "8")  # request, limit
LLM_MEMORY_PER_GPU_GI = (16, 24)
HF_TOKEN_SECRET = "mlp-hf-token"  # optional, per project namespace: gated hub models


def _function_predictor(spec: ServingSpec) -> dict[str, Any]:
    """A function: the project's own container as a KServe custom predictor. In Serverless
    mode Knative scales it between min and max replicas, to zero when idle."""
    f = FunctionServing.from_json(dict(spec.function or {})).to_json()
    env = f.get("env") or {}
    assert isinstance(env, dict)
    port = int(str(f["port"]))
    probe = (
        {"httpGet": {"path": f["readiness_path"], "port": port}}
        if f["readiness_path"]
        else {"tcpSocket": {"port": port}}
    )
    return {
        "minReplicas": int(str(f["min_scale"])),
        "maxReplicas": int(str(f["max_scale"])),
        "containerConcurrency": int(str(f["concurrency"])),
        "containers": [
            {
                "name": "kserve-container",
                "securityContext": container_security(),
                "image": spec.model_uri,
                "ports": [{"containerPort": port, "protocol": "TCP"}],
                "env": [{"name": k, "value": str(v)} for k, v in sorted(env.items())]
                + _secret_env(spec),
                "resources": {
                    "requests": f["requests"],
                    "limits": f["limits"],
                },
                "readinessProbe": {
                    **probe,
                    "timeoutSeconds": f["readiness_timeout_seconds"],
                    "initialDelaySeconds": f["readiness_initial_delay_seconds"],
                    "periodSeconds": 5,
                },
            }
        ],
    }


def _secret_env(spec: ServingSpec) -> list[dict[str, Any]]:
    return [
        {"name": k, "valueFrom": {"secretKeyRef": {"name": r.name, "key": r.key}}}
        for k, r in sorted(spec.secret_refs.env.items())
    ]


def _predictor_model(spec: ServingSpec) -> dict[str, Any]:
    if spec.runtime != "huggingface":
        return {
            "modelFormat": {"name": "mlflow"},
            "protocolVersion": "v2",
            "storageUri": spec.model_uri,
            **({"env": _secret_env(spec)} if spec.secret_refs.env else {}),
        }
    # KServe's Hugging Face server on its vLLM backend: OpenAI-compatible chat completions
    # at /openai/v1/chat/completions, the served model named after the deployment.
    args = [f"--model_name={spec.name}"]
    if spec.context_length:
        args.append(f"--max_model_len={spec.context_length}")
    if spec.gpus > 1:
        args.append(f"--tensor_parallel_size={spec.gpus}")
    gpus = str(spec.gpus)
    cpu_request, cpu_limit = LLM_CPU_PER_GPU
    mem_request, mem_limit = LLM_MEMORY_PER_GPU_GI
    return {
        "modelFormat": {"name": "huggingface"},
        "storageUri": spec.model_uri.replace("@", ":", 1),
        "args": args,
        "env": _secret_env(spec)
        + (
            [
                {
                    "name": "HF_TOKEN",
                    "valueFrom": {
                        "secretKeyRef": {"name": HF_TOKEN_SECRET, "key": "token", "optional": True}
                    },
                }
            ]
            if "HF_TOKEN" not in spec.secret_refs.env
            else []
        ),
        "resources": {
            "requests": {
                "cpu": str(int(cpu_request) * spec.gpus),
                "memory": f"{mem_request * spec.gpus}Gi",
                "nvidia.com/gpu": gpus,
            },
            "limits": {
                "cpu": str(int(cpu_limit) * spec.gpus),
                "memory": f"{mem_limit * spec.gpus}Gi",
                "nvidia.com/gpu": gpus,
            },
        },
    }


def storage_account_name(spec: ServingSpec) -> str:
    identity = hashlib.sha256(spec.name.encode()).hexdigest()[:12]
    return f"mlp-storage-{identity}-{spec.revision}"


def build_inference_service(spec: ServingSpec) -> dict[str, Any]:
    """The revision's model server: the MLflow server (v2 protocol) for classic models, an
    LLM runtime for language models.

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
                "automountServiceAccountToken": False,
                "securityContext": pod_security(),
                "serviceAccountName": storage_account_name(spec)
                if spec.secret_refs.storage_secret
                else SERVICE_ACCOUNT,
                **(
                    {"minReplicas": spec.min_scale, "maxReplicas": spec.max_scale}
                    if spec.runtime == "huggingface"
                    else {}
                ),
                **(
                    {"imagePullSecrets": [{"name": n} for n in spec.secret_refs.image_pull_secrets]}
                    if spec.secret_refs.image_pull_secrets
                    else {}
                ),
                # KServe copies component annotations into the immutable Knative
                # Revision template. Resource metadata alone describes intent.
                "annotations": {ANNOTATION_REVISION: str(spec.revision)},
                **(
                    _function_predictor(spec)
                    if spec.runtime == "container"
                    else {
                        "model": {**_predictor_model(spec), "securityContext": container_security()}
                    }
                ),
                "canaryTrafficPercent": spec.canary_percent,
            }
        },
    }


def _owned_matches(actual: Any, desired: Any, key: str = "") -> bool:
    if key in {"requests", "limits"}:
        if not isinstance(actual, dict) or set(actual) != set(desired):
            return False
        try:
            return all(parse_quantity(actual[k]) == parse_quantity(v) for k, v in desired.items())
        except (ValueError, TypeError):
            return False
    if isinstance(desired, dict):
        if not isinstance(actual, dict):
            return False
        # These fields affect execution. Extra defaults outside these owned fields
        # are allowed, while injected command/env/resource overrides are drift.
        owned = {
            "model",
            "containers",
            "storageUri",
            "runtime",
            "env",
            "envFrom",
            "args",
            "command",
            "resources",
            "imagePullSecrets",
            "minReplicas",
            "maxReplicas",
            "containerConcurrency",
            "volumeMounts",
            "volumes",
            "readinessProbe",
            "securityContext",
            "automountServiceAccountToken",
        }
        if any(k not in desired and actual.get(k) not in (None, [], {}) for k in owned):
            return False
        return all(_owned_matches(actual.get(k), v, k) for k, v in desired.items())
    if isinstance(desired, list):
        if not isinstance(actual, list) or len(actual) != len(desired):
            return False
        if key in {"env", "imagePullSecrets"}:
            actual, desired = (
                sorted(actual, key=lambda x: x["name"]),
                sorted(desired, key=lambda x: x["name"]),
            )
        return all(_owned_matches(a, d) for a, d in zip(actual, desired, strict=True))
    return bool(actual == desired)


def _headers() -> dict[str, str]:
    """Carry the caller's trace into the model server (the W3C `traceparent` header)."""
    headers = {"content-type": "application/json"}
    traceparent = current_traceparent()
    if traceparent:
        headers["traceparent"] = traceparent
    return headers


def _int(raw: str | None) -> int | None:
    return int(raw) if raw and raw.isdigit() else None


def _split(ref: str) -> tuple[str, str]:
    namespace, _, name = ref.partition("/")
    return namespace, name


def _ready(status: Mapping[str, Any]) -> bool:
    return any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in status.get("conditions") or []
    )


class KServeServingProvider:
    def __init__(self, api_client: client.ApiClient) -> None:
        self._custom = client.CustomObjectsApi(api_client)
        self._core = client.CoreV1Api(api_client)

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

    def matches(self, spec: ServingSpec) -> bool:
        existing = self._read(f"{spec.namespace}/{spec.name}")
        return (
            existing is not None
            and self._matches(existing, spec)
            and not self._storage_account(spec, write=False)
        )

    def _storage_account(self, spec: ServingSpec, *, write: bool) -> bool:
        """Return drift; create/repair only an owned revision-scoped serving account."""
        secret = spec.secret_refs.storage_secret
        if not secret:
            return False
        if spec.runtime != "mlflow" or not spec.model_uri.startswith("s3://"):
            raise Conflict("storage credentials require classic S3 serving")
        name = storage_account_name(spec)
        labels = {
            **spec.labels,
            "mlp.io/storage-owner": spec.name,
            "mlp.io/storage-revision": str(spec.revision),
        }
        try:
            current = self._core.read_namespaced_service_account(name, spec.namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
            current = None
        if current is not None:
            actual_labels = current.metadata.labels or {}
            if any(actual_labels.get(k) != v for k, v in labels.items()):
                raise Conflict("storage service account is not owned by this revision")
            names = [s.name for s in (current.secrets or [])]
            if names == [secret] and current.automount_service_account_token is False:
                return False
        if write:
            body = client.V1ServiceAccount(
                metadata=client.V1ObjectMeta(
                    name=name,
                    namespace=spec.namespace,
                    labels=labels,
                    resource_version=current.metadata.resource_version if current else None,
                ),
                secrets=[client.V1ObjectReference(name=secret)],
                automount_service_account_token=False,
            )
            if current is None:
                self._core.create_namespaced_service_account(spec.namespace, body)
            else:
                self._core.replace_namespaced_service_account(name, spec.namespace, body)
        return True

    @staticmethod
    def _matches(existing: dict[str, Any], spec: ServingSpec) -> bool:
        desired = build_inference_service(spec)
        return (
            _owned_matches(existing.get("spec", {}).get("predictor"), desired["spec"]["predictor"])
            and all(
                existing.get("metadata", {}).get("labels", {}).get(k) == v
                for k, v in spec.labels.items()
            )
            and (existing.get("metadata", {}).get("annotations") or {}).get(ANNOTATION_REVISION)
            == str(spec.revision)
        )

    def deploy(self, spec: ServingSpec) -> str:
        """Create-or-update. Idempotent: applying the same spec twice changes nothing."""
        ref = f"{spec.namespace}/{spec.name}"
        storage_changed = self._storage_account(spec, write=True)
        body = build_inference_service(spec)
        existing = self._read(ref)
        if existing is None:
            body["spec"]["predictor"]["annotations"][ANNOTATION_APPLY] = uuid4().hex
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
            if self._matches(existing, spec) and not storage_changed:
                return ref
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
        # Replace the owned predictor rather than merge: removed env/args/resources
        # must not survive the repair. Test resourceVersion to avoid lost updates.
        current = existing if existing is not None else self._read(ref)
        if current is None:
            raise ConnectionError("serving resource disappeared during apply")
        patches = []
        version = current.get("metadata", {}).get("resourceVersion")
        if version:
            patches.append({"op": "test", "path": "/metadata/resourceVersion", "value": version})
        annotations = {
            **(current.get("metadata", {}).get("annotations") or {}),
            **body["metadata"]["annotations"],
        }
        labels = {**(current.get("metadata", {}).get("labels") or {}), **spec.labels}
        predictor = body["spec"]["predictor"]
        predictor["annotations"][ANNOTATION_APPLY] = uuid4().hex
        if spec.canary_percent is None:
            predictor.pop("canaryTrafficPercent", None)
        patches.extend(
            [
                {"op": "add", "path": "/metadata/annotations", "value": annotations},
                {"op": "add", "path": "/metadata/labels", "value": labels},
                {"op": "add", "path": "/spec/predictor", "value": predictor},
            ]
        )
        self._custom.patch_namespaced_custom_object(
            GROUP,
            VERSION,
            spec.namespace,
            PLURAL,
            spec.name,
            patches,
            _content_type="application/json-patch+json",
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

        predictor = (status.get("components") or {}).get("predictor") or {}
        namespace, name = _split(ref)
        latest = predictor.get("latestCreatedRevision")
        expected_apply = (
            ((service.get("spec") or {}).get("predictor") or {})
            .get("annotations", {})
            .get(ANNOTATION_APPLY)
        )
        observed = self._read_revision(namespace, name, latest, expected_apply) if latest else None
        if observed is None or observed[0] != deployed or deployed is None or latest is None:
            return ServingStatus(ServingState.PENDING, deployed)

        # Failure info can survive a successful load, or describe an old backend.
        failure = model_status.get("lastFailureInfo")
        if (
            failure
            and not observed[1]
            and failure.get("modelRevisionName") == latest
            and model_status.get("transitionStatus")
            in {
                "BlockedByFailedLoad",
                "InvalidSpec",
            }
        ):
            return ServingStatus(
                ServingState.FAILED,
                deployed,
                reason=failure.get("message") or failure.get("reason"),
            )
        # Knative Revision readiness works for both model servers and custom
        # containers, and remains true when the activator scales pods to zero.
        # KServe may reset modelStatus to InProgress at zero pods; it is not a
        # callability test. Nor is ISVC observedGeneration a safe revision ID:
        # some versions copy a child's generation into that field.
        if not (_ready(status) and observed[1] and latest == predictor.get("latestReadyRevision")):
            return ServingStatus(ServingState.PENDING, deployed)

        backend: dict[int, str] = {deployed: latest}
        ready_revisions = [deployed]
        previous_backend = predictor.get("previousRolledoutRevision")
        if previous is not None and previous != deployed and previous_backend:
            old = self._read_revision(namespace, name, previous_backend)
            if old is not None and old == (previous, True):
                backend[previous] = previous_backend
                ready_revisions.append(previous)
        url = (status.get("address") or {}).get("url") or status.get("url")
        return ServingStatus(
            ServingState.READY,
            deployed,
            ready_revisions=tuple(sorted(ready_revisions)),
            backend_revisions=backend,
            url=url,
        )

    def _read_revision(
        self, namespace: str, name: str, backend: str, expected_apply: str | None = None
    ) -> tuple[int | None, bool] | None:
        """Read identity from the backend itself; never relabel a stale status."""
        try:
            revision = self._custom.get_namespaced_custom_object(
                KNATIVE_GROUP, KNATIVE_VERSION, namespace, "revisions", backend
            )
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        metadata = revision.get("metadata") or {}
        labels = metadata.get("labels") or {}
        if labels.get("serving.kserve.io/inferenceservice") != name:
            return None
        annotations = metadata.get("annotations") or {}
        if expected_apply is not None and annotations.get(ANNOTATION_APPLY) != expected_apply:
            return None
        return _int(annotations.get(ANNOTATION_REVISION)), _ready(revision.get("status") or {})

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
            headers=_headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=PREDICT_TIMEOUT_SECONDS) as response:  # noqa: S310
                body: Mapping[str, Any] = json.load(response)
        except urllib.error.URLError as exc:
            raise ConnectionError(f"inference request to {ref} failed: {exc}") from exc
        return body

    def chat(self, ref: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        status = self.get_status(ref)
        if status.state is not ServingState.READY or not status.url:
            raise ConnectionError(f"{ref} is not serving")
        _, name = _split(ref)
        request = urllib.request.Request(
            f"{status.url.rstrip('/')}/openai/v1/chat/completions",
            data=json.dumps({**payload, "model": name, "stream": False}).encode(),
            headers=_headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=CHAT_TIMEOUT_SECONDS) as response:  # noqa: S310
                body: Mapping[str, Any] = json.load(response)
        except urllib.error.URLError as exc:
            raise ConnectionError(f"chat request to {ref} failed: {exc}") from exc
        return body

    def invoke(self, ref: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        status = self.get_status(ref)
        if status.state is not ServingState.READY or not status.url:
            raise ConnectionError(f"{ref} is not serving")
        request = urllib.request.Request(
            f"{status.url.rstrip('/')}/",
            data=json.dumps(dict(payload)).encode(),
            headers=_headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=CHAT_TIMEOUT_SECONDS) as response:  # noqa: S310
                body: Mapping[str, Any] = json.load(response)
        except urllib.error.URLError as exc:
            raise ConnectionError(f"call to function {ref} failed: {exc}") from exc
        return body

    def delete(self, ref: str) -> None:
        namespace, name = _split(ref)
        try:
            self._custom.delete_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
        except ApiException as exc:
            if exc.status != 404:
                raise
        # KServe deletion can be asynchronous. Preserve rollback credentials while it exists.
        if self._read(ref) is None:
            self._delete_storage_accounts(namespace, name)

    def _delete_storage_accounts(self, namespace: str, name: str) -> None:
        try:
            ns = self._core.read_namespace(namespace)
        except ApiException as exc:
            if exc.status == 404:
                return
            raise
        labels = ns.metadata.labels or {}
        project_id = labels.get(LABEL_PROJECT_ID)
        if labels.get(LABEL_MANAGED_BY) != MANAGED_BY or not project_id:
            raise Conflict("storage account cleanup requires an owned project namespace")
        expected = {
            LABEL_PROJECT_ID: project_id,
            "mlp.io/deployment": name,
            "mlp.io/storage-owner": name,
        }
        selector = ",".join(f"{key}={value}" for key, value in expected.items())
        continuation = ""
        while True:
            accounts = self._core.list_namespaced_service_account(
                namespace, label_selector=selector, limit=100, _continue=continuation
            )
            for account in accounts.items:
                meta = account.metadata
                actual = meta.labels or {}
                revision = actual.get("mlp.io/storage-revision", "")
                identity = hashlib.sha256(name.encode()).hexdigest()[:12]
                if (
                    any(actual.get(key) != value for key, value in expected.items())
                    or not revision.isdigit()
                    or meta.name != f"mlp-storage-{identity}-{revision}"
                    or not meta.uid
                    or not meta.resource_version
                ):
                    continue  # Never delete a foreign or unidentifiable account.
                try:
                    self._core.delete_namespaced_service_account(
                        meta.name,
                        namespace,
                        body=client.V1DeleteOptions(
                            preconditions=client.V1Preconditions(
                                uid=meta.uid, resource_version=meta.resource_version
                            )
                        ),
                    )
                except ApiException as exc:
                    if exc.status != 404:
                        raise  # A conflict or outage retries cleanup before marking DELETED.
            continuation = accounts.metadata._continue or ""
            if not continuation:
                return
