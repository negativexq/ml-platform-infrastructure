# M5 Gate — evidence

Run date: 2026-09-10 · kube-prometheus-stack in namespace `observability`
(Prometheus, Grafana, Alertmanager, kube-state-metrics, node-exporter) ·
inference image `v4`

## Metrics the service actually emits

Defined in [`app/metrics.py`](../../../app/metrics.py). Nothing is claimed here
that is not in that file:

| Metric | Type |
| --- | --- |
| `http_requests_total{method,path,status}` | counter |
| `http_request_duration_seconds{method,path}` | histogram |
| `prediction_requests_total` | counter |
| `prediction_errors_total{reason}` | counter |
| `prediction_duration_seconds` | histogram |
| `model_load_duration_seconds` | histogram |
| `model_load_failures_total` | counter |
| `model_ready` | gauge |
| `model_info{model_uri,source}` | gauge |

Route labels use the route *template*, not the raw path, so cardinality stays
bounded. `prediction_errors_total` label sets are pre-initialised at import so
"no errors" reads as `0` rather than as a missing series.

## Prometheus is scraping both pods

```
$ curl -s 'localhost:9090/api/v1/targets?state=active'
ml-platform-inference  ml-platform-inference-6868f445bf-8t8gb  health=up
ml-platform-inference  ml-platform-inference-6868f445bf-jr8tr  health=up
```

Scraping is wired by the chart's own ServiceMonitor
([`templates/servicemonitor.yaml`](../../../helm/ml-platform/templates/servicemonitor.yaml)),
enabled per environment — not by a hand-applied manifest.

## Dashboard: 12 panels, 3 rows, every query verified

[`observability/dashboards/inference.json`](../../../observability/dashboards/inference.json),
imported into Grafana by the sidecar:

```
$ curl -s -u admin:admin 'localhost:3000/api/search?type=dash-db'
 - ML Platform — Inference | uid: ml-platform-inference
panels: 12
rows: ['Service health', 'Model serving', 'Kubernetes health']
```

Rather than trust that the panels work, every query in the dashboard is run
against Prometheus by
[`scripts/verify-dashboard-queries.sh`](../../../scripts/verify-dashboard-queries.sh):

```
$ ./scripts/verify-dashboard-queries.sh
  OK     Request rate                           4 series
  OK     Latency p50 / p95                      4 series
  OK     Error rate (5xx share)                 1 series
  OK     Loaded model                           2 series
  OK     Pods ready to serve                    1 series
  OK     Prediction latency p95                 1 series
  OK     Model load duration (avg over 30m)     1 series
  OK     Prediction and model-load failures     1 series
  OK     Replicas: desired vs available         1 series
  OK     Container restarts                     2 series
  OK     CPU usage                              4 series
  OK     Memory working set                     2 series

every dashboard query returns data
```

The first run of this check **failed** — `prediction_errors_total` returned no
series, because `prometheus_client` does not export a labelled counter until
its first increment. That is what prompted the pre-initialisation above. A
panel that never returns data is a claim the platform cannot back, so it was
fixed rather than left in place looking impressive.

## Measured numbers

Everything below was read from Prometheus during a drill, not estimated:

| Quantity | Value |
| --- | --- |
| Cold-start model load (fetch + load from MLflow/MinIO) | **3.98 s** and **5.26 s** on two pods |
| `/predict` p95, healthy | **4.99 ms** |
| `prediction_duration_seconds` p95, healthy | **2.78 ms** |
| Pod replacement → ready (F1) | **12 s** |
| Recovery after artifact store returns (F3) | **12 s**, 0 restarts |
| Argo drift detection → reconciled (F4) | **~1.4 s** |
| Argo Git-commit pickup | **163 s** (default 3 min poll) |

## Failure scenarios

| # | Failure | Detection | Containment | Recovery | Evidence |
| --- | --- | --- | --- | --- | --- |
| F1 | Pod crash | ReplicaSet controller | surviving replica serves | replacement ready in 12 s, 0 restarts | [../m2/pod-recovery.md](../m2/pod-recovery.md) |
| F2 | Invalid model artifact | readiness 503, `model_ready=0`, `model_load_failures_total=6` | new pod `0/1`, kept out of endpoints; rollout stalls | Git revert → Argo sync → smoke green | [invalid-artifact.md](invalid-artifact.md) |
| F3 | Artifact store outage | `model_ready=0`, load failures rising | pod excluded; 100% of requests still 200 | background recheck, 12 s, 0 restarts | [artifact-store-outage.md](artifact-store-outage.md) |
| F4 | Config drift | Argo resource watch → `OutOfSync` | Git holds desired state | self-heal in ~1.4 s | [../m4/argocd-drift-reconciliation.md](../m4/argocd-drift-reconciliation.md) |
| F5 | Latency regression | `http_request_duration_seconds` p95 4.99 ms → 956 ms | **none automatic** — degraded but serving | Git revert → 4.87 ms | [latency-regression.md](latency-regression.md) |
| F6 | Bad rollout | `ImagePullBackOff`; Argo stuck `Progressing` | `maxUnavailable: 0` keeps old ReplicaSet at full capacity | Git revert → bad ReplicaSet pruned | [bad-rollout.md](bad-rollout.md) |

Six scenarios, six detection mechanisms, five automatic-or-Git recovery paths —
F5 deliberately has none, because a latency regression is not something a
readiness probe can or should catch.

## Gate result

| Check | Result |
| --- | --- |
| 5+ failure scenarios | PASS (6) |
| 5+ detection mechanisms | PASS (6) |
| 5+ recovery paths | PASS (6, one of them manual by design) |
| Prometheus metrics | PASS (9 application metrics, both pods scraped) |
| Grafana dashboards | PASS (12 panels, all queries verified against live data) |
| documented evidence | PASS (this directory) |
| no invented metrics | PASS — every number above traces to a query in `scripts/verify-dashboard-queries.sh` output or a drill transcript |

## Not done

- No alert rules. Prometheus and Alertmanager are installed and the F5/F6
  sections describe what the rules should fire on, but none are written, so
  nothing pages anyone yet.
- No tracing. OpenTelemetry was optional in the roadmap and was skipped.
- Grafana is reached by `kubectl port-forward`; only NodePort 30080 is mapped
  through kind, so the Grafana NodePort (30300) is not exposed on the host.
