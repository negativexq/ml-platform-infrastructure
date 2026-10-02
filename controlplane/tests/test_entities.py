from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import UUID

import pytest

from controlplane.domain.entities import (
    Deployment,
    Evaluation,
    ModelVersion,
    PipelineRun,
    Project,
    Promotion,
    StepRun,
    validate_slug,
)
from controlplane.domain.errors import IllegalTransition, InvalidArgument
from controlplane.domain.states import (
    DeploymentStatus,
    EvaluationStatus,
    ModelStatus,
    ProjectStatus,
    PromotionStatus,
    RunStatus,
    StepStatus,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 1, 2, tzinfo=UTC)
ID = UUID(int=1)


def test_project_gets_a_stable_uuid_and_pending_status() -> None:
    p = Project.create(name="credit-risk", display_name=None, description="", now=T0)
    assert isinstance(p.id, UUID)
    assert p.display_name == "credit-risk"
    assert p.status is ProjectStatus.PENDING


@pytest.mark.parametrize(
    "name", ["ab", "Credit", "1abc", "a_b_c", "abc-", "-abc", "a--b", "x" * 41, ""]
)
def test_invalid_slugs_are_rejected(name: str) -> None:
    with pytest.raises(InvalidArgument):
        validate_slug(name)


@pytest.mark.parametrize("name", ["abc", "credit-risk", "a1b2", "x" * 40])
def test_valid_slugs(name: str) -> None:
    assert validate_slug(name) == name


def test_transition_returns_a_new_instance_and_leaves_the_original_untouched() -> None:
    p = Project.create(name="alpha", display_name=None, description="", now=T0)
    q = p.transition_to(ProjectStatus.PROVISIONING, T1)
    assert (p.status, q.status) == (ProjectStatus.PENDING, ProjectStatus.PROVISIONING)
    assert q.updated_at == T1 and q.id == p.id
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.status = ProjectStatus.READY  # type: ignore[misc]


def test_illegal_project_transition_is_rejected() -> None:
    p = Project.create(name="alpha", display_name=None, description="", now=T0)
    with pytest.raises(IllegalTransition):
        p.transition_to(ProjectStatus.READY, T1)


def test_every_stateful_entity_enforces_its_machine() -> None:
    cases = [
        (
            PipelineRun(project_id=ID, pipeline_definition_id=ID, created_at=T0, updated_at=T0),
            RunStatus.RUNNING,
        ),
        (
            StepRun(pipeline_run_id=ID, step_name="train", created_at=T0, updated_at=T0),
            StepStatus.SUCCEEDED,
        ),
        (ModelVersion(model_id=ID, version=1, created_at=T0, updated_at=T0), ModelStatus.CHAMPION),
        (Evaluation(model_version_id=ID, created_at=T0, updated_at=T0), EvaluationStatus.PASSED),
        (Promotion(model_version_id=ID, created_at=T0, updated_at=T0), PromotionStatus.PENDING),
        (
            Deployment(project_id=ID, name="prod", created_at=T0, updated_at=T0),
            DeploymentStatus.READY,
        ),
    ]
    for entity, illegal_target in cases:
        with pytest.raises(IllegalTransition):
            entity.transition_to(illegal_target, T1)  # type: ignore[attr-defined]


def test_ids_are_unique_per_instance() -> None:
    a = Deployment(project_id=ID, name="a", created_at=T0, updated_at=T0)
    b = Deployment(project_id=ID, name="a", created_at=T0, updated_at=T0)
    assert a.id != b.id
