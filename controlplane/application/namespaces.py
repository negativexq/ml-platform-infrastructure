"""Derives the cluster-side spec of a project. Pure: same project, same spec."""

from __future__ import annotations

from controlplane.application.providers import NamespaceSpec
from controlplane.domain.entities import Project

LABEL_PROJECT_ID = "mlp.io/project-id"
LABEL_PROJECT = "mlp.io/project"
LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
MANAGED_BY = "mlp-controlplane"

# Defaults until per-project quota becomes a platform setting.
DEFAULT_QUOTA = {
    "requests.cpu": "4",
    "requests.memory": "8Gi",
    "limits.cpu": "8",
    "limits.memory": "16Gi",
    "pods": "50",
}
DEFAULT_LIMITS = {"cpu": "500m", "memory": "512Mi"}
DEFAULT_REQUESTS = {"cpu": "100m", "memory": "128Mi"}


def namespace_spec(project: Project) -> NamespaceSpec:
    return NamespaceSpec(
        project_id=project.id,
        project_name=project.name,
        namespace=project.namespace,
        labels={
            LABEL_PROJECT_ID: str(project.id),
            LABEL_PROJECT: project.name,
            LABEL_MANAGED_BY: MANAGED_BY,
        },
        quota=DEFAULT_QUOTA,
        default_limits=DEFAULT_LIMITS,
        default_requests=DEFAULT_REQUESTS,
    )
