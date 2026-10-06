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
  candidates recover toward stable serving; candidate-only rollback restored real traffic
  in the ARM64 lab.
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
hung-leader, strict-egress, GPU or identity-provider acceptance.
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
4. The full seven-phase CPU lifecycle passed on the single-node ARM64 lab: project/RBAC,
   training/discovery/evaluation, serving/gateway, healthy canary, credential rotation,
   same-revision drift repair and zero-pod reactivation (1.25s request). Candidate-only
   503s separately triggered rollback; stable metrics remained healthy, candidate was
   rejected and 30 restored requests returned 200. Storage accounts were retained across
   canary and scoped cleanup preserved foreign/other-deployment accounts. These are dirty
   acceptance artifacts with local auth none, not production/OIDC/multi-node proof.
   KServe zero-delay/empty-list defaulting fixes are deployed; harness fixes retain
   metric samples during traffic and wait for matching backend apply identity.
   The five serving/initializer HIGH findings are resolved in new ARM64 acceptance
   images: zero fixable HIGH/CRITICAL, SBOMs and native inference passed; real private S3
   loading/gateway also passed. Final clean release/target-architecture reruns remain open.
   See [dependency remediation](serving-image-security.md).
   Shared-time Prometheus queries passed the deployed-image rollback rerun (candidate
   error rate exactly 1.0). Forced Secret deletion also passed in an isolated project:
   running value retained, future startup blocked, restored Secret recovered with a new
   boot/value. Private registry, cleanup conflict/outage retry and broader private
   initializer provider paths remain separate gates.
   See [ARM64 evidence](evidence/live-2026-10-06/serving-arm64/README.md).
5. Shared-limiter load measured 1/2/4 distinct gateway replicas: 50/100 RPS preserved
   shared budgets without availability errors; 500 offered RPS stayed within budget but
   failed availability (transport timeouts, 10/23/41 observed lock waiters). Isolated
   limiter latency telemetry and an in-cluster generator are needed for diagnosis.
   Basic real PostgreSQL outage/recovery passed across four gateways, including expired
   auth/route caches; those SQL failures now return redacted 503 rather than 500. The lab
   DB now has a Bound PVC; the initial emptyDir fixture lost its historical rows and was
   rebuilt, not restored from backup. Sustained outage/thread-growth remains open.
   Run backup/restore with roles/triggers,
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
