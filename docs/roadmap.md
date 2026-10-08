# Roadmap

Current consolidated status (2026-10-07): [status.md](status.md). Dated findings and test records below retain their original scope.

Current security remediation, local evidence and outstanding deployment/test work: [security-hardening.md](security-hardening.md).

Where the platform stands, what is missing, and what comes next. Kept up to date with the
code; the original milestone plan is in [history/](history/).

## Technical-review progress (reviewed 2026-10-05, updated 2026-10-06)

Priorities and the original findings are in [technical-review.md](technical-review.md).
Code fixes and live results are distinguished below; the current gate scopes are linked
to the 2026-10-06 evidence.

- KServe readiness now verifies the platform revision on the immutable Knative backend,
  including a separately verified previous backend for canary metrics. Custom containers
  and ready services at zero pods do not require `modelStatus == UpToDate`.
- MLflow alias reconciliation skips functions and Hub-based LLMs.
- New function versions require `repository@sha256:<64 hex digits>`. Tags are rejected;
  API docs, UI and demo fixtures match. Existing tagged versions are not rewritten and
  should be replaced with new digest registrations before relying on rollback identity.
- Project, run, pipeline, deployment and rollout batches isolate/log per-entity errors,
  then continue. Failed entities use process-local exponential backoff (5–300 seconds).
- [Control-plane image and chart](installation.md) are prepared: API, gateway, Lease-elected
  reconcilers, RBAC, migration hook, Services and optional Ingresses. Image runtime checks,
  pinned dependency/bootstrap bundles and live runtime/readiness/admission checks have
  passed in the [2026-10-06 evidence batch](evidence/live-2026-10-06/README.md). The full
  seven-phase CPU lifecycle also passed on single-node ARM64; multi-node failure drills
  remain open.
- [Connectivity matrix](networking.md) and database/OIDC policy corrections are prepared;
  configurable serving ingress and workload egress policies are implemented; actual CNI
  ingress allow/deny tests passed on an enforcing engine; strict-egress checks remain open.
- Live checks passed immutable backend/apply identity after drift repair, namespace-scoped
  Knative Revision reads, candidate-only metric attribution/rollback and actual zero-pod
  reactivation. Broader controller-default cases and multi-node behavior remain open.

## Done

| Area | What works today |
| --- | --- |
| **Projects and access** | Projects with their own namespace and quota. OIDC sign-in for the browser (server-side PKCE, signed session cookie) and for bearer tokens. Project roles (`invoker` < `viewer` < `operator` < `admin`) for users and groups, plus platform admins. A fail-closed policy table and an audit trail |
| **Scheduling** | Job/Pipeline cron schedules with timezone/DST, pause/resume, bounded queues, concurrency scopes, missed-run policy and execution/run history. [P0 contract](scheduling.md); backfill remains future work |
| **Data Management** | Project S3/MinIO connections, SecretRefs, immutable CSV/Parquet dataset versions, integrity identities, schema and producer lineage. [Contract](data-catalog.md); Connections/Datasets UI, typed version registration, schema/history and recorded lineage graph |
| **Batch Inference** | Managed S3/MinIO CSV/Parquet scoring with pinned classic models, bounded verification, conditional outputs, atomic dataset lineage, Jobs/Pipelines/Schedules and UI. [Contract](batch-inference.md); [local acceptance](evidence/live-2026-10-08/batch-inference/README.md) |
| **Run parameters** | Immutable schemas, validation/defaults, persisted snapshots, retry/idempotency, per-step container parameters and schedule bindings. [Contract](run-parameters.md) |
| **Training** | Jobs and pipeline DAGs on Argo Workflows. Retry, cancel and logs; step timelines and failure reasons. Lineage from a run to the model versions it produced |
| **Models** | Versions from the MLflow registry (including discovery after successful pipelines), or full-commit-pinned LLM versions from the Hugging Face Hub. Thresholds and evaluation, promotion to champion, registry aliases kept in sync |
| **Serving** | Immutable revisions. Canary rollouts with gates (error rate, p95, minimum traffic) that roll back automatically. Manual rollback. Metrics and trends per revision |
| **LLMs** | LLM models with GPU and context settings. Versions from the Hugging Face Hub, judged on offline results. KServe's Hugging Face runtime (vLLM) on GPUs. Per-project GPU quota, set by platform admins and checked on deploy, rollback and canary using maximum replica capacity. In the UI: model and hub forms with verdict previews, a chat playground, OpenAI snippets, token usage per caller, and GPU quota and use |
| **Gateway** | A separate service that makes endpoints public. Per-caller API keys, shown once and stored hashed. Limits per endpoint and per key: requests, or tokens for LLMs. Streaming, OpenAI-compatible chat completions, usage by caller. Ingress, TLS and NetworkPolicy manifests |
| **Model Monitoring core** | Pinned classic-model/reference/observed dataset checks, PSI/missing-rate drift, delayed-feedback regression/classification metrics, append-only reports and attention inbox. [Contract](model-monitoring.md). Online capture and rolling-window automation remain open |
| **Monitoring** | Monitor page (reconcilers, API, gateway, external systems). Grafana dashboard, 10 promtool-tested alerts, traces from request to reconciler |
| **Functions** | The team's own container behind an endpoint. Versions are images, deployable at once, served by KServe on Knative with a replica range (scale to zero), concurrency and environment. Called at `POST …/invoke` with any JSON, through the gateway with keys and limits, with canaries and rollback like models |
| **Web UI** | App shell with a sidebar and project switcher. Pages for runs, pipelines, models, deployments, activity, settings, members and API access. Charts built to a data-viz spec. Previews before risky changes. Light, dark and mobile |

## Missing in the code

### Gateway acceptance
- Native HTTP function/LLM tests cover streaming, usage refunds, concurrent reservations
  and disconnects (`make cp-http-test`). Real ingress/TLS and GPU serving remain live gates.

### Functions
- Private registry references are managed through project Secrets and registration selectors.
  Actual image pulls and credential rotation still need a live workload check.
- **Cold starts are not shown.** Neither the gateway nor the Monitor reports how often calls
  wait for a function to start.

## Missing to install it on a cluster

- **Build and verify the prepared control-plane image and chart.**
  `make cp-docker-build` builds `mlp-controlplane:dev`; `helm/controlplane` packages
  API/reconciler/gateway, RBAC and migrations. The ARM64 lab has a passing clean
  control-plane image gate and a live deployment; repeat the gate for the final release
  artifact and resolve the serving/initializer scan findings. See [installation.md](installation.md).
- **Verify networking and readiness in the target topology.** The [matrix](networking.md)
  records required paths. `/readyz` checks DB/schema; `/healthz` provides process liveness.
  Recorded ingress/DNS isolation and runtime probes passed on the lab; strict egress,
  real ingress/TLS and target-topology acceptance remain open.
- **Argo Workflows in `make local-up`.** Its `workflowNamespaces` and RBAC must cover the
  `mlp-*` namespaces.
- **Per-project secrets:**
  - MLflow and S3 credentials for training steps.
  - The Hugging Face token (`mlp-hf-token`) for gated models.

  Manage these with the project Secrets API/UI. Classic S3/MinIO storage-initializer authentication is implemented through revision
  accounts and storage_secret references; real private S3 artifact loading passed in the
  ARM64 lab. Other private/gated provider paths still need configuration/contracts; see [secrets.md](secrets.md).

## Not yet run against the real thing

The following scopes still lack live evidence. Recorded CPU results are listed separately
below; procedures are in [local-verification.md](local-verification.md).

- KServe vLLM on a GPU, maximum-replica/canary GPU quota and immutable/gated Hub downloads.
- Broader pipeline DAG/retry/cancel paths beyond the recorded CPU training/discovery flow.
- The gateway behind a real ingress with TLS (cert-manager); lab gateway inference and
  ingress NetworkPolicy allow/deny checks already passed.
- Full production `mlp_*` collection and alerts firing (gateway limiter OTLP ingestion
  is now live-verified).
- A Keycloak client-credentials token accepted by the gateway.

Already verified for real here:
- PostgreSQL migrations through `0021`, separate runtime grants, shared-budget load at
  1/2/4 gateways and basic real outage/recovery, including expired route/key caches.
- Full single-node ARM64 CPU lifecycle: real Argo/MLflow training/discovery/evaluation,
  KServe/gateway inference, healthy canary, Secret rotation/restart, drift repair and
  scale-to-zero/reactivation; separate candidate-only rollback and forced Secret recovery.
- Admission/PSA, namespace RBAC, enforcing ingress isolation, Lease failover/PDB and
  API/gateway rolling restart in the acceptance lab.
- Prometheus 3.1: every query the platform makes.
- Keycloak 26.4: browser sign-in.
- The gateway end to end, as a real process with HTTP to a model server.
- Chromium: the UI tests.
- The alerts with promtool, the manifests with kubeconform.

## Known limitations

- **Production limits use shared PostgreSQL buckets.** Native concurrency tests and
  live 1/2/4-gateway shared-budget checks passed. Basic DB outage/recovery passed with
  warm and expired caches. The new in-cluster comparison passes 500 offered RPS at
  two/four gateways and records isolated limiter latency. One gateway still queues,
  including the 2 CPU probe. Sustained/high-budget inference load, sustained outage/thread
  growth and targeted statement-timeout injections remain open. See [load report](limiter-performance.md).
  Explicit `CP_GATEWAY_LIMIT_STORE=memory` and demo mode keep per-process budgets.
- **Token admission uses estimates.** Missing usage and interrupted streams consume their
  reservation. A model-specific tokenizer is still needed for exact hard token ceilings.
- **LLM evaluation uses results supplied at registration.** There is no built-in evaluation
  harness.
- **Canary rollouts need `CP_PROMETHEUS_URL`.** Without it, they do not advance.
- **Missing operations:**
  - no durable log archive or physical run-history purging;
  - no log streaming.
- Dependencies are regenerated for Linux/amd64 with pinned uv; deliberate upgrades and
  image/runtime validation remain release work. See [installation.md](installation.md).
- **AWS is designed in Terraform but has never been applied.**

## Next, in order

Bottleneck instrumentation is implemented for gateway HTTP/HTTPX, reconciler SQL, DB
acquisition/query/transaction lifetime, pod identity and resource panels. Operator query
profiling passed on isolated PostgreSQL. Validate exported traces/new dashboard series
on the final cluster artifact; continuous PostgreSQL scraping, gateway phases/event-loop/
threadpool, collector health alerts and GPU latency signals remain open. See
[observability](observability.md#bottleneck-instrumentation-2026-10-07).

1. **Close release-image gates:** repeat the clean control-plane gate for the final
   artifact/architecture, including the remediated serving/initializer images. Their
   new ARM64 scans passed with zero fixable HIGH/CRITICAL; the prior five HIGH findings
   are resolved. Lab
   migration through `0021`, admission/PSA and ingress isolation already passed; repeat
   the required checks for the production topology and verified OIDC identities.
2. **Extend limiter capacity and HA/isolation evidence:** isolated latency and in-cluster
   500 RPS checks passed at two/four replicas. Diagnose single-replica queueing, then test
   sustained/high-budget inference load, sustained DB outage/thread growth, hung-leader fencing, strict egress
   and multi-node drain/loss. Full CPU lifecycle and basic DB outage/recovery are closed
   for the recorded single-node ARM64 scope.
3. **Finish workload credential and recovery gates:** private-image pulls/rotation,
   cleanup conflict/outage retry, private initializer providers beyond classic S3 and
   control-plane backup/restore with recovered roles, revoked keys, lineage and RPO/RTO.
4. **Finish LLM and remaining implementation work:** GPU serving, real ingress/TLS
   streaming, maximum-replica/canary quota and immutable/gated Hub downloads when resources
   are available; stable audit actor_subject persistence, safe rate-bucket GC, durable
   logs/streaming, cold-start observability and exact tokenization.
5. **AWS:** EKS, RDS and S3 from the existing Terraform.

## Further remediation (implemented 2026-10-05, updated 2026-10-06)

DB/schema readiness, topology-configured policies, function resources/probes, per-entity
backoff, persisted deadlines, deployment deletion and optional workflow retention are
implemented. LLM requests reserve budgets before forwarding and retain them on missing
usage/interruption. Control-plane backup/restore tooling is prepared.

See [operations.md](operations.md) and [recovery.md](recovery.md). Migrations `0014`/`0015`
were exercised by live migration through `0021`; recorded CPU/runtime checks passed on
the ARM64 lab. Restore and broader topology behavior remain open; local checks are
recorded in [local-verification.md](local-verification.md).

### Serving drift and project credential management

Serving reconciliation now compares the owned predictor configuration, including image,
environment, arguments, resources, probes and secret references. A same-revision drift
replaces the owned predictor with a resource-version precondition and a fresh apply ID;
readiness waits for the matching Knative backend. Unowned metadata/defaults are preserved
where comparison allows them. The live same-revision repair passed; omitted zero probe
delay and empty env slices exposed drift loops that were fixed and verified live.
Broader controller-default cases remain separate checks.

Project secret create/list/rotate/delete API and Settings UI are implemented. Values stay
in project-owned Kubernetes Secrets; registration validates names/keys and immutable
workload revisions snapshot references. Rotation uses version checks, referenced deletion
is protected, and secret values are excluded from API responses and audit payloads.
Migration `0016` passed as part of live migration through `0021`. Workload rotation,
restart, protected deletion and forced deletion/startup/recovery passed; private-registry
pulls and broader provider paths remain open. See
[secrets.md](secrets.md) for scope and limits.

### Shared budgets and release preparation

Production gateway buckets now live in PostgreSQL (migration `0017`), with atomic
endpoint/caller reservations. LLM GPU admission reserves per-replica GPUs multiplied by
`max_scale`, including active/desired and canary overlap (migration `0018`). New Hub
registrations require a full lowercase 40-character commit SHA.

Successful pipeline runs trigger delayed, lineage-scoped MLflow discovery with durable
checkpoints (migration `0019`); discovery does not evaluate or promote versions. Job/model
forms select secret names and keys; Settings shows referencing definitions/revisions.

Base-image/dependency pins, checksum-verified bootstrap bundles and manual-sync GitOps
manifests are prepared. See [installation.md](installation.md). These code changes are
locally verified and deployed on the ARM64 lab: the clean control-plane image gate,
live migrations through `0021`, shared-budget checks and basic DB outage/recovery passed.
Final release-image validation, real GPU/TLS workloads, single-replica limiter capacity/sustained outage
and backup/restore drills remain open.

### Security, HA and manual acceptance tooling

API Secret CRUD and workload/log reads are restricted by project RoleBindings; the
API ClusterRole retains only namespace get for ownership checks. Existing READY projects repair missing bindings. Migration processes
use PostgreSQL advisory locking and follow the documented expand/contract contract.
API/gateway default to two replicas with PDB/spread/rolling settings; two reconcilers
coordinate via Lease with a main-loop progress watchdog and their own PDB, with explicit
single-process local mode. Kubernetes SDK transport calls have bounded timeouts. Production values enforce
network/site/identity/image configuration. Private S3/MinIO initializer credentials now
have a separate revision-scoped serving account/reference path, with owned-account
cleanup after deployment deletion.

Manual image, limiter load/outage, database recovery and full CPU acceptance commands are
available. Image, full CPU, adversarial canary, ingress CNI isolation and basic DB outage
checks have recorded passes; the new 500 RPS limiter check passes at two/four replicas
while one replica still queues. Backup/restore, sustained
outage, strict egress and GPU/Hub/private-registry drills remain open. Logs/archive/streaming,
operational cold-start metrics and exact tokenization remain implementation work.
See [acceptance.md](acceptance.md).

### Reconciler admission boundary

The current chart 0.3.1 retains always-on fail-closed native policies for namespace ownership,
namespace-scoped provider writes, exact project RoleBindings/subjects and the workflow
executor Role. Kubernetes 1.30+ is required. Rendered CEL tests and a server dry-run live
gate passed on the installed lab policies: four policies/bindings, zero type warnings
and nine impersonated server dry runs. Full CPU provisioning also passed; broader
production admission/topology checks remain separate.
See [operations.md](operations.md#reconciler-provisioning-trust-boundary).

## 2026-10-06 ARM64 runtime follow-up

Native classic serving and S3 initializer images load real artifacts and reach KServe
READY. Gateway route normalization is deployed and a real prediction returns HTTP 200
with the expected result. The seven-phase CPU gate now passed, including healthy
canary, credential rotation, same-revision drift repair and zero-pod activation.
Candidate-only error attribution/rollback, scoped storage account cleanup and isolated
forced Secret deletion/startup/recovery also passed. The deployed metric snapshot uses
one observation time for all revision queries and measured candidate error rate 1.0.
The original five HIGH findings are retained in baseline scans. New serving/initializer
images passed their own scans with zero fixable HIGH/CRITICAL, SBOM generation, native
MLflow/V2 inference and cluster private S3 loading/gateway checks. Repeat for a final clean
release artifact; the earlier clean control-plane scan does not certify these images.
See [dependency remediation](serving-image-security.md) and
[ARM64 evidence](evidence/live-2026-10-06/serving-arm64/README.md).


## 2026-10-06 limiter follow-up

Live 1/2/4-replica measurements preserved shared budgets. 50/100 offered RPS had no
availability errors; 500 RPS failed availability with transport timeouts and growing
bucket lock waiters. The [2026-10-07 in-cluster follow-up](limiter-performance.md) now
passes 500 offered RPS at two/four replicas, with isolated telemetry and reduced
SQL round trips; single-replica queueing remains open. Basic PVC-backed PostgreSQL
outage/recovery passed, including expired auth/route caches after a redacted 503 fix.
Sustained outage/thread growth and backup/restore remain open. The initial ephemeral
acceptance DB was lost during the first pod-stop test; historical rows were not restored.
See [live evidence](evidence/live-2026-10-06/README.md).

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
