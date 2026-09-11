# M11 Gate — evidence

Run date: 2026-09-11. This is the real thing, not a description of it: the
kind cluster, every Docker image, and the build cache were destroyed first.

```
$ ./scripts/kind-down.sh && docker builder prune -af && docker image prune -af
Deleting cluster "ml-platform" ...
Total reclaimed: 4.3GB
Images: 6 remaining (none of them ml-platform-*)
```

Then, from the repo checkout alone:

```
$ make local-up      # ./scripts/local-up.sh
...
==========================================
 bootstrap complete in 901s
   inference : http://localhost:30080
   MLflow UI : http://localhost:30500
==========================================

$ make local-test    # ./scripts/local-test.sh
-- GitOps --
  OK    inference-local Application Synced/Healthy
  OK    platform-local Application Synced/Healthy
-- Service contract --
  OK    smoke test (/health, /ready, /predict, replicas)
-- ML lifecycle --
  OK    integration tests (MLflow, registry, prediction)
-- Persistence (M8) --
  OK    pod-delete drill: data survives
-- Security (M9) --
  OK    NetworkPolicy allow/deny matrix
  OK    namespace enforces Pod Security Standards: restricted
-- Scaling & alerting (M10) --
  OK    HPA present (min/max: 2/6)
  OK    PodDisruptionBudget present (minAvailable: 1)
  OK    alert rules unit tests
  OK    PrometheusRule applied
-- Observability (M5) --
  OK    dashboard queries return live data

LOCAL ACCEPTANCE: PASSED
```

**901 seconds (~15 minutes)** from nothing to a fully working, GitOps-managed,
observed, autoscaling, security-hardened platform. **11/11** acceptance checks
pass.

## What `local-up.sh` actually does, in order

```
1. kind cluster (3 nodes)
2. build + load 3 images (inference, mlflow, training) — no registry needed
3. namespace with Pod Security Standards: restricted
4. Argo CD installed; both Applications (platform-local, inference) applied
   from Git; wait for platform-local Healthy
5. training Job seeds the model registry; inference restarted; wait for
   inference-local Healthy
6. kube-prometheus-stack + dashboard + PrometheusRule + metrics-server
```

No step assumes a resource that a prior run of this script (or a human) left
behind. `docker/mlflow`, `docker/training` and the top-level `Dockerfile` are
the only image sources — everything else is `kubectl apply` / `helm` against
what Git already holds.

## Two real bugs this run found

This is the actual value of M11: running the automation for real, from zero,
surfaces interactions no single milestone's own testing would have hit.

### 1. The M9 NetworkPolicy drill broke itself by enabling M9

`scripts/drills/network-policy.sh`'s probe pods were plain `busybox` with no
`securityContext` — fine under M8's cluster, **rejected outright** once M9
turned on `pod-security.kubernetes.io/enforce: restricted` on the namespace:

```
Error from server (Forbidden): pods "np-inf-mlflow" is forbidden: violates
PodSecurity "restricted:latest": allowPrivilegeEscalation != false, ...
```

The script's `if kubectl run ...; then ALLOW; else DENY; fi` treated that
admission rejection exactly like a network timeout — so **every "should
ALLOW" edge silently reported DENY**, and the drill "passed" by accident
(the DENY edges were still correctly DENY, so a careless read looked fine).
Caught only because M11 re-ran the drill against a fresh, fully-hardened
cluster instead of trusting the M9 run's result to still hold.

Fixed: probe pods now carry a PSS-`restricted`-compliant `securityContext`,
and the script explicitly greps for `Forbidden`/`violates PodSecurity` and
reports that as its own `ERROR` state — distinct from a network `DENY` —
so an admission failure can never again read as a passing test.

```
$ ./scripts/drills/network-policy.sh
NetworkPolicy matrix (5 policies active)
   OK   ml-platform→platform-mlflow    :5000  -> ALLOW
   OK   ml-platform→platform-minio     :9000  -> ALLOW
   OK   ml-platform→platform-postgres  :5432  -> DENY
   OK   train→platform-mlflow          :5000  -> ALLOW
   OK   train→platform-postgres        :5432  -> DENY
   OK   mlflow→platform-postgres       :5432  -> ALLOW
   OK   mlflow→platform-minio          :9000  -> ALLOW
   OK   →platform-postgres             :5432  -> DENY
   OK   →platform-minio                :9000  -> DENY
all edges match the intended matrix
```

### 2. The "Error rate" dashboard panel was blank on a fresh deploy

`sum(rate(http_requests_total{status=~"5.."}[5m]))` returns **no series at
all** — not zero — when no 5xx response has ever happened, because the
`status="5xx"` label combination has literally never been created. Dividing
"no series" by a real number in PromQL drops the whole expression, so the
panel showed nothing on a brand-new, error-free cluster — exactly the
situation this repo asks readers to trust the dashboards in.

```
1 query/queries returned nothing:
  - Error rate (5xx share): no series
```

Fixed with the same pattern M5 already used for `prediction_errors_total`:
wrap the possibly-absent side in `or vector(0)`:

```promql
(sum(rate(http_requests_total{status=~"5.."}[5m])) or vector(0))
  / clamp_min(sum(rate(http_requests_total{...}[5m])), 0.0001)
```

Re-verified: `every dashboard query returns data`.

## A transient, non-bug observation

Right after `kubectl rollout restart` (an imperative action outside Argo)
completed, `inference-local`'s Argo health briefly read `Degraded` while every
individual resource's health showed `None` — even though the Deployment
itself was `Available: True`, `2/2/2/2`. It resolved to `Healthy` on its own
within about a minute (confirmed via a hard refresh). Recorded here because it
looked alarming enough to chase, and the conclusion — a benign health-cache
lag after a direct kubectl mutation, not a defect — is worth having on record
rather than re-discovering next time.

## Gate result

| Check | Result |
| --- | --- |
| fresh cluster, no leftover Docker state | PASS (verified: images/cache pruned first) |
| one command brings up the full platform | PASS (`make local-up`, 901s) |
| one command runs the acceptance suite | PASS (`make local-test`, 11/11) |
| no manually created resource required | PASS — only `kubectl`/`helm`/`docker build` against the repo |
| real defects found and fixed | PASS (2, both documented above with root cause) |

## Not done

- CI does not yet run `local-up` + `local-test` — the roadmap accepts this
  (kind-in-CI is the "optional/manual" tier); it is a candidate follow-up.
- The disruptive/slow drills (k6 autoscaling ramp, full backup-restore wipe,
  a real node drain) are not part of `local-test.sh` — they're each a
  multi-minute, occasionally destructive action, wrong for a suite meant to be
  re-run often. Their own evidence lives in M8/M10.
