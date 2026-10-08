# Batch and monitoring hardening — local acceptance, 2026-10-08

Four contracts are enforced together:

- Batch and monitoring Jobs reserve explicit ephemeral storage from bounded input,
  output, model and SQLite/journal needs, with scratch headroom. Smaller custom
  reservations are rejected. Namespace quota and node allocatable storage are checked;
  Kubernetes remains responsible for current contention and admission. Legacy batch
  Jobs receive the computed disk reservation at compilation.
- New batch definitions require a trusted model manifest. Only listed files are
  downloaded; their size and SHA-256 are checked before model deserialization.
  Existing definitions without a manifest must be recreated before another submission.
- PINNED, LATEST_AT_EXECUTION and BY_PROCESSING_DATE resolve an exact catalog version
  into the immutable Job/Pipeline execution snapshot. Idempotent replay and Job retry
  retain it. Scheduled date binding uses the planned local day; missing data records
  a missed execution without submitting a run.
- Migration `0027` enforces monitoring ownership with composite foreign keys;
  `0028` adds execution snapshots and a dataset processing-date lookup index.

Validation: **929 backend tests passed, 21 skipped**, including PostgreSQL migration,
transaction/snapshot and cross-project direct-write rejection tests. Eight skips are
memory-store variants of database-only ownership checks. Frontend build and **24 unit
checks** passed; **5 focused browser checks** passed. Go gateway race tests passed.
The actual Linux scientific runtime passed **24 worker tests**.

All **12 control-plane image gates** passed, including clean migration to `0028`,
readiness outage/recovery, pip consistency and dependency isolation. The control plane
has **89 distributions**, with no inference stack reintroduced. SPDX/CycloneDX SBOMs
were generated. Fixable HIGH/CRITICAL and secret gates found **0**; the full Python image
scans retain **55 unfixed Debian HIGH/CRITICAL findings**. The gateway's unfiltered
HIGH/CRITICAL gate passed.

Final Helm revision **44** runs the tested worker. An earlier revision placed its digest
under an unused Helm key and still selected the old worker. The configuration was
corrected and the entire small acceptance scenario rerun: actual CSV/Parquet predictions,
separate retry output/version, pipeline lineage, corrupt-input rejection, latest snapshot
replay/retry, scheduled date resolution, explicit pod disk resources and feedback reports
passed. A same-size S3 model mutation fails with **"model artifact differs from its
immutable manifest"** before MLflow loads the model, and publishes no dataset.

Batch main containers reserve **3 GiB**, and feedback monitoring reserves **5.5 GiB** at
these default byte bounds. These are declared reservations, not measured disk usage.
The nine-row fixture checks correctness, not throughput; the separate
[one-million-row scenario](../million-row-acceptance/README.md) exercises real data.

Clean build source snapshots: control-plane/migration
`032b5539884a6a1bc1ec9a5eaf6fc7a1b6a063db`; worker/gateway
`0c5861f62cb9fc7b015a25e7d455820142dd18e1`. Worker/gateway runtime sources are unchanged
between these snapshots. Image digests:

| Component | SHA-256 digest |
|---|---|
| Control plane | `7959c8c5edfd88fe9787d4139c3e82ba880ea31059ee28bcfc88e15476440f10` |
| Migration | `c8405f98adbe58577879b24612fe20980897e6b168c88b404cdcfa384ae25f1e` |
| Worker | `f18953fbde128be2d37ff38ca40298b030bec366dd4c9d258b829ab54ff92139` |
| Gateway | `b8050e56a859efa6942632696867dcc93699bb0deb3e75e22864713ae97bd6fe` |

Raw reports, SBOMs, logs and acceptance helpers stay outside Git at
`/Users/ofk/ml-platform-infra-artifacts/2026-10-08/batch-hardening/`.
[ARTIFACTS.sha256](ARTIFACTS.sha256) binds their identities. Partial failed attempts remain
there and are superseded by `acceptance-final.log` and `live.json`. Own small synthetic
fixtures are removed; the real-data project remains available for review. Feature Store
is not included. This is local acceptance, not a production rollout.
