"""Typed request/response models for the inference API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PredictRequest(BaseModel):
    """A single feature vector to score."""

    features: list[float] = Field(..., min_length=1, description="Ordered feature values")


class PredictResponse(BaseModel):
    prediction: float
    model_version: str


class HealthResponse(BaseModel):
    status: str = "ok"


class ReadyResponse(BaseModel):
    ready: bool
    state: str = "loading"
    model_version: str | None = None
    model_source: str | None = None
    detail: str | None = None
