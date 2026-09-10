"""Inference state and prediction logic."""

from __future__ import annotations

import structlog

from app.model_loader import LoadedModel, load_model

log = structlog.get_logger(__name__)


class InferenceService:
    """Holds the loaded model and exposes readiness + prediction."""

    def __init__(self) -> None:
        self._model: LoadedModel | None = None
        self._error: str | None = None

    def startup(self) -> None:
        try:
            self._model = load_model()
            self._error = None
        except Exception as err:  # noqa: BLE001 - reported via /ready
            self._model = None
            self._error = str(err)
            log.error("inference.startup_failed", error=self._error)

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def model_version(self) -> str | None:
        return self._model.version if self._model else None

    @property
    def error(self) -> str | None:
        return self._error

    def predict(self, features: list[float]) -> float:
        if self._model is None:
            raise RuntimeError("model not loaded")
        result = self._model.predictor.predict([features])
        return float(result[0])


service = InferenceService()
