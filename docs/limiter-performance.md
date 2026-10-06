# PostgreSQL limiter performance gate

The gateway shares endpoint and caller buckets through PostgreSQL. More gateway
replicas must not multiply the available budget. A 429 is an expected limit decision;
5xx responses, transport failures or a growing client queue are availability failures.

The original [port-forward load matrix](evidence/live-2026-10-06/limiter-load.json)
preserved budgets at 1/2/4 replicas, but failed availability at 500 offered RPS.
Its HTTP latency includes client queueing and port-forward overhead, so it cannot
establish an isolated limiter latency or a production throughput ceiling.

## SQL transaction path

The optimized PostgreSQL path uses three statements: transaction-local lock/statement
timeouts, an ordered bulk insert for missing buckets, and one statement that locks,
refills, admits and updates all buckets. The previous two-bucket path used nine
statements. The commit and connection pre-ping remain additional round trips.

Locks and first-use inserts use the same `C` collation order as the prior Python sort,
including during a mixed-version rollout. A materialized dependency forces the DB
clock to be read after all bucket locks are acquired. Admission, refund and token
debt remain atomic; denied admission consumes neither bucket. The 2s lock timeout,
3s statement timeout and fail-closed HTTP behavior remain in place. SQLite retains
its existing local transaction path; it is not the performance evidence.

## Repeating the in-cluster matrix

Use an isolated `acceptance-*` project with an existing public HTTP function endpoint,
endpoint and caller budgets of 600 units/minute, and a dedicated API key. No unrelated
traffic may use those two buckets during measurement. Record the endpoint's original
configuration; restore it and revoke the fixture key after the run.

Mount `scripts/controlplane_limiter_matrix.py` and
`scripts/controlplane_limiter_check.py` into a ConfigMap and run the matrix script
from the tested control-plane image in a restricted Kubernetes Job. Target four
**distinct gateway pod IPs**, not the same Service address four times. The generator
needs no Kubernetes API token. Inject these values from Secrets, without printing them:

- `CP_ACCEPTANCE_GATEWAY_TOKEN`: the dedicated fixture key.
- `CP_ACCEPTANCE_DATABASE_URL`: a gateway-role connection for observing lock waits.
- `CP_ACCEPTANCE_ADMIN_DATABASE_URL`: an isolated fixture-admin connection to reset
  only the explicitly named endpoint/caller buckets between completed samples.

Run with `--project`, `--endpoint`, `--caller`, four `--urls`, `--prometheus` and
`--confirm-test-endpoint`. The default matrix is 1/2/4 target replicas at 50/100/500
RPS, ten seconds per sample. Keep resource limits, telemetry and generator settings
identical between baseline and optimized images. Do not overlap image builds, disk
cleanup, deployments or other acceptance traffic with measured samples.

Enable the gateway's existing OTLP limiter histogram and unique `service.instance.id`
for each replica. In this lab, Prometheus can receive metrics directly through its
[OTLP receiver](https://prometheus.io/docs/guides/opentelemetry/); production can use
its usual collector. Set a short metric export interval for the fixture, and wait for
exports before and after each sample. The matrix subtracts cumulative histogram
snapshots, rejects resets/incomplete data, and checks that observations match requests.
Reported histogram quantiles are interpolated estimates, not exact per-request values.

The load path uses aiohttp with a persistent 200-connection pool and uvloop. The prior
HTTPX generator consumed almost a full CPU core and built large client queues in
some 500 RPS samples. Those diagnostic runs are retained separately; use the same
aiohttp/uvloop generator image and sampling code for the actual baseline comparison.
Generator scheduling lag, queueing and CPU observations are part of the evidence.

The output separates limiter transaction latency, HTTP request latency, client queue
latency and scheduling lag, and records generator CPU throttling and bucket lock
waiters. A passing sample requires shared-budget correctness, successful inference,
no 5xx/transport/DB-sampling failures, complete metric observations, and completed
throughput at least 90% of offered throughput, and client queue/scheduling p95
no greater than 100ms. Those guards detect fixture backlog; they are not production
inference latency SLOs. The low 600/minute budget emphasizes
admission/rejection contention; passing it does **not** prove 500 successful inference
requests per second, long-duration stability, multi-node behavior or GPU capacity.

## Recorded result — 2026-10-07

[Summary](evidence/live-2026-10-07/limiter/summary.json),
[baseline](evidence/live-2026-10-07/limiter/baseline.json) and
[optimized matrix](evidence/live-2026-10-07/limiter/optimized.json) record the same
single-node ARM64 kind cluster and sampling code. Four gateway pods ran throughout;
traffic targeted one, two or four distinct pod IPs. Each gateway had a 1 CPU limit,
and the fixed generator image had a 2 CPU limit. No image builds, scans, pushes,
deployments or disk cleanup overlapped these measured matrices.

**500 offered RPS passed with two and four target gateways. The full matrix remains
failed because the single-replica sample develops a queue.** All 50/100 RPS samples
passed; every shared-budget upper bound and limiter observation count held.

| Target gateways, 1 CPU each | Completed RPS, before → after | Limiter p95, before → after | HTTP request p95, before → after | Sampled peak lock waiters, before → after |
| --- | --- | --- | --- | --- |
| 1 | 403 → 383 | 81 → 67 ms | 1,220 → 1,699 ms | 11 → 10 |
| 2 | 492 → 493 | 96 → 30 ms | 519 → 161 ms | 22 → 20 |
| 4 | 490 → 495 | 187 → 4.7 ms | 516 → 4.0 ms | 47 → 0 |

The SQL change substantially reduced limiter and request latency at two/four replicas;
throughput at those levels was already near the offered rate after fixing the generator.
It does not improve the single-replica throughput in this sample. The old HTTPX/port-forward
results therefore cannot be interpreted as a PostgreSQL production capacity ceiling.

The [single-gateway 2 CPU probe](evidence/live-2026-10-07/limiter/single-replica-capacity.json)
completed 464 RPS with no transport/5xx errors and correct budgets, but retained a
2.2-second p95 client queue. Its initial throughput-only gate reported a pass. The
corrected gate also requires client queue and scheduling p95 ≤100ms, and re-evaluation
marks this probe failed. These are generator/backlog guards, not an inference latency
SLO. The two/four-gateway results pass these stricter guards. Keep the raw legacy result
and the counterexample; raising a CPU cap alone has not established single-pod capacity.

[Image provenance, checks and source hashes](evidence/live-2026-10-07/limiter/image.json)
record a working-tree ARM64 acceptance image, verified application wheel/source hash,
`pip check`, zero fixable HIGH/CRITICAL Trivy findings and an SPDX SBOM. The finalized
wheel was installed over the completed dependency image after a disk-full incident;
the exact acceptance recipe and base digest are recorded. This does not close the
full clean release-artifact or AMD64 runtime gates. The retained HTTPX diagnostic
reports are separate from the aiohttp/uvloop comparison.

The lab ended with two healthy gateway replicas at the original 1 CPU cap and the
optimized image. Endpoint limits were restored, the dedicated API key revoked, and
completed generator Jobs/ConfigMap/token Secret removed. API/reconciler images were
unchanged. Gateway image/telemetry were patched for this acceptance run; a future Helm
release must explicitly select its intended artifact and telemetry configuration.

**Remaining:** single-replica capacity/queue diagnosis, sustained load, high-budget
mostly-admitted inference traffic, sustained DB outage/thread growth, and targeted
statement-timeout injections. The real row-lock timeout and atomic concurrency/debt/
refund/clock-after-lock regression tests passed. No Redis replacement is justified by
these measurements alone.
