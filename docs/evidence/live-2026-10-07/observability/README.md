# Bottleneck observability follow-up — 2026-10-07

Source: `cb6e540` plus the observability working-tree changes. Initial checks were local code and isolated PostgreSQL evidence. The live follow-up
below now records Collector/Tempo export and a paired Kubernetes generator comparison.
Existing CPU/limiter results remain in their own reports.

## Changes and verified scope

- Gateway native FastAPI telemetry shares API exclusions/exporter ownership; the server
  span and HTTPX client span share the incoming trace, and the client injects W3C
  `traceparent` into the backend request. Only the forwarding client is instrumented.
  `/healthz` and `/readyz` emit no spans. Existing streaming/refund/redacted outage tests pass.
- Reconciler configures telemetry with its engine and attaches the stored entity origin
  as the current OTel context. Its real SQLAlchemy SELECT spans join that trace; context
  is detached after the entity. Origin-lookup queries precede that attachment.
- The test exposed an additional defect: SQLAlchemy 2.1.2 is rejected by instrumentor
  0.66b0. The control-plane lock now uses supported SQLAlchemy **2.0.54**, adding its
  Linux greenlet dependency. All other existing pins are preserved; HTTPX instrumentor
  and util-http are pinned to 0.66b0. The lock was regenerated with uv 0.12.23.
- Pool acquisition observes successful/error paths, including a deliberately exhausted
  one-connection pool. Query errors are timed; transaction attempts distinguish
  commit/rollback. Disposal retains the pool observer, gauges read the replacement pool,
  and rollback after connection invalidation is not disrupted by telemetry.
- Downward-API pod UID/name/namespace/node become resource defaults. Explicit OTel
  attributes win; tests verify this. No collector Kubernetes RBAC was added.
- Nine control-plane dashboard panels cover pool acquisition/usage, query/transaction
  latency, CPU, RAM, throttling, restarts and desired/available replicas. All **20**
  dashboard PromQL expressions passed `promtool check rules`; this is syntax proof,
  **not proof of populated cluster series**.
- Both charts lint/render. Opt-in `postgres.profiling.enabled=true` renders PostgreSQL
  preload/query-ID flags. Existing PVCs need the separate operator extension command.

## Real PostgreSQL query profiling

A temporary `postgres:16.4-alpine` container used tmpfs PGDATA and a loopback-only host
port. Extension preload/install passed. A dedicated monitoring login with
`pg_read_all_stats` obtained query-ID snapshots without `pg_read_all_settings`.
The fixture executed the same normalized query **11 times**, and the report counted
**11**. JSON contains statistics/IDs, not SQL text or credentials.

[Recorded result](postgres-profile.json) includes the image ID and observed counters.
The test container was stopped and removed; no cluster application DB, runtime grants
or Helm release changed. It is not continuous SQL metrics scraping.

## Checks

Final full suite: `TZ=UTC .venv/bin/pytest controlplane/tests` — **754 passed,
100 skipped**, one Starlette TestClient deprecation warning, 100.72 seconds. Skips
do not establish browser/optional external acceptance.

Targeted observability/gateway/architecture tests: **59 passed**. API project tests under
`TZ=UTC`: **20 passed**. Ruff, scoped mypy (18 sources, including the new tests/profiler),
Helm lint and dashboard PromQL syntax validation passed.

The initial full suite reported 750 passed / 100 skipped / 4 failed. Two failures were
local PostgreSQL timezone-dependent JSON comparisons; UTC fixes those. One caught the
new telemetry helper import crossing the composition boundary; the helper was moved
outside the observability package. The other exposed the existing gateway SQLAlchemy
import from `cb6e540`; the composition root now supplies the datastore exception type,
retaining the redacted 503 response without that layer dependency.

Broad mypy also has existing failures in revision/CPU/load tests and the limiter matrix
(module import fallback/types); modified production sources and observability tests pass
scoped checking. The shared development venv retains pre-existing MLflow 3.15.0
cryptography/pandas requirement conflicts. It is not a clean image dependency gate.

## Measurement limits and next live checks

Acquisition includes queue + connect + pre-ping. Query duration excludes result fetching.
Transaction lifetime ends **before DBAPI commit/rollback**, and its outcome is the
attempted boundary. HTTPX client spans stop at response headers; stream duration remains
the gateway metric. None of these establishes pure pool wait, commit latency, per-request
query counts or DNS/connect/TTFB/LLM token timing.

The initial package left phase/runtime instrumentation open. The follow-up below implements
those signals locally; the live follow-up below records subsequent export acceptance.

Collector export, trace inspection and populated application metrics subsequently passed
as recorded below. Resource panels remain unpopulated; enable profiling on the actual
control-plane DB in a planned restart. Then add continuous PostgreSQL scraping, collector drop/backpressure alerts and
GPU/vLLM signals. The paired follow-up below reruns the low-budget fixture with this instrumentation.

## Phase/runtime and Go loadgen follow-up

Latest full Python suite: **758 passed, 100 skipped**, 106.84 seconds under `TZ=UTC`.
Scoped changed-source mypy and Ruff pass. The control-plane dashboard now has **27**
PromQL expressions, all syntax-checked; no new populated cluster panels are claimed.

New tests verify auth refusal phases, limiter worker balance, full-stream inflight balance,
a deliberately blocked event loop, lifespan probe shutdown and real HTTP keep-alive:
connect is measured once for two calls, while first chunk and total stream are measured
for both. Existing streaming/refund/outage and architecture tests remain passing.

Go loadgen race/vet tests pass for round-robin 200/429, visible overload and partial-stream
timeout. Native macOS ARM64 and Linux ARM64/AMD64 binaries compile. The first local smoke
returned nonzero; its JSON was not retained. The scheduler was subsequently changed from
per-call waiting to reservations against one epoch, preventing late wakeups from shifting
later planned slots. The repeated 500 RPS / 2s / 32-worker / two-loopback-target smoke
recorded **1000 planned/offered/completed**, no drop/unscheduled/transport error,
~499.97 completed RPS, ~0.13 process CPU cores and ~17.2 MiB peak RSS.
[Recorded JSON](go-loadgen-smoke.json) retains timings/status/resource scope.

This is a local HTTP fixture, not a Python/Go generator A/B test, Kubernetes load test,
image build/scan/SBOM or actual gateway performance result. The fixed scheduler still
reports delayed/unissued slots rather than claiming that Go removes client bottlenecks.
[Go adoption plan](../../../go-runtime-plan.md) tracks the remaining stage gates.

## Live Collector/Tempo and paired generator acceptance

Single-node ARM64 kind / Kubernetes 1.32. API/gateway/reconciler ran the candidate
[overlay image](acceptance-image.json), with a real Collector 0.116.1 and Tempo 2.7.1.
Prometheus scrape configuration was reloaded without restarting its ephemeral-data pod.
Tracing sampled 1% of ordinary benchmark requests; warm-up explicitly requested sampling.

- [API trace](api-trace-summary.json): native HTTP server and SQL spans.
- [Gateway trace](gateway-trace-summary.json): server, SQL and HTTPX outbound spans in
  the same incoming trace across four warmed pods; pod/namespace/node/instance identity.
  This does not establish KServe/model-internal tracing.
- [Reconciler trace](reconciler-trace-summary.json): live SQL export and pod attribution.
  Stored-origin lifecycle correlation remains separately tested in unit integration tests.
- [Live metric inventory](live-metrics.json): 46 application metric-family/job combinations,
  including acquisition/query/transaction, gateway phases/inflight and runtime metrics.
  The lab has no cAdvisor/kube-state-metrics series: CPU/RAM/throttling/restart/replica
  dashboard population remains open. Collector alerts/continuous SQL profiling remain open.

[Raw paired comparison](loadgen-comparison.json): same pod image, 2 CPU / 384MiB generator
limit, 200 connections, 15s full-request timeout, 10s / 500 offered RPS, 600-units/minute
endpoint and caller budgets. Gateway pods each have a 1 CPU limit. Go queue capacity
5,000 matches Python's bounded 5,000 planned requests. Generator order alternates; this
is one pair per configuration, not a repeated statistically controlled performance study.

| Replicas | Generator | Completed RPS, including drain | Request p95 ms | Client queue p95 ms | CPU seconds | Peak RSS MiB | Limiter p95 ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Go | 363.5 | 1429.7 | 3459.7 | 0.871 | 76.4 | 81.0 |
| 1 | Python | 318.6 | 1922.0 | 5289.6 | 1.102 | 78.0 | 86.3 |
| 2 | Python | 499.8 | 395.5 | 63.5 | 1.105 | 78.9 | 69.8 |
| 2 | Go | 500.0 | 523.4 | 400.3 | 1.048 | 79.0 | 87.0 |
| 4 | Go | 499.9 | 262.0 | 0.012 | 1.056 | 80.4 | 117.2 |
| 4 | Python | 499.7 | 412.5 | 21.3 | 1.276 | 80.5 | 139.5 |

All six samples completed exactly 5,000 requests, with only 200/429 responses and
5,000 limiter observations each. No Go drops/unissued/transport failures; Python status
counts also total 5,000. Every sample respects the shared budget upper bound. Single-pod
Go completed only 3,091 requests during the offered 10s window; it drained afterward.
Two/four pods completed approximately 500 RPS with both clients. Mostly-429 traffic
**does not prove 500 successful inference RPS**. Results preserve the single-pod failure.

Go's process CPU was lower in these pairs, but memory was similar and latency outcomes
were mixed. The lab load and enabled telemetry differ from the earlier limiter run;
no telemetry-overhead percentage or gateway rewrite justification is established.
Detailed profiling and repeated runs remain the next performance gate.

Fixture limits were restored, the disposable API key revoked, job/configmap/token secret
removed and gateway returned to two replicas after collecting evidence.

## Clean-built candidate image runtime/scan/SBOM

The actual `docker/controlplane/Dockerfile` built from the updated locked dependencies;
`pip check` passed. Candidate digest:
`sha256:7271ee509006b80d56c687ea4613b60a5fb28a1c2d887bef878027af78d32072`.
This follows the overlay benchmark; the paired load test was not rerun on the clean image.

[Runtime report](candidate-image/report.json) passes all four command paths, migration to
0021, API/gateway health/readiness, DB outage → readiness failure → recovery, reconciler
configuration/empty-database passes, SPDX SBOM and Trivy gates (fixable HIGH/CRITICAL,
plus secret scan). Reconciler smoke uses a deliberately unreachable Kubernetes fixture,
not a live provider acceptance. [SBOM](candidate-image/sbom.spdx.json),
[vulnerability scan](candidate-image/trivy.json) and [secret scan](candidate-image/trivy-secrets.json)
are retained.

The repository release checker still requires clean committed source and an exact revision
label. A separate [candidate runner](candidate-image/candidate-check.py) ran the same
runtime/scanning body with explicit working-tree provenance; its report records
`source_clean=false` and `release_eligible=false`. [Provenance](candidate-image/provenance.json)
records the runner hash and candidate digest. **The final committed release gate remains
open.** No commit or push was made during this acceptance.

[Clean image rollout](clean-image-rollout.json): all three deployments reached 2/2 ready
on the clean candidate digest; API readiness returned 200. Collector/Tempo remain enabled
in the acceptance lab. Task-owned port forwards were stopped after evidence collection.

## Single gateway bottleneck inspection

[Historical histogram/gauge evidence](single-gateway-bottleneck.json) brackets the first
Go sample, excluding the next Python sample: before 1791332668, after 1791332688 UTC
epoch seconds. Limiter phase counts are exactly 5,000; upstream counts are exactly 737,
matching the 200 response count. DB observations also include readiness/background calls.

| Signal | Mean | Interpretation |
| --- | --- | --- |
| limiter queue | 228.6 ms | Waiting for asyncio executor worker |
| limiter work | 25.7 ms | Actual limiter operation, including DB acquisition/transaction |
| limiter total | 289.1 ms | Queue + work + scheduling/observation overhead; phases overlap |
| DB acquisition | 2.16 ms | Includes connect/pre-ping; not pure pool wait |
| DB query execution | 7.05 ms | Per statement, not per request; includes database/driver/network waits |
| event-loop lag | 16.2 ms | Runtime scheduling delay, histogram p95 approximately 85 ms |
| upstream headers | 1299.8 ms | Accepted 737 requests only; includes client/upstream queue/network/backend |

Sampled inflight reached 200; limiter workers and checked-out DB connections plateaued
at 12. The default DB pool capacity is 15. A separate process in the current Python 3.12
pod sees eight CPUs and a 12-worker default asyncio executor despite a 1 CPU pod limit.
No explicit gateway executor override exists. The AnyIO threadpool gauge does not describe
this executor. Most measured limiter elapsed time (~79% of summed limiter-total duration)
is waiting to start a worker; DB acquisition is comparatively small. SQL operation latency
still governs worker turnover. This does not isolate PostgreSQL row-lock wait from other
query execution costs or establish actual gateway CPU saturation/throttling.

The admission fixture starts with a full 600-request budget: accepted requests take much
longer than later 429s and occupy client/gateway concurrency while the executor queues.
Thus 363.5 completed RPS includes 13.75s of offering + drain; only 309.1 RPS completed
within the 10s offered window. It is not a steady-state 364 RPS capacity ceiling.

Next controlled probes: pre-exhaust the disposable budget for a rejection-only run;
compare 12/24 worker configurations at unchanged DB pool size, then matched pool sizes;
record gateway CPU/throttling from cgroups alongside loop lag and DB lock waits. Preserve
budgets and fail-closed behavior. Increasing workers without measurements can shift the
queue into the DB pool and hot-row locks. These probes subsequently ran; see the [worker experiment](worker-experiment.md).

## Worker/pool experiment follow-up

Go generated 35,000 rejection-only requests against the Python gateway. 12/15 completed
485–487 RPS without 503; 24/15 fell to 466–472 RPS with 18 DB-acquisition failures/503s
per sample; 24/30 removed those failures but reached only 482 RPS. CPU/quota and SQL/lock
contention remain relevant; increasing workers alone is not a fix.
[Full experiment and retained failures](worker-experiment.md).

## Existing telemetry distribution

[Latency distribution](latency-distribution.md) partitions the original 490 ms average:
47% executor queue, 39% upstream headers, 5% limiter work and the remaining measured/
residual timing. These are wall-time shares, not CPU percentages. Historical trace
inspection also exposed a Tempo OOM at 512MiB. The subsequent
[1Gi/limited-concurrency replay](tempo-tuning.md) passed 100 historical reads and five new
traces without another restart; larger/sustained query capacity remains open. No rewrite decision is established by these duration metrics.

## Tempo tuning and replay

Memory request/limit are now 256Mi/1Gi and querier concurrency is 2. The preserved
completed backend block was restored; 100 historical reads and five new API/SQL traces
passed, peak 344MiB, zero additional OOM/restarts during the check.
[Preservation, query and memory evidence](tempo-tuning.md).

## CPU profiling follow-up

The [single-gateway CPU profile](gateway-cpu-profile.md) completed 15,000 rejection-only
requests. OTel accounts for 35.2% and psycopg/SQLAlchemy 40.6% of attributed exclusive
CPU in the profiled run. Coverage is 81%; profiling itself slowed throughput about
3.3 times. Unprofiled brackets reached 473–475 RPS. A controlled OTel on/off test is
needed to quantify normal-runtime overhead before any rewrite.

The [unprofiled OTel on/off follow-up](gateway-otel-ab.md) now confirms material
instrumentation cost: CPU/request 2.352ms on vs 0.938ms off, 30,000 valid 429 responses.
Mean completed RPS was 424 on vs 500 off (off capped by offered load). Production
telemetry remains enabled; metrics/traces attribution and successful inference load remain open.

[Higher offered load with SDK disabled](gateway-otel-off-capacity.md) completed 47,500
429 requests without errors. At 1,500 offered RPS, repeats achieved 691 and 607 completed
RPS with about one CPU core and growing client queue delay: a short 600–700 RPS plateau.

## Enabled telemetry optimization

The [hot-path profile experiment](gateway-hotpath-ab.md) now retains observability:
normal repeats 492/500/500 RPS, full references 477/489; mean timed CPU 5.73s vs 10.21s
(43.8% lower). All 50,000 requests were 429 with exact request-counter integrity.
Real Tempo confirms retained HTTP traces and absent SQL spans in normal mode. Custom
DB/limiter/phase latency metrics remain; normal histograms use random 20% observations.
This is not a sustained or successful-inference gate. Forced flushes occur outside the
measurement and the ten-second window does not amortize 30-second export costs.

## Normal-profile five-minute soaks

The subsequent [normal-profile soak](gateway-normal-soak.md)
ran two five-minute 500 RPS windows across ten observed regular exports each. Mean CPU
was 0.53–0.56 core with stable sampled memory/threads. Both strict zero-error gates
failed: 299,997/300,000 attempts returned 429; three transport failures remain, including
two immediate header-phase connection resets. Socket/keep-alive investigation, longer
successful-upstream/streaming load and final clean release acceptance remain open.

## Keep-alive boundary experiment

The subsequent [keep-alive A/B/C](gateway-connection-ab.md)
reproduced a reset on a reused socket idle for 4,999 ms near Uvicorn's 5-second boundary.
Client idle timeout 2 seconds retained reuse and passed 150,000/150,000 rejection requests
at 500 RPS with no transport failures/drops and 0.60 mean CPU core. Keep-alive off failed
at 472 RPS, 0.98 core, 856 network errors and 2,535 queue drops. The recommended acceptance
client setting is idle 2 seconds; generic CLI defaults and installed deployments remain
unchanged. Successful upstream/streaming, longer runs and final release acceptance remain open.

## Python/Go gateway comparison

The isolated [Go gateway PoC comparison](gateway-runtime-ab.md)
now records normal OTel with the selected B transport (client idle 2 seconds). At 500
rejection RPS, Python used 0.62 CPU core versus Go 0.18 (70.7% less CPU/request), with
HTTP p95 251 versus 1.48 ms. Go completed 1,000/1,500 offered RPS without errors/drops
at 0.33/0.48 core; Python completed 554/518 RPS and dropped offered traffic near one
core. Real function forwarding/refusal smokes, shared PostgreSQL correctness and Tempo
server→upstream trace/counter checks passed. These are six 60-second rejection windows,
not successful inference capacity or complete OIDC/LLM/stream-timeout migration parity.
The installed gateway and reconciler remain Python; Go maximum capacity is unmeasured.


Go gateway has subsequently migrated into the two-replica lab deployment. See the
[Go migration report](gateway-go-migration.md), final source-image scan/SBOM/provenance,
normal-profile soak, rolling function probes and basic DB outage/recovery. The earlier
PoC comparison remains historical evidence with its original dependency versions and scope.
