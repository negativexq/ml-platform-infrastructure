# Data Management UI — local acceptance, 2026-10-08

Global/project Connections and Datasets, typed registration, immutable versions,
paginated history, schema/integrity details, producer links and recorded lineage.
Batch output panels link to exact dataset versions. Connection references appear
in secret usage, including connections beyond the first page. See [contract](../../../data-catalog.md).

- Final backend: **824 passed, 13 skipped** (optional integrations). Frontend: **22 passed**
  and TypeScript/Vite build passed. Typed creation/version preservation and populated
  lineage navigation passed in browser tests, with desktop/mobile captures.
- Audit/lineage/observability regression suite: **61 passed**. Initial live lineage returned
  500 because the observed audit wrapper omitted `latest()`. Explicit delegation fixes it;
  the wrapper now statically satisfies the audit protocol. Both plain and observed UOW
  paths are covered. Initial reports remain outside Git; final checks supersede them.
- Clean final source `3413c558719b0b80f97cd3647b501f866aad4652`: **all 12 image release gates
  passed**, including migration to existing head `0025`, dependency isolation, readiness
  outage/recovery, SPDX SBOM, fixable HIGH/CRITICAL and all-severity secret gates.
- Local acceptance Helm revision **38**, API/Gateway/reconciler/logs 2/2. Real MinIO
  CSV→Parquet and Parquet→CSV outputs retain matching hashes, identifiers and Ridge
  predictions (nine rows, not a scale test). Worker runs were created before the wrapper
  fix; final API re-reads verify pinned input/model/run/output lineage and secret usage.
  Deployed browser checks pass Connections, dataset schema, upstream version navigation
  and run-output navigation, with no JavaScript errors.
- No new schema or dependencies. Control-plane remains **89 distributions** and
  **452,405,760 bytes** of uncompressed Trivy artifact size. Fixable HIGH/CRITICAL **0**,
  secret findings **0**. The full scan retains **55 unfixed Debian findings** (53 HIGH,
  2 CRITICAL); this is not a claim of an empty vulnerability scan.

Lineage only follows recorded same-project relationships, with bounded expansion.
Catalog registration does not fetch objects or verify their contents. Metadata
forms do not expose secret values. No independent artifact browser or column-level
provenance is claimed. Own test project/model/S3 objects were removed after captures.
Unused image/cache cleanup preserves current configured image references and data volumes.

Full source bundles, JSON/SBOM/Trivy reports, test logs, scripts, captures and synthetic
outputs remain outside Git at `/Users/ofk/ml-platform-infra-artifacts/2026-10-08/data-management-ui/`.
[ARTIFACTS.sha256](ARTIFACTS.sha256) records their identities. This verifies the selected
local acceptance cluster, not a production environment.
