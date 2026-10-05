# Control-plane packaging

Prepared on 2026-10-05 without building images or changing the running cluster.
Helm rendering and Kubernetes schema validation are local checks, not installation evidence.
The default control-plane chart, platform-local chart and standalone gateway manifest
validated as 34 resources; enabling both control-plane Ingresses validated 16 chart
resources. Rendering with two reconciler replicas correctly fails. A control-plane-only
Python wheel was built in an isolated temporary directory and inspected for the four
entrypoints, migration files and committed UI assets. No Docker runtime was started.

## Image and processes

`docker/controlplane/Dockerfile` installs `.[controlplane]` with
`constraints/controlplane.txt` and includes migrations and the committed UI bundle.
The same non-root image supports four process commands:

| Process | Command |
| --- | --- |
| API | `uvicorn controlplane.main:app_factory --factory --host=0.0.0.0 --port=8080` |
| Gateway | `uvicorn controlplane.gateway_main:app_factory --factory --host=0.0.0.0 --port=8081` |
| Reconciler | `python -m controlplane.reconciler_main` |
| Migration | `python -m controlplane.persistence.migrate upgrade` |

When RAM is available, build with `make cp-docker-build`. The default image is
`mlp-controlplane:dev`; override `CP_IMAGE` to publish under your own repository.
For releases, set the chart's `image.repository` and `image.digest` to the built digest.
The Dockerfile pins `python:3.12-slim-bookworm` by multi-platform SHA-256 digest.
Constraints have been regenerated for CPython 3.12/Linux x86_64 using uv 0.12.23.
Install that tool version and run `make lock` (or set `MLP_UV` to its executable).
Existing pins are retained; `MLP_LOCK_UPGRADE=1 make lock` requests deliberate upgrades.

## Chart prerequisites

`helm/controlplane` packages the API, gateway and reconciler Deployments, their
ServiceAccounts and RBAC, Services, optional Ingresses, configuration, and a migration Job.
It deliberately uses an existing PostgreSQL database and Secrets.

Before installation:

1. Create a **dedicated control-plane database** and database user. Reusing the existing
   PostgreSQL server is fine; pointing migrations at MLflow's database/schema is not.
2. Create the release namespace and an existing Secret `mlp-controlplane-db` with key
   `url` containing its `postgresql+psycopg://…/controlplane` connection URL. Use the
   organisation's secret provisioning mechanism; credentials are not stored in values.
3. Install Argo Workflows, KServe in Serverless mode and Knative with a functioning ingress
   implementation. The Argo controller must watch the generated `mlp-*` namespaces.
4. Set the OIDC issuer, audience, platform admins, public URL and gateway URL. Browser
   sign-in additionally needs the client ID and an existing Secret containing
   `CP_OIDC_CLIENT_SECRET` and `CP_SESSION_SECRET`, selected by `identity.existingSecret`.
5. Select and validate the [network topology](networking.md), including PostgreSQL ingress,
   serving ingress and both API/gateway access to OIDC discovery and JWKS.

Local rendering, with no cluster contact:

```bash
make cp-helm-lint
helm template mlp helm/controlplane --namespace mlp-system > /tmp/controlplane.yaml
kubeconform -strict -summary /tmp/controlplane.yaml
```

Later, after prerequisites and the image are ready:

```bash
helm upgrade --install mlp helm/controlplane --namespace mlp-system \
  --values /path/to/site-values.yaml --wait --timeout 10m
```

`values-local.yaml` disables OIDC for trusted local development. It is not a shared-cluster
configuration. The default issuer in `values.yaml` is a placeholder and must be replaced.

## Migration and concurrency behavior

The migration Job runs as a `pre-install,pre-upgrade` hook. It needs only the pre-existing
database Secret; it does not depend on chart-owned configuration or ServiceAccounts that
do not exist during pre-install. A failed migration blocks the Helm operation. Failed Jobs
remain available for inspection; successful ones are removed.

Only one reconciler replica is allowed. Its Deployment uses `Recreate`, so a Helm upgrade
does not deliberately overlap old and new processes. This is a single-process deployment
policy, not leader election: operators must not start another reconciler against the same
database. Separate simultaneous Helm releases/migration processes are also unsupported.

The gateway defaults to shared PostgreSQL buckets (`CP_GATEWAY_LIMIT_STORE=postgres`),
so replicas share capacity and debt. The default replica count remains one. Explicit
`memory` mode and demo mode keep independent per-process limits.

API and gateway startup/liveness probes use `/healthz`; readiness uses `/readyz`
and checks the database and migration heads. The reconciler has no HTTP health surface. Runtime probes,
read-only filesystem compatibility and actual memory consumption still need cluster QA.

## What remains before closing installation P0

- Build and run all four commands from the image, including SQL migrations and UI serving.
- Validate the pinned dependency baseline against the target Kubernetes version; it is
  a reproducible preparation baseline, not a certified compatibility/security matrix.
- Exercise the prepared bootstrap and manual-sync GitOps path below. Existing
  `make local-up` still installs the original inference platform only.
- Configure the optional chart/project NetworkPolicies and validate actual CNI paths.
- Run the CPU lifecycle and failure gates in `local-verification.md`.

## Readiness and lifecycle configuration

Readiness probes use `/readyz` (database and matching schema heads); `/healthz` remains
liveness. Apply migrations through `0019` before starting this image. Set
`config.CP_WORKFLOW_RETENTION_SECONDS` only after choosing a log retention window; its
default `0` disables workflow cleanup. Network isolation is separately configurable
through `networkPolicy`; configure API CIDRs and required external destinations first.
See [operations.md](operations.md), [networking.md](networking.md) and
[recovery.md](recovery.md) for the contracts and pending acceptance drills.

## Pinned bootstrap and GitOps preparation

`scripts/controlplane-dependencies.json` records versions, artifact SHA-256s and KServe
OCI digests. The baseline follows [KServe 0.15 quick install](https://github.com/kserve/kserve/blob/v0.15.0/hack/quick_install.sh):
Gateway API 1.2.1, Istio 1.23.2, cert-manager 1.16.1, Knative operator 1.15.7 / Serving
1.15.2, KServe 0.15.0 and Argo Workflows 3.6.2. No live compatibility check was run.

Prepare locally with Helm and the Python control-plane environment:

```bash
python scripts/controlplane_bootstrap.py --out /tmp/mlp-release \
  --values /path/to/site-values.yaml \
  --image registry.example/team/controlplane@sha256:<FULL_IMAGE_DIGEST> \
  --source-revision <FULL_GIT_COMMIT_SHA> \
  --context <TARGET_CONTEXT> --cache /path/to/dependency-cache
```

Replace angle-bracket placeholders before running. By default preparation is offline and
never contacts the cluster. `--fetch` downloads only pinned artifacts and verifies their
checksums. Preparation emits a packaged chart, rendered resources, values, KnativeServing,
manual-sync Argo CD Application, plan and installer. Argo CD itself is a prerequisite for
using the Application; preparation does not apply it or enable automatic sync.

A dirty checkout can produce a review bundle, but installation requires a clean checkout
matching the full source SHA. Commit/push the release and regenerate before installation.
The installer verifies dependency/chart checksums before cluster writes and requires an
explicit context. Only `--apply` executes it; this work did not execute that option.
Provision the dedicated DB, namespace, DB/identity Secrets and site identity/network values
first. The bootstrap installs serving/workflow dependencies and the control plane, not
PostgreSQL or an identity provider. Actual image build, live migrations and CPU/GPU/TLS
lifecycle checks remain release gates.
