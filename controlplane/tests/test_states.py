from __future__ import annotations

import itertools
from enum import StrEnum

import pytest

from controlplane.domain import states
from controlplane.domain.errors import IllegalTransition
from controlplane.domain.states import (
    DeploymentStatus,
    ModelStatus,
    RunStatus,
    StateMachine,
)

MACHINES = [
    states.PROJECT,
    states.PIPELINE_RUN,
    states.RUN,
    states.STEP_RUN,
    states.MODEL_VERSION,
    states.EVALUATION,
    states.PROMOTION,
    states.DEPLOYMENT,
    states.ENDPOINT,
    states.ROLLOUT,
]


@pytest.mark.parametrize("machine", MACHINES, ids=lambda m: m.name)
def test_every_pair_is_either_declared_legal_or_rejected(machine: StateMachine[StrEnum]) -> None:
    for source, target in itertools.product(machine.states, repeat=2):
        if target in machine.allowed_from(source):
            machine.ensure(source, target)
        else:
            with pytest.raises(IllegalTransition):
                machine.ensure(source, target)


@pytest.mark.parametrize("machine", MACHINES, ids=lambda m: m.name)
def test_no_self_transitions(machine: StateMachine[StrEnum]) -> None:
    for state in machine.states:
        assert not machine.can_transition(state, state)


@pytest.mark.parametrize("machine", MACHINES, ids=lambda m: m.name)
def test_every_state_is_reachable_from_the_initial_state(machine: StateMachine[StrEnum]) -> None:
    seen = {machine.initial}
    frontier = [machine.initial]
    while frontier:
        for nxt in machine.allowed_from(frontier.pop()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    assert seen == set(machine.states)


def test_missing_transition_declaration_fails_at_definition() -> None:
    class Two(StrEnum):
        A = "A"
        B = "B"

    with pytest.raises(ValueError, match="no transitions declared"):
        StateMachine("Broken", Two, {Two.A: {Two.B}})


# The lifecycles the roadmap spells out, asserted literally.


def test_pipeline_run_happy_path_and_terminals() -> None:
    m = states.PIPELINE_RUN
    for a, b in [("PENDING", "SUBMITTED"), ("SUBMITTED", "RUNNING"), ("RUNNING", "SUCCEEDED")]:
        m.ensure(RunStatus(a), RunStatus(b))
    for terminal in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
        assert m.is_terminal(terminal)


def test_pipeline_run_cannot_skip_submission() -> None:
    with pytest.raises(IllegalTransition):
        states.PIPELINE_RUN.ensure(RunStatus.PENDING, RunStatus.RUNNING)


def test_only_archived_to_champion_can_resurrect_an_archived_version() -> None:
    allowed = states.MODEL_VERSION.allowed_from(ModelStatus.ARCHIVED)
    assert allowed == {ModelStatus.CHAMPION}


def test_rejected_model_can_never_be_promoted() -> None:
    assert states.MODEL_VERSION.is_terminal(ModelStatus.REJECTED)
    for target in (ModelStatus.CANDIDATE, ModelStatus.CHAMPION):
        with pytest.raises(IllegalTransition):
            states.MODEL_VERSION.ensure(ModelStatus.REJECTED, target)


def test_model_cannot_become_champion_without_being_a_candidate() -> None:
    for source in (ModelStatus.REGISTERED, ModelStatus.EVALUATING):
        with pytest.raises(IllegalTransition):
            states.MODEL_VERSION.ensure(source, ModelStatus.CHAMPION)


def test_deployment_cannot_jump_to_ready() -> None:
    for source in (DeploymentStatus.PENDING, DeploymentStatus.FAILED):
        with pytest.raises(IllegalTransition):
            states.DEPLOYMENT.ensure(source, DeploymentStatus.READY)
