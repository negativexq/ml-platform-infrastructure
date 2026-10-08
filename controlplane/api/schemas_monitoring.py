from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class FeatureMeasurement(ResultModel):
    name: str = Field(min_length=1, max_length=128)
    dtype: Literal["string", "integer", "number", "boolean"]
    psi: Annotated[float, Field(ge=0)] | None
    reference_missing_rate: Annotated[float, Field(ge=0, le=1)] | None
    observed_missing_rate: Annotated[float, Field(ge=0, le=1)] | None
    missing_rate_change: Annotated[float, Field(ge=0, le=1)] | None
    drifted: bool
    status: Literal["STABLE", "DRIFTED", "INSUFFICIENT_DATA"]


class PerformanceMeasurement(ResultModel):
    task: Literal["REGRESSION", "CLASSIFICATION"]
    matched_rows: Annotated[int, Field(strict=True, ge=0, le=100000000)]
    unmatched_predictions: Annotated[int, Field(strict=True, ge=0, le=100000000)]
    unmatched_truth: Annotated[int, Field(strict=True, ge=0, le=100000000)]
    coverage: Annotated[float, Field(ge=0, le=1)] | None
    status: Literal["MEASURED", "INSUFFICIENT_DATA"]
    metrics: dict[str, float | None] = Field(max_length=3)


class MonitoringResult(ResultModel):
    execution: str = Field(max_length=100)
    model_version_id: UUID
    reference_dataset_id: UUID
    observed_dataset_id: UUID
    feedback_dataset_id: UUID | None
    status: Literal["STABLE", "DRIFTED", "INSUFFICIENT_DATA"]
    reference_rows: Annotated[int, Field(strict=True, ge=0, le=100000000)]
    observed_rows: Annotated[int, Field(strict=True, ge=0, le=100000000)]
    features: list[FeatureMeasurement] = Field(min_length=1, max_length=32)
    performance: PerformanceMeasurement | None
