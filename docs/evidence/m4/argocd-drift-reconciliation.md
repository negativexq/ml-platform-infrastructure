# M4 — Drift detection and self-heal

**Question:** if someone changes the cluster by hand, does Git win?

## Setup

Argo CD v3.5.2 in the kind cluster. Application `inference-local` tracks
`helm/ml-platform` at `main` in
`github.com/negativexq/ml-platform-infrastructure` (private), with
`automated: {prune: true, selfHeal: true}`.

The Helm CLI release was uninstalled first — deployment authority now belongs
to Git, not to a laptop.

## Drift 1 — scale down by hand

Git says `replicaCount: 2`.

```
$ kubectl -n ml-platform scale deployment ml-platform-inference --replicas=1
t+1s  argo=Synced     spec.replicas=1
t+5s  argo=Synced     spec.replicas=2      <- healed
t+9s  argo=Synced     spec.replicas=2
```

Healed inside ~5s. The polling here (every 4s) was too coarse to catch the
`OutOfSync` state, so the run below samples faster.

## Drift 2 — change the image by hand, sampled every 400ms

```
$ kubectl -n ml-platform set image deployment/ml-platform-inference \
    inference=ml-platform-inference:v2

t+362ms   argo=Synced     image=v2     <- drift applied, Argo hasn't noticed yet
t+914ms   argo=OutOfSync  image=dev    <- detected, and already reverting
t+1427ms  argo=Synced     image=dev    <- reconciled to Git
t+1934ms  argo=Synced     image=dev
t+2443ms  argo=Synced     image=dev
```

The full `OutOfSync → Synced` transition, with the cluster returned to the
image Git asked for, in **under 1.5 seconds**. Manual changes to live
resources are watched, not polled — which is why this is orders of magnitude
faster than picking up a Git commit (below).

## Timeline

| Event | Detection | Containment | Recovery |
| --- | --- | --- | --- |
| `kubectl scale --replicas=1` | Argo resource watch | Git holds desired state | replicas back to 2, ~5s |
| `kubectl set image ...:v2` | Argo resource watch → `OutOfSync` | self-heal reverts the live object | image back to `dev`, ~1.4s |

## What this does not prove

Self-heal reverts *drift against tracked fields*. It does not prevent someone
with cluster access from deleting the Application itself, nor does it protect
resources the chart does not manage.
