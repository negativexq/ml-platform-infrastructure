from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from controlplane.api.errors import PlatformRoute
from controlplane.api.schemas_runs import JobList, JobOut
from controlplane.domain.data import DatasetFormat
from controlplane.domain.errors import NotFound


class ModelArtifactFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=0, strict=True)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    object_version_id: str | None = Field(default=None, min_length=1, max_length=1024)


class BatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=40)
    input_dataset_id: UUID
    input_selection_policy: Literal["PINNED", "LATEST_AT_EXECUTION", "BY_PROCESSING_DATE"] = (
        "PINNED"
    )
    model_version_id: UUID
    model_manifest: list[ModelArtifactFile] = Field(min_length=1, max_length=1000)
    model_connection_id: UUID
    output_connection_id: UUID
    output_dataset: str = Field(max_length=40)
    features: list[str] = Field(min_length=1, max_length=512)
    output_format: DatasetFormat = DatasetFormat.PARQUET
    prediction_dtype: Literal["number", "integer", "string", "boolean"] = "number"
    batch_size: int = Field(default=1000, ge=1, le=100000)
    max_rows: int = Field(default=10000000, ge=1, le=100000000)
    max_bytes: int = Field(default=1073741824, ge=1, le=2147483648)
    max_model_bytes: int = Field(default=536870912, ge=1, le=1073741824)
    timeout_seconds: int = Field(default=3600, ge=1, le=604800)
    resources: dict[str, str] = Field(default_factory=lambda: {"cpu": "1", "memory": "2Gi"})


def batch_router() -> APIRouter:
    router = APIRouter(
        route_class=PlatformRoute,
        prefix="/projects/{project}/batch-inference",
        tags=["batch inference"],
    )

    @router.post("", response_model=JobOut, status_code=201)
    def create(project: str, body: BatchCreate, request: Request, response: Response) -> JobOut:
        job, created = request.app.state.batch_inference.create(project, **body.model_dump())
        if not created:
            response.status_code = 200
        return JobOut.from_domain(job)

    @router.get("", response_model=JobList)
    def list_definitions(project: str, request: Request) -> JobList:
        return JobList(
            items=[
                JobOut.from_domain(j) for j in request.app.state.jobs.list(project) if j.batch_spec
            ]
        )

    @router.get("/{name}", response_model=JobOut)
    def get(project: str, name: str, request: Request) -> JobOut:
        job = request.app.state.jobs.get(project, name)
        if not job.batch_spec:
            raise NotFound("batch definition", name)
        return JobOut.from_domain(job)

    return router
