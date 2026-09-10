# ML Platform Infrastructure

A containerized inference service, hardened locally through six milestones and
then designed for AWS as code. Every claim below links to the transcript of the
run that produced it — commands, output, and the numbers actually measured.

## Milestone status

| Milestone | | Status | Evidence |
| --- | --- | --- | --- |
| M0 | Application & repository foundation | ✅ | [gate](docs/evidence/m0/gate.md) |
| M1 | Local ML lifecycle (MLflow + PostgreSQL + MinIO) | ✅ | [gate](docs/evidence/m1/gate.md) |
| M2 | Local Kubernetes with kind | ✅ | [gate](docs/evidence/m2/gate.md) |
| M3 | Helm packaging | ✅ | [gate](docs/evidence/m3/gate.md) |
| M4 | GitOps with Argo CD | ✅ | [gate](docs/evidence/m4/gate.md) |
| M5 | Observability & failure engineering | ✅ | [gate](docs/evidence/m5/gate.md) |
| M6 | Terraform & AWS migration design | ✅ | [gate](docs/evidence/m6/gate.md) |

Plan and gates: [`docs/roadmap.md`](docs/roadmap.md) ·
architecture: [`docs/architecture.md`](docs/architecture.md) ·
AWS design: [`docs/aws-architecture.md`](docs/aws-architecture.md) ·
cost: [`docs/cost-model.md`](docs/cost-model.md)

## Architecture

```
                       ┌──────────── Git (source of truth) ────────────┐
                       │  helm/ml-platform/  gitops/  infra/terraform/ │
                       └───────────────────────┬───────────────────────┘
                                               │ poll
                                        ┌──────▼──────┐
                                        │   Argo CD   │ auto-sync, self-heal
                                        └──────┬──────┘
                                               │
   ┌───────────────────────── kind cluster ────▼──────────────────────────┐
   │                                                                      │
   │   Service ──> inference (2 replicas)      Prometheus ──> Grafana     │
   │                    │  /health /ready         ▲                       │
   │                    │  /predict /metrics ─────┘                       │
   └────────────────────┼─────────────────────────────────────────────────┘
                        │ model artifact at startup
        ┌───────────────▼────────────────────────────────┐
        │  MLflow ──> PostgreSQL (metadata)              │  Docker Compose
        │         └─> MinIO      (artifacts)             │
        └────────────────────────────────────────────────┘
```

## Measured behaviour

Not estimates — read from Prometheus or a poll transcript during a drill:

| | |
| --- | --- |
| `/predict` p95 latency | **4.99 ms** |
| Model predict call p95 | **2.78 ms** |
| Cold-start model load (MLflow → MinIO → memory) | **3.98 s** / **5.26 s** |
| Pod deleted → replacement serving | **12 s**, Service never non-200 |
| Artifact store returns → pod recovers | **12 s**, **0 restarts** |
| Manual drift → reconciled by Argo | **~1.4 s** |
| Git commit → rolled out by Argo | **163 s** (default 3 min poll) |
| Rolling update, with `preStop` drain | **120/120** requests OK |

## Failure engineering

| Failure | Detection | Containment | Recovery | Evidence |
| --- | --- | --- | --- | --- |
| Pod crash | ReplicaSet controller | surviving replica serves | replacement ready 12 s | [m2](docs/evidence/m2/pod-recovery.md) |
| Invalid model artifact | readiness 503, `model_ready=0` | pod kept out of endpoints, rollout stalls | Git revert | [m5](docs/evidence/m5/invalid-artifact.md) |
| Artifact store outage | `model_load_failures_total` rising | 100% of requests still 200 | background recheck, 0 restarts | [m5](docs/evidence/m5/artifact-store-outage.md) |
| Config drift | Argo resource watch → `OutOfSync` | Git holds desired state | self-heal ~1.4 s | [m4](docs/evidence/m4/argocd-drift-reconciliation.md) |
| Latency regression | `http_request_duration_seconds` p95 | **none automatic** — degraded but serving | Git revert | [m5](docs/evidence/m5/latency-regression.md) |
| Bad rollout | `ImagePullBackOff`, Argo stuck `Progressing` | `maxUnavailable: 0` holds capacity | Git revert, ReplicaSet pruned | [m5](docs/evidence/m5/bad-rollout.md) |

Three defects were found by these drills and fixed, rather than written around:

- **Blocking startup** — model loading ran inside the FastAPI lifespan hook, so
  a slow artifact store made the container answer nothing at all, not even
  `/health`. In Kubernetes that turns a dependency outage into a
  CrashLoopBackOff. Loading moved to a background thread.
  [Detail](docs/evidence/m1/blocking-startup-defect.md)
- **A dropped request per rolling update** — `kubectl rollout status` reported
  success while an external probe measured 1 failure in 90. Endpoint removal
  races container shutdown; fixed with a `preStop` drain, re-measured 120/120.
  [Detail](docs/evidence/m2/rolling-update.md)
- **An empty dashboard panel** — `prediction_errors_total` exported no series
  until its first increment, so "no errors" was indistinguishable from "not
  instrumented". Label sets pre-initialised.
  [Detail](docs/evidence/m5/gate.md)

## Quickstart

```bash
make install          # app + dev + training deps
make platform-up      # PostgreSQL + MinIO + MLflow + inference (compose)
make train            # tracked run, registers a model version
make check            # ruff + mypy + pytest
```

Kubernetes:

```bash
make kind-up          # cluster, image load, manifests
make helm-deploy      # or: install via Helm
make argocd-up        # hand deployment authority to Git
make observability-up # Prometheus + Grafana + dashboards
make smoke            # end-to-end check through the Service
```

Terraform (design only — nothing is applied):

```bash
make tf-check         # fmt + validate + tflint
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
pod get traffic". Conflating them is what turns an artifact-store outage into a
restart storm.

## Repository layout

```
app/                  FastAPI inference service + metrics
scripts/              training, cluster bootstrap, smoke tests, drill helpers
tests/                unit + integration
helm/ml-platform/     one chart, values-local.yaml / values-aws-dev.yaml
k8s/                  raw manifests from M2, kept as the pre-Helm baseline
gitops/               Argo CD Application
observability/        kube-prometheus-stack values + Grafana dashboards
infra/terraform/      modules/{vpc,ecr,s3,rds,eks,iam} + environments/aws-dev
docs/evidence/m0..m6/ transcripts behind every claim above
```

## What this does not do

- **The AWS side has never been applied.** `fmt`, `validate` and `tflint` pass;
  `terraform plan` needs credentials and has not run.
- **No alert rules.** Prometheus and Alertmanager are installed and the
  evidence names what the rules should fire on, but none are written.
- **No tracing.** OpenTelemetry was optional in the roadmap and skipped.
- **MLflow is not in the cluster.** It stays in Docker Compose; the AWS design
  provisions RDS, S3 and its IAM role, but not the deployment.
- **Single-node local platform.** No HA anywhere, by design and by budget.
