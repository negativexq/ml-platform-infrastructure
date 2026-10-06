# Manual release and acceptance gates

The original command-preparation batch did not start Docker, PostgreSQL, a browser or a cluster.
Subsequent isolated SQL evidence is in [security-hardening.md](security-hardening.md) and
[concurrency-identity-audit.md](concurrency-identity-audit.md); current release status is
[status.md](status.md). The commands below
produce evidence only when actually run. Local orchestration/schema tests are separate
from live acceptance. CI is unchanged.

## Image runtime, scan and SBOM

From a clean committed checkout, with Docker, Trivy and Syft installed:

```bash
make cp-release-check CP_IMAGE=mlp-controlplane:release-check \
  CP_RELEASE_OUT=/tmp/mlp-release-evidence
```

The output directory must be new. The gate builds the control-plane Dockerfile with its
full source SHA label; every runtime uses the resulting immutable image ID. It tests clean
PostgreSQL migrations to the current image head (`0021`), API/gateway liveness/readiness,
reconciler configuration/empty-state passes and database restart recovery. The fixture
kubeconfig is deliberately not connected to Kubernetes; election/RBAC are live gates.
The disposable owner-credential fixture explicitly disables runtime-role enforcement; it does not prove DB privilege separation. An isolated Docker network/database is removed afterwards, including labelled temporary
probe containers. It writes `report.json`, `trivy.json`, `trivy-secrets.json` and `sbom.spdx.json`. The SBOM is
required; no scan result is silently skipped. The scan gate fails on fixable HIGH/CRITICAL
vulnerabilities and detected secrets. It does not assert absence of every unfixed CVE.

The direct CLI also accepts `--postgres-image` and an existing built image without
`--build`; its source label must match the clean checkout. Record the PostgreSQL fixture
image ID from the report. This database is a disposable fixture, not the platform DB.
See [Trivy image scanning](https://trivy.dev/docs/latest/target/container_image/) and
[Syft sources/output](https://github.com/anchore/syft).

## PostgreSQL ownership and limiter correctness

Supply an explicit, dedicated, migrated PostgreSQL test database:

```bash
export CP_ACCEPTANCE_DATABASE_URL='postgresql+psycopg://…/isolated_controlplane'
python -m pytest controlplane/tests/test_limiter_acceptance.py
```

These tests never start a server. They check two limiter instances/concurrent callers,
shared capacity, a real held-row lock and timeout/recovery, and refusal of a second
migration while its advisory lock is held. Only their uniquely named bucket rows are
removed. Do not use a production database.

## Gateway load and outage matrix

Provision four gateway pods backed by the same DB, a dedicated public **request-unit**
CPU/function test endpoint and a caller key. Supply four distinct pod/port-forward URLs
for that same endpoint, not four copies of a shared load-balancer URL. The endpoint and
caller must both use the `--limit` capacity. Configure a real upstream that handles the
intended throughput. Keep other callers off the test endpoint.

```bash
export CP_ACCEPTANCE_GATEWAY_TOKEN='…'
export CP_ACCEPTANCE_PROMETHEUS_URL='http://prometheus.test:9090'
python scripts/controlplane_limiter_check.py \
  --urls http://127.0.0.1:8101/v1/test/model/predict \
         http://127.0.0.1:8102/v1/test/model/predict \
         http://127.0.0.1:8103/v1/test/model/predict \
         http://127.0.0.1:8104/v1/test/model/predict \
  --body /tmp/test-request.json --limit 600 --seconds 10 \
  --confirm-test-endpoint --scenario load --out /tmp/limiter-load.json
```

The matrix uses 1/2/4 replica addresses and 50/100/500 offered RPS. It reports HTTP
p50/p95/p99 (including client queue time), completed throughput, status counts, successful
calls against a token-bucket upper bound and optional PostgreSQL lock-waiter samples.
`CP_ACCEPTANCE_DATABASE_URL` enables samples; the role needs appropriate statistics
visibility. The `mlp_gateway_limiter_duration_seconds` histogram reports transaction
p50/p95/p99 via Prometheus; it requires gateway OTEL export/scraping. Quantiles use a
rolling one-minute aggregate, so isolate the test and interpret overlapping windows.
Missing metrics/samples are reported as missing, never zero-latency proof.

The generator caps concurrent calls at 200; compare achieved with offered throughput and
check load-generator resources before blaming the server. Reservations persist/refill
between matrix cells. The upper bound accounts for initial capacity plus elapsed refill;
it is not an exact per-window or LLM token correctness proof.

Before a pod-stop drill, verify the isolated database data mount uses a Bound PVC
and that the existing schema/fixture identity survives one restart. `emptyDir` erases
the test database on pod deletion and cannot prove recovery. Keep the original replica
count and restore it in a `finally` block. Probe both warmed and expired route/key caches.

On the **isolated** database, start normal traffic, interrupt DB access while traffic is
active, then repeat with `--scenario outage` and a fresh output path. Inspect 503/error
codes, limiter error latency, pool/thread growth and health/readiness. Restore DB access
and repeat `--scenario recovery`; retain the pre-cut/in-cut/post-cut reports. The tool does
not stop databases or change firewall rules. The outage scenario rejects any successful
forwarding; automatic cutover timing, thread-growth measurements and exact
`limit_store_unavailable` checks still need operator observation. A DB outage may also
fail route/auth lookup before limiter admission; SQL-backed cache misses return
`503 data_store_unavailable`, while admission failures return `503 limit_store_unavailable`.
Compare measured lock/statement waits
with their per-statement bounds (2/3 seconds); pool/connect waits are separate bounds.

## Full CPU lifecycle

Use an isolated acceptance cluster with Argo, MLflow/artifacts, KServe/Knative, enforcing
networking, ingress, Prometheus and the updated control plane. A platform-admin API token
and an explicit kubectl context with fixture/impersonation permissions are required.
Build/push the training image and `docker/acceptance-function/Dockerfile`; supply full
image digests. The function fixture uses stdlib HTTP and reports credential hashes plus
an instance boot ID, never raw credentials.

```bash
python scripts/controlplane_cpu_acceptance.py \
  --api https://platform.test --gateway https://inference.test \
  --prometheus http://prometheus.test:9090 --context acceptance \
  --training-image registry.test/training@sha256:<FULL_DIGEST> \
  --function-image registry.test/acceptance-function@sha256:<FULL_DIGEST> \
  --out /tmp/cpu-acceptance.json
```

Replace digest placeholders. Default execution prints the plan and changes nothing.
Add `--execute` to run. `CP_ACCEPTANCE_API_TOKEN` contains the admin token. Optional
`--storage-secret`/`--registry-secret` files contain SecretWrite JSON (mode 0600); these
create credentials in the new project. Training AWS keys use secret env references;
classic serving uses `storage_secret`. `--training-env` supplies nonsecret training
configuration only. Registry references permit a positive private-image pull case.

The flow creates a uniquely named project and checks namespace quota/executor/Secret
binding and actual API-SA Secret allow/deny results. It runs two training pipelines,
waits for automatic lineage-scoped discovery, evaluates CPU models, serves through the
gateway, drives healthy canary traffic and records distinct backend-scoped Prometheus
request/error/p95 evidence. It then checks function credential retention on rotation,
new credentials after a pod restart, protected deletion, same-platform-revision drift
repair with a fresh backend/apply identity, and zero-pod activation with a new boot ID.
The fixture function must match the supplied image; arbitrary images will fail the checks.

Failures produce a failed/partial report. Fixtures are retained for inspection; delete
the reported project after reviewing its evidence. The script does not deploy dependencies
or automatically clean other resources. A healthy canary pass does not prove adversarial
label/error attribution. Missing Prometheus series fail the gate rather than promote
without evidence. Inspect candidate/stable counters with injected candidate-only errors
as a separate test. Observe intermediate readiness during drift repair separately; the
script proves final backend/apply alignment, not every transient controller snapshot.

Still separate live gates: wrong/rotated registry credentials and ImagePullBackOff
messaging; forced Secret deletion/future-start failure; cross-project CNI deny plus allowed
paths; leader loss/API partitions/node drain; real GPU autoscaling/overlap quota; exact HF
commit downloads and gated-model auth; and bad-candidate rollback attribution.

## Database recovery evidence

Native tools: compatible `psql`, `pg_dump`, `pg_restore`, plus libpq credentials/TLS
through the environment described in [recovery.md](recovery.md). Stop source writers for
this equality drill and pre-create a separate empty database on the same server:

```bash
python scripts/controlplane_recovery_check.py --out /secure/drills/controlplane \
  --target controlplane_recovery --source-quiescent
```

The script refuses the source as target, takes a snapshot backup, verifies source
fingerprints did not change, restores atomically, checks schema/counts and all durable
row fingerprints, and records measured backup/restore/database recovery duration. It
restores the original `PGDATABASE` environment even on failure. Tokens/rows are not
printed; reports contain hashes/metadata only. This is a quiescent database drill, not
platform RPO/RTO. Source or target data is never dropped/cleaned automatically.

Then start the same API/gateway image on the recovered DB in isolation, confirm readiness,
revoked-key rejection, signed-in project roles, deployments and run/model lineage. Keep
the reconciler stopped until external namespaces/Secrets/workflows and artifact state
have been reconciled. Record these service results and actual RPO/RTO separately. The
prepared database command does not claim these service/OIDC checks.

## Workload read scope, stalled leader and credential cleanup

CPU acceptance now checks the API identity's Secret and workload-reader permissions:
allowed in its owned project namespace, denied in system and kube-system namespaces.
This includes pod logs, workflows, InferenceServices and Knative Revisions. Execute the
gate to obtain cluster authorization evidence; render/unit tests alone do not prove it.

For a live hung-leader drill, stall the main reconciler thread in a provider call while
allowing the renewal thread to run. Confirm Lease renewals continue initially, then
watchdog exit at `CP_RECONCILER_WATCHDOG_SECONDS`, pod restart and standby acquisition
after Lease expiry. Record timings and convergence. Also evict a reconciler and verify
its PDB prevents simultaneous voluntary eviction of both ready replicas.

For S3 cleanup, create multiple storage-bound revisions, confirm their accounts remain
after canary/rollback, then delete the deployment. Wait for KServe disappearance and
platform DELETED; verify only that deployment's owned storage accounts disappear and
other deployment/foreign accounts remain. Retry a cleanup conflict/outage and verify
it is not marked DELETED prematurely. These live drills remain pending.

## Reconciler admission enforcement

Offline, with Helm and Go 1.23+ (dependencies pinned in `scripts/admission-cel/go.sum`):

```bash
make cp-admission-check
```

The gate evaluates the actual rendered CEL using cel-go. Allowed namespace provisioning,
project bindings/repair/deletion, exact workflow Role and Lease writes pass. Direct-SA
fixtures reject foreign namespaces/provider writes, ownership adoption/removal/change,
wrong binding name/roleRef/subject and extra subjects. These are CEL expression and chart
contracts, not Kubernetes structural-schema type checking or live admission evidence.

On the deployed 0.3.1 chart, with a preexisting owned project:

```bash
python scripts/controlplane_admission_check.py --context <context> \
  --release mlp --system-namespace mlp-system --project-namespace mlp-<project>
```

This reads all four policies/bindings, requires current observed generations and completed
zero-warning type checks, and impersonates the reconciler for nine server dry runs.
It checks allowed owned binding repair and denial of alternate subjects/names, extra
subjects, foreign bindings/provider writes, ownership changes and foreign label forgery.
Dry runs persist nothing. Denied operations must report the expected release's policy;
RBAC denial, network failure, bad fixtures and unrelated policies are gate failures.
CPU acceptance invokes this gate during its project/RBAC phase. On 2026-10-06, the
installed gate passed on Kubernetes 1.32.0 with four policies/bindings, zero type warnings
and nine server dry runs. See [live evidence](evidence/live-2026-10-06/README.md).
