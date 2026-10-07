# Scheduling P0 acceptance — 2026-10-08

Implemented contract: [scheduling.md](../../../scheduling.md).

- Backend: 741 passed, 13 skipped under UTC. PostgreSQL is real in the SQL
  fixture, including concurrent dispatchers, atomic rollback and runtime grants.
- Existing UI regression: 158 passed; the existing promote test was deselected.
  New real-browser scheduling flow: 1 passed. Frontend: 22 passed.
- Modified-source type checks: 15 files passed. Ruff and diff checks passed.
- Clean-source control-plane release gate: 12 checks passed, including migration
  `0022`, runtime dependency isolation, readiness/DB recovery, SPDX and Trivy.
- Existing local acceptance cluster: Helm revision 29. API, reconciler, gateway and
  log service each have two ready replicas. Active registry digests were rechecked
  after cleanup.
- Real Argo acceptance: two same-target schedules produced one dispatched run and
  one queued intent with no run; pause preserved the queue and the started run;
  queued LATEST retained immutable v1 after v2 publication and dispatched once;
  an independent scheduled Job also succeeded. The disposable project was deleted
  after screenshots, with its schedules paused first.

Trivy: fixable HIGH/CRITICAL is zero for control-plane, migration and Go gateway.
Unfiltered scans retain 55 unfixed HIGH/CRITICAL findings in each Python image's
Debian base (53 HIGH, 2 CRITICAL); Go gateway has zero total HIGH/CRITICAL. The
control-plane and migration secret scans report zero findings at all severities.
Scheduling adds only `croniter` to the control-plane lock. Runtime dependency
isolation continues to exclude the inference scientific/AWS/Prometheus stack.

The gateway accepts explicitly compatible heads `0021`/`0022` and was rolled out
before the additive migration. The initial scheduling rollout used the wrong Helm
key for the migration image; its schema guard correctly stopped the new pods. The
verified minimal image applied `0022`, then a corrected `migrations.image` hook
completed successfully. Final replicas and registry references all passed checks.

Source for the clean control-plane image: `e22d43ea2ab351ffaa2e10ce0866297660d21fbd`
(local acceptance snapshot, not a remote branch commit). Gateway source is that
snapshot plus `gateway-source.patch`; its build inputs are separately hashed.

Raw JSON, SBOMs, logs, screenshots and source bundle are outside Git:
`/Users/ofk/ml-platform-infra-artifacts/2026-10-08/schedules/`.
`ARTIFACTS.sha256` hashes the reports used above. Backfill/event triggers and
first-class batch inference remain outside P0.
