from dataclasses import asdict
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from controlplane.api.errors import PlatformRoute
from controlplane.domain.data import DatasetColumn, DatasetFormat


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=40)
    endpoint: str = Field(max_length=512)
    bucket: str = Field(max_length=63)
    credential_secret: str = Field(max_length=253)
    region: str = Field(default="us-east-1", max_length=64)
    prefix: str = Field(default="", max_length=1024)


class ConnectionOut(ConnectionCreate):
    id: UUID
    project_id: UUID
    created_at: datetime


class ConnectionList(BaseModel):
    items: list[ConnectionOut]
    limit: int
    offset: int


class ColumnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    dtype: str
    nullable: bool = False


class DatasetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=40)
    connection_id: UUID
    uri: str = Field(max_length=4096)
    format: DatasetFormat
    columns: list[ColumnIn] = Field(min_length=1, max_length=512)
    checksum_sha256: str | None = Field(default=None, max_length=64)
    object_version_id: str | None = Field(default=None, max_length=1024)
    producer_run_id: UUID | None = None
    producer_pipeline_run_id: UUID | None = None
    row_count: int | None = Field(default=None, ge=0, le=9223372036854775807)
    expected_latest_version: int = Field(default=0, ge=0)


class DatasetOut(BaseModel):
    id: UUID
    project_id: UUID
    connection_id: UUID
    name: str
    version: int
    uri: str
    format: DatasetFormat
    columns: list[ColumnIn]
    checksum_sha256: str | None
    object_version_id: str | None
    producer_run_id: UUID | None
    producer_pipeline_run_id: UUID | None
    row_count: int | None
    created_at: datetime


class DatasetList(BaseModel):
    items: list[DatasetOut]
    limit: int
    offset: int


def data_catalog_router() -> APIRouter:
    router = APIRouter(
        route_class=PlatformRoute, prefix="/projects/{project}", tags=["data catalog"]
    )

    @router.post(
        "/data-connections", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED
    )
    def create_connection(
        project: str, body: ConnectionCreate, request: Request, response: Response
    ) -> ConnectionOut:
        entity, created = request.app.state.data_catalog.create_connection(
            project, **body.model_dump()
        )
        if not created:
            response.status_code = 200
        return ConnectionOut(**asdict(entity))

    @router.get("/data-connections", response_model=ConnectionList)
    def connections(
        project: str,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ConnectionList:
        return ConnectionList(
            items=[
                ConnectionOut(**asdict(e))
                for e in request.app.state.data_catalog.connections(project, limit, offset)
            ],
            limit=limit,
            offset=offset,
        )

    @router.post("/datasets", response_model=DatasetOut, status_code=status.HTTP_201_CREATED)
    def publish_dataset(
        project: str, body: DatasetCreate, request: Request, response: Response
    ) -> DatasetOut:
        spec = body.model_dump()
        spec["columns"] = tuple(DatasetColumn(**column) for column in spec["columns"])
        entity, created = request.app.state.data_catalog.publish_dataset(project, **spec)
        if not created:
            response.status_code = 200
        return DatasetOut(**asdict(entity))

    @router.get("/datasets", response_model=DatasetList)
    def datasets(
        project: str,
        request: Request,
        name: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> DatasetList:
        return DatasetList(
            items=[
                DatasetOut(**asdict(e))
                for e in request.app.state.data_catalog.datasets(project, name, limit, offset)
            ],
            limit=limit,
            offset=offset,
        )

    @router.get("/datasets/{name}", response_model=DatasetOut)
    def dataset(
        project: str,
        name: str,
        request: Request,
        version: Annotated[int | None, Query(ge=1)] = None,
    ) -> DatasetOut:
        return DatasetOut(**asdict(request.app.state.data_catalog.dataset(project, name, version)))

    return router
