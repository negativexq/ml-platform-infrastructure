from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from controlplane.application.models import DiscoveryResult, ModelView, VersionView
from controlplane.domain.entities import (
    Check,
    Evaluation,
    LlmServing,
    ModelVersion,
    Promotion,
    Threshold,
)
from controlplane.domain.states import EvaluationStatus, ModelKind, ModelStatus, PromotionStatus


class ThresholdIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: float | None = None
    max: float | None = None

    def to_domain(self) -> Threshold:
        return Threshold(min=self.min, max=self.max)


class LlmServingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gpus: int = Field(1, ge=1, le=8, description="GPUs per replica (tensor parallel above 1)")
    context_length: int | None = Field(
        None, ge=256, le=1_048_576, description="max tokens in a request; null: the model's"
    )


class ModelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: ModelKind = Field(ModelKind.CLASSIC, description="classic, or llm")
    llm: LlmServingIn | None = Field(None, description="how an LLM is served (kind llm only)")
    thresholds: dict[str, ThresholdIn] = Field(
        default_factory=dict, examples=[{"auc": {"min": 0.9}, "f1": {"min": 0.85}}]
    )

    def serving(self) -> LlmServing | None:
        if self.kind is not ModelKind.LLM:
            return None
        given = self.llm or LlmServingIn()
        return LlmServing(gpus=given.gpus, context_length=given.context_length)


class HubVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(
        description="hf://<org>/<model>[@<revision>]; pin a revision so the version always "
        "means the same weights",
        examples=["hf://Qwen/Qwen2.5-7B-Instruct@a09a354"],
    )
    metrics: dict[str, float] = Field(
        default_factory=dict,
        description="offline evaluation results the version is judged on",
        examples=[{"helpfulness": 0.82, "toxicity": 0.002}],
    )


class ThresholdsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thresholds: dict[str, ThresholdIn]


class ModelVersionSummary(BaseModel):
    """The registry's version number is deliberately not exposed."""

    id: UUID
    model_id: UUID
    version: int
    status: ModelStatus
    source_pipeline_run_id: UUID | None
    source_uri: str | None = Field(None, description="hub source, for versions not in the registry")
    metrics: dict[str, float] = Field(default_factory=dict, description="results it came with")
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, v: ModelVersion) -> ModelVersionSummary:
        return cls(
            id=v.id,
            model_id=v.model_id,
            version=v.version,
            status=v.status,
            source_pipeline_run_id=v.source_pipeline_run_id,
            source_uri=v.source_uri,
            metrics=dict(v.metrics),
            created_at=v.created_at,
            updated_at=v.updated_at,
        )


class ModelOut(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    registry_name: str = Field(description="Name to register versions under in the model registry")
    thresholds: dict[str, ThresholdIn]
    alias_drift: str | None
    kind: ModelKind
    llm: LlmServingIn | None
    champion: ModelVersionSummary | None
    versions: int
    created_at: datetime

    @classmethod
    def from_view(cls, view: ModelView) -> ModelOut:
        champion = view.champion
        return cls(
            id=view.model.id,
            project_id=view.model.project_id,
            name=view.model.name,
            registry_name=view.registry_name,
            thresholds={
                k: ThresholdIn(min=t.min, max=t.max) for k, t in view.model.thresholds.items()
            },
            alias_drift=view.model.alias_drift,
            kind=view.model.kind,
            llm=(
                LlmServingIn(
                    gpus=view.model.serving.gpus,
                    context_length=view.model.serving.context_length,
                )
                if view.model.serving
                else None
            ),
            champion=ModelVersionSummary.from_domain(champion) if champion else None,
            versions=len(view.versions),
            created_at=view.model.created_at,
        )


class ModelList(BaseModel):
    items: list[ModelOut]


class VersionList(BaseModel):
    items: list[ModelVersionSummary]


class CheckOut(BaseModel):
    metric: str
    value: float | None
    min: float | None
    max: float | None
    passed: bool

    @classmethod
    def from_domain(cls, c: Check) -> CheckOut:
        return cls(
            metric=c.metric,
            value=c.value,
            min=c.threshold.min,
            max=c.threshold.max,
            passed=c.passed,
        )


class EvaluationOut(BaseModel):
    id: UUID
    status: EvaluationStatus
    baseline_version_id: UUID | None
    metrics: dict[str, float]
    checks: list[CheckOut]
    created_at: datetime

    @classmethod
    def from_domain(cls, e: Evaluation) -> EvaluationOut:
        return cls(
            id=e.id,
            status=e.status,
            baseline_version_id=e.baseline_version_id,
            metrics=dict(e.metrics),
            checks=[CheckOut.from_domain(c) for c in e.checks],
            created_at=e.created_at,
        )


class PromotionOut(BaseModel):
    id: UUID
    status: PromotionStatus
    previous_champion_id: UUID | None
    created_at: datetime

    @classmethod
    def from_domain(cls, p: Promotion) -> PromotionOut:
        return cls(
            id=p.id,
            status=p.status,
            previous_champion_id=p.previous_champion_id,
            created_at=p.created_at,
        )


class ModelVersionOut(ModelVersionSummary):
    model_name: str
    evaluations: list[EvaluationOut]
    promotions: list[PromotionOut]

    @classmethod
    def from_view(cls, view: VersionView) -> ModelVersionOut:
        return cls(
            **ModelVersionSummary.from_domain(view.version).model_dump(),
            model_name=view.model.name,
            evaluations=[EvaluationOut.from_domain(e) for e in view.evaluations],
            promotions=[PromotionOut.from_domain(p) for p in view.promotions],
        )


class DiscoveryOut(BaseModel):
    created: list[ModelVersionSummary]
    already_known: int

    @classmethod
    def from_domain(cls, r: DiscoveryResult) -> DiscoveryOut:
        return cls(
            created=[ModelVersionSummary.from_domain(v) for v in r.created],
            already_known=r.existing,
        )
