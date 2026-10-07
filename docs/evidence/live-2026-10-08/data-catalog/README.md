# Data catalog acceptance — 2026-10-08

Immutable S3/MinIO connection metadata, project SecretRefs, CSV/Parquet dataset versions,
column schemas, integrity identities and producer-run lineage. [Contract](../../../data-catalog.md).
Registration is metadata-only; object content/connectivity verification is not claimed.

- Clean source snapshot `6566a12db14be0403e3c1d84073da97df6993dab`; external source bundle.
- Backend suite: 779 passed, 13 skipped. Catalog + authorization/grants: 22 passed,
  including a real PostgreSQL two-publisher race with one accepted version.
- Ruff/type checks passed; generated API types, frontend build and 22 frontend tests passed.
- CP + dedicated migration image release gate: all 12 checks passed; schema 0024.
- Go gateway: race/vet/build passed, unfiltered HIGH/CRITICAL vulnerabilities 0.
- CP fixable HIGH/CRITICAL 0, secret findings at all severities 0. Unfiltered scan retains
  55 unfixed Debian findings (53 HIGH, 2 CRITICAL). Runtime still has no scientific stack.
- Local Helm revision 33: API/reconciler/gateway/log services all 2/2 ready.
- Live native Kubernetes SecretRef + API database role checks passed: no credential values
  returned, immutable connection replay/conflict, scoped URI rejection, dataset v1/v2,
  stale-version conflict and referenced-secret deletion protection. Fixtures soft-deleted.
- Cleanup removed 24 obsolete registry manifests and 7 unused node images; host image prune
  reclaimed 1.412 GB and build cache prune reclaimed 3.276 GB. Free disk rose from about
  8.8 GiB to 13 GiB. Current image manifests returned HTTP 200 after registry GC.
  Database/user volumes and unrelated containers were preserved.

Images:

- CP: `localhost:5201/mlp-controlplane@sha256:fbf30c4ff0c122eb374e5366f70049ca3eddf7d194186d2c3e53b91525ec4b19`
- Migration: `localhost:5201/mlp-controlplane-migrate@sha256:f3002f3b79b2d08d552a5c6c358be12b935ced837d10b43eb08f907a8a100b39`
- Gateway: `localhost:5201/mlp-gateway-go@sha256:ed7190f822516aa17a4e383548fd769ef341ba38c1532d9e8ede74ef223c6536`

Full logs, SBOM, Trivy and live records are outside Git at
`/Users/ofk/ml-platform-infra-artifacts/2026-10-08/data-catalog/`.
The checksum list identifies external artifacts; no large JSON reports are vendored.
Batch Inference and the full Data navigation/UI remain the next milestones.
