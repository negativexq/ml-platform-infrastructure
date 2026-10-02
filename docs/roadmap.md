# Roadmap

Where the platform stands, what is missing, and what comes next. Kept up to date with the
code; the original milestone plan is in [history/](history/).

## Done

| Area | What works today |
| --- | --- |
| **Projects and access** | Projects with their own namespace and quota. OIDC sign-in for the browser (server-side PKCE, signed session cookie) and for bearer tokens. Project roles (`invoker` < `viewer` < `operator` < `admin`) for users and groups, plus platform admins. A fail-closed policy table and an audit trail |
| **Training** | Jobs and pipeline DAGs on Argo Workflows. Retry, cancel and logs; step timelines and failure reasons. Lineage from a run to the model versions it produced |
| **Models** | Versions from the MLflow registry, or for LLMs from the Hugging Face Hub. Thresholds and evaluation, promotion to champion, registry aliases kept in sync |
| **Serving** | Immutable revisions. Canary rollouts with gates (error rate, p95, minimum traffic) that roll back automatically. Manual rollback. Metrics and trends per revision |
| **LLMs (API)** | LLM models with GPU and context settings. KServe's Hugging Face runtime (vLLM) on GPUs. Per-project GPU quota, set by platform admins and checked on deploy and canary |
| **Gateway** | A separate service that makes endpoints public. Per-caller API keys, shown once and stored hashed. Limits per endpoint and per key: requests, or tokens for LLMs. Streaming, OpenAI-compatible chat completions, usage by caller. Ingress, TLS and NetworkPolicy manifests |
| **Monitoring** | Monitor page (reconcilers, API, gateway, external systems). Grafana dashboard, 10 promtool-tested alerts, traces from request to reconciler |
| **Web UI** | App shell with a sidebar and project switcher. Pages for runs, pipelines, models, deployments, activity, settings, members and API access. Charts built to a data-viz spec. Previews before risky changes. Light, dark and mobile |

## Missing in the code

### LLMs in the UI
- **"Try it" is wrong for LLMs.** On an LLM deployment it calls `predict` and fails. It
  should be a chat playground; the backend route (`POST /projects/{p}/endpoints/{name}/chat`)
  already exists.
- **API access shows the wrong snippets for LLMs.** On an LLM endpoint it shows the
  classic-model example (`/predict`, `instances`). It should show `chat/completions` with
  curl, streaming and the OpenAI SDK.
- **No form to create an LLM model** (kind, GPUs, context length).
- **No form to register a version from the Hugging Face Hub** (source and offline metrics).
- **Tokens are not shown per direction.** The usage table should show prompt and completion
  tokens; the API already returns them.
- **GPU quota and use are not shown** in project settings or the overview. Only platform
  admins should be able to edit the quota.
- **Revisions do not show** their runtime or GPU count.
- **No browser tests** for any of the above.

### LLM docs and checks
- **`docs/gateway.md`:** add chat completions, streaming, token metering and token limits.
- **`docs/identity.md`:** add the GPU quota rule (platform admins only).
- **`docs/local-verification.md`:** add a gate for a real vLLM model on a GPU.
- **`make gateway-e2e`:** add an LLM case (stream, token count, token limit).

### Functions (phase 3)
- **Not started.** Endpoints of kind `function` (a container image, scaled to zero by
  Knative, `POST …/invoke`) are reserved in the contract. The gateway, keys and limits
  already allow for them.

## Missing to install it on a cluster

- **A container image for the control plane.** It would serve the API, the reconciler and
  the gateway. `k8s/gateway/gateway.yaml` expects `mlp-controlplane:dev`, which nothing
  builds yet.
- **A Helm chart for the control plane:**
  - API, reconciler and gateway Deployments.
  - The reconciler's ClusterRole: namespaces, service accounts, resource quotas, limit
    ranges, network policies, roles, role bindings, Argo workflows, pod logs.
  - Migrations as a Job.
- **Argo Workflows in `make local-up`.** Its `workflowNamespaces` and RBAC must cover the
  `mlp-*` namespaces.
- **Per-project secrets:**
  - MLflow and S3 credentials for training steps.
  - The Hugging Face token (`mlp-hf-token`) for gated models.

  Today all of these are created by hand.

## Not yet run against the real thing

Code and tests exist; these have only met fakes, schema validation or kubeconform. The steps
for each are in [local-verification.md](local-verification.md).

- KServe:
  - the MLflow server;
  - the Knative canary traffic split;
  - vLLM on a GPU.
- Argo Workflows running real jobs and pipelines.
- The gateway in a cluster, behind an ingress with TLS (cert-manager), and with its
  NetworkPolicy.
- Prometheus scraping `mlp_*` in the cluster, and the alerts firing.
- A Keycloak client-credentials token accepted by the gateway.

Already verified for real here:
- PostgreSQL, through the migrations and the full test suite.
- Prometheus 3.1: every query the platform makes.
- Keycloak 26.4: browser sign-in.
- The gateway end to end, as a real process with HTTP to a model server.
- Chromium: the UI tests.
- The alerts with promtool, the manifests with kubeconform.

## Known limitations

- **Rate limits are kept per gateway replica.** With N replicas, the real limit can be up to
  N times the configured one. A shared store (Redis) behind the `RateLimiter` port fixes it.
- **Token metering falls back to zero.** If the model server does not report `usage`, the
  call counts as 0 tokens.
- **LLM evaluation uses results supplied at registration.** There is no built-in evaluation
  harness.
- **Canary rollouts need `CP_PROMETHEUS_URL`.** Without it, they do not advance.
- **Missing operations:**
  - no deployment delete;
  - no clean-up of pipeline runs or workflow pods;
  - no log streaming;
  - no per-run timeout;
  - no automatic discovery of model versions after a pipeline run.
- **`constraints/controlplane.txt` was edited by hand** (`httpx`). Regenerate it with
  `make lock`.
- **AWS is designed in Terraform but has never been applied.**

## Next, in order

1. **Finish LLMs:** the UI, docs and tests above. This closes phase 2 and removes the
   errors that show in the demo.
2. **Make it installable:** the control plane image, the Helm chart, and Argo in
   `local-up`. Then the platform can run on a real cluster for the first time.
3. **Verify on a real cluster:** KServe, Argo, a GPU node, ingress and TLS, following
   `local-verification.md`.
4. **Production hardening:**
   - a shared rate-limit store;
   - per-project secrets;
   - deployment delete and run clean-up;
   - log streaming.
5. **Functions (phase 3):** serverless endpoints of kind `function`.
6. **AWS:** EKS, RDS and S3 from the existing Terraform.
