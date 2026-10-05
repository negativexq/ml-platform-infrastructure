from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from controlplane.application.deployments import DeploymentView, RevisionView
from controlplane.domain.entities import Endpoint
from controlplane.domain.states import (
    DeploymentStatus,
    EndpointKind,
    EndpointProtocol,
    EndpointStatus,
    Exposure,
    ServingRuntime,
)


class DeploymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


class RevisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="Name of a model in the same project")
    version: int = Field(ge=1, description="Platform version number of the model")


class EndpointLimitsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    units_per_minute: int = Field(600, ge=1, le=1_000_000, description="all callers together")
    max_body_kb: int = Field(256, ge=1, le=10_240)
    timeout_seconds: int = Field(30, ge=1, le=600)


class EndpointOut(BaseModel):
    id: UUID
    name: str
    status: EndpointStatus
    url: str | None = Field(description="inside the cluster; the public address is the gateway's")
    kind: EndpointKind
    protocol: EndpointProtocol
    exposure: Exposure
    limits: EndpointLimitsBody
    updated_at: datetime

    @classmethod
    def from_domain(cls, e: Endpoint) -> EndpointOut:
        return cls(
            id=e.id,
            name=e.name,
            status=e.status,
            url=e.url,
            kind=e.kind,
            protocol=e.protocol,
            exposure=e.exposure,
            limits=EndpointLimitsBody(
                units_per_minute=e.limits.units_per_minute,
                max_body_kb=e.limits.max_body_kb,
                timeout_seconds=e.limits.timeout_seconds,
            ),
            updated_at=e.updated_at,
        )


class RevisionOut(BaseModel):
    """Immutable. The artifact location is internal and not exposed."""

    revision: int
    model: str
    model_version: int
    model_version_id: UUID
    runtime: ServingRuntime
    gpus: int
    min_scale: int | None = Field(None, description="functions: fewest replicas (0: to zero)")
    max_scale: int | None = None
    created_at: datetime

    @classmethod
    def from_domain(cls, v: RevisionView) -> RevisionOut:
        return cls(
            revision=v.revision.revision,
            model=v.model_name,
            model_version=v.model_version,
            model_version_id=v.revision.model_version_id,
            runtime=v.revision.runtime,
            gpus=v.revision.gpus,
            min_scale=v.revision.function.min_scale
            if v.revision.function
            else v.revision.min_scale,
            max_scale=v.revision.function.max_scale
            if v.revision.function
            else v.revision.max_scale,
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


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(system|user|assistant)$")
    content: str = Field(max_length=100_000)


class ChatRequest(BaseModel):
    """The playground's request: an OpenAI-style chat, answered in one piece (not streamed)."""

    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage] = Field(min_length=1, max_length=200)
    max_tokens: int | None = Field(None, ge=1, le=32_768)
    temperature: float | None = Field(None, ge=0, le=2)
