# Current platform status

Updated 2026-10-06. Initial code review used `974a7b0`; subsequent live checks used
the commits recorded in [live evidence](evidence/live-2026-10-06/README.md), with the latest
image gate at `b7a3420`. Historical counts retain their original scope.

## Implemented

- API Secret CRUD and workload/log reads use owned-project RoleBindings. Reconciler writes
  have fail-closed native admission policies for project ownership and exact RBAC shapes.
- Chart **0.3.1**, Kubernetes **1.30+**; restricted PSA enforcement is pinned to `v1.30`.
  Training uses `mlp-training`; serving uses token-disabled `mlp-serving` or revision-scoped
  storage accounts. Training images preserve their numeric non-root image USER.
- API/gateway/reconciler default to two replicas with PDBs and pod spreading. Reconcilers
  use Lease election, bounded Kubernetes transport and main-loop progress watchdog;
  classic-model alias passes heartbeat between models.
- Separate migration/API/reconciler/gateway database credentials and runtime grants;
  migration advisory locking and expand/contract release contract. Schema head **0021**:
  `0020` protects append-only audit rows; `0021` enforces active rollout version uniqueness.
- Stable OIDC issuer/subject identity, shorter bounded browser sessions and no ID token in
  cookies. Username-only grants/cookies fail closed and require verified re-grant.
- Concurrent membership changes preserve the last admin; model/endpoint field updates
  have separate ownership. Pipeline/run replay includes execution/lineage fingerprints.
  Deploy revalidates locked versions and reuses revisions after concurrent lookup.
- Manual promotion of an actively reserved rollout version returns 409. Failed/rejected
  candidates recover toward stable serving; real traffic restoration is still unproven.
- Shared PostgreSQL inference budgets, maximum-replica GPU admission, SHA-pinned Hub and
  function images, automatic lineage-scoped model discovery, deployment deletion,
  optional workflow retention and owned storage-account cleanup.
- Project Secret API/UI, classic S3/MinIO storage-initializer authentication and manual
  image/SBOM/scan, admission, CPU, limiter and recovery gates are prepared.

## Recorded verification

The latest recorded lightweight suite is **419 passed, 5 skipped, 217 deselected**;
Ruff and mypy (**183 source files**) passed. Targeted model/promotion/rollout suites recorded
**93 passed, 2 skipped**, including real PostgreSQL lock-order tests. Earlier isolated
PostgreSQL tests covered grants/audit protection and identity/concurrency invariants.
See [concurrency-identity-audit.md](concurrency-identity-audit.md) and
[security-hardening.md](security-hardening.md) for exact scopes. These historical counts were not rerun in full. The new live batch separately passed
24 project networking/Secret tests and 16 bootstrap/release tests.

On isolated single-node `kind-mlp-acceptance` (Kubernetes 1.32.0), the control-plane image
passed all nine runtime/scan/SBOM checks, including clean migration through 0021. Four
admission policies passed type checking and nine real server dry runs; 24 API namespace
RBAC checks and restricted-PSA checks passed. An enforcing network-policy engine denied
cross-project traffic while permitted ingress and DNS worked. Lease takeover took 31.44s;
the reconciler PDB rejected the second concurrent eviction. API/gateway rolling restarts
passed 200/200 probes after adding a 10s preStop drain. This does not establish node-loss,
hung-leader, strict-egress, inference, GPU or identity-provider acceptance.
See [live evidence](evidence/live-2026-10-06/README.md).

## Required before deployment acceptance

1. Review existing duplicate active rollouts and inconsistent manually promoted canaries
   before migration `0021`; repair with trusted operators. Migrate through `0021`, reapply
   runtime grants and provision four independent database Secrets. Re-grant verified stable
   identities; replace legacy training definitions and old shared-SA pods.
2. Repeat the passing control-plane image gate for the release artifact/architecture. The
   isolated cluster uses separated runtime credentials with role enforcement; training,
   serving and storage images still need their own release scans. See [installation.md](installation.md).
3. Extend the passing admission/PSA, ingress isolation, election/PDB and rolling-restart
   checks to serving sidecars, strict egress, workload secret/token boundaries, hung-leader
   and multi-node drain/loss drills.
4. CPU project → Argo training → MLflow registration/discovery → evaluation passed.
   The logged-model URI fix is deployed; native ARM64 serving reaches READY and the
   real gateway prediction returns 200 with the expected result. Complete canary/rollback
   → cold-start, secret rotation/private pulls and broader initializer checks.
   Knative feature flags and MLflow PostgreSQL driver selection also required live fixes;
   the upstream MLServer and initializer are AMD64. Native ARM64 replacements now
   load the model and reach KServe READY with project storage credentials. The gateway
   JSON-route correction is deployed and verified with real inference traffic. The replacements
   have five fixable HIGH scan findings, so their production image gate is blocked.
   See [ARM64 evidence](evidence/live-2026-10-06/serving-arm64/README.md).
5. Measure shared-limiter load/outage recovery and backup/restore with roles/triggers,
   recovered services and RPO/RTO. GPU/immutable HF, ingress/TLS and AWS isolation remain
   separate resource-dependent gates. Commands are in [acceptance.md](acceptance.md).

## Remaining implementation work

- Persist a stable audit `actor_subject` alongside display username; authorization identity
  is fixed, but old/new audit actor display names can still be ambiguous.
- Safe bounded rate-bucket GC, preserving token debt, in-flight and concurrent reservations.
- Durable Argo log archives and streaming; operational function cold-start metrics.
- Model-specific exact token accounting, fuller platform disaster recovery and AWS
  deployment completion. Private/gated initializer paths beyond classic S3 need contracts.

CI remains intentionally manual/disabled; it is not a new defect. Keep deployment evidence
separate from implementation status. This page summarizes the roadmap; detailed findings
and procedures remain in [technical-review.md](technical-review.md),
[operations.md](operations.md) and [recovery.md](recovery.md).
