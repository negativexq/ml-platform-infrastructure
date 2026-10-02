from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from controlplane.application.rollouts import RolloutView
from controlplane.domain.entities import RolloutGate
from controlplane.domain.states import RolloutStatus


class GateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_error_rate: float = Field(0.01, ge=0, le=1, description="Fraction of 5xx responses")
    max_p95_latency_ms: float = Field(500.0, gt=0)
    min_requests: int = Field(20, ge=1, description="Requests needed before the gate can judge")
    step_seconds: int = Field(60, ge=0, description="Observation time per step")
    ready_timeout_seconds: int = Field(600, ge=1, description="Time allowed for the canary to load")

    def to_domain(self) -> RolloutGate:
        return RolloutGate(**self.model_dump())


class RolloutCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="Name of a model in the same project")
    version: int = Field(ge=1, description="Platform version number to roll out")
    steps: list[int] | None = Field(
        default=None,
        description="Increasing canary percentages ending in 100; default 10,25,50,100",
    )
    gate: GateIn | None = None


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_revision: int | None = Field(
        default=None, ge=1, description="Default: the revision before the active one"
    )


class RolloutOut(BaseModel):
    id: UUID
    deployment: str
    model: str
    model_version: int
    status: RolloutStatus
    status_reason: str | None
    from_revision: int = Field(description="Stable revision")
    to_revision: int = Field(description="Canary revision")
    steps: list[int]
    current_step: int
    canary_percent: int
    traffic: dict[int, int] = Field(description="Share of traffic per revision, right now")
    gate: GateIn
    abort_requested: bool
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None

    @classmethod
    def from_view(cls, v: RolloutView) -> RolloutOut:
        r = v.rollout
        return cls(
            id=r.id,
            deployment=v.deployment_name,
            model=v.model_name,
            model_version=v.model_version,
            status=r.status,
            status_reason=r.status_reason,
            from_revision=r.from_revision,
            to_revision=r.to_revision,
            steps=list(r.steps),
            current_step=r.current_step,
            canary_percent=r.percent,
            traffic=v.traffic,
            gate=GateIn(
                max_error_rate=r.gate.max_error_rate,
                max_p95_latency_ms=r.gate.max_p95_latency_ms,
                min_requests=r.gate.min_requests,
                step_seconds=r.gate.step_seconds,
                ready_timeout_seconds=r.gate.ready_timeout_seconds,
            ),
            abort_requested=r.abort_requested,
            created_at=r.created_at,
            updated_at=r.updated_at,
            finished_at=r.finished_at,
        )


class RolloutList(BaseModel):
    items: list[RolloutOut]
