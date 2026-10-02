from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from controlplane.domain.entities import Project
from controlplane.domain.states import ProjectStatus


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(description="Unique DNS-label slug; becomes the namespace name.")]
    display_name: str | None = None
    description: str = ""


class ProjectOut(BaseModel):
    id: UUID
    name: str
    display_name: str
    description: str
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, project: Project) -> ProjectOut:
        return cls(
            id=project.id,
            name=project.name,
            display_name=project.display_name,
            description=project.description,
            status=project.status,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )


class ProjectList(BaseModel):
    items: list[ProjectOut]
    limit: int
    offset: int


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorOut(BaseModel):
    error: ErrorBody
