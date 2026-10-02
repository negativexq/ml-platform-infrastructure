"""Metrics adapters. Implements MetricsProvider; the in-memory fake lives in adapters/fakes.py."""

from controlplane.adapters.metrics.prometheus import PrometheusMetricsProvider

__all__ = ["PrometheusMetricsProvider"]
