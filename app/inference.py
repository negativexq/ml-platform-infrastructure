"""Inference state and prediction logic.

Model loading never blocks process startup. The HTTP server must answer
`/health` immediately so a liveness probe cannot kill a pod that is merely
waiting on a slow or unavailable artifact store; readiness is what gates
traffic. Loading therefore runs on a background thread:

    loading ──success──> ready
       │
       └──all initial attempts failed──> failed ──(recheck loop)──> ready
"""

from __future__ import annotations

import threading
import time
from typing import Literal

import numpy as np
import structlog

from app.config import settings
from app.model_loader import LoadedModel, load_model

log = structlog.get_logger(__name__)

State = Literal["loading", "ready", "failed"]


class InferenceService:
    """Holds the loaded model and exposes readiness + prediction."""

    def __init__(self) -> None:
        self._model: LoadedModel | None = None
        self._error: str | None = None
        self._load_seconds: float | None = None
        self._state: State = "loading"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Kick off model loading in the background and return immediately."""
        self._stop.clear()
        thread = threading.Thread(target=self._run, name="model-loader", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        self._attempt_load()
        # Keep retrying in the background so the service recovers on its own
        # once a bad artifact is fixed or the artifact store comes back.
        interval = settings.model_load_recheck_seconds
        while interval > 0 and not self._stop.wait(interval):
            with self._lock:
                if self._state == "ready":
                    return
            self._attempt_load()

    def _attempt_load(self) -> None:
        started = time.perf_counter()
        try:
            model = load_model()
        except Exception as err:  # noqa: BLE001 - reported via /ready
            with self._lock:
                self._model = None
                self._error = str(err)
                self._load_seconds = None
                self._state = "failed"
            log.error("inference.model_unavailable", error=str(err))
            return

        with self._lock:
            self._model = model
            self._error = None
            self._load_seconds = time.perf_counter() - started
            self._state = "ready"

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._state == "ready"

    @property
    def model_version(self) -> str | None:
        with self._lock:
            return self._model.version if self._model else None

    @property
    def model_source(self) -> str | None:
        with self._lock:
            return self._model.source if self._model else None

    @property
    def load_seconds(self) -> float | None:
        with self._lock:
            return self._load_seconds

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    # -- serving -----------------------------------------------------------

    def predict(self, features: list[float]) -> float:
        with self._lock:
            model = self._model
        if model is None:
            raise RuntimeError("model not loaded")
        batch = np.asarray([features], dtype=float)
        result = model.predictor.predict(batch)
        return float(np.asarray(result).reshape(-1)[0])


service = InferenceService()
