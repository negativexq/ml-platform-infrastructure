"""Project secret management. Values go only to the configured secret provider."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from controlplane.application.identity import current_actor
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import Project
from controlplane.domain.errors import Conflict, InvalidArgument
from controlplane.domain.secrets import SecretRefs, secret_key, secret_name
from controlplane.domain.states import ProjectStatus


@dataclass(frozen=True, slots=True)
class SecretInfo:
    name: str
    keys: tuple[str, ...]
    kind: str
    version: str


class SecretProvider(Protocol):
    def list(self, project: Project) -> Sequence[SecretInfo]: ...
    def get(self, project: Project, name: str) -> SecretInfo: ...
    def put(
        self,
        project: Project,
        name: str,
        values: Mapping[str, str],
        kind: str,
        expected_version: str | None,
    ) -> SecretInfo: ...
    def delete(self, project: Project, name: str, expected_version: str) -> None: ...


def validate_refs(
    provider: SecretProvider | None, project: Project, refs: SecretRefs, env: Mapping[str, str]
) -> None:
    if set(refs.env) & set(env):
        raise InvalidArgument("secret and plaintext environment names overlap")
    if not refs.names:
        return
    if provider is None:
        raise Conflict("project secret management is not configured")
    found = {name: provider.get(project, name) for name in refs.names}
    for ref in refs.env.values():
        if ref.key not in found[ref.name].keys:
            raise InvalidArgument("referenced secret key does not exist")
    for name in refs.image_pull_secrets:
        if found[name].kind != "kubernetes.io/dockerconfigjson":
            raise InvalidArgument("image pull references require a registry secret")


class ProjectSecretService:
    def __init__(
        self, uow: UnitOfWorkFactory, provider: SecretProvider | None, clock: Clock = utc_now
    ) -> None:
        self._uow, self._provider, self._clock = uow, provider, clock

    def _project(self, ref: str) -> Project:
        from controlplane.application.jobs import resolve_project

        with self._uow() as uow:
            project = resolve_project(uow, ref)
        if project.status is not ProjectStatus.READY:
            raise Conflict("secret management requires a READY project")
        return project

    def _backend(self) -> SecretProvider:
        if self._provider is None:
            raise Conflict("project secret management is not configured")
        return self._provider

    def list(self, ref: str) -> Sequence[SecretInfo]:
        return self._backend().list(self._project(ref))

    def put(
        self,
        ref: str,
        name: str,
        values: Mapping[str, str],
        kind: str,
        expected_version: str | None = None,
    ) -> SecretInfo:
        project = self._project(ref)
        secret_name(name)
        if not values or len(values) > 64:
            raise InvalidArgument("a secret requires 1-64 keys")
        for key in values:
            secret_key(key)
        if sum(len(k.encode()) + len(v.encode()) for k, v in values.items()) > 65536:
            raise InvalidArgument("secret values exceed 64 KiB")
        if kind not in {"Opaque", "kubernetes.io/dockerconfigjson"}:
            raise InvalidArgument("unsupported secret type")
        if kind == "kubernetes.io/dockerconfigjson":
            import json

            try:
                config = json.loads(values[".dockerconfigjson"])
                if (
                    set(values) != {".dockerconfigjson"}
                    or not isinstance(config.get("auths"), dict)
                    or not config["auths"]
                ):
                    raise ValueError()
            except (ValueError, KeyError, AttributeError):
                raise InvalidArgument(
                    "registry secret requires a valid .dockerconfigjson auths object"
                ) from None
        # Rotation may change values, not remove keys/type used by existing references.
        if expected_version is not None:
            old = self._backend().get(project, name)
            if old.kind != kind or set(old.keys) - set(values):
                raise Conflict("rotation must preserve secret type and existing keys")
        self._audit(project, name, "secret.write_requested", expected_version or "new")
        info = self._backend().put(project, name, values, kind, expected_version)
        self._audit(
            project,
            name,
            "secret.created" if expected_version is None else "secret.rotated",
            info.version,
        )
        return info

    def delete(self, ref: str, name: str, expected_version: str, force: bool = False) -> None:
        project = self._project(ref)
        secret_name(name)
        self._audit(project, name, "secret.delete_requested", expected_version)
        with self._uow() as uow:
            locked = uow.projects.lock(project.id)
            if locked is None or locked.status is not ProjectStatus.READY:
                raise Conflict("secret deletion requires a READY project")
            used = (
                any(name in j.secret_refs.names for j in uow.jobs.list(project.id))
                or any(name in m.secret_refs.names for m in uow.models.list(project.id))
                or any(
                    name in r.secret_refs.names
                    for d in uow.deployments.list(project.id)
                    for r in uow.revisions.list(d.id)
                )
            )
            if used and not force:
                raise Conflict(
                    "secret is referenced by immutable workloads; "
                    "force deletion may prevent starts and rollback"
                )
            self._backend().delete(project, name, expected_version)
            uow.audit.record(
                AuditEvent(
                    occurred_at=self._clock(),
                    actor=current_actor(),
                    action="secret.deleted",
                    entity_type="project",
                    entity_id=project.id,
                    project_id=project.id,
                    payload={"secret_name": name, "version": expected_version},
                )
            )
            uow.commit()

    def _audit(self, project: Project, name: str, action: str, version: str) -> None:
        with self._uow() as uow:
            uow.audit.record(
                AuditEvent(
                    occurred_at=self._clock(),
                    actor=current_actor(),
                    action=action,
                    entity_type="project",
                    entity_id=project.id,
                    project_id=project.id,
                    payload={"secret_name": name, "version": version},
                )
            )
            uow.commit()
