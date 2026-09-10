"""Model loading with a bounded-retry contract.

M0 loads a local joblib file. M1 will extend `load_model` to resolve an
MLflow model URI and download the artifact from object storage before load.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol

import joblib
import structlog

from app.config import settings

log = structlog.get_logger(__name__)


class Predictor(Protocol):
    def predict(self, x: list[list[float]]) -> list[float]: ...


class LoadedModel:
    def __init__(self, predictor: Predictor, version: str) -> None:
        self.predictor = predictor
        self.version = version


def _load_once() -> LoadedModel:
    path = Path(settings.model_path)
    if not path.is_file():
        raise FileNotFoundError(f"model file not found: {path}")
    predictor: Predictor = joblib.load(path)
    version = settings.model_uri or path.stem
    return LoadedModel(predictor=predictor, version=version)


def load_model() -> LoadedModel:
    """Attempt to load the model, retrying up to the configured bound."""
    attempts = settings.model_load_max_retries
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            model = _load_once()
            log.info("model.loaded", version=model.version, attempt=attempt)
            return model
        except Exception as err:  # noqa: BLE001 - surfaced via readiness
            last_err = err
            log.warning("model.load_failed", attempt=attempt, error=str(err))
            if attempt < attempts:
                time.sleep(settings.model_load_retry_seconds)
    assert last_err is not None
    raise last_err
