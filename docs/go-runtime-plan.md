# Go adoption and performance gates

Updated 2026-10-07. Keep existing correctness-heavy services in Python until measurements
justify a replacement. Rust is out of scope. The installed gateway now uses Go; API/reconciler remain Python. The original isolated PoC evidence is preserved.

| Order | Work | Current scope |
| --- | --- | --- |
| 1 | Python/OTel bottleneck signals | HTTP/HTTPX/reconciler SQL, DB acquisition/query/transaction lifetime, gateway phases, loop lag, inflight, limiter workers and AnyIO pool metrics implemented/tested locally. Real Collector/Tempo application export and native HTTP/SQL/outbound traces passed. Resource panels remain unpopulated. |
| 2 | Go load generator | `tools/loadgen`, race/vet tests and local 500 offered RPS smoke pass. Native + Linux ARM64 binaries compile. Paired in-cluster 1/2/4-pod comparison passed request/budget integrity; both clients approach 500 RPS at 2/4 pods, one pod queues. Production scratch-image gate remains open. |
| 3 | Go S3/MinIO storage initializer | Next implementation component; existing Python initializer remains active. |
| 4 | Profile Python gateway | Hot-path normal/diagnostic profiles implemented; live normal 492–500 RPS with retained counters/traces, 43.8% lower timed CPU vs full reference. Paired generator and 429-only 12/24-worker probes recorded. Larger worker/pool settings did not improve throughput. CPU profile recorded with substantial profiler overhead; Unprofiled OTel A/B recorded: CPU/request falls 60% with SDK disabled; SDK-disabled higher-load probes plateau around 600–700 RPS; precise SQL lock attribution remains open. |
| 5 | Go gateway migration | OIDC/project roles, LLM reservation/settlement, streaming deadlines and full DB-role readiness implemented/tested. Separate source-built image, scan/SBOM, normal-profile soak and live two-replica deployment recorded in the migration report. Successful-upstream load, real identity-provider/GPU acceptance and multi-node drills remain open. |
| 6 | Go log streaming | Separate feature after performance work; retain project authorization and scoped Kubernetes log access. |
| 7–8 | Reconciler | Keep Python CAS/idempotency/rollout/watchdog behavior. Consider informer/workqueue only if polling scale is measured as a problem. |
| 9 | Rust | No implementation planned. |

## Next acceptance: benchmark client

Use identical payloads, rate/window/concurrency/connection budgets, target pod IPs,
endpoint/caller budgets and warm-up for Python and Go. Record generator CPU/RSS/cgroup
limits, scheduling lag, queue delay, dropped/unscheduled requests, status counts and full
body completion. A mostly-429 limiter fixture is not 500 successful inference RPS.
Run 1/2/4 distinct targets and preserve existing failed single-replica evidence.
Do not accept a result whose generator hides backlog or loses offered requests.

## S3 initializer contract before replacement

Initial scope is S3/MinIO, not an implicit replacement for HF/gated-model protocols.
Preserve the existing KServe argument contract, AWS credential/endpoint/region/HTTPS
semantics and revision-scoped storage service account integration. Use AWS SDK Go v2.

Acceptance covers prefix/object downloads, traversal/symlink protection, bounded memory
and request/retry deadlines, cancellation and incomplete-download cleanup, optional SHA256
verification, atomic file publication and explicit failure exit codes. Compare image
size/startup/RSS and scan/SBOM with the remediated Python image; lower CVE exposure remains
a hypothesis until measured. Repeat actual private MinIO → model serving acceptance
before switching the installed initializer image.

## Normal-profile soak follow-up

The subsequent [normal-profile soak](evidence/live-2026-10-07/observability/gateway-normal-soak.md)
ran two five-minute 500 RPS windows across ten observed regular exports each. Mean CPU
was 0.53–0.56 core with stable sampled memory/threads. Both strict zero-error gates
failed: 299,997/300,000 attempts returned 429; three transport failures remain, including
two immediate header-phase connection resets. The connection follow-up below narrows the failure mechanism; longer
successful-upstream/streaming load and final clean release acceptance remain open.

At 500 rejection-only RPS, mean Python CPU does not establish saturation. The paired
Go PoC below now provides separate comparative evidence of a runtime/driver advantage. Use the short-idle client result below and measure
successful upstream/streaming traffic before applying the migration gate.

## Gateway migration gate

A PoC must preserve API-key hash checks/revocation, project authorization, shared limiter
locking/debt/settlement, route cache semantics, errors/headers and streaming cancellation.
Compare with the same schema, CPU/memory limits, connection budget and traffic.
Prefer Python if DB or upstream dominates. Migrate only for a measurable operational win.

[Observability boundaries](observability.md#gateway-phase-and-runtime-follow-up) and
[loadgen contract](../tools/loadgen/README.md) describe current measurements and open gates.

[Live paired results and measurement limits](evidence/live-2026-10-07/observability/README.md#live-collectortempo-and-paired-generator-acceptance) record the latest closure scope.

The subsequent [keep-alive A/B/C](evidence/live-2026-10-07/observability/gateway-connection-ab.md)
reproduced a reset on a reused socket idle for 4,999 ms near Uvicorn's 5-second boundary.
Client idle timeout 2 seconds retained reuse and passed 150,000/150,000 rejection requests
at 500 RPS with no transport failures/drops and 0.60 mean CPU core. Keep-alive off failed
at 472 RPS, 0.98 core, 856 network errors and 2,535 queue drops. The recommended acceptance
client setting is idle 2 seconds; the generic CLI default remains 90 seconds.
The gateway subsequently migrated to Go as recorded above. Sustained successful
upstream/streaming and full platform release acceptance remain open.

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
