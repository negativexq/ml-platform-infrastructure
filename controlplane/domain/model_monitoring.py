"""Append-only model-quality measurements, separate from offline promotion evaluations."""

import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from controlplane.domain.ids import new_id


@dataclass(frozen=True, kw_only=True)
class MonitoringReport:
    project_id: UUID
    job_definition_id: UUID
    model_version_id: UUID
    model_name: str
    model_version: int
    check_name: str
    reference_dataset_id: UUID
    observed_dataset_id: UUID
    feedback_dataset_id: UUID | None
    job_run_id: UUID | None
    pipeline_run_id: UUID | None
    step: str
    status: str
    result: Mapping[str, Any]
    created_at: datetime
    id: UUID = field(default_factory=new_id)


# The worker protocol is validated without framework/runtime dependencies.
def exact(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("unexpected result fields")
    return value


def number(
    value: Any, low: float | None = None, high: float | None = None, nullable: bool = False
) -> float | None:
    if value is None and nullable:
        return None
    if (
        type(value) not in {int, float}
        or not math.isfinite(value)
        or (low is not None and value < low)
        or (high is not None and value > high)
    ):
        raise ValueError("invalid result number")
    return float(value)


def count(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 100000000:
        raise ValueError("invalid result count")
    return value


def choice(value: Any, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("invalid result choice")
    return value


@dataclass(frozen=True)
class FeatureMeasurement:
    name: str
    dtype: str
    psi: float | None
    reference_missing_rate: float | None
    observed_missing_rate: float | None
    missing_rate_change: float | None
    drifted: bool
    status: str

    @classmethod
    def parse(cls, raw: Any) -> "FeatureMeasurement":
        value = exact(
            raw,
            {
                "name",
                "dtype",
                "psi",
                "reference_missing_rate",
                "observed_missing_rate",
                "missing_rate_change",
                "drifted",
                "status",
            },
        )
        if (
            not isinstance(value["name"], str)
            or not 1 <= len(value["name"]) <= 128
            or type(value["drifted"]) is not bool
        ):
            raise ValueError("invalid feature identity")
        return cls(
            name=value["name"],
            dtype=choice(value["dtype"], {"string", "integer", "number", "boolean"}),
            psi=number(value["psi"], 0, nullable=True),
            reference_missing_rate=number(value["reference_missing_rate"], 0, 1, nullable=True),
            observed_missing_rate=number(value["observed_missing_rate"], 0, 1, nullable=True),
            missing_rate_change=number(value["missing_rate_change"], 0, 1, nullable=True),
            drifted=value["drifted"],
            status=choice(value["status"], {"STABLE", "DRIFTED", "INSUFFICIENT_DATA"}),
        )


@dataclass(frozen=True)
class PerformanceMeasurement:
    task: str
    matched_rows: int
    unmatched_predictions: int
    unmatched_truth: int
    coverage: float | None
    status: str
    metrics: Mapping[str, float | None]

    @classmethod
    def parse(cls, raw: Any) -> "PerformanceMeasurement":
        value = exact(
            raw,
            {
                "task",
                "matched_rows",
                "unmatched_predictions",
                "unmatched_truth",
                "coverage",
                "status",
                "metrics",
            },
        )
        metrics = value["metrics"]
        if (
            not isinstance(metrics, dict)
            or not 1 <= len(metrics) <= 3
            or any(not isinstance(key, str) for key in metrics)
        ):
            raise ValueError("invalid metrics")
        return cls(
            task=choice(value["task"], {"REGRESSION", "CLASSIFICATION"}),
            matched_rows=count(value["matched_rows"]),
            unmatched_predictions=count(value["unmatched_predictions"]),
            unmatched_truth=count(value["unmatched_truth"]),
            coverage=number(value["coverage"], 0, 1, nullable=True),
            status=choice(value["status"], {"MEASURED", "INSUFFICIENT_DATA"}),
            metrics={key: number(score, nullable=True) for key, score in metrics.items()},
        )


@dataclass(frozen=True)
class MonitoringResult:
    execution: str
    model_version_id: UUID
    reference_dataset_id: UUID
    observed_dataset_id: UUID
    feedback_dataset_id: UUID | None
    status: str
    reference_rows: int
    observed_rows: int
    features: tuple[FeatureMeasurement, ...]
    performance: PerformanceMeasurement | None

    @classmethod
    def from_json(cls, raw: str) -> "MonitoringResult":
        value = exact(
            json.loads(raw),
            {
                "execution",
                "model_version_id",
                "reference_dataset_id",
                "observed_dataset_id",
                "feedback_dataset_id",
                "status",
                "reference_rows",
                "observed_rows",
                "features",
                "performance",
            },
        )
        if (
            not isinstance(value["execution"], str)
            or len(value["execution"]) > 100
            or not isinstance(value["features"], list)
            or not 1 <= len(value["features"]) <= 32
        ):
            raise ValueError("invalid result identity or features")
        ids: dict[str, Any] = {}
        for key in [
            "model_version_id",
            "reference_dataset_id",
            "observed_dataset_id",
            "feedback_dataset_id",
        ]:
            if value[key] is None and key == "feedback_dataset_id":
                ids[key] = None
            elif isinstance(value[key], str):
                ids[key] = UUID(value[key])
            else:
                raise ValueError("invalid UUID")
        return cls(
            execution=value["execution"],
            model_version_id=ids["model_version_id"],
            reference_dataset_id=ids["reference_dataset_id"],
            observed_dataset_id=ids["observed_dataset_id"],
            feedback_dataset_id=ids["feedback_dataset_id"],
            status=choice(value["status"], {"STABLE", "DRIFTED", "INSUFFICIENT_DATA"}),
            reference_rows=count(value["reference_rows"]),
            observed_rows=count(value["observed_rows"]),
            features=tuple(FeatureMeasurement.parse(feature) for feature in value["features"]),
            performance=PerformanceMeasurement.parse(value["performance"])
            if value["performance"] is not None
            else None,
        )

    def as_json(self) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(json.dumps(asdict(self), default=str, allow_nan=False))
        return value
