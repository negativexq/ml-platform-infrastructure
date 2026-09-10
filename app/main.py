"""FastAPI entrypoint for the inference service."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.config import settings
from app.inference import service
from app.schemas import (
    HealthResponse,
    PredictRequest,
    PredictResponse,
    ReadyResponse,
)


def _configure_logging() -> None:
    logging.basicConfig(level=settings.log_level, format="%(message)s")
    renderer = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ]
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    _configure_logging()
    # Non-blocking: the server must answer /health even while the model is
    # still downloading, or a liveness probe would kill a healthy process.
    service.start()
    try:
        yield
    finally:
        service.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.get("/ready")
def ready() -> JSONResponse:
    ready_now = service.ready
    body = ReadyResponse(
        ready=ready_now,
        state=service.state,
        model_version=service.model_version,
        model_source=service.model_source,
        detail=service.error,
    )
    status = 200 if ready_now else 503
    return JSONResponse(status_code=status, content=body.model_dump())


@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest) -> PredictResponse:
    if not service.ready:
        raise HTTPException(status_code=503, detail="model not ready")
    value = service.predict(req.features)
    version = service.model_version or "unknown"
    return PredictResponse(prediction=value, model_version=version)
