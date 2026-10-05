# Concurrency and identity audit review

Reviewed on 2026-10-05 against `17d18b2`, following the external audit of `9f984db`.
The UID/PSA follow-up does not change these paths. The findings below describe the original reviewed behavior. The subsequent fix implements
all nine corrections; implementation and validation are recorded below.

No Kubernetes cluster, Docker daemon, PostgreSQL instance or AWS resources were started
or modified. Six behaviors were reproduced using existing in-memory UnitOfWork and fake
provider fixtures, or direct principal conversion. These reproductions establish application
behavior, not PostgreSQL concurrency or real data-plane evidence. The other three findings
were checked against SQL statements and transaction boundaries.

## Findings

| Priority | Finding | Evidence and qualification | Required change |
| --- | --- | --- | --- |
| HIGH | OIDC authorization uses a mutable username | `adapters/identity/oidc.py:principal` prefers username/email over `sub`; `domain/access.py:Principal.subjects` matches `user:<username>`. Two claims with different subs and the same username produced identical authorization subjects. An explicit `user:alice` platform-admin grant also applied to both identities. Signature/sub validation does not prevent this collision. | Carry an issuer-scoped stable subject separately from display username through bearer authentication, sessions, owner creation, memberships and explicit platform-admin grants. |
| HIGH | Active rollouts can share a candidate version | `application/rollouts.py:start` locks the project/deployment but checks only that deployment's active rollout. SQL and memory repositories enforce only deployment uniqueness. Sequential starts on two deployments already reproduce the failure; simultaneous starts are unnecessary. After both reached 100%, rollback of A rejected the shared version; two subsequent reconciliation attempts for B raised promotion Conflict while B stayed PROGRESSING at 100%. | Reserve the version transactionally, enforce active-version uniqueness at the DB boundary, and recover already-invalid active rollouts safely. |
| HIGH/MEDIUM | Last-admin check is not serialized | `application/members.py:set_role/remove` read memberships without locking the project. SQL updates/removals operate on separate membership rows, permitting both transactions to observe the other admin and then remove/demote both. This was statically verified; no real two-connection test was run. | Lock the project before reading memberships for all mutations, then check and commit under that lock. Cover mixed removal/demotion and lock ordering. |
| MEDIUM | Threshold and alias-drift updates overwrite each other | `persistence/sql.py:SqlModels.update` writes both fields. Alias reconciler `_record` actually re-reads the model immediately before updating, so the external audit's long-lived initial snapshot is not the exact path. An admin commit between this re-read and the UPDATE still loses the new thresholds. `_set_drift` has the same read/write gap. | Separate threshold and alias-drift writes in ports, SQL and memory adapters, or use row-wide revision/CAS and retry. |
| MEDIUM | Pipeline replay has an incomplete fingerprint | Changed commit SHA with the same key returned the original run with its old SHA. Normal replay compares definition and timeout only. The unique-race recovery path compares timeout only, also admitting another pipeline definition/version. | Resolve the requested definition once and retain its ID; compare definition ID, commit SHA and effective timeout on both normal and race-recovery replay. |
| LOW/MEDIUM | Retry replay ignores its original run | Retrying two different failed runs of the same job with one key returned the first retry, including the first run's retry_of. Both normal and race-recovery paths omit retry_of. | Compare job definition, effective timeout and retry_of in both paths; distinguish ordinary create from retry. |
| MEDIUM | Deploy permits a version rejected during artifact lookup | A deterministic hook rejected v2 while `_artifact` ran. `deploy` subsequently committed desired revision 2 despite v2 being REJECTED. `_ensure_revision` has the same second-phase stale-version gap. | Re-read and validate the version in the write transaction, and serialize/CAS against concurrent lifecycle transitions through commit. A re-read alone still permits a transition immediately after the read. |
| LOW/MEDIUM | ensure_revision can duplicate a model-version revision | During A's artifact lookup, B created the revision. After lookup A acquired the deployment lock but did not re-check existing revisions, creating another revision for the same version. Both remained present. | After the lock, re-check all existing revisions for the version and return the existing one. Do not add a blanket deployment/version unique constraint without reviewing intentional redeploy/rollback history semantics. |
| MEDIUM | Endpoint field ownership is mixed | `SqlEndpoints.update` writes lifecycle fields and exposure/limits together. Access settings, deployment kind changes and reconcilers share it. expected_status is real protection when status changes, but does not reject competing writes that preserve status, including first-deploy kind/protocol changes versus access updates. This was statically verified, not tested on PostgreSQL. | Separate access-setting and lifecycle writes across ports/adapters/callers; define timestamp/CAS semantics explicitly. |

The external audit's final session item repeats the identity issue; it is not a tenth
independent bug. Short session lifetimes reduce claim staleness but do not fix identity
reuse. Likewise, existing project locks in rollout creation serialize requests but do not
establish version exclusivity: a second request can start another rollout after the first
commits.

## Identity migration constraints

- Existing `user:<username>` memberships cannot be safely converted by resolving whoever
  currently owns that username: this could preserve the exact privilege transfer being fixed.
  Require a trusted account mapping or explicit administrator re-grant; unresolved grants
  must not silently authorize a new OIDC identity.
- Change session purpose/version so username-only cookies cannot retain legacy authorization.
  Preserve the stable issuer/subject in new cookies and project-owner creation.
- Handle explicit user platform-admin configuration and development/static identity modes
  deliberately. Group grants remain a separate identity-provider-managed authorization path.
- Use unambiguous encoding for issuer and subject; test issuer isolation and subject length
  against membership storage/API validation limits. Keep username available for display/audit.

## Recommended implementation order

1. Reproduce and fix rollout reservation plus rejected-candidate recovery. Review migration
   behavior for existing duplicate active reservations before adding the unique constraint.
2. Implement stable OIDC identity and a fail-closed membership/session migration path.
3. Serialize membership mutations; split model and endpoint field ownership.
4. Unify full idempotency fingerprints on normal and unique-race recovery paths.
5. Revalidate/serialize version state after lookup and re-check revision reuse under lock.

## Required regression and acceptance evidence

- [x] Retain deterministic regression tests for the six reproduced behaviors above.
- [ ] Two PostgreSQL connections: concurrent last-admin removal, demotion and mixed
  operations leave at least one admin; permitted/no-op operations remain usable.
- [ ] Two deployments: same candidate rollout is rejected before duplicate reservation;
  same/different candidate and project cases behave as intended. Direct DB writes cannot
  bypass the invariant. Verify migration against pre-existing duplicate active rollouts.
- [ ] Existing rollout with a rejected candidate exits safely, restores stable traffic and
  releases reservations after controller restart/failure; never stays at 100% with retries.
- [ ] Barrier-controlled SQL writes preserve both threshold and alias-drift changes, and both
  endpoint lifecycle and access-setting changes, including equal-status interleavings.
- [ ] Force unique-key races between different pipeline versions/commits/timeouts and retry
  parents. Identical requests replay; different execution/lineage fingerprints conflict.
  Changing the latest definition during recovery must not change the original request's ID.
- [ ] Reject/archive a version during artifact lookup and between revalidation/commit;
  neither deploy nor ensure_revision commits an invalid version.
- [ ] Concurrent ensure_revision calls return the same revision; intentional redeploy and
  rollback behavior remain correct, with no extra artifact/revision side effects.
- [ ] Distinct subjects sharing a username get different permissions; username changes
  retain identity, different issuers cannot collide, and old cookies/grants fail closed.
- [ ] Real OIDC browser/bearer/gateway flows, owner grants, explicit platform-admin grants,
  membership migration and audit display are validated together.
- [ ] Real Argo/KServe rollout failure drills verify actual traffic restoration; fake-provider
  percentages alone are not proof of production traffic routing.

For the prior hardening implementation and pending platform acceptance checks, see
[security-hardening.md](security-hardening.md).

## Implemented corrections

- OIDC carries an issuer/sub digest in `Principal.subject_id`; username remains display/audit
  data. Membership subjects, project ownership, explicit user platform-admin grants,
  gateway caller buckets and notification-read keys use stable identity. `/me` exposes
  `user_subject`. Session-v3 invalidates all prior browser cookies. Existing username grants
  are deliberately not auto-migrated; see [identity.md](identity.md#upgrading-from-username-grants).
- Migration `0021` adds a partial unique index on active rollout `model_version_id`; the memory
  adapter and service implement the same reservation contract. Start revalidates the locked
  version. Reconciliation rolls invalid/rejected candidates back, including rejection during
  the final promotion transaction. A rollback failure remains retryable until serving repairs.
- Membership writes lock their project before reading/checking membership state.
- Model `update_thresholds` and `update_alias_drift` have disjoint field ownership. Endpoint
  `update_lifecycle` and `update_access` likewise preserve each other's fields. Runtime default
  limits initialize conditionally against original timestamp/default values and never replace
  a concurrent administrator change.
- Pipeline replay checks frozen definition ID, commit SHA and effective timeout in both paths.
  Latest version is resolved before a possible race. Run replay checks job, timeout and retry
  parent on both normal and unique-race recovery paths.
- Deployment/ensure_revision lock and revalidate the version after artifact lookup, holding
  that lock until commit; state-changing SQL UPDATEs must wait. ensure_revision re-checks
  existing version revisions after acquiring its deployment lock. Intentional deployment
  revision history remains supported.

### Migration and rollout precautions

No existing deployment was modified by this task. Before installing migration 0021, inspect:

```sql
SELECT model_version_id, count(*)
FROM rollouts WHERE status IN ('PENDING', 'PROGRESSING')
GROUP BY model_version_id HAVING count(*) > 1;
```

Abort/rollback duplicate active rollouts using the existing release, and verify stable traffic
before applying the migration. The unique-index migration fails if duplicates remain; it does
not delete or declare traffic-bearing rollouts finished. Deploy the migration and matching
application release together. Re-grant verified stable OIDC users and adjust platform-admin
configuration as described above; username-only grants/cookies are intentionally fail-closed.

### Verification of the fix

- Final lightweight suite: **416 passed, 5 skipped, 210 deselected**. SQL parametrizations
  and browser suites were excluded; fake controllers were used.
- Isolated native PostgreSQL: **62 passed, 3 skipped, 69 deselected** across targeted regression,
  schema, real OIDC API, function and LLM suites. Memory row-lock cases were skipped. This includes actual two-connection
  remove/remove and demote/demote last-admin races, DB rollout uniqueness, stale field-owner
  writes in both ownership directions, lookup interleavings, lock-held status transitions,
  final-promotion rejection recovery, signed-token username reuse/rename, schema/ORM
  agreement and full downgrade/re-upgrade.
- Ruff and mypy (**182 source files**) passed. No Kubernetes, Docker or AWS resources were started or modified.
- Browser/controller/data-plane acceptance remains pending. SQL field-owner tests deliberately
  submit stale snapshots; broader load, mixed membership operations, migration of real legacy
  identities/duplicate rollouts, and real traffic restoration still require acceptance evidence.

## Follow-up: manual promotion during an active rollout

The follow-up audit of `16d8a68` identified a further correctness bug: manual promotion
could turn an active canary into CHAMPION before the rollout verdict, leaving its stable
version ARCHIVED even after the canary rolled back. This path is now closed.

`PromotionService.promote` locks the requested version before querying its active rollout
reservation in the same transaction. A PENDING or PROGRESSING reservation returns Conflict
(HTTP 409); no champion transition, promotion record or audit event is committed. Rollout
start uses the same version lock and the new `get_active_by_version` repository query. SQL
uses the existing active-version index from migration 0021; no further migration is needed.
The rollout's own successful promotion remains permitted. A completed/aborted reservation
no longer blocks the normal manual promotion rules or already-champion idempotent replay.

Two PostgreSQL connections test both orders while deliberately holding the winning version
lock: rollout-first blocks then rejects manual promotion, while promotion-first completes
before rollout start observes CHAMPION. The rollout path takes project/deployment locks
before version lock; manual promotion takes only the version lock and does not subsequently
request project/deployment locks.

- Final lightweight suite: **419 passed, 5 skipped, 217 deselected**. Browser and SQL
  parametrizations were excluded from this run.
- Targeted promotion/model/rollout suites: **93 passed, 2 skipped**. The skipped cases are
  memory variants of the two PostgreSQL concurrency tests.
- Regression tests cover PENDING and PROGRESSING denial, stable champion preservation on
  abort/failure, successful rollout promotion, terminal reservation release and manual replay.
- Ruff and mypy (**183 source files**) passed. Native PostgreSQL was an isolated temporary
  fixture; no Kubernetes cluster, Docker daemon or AWS resources were started or changed.
- [ ] Live API/canary drill: promotion returns 409 during traffic shifting, failed canary
  restores stable serving, and MLflow champion/candidate aliases follow the intended DB state.
- [ ] Inspect pre-upgrade active CHAMPION rollouts: distinguish versions promoted before
  rollout start from versions manually promoted mid-rollout by the old release. Existing
  inconsistent champion/serving/alias state needs explicit review and repair; the guard does
  not infer or rewrite historical promotion intent.

### Remaining audit identity nuance

`current_actor()` still records the display username. Reused usernames are authorization-safe
but historical audit actors can be ambiguous. Add a separately persisted stable `actor_subject`
while keeping the display actor, propagate it through audit API/export/recovery paths, and test
rename/reuse across both bearer and browser flows. Do not backfill old username-only events
by resolving today's username owner; retain unknown identity where no trusted mapping exists.
This metadata enhancement remains pending and is not part of the promotion fix.
