"""OpenTelemetry providers from the standard OTEL_* environment.

Nothing is exported unless an OTLP endpoint is configured, so tests and a bare `uvicorn`
run carry no exporter, no threads and no overhead. Export is OTLP over HTTP/protobuf (the
same protocol FastAPI's own automatic configuration supports).

    OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.observability:4318
    OTEL_SERVICE_NAME=mlp-controlplane-api            # optional: the code supplies a default
    OTEL_RESOURCE_ATTRIBUTES=deployment.environment=local,k8s.namespace.name=ml-platform
    OTEL_TRACES_SAMPLER=parentbased_traceidratio  OTEL_TRACES_SAMPLER_ARG=0.25
    OTEL_SDK_DISABLED=true                            # kill switch
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

_ENDPOINT_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
)


@dataclass
class Telemetry:
    enabled: bool
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None

    def shutdown(self) -> None:
        """Flush what is buffered. Call on a clean exit so the last spans are not lost."""
        for provider in (self.tracer_provider, self.meter_provider):
            if provider is not None:
                provider.shutdown()


def export_requested() -> bool:
    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return False
    return any(os.getenv(name) for name in _ENDPOINT_VARS)


def _resource(service_name: str) -> Resource:
    resource = Resource.create({})  # defaults + OTEL_SERVICE_NAME + OTEL_RESOURCE_ATTRIBUTES
    if str(resource.attributes.get("service.name", "")).startswith("unknown_service"):
        resource = resource.merge(Resource({"service.name": service_name}))
    return resource


def setup_providers(service_name: str, *, sdk: Any = None) -> Telemetry:
    """Create and register the global tracer and meter providers, if export is requested."""
    if not export_requested():
        return Telemetry(enabled=False)
    protocol = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    if protocol != "http/protobuf":
        raise RuntimeError(
            f"OTEL_EXPORTER_OTLP_PROTOCOL={protocol!r} is not supported; use http/protobuf "
            "(point at the collector's OTLP/HTTP port, 4318)"
        )
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = _resource(service_name)
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    meter_provider = MeterProvider(
        resource=resource, metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())]
    )
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    return Telemetry(True, tracer_provider, meter_provider)
