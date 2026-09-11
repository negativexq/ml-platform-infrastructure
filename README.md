# ML Platform Infrastructure

A containerized inference service, hardened locally through twelve milestones,
then designed for AWS as code. Every claim below links to the transcript of
the run that produced it — commands, output, and the numbers actually
measured. Nothing in this README was written before the evidence that backs
it.

**Status: `local-v1.0.0` — local development is frozen.** AWS work (M13+)
swaps each local dependency for its managed counterpart and re-runs the same
gates; it does not touch what's below.

## Try it

```bash
git clone https://github.com/negativexq/ml-platform-infrastructure.git
cd ml-platform-infrastructure
make local-up      # fresh kind cluster → GitOps → ML lifecycle → observability, ~15 min
make local-test    # 11-check acceptance suite
make local-down    # tear it all down
```

No manual step, no pre-existing cluster resource, no registry. Proved for
real in [M11](docs/evidence/m11/gate.md): cluster, images, and build cache
destroyed first, then `local-up` → `local-test` from the repo alone.

## Milestone status

| Milestone | | Evidence |
| --- | --- | --- |
| M0 | Containerized inference service | [gate](docs/evidence/m0/gate.md) |
| M1 | MLflow + PostgreSQL + MinIO lifecycle | [gate](docs/evidence/m1/gate.md) |
| M2 | Kubernetes with kind | [gate](docs/evidence/m2/gate.md) |
| M3 | Helm packaging | [gate](docs/evidence/m3/gate.md) |
| M4 | GitOps with Argo CD | [gate](docs/evidence/m4/gate.md) |
| M5 | Observability & failure engineering | [gate](docs/evidence/m5/gate.md) |
| M6 | Terraform & AWS migration design | [gate](docs/evidence/m6/gate.md) |
| M7 | Full local Kubernetes platform (no Compose) | [gate](docs/evidence/m7/gate.md) |
| M8 | Stateful persistence & recovery | [gate](docs/evidence/m8/gate.md) |
| M9 | Security hardening | [gate](docs/evidence/m9/gate.md) |
| M10 | Scaling, SLO & alerting | [gate](docs/evidence/m10/gate.md) |
| M11 | End-to-end reproducibility | [gate](docs/evidence/m11/gate.md) |
| M12 | Local Release Candidate — this freeze | [gate](docs/evidence/m12/gate.md) |
| M13+ | AWS (not started — no cloud resource has been created) | — |

Plan and per-milestone detail: [`docs/roadmap.md`](docs/roadmap.md) ·
architecture: [`docs/architecture.md`](docs/architecture.md) ·
SLOs: [`docs/slo.md`](docs/slo.md) ·
AWS design: [`docs/aws-architecture.md`](docs/aws-architecture.md) ·
cost: [`docs/cost-model.md`](docs/cost-model.md)

## Architecture

```
                       ┌──────────── Git (source of truth) ────────────┐
                       │  helm/*/  gitops/  infra/terraform/           │
                       └───────────────────────┬───────────────────────┘
                                               │ poll / self-heal
                                        ┌──────▼──────┐
                                        │   Argo CD   │
                                        └──────┬──────┘
                                               │
   ┌───────────────────────── kind cluster (PSS: restricted) ─────────────┐
   │                                                                      │
   │  Service ──> inference (HPA 2-6, PDB min 1)   Prometheus ──> Grafana │
   │       NetworkPolicy: default-deny + allow-list      ▲               │
   │                    │  /health /ready                 │               │
   │                    │  /predict /metrics ──────────────┘               │
   │                    │                                                 │
   │                    ▼ (NetworkPolicy: MLflow/MinIO only, no Postgres) │
   │       platform-local: MLflow ──> PostgreSQL (PVC)                    │
   │                            └──> MinIO      (PVC)                     │
   └────────────────────────────────────────────────────────────────────┘
```

Everything in the cluster; nothing in Docker Compose (that split was closed
in [M7](docs/evidence/m7/gate.md)).

## Evidence matrix

Every capability claim, with the run that proves it.

| Capability | Evidence |
| --- | --- |
| ML lifecycle — Postgres metadata + MinIO artifacts, in-cluster | [M7](docs/evidence/m7/gate.md) |
| Serving — MLflow artifact → inference, cross-version verified | [M1](docs/evidence/m1/gate.md), [M7](docs/evidence/m7/gate.md) |
| Health semantics — non-blocking health/readiness | [M1 defect + fix](docs/evidence/m1/blocking-startup-defect.md) |
| Kubernetes recovery — measured pod recovery | [M2](docs/evidence/m2/pod-recovery.md) |
| Rolling update — measured zero-drop, after fixing a real 1-in-90 drop | [M2](docs/evidence/m2/rolling-update.md) |
| GitOps — OutOfSync → Synced, sub-2s | [M4](docs/evidence/m4/argocd-drift-reconciliation.md) |
| Observability — live Prometheus/Grafana queries, every panel verified | [M5](docs/evidence/m5/gate.md) |
| Failure engineering — 6 drills, 3 real defects found and fixed | [M5](docs/evidence/m5/gate.md) |
| Persistence — pod-delete + destructive backup/restore | [M8](docs/evidence/m8/gate.md) |
| Security — NetworkPolicy allow/deny (9 edges), PSS `restricted` enforced | [M9](docs/evidence/m9/gate.md) |
| Scaling — measured HPA (2→6→2) under real load, PDB honoured through a real drain | [M10](docs/evidence/m10/gate.md) |
| Alerting — 5 rules, unit-tested with `promtool`, loaded into live Prometheus | [M10](docs/evidence/m10/gate.md) |
| Reproducibility — fresh-cluster acceptance, 2 bugs it found and fixed | [M11](docs/evidence/m11/gate.md) |
| AWS design — Terraform validated/linted, nothing applied | [M6](docs/evidence/m6/gate.md) |

## Measured behaviour

Not estimates — read from Prometheus or a drill transcript:

| | |
| --- | --- |
| `/predict` p95, at rest | **4.99 ms** |
| `/predict` p95, saturated (6 pods, 2,935 req/s) | **32.9 ms** |
| Cold-start model load (MLflow → MinIO → memory) | **3.98 s** / **5.26 s** |
| Pod deleted → replacement serving | **12–15 s**, Service never non-200 |
| HPA scale-up under load | **2 → 6 replicas in 71 s** |
| HPA scale-down after load | **6 → 2 over ~230 s**, stepped |
| Node drain with a stateful pod on it | **60/60 requests still 200** |
| Argo drift → reconciled | **~1.4 s** |
| Argo Git-commit pickup | **163 s** (default 3 min poll) |
| Fresh-cluster bootstrap → 11/11 acceptance | **901 s** |

## Failure engineering

| Failure | Detection | Containment | Recovery | Evidence |
| --- | --- | --- | --- | --- |
| Pod crash | ReplicaSet controller | surviving replica serves | replacement ready 12–15 s | [M2](docs/evidence/m2/pod-recovery.md) |
| Invalid model artifact | readiness 503, `model_ready=0` | pod kept out of endpoints, rollout stalls | Git revert | [M5](docs/evidence/m5/invalid-artifact.md) |
| Artifact store outage | `model_load_failures_total` rising | 100% of requests still 200 | background recheck, 0 restarts | [M5](docs/evidence/m5/artifact-store-outage.md) |
| Config drift | Argo resource watch → `OutOfSync` | Git holds desired state | self-heal ~1.4 s | [M4](docs/evidence/m4/argocd-drift-reconciliation.md) |
| Latency regression | `http_request_duration_seconds` p95 | **none automatic** — degraded but serving | Git revert | [M5](docs/evidence/m5/latency-regression.md) |
| Bad rollout | `ImagePullBackOff`, Argo stuck `Progressing` | `maxUnavailable: 0` holds capacity | Git revert, ReplicaSet pruned | [M5](docs/evidence/m5/bad-rollout.md) |
| Volume loss | — | — | full backup/restore, registry identical after | [M8](docs/evidence/m8/gate.md) |
| Node drain (stateful pod on it) | eviction event | surviving inference pod absorbs traffic | Postgres/MinIO reschedule automatically | [M10](docs/evidence/m10/gate.md) |

## Real defects this project found in itself

Not written around — found by running the automation, then fixed:

- **Blocking startup** ([M1](docs/evidence/m1/blocking-startup-defect.md)) —
  model loading ran inside the FastAPI lifespan hook, so a slow artifact store
  made the container answer nothing, not even `/health`. In Kubernetes that
  turns a dependency outage into a CrashLoopBackOff. Moved to a background
  thread.
- **A dropped request per rolling update** ([M2](docs/evidence/m2/rolling-update.md))
  — `kubectl rollout status` reported success while an external probe measured
  1 failure in 90. Fixed with a `preStop` drain; re-measured 120/120, then
  120/120 again under HPA in M10.
- **An empty dashboard panel** (M5, recurring in [M11](docs/evidence/m11/gate.md))
  — a `status=~"5.."`-filtered counter emits no series at all until the first
  5xx ever happens, so "zero errors" and "not instrumented" looked identical
  on a fresh dashboard. Fixed twice — once for `prediction_errors_total` in
  M5, once for the dashboard's own error-rate panel in M11 — with the same
  `or vector(0)` pattern.
- **A security drill that broke itself** ([M11](docs/evidence/m11/gate.md)) —
  M9 turned on Pod Security Standards `restricted`; M9's own NetworkPolicy
  drill used plain probe pods that PSS then rejected outright, and the script
  read every admission failure as a network `DENY` — so every "should ALLOW"
  edge silently reported the wrong thing while looking like it passed. Only
  caught because M11 re-ran the drill against a truly fresh cluster instead of
  trusting an earlier milestone's result to still hold.
- **The MLflow security/memory trade** ([M9](docs/evidence/m9/gate.md)) — M7's
  MLflow 2.19 (chosen for a 297 MiB footprint) turned out to carry 7 unpatched
  CRITICAL CVEs; every fix lands only in ≥3.15, which has a measured 1.46 GiB
  floor. Paid the memory rather than ship known-critical software in a
  security milestone.

## Quickstart (piece by piece)

```bash
make install           # app + dev + training deps
make kind-up           # cluster, image load, platform-local, train, inference
make argocd-up         # hand deployment authority to Git
make observability-up  # Prometheus + Grafana + dashboards
make alert-rules-apply # PrometheusRule, tested with promtool first
make smoke             # end-to-end check through the Service
make security-scan     # kubeconform + trivy (fixable-only gate) + secret scan
make drill-netpol      # NetworkPolicy allow/deny, 9 edges
make drill-autoscale   # k6 load + HPA scale-up/down (~6 min)
make drill-drain       # PDB honoured through a real node drain
```

Terraform (design only — nothing is ever applied):

```bash
make tf-check          # fmt + validate + tflint
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness — process is serving. Never depends on the artifact store. |
| GET | `/ready` | Readiness — `200` when a model is loaded, `503` otherwise, with the reason |
| POST | `/predict` | `{"features": [...]}` → `{"prediction": float, "model_version": str}` |
| GET | `/metrics` | Prometheus exposition |

The split between the first two is the load-bearing design decision in this
repo: liveness answers "is the process alive", readiness answers "should this
pod get traffic". Conflating them is what turns an artifact-store outage into
a restart storm.

## Repository layout

```
app/                    FastAPI inference service + metrics
scripts/                bootstrap, drills, backup/restore, load test
scripts/drills/         network-policy, persistence, backup-restore, autoscaling, node-drain
tests/                  unit + integration
helm/ml-platform/       the application chart — values-local.yaml / values-aws-dev.yaml
helm/platform-local/    PostgreSQL + MinIO + MLflow — local-only, never applied to AWS
k8s/                    raw manifests from M2 (pre-Helm baseline) + training/backup Jobs
gitops/                 Argo CD Applications (platform-local, inference)
observability/          kube-prometheus-stack values, Grafana dashboard, alert rules (+ promtool tests)
infra/terraform/        modules/{vpc,ecr,s3,rds,eks,iam} + environments/aws-dev — never applied
docs/evidence/m0..m12/  the transcript behind every claim in this file
```

## What this does not do

- **AWS has never been applied.** `fmt`, `validate` and `tflint` pass;
  `terraform plan` needs credentials and has not run.
- **No burn-rate alerting.** `docs/slo.md`'s error budget isn't tracked yet —
  flat thresholds only.
- **No PDB on PostgreSQL/MinIO.** Single replica each; M10's drain drill
  documents the actual (fine) behaviour instead of adding a PDB that would be
  meaningless at replica count 1.
- **CI doesn't run `local-up`/`local-test`.** They're proven manually
  (M11); kind-in-CI is future work, not silently skipped.
- **Single-node-each local platform.** No HA anywhere, by design and by
  budget — AWS (M13+) replaces PostgreSQL/MinIO with RDS/S3, which do have it.
