from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from controlplane.application.deployments import DeploymentView, RevisionView
from controlplane.domain.entities import Endpoint
from controlplane.domain.states import DeploymentStatus, EndpointStatus


class DeploymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


class RevisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="Name of a model in the same project")
    version: int = Field(ge=1, description="Platform version number of the model")


class EndpointOut(BaseModel):
    id: UUID
    name: str
    status: EndpointStatus
    url: str | None
    updated_at: datetime

    @classmethod
    def from_domain(cls, e: Endpoint) -> EndpointOut:
        return cls(id=e.id, name=e.name, status=e.status, url=e.url, updated_at=e.updated_at)


class RevisionOut(BaseModel):
    """Immutable. The artifact location is internal and not exposed."""

    revision: int
    model: str
    model_version: int
    model_version_id: UUID
    created_at: datetime

    @classmethod
    def from_domain(cls, v: RevisionView) -> RevisionOut:
        return cls(
            revision=v.revision.revision,
            model=v.model_name,
            model_version=v.model_version,
            model_version_id=v.revision.model_version_id,
            created_at=v.revision.created_at,
        )


class DeploymentOut(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    status: DeploymentStatus
    status_reason: str | None
    desired_revision: int | None
    active_revision: int | None
    endpoint: EndpointOut
    revisions: list[RevisionOut]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, view: DeploymentView) -> DeploymentOut:
        d = view.deployment
        return cls(
            id=d.id,
            project_id=d.project_id,
            name=d.name,
            status=d.status,
            status_reason=d.status_reason,
            desired_revision=d.desired_revision,
            active_revision=d.active_revision,
            endpoint=EndpointOut.from_domain(view.endpoint),
            revisions=[RevisionOut.from_domain(r) for r in view.revisions],
            created_at=d.created_at,
            updated_at=d.updated_at,
        )


class DeploymentList(BaseModel):
    items: list[DeploymentOut]


class PredictRequest(BaseModel):
    """Passed to the model server unchanged (e.g. `{"instances": [[1, 2, 3]]}`)."""

    model_config = ConfigDict(extra="allow")


PredictResponse = dict[str, Any]
