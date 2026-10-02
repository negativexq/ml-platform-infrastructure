# Observability

Logs, metrics and traces for the control plane and the inference service, wired through
OpenTelemetry. The goal is one thing you could not do before: **open the trace of a request
and follow it into the reconciler minutes later**, down to the call that failed.

```
 client ──traceparent──▶ API ──┐                         ┌─▶ Tempo (traces)
                               ├─ OTLP/HTTP :4318 ─▶ Collector ─┤
 reconciler (other process) ───┤                         └─▶ Prometheus exporter ─▶ ServiceMonitor ─▶ Prometheus ─▶ alerts
 inference service ────────────┘
```

## What you get

| Signal | Source | Notes |
| --- | --- | --- |
| **Server spans, HTTP metrics** | FastAPI's native OpenTelemetry (>= 0.142) | span per request named by route template (`GET /projects/{project_id}`), `fastapi.dependencies/endpoint/serialization` child spans, `http.server.request.duration`. Probes and `/ui` are excluded. |
| **Lifecycle spans** | `observability/uow.py` | one short span per audit event (`pipeline_run.submitted`, `deployment.ready`, `rollout.rolled_back`, ...), emitted **only after the transaction commits**. A rollback reports nothing. A pass that changes nothing emits nothing. |
| **Provider spans** | `observability/providers.py` | `workflow.submit`, `serving.deploy`, `cluster.apply`, ... only for calls that *change* something. Reads (`get_status`, `observe`) are metrics only, so polling does not flood the trace of the request that started the work. |
| **Control-plane metrics** | `observability/metrics.py` | see below |
| **Trace-correlated logs** | `observability/log.py`, `app/main.py` | JSON logs with `service`, `trace_id`, `span_id` |
| **Trace-correlated audit** | `audit_events.trace_id` (migration 0008) | every audit row carries the trace it happened in |

## One trace across the async boundary

The API and the reconciler are different processes; the request is long gone when the
reconciler acts. So:

1. The API stores the request's W3C `traceparent` on the entity it creates (a column on
   projects, runs, pipeline runs, deployments, rollouts; `application/context.py` is the only
   thing the application layer knows about tracing, and it sees an opaque string).
2. The reconciler reads that `traceparent` before reconciling the entity and binds it for the
   duration (`observability/reconcile.py`). Lifecycle spans and provider spans are parented on it.
3. Result: `POST /projects/x/pipelines/p/runs` → `pipeline_run.created` → (API process) …
   `pipeline_run.submitted`, `argo.submit`, `step_run.running`, … `pipeline_run.succeeded`
   (reconciler process) are **one trace**. A deployment's trace carries its rollout, every
   canary step and a rollback if there is one.

Tested: `controlplane/tests/test_observability.py::test_one_trace_from_request_to_reconciler`.

## Enabling it

Nothing is exported unless an OTLP endpoint is set; tests and a bare `uvicorn` carry no
exporter, thread or overhead.

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.observability.svc.cluster.local:4318
OTEL_RESOURCE_ATTRIBUTES=deployment.environment=local,k8s.namespace.name=ml-platform
OTEL_TRACES_SAMPLER=parentbased_traceidratio OTEL_TRACES_SAMPLER_ARG=0.25   # optional
OTEL_SDK_DISABLED=true                                                      # kill switch
```

Only OTLP over **HTTP/protobuf** is supported (port 4318); `OTEL_EXPORTER_OTLP_PROTOCOL=grpc`
fails at start with a clear message. Service names: `mlp-controlplane-api`,
`mlp-controlplane-reconciler`, `ml-platform-inference`.

Settings: `CP_LOG_JSON` (default true), `CP_LOG_LEVEL` (default INFO).

## Metrics (as Prometheus sees them)

| Metric | Labels | Use |
| --- | --- | --- |
| `mlp_reconcile_passes_total` | `reconciler` | **heartbeat**: flat = stalled reconciler |
| `mlp_reconcile_runs_total` | `reconciler`, `outcome` (`changed` `unchanged` `conflict` `error`) | what reconciling is doing |
| `mlp_reconcile_duration_seconds` | `reconciler` | histogram per entity |
| `mlp_state_transitions_total` | `entity_type`, `action` | every audit event |
| `mlp_provider_calls_total` | `provider`, `operation`, `outcome` (`ok` `not_found` `error`) | health of Argo, KServe, MLflow, Prometheus, Kubernetes API |
| `mlp_provider_duration_seconds` | `provider`, `operation` | histogram |
| `http_server_request_duration_seconds_*` | `http_route`, `http_response_status_code`, ... | API traffic, from FastAPI itself |

The inference service keeps its existing Prometheus metrics (`/metrics`): the M5/M10 alerts
are written on them.

## Where it lives

```
observability/otel/collector.yaml       Collector config (OTLP in; Tempo + Prometheus exporter out)
observability/otel/tempo.yaml           Tempo, single binary
observability/controlplane-alert-rules.yaml (+ .test.yaml)   8 alerts, promtool-tested
observability/dashboards/controlplane.json
k8s/observability/                      Collector, Tempo, ServiceMonitor, Grafana datasource
scripts/observability-up.sh             installs all of it next to kube-prometheus-stack
```

Alerts: `ReconcilerStalled`, `ReconcilerGone`, `ReconcileErrors`, `ProviderCallsFailing`,
`RolloutRolledBack`, `DeploymentFailed`, `ControlPlaneHighErrorRate`, `ControlPlaneHighLatency`.

## Verified here (and what was not)

Run for real in the sandbox, with the actual binaries (otelcol-contrib 0.116.1, Tempo 2.7.1,
Prometheus 3.1.0): the demo control plane and the inference service exported to the Collector;
a trace sent with an incoming `traceparent` was read back from **Tempo** with the request span,
the FastAPI child spans and the lifecycle spans in the one trace; Prometheus scraped the
Collector's exporter and every query in `controlplane.json` returned data; `promtool test rules`
passes; the configs validate (`otelcol validate`, Tempo `-config.verify`); the manifests pass
`kubeconform`. **Not verified:** the manifests inside a real cluster, Grafana provisioning the
datasource, and the Collector under load. See `docs/local-verification.md` §10.

## Deliberately not done

- No OTLP *logs* pipeline: structured stdout logs carry `trace_id`, which is the join. A log
  backend (Loki) is a separate decision.
- No tail sampling or span metrics in the Collector. Add them when volume warrants.
- Tempo keeps traces for 24 h on an `emptyDir`.
- Reads against providers are not spanned (see above).
