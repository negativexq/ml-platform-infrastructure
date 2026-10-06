# Security hardening and remaining verification

Current consolidated status (2026-10-06): [status.md](status.md). Dated findings and test records below retain their original scope.

Date: 2026-10-05. Starting HEAD: `20bb0a7` (the supplied audit reviewed `e6f2981`).
This document records the repository changes from the security remediation task, their
local evidence, deployment prerequisites, and work that remains. Changes have not been
installed in a cluster or AWS. No cluster or Docker container was started or changed.
Small isolated PostgreSQL test fixtures were started and stopped for grant/migration tests;
no platform database was used. CI remains intentionally disabled and is not a finding.

## Finding status

| Audit finding | Repository remediation | Remaining evidence or work |
| --- | --- | --- |
| **1 — dynamic namespace PSA** | Namespace desired state now includes enforce/warn/audit `restricted`, with enforcement pinned to `v1.30` and warn/audit at `latest`. Namespace admission requires these exact versions and rejects missing versions or policy weakening. Argo main/init/wait and KServe predictor containers receive explicit restricted contexts. | Actual PSA rejection and compatibility of injected Argo/KServe/Knative containers remain untested in a cluster. |
| **2 — shared workload SA** | `mlp-training` has the executor RoleBinding; `mlp-serving` has no RoleBinding and disables token automount. Serving PodSpecs also explicitly disable automount, including storage-account workloads. The old `mlp-workload` account loses its executor binding and desired automount is false. | Reconcile existing projects and replace old running pods; inspect projected volumes and real authorization. |
| **3 — shared database credential** | Chart 0.3.0 requires four distinct Secret names. A DBA grant tool configures separate API/reconciler/gateway privilege roles. Runtime startup/readiness checks reject owner/privileged/mixed-role credentials. Gateway key usage now writes only `last_used_at`. Only API mounts the OIDC/session Secret; gateway bearer authentication explicitly disables browser login. | Provision separate LOGIN users/passwords and Secrets; run the full API/reconciler/gateway lifecycle using those credentials. |
| **4 — audit append-only boundary** | Migration `0020` adds a PostgreSQL trigger rejecting UPDATE, DELETE and TRUNCATE. Runtime grant manifests allow audit SELECT/INSERT for API/reconciler, with no audit access for gateway. | This protects against runtime credentials; DBA/migration owners can explicitly disable/drop the trigger. It is not WORM storage or protection against a DBA. External archival/hash-chain retention remains optional future work. |
| **5 — ID token cookie** | ID token is discarded after sign-in validation and never placed in the session cookie. Logout uses `client_id` rather than `id_token_hint`. Oversized identity claims fail sign-in before setting an oversized cookie. Old cookie signatures use a different purpose and are rejected. | Validate client-based logout/confirmation behavior against the installed IdP. Large claim sets may require a future shared server-side session store. |
| **6 — stale session authorization** | Default/max session lifetime is 15 minutes; platform-admin sessions are capped at 5 minutes. No sliding refresh of the authorization snapshot. | IdP revocation is still bounded by this lifetime, not instantaneous. Test group removal followed by expiry and fresh sign-in. |
| **7 — ephemeral storage** | Project quota includes 16Gi requested/32Gi limited ephemeral storage; LimitRange defaults are 256Mi request/2Gi limit per container. | Validate admission/accounting and eviction on the target nodes/CNI/storage configuration, including Argo sidecars. |
| **8 — mutable training images** | Production API rejects job registration without a full lowercase SHA-256 image digest. The Argo submit path also rejects mutable images from previously stored job/pipeline definitions. Production chart refuses disabling the policy. | Re-register legacy tagged definitions under new names with real digests. Trusted local profiles explicitly allow mutable images. |
| **9 — gateway PostgreSQL dependency** | Retained the existing fail-closed design. Role separation does not remove route/key/limiter database dependencies. | 1/2/4 replica load, lock contention, DB outage/recovery and SLO evidence remain required; Redis has not been introduced. |
| **10 — rate-bucket growth** | Not changed in this batch. | Add safe bounded retention/GC, including tests for token debt, in-flight reservations and concurrent access. Monitor cardinality meanwhile. |
| **11 — reconciler RBAC boundary** | Already addressed by `20bb0a7` before this task. Policies now accept only the new training SA subject and reject the legacy shared-SA binding. PSA downgrade protection was added. | Actual admission activation/schema type checks and impersonated allow/deny tests remain required. |
| **12 — alias heartbeat** | Already addressed by `5a9c362` before this task; retained. | Slow real MLflow and leader/watchdog drills remain required. |
| **13 — AWS completeness** | README and Terraform lab documentation distinguish the historical infrastructure from the current platform deployment path. | Dedicated control-plane DB/users/Secrets, identities, ingress/TLS, Argo, KServe/Knative, observability and production networking still need an AWS deployment phase. |
| **14 — public EKS API** | Environment and reusable EKS module now require explicit non-empty valid CIDRs and reject all `/0` ranges. The example uses a documentation-only `/32` placeholder. | Replace placeholders; verify plan output and actual access restrictions before any deployment. |
| **15 — RDS network boundary** | Environment requires explicit dedicated database-client pod SG IDs, rather than using the shared EKS/node SG. A lifecycle precondition rejects that shared SG. | Provision/configure AWS VPC CNI `SecurityGroupPolicy`; prove authorized pod access and unrelated pod denial. Static validation does not establish packet isolation. |
| **16 — legacy inference chart** | Chart is explicitly scoped to legacy/local labs. Enabling its NetworkPolicy no longer opens ingress to `0.0.0.0/0`; callers must come from configured ingress/monitoring namespaces or explicit trusted source CIDRs. | NodePort labs must supply observed trusted node/source CIDRs. The old chart still has local-oriented defaults and open egress; use `helm/controlplane` for the current product, not this chart as a production profile. |
| **17 — image reproducibility** | Root inference, training and MLflow images use the same digest-pinned Python base as controlplane. Training and MLflow now install generated complete version locks; `scripts/lock.sh` supports both. Removed unconstrained pip upgrades from these paths and added source-revision labels. | No images were built, run or scanned in this task. SBOM, Trivy and supported-architecture build/import/runtime gates remain required for each image. See dependency caveat below. |
| **18 — durable Argo logs** | Not changed in this batch. | Object-storage log archives and streaming remain future work. |
| **19 — exact tokenizer quotas** | Not changed in this batch. | Model-specific tokenization/billing accuracy remains future work; current conservative reservation/fail-closed behavior is retained. |
| **20 — live evidence** | Local/static evidence improved; no production acceptance claim. | The checklist below remains pending. |

## Implementation details

### Kubernetes workloads

Shared context builders live in `controlplane/adapters/kubernetes/security.py`.
Containers use non-root execution, RuntimeDefault seccomp, no privilege escalation and
`capabilities.drop: [ALL]`. Argo main workloads retain the image's numeric `USER` and primary group; pod/main contexts
only require non-root execution, without a fixed UID/GID. Strategic PodSpec patches set
UID/GID 1000 explicitly on injected `init` and `wait` executors. Pod `fsGroup: 1000` remains
as a supplemental shared-volume group, without overriding the main container's identity.
The bundled training Dockerfile declares `USER 1000:1000`, allowing kubelet to verify its
non-root identity; external images must likewise declare a numeric non-root USER. KServe sets the predictor
pod context and model/custom container context, and detects drift in security/token fields.
Storage revision accounts retain their existing no-token behavior.

Chart 0.3.1 and namespace desired state pin PSA enforcement to `v1.30`, the chart's declared
minimum Kubernetes version. Warn/audit stay at `latest` to expose upcoming policy changes.
This is a deterministic supported baseline, **not a live-tested Kubernetes certification**.
Raising it requires a coordinated code/chart release and workload acceptance tests. Existing
owned namespaces can migrate from `latest` to the pin on their next reconciliation after
the updated admission policy is installed. The pin intentionally excludes newer restricted
checks from enforcement until that reviewed upgrade; monitor warn/audit findings.

These settings intentionally do not force a read-only filesystem on arbitrary training or
serving images; that is not required by restricted PSA. Root-dependent images or injected
sidecars that do not satisfy restricted PSA will be denied, rather than exempted.
The target controller/runtime installation must support these constraints.

Reference behavior: [Argo Workflow pod security](https://argo-workflows.readthedocs.io/en/latest/workflow-pod-security-context/)
and [KServe predictor API source](https://github.com/kserve/kserve/blob/v0.15.0/pkg/apis/serving/v1beta1/predictor.go).
The installed controller versions still require live compatibility checks.

### PostgreSQL privileges and deployment order

This requires a **dedicated control-plane database/public schema**, separate from MLflow.
The administrative tool revokes existing table/column/schema/function grants from the
specified runtime users and roles and revokes PUBLIC table access/schema CREATE. Do not
point it at a shared application database. Future tables receive no implicit privileges;
review/update the explicit manifest after schema changes.

| Connection | Privileges |
| --- | --- |
| Migration owner | DDL/schema migration ownership; trusted administrative boundary; never mount this credential in a runtime process. |
| API LOGIN → `mlp_api` NOLOGIN role | Explicit application-table SELECT/INSERT, UPDATE on mutable state, DELETE only on memberships; audit SELECT/INSERT; schema-version SELECT. Immutable definitions/revisions have no UPDATE/DELETE grants. No bucket table access or schema CREATE. |
| Reconciler LOGIN → `mlp_reconciler` NOLOGIN role | Application-state SELECT excluding caller keys/memberships/notification reads; INSERT/UPDATE only on lifecycle tables; audit SELECT/INSERT; schema-version SELECT. No caller-key/membership writes or schema CREATE. |
| Gateway LOGIN → `mlp_gateway` NOLOGIN role | Routing/key SELECT, schema-version SELECT; bucket SELECT/INSERT/UPDATE; only `api_keys.last_used_at` UPDATE. Read-only membership lookup for OIDC invoker authorization; no audit/jobs access or membership writes; no DDL. |

The tool requires existing unprivileged LOGIN users, no unexpected memberships, and no
object ownership. It also refuses privileged/owning/inheriting pre-existing NOLOGIN roles.
Different Secret names alone are not enough: use different database users and independent
random passwords. Do not reuse a superuser, migration login, or a shared password.

The following are **future operator steps, not commands run against your platform**:

1. Provision a dedicated migration/schema owner and three unprivileged LOGIN accounts
   through the organisation's DBA/secret-management mechanism. Keep credentials out of Git.
2. With the migration-owner connection in `CP_DATABASE_URL`, run
   `python -m controlplane.persistence.migrate upgrade` to apply current head `0021` (including audit protection from `0020`).
3. With an appropriate DBA connection in `CP_DATABASE_ADMIN_URL`, run:

   ```bash
   python -m controlplane.persistence.security \
     --api-user cp_api --gateway-user cp_gateway --reconciler-user cp_reconciler
   ```

   Choose actual existing LOGIN names. The tool does not create passwords and is repeatable.
   Reapply the manifest after every migration before deploying runtime workloads.
4. Create four release-namespace Secrets with `url` keys and their respective SQLAlchemy
   PostgreSQL connection URLs. Defaults are `mlp-controlplane-db-api`,
   `mlp-controlplane-db-gateway`, `mlp-controlplane-db-reconciler` and
   `mlp-controlplane-db-migration`. Chart values now use `database.<component>.existingSecret`
   and `database.<component>.urlKey`; the old shared `database.existingSecret` is obsolete.
5. Install/upgrade the chart only after the grants and Secrets are ready. Its migration hook
   uses the migration Secret; it does not create roles. Check startup/readiness and execute
   the full lifecycle with separated users. The grant test proves privilege boundaries,
   but does not prove every runtime lifecycle operation has its necessary grant.

`CP_DATABASE_ROLE_ENFORCEMENT=true` is the runtime default and mandatory in the production
chart. API/gateway/reconciler reject privileged/incorrect role membership at startup and
HTTP readiness rechecks the boundary. Local chart/Make targets explicitly allow relaxed
roles and mutable images; the disposable release/gateway fixtures also explicitly disable
role enforcement and are not DB privilege acceptance evidence.

### Dependency caveat

The old full MLflow images first installed `mlflow==3.15.0` and then forcibly upgraded
cryptography to `>=50`. Fresh dependency resolution showed MLflow's declared constraint is
`cryptography>=43,<50`; full MLflow also requires `pandas<3`. The new training/MLflow locks
use the supported declared dependency graph: `cryptography==49.0.0`, `pandas==2.3.3`, and
MLflow 3.15.0. This avoids silently ignoring dependency metadata. It is **not evidence of
CVE clearance**, nor a reason to bypass the security scanner. If the supported dependency
version fails the scan policy, upgrade/test MLflow as a compatible set before releasing.
The inference/controlplane locks were not upgraded in that historical batch; cross-image model
serialization compatibility must be tested. Linux amd64 was the resolver target; other
architectures and CUDA/GPU image compatibility have not been verified.

**Recorded serving remediation (2026-10-06):** the classic serving image now uses MLflow
3.16.1/cryptography 50.0.2 and the explicit MLServer metadata fork with fixed Starlette.
The S3 initializer uses standalone `kserve-storage` with fixed protobuf. Their new ARM64
scans and native inference/private S3 gateway checks passed; the old five HIGH findings
are resolved for these two artifacts. Training and tracking-server locks above retain
their separate scope. See [serving-image-security.md](serving-image-security.md).

## Verification completed locally

### UID compatibility and PSA pin follow-up (2026-10-05)

- Lightweight suite: **399 passed, 5 skipped, 188 deselected**; browser and SQL suites
  excluded. Targeted security/admission tests: **16 passed**.
- Actual rendered admission CEL: **184 cases passed**, including missing/wrong version
  rejection and the upgrade from old `latest` namespace labels to the fixed pin.
- Regression checks verify no pod/main UID or primary GID override, restricted contexts
  retained, explicit numeric executor identities and the bundled numeric Dockerfile USER.
  They inspect generated specs; they do not run images with USER 65532.
- Helm lint, Ruff, mypy (**180 controlplane source files**) and diff whitespace checks
  passed. No cluster, Docker daemon, database or AWS resources were started or changed.

### Original hardening batch

- Lightweight suite: **399 passed, 5 skipped, 188 deselected**. SQL parametrizations and
  browser suites were excluded from that run; no cluster startup is part of it.
- Rendered native admission CEL: **178 cases passed**, including PSA downgrade/version
  attempts and the old shared-SA binding. This is offline CEL execution, not proof of
  Kubernetes schema type checks or active policy bindings.
- Isolated PostgreSQL security test: privilege separation, column-only key touch permission,
  denial of DDL/audit mutation/trigger disabling, read-only OIDC membership access, direct-grant detection and grant repair,
  permitted audit INSERT, repeatable grant bootstrap and `0020` downgrade/upgrade.
- PostgreSQL security + schema suites: **6 passed**, against an isolated native fixture.
  The security test also exercised distinct real runtime LOGIN connections through core
  model/deployment setup, reconciler recovery after serving loss, public API-key calls,
  and OIDC invoker denial/grant. Kubernetes/serving used fake adapters in this test;
  it is not a real Argo/KServe acceptance run.
- Ruff passed; mypy passed for **188 source files**, including the administrative tool.
- Helm controlplane default/local lint passed. Kubeconform: default **29 valid**, production
  with synthetic site overrides **33 valid**, zero invalid/errors/skips. Those synthetic
  values are schema-validation inputs, not a deployable site's networking configuration.
- Terraform `aws-dev` validation passed; no AWS plan/apply was run. Legacy inference chart
  default/local lint passed; default lint warns about missing local artifact credentials.
- `scripts/lock.sh training` and `scripts/lock.sh mlflow` regeneration passed with the
  pinned resolver. Administrative CLI help, shell syntax and `git diff --check` passed.
- One final lightweight run hit an intermittent failure in the unchanged in-memory
  concurrent project-creation test (two returned UUIDs). The isolated rerun and subsequent
  full lightweight run passed.
  The fake UnitOfWork's concurrency path was not modified by this hardening task; retain
  this as test reliability debt rather than counting a retry as concurrency proof.
- Version locks resolved without installing the MLflow/training dependencies or building
  images. No AWS plan/apply, live cluster tests, GPU tests or image scans were performed.

The subsequent identity/concurrency fixes and their migration steps are recorded in
[concurrency-identity-audit.md](concurrency-identity-audit.md).

## Tests and evidence still required

Run these on an **isolated acceptance deployment**, with the exact released images/chart,
controller versions, enforcing CNI, separated DB credentials and production site values.
Record the source SHA, image digests, chart version, controller versions, settings, timestamps,
assertions and retained reports; a script's presence is not a passing result.

### Security boundaries and upgrade behavior

- [ ] Create a project; verify all six PSA labels. Submit a privileged/root/hostPath pod
  and assert the expected restricted PSA denial. Test removing/lowering PSA labels and
  missing/incorrect version labels while impersonating the reconciler; assert the platform policy denial.
- [ ] Reconcile an existing project: RoleBinding subject changes to `mlp-training`, old
  `mlp-workload` loses executor permissions, serving/storage accounts disable automount.
  Replace existing pods and prove serving has no projected Kubernetes token and cannot
  write WorkflowTaskResults; training can report results but cannot access foreign projects.
- [ ] Run real Argo job and multi-step pipeline under restricted PSA, including init/wait
  containers, output volumes and private image pulls. Run permission-sensitive training
  images with numeric USER 1000 and 65532; verify primary UID/GID, shared-volume outputs
  and executor artifact handling. Root or name-only USER images must fail safely.
- [ ] Upgrade Kubernetes with the same platform release: enforcement must remain `v1.30`,
  while warn/audit follow the new server version. Verify migration from old `latest` labels
  and review newer restricted warnings before advancing the enforced baseline.
- [ ] Run real KServe custom function, MLflow model and pinned Hugging Face model; inspect
  predictor, storage-initializer and Knative sidecars for PSA/token behavior. Deliberate
  security-context drift must be rejected or repaired without a false READY result.
- [ ] Run the installed admission gate (policy observed generation, completed zero-warning
  Kubernetes type checking, matching Deny bindings and impersonated request cases).
- [ ] Prove project A → B CNI denial, allowed serving ingress and intended egress only.
- [ ] Use each real DB LOGIN to run allowed API creation/membership/deployment/pipeline
  operations, reconciler state transitions/discovery/rollback and gateway request/key usage.
  Assert gateway cannot read audit/jobs or mutate memberships/key grants/expiry/revocation. Test both API-key and OIDC invoker calls.
- [ ] Attempt runtime DDL, audit UPDATE/DELETE/TRUNCATE and trigger disabling. Repeat after
  credential rotation and backup/restore. Verify restored owners, grants and triggers before
  exposing recovered API/gateway traffic; logical backups do not provision LOGIN passwords.
- [ ] Remove an IdP admin group while a browser session is active; verify its five-minute
  bound, expiry denial and fresh sign-in claims. Verify normal session expiry, old-cookie
  rejection, large-claim handling, HTTPS cookie flags, CSRF and IdP logout confirmation.
- [ ] Exceed ephemeral-storage quota; verify rejection and node disk-pressure behavior,
  including sidecars and emptyDir use. Tune the defaults for the target workload sizes.
- [ ] Reject tagged training images in registration and persisted legacy pipeline submission;
  run the same pinned digest twice and compare image IDs/lineage. Test private registry
  wrong/rotated credentials and storage-credential cleanup.

### Availability, performance and recovery

- [ ] Canary traffic with Prometheus revision attribution; bad canary → rollback.
- [ ] Scale-to-zero → wake-up and ingress TLS streaming/disconnect behavior.
- [ ] Leader pod kill, Kubernetes API partition, progressing slow-MLflow alias pass,
  watchdog fail-stop and node drain with PDB behavior.
- [ ] Gateway 1/2/4 replica load and PostgreSQL lock contention; DB outage/recovery,
  fail-closed responses, thread/pool growth and limiter p95/p99.
- [ ] Backup → empty-database restore → verify grants/triggers → recovered API/gateway;
  retain measured RPO/RTO and dependency/credential rotation evidence.
- [ ] GPU/vLLM operation, capacity reservation and exact pinned HF commit download.

### Build, AWS and remaining debt

- [ ] Build every changed image, run `pip check` and import/model-load smoke checks;
  generate SBOMs and apply the vulnerability/secrets scan gates to the exact image digests.
- [ ] Verify training/inference serialization and supported Linux architectures; resolve
  the MLflow/cryptography constraint as a supported tested set if scan policy requires it.
- [ ] Prepare AWS platform deployment completeness. Verify explicit EKS API CIDRs,
  dedicated pod SG policy, TLS and authorized/unauthorized RDS packet paths before use.
- [ ] Configure explicit trusted NodePort source CIDRs for legacy lab use; verify monitoring
  and prediction ingress. Do not interpret the legacy chart as a production platform.
- [ ] Investigate/reproduce the in-memory concurrent project-creation test timing failure.
- [ ] Implement safe rate-bucket GC; durable/streamed training logs; exact model-specific
  tokenizer accounting if hard billing quotas are required.

Existing execution tooling and broader scenarios are documented in [acceptance.md](acceptance.md),
[recovery.md](recovery.md) and [local-verification.md](local-verification.md).
