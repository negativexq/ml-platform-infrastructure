# Failure engineering

Every row below is a fault injected on purpose against the running local
platform — a stopped container, a deleted PVC, a drained node, a bad image
tag — with the actual detection signal and recovery path that followed. None
of this is simulated; each links to the drill transcript that produced it.

## Failure → detection → recovery

| Failure | Detection | Containment | Recovery | Evidence |
| --- | --- | --- | --- | --- |
| Pod crash | ReplicaSet controller | surviving replica serves | replacement ready 12–15 s | [M2](evidence/m2/pod-recovery.md) |
| Invalid model artifact | readiness 503, `model_ready=0` | pod kept out of endpoints, rollout stalls | Git revert | [M5](evidence/m5/invalid-artifact.md) |
| Artifact store outage | `model_load_failures_total` rising | 100% of requests still 200 | background recheck, 0 restarts | [M5](evidence/m5/artifact-store-outage.md) |
| Config drift | Argo resource watch → `OutOfSync` | Git holds desired state | self-heal ~1.4 s | [M4](evidence/m4/argocd-drift-reconciliation.md) |
| Latency regression | `http_request_duration_seconds` p95 | **none automatic** — degraded but serving | Git revert | [M5](evidence/m5/latency-regression.md) |
| Bad rollout | `ImagePullBackOff`, Argo stuck `Progressing` | `maxUnavailable: 0` holds capacity | Git revert, ReplicaSet pruned | [M5](evidence/m5/bad-rollout.md) |
| Volume loss | — | — | full backup/restore, registry identical after | [M8](evidence/m8/gate.md) |
| Node drain (stateful pod on it) | eviction event | surviving inference pod absorbs traffic | Postgres/MinIO reschedule automatically | [M10](evidence/m10/gate.md) |

## Real defects this project found in itself

Not written around — found by running the automation (or, in one case, by
someone asking a question about it), then fixed:

- **Blocking startup** ([M1](evidence/m1/blocking-startup-defect.md)) —
  model loading ran inside the FastAPI lifespan hook, so a slow artifact store
  made the container answer nothing, not even `/health`. In Kubernetes that
  turns a dependency outage into a CrashLoopBackOff. Moved to a background
  thread.
- **A dropped request per rolling update** ([M2](evidence/m2/rolling-update.md))
  — `kubectl rollout status` reported success while an external probe measured
  1 failure in 90. Fixed with a `preStop` drain; re-measured 120/120, then
  120/120 again under HPA in M10.
- **An empty dashboard panel** (M5, recurring in [M11](evidence/m11/gate.md))
  — a `status=~"5.."`-filtered counter emits no series at all until the first
  5xx ever happens, so "zero errors" and "not instrumented" looked identical
  on a fresh dashboard. Fixed twice — once for `prediction_errors_total` in
  M5, once for the dashboard's own error-rate panel in M11 — with the same
  `or vector(0)` pattern.
- **A security drill that broke itself** ([M11](evidence/m11/gate.md)) —
  M9 turned on Pod Security Standards `restricted`; M9's own NetworkPolicy
  drill used plain probe pods that PSS then rejected outright, and the script
  read every admission failure as a network `DENY` — so every "should ALLOW"
  edge silently reported the wrong thing while looking like it passed. Only
  caught because M11 re-ran the drill against a truly fresh cluster instead of
  trusting an earlier milestone's result to still hold.
- **The MLflow security/memory trade** ([M9](evidence/m9/gate.md)) — M7's
  MLflow 2.19 (chosen for a 297 MiB footprint) turned out to carry 7 unpatched
  CRITICAL CVEs; every fix lands only in ≥3.15, which has a measured 1.46 GiB
  floor. Paid the memory rather than ship known-critical software in a
  security milestone.
- **CI silently broken for four milestones** ([M9 correction](evidence/m9/gate.md#correction-added-2026-09-11))
  — M9 added a `trivy-action@0.28.0` pin missing the `v` prefix the tag
  actually needs; the `image-scan` GitHub Actions job failed at "set up job"
  on every push from M9 through M12 without anyone checking `gh run list`.
  The gate table had marked it `PASS` based on a real local script run, while
  the GitHub Actions job itself never once executed. Not found by a drill —
  found because the repo owner asked. Fixed by pinning `v0.36.0`.
