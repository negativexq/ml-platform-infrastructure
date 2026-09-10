"""Model loading with a bounded-retry contract.

Two sources are supported:

* `ML_MODEL_URI` set  → resolve through MLflow (artifact lives in MinIO/S3)
* otherwise           → load the local joblib file at `ML_MODEL_PATH`

Either way a failure is surfaced to readiness rather than crashing the process.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Protocol

import joblib
import structlog

from app.config import settings

log = structlog.get_logger(__name__)


class Predictor(Protocol):
    def predict(self, x: Any) -> Any: ...


class LoadedModel:
    def __init__(self, predictor: Predictor, version: str, source: str) -> None:
        self.predictor = predictor
        self.version = version
        self.source = source


def _load_local() -> LoadedModel:
    path = Path(settings.model_path)
    if not path.is_file():
        raise FileNotFoundError(f"model file not found: {path}")
    predictor: Predictor = joblib.load(path)
    return LoadedModel(predictor=predictor, version=path.stem, source="local")


def _load_mlflow(model_uri: str) -> LoadedModel:
    # Imported lazily so the local path stays importable without MLflow config.
    import mlflow

    if settings.mlflow_s3_endpoint_url:
        os.environ.setdefault("MLFLOW_S3_ENDPOINT_URL", settings.mlflow_s3_endpoint_url)
    # Keep MLflow's own retry budget small so a single attempt fails fast and
    # this module's bounded loop stays the one retry policy that matters.
    os.environ.setdefault(
        "MLFLOW_HTTP_REQUEST_MAX_RETRIES", str(settings.mlflow_http_request_max_retries)
    )
    os.environ.setdefault(
        "MLFLOW_HTTP_REQUEST_TIMEOUT", str(settings.mlflow_http_request_timeout)
    )
    if settings.mlflow_tracking_uri:
        mlflow.set_tracking_uri(settings.mlflow_tracking_uri)

    predictor: Predictor = mlflow.pyfunc.load_model(model_uri)
    return LoadedModel(predictor=predictor, version=model_uri, source="mlflow")


def _load_once() -> LoadedModel:
    if settings.model_uri:
        return _load_mlflow(settings.model_uri)
    return _load_local()


def load_model() -> LoadedModel:
    """Attempt to load the model, retrying up to the configured bound."""
    attempts = settings.model_load_max_retries
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            model = _load_once()
            log.info(
                "model.loaded",
                version=model.version,
                source=model.source,
                attempt=attempt,
            )
            return model
        except Exception as err:  # noqa: BLE001 - surfaced via readiness
            last_err = err
            log.warning("model.load_failed", attempt=attempt, error=str(err))
            if attempt < attempts:
                time.sleep(settings.model_load_retry_seconds)
    assert last_err is not None
    raise last_err
