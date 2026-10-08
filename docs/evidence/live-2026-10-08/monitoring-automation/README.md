# Dataset-triggered NYC monitoring acceptance — 8 October 2026

Local acceptance cluster, Helm revision 49. Implementation image source: `a3202e3`.

- Cron started a real NYC TLC February 2025 scoring pipeline: **1,000,000 rows**.
- `fare-predictions` version **3** was published successfully.
- Rule `nyc-published-fare-quality` automatically created monitoring; no manual monitoring run.
- Run `c0b12ac4-1413-4e8e-8782-a412df5090d3` succeeded; report `de2e4d10-8b3f-4a5d-abd4-8a8b2bb19f77` is **STABLE**.
- Ground-truth match: **1,000,000 rows, 100% coverage**, pinned model/reference and same processing date.
- Reconciler restart preserved the same execution/run; no duplicate.
- Schedule paused after demonstration. Data, rule, execution and report retained in UI.

Backend: **951 passed, 24 skipped**. Browser creation/pause/history test: **1 passed**.
Control-plane release gate: **12/12 passed**, including migration 0030, dependency isolation,
readiness recovery, SBOM, fixable HIGH/CRITICAL and secrets scans. Go gateway gate passed.

The first live dispatch exposed missing reconciler INSERT permission on job_definitions.
An owner-issued grant repaired the live role; the pending event retried without rerunning
batch. Migration and idempotent role provisioning now include this permission. Dedicated
PostgreSQL role/automation regression checks: **16 passed, 1 memory-only skip**.
Existing 0030 installations need role reprovisioning or the documented narrow grant.
The deployed image remains a3202e3; the subsequent source change repairs provisioning.

Monitoring failure is independent of batch success. This is historical-data replay on
one local node, not online prediction capture or a production capacity benchmark.
Raw JSON/SBOM/data remain outside Git at `/Users/ofk/ml-platform-infra-artifacts/2026-10-08/monitoring-automation`; hashes below identify the artifacts.
