"""The MLflow adapter against a real MLflow (local store, no server needed)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from mlflow import MlflowClient

from controlplane.adapters.mlflow import MlflowExperimentProvider
from controlplane.application.providers import ExperimentProvider
from controlplane.application.workflow_compiler import TAG_PIPELINE_RUN_ID, TAG_STEP
from controlplane.domain.errors import NotFound


@pytest.fixture
def uri(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'mlflow.db'}"


def test_satisfies_the_port(uri: str) -> None:
    assert isinstance(MlflowExperimentProvider(uri), ExperimentProvider)


def test_ensure_experiment_is_deterministic(uri: str) -> None:
    provider = MlflowExperimentProvider(uri)
    pid = uuid4()
    first = provider.ensure_experiment(pid, "mlp-credit-risk")
    assert provider.ensure_experiment(pid, "mlp-credit-risk") == first
    assert provider.ensure_experiment(pid, "mlp-other") != first


def test_runs_are_found_by_platform_tags_with_params_metrics_and_artifact(uri: str) -> None:
    provider = MlflowExperimentProvider(uri)
    experiment = provider.ensure_experiment(uuid4(), "mlp-credit-risk")
    client = MlflowClient(tracking_uri=uri)
    mine, other = str(uuid4()), str(uuid4())
    for pipeline_run, step, auc in (
        (mine, "train", 0.93),
        (mine, "evaluate", 0.91),
        (other, "train", 0.5),
    ):
        run = client.create_run(
            experiment, tags={TAG_PIPELINE_RUN_ID: pipeline_run, TAG_STEP: step}
        )
        client.log_param(run.info.run_id, "alpha", "1.0")
        client.log_metric(run.info.run_id, "auc", auc)
        client.set_terminated(run.info.run_id)

    found = provider.find_runs(experiment, {TAG_PIPELINE_RUN_ID: mine})
    assert sorted(r.tags[TAG_STEP] for r in found) == ["evaluate", "train"]
    train = next(r for r in found if r.tags[TAG_STEP] == "train")
    assert train.params == {"alpha": "1.0"} and train.metrics == {"auc": 0.93}
    assert train.artifact_uri
    assert not any(k.startswith("mlflow.") for r in found for k in r.tags)

    both = provider.find_runs(experiment, {TAG_PIPELINE_RUN_ID: mine, TAG_STEP: "train"})
    assert len(both) == 1 and provider.get_run(both[0].ref) == both[0]
    assert provider.find_runs(experiment, {TAG_PIPELINE_RUN_ID: str(uuid4())}) == []


def test_unknown_run_and_alias(uri: str) -> None:
    provider = MlflowExperimentProvider(uri)
    with pytest.raises(NotFound):
        provider.get_run("does-not-exist")
    assert provider.get_model_alias("no-such-model", "champion") is None


def test_registry_versions_and_aliases(uri: str, tmp_path: Path) -> None:
    provider = MlflowExperimentProvider(uri)
    client = MlflowClient(tracking_uri=uri)
    experiment = provider.ensure_experiment(uuid4(), "mlp-credit-risk")
    assert provider.list_model_versions("credit-risk-scorer") == []  # unknown name: empty

    client.create_registered_model("credit-risk-scorer")
    run_ids = []
    for _ in range(2):
        run = client.create_run(experiment)
        client.set_terminated(run.info.run_id)
        run_ids.append(run.info.run_id)
        client.create_model_version(
            "credit-risk-scorer", source=str(tmp_path), run_id=run.info.run_id
        )

    versions = provider.list_model_versions("credit-risk-scorer")
    assert [(v.ref, v.run_ref) for v in versions] == [("1", run_ids[0]), ("2", run_ids[1])]

    assert provider.model_artifact_uri("credit-risk-scorer", "1") == str(tmp_path)
    assert provider.model_artifact_uri("credit-risk-scorer", "99") is None
    assert provider.get_model_alias("credit-risk-scorer", "champion") is None
    provider.set_model_alias("credit-risk-scorer", "champion", "1")
    assert provider.get_model_alias("credit-risk-scorer", "champion") == "1"
    provider.set_model_alias("credit-risk-scorer", "champion", "2")  # move it
    assert provider.get_model_alias("credit-risk-scorer", "champion") == "2"
    provider.delete_model_alias("credit-risk-scorer", "champion")
    assert provider.get_model_alias("credit-risk-scorer", "champion") is None
    provider.delete_model_alias("credit-risk-scorer", "champion")  # already gone: no error


@pytest.mark.parametrize("include_model_id", [False, True])
def test_logged_model_source_resolves_without_registry_model_id(
    uri: str, include_model_id: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    provider = MlflowExperimentProvider(uri)
    client = MlflowClient(tracking_uri=uri)
    experiment = provider.ensure_experiment(uuid4(), "logged-model")
    logged = client.create_logged_model(experiment, name="scorer")
    client.create_registered_model("scorer")
    version = client.create_model_version(
        "scorer",
        source=f"models:/{logged.model_id}",
        model_id=logged.model_id if include_model_id else None,
    )
    assert provider.model_artifact_uri("scorer", version.version) == logged.artifact_location
