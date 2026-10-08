# Model Monitoring core — local acceptance, 2026-10-08

Immutable classic-model checks against pinned S3/MinIO CSV/Parquet reference/observed
versions, optional delayed feedback, bounded scientific worker, append-only reports,
feature-drift inbox notifications and typed report UI. [Contract](../../../model-monitoring.md).

- Backend **878 passed, 13 skipped**; frontend **22 passed**, TypeScript/Vite build passed.
  Focused browser checks pass typed creation, report and pinned-version navigation.
  Memory and PostgreSQL tests cover permissions, publication/reconciliation, malformed
  output, frozen identities, retry, pipeline attribution and pre-commit rollback.
- Actual Linux runtime worker suite **21 passed** (9 batch, 12 monitoring), including
  CSV/Parquet, missing/unseen/insufficient distributions, integrity verification,
  delayed-feedback metrics, duplicate keys and bounded joins.
- Final control-plane source `b54e53754525fd2da040dd5669226e52fd49a2bd`: **all 12 control-plane image
  gates passed**, including clean migration to `0026`, dependency isolation, readiness
  outage/recovery, SBOM, fixable HIGH/CRITICAL and secret gates. Gateway race/vet/build
  and its unfiltered HIGH/CRITICAL gate passed; it accepts additive schema `0026`.
- Local acceptance Helm revision **41**: actual Ridge batch output in MinIO feeds a
  monitoring Argo Job. Shifted income produces PSI **1.625**; unchanged age PSI **0**.
  Delayed regression feedback matches **8/9** predictions, leaves one prediction and
  one label unmatched, and gives MAE/RMSE **0.1**. The drift report appears in the inbox.
  Retry produces a distinct report with the same pinned inputs. A pipeline report
  retains its step/producer identity. Duplicate ground-truth keys fail without a report.
  Nine synthetic rows establish correctness, not production throughput.
  Runtime/gateway gates used the initial `483f5032` snapshot; their source remains
  identical in the final control-plane snapshot.
- Deployed UI checks cover feature/metric tables, coverage, pinned model navigation,
  Job/Pipeline report links and desktop/mobile captures. No JavaScript errors.
- Control-plane: **89 distributions**, **452,537,856 bytes** uncompressed artifact size;
  scientific runtime **825,035,264 bytes**, reused for batch and monitoring. No new
  dependency profile. Fixable HIGH/CRITICAL and secret findings **0**. Both full Python
  image scans retain **55 unfixed Debian findings** (53 HIGH, 2 CRITICAL).

A successful measurement may report feature drift; it does not fail the run or imply
prediction error. Classification metrics use labels, not inferred AUC. Scheduling
retains pinned datasets. Online capture, automatic production-window selection,
performance-threshold alerts and automatic retraining are not claimed.

Own synthetic project/model/S3 fixtures are removed after UI checks. Obsolete image/cache
cleanup preserves active/configured references and data volumes. Raw JSON/SBOM/Trivy,
source bundle, scripts, logs, screenshots and outputs stay outside Git at
`/Users/ofk/ml-platform-infra-artifacts/2026-10-08/model-monitoring/`.
[ARTIFACTS.sha256](ARTIFACTS.sha256) records their identities. Local cluster validation
is separate from production deployment. Initial failed/partial checks remain in the
external evidence and are superseded by the named final checks.
