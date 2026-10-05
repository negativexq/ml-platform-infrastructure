"""Derives the cluster-side spec of a project. Pure: same project, same spec."""

from __future__ import annotations

from controlplane.application.providers import NamespaceSpec
from controlplane.domain.entities import Project

LABEL_PROJECT_ID = "mlp.io/project-id"
LABEL_PROJECT = "mlp.io/project"
LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
MANAGED_BY = "mlp-controlplane"

# Pin enforcement to the chart minimum Kubernetes version; preview upgrades with warn/audit.
PSA_ENFORCE_VERSION = "v1.30"

# Defaults until per-project quota becomes a platform setting.
DEFAULT_QUOTA = {
    "requests.cpu": "4",
    "requests.memory": "8Gi",
    "limits.cpu": "8",
    "limits.memory": "16Gi",
    "requests.ephemeral-storage": "16Gi",
    "limits.ephemeral-storage": "32Gi",
    "pods": "50",
}
DEFAULT_LIMITS = {"cpu": "500m", "memory": "512Mi", "ephemeral-storage": "2Gi"}
# What each GPU of quota brings with it, so an LLM replica fits (see adapters/serving/kserve.py).
PER_GPU = {"requests.cpu": 4, "limits.cpu": 8, "requests.memory": 16, "limits.memory": 24}
DEFAULT_REQUESTS = {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "256Mi"}


def namespace_spec(project: Project) -> NamespaceSpec:
    return NamespaceSpec(
        project_id=project.id,
        project_name=project.name,
        namespace=project.namespace,
        labels={
            LABEL_PROJECT_ID: str(project.id),
            LABEL_PROJECT: project.name,
            LABEL_MANAGED_BY: MANAGED_BY,
            **{
                f"pod-security.kubernetes.io/{mode}": "restricted"
                for mode in ("enforce", "warn", "audit")
            },
            **{
                f"pod-security.kubernetes.io/{mode}-version": (
                    PSA_ENFORCE_VERSION if mode == "enforce" else "latest"
                )
                for mode in ("enforce", "warn", "audit")
            },
        },
        quota=_quota(project.gpu_quota),
        default_limits=DEFAULT_LIMITS,
        default_requests=DEFAULT_REQUESTS,
    )


def _quota(gpus: int) -> dict[str, str]:
    """The default quota, plus the GPUs a platform admin granted and the CPU and memory that
    come with them. No GPU quota means GPUs are explicitly refused."""
    quota = dict(DEFAULT_QUOTA)
    quota["requests.nvidia.com/gpu"] = str(gpus)
    if gpus:
        for key, per in PER_GPU.items():
            base = int(quota[key].removesuffix("Gi"))
            unit = "Gi" if key.endswith("memory") else ""
            quota[key] = f"{base + per * gpus}{unit}"
    return quota
