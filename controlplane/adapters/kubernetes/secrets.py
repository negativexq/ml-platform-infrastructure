"""Project-owned Kubernetes Secrets, with metadata-only public reads and CAS rotation."""

from collections.abc import Mapping, Sequence

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from controlplane.application.namespaces import LABEL_MANAGED_BY, LABEL_PROJECT_ID, MANAGED_BY
from controlplane.application.secrets import SecretInfo
from controlplane.domain.entities import Project
from controlplane.domain.errors import AlreadyExists, Conflict, NotFound


class KubernetesSecretProvider:
    def __init__(self, api_client: client.ApiClient) -> None:
        self._core = client.CoreV1Api(api_client)

    @staticmethod
    def _info(secret: client.V1Secret) -> SecretInfo:
        return SecretInfo(
            secret.metadata.name,
            tuple(sorted(secret.data or {})),
            secret.type or "Opaque",
            secret.metadata.resource_version,
        )

    @staticmethod
    def _owned(secret: client.V1Secret, project: Project) -> bool:
        labels = secret.metadata.labels or {}
        return labels.get(LABEL_MANAGED_BY) == MANAGED_BY and labels.get(LABEL_PROJECT_ID) == str(
            project.id
        )

    def _namespace(self, project: Project) -> None:
        try:
            namespace = self._core.read_namespace(project.namespace)
        except ApiException:
            raise Conflict("project namespace unavailable") from None
        labels = namespace.metadata.labels or {}
        if labels.get(LABEL_MANAGED_BY) != MANAGED_BY or labels.get(LABEL_PROJECT_ID) != str(
            project.id
        ):
            raise Conflict("project does not own the namespace")

    def list(self, project: Project) -> Sequence[SecretInfo]:
        self._namespace(project)
        try:
            rows = self._core.list_namespaced_secret(
                project.namespace,
                label_selector=f"{LABEL_MANAGED_BY}={MANAGED_BY},{LABEL_PROJECT_ID}={project.id}",
            ).items
        except ApiException:
            raise Conflict("secret backend unavailable") from None
        return sorted((self._info(s) for s in rows), key=lambda s: s.name)

    def get(self, project: Project, name: str) -> SecretInfo:
        self._namespace(project)
        try:
            secret = self._core.read_namespaced_secret(name, project.namespace)
        except ApiException as exc:
            if exc.status == 404:
                raise NotFound("project secret", name) from None
            raise Conflict("secret backend unavailable") from None
        if not self._owned(secret, project):
            raise NotFound("project secret", name)
        return self._info(secret)

    def put(
        self,
        project: Project,
        name: str,
        values: Mapping[str, str],
        kind: str,
        expected_version: str | None,
    ) -> SecretInfo:
        self._namespace(project)
        if expected_version is not None:
            existing = self.get(project, name)
            if existing.version != expected_version:
                raise Conflict("secret changed; refresh before rotating")
        secret = client.V1Secret(
            type=kind,
            string_data=dict(values),
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=project.namespace,
                resource_version=expected_version,
                labels={LABEL_MANAGED_BY: MANAGED_BY, LABEL_PROJECT_ID: str(project.id)},
            ),
        )
        try:
            saved = (
                self._core.create_namespaced_secret(project.namespace, secret)
                if expected_version is None
                else self._core.replace_namespaced_secret(name, project.namespace, secret)
            )
        except ApiException as exc:
            if exc.status == 409:
                if expected_version is None:
                    raise AlreadyExists("project secret", name) from None
                raise Conflict("secret changed; refresh before rotating") from None
            raise Conflict("secret write failed") from None
        return self._info(saved)

    def delete(self, project: Project, name: str, expected_version: str) -> None:
        existing = self.get(project, name)
        if existing.version != expected_version:
            raise Conflict("secret changed; refresh before deleting")
        try:
            self._core.delete_namespaced_secret(
                name,
                project.namespace,
                body=client.V1DeleteOptions(
                    preconditions=client.V1Preconditions(resource_version=expected_version)
                ),
            )
        except ApiException as exc:
            if exc.status == 409:
                raise Conflict("secret changed; refresh before deleting") from None
            if exc.status != 404:
                raise Conflict("secret deletion failed") from None
