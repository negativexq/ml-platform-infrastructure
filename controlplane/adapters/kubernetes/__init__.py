"""Kubernetes adapter: implements ClusterProvider against a real API server."""

from controlplane.adapters.kubernetes.provisioner import KubernetesClusterProvider

__all__ = ["KubernetesClusterProvider"]
