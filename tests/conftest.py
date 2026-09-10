from __future__ import annotations

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
    return str(path)


@pytest.fixture
def client_ready(model_file) -> Iterator[TestClient]:
    _reload_settings()
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_no_model(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("ML_MODEL_PATH", str(tmp_path / "missing.joblib"))
    monkeypatch.setenv("ML_MODEL_LOAD_MAX_RETRIES", "1")
    monkeypatch.setenv("ML_MODEL_LOAD_RETRY_SECONDS", "0")
    _reload_settings()
    from app.main import app

    with TestClient(app) as c:
        yield c


def _reload_settings() -> None:
    import importlib

    import app.config
    import app.inference
    import app.model_loader

    importlib.reload(app.config)
    importlib.reload(app.model_loader)
    importlib.reload(app.inference)
    import app.main

    importlib.reload(app.main)
