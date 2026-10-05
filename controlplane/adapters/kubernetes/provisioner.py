"""Namespace provisioning on a real Kubernetes API server.

Idempotency rule: look first, write only what differs. A converged project
therefore causes no writes at all, which keeps resourceVersions stable and the
audit trail quiet. Comparison is "desired is a subset of actual", so fields the
API server defaults do not look like drift.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from controlplane.adapters.kubernetes.client import BoundedApiClient
from controlplane.adapters.kubernetes.networking import NetworkTopology, project_policies
from controlplane.adapters.kubernetes.security import SERVING_ACCOUNT, TRAINING_ACCOUNT
from controlplane.application.namespaces import LABEL_MANAGED_BY, LABEL_PROJECT_ID, MANAGED_BY
from controlplane.application.providers import NamespaceSpec, NamespaceState, Observation
from controlplane.domain.errors import Conflict

SERVICE_ACCOUNT = TRAINING_ACCOUNT
QUOTA = "mlp-quota"
LIMIT_RANGE = "mlp-limits"
NETWORK_POLICY = "mlp-baseline"
WORKFLOW_ROLE = "mlp-workflow-executor"


def _covers(actual: Any, desired: Any) -> bool:
    """True when `actual` contains everything in `desired` (recursively)."""
    if isinstance(desired, dict):
        return isinstance(actual, dict) and all(
            k in actual and _covers(actual[k], v) for k, v in desired.items()
        )
    if isinstance(desired, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(desired)
            and all(_covers(a, d) for a, d in zip(actual, desired, strict=True))
        )
    return bool(actual == desired)


def load_api_client(path: str | None = None) -> client.ApiClient:
    """`path=None` uses in-cluster config, falling back to ~/.kube/config."""
    if path:
        config.load_kube_config(config_file=path)
    else:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
    return BoundedApiClient()


class KubernetesClusterProvider:
    def __init__(
        self,
        api_client: client.ApiClient,
        topology: NetworkTopology | None = None,
        *,
        api_service_account: str = "",
        api_namespace: str = "mlp-system",
        secret_cluster_role: str = "",
        workload_cluster_role: str = "",
    ) -> None:
        self._topology = topology or NetworkTopology()
        self._api_service_account = api_service_account
        self._api_namespace = api_namespace
        self._secret_cluster_role = secret_cluster_role
        self._workload_cluster_role = workload_cluster_role
        if workload_cluster_role and not api_service_account:
            raise ValueError("API workload RBAC requires a service account")
        if bool(api_service_account) != bool(secret_cluster_role):
            raise ValueError("API secret RBAC requires both service account and ClusterRole")
        self._api = api_client
        self._core = client.CoreV1Api(api_client)
        self._net = client.NetworkingV1Api(api_client)
        self._rbac = client.RbacAuthorizationV1Api(api_client)

    @classmethod
    def from_kubeconfig(cls, path: str | None = None) -> KubernetesClusterProvider:
        return cls(load_api_client(path))

    # -- desired state (wire format: what the API server stores) -----------

    def _desired(self, spec: NamespaceSpec) -> dict[str, dict[str, Any]]:
        meta = {"namespace": spec.namespace, "labels": dict(spec.labels)}
        bindings: dict[str, dict[str, Any]] = {}
        if self._api_service_account:
            bindings["secretrolebinding"] = {
                "metadata": {"name": "mlp-api-secrets", **meta},
                "subjects": [
                    {
                        "kind": "ServiceAccount",
                        "name": self._api_service_account,
                        "namespace": self._api_namespace,
                    }
                ],
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": self._secret_cluster_role,
                },
            }
            if self._workload_cluster_role:
                bindings["workloadrolebinding"] = {
                    **bindings["secretrolebinding"],
                    "metadata": {"name": "mlp-api-workload-reader", **meta},
                    "roleRef": {
                        "apiGroup": "rbac.authorization.k8s.io",
                        "kind": "ClusterRole",
                        "name": self._workload_cluster_role,
                    },
                }
        return {
            **bindings,
            "serviceaccount": {
                "metadata": {"name": TRAINING_ACCOUNT, **meta},
                "automountServiceAccountToken": True,
            },
            "servingserviceaccount": {
                "metadata": {"name": SERVING_ACCOUNT, **meta},
                "automountServiceAccountToken": False,
            },
            # Remove token mounting from the old shared account during upgrades.
            "legacyserviceaccount": {
                "metadata": {"name": "mlp-workload", **meta},
                "automountServiceAccountToken": False,
            },
            "resourcequota": {
                "metadata": {"name": QUOTA, **meta},
                "spec": {"hard": dict(spec.quota)},
            },
            "limitrange": {
                "metadata": {"name": LIMIT_RANGE, **meta},
                "spec": {
                    "limits": [
                        {
                            "type": "Container",
                            "default": dict(spec.default_limits),
                            "defaultRequest": dict(spec.default_requests),
                        }
                    ]
                },
            },
            # Argo's executor reports each step's result as a WorkflowTaskResult from inside
            # the workload pod; without this the workflow can never record that a step ended.
            "role": {
                "metadata": {"name": WORKFLOW_ROLE, **meta},
                "rules": [
                    {
                        "apiGroups": ["argoproj.io"],
                        "resources": ["workflowtaskresults"],
                        "verbs": ["create", "patch"],
                    }
                ],
            },
            "rolebinding": {
                "metadata": {"name": WORKFLOW_ROLE, **meta},
                "subjects": [
                    {
                        "kind": "ServiceAccount",
                        "name": SERVICE_ACCOUNT,
                        "namespace": spec.namespace,
                    }
                ],
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Role",
                    "name": WORKFLOW_ROLE,
                },
            },
            **project_policies(spec.namespace, dict(spec.labels), self._topology),
        }

    # -- reads ------------------------------------------------------------

    def _read(self, kind: str, ns: str, name: str) -> Any | None:
        kind = "serviceaccount" if kind.endswith("serviceaccount") else kind
        kind = "networkpolicy" if kind.endswith("networkpolicy") else kind
        kind = "rolebinding" if kind.endswith("rolebinding") else kind
        readers = {
            "serviceaccount": self._core.read_namespaced_service_account,
            "resourcequota": self._core.read_namespaced_resource_quota,
            "limitrange": self._core.read_namespaced_limit_range,
            "networkpolicy": self._net.read_namespaced_network_policy,
            "role": self._rbac.read_namespaced_role,
            "rolebinding": self._rbac.read_namespaced_role_binding,
        }
        try:
            return readers[kind](name, ns)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise

    def _namespace(self, name: str) -> Any | None:
        try:
            return self._core.read_namespace(name)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise

    @staticmethod
    def _owned(namespace: Any, project_id: UUID) -> bool:
        labels = namespace.metadata.labels or {}
        return (
            labels.get(LABEL_PROJECT_ID) == str(project_id)
            and labels.get(LABEL_MANAGED_BY) == MANAGED_BY
        )

    @staticmethod
    def _terminating(namespace: Any) -> bool:
        return bool(namespace.status is not None and namespace.status.phase == "Terminating")

    def _drifted(self, spec: NamespaceSpec) -> list[str]:
        drifted = []
        for kind, desired in self._desired(spec).items():
            current = self._read(kind, spec.namespace, desired["metadata"]["name"])
            if current is None:
                drifted.append(kind)
                continue
            actual: dict[str, Any] = self._api.sanitize_for_serialization(current)
            if not _covers(actual, desired):
                drifted.append(kind)
        if not self._topology.isolate_egress:
            current = self._read("networkpolicy", spec.namespace, "mlp-workload-egress")
            if current is not None:
                labels = current.metadata.labels or {}
                if labels.get(LABEL_MANAGED_BY) == MANAGED_BY and labels.get(
                    LABEL_PROJECT_ID
                ) == str(spec.project_id):
                    drifted.append("obsolete-egress")
        return drifted

    # -- ClusterProvider --------------------------------------------------

    def observe(self, spec: NamespaceSpec) -> Observation:
        ns = self._namespace(spec.namespace)
        if ns is None:
            return Observation(NamespaceState.ABSENT)
        if self._terminating(ns):
            return Observation(NamespaceState.TERMINATING)
        if not self._owned(ns, spec.project_id):
            return Observation(NamespaceState.PRESENT, ("namespace",))
        drifted = [] if _covers(ns.metadata.labels or {}, dict(spec.labels)) else ["namespace"]
        return Observation(NamespaceState.PRESENT, tuple(drifted + self._drifted(spec)))

    def apply(self, spec: NamespaceSpec) -> tuple[str, ...]:
        changed: list[str] = []
        ns = self._namespace(spec.namespace)
        if ns is None:
            self._core.create_namespace(
                client.V1Namespace(
                    metadata=client.V1ObjectMeta(name=spec.namespace, labels=dict(spec.labels))
                )
            )
            changed.append("namespace")
        elif self._terminating(ns):
            raise Conflict(f"namespace {spec.namespace!r} is still terminating")
        elif not self._owned(ns, spec.project_id):
            # Never adopt: a namespace we did not create may hold someone else's workloads.
            raise Conflict(f"namespace {spec.namespace!r} exists and is not owned by this project")
        elif not _covers(ns.metadata.labels or {}, dict(spec.labels)):
            self._core.patch_namespace(spec.namespace, {"metadata": {"labels": dict(spec.labels)}})
            changed.append("namespace")

        desired = self._desired(spec)
        for kind in self._drifted(spec):
            if kind == "obsolete-egress":
                try:
                    self._net.delete_namespaced_network_policy(
                        "mlp-workload-egress", spec.namespace
                    )
                except ApiException as exc:
                    if exc.status != 404:
                        raise
            else:
                self._upsert(kind, spec.namespace, desired[kind])
            changed.append(kind)
        return tuple(changed)

    def delete(self, namespace: str, project_id: UUID) -> None:
        ns = self._namespace(namespace)
        if ns is None:
            return
        if not self._owned(ns, project_id):
            raise Conflict(f"namespace {namespace!r} is not owned by project {project_id}")
        if not self._terminating(ns):
            self._core.delete_namespace(namespace)

    # -- writes -----------------------------------------------------------

    def _upsert(self, kind: str, ns: str, body: dict[str, Any]) -> None:
        api_binding = kind in {"secretrolebinding", "workloadrolebinding"}
        kind = "serviceaccount" if kind.endswith("serviceaccount") else kind
        kind = "networkpolicy" if kind.endswith("networkpolicy") else kind
        kind = "rolebinding" if kind.endswith("rolebinding") else kind
        create, replace = {
            "serviceaccount": (
                self._core.create_namespaced_service_account,
                self._core.replace_namespaced_service_account,
            ),
            "resourcequota": (
                self._core.create_namespaced_resource_quota,
                self._core.replace_namespaced_resource_quota,
            ),
            "limitrange": (
                self._core.create_namespaced_limit_range,
                self._core.replace_namespaced_limit_range,
            ),
            "networkpolicy": (
                self._net.create_namespaced_network_policy,
                self._net.replace_namespaced_network_policy,
            ),
            "role": (
                self._rbac.create_namespaced_role,
                self._rbac.replace_namespaced_role,
            ),
            "rolebinding": (
                self._rbac.create_namespaced_role_binding,
                self._rbac.replace_namespaced_role_binding,
            ),
        }[kind]
        name = body["metadata"]["name"]
        current = self._read(kind, ns, name)
        if current is None:
            create(ns, body)
        else:
            if api_binding:
                labels = current.metadata.labels or {}
                if any(labels.get(k) != v for k, v in body["metadata"]["labels"].items()):
                    raise Conflict("API project binding exists and is not owned by this project")
                if self._api.sanitize_for_serialization(current.role_ref) != body["roleRef"]:
                    raise Conflict("API project binding has an unexpected immutable role reference")
                body["metadata"]["resourceVersion"] = current.metadata.resource_version
            replace(name, ns, body)
