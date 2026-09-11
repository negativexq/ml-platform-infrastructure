# Architecture

## Current state (M7+)

Every platform dependency runs inside the kind cluster; nothing is left in
Docker Compose (that split was closed in
[M7](evidence/m7/gate.md)).

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

- **inference** — FastAPI service, autoscaled 2–6 replicas by an HPA on CPU,
  protected by a PodDisruptionBudget (`minAvailable: 1`). Reaches only MLflow
  and MinIO under NetworkPolicy — not PostgreSQL directly.
- **platform-local** — MLflow, PostgreSQL and MinIO, PVC-backed
  ([M8](evidence/m8/gate.md)), local-only and never applied to AWS.
- **Argo CD** — two Applications (`platform-local`, `inference-local`) poll
  Git and self-heal live drift ([M4](evidence/m4/argocd-drift-reconciliation.md)).
- **Prometheus / Grafana** — scrape `inference`'s `/metrics`; alerting rules
  are unit-tested with `promtool` before being loaded ([M10](evidence/m10/gate.md)).

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness — process is serving. Never depends on the artifact store. |
| GET | `/ready` | Readiness — `200` when a model is loaded, `503` otherwise, with the reason |
| POST | `/predict` | `{"features": [...]}` → `{"prediction": float, "model_version": str}` |
| GET | `/metrics` | Prometheus exposition |

The split between the first two is the load-bearing design decision in this
repo:

- **liveness** (`/health`) answers "is the process alive" — it must stay
  `200` even while a model is loading or an artifact store is unreachable.
- **readiness** (`/ready`) answers "should this pod receive traffic" — it is
  the only thing allowed to say `503`.

Conflating them is what turns an artifact-store outage into a restart storm:
if liveness depended on the model being loaded, Kubernetes would kill and
restart a pod that is working correctly but simply waiting on a dependency —
compounding one outage into a crash loop. This was a real defect, not a
hypothetical one; see
[M1's blocking-startup writeup](evidence/m1/blocking-startup-defect.md).

## Application internals

- **`app/config.py`** — env-sourced `Settings` (`ML_` prefix).
- **`app/schemas.py`** — typed request/response models; invalid input →
  422 with a typed validation error.
- **`app/model_loader.py`** — `load_model()` with a bounded retry loop against
  MLflow/object storage; failure is surfaced on `/ready`, never swallowed.
- **`app/inference.py`** — holds the loaded model; `ready` is `False` until a
  successful load. Prediction failure never crashes the process.
- **`app/main.py`** — wires routes, structured logging, and the Prometheus
  middleware via a lifespan hook.

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
docs/evidence/m0..m12/  the transcript behind every claim in the README
```

## Local → AWS mapping

Frozen contract, full detail (identity boundaries, network design, cost
model) in [aws-architecture.md](aws-architecture.md):

| Local (M0–M12) | AWS (M13+, not yet applied) |
| --- | --- |
| kind | EKS |
| local image | ECR |
| MinIO (PVC) | S3 |
| PostgreSQL (PVC) | RDS PostgreSQL |
| kindnet NetworkPolicy | VPC security groups + NetworkPolicy |
| local ServiceAccount | IAM workload identity (IRSA) |

See [roadmap.md](roadmap.md) for the milestone breakdown and gates.
