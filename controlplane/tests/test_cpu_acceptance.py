"""Live-gate polling must accept Kubernetes status objects and reject app failures."""

import pytest

from controlplane.application.providers import RevisionMetrics
from scripts import controlplane_cpu_acceptance as gate


def test_kubernetes_status_object_can_progress_to_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate.time, "sleep", lambda _: None)
    states = iter([{"status": {"conditions": []}}, {"status": {"ready": True}}])
    result = gate._poll(
        lambda: next(states), lambda x: x["status"].get("ready", False), "drift", timeout=1
    )
    assert result["status"]["ready"]


@pytest.mark.parametrize("status", ["FAILED", "REJECTED"])
def test_application_terminal_failure_stops_polling(status: str) -> None:
    with pytest.raises(RuntimeError, match="training failed"):
        gate._poll(lambda: {"status": status}, lambda _: False, "training", timeout=1)


@pytest.mark.parametrize(
    ("backend", "backend_apply", "matched"),
    [
        (None, "old", False),
        ("original", "old", False),
        ("injected", "old", False),
        ("repaired", "repair", True),
    ],
)
def test_drift_poll_waits_for_backend_apply_identity(
    backend: str | None, backend_apply: str, matched: bool
) -> None:
    service = {
        "spec": {"predictor": {"annotations": {"mlp.io/apply-id": "repair"}}},
        "status": {"components": {"predictor": {"latestReadyRevision": backend}}},
    }
    assert (
        gate._repaired_backend_matches(
            service,
            lambda _: {"metadata": {"annotations": {"mlp.io/apply-id": backend_apply}}},
            "old",
            "original",
        )
        is matched
    )


def test_idle_window_does_not_erase_observed_backend_traffic() -> None:
    measured = {}
    gate._retain_metric_evidence(measured, "stable", 1, RevisionMetrics(5, 0, 2, 120))
    observed = dict(measured["stable"])
    gate._retain_metric_evidence(measured, "stable", 1, RevisionMetrics(None, None, 0, 0))
    assert measured["stable"] == observed
    gate._retain_metric_evidence(measured, "candidate", 2, RevisionMetrics(None, None, None, None))
    assert "candidate" not in measured
