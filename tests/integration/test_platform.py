"""Integration tests against the running M1 stack.

Skipped unless `RUN_INTEGRATION=1` and the compose stack is up:

    make platform-up && make integration
"""

from __future__ import annotations

import os

import pytest

requests = pytest.importorskip("requests")

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the compose stack running",
)

MLFLOW = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5001")
INFERENCE = os.environ.get("INFERENCE_URL", "http://localhost:8000")


def test_mlflow_server_is_healthy():
    assert requests.get(f"{MLFLOW}/health", timeout=10).status_code == 200


def test_registered_model_has_versions():
    r = requests.get(
        f"{MLFLOW}/api/2.0/mlflow/registered-models/get",
        params={"name": "ml-platform-model"},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    assert r.json()["registered_model"]["name"] == "ml-platform-model"


def test_inference_is_live_and_ready():
    assert requests.get(f"{INFERENCE}/health", timeout=10).status_code == 200
    r = requests.get(f"{INFERENCE}/ready", timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ready"] is True
    assert body["model_source"] == "mlflow"


def test_inference_serves_the_mlflow_artifact():
    r = requests.post(
        f"{INFERENCE}/predict",
        json={"features": [1.0, 2.0, 3.0]},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["prediction"], float)
    assert body["model_version"].startswith("models:/ml-platform-model/")
