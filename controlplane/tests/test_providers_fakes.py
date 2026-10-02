from __future__ import annotations

from uuid import UUID

import pytest

from controlplane.adapters.fakes import (
    FakeArtifactProvider,
    FakeExperimentProvider,
    FakeMetricsProvider,
    FakeServingProvider,
    FakeWorkflowProvider,
)
from controlplane.application.providers import (
    ArtifactProvider,
    ExperimentProvider,
    ExternalState,
    MetricsProvider,
    ServingProvider,
    ServingSpec,
    ServingState,
    StepSpec,
    WorkflowProvider,
    WorkflowSpec,
)
from controlplane.domain.errors import NotFound

SPEC = WorkflowSpec(
    name="train", namespace="mlp-credit-risk", steps=(StepSpec(name="train", image="img:1"),)
)


def test_fakes_satisfy_every_port() -> None:
    assert isinstance(FakeExperimentProvider(), ExperimentProvider)
    assert isinstance(FakeWorkflowProvider(), WorkflowProvider)
    assert isinstance(FakeServingProvider(), ServingProvider)
    assert isinstance(FakeMetricsProvider(), MetricsProvider)
    assert isinstance(FakeArtifactProvider(), ArtifactProvider)


def test_workflow_submit_is_idempotent_and_status_progresses() -> None:
    wf = FakeWorkflowProvider()
    ref = wf.submit(SPEC, "run-1")
    assert wf.submit(SPEC, "run-1") == ref
    assert len(wf.submitted) == 1
    assert wf.submit(SPEC, "run-2") != ref
    assert wf.get_status(ref).state is ExternalState.PENDING
    wf.set_state(ref, ExternalState.FAILED, reason="ImagePullBackOff")
    status = wf.get_status(ref)
    assert (status.state, status.reason) == (ExternalState.FAILED, "ImagePullBackOff")


def test_cancel_does_not_overwrite_a_terminal_state() -> None:
    wf = FakeWorkflowProvider()
    ref = wf.submit(SPEC, "run-1")
    wf.set_state(ref, ExternalState.SUCCEEDED)
    wf.cancel(ref)
    assert wf.get_status(ref).state is ExternalState.SUCCEEDED
    ref2 = wf.submit(SPEC, "run-2")
    wf.cancel(ref2)
    assert wf.get_status(ref2).state is ExternalState.CANCELLED


def test_unknown_workflow_is_not_found() -> None:
    with pytest.raises(NotFound):
        FakeWorkflowProvider().get_status("nope")


def test_experiment_is_deterministic_per_project() -> None:
    ex = FakeExperimentProvider()
    pid = UUID(int=7)
    assert ex.ensure_experiment(pid, "credit-risk") == ex.ensure_experiment(pid, "credit-risk")
    assert ex.get_model_alias("m", "champion") is None
    ex.set_model_alias("m", "champion", "3")
    assert ex.get_model_alias("m", "champion") == "3"


def test_serving_lifecycle_and_traffic_validation() -> None:
    sv = FakeServingProvider()
    spec = ServingSpec(name="credit-risk", namespace="mlp-a", model_uri="models:/m/1", revision=1)
    ref = sv.deploy(spec)
    assert sv.get_status(ref).state is ServingState.READY
    sv.set_traffic(ref, {1: 90, 2: 10})
    with pytest.raises(ValueError):
        sv.set_traffic(ref, {1: 50})
    sv.delete(ref)
    assert sv.get_status(ref).state is ServingState.ABSENT


def test_artifacts_roundtrip() -> None:
    art = FakeArtifactProvider()
    assert not art.exists("s3://b/k")
    art.write("s3://b/k", b"x")
    assert art.exists("s3://b/k") and art.read("s3://b/k") == b"x"
    with pytest.raises(NotFound):
        art.read("s3://b/missing")
