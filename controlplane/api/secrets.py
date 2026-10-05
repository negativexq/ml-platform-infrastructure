"""Secret values are write-only. Responses contain only references and metadata."""

from typing import Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from controlplane.api.errors import PlatformRoute
from controlplane.application.secrets import ProjectSecretService, SecretInfo
from controlplane.domain.secrets import SecretKeyRef, SecretRefs


class SecretKeyRefIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    key: str


class SecretRefsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    env: dict[str, SecretKeyRefIn] = Field(default_factory=dict)
    image_pull_secrets: list[str] = Field(default_factory=list)
    storage_secret: str | None = None

    def to_domain(self) -> SecretRefs:
        return SecretRefs(
            {k: SecretKeyRef(v.name, v.key) for k, v in self.env.items()},
            tuple(self.image_pull_secrets),
            self.storage_secret,
        )

    @classmethod
    def from_domain(cls, refs: SecretRefs) -> "SecretRefsIn":
        return cls.model_validate(refs.to_json())


class SecretWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    annotations: dict[str, str] | None = None
    values: dict[str, SecretStr]
    kind: Literal["Opaque", "kubernetes.io/dockerconfigjson"] = "Opaque"


class SecretRotate(SecretWrite):
    expected_version: str = Field(min_length=1, max_length=128)


class SecretUseOut(BaseModel):
    kind: str
    name: str
    revision: int | None = None


class SecretOut(BaseModel):
    annotations: dict[str, str] = Field(default_factory=dict)
    used_by: list[SecretUseOut] = Field(default_factory=list)
    name: str
    keys: list[str]
    kind: str
    version: str

    @classmethod
    def from_info(cls, info: SecretInfo) -> "SecretOut":
        return cls(
            name=info.name,
            keys=list(info.keys),
            kind=info.kind,
            version=info.version,
            annotations=dict(info.annotations),
        )


class SecretList(BaseModel):
    items: list[SecretOut]


def secrets_router() -> APIRouter:
    router = APIRouter(prefix="/projects/{project}", tags=["secrets"], route_class=PlatformRoute)

    def service(request: Request) -> ProjectSecretService:
        result: ProjectSecretService = request.app.state.secrets
        return result

    @router.get("/secrets", response_model=SecretList)
    def list_secrets(project: str, request: Request) -> SecretList:
        usage = service(request).usage(project)
        items = []
        for info in service(request).list(project):
            item = SecretOut.from_info(info)
            item.used_by = [
                SecretUseOut(kind=u.kind, name=u.name, revision=u.revision)
                for u in usage.get(info.name, [])
            ]
            items.append(item)
        return SecretList(items=items)

    @router.post("/secrets/{name}", response_model=SecretOut, status_code=201)
    def create_secret(project: str, name: str, body: SecretWrite, request: Request) -> SecretOut:
        return SecretOut.from_info(
            service(request).put(
                project,
                name,
                {k: v.get_secret_value() for k, v in body.values.items()},
                body.kind,
                annotations=body.annotations,
            )
        )

    @router.put("/secrets/{name}", response_model=SecretOut)
    def rotate_secret(project: str, name: str, body: SecretRotate, request: Request) -> SecretOut:
        return SecretOut.from_info(
            service(request).put(
                project,
                name,
                {k: v.get_secret_value() for k, v in body.values.items()},
                body.kind,
                body.expected_version,
                body.annotations,
            )
        )

    @router.delete("/secrets/{name}", status_code=204)
    def delete_secret(
        project: str,
        name: str,
        request: Request,
        expected_version: str = Query(min_length=1, max_length=128),
        force: bool = False,
    ) -> Response:
        service(request).delete(project, name, expected_version, force)
        return Response(status_code=204)

    @router.get("/secret-references", response_model=SecretList)
    def reference_catalog(project: str, request: Request) -> SecretList:
        # Workload operators need names/keys to bind credentials, never values.
        return SecretList(items=[SecretOut.from_info(s) for s in service(request).list(project)])

    return router
