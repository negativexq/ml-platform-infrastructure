"""Serving adapters. Implements ServingProvider; the in-memory fake lives in adapters/fakes.py."""

from controlplane.adapters.serving.kserve import KServeServingProvider

__all__ = ["KServeServingProvider"]
