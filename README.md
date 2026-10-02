# ML Platform Infrastructure

A local ML platform reference implementation in two layers:

1. **The infrastructure** (M0–M12, frozen as `local-v1.0.0`): an inference service, its full
   MLflow/PostgreSQL/MinIO lifecycle, GitOps, autoscaling, security hardening and
   observability. It runs on Kubernetes and is validated with real drills, not descriptions.
2. **The platform** (M13+, branch `v2`): a control plane that teams use to run ML work end to
   end. They train with jobs and pipelines, register and evaluate models, and deploy with
   canaries. They can open models to outside callers through a gateway with API keys and
   serve LLMs on GPUs. There is a web UI, OIDC sign-in with project roles, and full
   observability.

AWS is designed as code but has not been built yet.

**Status: `local-v1.0.0`.** The local implementation (M0–M12) is validated and
frozen. AWS work (M22+) has not started — no cloud resource has been created,
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

## The platform (control plane)

```
 people (browser)            services and partners (API keys, OAuth)
        │                                 │
        ▼                                 ▼
 ┌──────────────┐  OIDC   ┌──────────────────────────┐
 │  Web UI      │◀──────▶│  Identity provider       │  Keycloak locally
 │  (React)     │         │  users, groups           │
 └──────┬───────┘         └──────────────────────────┘
        │ same-origin, session cookie
        ▼
 ┌─────────────────────────────┐      ┌────────────────────────────────────┐
 │  Control plane API          │      │  Inference gateway (separate pods) │
 │  projects · roles · jobs    │      │  /v1/{project}/{endpoint}/predict  │
 │  pipelines · models · evals │      │  /v1/.../chat/completions (LLMs)   │
 │  deployments · canaries     │      │  API keys · invoker role           │
 │  API keys · GPU quota       │      │  limits (requests or tokens)       │
 │  audit · monitor            │      │  streaming · usage metering        │
 └──────┬──────────────────────┘      └──────┬─────────────────────────────┘
        │ desired state                      │ reads endpoints and keys (5 s cache)
        ▼                                    ▼
 ┌──────────────────────────────────────────────────┐
 │  PostgreSQL: the single source of lifecycle truth │  Alembic migrations 0001–0011
 └──────┬───────────────────────────────────────────┘
        │ reconcile loop (idempotent, audited, traced)
        ▼
 ┌───────────────┬────────────────┬──────────────────────┬─────────────────────┐
 │ Kubernetes    │ Argo Workflows │ MLflow registry      │ KServe (Knative)    │
 │ namespace per │ jobs and       │ runs, metrics,       │ MLflow server (v2)  │
 │ project,      │ pipeline DAGs  │ model versions,      │ or vLLM on GPUs,    │
 │ quotas, GPUs  │                │ aliases              │ canary traffic split│
 └───────────────┴────────────────┴──────────────────────┴─────────────────────┘
        all of it → OpenTelemetry Collector → Prometheus · Tempo · Grafana · alerts
```

The design is hexagonal: domain and application code know no frameworks, and every external
system sits behind a port with an adapter and an in-memory fake. An architecture test
enforces this. The UI talks only to the platform API.

### What a team can do

| Area | Features |
| --- | --- |
| **Projects and access** | Projects with their own Kubernetes namespace and quota. OIDC sign-in (browser and bearer tokens). Roles `invoker < viewer < operator < admin` for users and groups. Platform admins. A fail-closed policy table. Every change is in the audit trail, with the person's name |
| **Training** | Jobs and pipeline DAGs on Argo Workflows, with retry, cancel, logs, step timelines and failure reasons. Lineage from a run to the model versions it produced |
| **Models** | Versions discovered from the MLflow registry or, for LLMs, registered from the Hugging Face Hub. Acceptance thresholds, evaluation to CANDIDATE or REJECTED, promotion to CHAMPION, registry aliases kept in sync |
| **Serving** | Immutable revisions. Canary rollouts with traffic steps and gates (error rate, p95, minimum traffic) that roll back automatically. Rollback. Per-revision metrics and trends |
| **LLMs** | LLM models served by KServe's Hugging Face runtime (vLLM) on GPUs. Per-project GPU quota, set by platform admins and checked on deploy and canary. OpenAI-compatible chat completions with streaming |
| **Public API** | A separate gateway service: `POST /v1/{project}/{endpoint}/predict` or `/chat/completions`. Per-caller API keys, shown once and stored hashed. Endpoint and per-key limits: requests per minute, or tokens for LLMs. One error shape and request ids. Usage by caller. Ingress, TLS and NetworkPolicy manifests |
| **Monitoring** | A Monitor page for the platform's own health (reconciler heartbeats, API, gateway, external systems), with alert-matched thresholds. Grafana dashboard and 10 promtool-tested alerts. Traces from an API request through the reconciler |
| **UI** | Enterprise app shell with a sidebar and project switcher. Runs, pipelines, models, deployments, activity, settings, members and API access. Charts built to a data-viz spec (validated colours, table twins, keyboard tooltips). Previews before risky changes. Light, dark and mobile |

### Run it

```bash
make cp-demo        # control plane + gateway on in-memory fakes: http://localhost:8080/ui
                    # prints a ready-to-run curl for the public gateway
make cp-test        # unit, API, PostgreSQL and real-browser tests
make gateway-e2e    # real PostgreSQL + the real gateway process + a model server over HTTP
make identity-up    # Keycloak in kind with demo users (alice, bob, carol)
```

What was verified, and what still needs a real cluster:
- **Verified here:** the test suite (memory and PostgreSQL), real Prometheus queries, real
  Keycloak sign-in, promtool alert tests and kubeconform.
- **Needs a real cluster** (KServe, Argo, GPUs, ingress, TLS): listed gate by gate in
  [`docs/local-verification.md`](docs/local-verification.md).

Docs: [identity](docs/identity.md) · [gateway](docs/gateway.md) ·
[UI](docs/ui.md) · [observability](docs/observability.md) ·
[roadmap](docs/platform-roadmap.md).

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
| M13 | Platform domain foundation — control plane, PostgreSQL-owned lifecycle state | [gate](docs/evidence/m13/gate.md) |
| M14–M21 | Platform MVP: projects, runs, pipelines, models, serving, canary, UI | [plan](docs/platform-roadmap.md) |
| v2 | Identity and roles, observability, Monitor, public gateway with API keys, LLM serving with GPU quota (UI for LLMs in progress) | [local verification](docs/local-verification.md) |
| M22+ | AWS — **not started, no cloud resource has been created** | — |

## Infrastructure architecture (local-v1.0.0)

```
                                              client
                                                │
                                                ▼
┌────────────────────── Git (source of truth) ─────────────────────────────────┐
│  helm/ml-platform/   helm/platform-local/   gitops/   infra/terraform/       │
└──────────────────────────────────────────────────────────────────────────────┘
                          │  poll ~3 min · watch + self-heal ~1.4 s
                   ┌──────▼──────┐
                   │   Argo CD   │   Applications: platform-local, inference-local
                   └──────┬──────┘
                          │ apply
┌───────────────────── kind cluster · Pod Security Standards: restricted ──────────────────────┐
│                                                                                              │
│ namespace: ml-platform ──────────────────────────────────────────────────────────────────────│
│                                                                                              │
│  Service ──▶ inference    Deployment · HPA 2↔6 on CPU · PDB minAvailable=1                   │
│                  │  GET /health   GET /ready   POST /predict   GET /metrics                  │
│                  │                                                                           │
│                  │  NetworkPolicy: default-deny + explicit allow-list                        │
│                  ├── allowed ──▶ MLflow ──▶ PostgreSQL   StatefulSet, PVC                    │
│                  │                     └──▶ MinIO        StatefulSet, PVC                    │
│                  └── denied  ──▶ PostgreSQL directly                                         │
│                                                                                              │
│ namespace: observability ────────────────────────────────────────────────────────────────────│
│                                                                                              │
│  Prometheus ── scrapes /metrics ──▶ inference (above)                                        │
│  Prometheus ──▶ Grafana        Prometheus ──▶ Alertmanager  5 rules, promtool-tested         │
│                                                                                              │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

Everything runs inside the cluster; Docker Compose was retired in
[M7](docs/evidence/m7/gate.md). Argo CD watches live cluster state
continuously (drift reverted in ~1.4 s) and polls Git independently (default
~3 min) — the two paths have very different latency, which is why a Git
commit lands slower than a manual edit gets reverted
([M4](docs/evidence/m4/argocd-drift-reconciliation.md)). NetworkPolicy denies
`inference → PostgreSQL` directly, verified rather than assumed
([M9](docs/evidence/m9/gate.md)). Full component notes, the `/health` vs
`/ready` design decision, and the repository layout:
[`docs/architecture.md`](docs/architecture.md).

## Key results

Measured on this local kind cluster, not estimated:

| | |
| --- | --- |
| k6 load test | **645,809 requests, 0% errors** |
| Saturated throughput | **2,935 req/s** |
| Saturated `/predict` p95 | **32.9 ms** |
| HPA scale-up under load | **2 → 6 replicas in 71 s** |
| HPA scale-down after load | **6 → 2 in ~230 s**, stepped |
| Pod deleted → replacement serving | **12–15 s** |
| Argo drift → reconciled | **~1.4 s** |
| Fresh cluster → 11/11 acceptance | **901 s (~15 min)** |

Autoscaling: [M10](docs/evidence/m10/gate.md) · pod recovery:
[M2](docs/evidence/m2/pod-recovery.md) · GitOps reconciliation:
[M4](docs/evidence/m4/argocd-drift-reconciliation.md) · reproducibility:
[M11](docs/evidence/m11/gate.md).

## Failure engineering

Eight faults injected on purpose against the running cluster — not simulated.
Representative scenarios:

| Scenario | Observed | Recovery |
| --- | --- | --- |
| Pod crash | ReplicaSet notices, surviving replica keeps serving | replacement ready in 12–15 s |
| Invalid model artifact | readiness 503, pod held out of Service endpoints | Git revert |
| Artifact store outage | 100% of requests still 200 while unready | background recheck, 0 restarts |
| Config drift | Argo marks `OutOfSync` the moment it diverges | self-heal in ~1.4 s |
| Bad rollout | `maxUnavailable: 0` keeps old replicas serving | Git revert, bad ReplicaSet pruned |
| Node drain (stateful pod on it) | surviving inference pod absorbs traffic | Postgres/MinIO reschedule automatically |

Full 8-row table with detection/containment detail:
[`docs/failure-engineering.md`](docs/failure-engineering.md).

## Real defects this project found in itself

Not written around — found by running the automation (one of them by someone
just asking), then fixed. Full writeups:
[`docs/failure-engineering.md`](docs/failure-engineering.md).

- **Blocking startup** — model loading blocked the process; a slow artifact
  store took `/health` down with it. Fixed: moved to a background thread.
- **Dropped request during rolling update** — `kubectl rollout status` said
  success while an external probe measured 1 failure in 90. Fixed: `preStop` drain.
- **Empty dashboard panel** — a labelled counter emits no series until its
  first increment, so "zero errors" and "not instrumented" looked identical.
  Fixed twice with `or vector(0)`.
- **Security drill false positive** — PSS `restricted` rejected the drill's
  own probe pods, and every admission rejection was misread as a network
  `DENY`. Fixed: PSS-compliant probe pods.
- **MLflow CVE vs. memory trade-off** — the 297 MiB version carried 7
  unpatched CRITICAL CVEs; every fix lands only in a version with a 1.46 GiB
  floor. Paid the memory.
- **Broken GitHub Actions Trivy scan** — an action tag missing its `v` prefix
  never resolved, so `image-scan` silently failed for four milestones while
  the gate table said `PASS`. Fixed the pin and corrected the M9 evidence.

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
- The platform (v2) is verified with fakes, real PostgreSQL, Prometheus and Keycloak. It
  has not yet run against real KServe, Argo or GPUs, and its own Helm chart is not written.
  The UI for LLMs (playground, token usage, GPU quota) is still to come; LLMs work through
  the API and the gateway.
- AWS (M22+) will replace each local dependency with its managed equivalent
  (kind→EKS, MinIO→S3, PostgreSQL→RDS, local image→ECR) and re-run the
  equivalent gates — it does not change anything above.

## Security note

All credentials committed in this repository are disposable local-development
defaults (e.g. MinIO's own upstream `minioadmin`/`minioadmin`). No production
credentials, cloud secrets, proprietary code, or customer data are included.

## License

[MIT](LICENSE)
