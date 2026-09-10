# F5 — Latency regression

**Question:** if the model gets slower, do the metrics show it, and does
anything else notice?

## Method

`faultInjection.predictLatencyMs: 500` committed to `values-local.yaml` and
pushed. Argo CD applied it — the fault was injected through Git, not by hand,
because self-heal reverts direct `kubectl` edits within ~1s (see
[../m4/argocd-drift-reconciliation.md](../m4/argocd-drift-reconciliation.md)).

The sleep sits inside the measured region of `InferenceService.predict`, so it
simulates the model itself getting slower rather than transport overhead.

## Measured

| Metric | Baseline | With fault | After revert |
| --- | --- | --- | --- |
| `http_request_duration_seconds` p95, `/predict` | **4.99 ms** | **956 ms** | **4.87 ms** |
| `http_request_duration_seconds` p50, `/predict` | — | 564 ms | — |
| `prediction_duration_seconds` p95 | **2.78 ms** | **946 ms** | **0.99 ms** |
| `http_request_duration_seconds` p95, `/health` | 4.81 ms | **4.81 ms** | — |

Queries used are the dashboard's own, e.g.

```promql
histogram_quantile(0.95,
  sum(rate(http_request_duration_seconds_bucket{
    job="ml-platform-inference", path="/predict"}[5m])) by (le))
```

## What the numbers actually say

- **The regression is isolated.** `/health` p95 did not move — 4.81 ms before
  and during. So the process was fine; only the prediction path degraded. A
  single "service is slow" number would not have told us that.
- **p95 reads 956 ms, not 500 ms.** The histogram's buckets jump `0.5 → 1.0`,
  so a 500 ms-plus observation lands in the 1.0 bucket and
  `histogram_quantile` interpolates upward. The metric is not lying; the
  bucket layout limits its resolution in exactly the range this fault lives
  in. Quoting "p95 = 956 ms" as the true latency would be wrong.
- **Nothing self-healed, and nothing should have.** `readyReplicas` stayed 2
  throughout. A latency regression is not a readiness failure — the pods were
  serving correct answers, slowly. Recovering from this needs an SLO alert and
  a rollback decision, not a probe.

## Recovery

`git revert` + push. The 5m rate window still showed 953 ms right after the
revert because it still covered the fault period; a 1m window over fresh
traffic showed **4.87 ms**. Worth remembering when reading a dashboard during
an incident — the window lags the fix.

| | |
| --- | --- |
| Detection | Prometheus `http_request_duration_seconds` / `prediction_duration_seconds` |
| Containment | none automatic — the service kept serving, degraded |
| Recovery | Git revert → Argo sync → p95 back to 4.87 ms |
| Evidence | the table above |
