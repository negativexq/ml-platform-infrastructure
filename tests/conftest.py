from __future__ import annotations

import importlib
import time
from collections.abc import Iterator

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sklearn.linear_model import LinearRegression


@pytest.fixture
def model_file(tmp_path, monkeypatch) -> str:
    import joblib

    rng = np.random.default_rng(0)
    x = rng.normal(size=(50, 3))
    y = x @ np.array([1.0, 0.0, -1.0])
    path = tmp_path / "model.joblib"
    joblib.dump(LinearRegression().fit(x, y), path)
    monkeypatch.setenv("ML_MODEL_PATH", str(path))
    monkeypatch.setenv("ML_MODEL_LOAD_RECHECK_SECONDS", "0")
    return str(path)


@pytest.fixture
def client_ready(model_file) -> Iterator[TestClient]:
    _reload_app()
    from app.main import app

    with TestClient(app) as c:
        _wait_ready(c)
        yield c


@pytest.fixture
def client_no_model(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("ML_MODEL_PATH", str(tmp_path / "missing.joblib"))
    monkeypatch.setenv("ML_MODEL_LOAD_MAX_RETRIES", "1")
    monkeypatch.setenv("ML_MODEL_LOAD_RETRY_SECONDS", "0")
    monkeypatch.setenv("ML_MODEL_LOAD_RECHECK_SECONDS", "0")
    _reload_app()
    from app.main import app

    with TestClient(app) as c:
        _wait_state(c, "failed")
        yield c


def _wait_ready(client: TestClient, timeout: float = 10.0) -> None:
    _wait_state(client, "ready", timeout)


def _wait_state(client: TestClient, state: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get("/ready").json().get("state") == state:
            return
        time.sleep(0.05)
    raise AssertionError(f"service did not reach state {state!r} within {timeout}s")


def _reload_app() -> None:
    """Re-import the app modules so patched env vars take effect."""
    import app.config
    import app.inference
    import app.main
    import app.model_loader

    importlib.reload(app.config)
    importlib.reload(app.model_loader)
    importlib.reload(app.inference)
    importlib.reload(app.main)
