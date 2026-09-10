# M2 — Rolling update, and the request it dropped

**Question:** does a rolling update maintain availability? Measured, not
assumed — by firing requests at the Service continuously through the rollout
and counting outcomes.

## First attempt — one request lost

Probe: `POST /predict` once a second for 90s, `--max-time 2`, while the image
was rolled `dev` → `v2`.

```
$ kubectl set image deployment/inference inference=ml-platform-inference:v2
deployment "inference" successfully rolled out

=== request outcomes during the rollout ===
   1 000      <- connection failed
  89 200
```

`kubectl rollout status` reported success, and `readyReplicas` never dropped —
yet a client request still failed. The Deployment's own signals said the
rollout was clean; only the external probe disagreed.

## Cause

Endpoint removal and container shutdown race. When a pod is deleted, kube-proxy
updating its rules and the container receiving SIGTERM happen concurrently — so
a connection can still be routed to a pod that has already begun shutting down.
`maxUnavailable: 0` does not help here; it governs replica counts, not
in-flight connections.

## Fix

A `preStop` hook that keeps the pod serving while its endpoint drains, plus an
explicit grace period ([`k8s/base/deployment.yaml`](../../../k8s/base/deployment.yaml)):

```yaml
terminationGracePeriodSeconds: 30
lifecycle:
  preStop:
    exec:
      command: ["sleep", "8"]
```

## Re-measured

Probe: `POST /predict` every 0.5s for 60s across the same rollout.

```
deployment "inference" successfully rolled out

=== request outcomes during rolling update (with preStop) ===
 120 200
```

## Result

| | |
| --- | --- |
| Detection | external request probe (not `rollout status`, which reported success either way) |
| Containment | `maxUnavailable: 0` + `maxSurge: 1` — capacity never dips below 2 |
| Recovery | `preStop` drain hook; re-measured 120/120 successful |
| Evidence | request-outcome counts above |

Worth stating plainly: without the client-side probe this defect would have
been invisible, and the README could honestly have claimed "zero-downtime
rolling updates" while dropping requests.
