# F3 — Artifact store outage

**Question:** MinIO goes away and a pod is rescheduled. Does the platform
degrade gracefully, or does an artifact-store outage turn into a restart storm?

## Method

MinIO stopped, then a pod deleted so its replacement had to fetch the artifact
with the store down.

## Observed

```
=== stopping MinIO (artifact store) ===
deleting ml-platform-inference-86bf967968-lkz6m so it must re-fetch the artifact
t+0s    availableReplicas=2  service/ready=200  service/predict=200
t+8s    availableReplicas=1  service/ready=200  service/predict=200
...
t+106s  availableReplicas=1  service/ready=200  service/predict=200

ml-platform-inference-86bf967968-9mbrr   0/1   Running   0   114s   <- new, stuck
ml-platform-inference-86bf967968-sk6x9   1/1   Running   0   3m56s  <- serving
```

Metrics during the outage:

```
model_ready
  ml-platform-inference-86bf967968-9mbrr   0
  ml-platform-inference-86bf967968-sk6x9   1

model_load_failures_total
  ml-platform-inference-86bf967968-9mbrr   2
  ml-platform-inference-86bf967968-sk6x9   0

kube_pod_container_status_restarts_total
  ml-platform-inference-86bf967968-9mbrr   0
  ml-platform-inference-86bf967968-sk6x9   0
```

**Zero restarts.** The blocked pod stayed up, reported itself not-ready, and
was kept out of the Service. Capacity halved; correctness did not.

## Recovery — no restart needed

MinIO started again:

```
t+0s   availableReplicas=1
t+6s   availableReplicas=1
t+12s  availableReplicas=2

model_ready                                1, 1
kube_pod_container_status_restarts_total   0, 0
smoke test PASSED
```

Recovered in **12s** via the background recheck loop
(`ML_MODEL_LOAD_RECHECK_SECONDS=15`) — the same pod, never restarted. This is
the M1 fix ([../m1/blocking-startup-defect.md](../m1/blocking-startup-defect.md))
paying off a third time.

| | |
| --- | --- |
| Detection | `model_ready=0`, `model_load_failures_total` rising, readiness probe 503 |
| Containment | pod excluded from Service endpoints; surviving replica serves 100% |
| Recovery | background recheck loop, 12s after the store returned, 0 restarts |
| Evidence | metric dumps above |

## Honest limitation

With a single replica, or if the outage had outlasted every pod, the service
would have gone down — nothing here creates availability out of nothing. What
the platform guarantees is that an artifact-store outage costs *capacity*, not
*correctness*, and does not amplify itself into a crash loop.
