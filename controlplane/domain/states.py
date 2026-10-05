"""Explicit state machines for every stateful entity.

A transition not listed here is illegal. Each machine must name every member
of its enum, so adding a state without deciding its edges fails at import.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from enum import StrEnum

from controlplane.domain.errors import IllegalTransition


class StateMachine[S: StrEnum]:
    def __init__(self, name: str, states: type[S], transitions: Mapping[S, Collection[S]]) -> None:
        missing = set(states) - set(transitions)
        if missing:
            raise ValueError(f"{name}: no transitions declared for {sorted(missing)}")
        self.name = name
        self.initial = next(iter(states))
        self._states = states
        self._transitions = {src: frozenset(dst) for src, dst in transitions.items()}

    @property
    def states(self) -> tuple[S, ...]:
        return tuple(self._states)

    def allowed_from(self, source: S) -> frozenset[S]:
        return self._transitions[source]

    def is_terminal(self, state: S) -> bool:
        return not self._transitions[state]

    def can_transition(self, source: S, target: S) -> bool:
        return target in self._transitions[source]

    def ensure(self, source: S, target: S) -> None:
        if not self.can_transition(source, target):
            raise IllegalTransition(self.name, source.value, target.value)


class ProjectStatus(StrEnum):
    PENDING = "PENDING"
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    DRIFTED = "DRIFTED"
    FAILED = "FAILED"
    DELETING = "DELETING"
    DELETED = "DELETED"


PROJECT = StateMachine(
    "Project",
    ProjectStatus,
    {
        ProjectStatus.PENDING: {ProjectStatus.PROVISIONING, ProjectStatus.DELETING},
        ProjectStatus.PROVISIONING: {
            ProjectStatus.READY,
            ProjectStatus.FAILED,
            ProjectStatus.DELETING,
        },
        ProjectStatus.READY: {ProjectStatus.DRIFTED, ProjectStatus.DELETING},
        ProjectStatus.DRIFTED: {ProjectStatus.PROVISIONING, ProjectStatus.DELETING},
        ProjectStatus.FAILED: {ProjectStatus.PROVISIONING, ProjectStatus.DELETING},
        ProjectStatus.DELETING: {ProjectStatus.DELETED, ProjectStatus.FAILED},
        ProjectStatus.DELETED: set(),
    },
)


class RunStatus(StrEnum):
    """PipelineRun lifecycle."""

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_RUN_EDGES: dict[RunStatus, set[RunStatus]] = {
    RunStatus.PENDING: {RunStatus.SUBMITTED, RunStatus.FAILED, RunStatus.CANCELLED},
    # SUBMITTED -> FAILED covers workloads that never start (bad image).
    RunStatus.SUBMITTED: {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.RUNNING: {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.SUCCEEDED: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELLED: set(),
}

PIPELINE_RUN = StateMachine("PipelineRun", RunStatus, _RUN_EDGES)
# A single-job Run follows the same lifecycle as a pipeline run.
RUN = StateMachine("Run", RunStatus, _RUN_EDGES)


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"  # an upstream step did not succeed


STEP_RUN = StateMachine(
    "StepRun",
    StepStatus,
    {
        StepStatus.PENDING: {StepStatus.RUNNING, StepStatus.SKIPPED, StepStatus.CANCELLED},
        StepStatus.RUNNING: {StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.CANCELLED},
        StepStatus.SUCCEEDED: set(),
        StepStatus.FAILED: set(),
        StepStatus.CANCELLED: set(),
        StepStatus.SKIPPED: set(),
    },
)


class ModelStatus(StrEnum):
    REGISTERED = "REGISTERED"
    EVALUATING = "EVALUATING"
    REJECTED = "REJECTED"
    CANDIDATE = "CANDIDATE"
    CHAMPION = "CHAMPION"
    ARCHIVED = "ARCHIVED"  # a former champion or candidate that was superseded


MODEL_VERSION = StateMachine(
    "ModelVersion",
    ModelStatus,
    {
        ModelStatus.REGISTERED: {ModelStatus.EVALUATING},
        ModelStatus.EVALUATING: {ModelStatus.REJECTED, ModelStatus.CANDIDATE},
        ModelStatus.REJECTED: set(),
        # CANDIDATE -> REJECTED: the version passed evaluation but failed its canary.
        ModelStatus.CANDIDATE: {ModelStatus.CHAMPION, ModelStatus.ARCHIVED, ModelStatus.REJECTED},
        ModelStatus.CHAMPION: {ModelStatus.ARCHIVED},
        # ARCHIVED -> CHAMPION exists only for rolling a deployment back to the
        # previous champion; nothing else may resurrect an archived version.
        ModelStatus.ARCHIVED: {ModelStatus.CHAMPION},
    },
)


class EvaluationStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"


EVALUATION = StateMachine(
    "Evaluation",
    EvaluationStatus,
    {
        EvaluationStatus.PENDING: {EvaluationStatus.RUNNING},
        EvaluationStatus.RUNNING: {EvaluationStatus.PASSED, EvaluationStatus.FAILED},
        EvaluationStatus.PASSED: set(),
        EvaluationStatus.FAILED: set(),
    },
)


class PromotionStatus(StrEnum):
    PENDING = "PENDING"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


PROMOTION = StateMachine(
    "Promotion",
    PromotionStatus,
    {
        PromotionStatus.PENDING: {PromotionStatus.APPLIED, PromotionStatus.FAILED},
        PromotionStatus.APPLIED: set(),
        PromotionStatus.FAILED: set(),
    },
)


class DeploymentStatus(StrEnum):
    PENDING = "PENDING"
    DEPLOYING = "DEPLOYING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    DELETING = "DELETING"
    DELETED = "DELETED"


DEPLOYMENT = StateMachine(
    "Deployment",
    DeploymentStatus,
    {
        DeploymentStatus.PENDING: {DeploymentStatus.DEPLOYING, DeploymentStatus.DELETING},
        DeploymentStatus.DEPLOYING: {
            DeploymentStatus.READY,
            DeploymentStatus.FAILED,
            DeploymentStatus.DELETING,
        },
        # READY/DEGRADED -> DEPLOYING: a new revision rolls out, or a lost serving
        # resource is being recreated.
        DeploymentStatus.READY: {
            DeploymentStatus.DELETING,
            DeploymentStatus.DEGRADED,
            DeploymentStatus.FAILED,
            DeploymentStatus.DEPLOYING,
        },
        DeploymentStatus.DEGRADED: {
            DeploymentStatus.DELETING,
            DeploymentStatus.READY,
            DeploymentStatus.FAILED,
            DeploymentStatus.DEPLOYING,
        },
        # A failed deployment is retried by re-deploying, not by resurrecting READY.
        DeploymentStatus.FAILED: {DeploymentStatus.DEPLOYING, DeploymentStatus.DELETING},
        DeploymentStatus.DELETING: {DeploymentStatus.DELETED},
        DeploymentStatus.DELETED: set(),
    },
)


class ModelKind(StrEnum):
    """CLASSIC: a predictive model served by the MLflow model server (v2 protocol).
    LLM: a language model served by an LLM runtime (vLLM through KServe's Hugging Face
    server), answering OpenAI-compatible chat completions."""

    CLASSIC = "classic"
    LLM = "llm"
    FUNCTION = "function"  # your own container: any JSON in, any JSON out, scales to zero


class ServingRuntime(StrEnum):
    MLFLOW = "mlflow"
    HUGGINGFACE = "huggingface"
    CONTAINER = "container"  # a function's own image


class EndpointKind(StrEnum):
    """What answers behind an endpoint. Only models are served today; the others are reserved
    so the public contract (URL, keys, limits) does not change when they arrive."""

    MODEL = "model"
    LLM = "llm"
    FUNCTION = "function"


class EndpointProtocol(StrEnum):
    V2_INFER = "v2-infer"  # {"instances": ...} -> {"predictions": ...}
    OPENAI = "openai"  # chat completions, streamed (reserved for LLMs)
    HTTP = "http"  # any JSON in, any JSON out (reserved for functions)


class Exposure(StrEnum):
    INTERNAL = "internal"  # reachable inside the platform only
    PUBLIC = "public"  # reachable through the gateway with a key


class EndpointStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    UNAVAILABLE = "UNAVAILABLE"


ENDPOINT = StateMachine(
    "Endpoint",
    EndpointStatus,
    {
        EndpointStatus.PENDING: {EndpointStatus.READY, EndpointStatus.UNAVAILABLE},
        EndpointStatus.READY: {EndpointStatus.UNAVAILABLE},
        EndpointStatus.UNAVAILABLE: {EndpointStatus.READY},
    },
)


class RolloutStatus(StrEnum):
    PENDING = "PENDING"  # recorded, no traffic shifted yet
    PROGRESSING = "PROGRESSING"  # canary receiving traffic, step by step
    SUCCEEDED = "SUCCEEDED"  # canary took 100% and the model was promoted
    ROLLED_BACK = "ROLLED_BACK"  # traffic returned to the stable revision


ROLLOUT = StateMachine(
    "Rollout",
    RolloutStatus,
    {
        RolloutStatus.PENDING: {RolloutStatus.PROGRESSING, RolloutStatus.ROLLED_BACK},
        RolloutStatus.PROGRESSING: {RolloutStatus.SUCCEEDED, RolloutStatus.ROLLED_BACK},
        RolloutStatus.SUCCEEDED: set(),
        RolloutStatus.ROLLED_BACK: set(),
    },
)
