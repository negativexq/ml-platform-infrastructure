# M10 Gate — evidence

Run date: 2026-09-11 · metrics-server (kind, `--kubelet-insecure-tls`) ·
k6 v2.2.0 · promtool (Prometheus 3.x) · kube-prometheus-stack reinstalled on
the M7+ cluster

## HPA — measured, not assumed

`helm/ml-platform/templates/hpa.yaml`: target 50% CPU, min 2 / max 6 replicas.
Load: `scripts/loadtest/predict.js` (k6, ramping to 60 VUs, ~3.5 minutes) run
by `scripts/drills/autoscaling.sh` while sampling replica count every 10s.

```
=== baseline ===
t+0s   readyReplicas=2

=== k6 load starts ===
t+10s  readyReplicas=2
t+30s  readyReplicas=3
t+40s  readyReplicas=4
t+61s  readyReplicas=5
t+71s  readyReplicas=6   <- ceiling reached
...    readyReplicas=6   (holds at max for the remainder of the load)

=== k6 summary ===
checks_succeeded...: 100.00%  645809 out of 645809
http_req_duration..: avg=18.53ms  p(90)=23.54ms  p(95)=32.94ms  max=256.43ms
http_reqs..........: 645809  2935.415118/s
http_req_failed....: 0.00%

=== scale-down (stabilization window) ===
t+15s   readyReplicas=6
t+46s   readyReplicas=5
t+107s  readyReplicas=4
t+168s  readyReplicas=3
t+230s  readyReplicas=2   <- back to baseline
```

**2 → 6 replicas in 71 seconds** under load; **6 → 2 over ~230 seconds** after
it stops, stepping down one pod at a time rather than dropping straight to
baseline — the `scaleDown.stabilizationWindowSeconds` + one-pod-per-60s policy
in the HPA spec doing exactly what it says. **645,809 requests, 0 failures**
across the whole run, p95 32.94ms even saturated at 6 pods / 2,935 req/s.

(The drill's own CPU-percentage sampling used the wrong `kubectl` jsonpath —
HPA v2 stores it under `.status.currentMetrics[0].resource.current`, not the
v1-legacy field the script queried — so the replica counts above are the
load-bearing evidence; a corrected spot-check read `cpu: 3%/50%` from a
plain `kubectl get hpa` afterward.)

## PodDisruptionBudget — capacity protected through a real node drain

`minAvailable: 1`. Drill: `scripts/drills/node-drain.sh` drains the node
carrying one inference pod, with a request probe running throughout.

```
draining node: ml-platform-worker
evicting pod ml-platform/ml-platform-inference-...-2lz47
evicting pod ml-platform/platform-minio-0
evicting pod ml-platform/platform-postgres-0
drained in 11s

=== request outcomes during the drain ===
   60 200
```

**60/60 requests returned 200.** The surviving inference pod (already serving,
model in memory) absorbed all traffic while the evicted one was replaced.

**Unplanned finding:** the drained node also happened to carry
`platform-postgres-0` and `platform-minio-0` — single-replica StatefulSets
with no PDB of their own. Both were evicted and rescheduled (back onto the
same node once uncordoned, since their PVs are node-local via
`local-path-provisioner`). `/predict` never noticed, because a loaded model
doesn't touch Postgres or MinIO per request — the same design point M1's
non-blocking fix and M8's persistence drill already established. This is
accepted, not fixed: M8 already documents these as single-replica, no-HA local
dependencies; AWS replaces them with RDS and S3, which do not have this
failure mode.

## Alert rules — unit-tested, not just written

`observability/alert-rules.yaml`, tested with
`observability/alert-rules.test.yaml`:

```
$ promtool check rules alert-rules.yaml
Checking alert-rules.yaml
  SUCCESS: 5 rules found

$ promtool test rules alert-rules.test.yaml
SUCCESS
```

5 rules, 5 test groups, each asserting **both** a not-yet-firing point (before
`for:` elapses) and a firing point with the exact labels and rendered
annotation text. The first draft of this test suite failed 6 times over —
wrong percentage math, YAML folding adding a stray `\n` to every description,
a guessed `humanizeDuration` string, missing propagated labels — all caught by
running the assertions, not by reading the YAML and assuming it was right.

Applied to the cluster as a generated `PrometheusRule`
(`scripts/render-prometheus-rule.sh` wraps `alert-rules.yaml` — one source, so
the tested rules and the applied rules cannot drift):

```
$ kubectl -n ml-platform get prometheusrule
NAME                    AGE
ml-platform-inference   5s

$ curl localhost:9090/api/v1/rules | jq '.data.groups[] | select(.name=="ml-platform-inference")'
HighErrorRate        health: ok   state: inactive
HighLatency          health: ok   state: inactive
ModelNotReady        health: ok   state: inactive
InferenceUnavailable health: ok   state: inactive
ReplicaUnavailable   health: ok   state: inactive
```

All five load into live Prometheus with `health: ok`.

## SLOs

Defined in [`docs/slo.md`](../../slo.md), frozen from measured numbers: 99.5%
availability (100% observed in every drill so far), p95 < 100ms (4.99ms at
rest, 32.9ms saturated). No burn-rate alerting yet — noted as a gap, not
silently skipped.

## Gate result

| Check | Result |
| --- | --- |
| metrics-server | PASS |
| HPA scales up under load | PASS (2→6 in 71s, measured) |
| HPA scales down after load | PASS (6→2 in ~230s, stepped) |
| 0 request failures during scale events | PASS (645,809/645,809) |
| PodDisruptionBudget honoured during a real drain | PASS (60/60 requests OK) |
| alert rules unit-tested | PASS (5/5, `promtool test rules`) |
| alert rules loaded into live Prometheus | PASS (`health: ok` ×5) |
| SLOs defined from measured data | PASS |

## Not done

- No burn-rate / multi-window alerting — flat thresholds only.
- No PDB on PostgreSQL/MinIO — single replica each, so `minAvailable` would be
  meaningless; the drain finding above documents the actual behaviour instead.
- `HighErrorRate`/`HighLatency` were unit-tested but never fired against real
  traffic (the load test stayed well under both thresholds by design — the
  point was to prove scaling, not to trip an alert).
