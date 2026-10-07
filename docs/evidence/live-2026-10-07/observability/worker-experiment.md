# Rejection-only gateway worker experiment — 2026-10-07

Traffic generator: **Go mlp-loadgen**. Gateway: existing **Python 3.12** application.
Python scripts orchestrate the lab, collect measurements and clean up. No Go gateway
implementation is being benchmarked here.

Single-node ARM64 Kubernetes 1.32; disposable gateway pods use the clean candidate digest
`sha256:7271ee509006b80d56c687ea4613b60a5fb28a1c2d887bef878027af78d32072`, each with
1 CPU / 1GiB limit. Collector export and 1% tracing remain enabled. Each sample uses
500 offered RPS for 10s, 200 client connections, 5,000 queue slots and a 15s timeout.
The caller has a unique name and its bucket is set to -6,000 tokens before warm-up;
this preserves fail-closed admission and never changes the shared endpoint limit.
100 sequential 429 warm-up calls precede the timed sample and histogram baseline.

The first runner stopped at the 24/15 availability failure. Its result was retained in
[initial evidence](worker-experiment-initial.json). The full five-case sequence was then
rerun with availability failures recorded separately from request accounting; see
[follow-up](worker-experiment-followup.json). No failed result was removed.
The actual combined order is 12/15 → 24/15 → 12/15 → 24/15 → 24/30 → 24/15 → 12/15.

| Workers / DB capacity | Samples | Completed RPS incl. drain | 503 total | Mean worker queue ms | Mean limiter work ms | Gateway CPU cores |
| --- | --- | --- | --- | --- | --- | --- |
| 12 / 15 | 3 | 485–487 | 0 | 131–177 | 16–24 | 0.84–1.00 |
| 24 / 15 | 3 | 466–472 | 54 | 279–314 | 50–50 | 1.00–1.00 |
| 24 / 30 | 1 | 482–482 | 0 | 311–311 | 49–49 | 1.00–1.00 |

[Combined JSON](worker-experiment.json) verifies **35,000 accounted requests**, no
transport errors, drops or unissued requests; every phase snapshot has exactly 5,000
limiter observations and no upstream calls. There are **54 fail-closed 503s**, all from
the three 24/15 samples: 18 each, exactly matching DB acquisition error counters.
The other 34,946 responses are 429. The run completed; availability does **not** pass
for every experimental configuration.

## What caused the waiting

- With 12/15, worker count peaks at 12 and DB connections at 12, below the pool's 15
  capacity. Mean connection acquisition is 1.08–1.61 ms. Request throughput is about
  485–487 RPS, with a remaining worker queue and visible CPU throttling. Removing the
  admitted/upstream burst improves the earlier mixed single-pod result (364 RPS), but
  this new clean-image/fresh-pod experiment is not an exact paired upstream A/B.
- With 24/15, all 15 DB connections and 24 workers fill. Mean acquisition rises to
  21.0–21.4 ms, with 18 acquisition errors/503s in every sample. Gateway CPU averages
  approximately one full core; 104/107, 104/107 and 106/107 cgroup periods are throttled.
  Adding workers shifts waiting into the pool and raises contention; throughput declines.
- With 24/30, acquisition returns to 1.82 ms and errors disappear, but throughput stays
  near 482 RPS. Mean SQL execution increases to 15.05 ms per statement, compared with
  4.35–6.89 ms in 12/15; mean limiter work is 49.25 ms. Cluster-wide lock waiter samples
  peak at 21 versus 10 for 12/15. CPU remains near one core, throttled in 103/104 periods.
  Larger pool capacity fixes the acquisition failures, not the throughput bottleneck.

The measured queue is a symptom of limited service rate: the 1-CPU Python/SQL path and
shared bucket locking govern worker turnover. Worker count alone is not a fix.
The lock sampler sees `pg_locks` cluster-wide, not exact per-query/table wait duration;
these signals do not allocate a precise percentage to PostgreSQL locks versus Python,
telemetry or GIL. Histogram quantiles are estimates and phases overlap.
CPU counters include the tiny diagnostic endpoint, readiness and telemetry; they are
not pure Python business-logic CPU. Ten-second runs do not establish sustained capacity.

Keep the installed two-replica gateway and its existing worker/pool settings. A next
controlled profile can vary CPU at fixed worker/pool counts, inspect Python hot paths,
and isolate query/row-lock time before a Go gateway decision. No production tuning or
rewrite was applied based on these probes.

## Repeat and cleanup

The lab-specific command refuses to overwrite existing evidence:

```sh
.venv/bin/python scripts/controlplane_gateway_worker_check.py \
  --out /tmp/mlp-worker-experiment-new.json
```

Optional `--cases 12:15 24:15 24:30` selects configurations. All configurations run before
availability failures cause a nonzero exit. The isolated wrapper changes only probe
executor/pool capacity; it is not a production entrypoint. Runtime CPU is read from
cgroups and exported over a disposable diagnostic port. No service publishes that port.
Both API keys were revoked, unique caller buckets deleted, probe pods/jobs/configmaps/
secrets removed, and task port-forward processes stopped. Normal deployments remain
unchanged. Application metric export remains enabled. Ruff, Python compile checks and
real request/metric accounting passed for the acceptance scripts.

Subsequent evidence: [CPU profile](gateway-cpu-profile.md) identifies telemetry and
DB client CPU candidates, with 3.3-times profiler slowdown and 81% attribution coverage.
This follow-up does not turn the wall-time percentages above into CPU shares.
