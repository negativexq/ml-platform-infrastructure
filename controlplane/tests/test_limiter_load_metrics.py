"""Load evidence must isolate each sample and reject reset/incomplete histograms."""

import pytest

from scripts.controlplane_limiter_check import limiter_histogram_delta


def series(instance: str, count: float, total: float) -> list[dict]:
    base = {"job": "mlp-gateway", "instance": instance, "operation": "take"}
    result = []
    for boundary, value in (("0.01", count / 2), ("0.1", count), ("+Inf", count)):
        result.append(
            {
                "metric": {
                    **base,
                    "__name__": "mlp_gateway_limiter_duration_seconds_bucket",
                    "le": boundary,
                },
                "value": [0, str(value)],
            }
        )
    for name, value in (("count", count), ("sum", total)):
        result.append(
            {
                "metric": {**base, "__name__": "mlp_gateway_limiter_duration_seconds_" + name},
                "value": [0, str(value)],
            }
        )
    return result


def test_histogram_delta_excludes_previous_load_and_combines_replicas() -> None:
    before = series("first", 100, 1) + series("second", 200, 2)
    after = series("first", 110, 1.2) + series("second", 220, 2.4)
    result = limiter_histogram_delta(before, after)
    assert result["observations"] == 30
    assert result["mean_ms"] == pytest.approx(20)
    assert result["p50"] == pytest.approx(10)
    assert result["p95"] == pytest.approx(91)


@pytest.mark.parametrize("after", [[], series("first", 90, 0.9), series("first", 110, 1.2)[:-2]])
def test_histogram_rejects_empty_reset_or_incomplete_sample(after: list[dict]) -> None:
    with pytest.raises(ValueError):
        limiter_histogram_delta(series("first", 100, 1), after)


def healthy_sample() -> dict:
    return {
        "shared_budget_upper_bound_holds": True,
        "allowed": 699,
        "limiter_observation_count_matches": True,
        "statuses": {"200": 699, "429": 4301},
        "completed_rps": 493,
        "offered_rps": 500,
        "client_queue_latency_ms": {"p95": 0},
        "scheduling_lag_p95_ms": 1,
    }


def test_expected_429s_do_not_fail_a_responsive_shared_budget() -> None:
    from scripts.controlplane_limiter_matrix import sample_passed

    assert sample_passed(healthy_sample())


@pytest.mark.parametrize(
    "changes",
    [
        {"client_queue_latency_ms": {"p95": 101}},
        {"scheduling_lag_p95_ms": 101},
        {"completed_rps": 400},
        {"shared_budget_upper_bound_holds": False},
        {"limiter_observation_count_matches": False},
        {"allowed": 0},
        {"statuses": {"500": 1, "200": 699, "429": 4300}},
    ],
)
def test_availability_gate_rejects_backlog_missing_evidence_and_errors(changes: dict) -> None:
    from scripts.controlplane_limiter_matrix import sample_passed

    assert not sample_passed({**healthy_sample(), **changes})
