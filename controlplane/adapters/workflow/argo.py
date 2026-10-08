"""WorkflowProvider backed by Argo Workflows (the `Workflow` custom resource).

Not exercised against a real Argo yet: see docs/local-verification.md.
"""

from __future__ import annotations

import json
from typing import Any

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from controlplane.adapters.kubernetes import load_api_client
from controlplane.adapters.kubernetes.security import (
    TRAINING_ACCOUNT,
    container_security,
    pod_security,
)
from controlplane.application.jobs import require_training_digest
from controlplane.application.providers import (
    ExternalState,
    LogTarget,
    StepSpec,
    WorkflowSpec,
    WorkflowStatus,
)
from controlplane.domain.errors import NotFound

GROUP, VERSION, PLURAL = "argoproj.io", "v1alpha1", "workflows"

SERVICE_ACCOUNT = TRAINING_ACCOUNT
DEFAULT_DEADLINE_SECONDS = 3600
# A pod stuck on one of these will never start; Argo itself would wait for the deadline.
_UNSTARTABLE = ("ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull")

_PHASES = {
    "": ExternalState.PENDING,
    "Pending": ExternalState.PENDING,
    "Running": ExternalState.RUNNING,
    "Succeeded": ExternalState.SUCCEEDED,
    "Failed": ExternalState.FAILED,
    "Error": ExternalState.FAILED,
}


# Node phases add "Omitted"/"Skipped": a DAG task whose dependency failed never runs.
_NODE_PHASES = {
    **_PHASES,
    "Omitted": ExternalState.SKIPPED,
    "Skipped": ExternalState.SKIPPED,
}


def _step_key(node: dict[str, Any]) -> str | None:
    """Pod nodes are named after their template; omitted tasks have no pod, only a
    display name. Both equal our step name because we name templates after steps."""
    if node.get("type") == "Pod":
        return node.get("templateName")
    if node.get("type") == "Skipped":
        return node.get("displayName")
    return None


def _ref(namespace: str, name: str) -> str:
    return f"{namespace}/{name}"


def _split(ref: str) -> tuple[str, str]:
    namespace, _, name = ref.partition("/")
    return namespace, name


def _container(step: StepSpec) -> dict[str, Any]:
    container: dict[str, Any] = {
        "image": step.image,
        "securityContext": container_security(),
        "env": [{"name": k, "value": v} for k, v in step.env.items()]
        + [
            {"name": k, "valueFrom": {"secretKeyRef": {"name": r.name, "key": r.key}}}
            for k, r in sorted(step.secret_refs.env.items())
        ],
    }
    if step.command:
        container["command"] = list(step.command)
    if step.resources:
        container["resources"] = {"requests": dict(step.resources), "limits": dict(step.resources)}
    template = {"name": step.name, "container": container}
    if step.result_path:
        template["outputs"] = {
            "parameters": [{"name": "mlp-result", "valueFrom": {"path": step.result_path}}]
        }
    return template


def build_workflow(spec: WorkflowSpec) -> dict[str, Any]:
    """Translate a WorkflowSpec to an Argo Workflow manifest. One step runs as a
    plain container template; several become a DAG."""
    templates = [_container(step) for step in spec.steps]
    if len(spec.steps) == 1:
        entrypoint = spec.steps[0].name
    else:
        entrypoint = "dag"
        templates.append(
            {
                "name": "dag",
                "dag": {
                    "tasks": [
                        {
                            "name": step.name,
                            "template": step.name,
                            "dependencies": list(step.depends_on),
                        }
                        for step in spec.steps
                    ]
                },
            }
        )
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "Workflow",
        "metadata": {
            "name": spec.name,
            "namespace": spec.namespace,
            "labels": dict(spec.labels),
        },
        "spec": {
            "entrypoint": entrypoint,
            "serviceAccountName": SERVICE_ACCOUNT,
            "securityContext": {
                **pod_security(),
                # Shared-volume supplemental group; preserve the image's primary UID/GID.
                "fsGroup": 1000,
            },
            "podSpecPatch": json.dumps(
                {
                    "initContainers": [
                        {
                            "name": "init",
                            "securityContext": {
                                **container_security(),
                                "runAsUser": 1000,
                                "runAsGroup": 1000,
                            },
                        }
                    ],
                    "containers": [
                        {
                            "name": "wait",
                            "securityContext": {
                                **container_security(),
                                "runAsUser": 1000,
                                "runAsGroup": 1000,
                            },
                        }
                    ],
                }
            ),
            "activeDeadlineSeconds": spec.timeout_seconds,
            **(
                {"imagePullSecrets": [{"name": n} for n in spec.image_pull_secrets]}
                if spec.image_pull_secrets
                else {}
            ),
            "templates": templates,
        },
    }


class ArgoWorkflowProvider:
    def __init__(self, api_client: client.ApiClient, *, require_image_digest: bool = True) -> None:
        self._require_image_digest = require_image_digest
        self._custom = client.CustomObjectsApi(api_client)
        self._core = client.CoreV1Api(api_client)

    @classmethod
    def from_kubeconfig(
        cls, path: str | None = None, *, require_image_digest: bool = True
    ) -> ArgoWorkflowProvider:
        return cls(load_api_client(path), require_image_digest=require_image_digest)

    def submit(self, spec: WorkflowSpec, idempotency_key: str) -> str:
        if self._require_image_digest:
            for step in spec.steps:
                require_training_digest(step.image)
        # The workflow name is derived from the run, so the name *is* the idempotency
        # key: a second submit hits 409 and returns the workflow that already exists.
        try:
            self._custom.create_namespaced_custom_object(
                GROUP, VERSION, spec.namespace, PLURAL, build_workflow(spec)
            )
        except ApiException as exc:
            if exc.status != 409:
                raise
        return _ref(spec.namespace, spec.name)

    def _get(self, ref: str) -> dict[str, Any]:
        namespace, name = _split(ref)
        try:
            workflow: dict[str, Any] = self._custom.get_namespaced_custom_object(
                GROUP, VERSION, namespace, PLURAL, name
            )
        except ApiException as exc:
            if exc.status == 404:
                raise NotFound("workflow", ref) from None
            raise
        return workflow

    def delete(self, ref: str) -> None:
        namespace, name = _split(ref)
        try:
            self._custom.delete_namespaced_custom_object(
                GROUP,
                VERSION,
                namespace,
                PLURAL,
                name,
                body=client.V1DeleteOptions(propagation_policy="Foreground"),
            )
        except ApiException as exc:
            if exc.status != 404:
                raise

    def get_status(self, ref: str) -> WorkflowStatus:
        workflow = self._get(ref)
        status = workflow.get("status") or {}
        nodes: dict[str, Any] = status.get("nodes") or {}
        state = _PHASES.get(status.get("phase", ""), ExternalState.PENDING)
        reason: str | None = status.get("message") or None
        steps = {key: _node_state(n) for n in nodes.values() if (key := _step_key(n)) is not None}
        exit_codes = {
            n["templateName"]: int(n["outputs"]["exitCode"])
            for n in nodes.values()
            if n.get("type") == "Pod" and (n.get("outputs") or {}).get("exitCode") is not None
        }

        stuck = next(
            (
                n.get("message", "")
                for n in nodes.values()
                if n.get("type") == "Pod"
                and n.get("phase") == "Pending"
                and any(marker in (n.get("message") or "") for marker in _UNSTARTABLE)
            ),
            None,
        )
        if state is ExternalState.RUNNING and stuck:
            self.cancel(ref)
            return WorkflowStatus(ExternalState.FAILED, steps, stuck, exit_codes)
        if state is ExternalState.FAILED and workflow.get("spec", {}).get("shutdown"):
            state = ExternalState.CANCELLED
        results = {
            key: p["value"]
            for n in nodes.values()
            if (key := _step_key(n)) is not None
            for p in (n.get("outputs") or {}).get("parameters", [])
            if p.get("name") == "mlp-result" and isinstance(p.get("value"), str)
        }
        return WorkflowStatus(state, steps, reason, exit_codes, results)

    def cancel(self, ref: str) -> None:
        namespace, name = _split(ref)
        self._custom.patch_namespaced_custom_object(
            GROUP, VERSION, namespace, PLURAL, name, {"spec": {"shutdown": "Terminate"}}
        )

    def get_logs(self, ref: str, step: str) -> str:
        namespace, _ = _split(ref)
        nodes: dict[str, Any] = (self._get(ref).get("status") or {}).get("nodes") or {}
        pod = next(
            (
                n["id"]
                for n in nodes.values()
                if n.get("type") == "Pod" and n.get("templateName") == step
            ),
            None,
        )
        if pod is None:
            return ""
        try:
            # Decode the HTTP body ourselves. Some client versions deserialize text bytes
            # with str(bytes), exposing b'...' and literal escaped newlines to the UI.
            response = self._core.read_namespaced_pod_log(
                pod, namespace, container="main", _preload_content=False
            )
        except ApiException as exc:
            if exc.status in (400, 404):  # pod not started yet, or already garbage-collected
                return ""
            raise
        if isinstance(response, str):
            return response
        if isinstance(response, bytes):
            return response.decode("utf-8", errors="replace")
        try:
            data = response.data
            if not isinstance(data, bytes):
                raise TypeError("Kubernetes log response must contain bytes")
            return data.decode("utf-8", errors="replace")
        finally:
            response.close()
            response.release_conn()

    def get_log_target(self, ref: str, step: str) -> LogTarget | None:
        namespace, name = _split(ref)
        workflow = self._get(ref)
        nodes = (workflow.get("status") or {}).get("nodes") or {}
        pod_name = next(
            (
                n["id"]
                for n in nodes.values()
                if n.get("type") == "Pod" and n.get("templateName") == step
            ),
            None,
        )
        if pod_name is None:
            return None
        try:
            pod = self._core.read_namespaced_pod(pod_name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        uid = workflow["metadata"]["uid"]
        labels = pod.metadata.labels or {}
        owners = pod.metadata.owner_references or []
        if (
            not pod.metadata.uid
            or labels.get("workflows.argoproj.io/workflow") != name
            or not any(o.kind == "Workflow" and o.uid == uid for o in owners)
            or not any(c.name == "main" for c in pod.spec.containers)
        ):
            raise NotFound("workflow pod", pod_name)
        return LogTarget(namespace, pod_name, pod.metadata.uid, name, uid)


def _node_state(node: dict[str, Any]) -> ExternalState:
    return _NODE_PHASES.get(node.get("phase", ""), ExternalState.PENDING)
