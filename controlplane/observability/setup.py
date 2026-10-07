"""OpenTelemetry providers from the standard OTEL_* environment.

No exporter threads are started unless an OTLP endpoint is configured.
Signals are independently enabled; calls to no-op instruments can still incur cost.
Export uses OTLP over HTTP/protobuf (the
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
    return signal_requested("traces") or signal_requested("metrics")


def signal_requested(signal: str) -> bool:
    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return False
    if os.getenv(f"OTEL_{signal.upper()}_EXPORTER", "").lower() == "none":
        return False
    return bool(
        os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.getenv(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT")
    )


def _resource(service_name: str) -> Resource:
    resource = Resource.create({})  # defaults + OTEL_SERVICE_NAME + OTEL_RESOURCE_ATTRIBUTES
    if str(resource.attributes.get("service.name", "")).startswith("unknown_service"):
        resource = resource.merge(Resource({"service.name": service_name}))
    pod_attributes = {
        "service.instance.id": os.getenv("MLP_POD_UID"),
        "k8s.pod.uid": os.getenv("MLP_POD_UID"),
        "k8s.pod.name": os.getenv("MLP_POD_NAME"),
        "k8s.namespace.name": os.getenv("MLP_POD_NAMESPACE"),
        "k8s.node.name": os.getenv("MLP_NODE_NAME"),
    }
    # Explicit OTEL_RESOURCE_ATTRIBUTES win; downward API supplies missing defaults.
    return Resource({k: v for k, v in pod_attributes.items() if v}).merge(resource)


def setup_providers(
    service_name: str,
    *,
    sdk: Any = None,
    trace_sample_rate: float | None = None,
    metric_export_interval_ms: int | None = None,
) -> Telemetry:
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
    tracer_provider = None
    meter_provider = None
    if signal_requested("traces"):
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

        sampler = (
            ParentBased(TraceIdRatioBased(trace_sample_rate))
            if trace_sample_rate is not None
            else None
        )
        tracer_provider = TracerProvider(resource=resource, sampler=sampler)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tracer_provider)
    if signal_requested("metrics"):
        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(),
                    export_interval_millis=metric_export_interval_ms,
                )
            ],
        )
        metrics.set_meter_provider(meter_provider)
    return Telemetry(bool(tracer_provider or meter_provider), tracer_provider, meter_provider)
