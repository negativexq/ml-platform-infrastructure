# Current platform status

Updated 2026-10-08. Initial code review used `974a7b0`; subsequent live checks used
the commits recorded in [live evidence](evidence/live-2026-10-06/README.md), with the latest
image gate at `b7a3420`. Historical counts retain their original scope.

## Implemented

- Scheduling P0 adds PostgreSQL-backed Job/Pipeline schedules, transactional execution/run
  creation, bounded queues, concurrency scopes, timezone/DST and UI history. Schema `0022`.
  See the [contract](scheduling.md) for policy boundaries and validation scope.

- Run Parameters adds immutable Job/Pipeline schemas, typed validation/defaults,
  persisted execution snapshots, retry/idempotency and scheduled date/time bindings.
  Head `0023`; local acceptance revision 31 and all 12 image gates passed.
  [Contract](run-parameters.md) and [validation scope](evidence/live-2026-10-08/run-parameters/README.md).

- Data Catalog adds immutable S3/MinIO connections backed by project SecretRefs and
  CSV/Parquet dataset versions with integrity identity, column schema and producer lineage.
  Head `0024`; backend 779 passed/13 skipped, all 12 image checks and live metadata API
  checks passed on local acceptance revision 33. Object verification belongs to consumers.
  [Contract](data-catalog.md) and [scope](evidence/live-2026-10-08/data-catalog/README.md).

- Managed Batch Inference adds pinned classic model + CSV/Parquet dataset definitions,
  bounded S3 verification/prediction, conditional output publication and atomic catalog lineage.
  Job retry/cancel, pipeline steps and recurring schedules reuse existing orchestration.
  Head `0025`; backend 816 passed/13 skipped, all 12 image gates and real MinIO/MLflow/Argo
  job, retry, pipeline and schedule passed on local acceptance revision 36.
  [Contract](batch-inference.md) and [evidence](evidence/live-2026-10-08/batch-inference/README.md).
  The Batch page and run output panels link to catalog versions.

- Data Management UI adds global/project Connections and Datasets, typed registration,
  immutable version history, schema/integrity details and recorded batch/model/run lineage.
  Backend 824 passed/13 skipped, frontend 22 tests, focused browser flows and all 12 final
  image gates passed. Local acceptance revision 38 verifies real MinIO predictions and
  the deployed lineage/navigation. Telemetry audit delegation is covered by regression tests.
  [Contract](data-catalog.md) and [evidence](evidence/live-2026-10-08/data-management-ui/README.md).

- Bottleneck telemetry now wires gateway native HTTP/outbound HTTPX spans and reconciler
  SQL tracing, including stored-origin context for DB spans. SQLAlchemy 2.0 pin fixes the
  instrumentor incompatibility with 2.1. DB acquisition/query/transaction-lifetime metrics,
  per-pod identity and control-plane resource dashboard panels are implemented. Operator
  `pg_stat_statements` profiling passed on isolated local PostgreSQL. Real Collector/Tempo
  export, native HTTP/SQL/outbound traces and application metrics now pass live checks.
  Resource dashboard population, final committed image gate and continuous PostgreSQL
  scraping remain open.
  Full local suite: 758 passed / 100 skipped under UTC; modified-source type checks and
  both Helm charts passed. The clean-built working-tree candidate also passed four-command
  runtime, DB readiness recovery, Trivy and SBOM checks; it is not a committed release gate.
  See [live acceptance](evidence/live-2026-10-07/observability/README.md).
  See [observability scope](observability.md#bottleneck-instrumentation-2026-10-07).

- API Secret CRUD and workload/log reads use owned-project RoleBindings. Reconciler writes
  have fail-closed native admission policies for project ownership and exact RBAC shapes.
- Chart **0.3.1**, Kubernetes **1.30+**; restricted PSA enforcement is pinned to `v1.30`.
  Training uses `mlp-training`; serving uses token-disabled `mlp-serving` or revision-scoped
  storage accounts. Training images preserve their numeric non-root image USER.
- API/gateway/reconciler default to two replicas with PDBs and pod spreading. Reconcilers
  use Lease election, bounded Kubernetes transport and main-loop progress watchdog;
  classic-model alias passes heartbeat between models.
- Separate migration/API/reconciler/gateway database credentials and runtime grants;
  migration advisory locking and expand/contract release contract. Schema head **0022**:
  `0020` protects append-only audit rows; `0021` enforces active rollout version uniqueness;
  `0022` adds scheduling intent and execution records.
- Stable OIDC issuer/subject identity, shorter bounded browser sessions and no ID token in
  cookies. Username-only grants/cookies fail closed and require verified re-grant.
- Concurrent membership changes preserve the last admin; model/endpoint field updates
  have separate ownership. Pipeline/run replay includes execution/lineage fingerprints.
  Deploy revalidates locked versions and reuses revisions after concurrent lookup.
- Manual promotion of an actively reserved rollout version returns 409. Failed/rejected
  candidates recover toward stable serving; candidate-only rollback restored real traffic
  in the ARM64 lab.
- Shared PostgreSQL inference budgets, maximum-replica GPU admission, SHA-pinned Hub and
  function images, automatic lineage-scoped model discovery, deployment deletion,
  optional workflow retention and owned storage-account cleanup.
- Project Secret API/UI, classic S3/MinIO storage-initializer authentication and manual
  image/SBOM/scan, admission, CPU, limiter and recovery gates are prepared.

## Recorded verification

The latest recorded lightweight suite is **419 passed, 5 skipped, 217 deselected**;
Ruff and mypy (**183 source files**) passed. Targeted model/promotion/rollout suites recorded
**93 passed, 2 skipped**, including real PostgreSQL lock-order tests. Earlier isolated
PostgreSQL tests covered grants/audit protection and identity/concurrency invariants.
See [concurrency-identity-audit.md](concurrency-identity-audit.md) and
[security-hardening.md](security-hardening.md) for exact scopes. These historical counts were not rerun in full. The new live batch separately passed
24 project networking/Secret tests and 16 bootstrap/release tests.

On isolated single-node `kind-mlp-acceptance` (Kubernetes 1.32.0), the control-plane image
passed all nine runtime/scan/SBOM checks, including clean migration through 0021. Four
admission policies passed type checking and nine real server dry runs; 24 API namespace
RBAC checks and restricted-PSA checks passed. An enforcing network-policy engine denied
cross-project traffic while permitted ingress and DNS worked. Lease takeover took 31.44s;
the reconciler PDB rejected the second concurrent eviction. API/gateway rolling restarts
passed 200/200 probes after adding a 10s preStop drain. This does not establish node-loss,
hung-leader, strict-egress, GPU or identity-provider acceptance.
See [live evidence](evidence/live-2026-10-06/README.md).

## Required before deployment acceptance

1. Review existing duplicate active rollouts and inconsistent manually promoted canaries
   before migration `0021`; repair with trusted operators. Migrate through `0021`, reapply
   runtime grants and provision four independent database Secrets. Re-grant verified stable
   identities; replace legacy training definitions and old shared-SA pods.
2. Repeat the passing control-plane image gate for the release artifact/architecture. The
   isolated cluster uses separated runtime credentials with role enforcement; training,
   serving and storage images still need their own release scans. See [installation.md](installation.md).
3. Extend the passing admission/PSA, ingress isolation, election/PDB and rolling-restart
   checks to serving sidecars, strict egress, workload secret/token boundaries, hung-leader
   and multi-node drain/loss drills.
4. The full seven-phase CPU lifecycle passed on the single-node ARM64 lab: project/RBAC,
   training/discovery/evaluation, serving/gateway, healthy canary, credential rotation,
   same-revision drift repair and zero-pod reactivation (1.25s request). Candidate-only
   503s separately triggered rollback; stable metrics remained healthy, candidate was
   rejected and 30 restored requests returned 200. Storage accounts were retained across
   canary and scoped cleanup preserved foreign/other-deployment accounts. These are dirty
   acceptance artifacts with local auth none, not production/OIDC/multi-node proof.
   KServe zero-delay/empty-list defaulting fixes are deployed; harness fixes retain
   metric samples during traffic and wait for matching backend apply identity.
   The five serving/initializer HIGH findings are resolved in new ARM64 acceptance
   images: zero fixable HIGH/CRITICAL, SBOMs and native inference passed; real private S3
   loading/gateway also passed. Final clean release/target-architecture reruns remain open.
   See [dependency remediation](serving-image-security.md).
   Shared-time Prometheus queries passed the deployed-image rollback rerun (candidate
   error rate exactly 1.0). Forced Secret deletion also passed in an isolated project:
   running value retained, future startup blocked, restored Secret recovered with a new
   boot/value. Private registry, cleanup conflict/outage retry and broader private
   initializer provider paths remain separate gates.
   See [ARM64 evidence](evidence/live-2026-10-06/serving-arm64/README.md).
5. Shared-limiter load measured 1/2/4 distinct gateway targets. The original 500 RPS
   port-forward gate failed; the new in-cluster aiohttp/uvloop comparison passes 500
   offered RPS at two/four replicas (~493/495 completed RPS, no 5xx/transport errors or
   client backlog). Isolated limiter p95 improved from 96→30ms and 187→4.7ms; the
   two-bucket transaction now uses three statements rather than nine. The full matrix
   remains failed at one replica; a 2 CPU probe still queues. Sustained and mostly-admitted
   inference load remain open. See [load report](limiter-performance.md).
   Basic real PostgreSQL outage/recovery passed across four gateways, including expired
   auth/route caches; those SQL failures now return redacted 503 rather than 500. The lab
   DB now has a Bound PVC; the initial emptyDir fixture lost its historical rows and was
   rebuilt, not restored from backup. Sustained outage/thread-growth remains open.
   Run backup/restore with roles/triggers,
   recovered services and RPO/RTO. GPU/immutable HF, ingress/TLS and AWS isolation remain
   separate resource-dependent gates. Commands are in [acceptance.md](acceptance.md).

## Remaining implementation work

- Persist a stable audit `actor_subject` alongside display username; authorization identity
  is fixed, but old/new audit actor display names can still be ambiguous.
- Safe bounded rate-bucket GC, preserving token debt, in-flight and concurrent reservations.
- Durable Argo log archives and streaming; operational function cold-start metrics.
- Model-specific exact token accounting, fuller platform disaster recovery and AWS
  deployment completion. Private/gated initializer paths beyond classic S3 need contracts.

CI remains intentionally manual/disabled; it is not a new defect. Keep deployment evidence
separate from implementation status. This page summarizes the roadmap; detailed findings
and procedures remain in [technical-review.md](technical-review.md),
[operations.md](operations.md) and [recovery.md](recovery.md).

## Go and runtime observability follow-up

Gateway phase/loop-lag/inflight/limiter-worker and AnyIO pool metrics are implemented.
Go loadgen has bounded visible queueing, local race/vet tests and a 500 RPS HTTP-fixture
smoke. Real Collector/Tempo application export and the paired 1/2/4-pod 500 offered
RPS comparison passed request/budget integrity; one pod still queues. Resource dashboard
population and final committed release rerun remain open. Go S3 initializer
is next; API/reconciler stay Python, the gateway now uses Go, and Rust is out of scope.
See [staged plan](go-runtime-plan.md).

The [rejection-only worker/pool experiment](evidence/live-2026-10-07/observability/worker-experiment.md)
completed 35,000 requests with Go against Python: 12/15 retains availability; 24/15
introduces DB-acquisition 503s; 24/30 fixes those errors without increasing throughput.
No production worker/pool settings changed. A disposable CPU profile is now recorded
(see below); precise SQL lock attribution and sustained successful inference capacity remain open.

The [existing OTel latency distribution](evidence/live-2026-10-07/observability/latency-distribution.md)
separates queue, DB/limiter and upstream duration; a subsequent CPU profile is recorded
below with its overhead and attribution limits. Historical trace inspection exposed a Tempo OOM at its 512MiB lab limit;
the [1Gi/concurrency-2 follow-up](evidence/live-2026-10-07/observability/tempo-tuning.md)
now passes 100 historical reads and five new API/SQL traces, peak 344MiB, no additional
OOM/restarts during the check. Sustained/larger-dataset query capacity remains open.

The [CPU profile](evidence/live-2026-10-07/observability/gateway-cpu-profile.md)
identifies OTel and the PostgreSQL client stack as the largest attributed CPU groups.
Profiler overhead reduced throughput about 3.3 times; 19% of thread CPU is unassigned.
Normal-runtime attribution requires an unprofiled controlled comparison.

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

The [gateway hot-path profile](evidence/live-2026-10-07/observability/gateway-hotpath-ab.md)
is now implemented and live-tested: independent signal providers, gateway SQL tracing
off in normal mode, exact counters with separate caller usage, sampled latency metrics
and 30-second normal export. Normal repeats achieved 492/500/500 RPS versus full
477/489, with 43.8% lower mean timed-window CPU. All 50,000 requests were valid 429s;
exact counters, sampled HTTP traces and custom DB metrics survived. These short runs
exclude forced flush cost and do not establish sustained successful inference capacity.
Installed deployments retain their earlier image/settings.

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


The [gateway concurrency follow-up](evidence/live-2026-10-07/observability/gateway-concurrency-followup.md)
removes locks around OIDC verification/network fetches and cache DB loads, and makes Go
idle timeout configurable (60-second default). The source-built `8e4dd9b` artifact passed
its HIGH/CRITICAL scan and is installed on two ARM64 lab replicas. During Helm rollout,
200/200 real function requests succeeded; post-rollout connection reuse after 5.2 seconds
idle and health/readiness passed. API/reconciler pod templates were unchanged.
Heterogeneous-client/real-OIDC load acceptance remains pending.

Local kind [storage housekeeping](local-storage.md) now configures native kubelet image GC
(six-hour unused-image age, 80/70% imagefs thresholds) and three 10Mi log files. The ARM64
acceptance node loaded the policy; runtime config, idempotent reapply and final component
readiness passed. Registry/PVC retention and host disk alerts are separate; this is not a
total volume size cap. A natural six-hour GC/log-rotation drill remains unobserved.


Control-plane dependency split (2026-10-07): inference-only requirements now live in
`.[inference]`; the shared Python control-plane lock drops from 97 to 82 packages
without upgrading retained versions. Native ARM64 Trivy image size decreased from
838.44 MB to 448.90 MB; API/reconciler startup RSS decreased by about 22%/24% in the
isolated empty-state fixture. Both full image release gates passed, including scans
and SBOM. Broader regression testing found only baseline-confirmed UI, memory-race and
typecheck diagnostics; it is not reported as wholly green. The slim image was subsequently
deployed to the local acceptance cluster; the seven-phase CPU lifecycle gate passed.
[Measurements and exact limits](evidence/live-2026-10-07/controlplane-dependency-split/README.md).

Go log streaming and the readable Runs detail log panel are deployed in the local acceptance
cluster (2026-10-08). The user-reported `a061e088` / `train` log snapshot decodes UTF-8,
groups three Git metadata warnings, preserves details/raw downloads and treats Argo INFO
`error="<nil>"` exits correctly. Eight live stream/browser checks and 22 UI tests passed.
[Validation evidence](evidence/live-2026-10-07/log-stream/README.md).

The MLflow tracking/registry server profile also passed native ARM64 build, pip check,
seven PostgreSQL/MinIO fixture checks, fixable HIGH/CRITICAL and secret scans. The image
has 82 Python distributions; its measured Trivy size is 540.82 MB. It has not been
rolled out to the cluster. [Audit evidence](evidence/live-2026-10-07/mlflow-server/README.md).
