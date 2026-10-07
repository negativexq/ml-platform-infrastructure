# Observability

Logs, metrics and traces for the control plane and the inference service, wired through
OpenTelemetry. The goal is one thing you could not do before: **open the trace of a request
and follow it into the reconciler minutes later**, down to the call that failed.

```
 client ──traceparent──▶ API ──┐                         ┌─▶ Tempo (traces)
                               ├─ OTLP/HTTP :4318 ─▶ Collector ─┤
 reconciler (other process) ───┤                         └─▶ Prometheus exporter ─▶ ServiceMonitor ─▶ Prometheus ─▶ alerts
 gateway / inference ──────────┘
```

## What you get

| Signal | Source | Notes |
| --- | --- | --- |
| **Server spans, HTTP metrics (API + gateway)** | FastAPI's native OpenTelemetry (>= 0.142) | span per request named by route template (`GET /projects/{project_id}`), `fastapi.dependencies/endpoint/serialization` child spans, `http.server.request.duration`. Probes and `/ui` are excluded. |
| **Lifecycle spans** | `observability/uow.py` | one short span per audit event (`pipeline_run.submitted`, `deployment.ready`, `rollout.rolled_back`, ...), emitted **only after the transaction commits**. A rollback reports nothing. A pass that changes nothing emits nothing. |
| **Provider spans** | `observability/providers.py` | `workflow.submit`, `serving.deploy`, `cluster.apply`, ... only for calls that *change* something. Reads (`get_status`, `observe`) are metrics only, so polling does not flood the trace of the request that started the work. |
| **SQL spans** | SQLAlchemy instrumentation on API, gateway and reconciler engines | Reconciler queries join the stored entity trace after origin lookup. SQLAlchemy is pinned to 2.0: instrumentor 0.66b0 rejects 2.1. |
| **Outbound gateway spans** | HTTPX instrumentation on the forwarding client only | Propagates W3C trace context to serving. The HTTP span ends at response headers; whole-response/stream duration remains the gateway metric. It does not separately measure DNS/connect/TTFB/token latency. |
| **Database metrics** | `observability/database.py` + timed PostgreSQL QueuePool | Acquisition, checked-out/capacity, connection creation/invalidation counts, query execution and transaction lifetime. No SQL/parameters/DSN in metric attributes. |
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
`mlp-controlplane-reconciler`, `mlp-gateway`, `ml-platform-inference`.

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
| `mlp_gateway_requests_total` | `project`, `endpoint`, `code` | exact matched gateway call count |
| `mlp_gateway_usage_requests_total` | `project`, `endpoint`, `caller`, `code` | exact per-caller rejection/error usage |
| `mlp_gateway_units_total` | `project`, `endpoint`, `caller`, `unit` | what quotas count: requests for models, tokens for LLMs |
| `mlp_gateway_tokens_total` | `project`, `endpoint`, `caller`, `direction` (`prompt` `completion`) | LLM tokens sent and generated |
| `mlp_gateway_duration_seconds` | `project`, `endpoint` | histogram, whole call including streaming |
| `http_server_request_duration_seconds_*` | `http_route`, `http_response_status_code`, ... | API traffic, from FastAPI itself |

The same metrics are read back by the control plane for the UI's **Monitor** page
(`GET /platform/health`, adapter `PrometheusPlatformTelemetry`): reconciler heartbeat and
error ratio, API traffic, 5xx ratio and p95, external system error ratio and p95, state
changes per minute, gateway traffic, errors and p95, and LLM tokens per minute. Thresholds match the alert rules. The adapter's queries are tested
against a real Prometheus (`test_prometheus_adapter.py`).

The inference service keeps its existing Prometheus metrics (`/metrics`): the M5/M10 alerts
are written on them.

## Where it lives

```
observability/otel/collector.yaml       Collector config (OTLP in; Tempo + Prometheus exporter out)
observability/otel/tempo.yaml           Tempo, single binary
observability/controlplane-alert-rules.yaml (+ .test.yaml)   10 alerts, promtool-tested
observability/dashboards/controlplane.json
k8s/observability/                      Collector, Tempo, ServiceMonitor, Grafana datasource
scripts/observability-up.sh             installs all of it next to kube-prometheus-stack
```

Alerts: `ReconcilerStalled`, `ReconcilerGone`, `ReconcileErrors`, `ProviderCallsFailing`,
`RolloutRolledBack`, `DeploymentFailed`, `ControlPlaneHighErrorRate`, `ControlPlaneHighLatency`,
`GatewayHighErrorRate`, `GatewayHighLatency` (per project and endpoint).

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
- Tempo keeps traces for 24 h on an `emptyDir`; pod replacement loses the local trace store.
  Laptop defaults are 256Mi request / 1Gi limit with querier concurrency 2.
- Reads against providers are not spanned (see above).

## Bottleneck instrumentation (2026-10-07)

The control-plane dashboard now correlates DB latency with API/gateway/reconciler CPU,
working-set RAM, throttled CPU periods, restarts and desired/available replicas. Resource
panels require kubelet/cAdvisor and kube-state-metrics scraping. Empty panels indicate a
missing source; these new queries passed PromQL syntax validation, not live cluster
series validation. DB gauges are per process; inspect `instance` rather than summing
capacity across replicas when diagnosing a saturated pool.

| Metric (Prometheus, unless marked OTel) | Measurement |
| --- | --- |
| `mlp_db_pool_acquire_duration_seconds` | Pool.connect duration: queueing + new connection + pre-ping; includes failed acquisition/timeout (`outcome=error`) |
| `mlp_db_pool_checked_out`, `mlp_db_pool_capacity` | Current connections held and configured size + overflow (default 5 + 10) |
| `mlp.db.pool.connections`, `mlp.db.pool.invalidations` (OTel) | Creation/invalidation counters; creation time is not separately measured |
| `mlp_db_query_duration_seconds` | DBAPI execution until return/error; includes server/lock time, excludes fetching rows |
| `mlp_db_transaction_duration_seconds` | Begin until commit/rollback starts; **excludes actual DBAPI commit/rollback latency**, outcome describes the attempted boundary |

Histogram `_count` supplies query/transaction counts; it is not a per-request query count.
Database metric labels are only database dialect and bounded outcome values. SQL tracing
uses the instrumentor's SQL statement attribute (no bound parameters); control trace
access and avoid literal secrets in statements. Read-only polling also emits DB spans
when tracing is enabled, so use the documented sampler for busy environments.

The chart supplies pod UID/name/namespace/node via the downward API. Resource defaults
include `service.instance.id=<pod UID>` and standard `k8s.*` attributes, with explicit
`OTEL_RESOURCE_ATTRIBUTES` taking precedence. This does not add the collector's
`k8sattributes` processor or grant it Kubernetes API access.

### PostgreSQL query profiling

For the local database chart, opt in with `postgres.profiling.enabled=true`. This adds
`shared_preload_libraries=pg_stat_statements` and `compute_query_id=on` to PostgreSQL
startup. **It restarts the database pod.** The chart's database may be MLflow's; an
external/control-plane database needs its own equivalent PostgreSQL configuration.
Preserve other preload libraries on an existing server. Managed PostgreSQL may require
provider parameter-group changes instead.

After restart, install in each target database using a DB operator credential:

```bash
# Supply the DSN from the operator's secret store; never put it in a committed file.
CP_PROFILE_DATABASE_URL="$OPERATOR_DATABASE_URL" \
  .venv/bin/python scripts/controlplane_query_profile.py --install
```

Then use a dedicated monitoring login with `pg_read_all_stats` for cross-runtime-role
query IDs (this built-in role can also read other users' query text; keep it away from
application runtime roles). Snapshots do not need `pg_read_all_settings` or extension
installation permissions:

```bash
CP_PROFILE_DATABASE_URL="$MONITORING_DATABASE_URL" \
  .venv/bin/python scripts/controlplane_query_profile.py --limit 20
```

Output contains query IDs, calls/rows, execution times in **milliseconds** and buffer
counts, never SQL text. Values are cumulative since reset, not calls/sec; take two
snapshots to calculate rates. Runtime migrations neither install extensions nor change
these grants. Continuous PostgreSQL exporter/scraping remains open.

Recorded checks: [observability evidence](evidence/live-2026-10-07/observability/README.md).
Real Collector/Tempo application export, traces and application metrics now pass live
checks. Resource panels lack their infrastructure series in this lab; final committed
image acceptance remains open. The working-tree clean-built candidate runtime/scan/SBOM
passed. Phase/runtime signals are implemented in the follow-up below. Further work:
collector drop/backpressure alerts, precise queue/commit breakdown,
cold-start and vLLM TTFT/token/GPU metrics, central log storage.

References: [OTel HTTPX client instrumentation](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/httpx/httpx.html),
[PostgreSQL pg_stat_statements](https://www.postgresql.org/docs/16/pgstatstatements.html).

## Gateway phase and runtime follow-up

Optional observers (no OTel dependency in the application layer) record
`mlp_gateway_phase_duration_seconds{phase,outcome}` for `auth`, `route`, `authorize`,
`limiter.queue`, `limiter.work`, `limiter.total`, `upstream.headers` and `stream`.
Auth/route include cache lookup and synchronous cache-miss DB work; they are not pure
CPU measurements. Limiter queue measures submission until the asyncio worker begins;
work includes DB pool acquisition, and total includes both. Existing limiter transaction
metrics remain separate. `stream` covers body iteration through completion/cancellation,
including downstream backpressure; settlement is outside that phase.

HTTPcore trace callbacks additionally measure new TCP connects (`upstream.connect`,
including DNS) and TLS (`upstream.tls`). Keep-alive reuse emits no connect sample.
`upstream.first_chunk` measures call start until first decoded body data; **not exact
network TTFB or model TTFT**. `upstream.total` ends at body completion/close and includes
streaming/backpressure. The headers phase also includes HTTP client connection-pool wait.
See [HTTPcore trace extension](https://www.encode.io/httpcore/extensions/).

Runtime metrics: `mlp_gateway_inflight` includes the whole ASGI request/stream except
probes; `mlp_gateway_limiter_workers` counts actual executing limiter workers, including
work still completing after request cancellation. The 100ms loop probe records
`mlp_gateway_event_loop_lag_seconds`; lifespan shutdown cancels it. AnyIO borrowed/capacity
metrics describe FastAPI/AnyIO workers, **not** the asyncio executor used by the limiter.
There is no fabricated executor saturation ratio; limiter queue/work/worker counts expose
that path. DB pool idle connections are now reported as `mlp_db_pool_idle`.

These phases overlap (parent and child), so their p95s must not be added. No endpoint,
caller, body, hostname or trace event details are added to phase/runtime metric labels.
Local tests cover refusal, worker balance, blocking-loop lag, inflight balance and real
keep-alive streaming. Cluster exports and short 1 × 1 CPU rejection-only load runs are
recorded in the live evidence; sustained successful inference remains open.

The next performance tool is [Go loadgen](../tools/loadgen/README.md); the staged adoption
plan is [go-runtime-plan.md](go-runtime-plan.md).

## Tempo memory and historical query acceptance

The 2026-10-07 historical lookup caused a recorded OOM at 512MiB. Laptop settings now
use memory request 256Mi / limit 1Gi and `querier.max_concurrent_queries: 2`. The actual
Tempo 2.7.1 binary validates the configuration. A preserved completed backend block
was restored after pod replacement; 100 historical reads with two concurrent clients
and five new API/SQL traces passed. Peak memory was 344MiB and no additional OOM/restart
occurred during the check. The deliberate restore-discovery restart is recorded separately.
This is a short acceptance of the old dataset, not sustained production capacity or
durable trace storage. See [preservation and memory evidence](evidence/live-2026-10-07/observability/tempo-tuning.md).

## Gateway CPU profile

A disposable Yappi CPU-clock profile identified OTel and the PostgreSQL client stack
as the largest attributed groups. It covered 81% of thread CPU and slowed throughput
about 3.3 times, so category percentages are investigative evidence rather than exact
normal-runtime costs. [Profile, provenance and limitations](evidence/live-2026-10-07/observability/gateway-cpu-profile.md).

An [unprofiled OTel SDK on/off comparison](evidence/live-2026-10-07/observability/gateway-otel-ab.md)
completed 30,000 429-only requests: mean 424 RPS on versus offered-load-capped 500 off.
Pod CPU/request fell from 2.352ms to 0.938ms (60.1%). This establishes combined
instrumentation overhead, not metrics-versus-traces attribution or successful serving capacity.
Production observability/settings remain active.

The [OTel-disabled capacity probe](evidence/live-2026-10-07/observability/gateway-otel-off-capacity.md)
completed 47,500 valid 429 responses at 750/1,000/1,500 offered RPS. A 1-CPU pod
plateaus around 600–700 completed RPS in short tests (1,500 repeats: 691 and 607),
with increasing client queue delay. This is neither a sustained capacity guarantee nor
successful inference evidence; production telemetry remains active.

## Gateway telemetry profiles

Gateway defaults to `CP_GATEWAY_OBSERVABILITY_PROFILE=normal`; the Helm value is
`gateway.observabilityProfile`. API/reconciler defaults are unchanged.

| Setting | normal | diagnostic | acceptance |
|---|---|---|---|
| Metrics | enabled when its OTLP endpoint is configured | same | same |
| Root trace sampling | 1% | 100% | 1% |
| Generic SQLAlchemy spans | off | on | off |
| Custom DB/limiter/phase metrics | on | on | on |
| Latency histogram observations | independent random 20% | 100% | 100% |
| Export interval default | 30 seconds | 1 second | 1 second |
| Native HTTP metrics / operation spans | off / off | on / on | off / off |
| Operational request caller label | absent | absent | absent |
| OTLP logs | off | off | off |

`mlp_gateway_duration_seconds` remains the authoritative endpoint latency histogram
(including streaming) for the existing UI/alerts. Normal mode disables duplicate native
HTTP metrics; native HTTP server traces remain sampled. Diagnostic mode deliberately
adds operation spans/native metrics for investigation. HTTP errors/red responses are
counted exactly, not selectively excluded after their response status is known.
Root sampling does not override a sampled incoming parent; a trusted sampled parent
can still produce a trace. There is no special full-trace policy for 429 responses.

`mlp_gateway_requests_total` now has project/endpoint/code. Caller-specific red/error
usage moves to `mlp_gateway_usage_requests_total`; units and prompt/completion tokens
retain caller attribution. The UI reads the new usage series with a fallback to older
caller-labelled request series during upgrade. These exact counters and inflight/worker
balance are never latency-sampled. DB acquire/query errors have an exact
`mlp_db_errors_total{db_system,operation}` counter, independent of sampled histograms.
Caller usage series can still have high cardinality; removing caller from operational
metrics does not make usage cardinality unboundedly safe.

Sampled histogram `_count`/`_sum` describe sampled observations, **not all requests**.
Use the exact request counter for RPS/errors/budgets; do not derive traffic volume from
histogram counts. Quantiles and means are estimates, especially at low traffic. Each
observation is sampled independently, so phase sums do not form an exact partition.
Raw histogram observation counters are not upscaled by the sample rate. The event-loop
lag histogram is a periodic runtime probe, not request-sampled.

Overrides (boolean values must be `true`/`false`, rates within 0–1, interval positive):

```text
CP_GATEWAY_SQL_TRACING
CP_GATEWAY_TRACE_SAMPLE_RATE
CP_GATEWAY_LATENCY_SAMPLE_RATE
CP_GATEWAY_REQUEST_CALLER_LABEL
CP_GATEWAY_HTTP_METRICS
CP_GATEWAY_HTTP_OPERATION_SPANS
CP_GATEWAY_METRIC_EXPORT_INTERVAL_MS
```

Explicit gateway trace-rate overrides use a parent-based ratio sampler. Otherwise a
standard `OTEL_TRACES_SAMPLER` setting is honored in normal/acceptance; diagnostic
uses its profile rate. Explicit gateway export interval wins over standard
`OTEL_METRIC_EXPORT_INTERVAL`, which otherwise wins over profile defaults. An existing
short benchmark interval does not silently become 30 seconds.

Trace/metric providers are independent: use `OTEL_TRACES_EXPORTER=none` for metrics
only, or `OTEL_METRICS_EXPORTER=none` for traces only. A signal-specific OTLP endpoint
alone enables only that signal; a common endpoint enables both unless disabled.
`OTEL_SDK_DISABLED=true` remains the overall kill switch. Custom DB metrics are attached
when metrics are enabled, independently of generic SQL tracing. Existing API/reconciler
SQL tracing remains enabled when their trace provider is enabled.

Use diagnostic mode briefly, then return to normal. Sampling does not replace retention,
collector/backpressure monitoring, or real cluster resource metrics. This change does
not add an exact PostgreSQL lock-wait metric; DB execute timing includes lock/network
waits, and the acceptance SQL monitor counts cluster-wide waiters.


The [live profile A/B](evidence/live-2026-10-07/observability/gateway-hotpath-ab.md)
retains exact counters and sampled server traces: normal 492–500 RPS with 43.8% lower
mean timed-window CPU versus full instrumentation, 50,000 valid 429 responses. See
its complete per-run latency/CPU data and the short-window/export-cost limitations.

## Normal-profile soak evidence

The subsequent [normal-profile soak](evidence/live-2026-10-07/observability/gateway-normal-soak.md)
ran two five-minute 500 RPS windows across ten observed regular exports each. Mean CPU
was 0.53–0.56 core with stable sampled memory/threads. Both strict zero-error gates
failed: 299,997/300,000 attempts returned 429; three transport failures remain, including
two immediate header-phase connection resets. The connection follow-up below narrows the failure mechanism; longer
successful-upstream/streaming load and final clean release acceptance remain open.

The subsequent [keep-alive A/B/C](evidence/live-2026-10-07/observability/gateway-connection-ab.md)
reproduced a reset on a reused socket idle for 4,999 ms near Uvicorn's 5-second boundary.
Client idle timeout 2 seconds retained reuse and passed 150,000/150,000 rejection requests
at 500 RPS with no transport failures/drops and 0.60 mean CPU core. Keep-alive off failed
at 472 RPS, 0.98 core, 856 network errors and 2,535 queue drops. The recommended acceptance
client setting is idle 2 seconds; generic CLI defaults and installed deployments remain
unchanged. Successful upstream/streaming, longer runs and final release acceptance remain open.

The isolated [Go gateway PoC comparison](evidence/live-2026-10-07/observability/gateway-runtime-ab.md)
now records normal OTel with the selected B transport (client idle 2 seconds). At 500
rejection RPS, Python used 0.62 CPU core versus Go 0.18 (70.7% less CPU/request), with
HTTP p95 251 versus 1.48 ms. Go completed 1,000/1,500 offered RPS without errors/drops
at 0.33/0.48 core; Python completed 554/518 RPS and dropped offered traffic near one
core. Real function forwarding/refusal smokes, shared PostgreSQL correctness and Tempo
server→upstream trace/counter checks passed. These are six 60-second rejection windows,
not successful inference capacity. OIDC/LLM/stream-timeout contract work and the
subsequent migration are recorded separately below.
The gateway has since migrated to Go; API/reconciler remain Python.
See [migration and final-artifact checks](evidence/live-2026-10-07/observability/gateway-go-migration.md).
Go maximum capacity and successful inference throughput remain unmeasured.
