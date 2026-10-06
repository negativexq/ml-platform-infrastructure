"""Topology-configured project isolation; no cluster discovery or policy application here."""

from dataclasses import dataclass
from ipaddress import ip_network
from typing import Any


@dataclass(frozen=True)
class NetworkTopology:
    system_namespace: str = "mlp-system"
    platform_namespace: str = "ml-platform"
    serving_namespaces: tuple[str, ...] = ("knative-serving", "kourier-system", "istio-system")
    observability_namespace: str = "observability"
    api_cidrs: tuple[str, ...] = ()
    external_https_cidrs: tuple[str, ...] = ()
    isolate_egress: bool = False

    def __post_init__(self) -> None:
        for cidr in (*self.api_cidrs, *self.external_https_cidrs):
            ip_network(cidr, strict=True)
        if self.isolate_egress and not self.api_cidrs:
            raise ValueError("project egress isolation requires Kubernetes API endpoint CIDRs")


def peer(namespace: str, app: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}}
    }
    if app:
        result["podSelector"] = {"matchLabels": {"app.kubernetes.io/name": app}}
    return result


def project_policies(
    namespace: str, labels: dict[str, str], topology: NetworkTopology
) -> dict[str, dict[str, Any]]:
    def policy(name: str, spec: dict[str, Any]) -> dict[str, Any]:
        return {"metadata": {"name": name, "namespace": namespace, "labels": labels}, "spec": spec}

    callers = [peer(topology.system_namespace)]
    callers[0]["podSelector"] = {
        "matchExpressions": [
            {
                "key": "app.kubernetes.io/name",
                "operator": "In",
                "values": ["mlp-controlplane-api", "mlp-controlplane-gateway", "mlp-gateway"],
            }
        ]
    }
    callers.extend(peer(ns) for ns in topology.serving_namespaces)
    policies = {
        "networkpolicy": policy(
            "mlp-baseline",
            {
                "podSelector": {},
                "policyTypes": ["Ingress"],
                "ingress": [{"from": [{"podSelector": {}}]}],
            },
        ),
        "servingnetworkpolicy": policy(
            "mlp-serving-ingress",
            {
                "podSelector": {
                    "matchExpressions": [
                        {"key": "serving.kserve.io/inferenceservice", "operator": "Exists"}
                    ]
                },
                "policyTypes": ["Ingress"],
                "ingress": [{"from": callers}],
            },
        ),
        "servingmetricsnetworkpolicy": policy(
            "mlp-serving-metrics",
            {
                "podSelector": {
                    "matchExpressions": [
                        {"key": "serving.kserve.io/inferenceservice", "operator": "Exists"}
                    ]
                },
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [peer(topology.observability_namespace, "prometheus")],
                        "ports": [{"port": 9091, "protocol": "TCP"}],
                    }
                ],
            },
        ),
    }
    if topology.isolate_egress:
        egress: list[dict[str, Any]] = [
            {"to": [{"podSelector": {}}]},
            {
                "to": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                        },
                        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                    }
                ],
                "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}],
            },
            {"to": [peer(topology.platform_namespace, "mlflow")], "ports": [{"port": 5000}]},
            {"to": [peer(topology.platform_namespace, "minio")], "ports": [{"port": 9000}]},
            *(
                [{"to": [peer(ns) for ns in topology.serving_namespaces]}]
                if topology.serving_namespaces
                else []
            ),
            {
                "to": [peer(topology.observability_namespace, "otel-collector")],
                "ports": [{"port": 4317}, {"port": 4318}],
            },
            # API Service DNAT behavior is CNI-specific; allow only configured API IPs.
            {
                "to": [{"ipBlock": {"cidr": cidr}} for cidr in topology.api_cidrs],
                "ports": [{"port": 443}, {"port": 6443}],
            },
        ]
        if topology.external_https_cidrs:
            egress.append(
                {
                    "to": [{"ipBlock": {"cidr": cidr}} for cidr in topology.external_https_cidrs],
                    "ports": [{"port": 443}],
                }
            )
        policies["egressnetworkpolicy"] = policy(
            "mlp-workload-egress",
            {
                "podSelector": {},
                "policyTypes": ["Egress"],
                "egress": egress,
            },
        )
    return policies
