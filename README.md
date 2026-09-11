# ML Platform Infrastructure

A local ML platform reference implementation — an inference service, its full
MLflow/PostgreSQL/MinIO lifecycle, GitOps, autoscaling, security hardening and
observability, all running on Kubernetes and validated with real drills
instead of descriptions. AWS is designed as code but has not been built yet.

**Status: `local-v1.0.0`.** The local implementation (M0–M12) is validated and
frozen. AWS work (M13+) has not started — no cloud resource has been created,
and Terraform is currently at the design/static-validation level only (`fmt`,
`validate`, `tflint`; no `plan`, no `apply`).

## Try it

```bash
git clone https://github.com/negativexq/ml-platform-infrastructure.git
cd ml-platform-infrastructure
make local-up      # fresh kind cluster → GitOps → ML lifecycle → observability, ~15 min
make local-test    # 11-check acceptance suite
make local-down    # tear it all down
```

No manual step, no pre-existing cluster resource, no registry. Proved for real
in [M11](docs/evidence/m11/gate.md): cluster, images, and build cache
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
| M13+ | AWS — **not started, no cloud resource has been created** | — |

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
   │  Service ──> inference (HPA 2-6, PDB min 1)   Prometheus ──> Grafana │
   │       NetworkPolicy: default-deny + allow-list                      │
   │       platform-local: MLflow ──> PostgreSQL (PVC)                   │
   │                            └──> MinIO      (PVC)                    │
   └───────────────────────────────────────────────────────────────────────┘
```

Everything runs inside the cluster; Docker Compose was retired in
[M7](docs/evidence/m7/gate.md). Full diagram, the `/health` vs `/ready`
design decision, and the repository layout:
[`docs/architecture.md`](docs/architecture.md).

## Key results

Measured on this local kind cluster, not estimated — full detail linked per row:

| | |
| --- | --- |
| k6 load test | **645,809 requests, 0% errors** |
| Saturated throughput | **2,935 req/s** ([M10](docs/evidence/m10/gate.md)) |
| Saturated `/predict` p95 | **32.9 ms** |
| HPA scale-up under load | **2 → 6 replicas in 71 s** |
| Pod deleted → replacement serving | **12–15 s** ([M2](docs/evidence/m2/pod-recovery.md)) |
| Argo drift → reconciled | **~1.4 s** ([M4](docs/evidence/m4/argocd-drift-reconciliation.md)) |
| Fresh cluster → 11/11 acceptance | **901 s (~15 min)** ([M11](docs/evidence/m11/gate.md)) |

## Failure engineering

Six drills — pod crash, invalid model artifact, artifact-store outage, config
drift, latency regression, bad rollout, volume loss, node drain — run for
real against the cluster, each with its own detection/containment/recovery.
They surfaced six real defects along the way, including a blocking model load
that took down `/health`, a rolling update that dropped 1 request in 90 while
`kubectl` reported success, and a security drill that produced a false
positive after PSS `restricted` went on. Full table and all six writeups:
[`docs/failure-engineering.md`](docs/failure-engineering.md).

## Engineering evidence

- [Milestone roadmap](docs/roadmap.md)
- [Architecture](docs/architecture.md)
- [Failure engineering](docs/failure-engineering.md)
- [SLOs and alerting](docs/slo.md)
- [AWS migration design](docs/aws-architecture.md)
- [Cost model](docs/cost-model.md)
- [M0–M12 evidence transcripts](docs/evidence/)

## Current scope

- `local-v1.0.0` is a validated local reference implementation — AWS has
  never been applied.
- Terraform `fmt`/`validate`/`tflint` pass; a real `terraform plan`/`apply`
  against an AWS account has not happened. Detail:
  [docs/aws-architecture.md](docs/aws-architecture.md).
- PostgreSQL and MinIO run single-replica by design — intentionally non-HA
  for a local lab.
- Alerting uses static thresholds; there is no burn-rate/error-budget alerting
  yet.
- `local-up`/`local-test` were proven manually from a destroyed-and-rebuilt
  environment ([M11](docs/evidence/m11/gate.md)) but do not yet run
  automatically in CI.
- AWS (M13+) will replace each local dependency with its managed equivalent
  (kind→EKS, MinIO→S3, PostgreSQL→RDS, local image→ECR) and re-run the
  equivalent gates — it does not change anything above.

## Security note

All credentials committed in this repository are disposable local-development
defaults (e.g. MinIO's own upstream `minioadmin`/`minioadmin`). No production
credentials, cloud secrets, proprietary code, or customer data are included.

## License

[MIT](LICENSE)
